"""
core/user_bot/runner.py - 每用户 Bot 主循环（多用户版）

改进点：
  1. 被动平仓检测：优先拉取交易所真实成交记录，废弃推断逻辑
  2. 网络错误分级处理：限频退避、维护暂停、API Key失效立停
  3. 每用户独立 logger（写入 tradelog/{username}.log）
  4. 每用户策略/品种/杠杆配置（DB 优先，fallback 到 config.yaml）
  5. 告警去重：同类错误 5 分钟内只推一次 Telegram
  6. 订单对账：Bot 启动时核对交易所侧条件单是否存在
  7. 连续查询失败计数：网络持续抖动时主动告警

V4.2 重构：
  - 交易所操作已移至 exchange_ops.py
  - 上下文/配置/通知/状态已移至 bot_context.py
  - 主循环拆分为子函数，降低单函数圈复杂度
"""
import time
import math
import traceback
from datetime import datetime

import ccxt
import pandas as pd
import numpy as np

from utils.logger import bot_logger
from utils.config_loader import get_config
from strategy.registry import get_strategy
from execution.db_handler import (
    record_balance, record_trade,
)
from api.routes.keys import get_user_exchange
from risk.risk_manager import RiskManager

# ── 拆分后的子模块 ────────────────────────────────────────────────────────────
from core.user_bot.bot_context import (
    should_alert, empty_state, load_state, save_state, clear_state,
    persist_risk_state, restore_risk_state, resolve_config,
    load_notifier, record_strategy_performance, get_strategy_win_rate,
)
from core.user_bot.exchange_ops import (
    get_swap_usdt, detect_pos_mode, fetch_ohlcv_safe,
    place_algo, cancel_all_algo, live_position_amount,
)


# ── 常量定义 ──────────────────────────────────────────────────────────────────
_ALERT_COOLDOWN_DEFAULT_SEC: int = 300   # 告警去重默认冷却期（5分钟）
_ALERT_COOLDOWN_LONG_SEC:   int = 3600  # 长冷却期（1小时，用于零余额等低频告警）
_ZERO_BALANCE_ALERT_ROUNDS: int = 5     # 连续零余额 N 轮后发出告警
_POS_QUERY_FAIL_ALERT_THRESHOLD: int = 5  # 持仓查询连续失败 N 次后告警
_RATE_LIMIT_MIN_WAIT_SEC:   int = 30    # 限频最短退避秒数
_RATE_LIMIT_MAX_WAIT_SEC:   int = 120   # 限频最长退避秒数
_MAINTENANCE_WAIT_SEC:      int = 120   # 交易所维护等待秒数
_MAINTENANCE_ALERT_COOLDOWN_SEC: int = 1800  # 维护告警冷却期（30分钟）
_NETWORK_ERROR_WAIT_SEC:    int = 15    # 网络错误短暂等待
_GENERIC_ERROR_WAIT_SEC:    int = 10    # 通用错误等待
_KLINE_MIN_LIMIT:           int = 200   # K线拉取最小条数
_KLINE_WARMUP_MULTIPLIER:   int = 2     # 预热期乘数
_KLINE_WARMUP_EXTRA:        int = 10    # 预热期额外条数
_LOW_WIN_RATE_THRESHOLD:  float = 0.35  # 策略降权胜率阈值
_LOW_WIN_RATE_SCALE:      float = 0.6   # 低胜率策略仓位缩放
_LOW_SIGNAL_QUALITY_SKIP:   int = 15    # V5.2: 从25降到15，仅极差信号才完全跳过
_MID_SIGNAL_QUALITY:        int = 35    # V5.2: 从45降到35，中等信号质量阈值
_MIN_SIGNAL_QUALITY_SCALE: float = 0.5  # V5.0: 从0.4提升到0.5，最低信号质量缩放
_SL_TIGHTEN_RATIO:        float = 0.5   # Regime切换时SL收紧比例
_TIME_STOP_FORCE_MULT:    float = 1.5   # 强制时间止损超时倍数
_ERROR_TRACEBACK_LINES:     int = 4     # 错误推送时包含的调用栈行数
_ERROR_MSG_MAX_LEN:         int = 200   # 错误消息最大长度
_SHORT_TB_MAX_LEN:          int = 300   # 短调用栈最大长度


# ── 被动平仓成交价获取（优先拉取真实成交，废弃推断）────────────────────────────

def _fetch_passive_fill_price(ex, symbol: str, state: dict) -> tuple[float, str]:
    """
    从交易所拉取最近的 reduceOnly 成交记录，返回真实成交价。
    若拉取失败，fallback 到 SL/TP 价格（仅作为最后手段，会在 reason 里标注）。
    返回: (fill_price, 来源说明)
    """
    # ── 方法一：从 fetch_my_trades 取最近的减仓成交 ──────────────────────────
    try:
        since_ms = None
        entry_time_str = state.get("entry_time", "")
        if entry_time_str:
            dt = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S")
            since_ms = int(dt.timestamp() * 1000)

        trades = ex.fetch_my_trades(symbol, since=since_ms, limit=20)
        close_trades = [
            t for t in trades
            if (
                t.get("reduceOnly") or
                t.get("info", {}).get("reduceOnly") or
                str(t.get("info", {}).get("side", "")).lower() in ("close_long", "close_short")
            )
        ]
        if close_trades:
            latest = sorted(close_trades, key=lambda x: x.get("timestamp", 0))[-1]
            price = float(latest.get("price") or latest.get("average") or 0)
            if price > 0:
                return price, "交易所真实成交价"
    except Exception:
        pass

    # ── 方法二：从 fetch_orders 找已成交的条件单 ────────────────────────────
    try:
        sl_id = state.get("exchange_order_ids", {}).get("sl_order")
        tp_id = state.get("exchange_order_ids", {}).get("tp_order")
        for order_id in filter(None, [tp_id, sl_id]):
            try:
                order = ex.fetch_order(order_id, symbol, params={"stop": True})
                status = (order.get("status") or "").lower()
                if status in ("closed", "filled"):
                    fill = float(order.get("average") or order.get("price") or 0)
                    if fill > 0:
                        label = "TP成交价" if order_id == tp_id else "SL成交价"
                        return fill, f"条件单{label}（真实）"
            except Exception:
                continue
    except Exception:
        pass

    # ── Fallback：使用 SL/TP 价格作为近似值（标注为估算）────────────────────
    is_long   = state["position_side"] == "long"
    entry     = state["entry_price"]
    active_sl = state["active_sl"]
    active_tp = state["active_tp1"]

    if active_sl > 0 and active_tp > 0:
        if is_long:
            fill = active_tp if active_tp > entry else active_sl
        else:
            fill = active_tp if active_tp < entry else active_sl
    else:
        fill = active_sl if active_sl > 0 else entry

    return fill, "⚠️ 近似估算（建议人工核实）"


# ── 订单对账：Bot 启动时核对 SL/TP 条件单是否仍存在 ─────────────────────────────

def _reconcile_orders(ex, symbol: str, state: dict, logger, notify, tag: str):
    """
    Bot 启动时调用：若本地记录了持仓但条件单可能已失效，重新核查并补挂。
    返回修正后的 state。
    """
    if state["position_amount"] <= 0:
        return state

    sl_id = state.get("exchange_order_ids", {}).get("sl_order")
    tp_id = state.get("exchange_order_ids", {}).get("tp_order")

    def _order_alive(order_id) -> bool:
        if not order_id:
            return False
        try:
            order = ex.fetch_order(order_id, symbol, params={"stop": True})
            status = (order.get("status") or "").lower()
            return status in ("open", "live")
        except Exception:
            return False

    sl_alive = _order_alive(sl_id)
    tp_alive = _order_alive(tp_id)

    if sl_alive and (not tp_id or tp_alive):
        logger.info(f"{tag} 订单对账：SL/TP 条件单均正常")
        return state

    logger.warning(f"{tag} 订单对账：SL alive={sl_alive}, TP alive={tp_alive}，尝试补挂")
    notify(
        f"⚠️ <b>{tag} 订单对账异常</b>\n"
        f"SL存活: {sl_alive} | TP存活: {tp_alive}\n正在补挂条件单..."
    )

    is_long    = state["position_side"] == "long"
    pos_side   = "long" if is_long else "short"
    close_side = "sell" if is_long else "buy"
    amount     = state["position_amount"]

    if not sl_alive:
        new_sl = place_algo(ex, symbol, close_side, amount,
                            state["active_sl"], pos_side, "sl")
        if new_sl:
            state["exchange_order_ids"]["sl_order"] = new_sl.get("id")
            logger.info(f"{tag} SL 条件单已补挂")
        else:
            logger.error(f"{tag} ⚠️ SL 补挂失败！请人工检查！")
            notify(f"🚨 <b>{tag} SL 补挂失败</b>，请立即人工检查持仓！")

    if not tp_alive and state["active_tp1"] > 0:
        new_tp = place_algo(ex, symbol, close_side, amount,
                            state["active_tp1"], pos_side, "tp")
        if new_tp:
            state["exchange_order_ids"]["tp_order"] = new_tp.get("id")
            logger.info(f"{tag} TP 条件单已补挂")

    return state


# ── V2.5: Trailing Stop 追踪止损 ──────────────────────────────────────────────

def _do_trailing_stop(state: dict, current_price: float, is_long: bool,
                      entry: float, df, trigger_mult: float,
                      distance_mult: float, ex, symbol: str,
                      close_side: str, pos_side_str: str,
                      logger, notify, tag: str, user_id: int):
    """
    V2.5 追踪止损逻辑：
    1. 盈利达到 ATR * trigger_mult 后激活追踪
    2. 价格回撤 ATR * distance_mult 时更新 SL 到最优价 - distance

    修改 state（in-place），不直接平仓，只调整交易所 SL 条件单。
    """
    try:
        close = df['close']
        high = df['high']
        low = df['low']
        prev_c = close.shift(1)
        tr = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False).mean()
        current_atr = float(atr.iloc[-2])
        if current_atr <= 0:
            return
    except Exception:
        return

    if is_long:
        profit_points = current_price - entry
    else:
        profit_points = entry - current_price

    trigger_level = current_atr * trigger_mult
    if profit_points < trigger_level:
        state["trailing_stop_active"] = False
        return

    if not state.get("trailing_stop_active"):
        state["trailing_stop_active"] = True
        state["trailing_stop_best_price"] = current_price
        logger.info(f"{tag} ✅ Trailing Stop 已激活，盈利={profit_points:.2f} > 触发={trigger_level:.2f}")

    best = state.get("trailing_stop_best_price", entry)
    if is_long:
        if current_price > best:
            state["trailing_stop_best_price"] = current_price
            best = current_price
    else:
        if current_price < best:
            state["trailing_stop_best_price"] = current_price
            best = current_price

    trail_distance = current_atr * distance_mult
    if is_long:
        new_sl = best - trail_distance
    else:
        new_sl = best + trail_distance

    old_sl = state.get("active_sl", 0.0)
    should_update = False
    if is_long and new_sl > old_sl:
        should_update = True
    elif not is_long and (old_sl == 0 or new_sl < old_sl):
        should_update = True

    if should_update:
        try:
            cancel_all_algo(ex, symbol, logger=logger)
            sl_ord = place_algo(
                ex, symbol, close_side, state["position_amount"],
                new_sl, pos_side_str, "sl"
            )
            tp_price = state.get("active_tp1", 0) or state.get("active_tp2", 0)
            tp_ord = None
            if tp_price > 0:
                tp_ord = place_algo(
                    ex, symbol, close_side, state["position_amount"],
                    tp_price, pos_side_str, "tp"
                )
            state["active_sl"] = new_sl
            state["exchange_order_ids"] = {
                "sl_order": sl_ord.get("id") if sl_ord else None,
                "tp_order": tp_ord.get("id") if tp_ord else None,
            }
            logger.info(
                f"{tag} 📈 Trailing Stop 更新: SL {old_sl:.2f} → {new_sl:.2f} "
                f"(最优价={best:.2f}, 回撤距离={trail_distance:.2f})"
            )
        except Exception as e:
            logger.warning(f"{tag} Trailing Stop 更新SL失败: {e}")


# ── 运行时参数容器 ─────────────────────────────────────────────────────────────

class _RunParams:
    """将主循环中大量传递的参数收集到一个对象，减少函数签名长度。"""
    __slots__ = (
        'user_id', 'username', 'rm', 'stop_ev', 'logger', 'notify', 'tag',
        'ex', 'cfg', 'symbol', 'timeframe', 'leverage', 'contract_size',
        'fee_rate', 'interval', 'risk_pct', 'cached_pos_mode',
        'use_auto', 'selector', 'strategy',
        'trailing_stop_enable', 'trailing_stop_trigger', 'trailing_stop_distance',
        'time_stop_bars', 'time_stop_enable', 'dynamic_position_enable',
    )


# ── 子流程：处理被动平仓 ──────────────────────────────────────────────────────

def _handle_passive_close(
    p: _RunParams, state: dict, live_amt: float, usdt_free: float,
) -> dict | None:
    """
    检测到交易所实际持仓为 0（条件单已触发），处理被动平仓。
    返回更新后的 state；若未触发被动平仓，返回 None。
    """
    if live_amt != 0.0:
        return None

    est_fill, fill_src = _fetch_passive_fill_price(p.ex, p.symbol, state)

    is_long = state["position_side"] == "long"
    gross = (
        (est_fill - state["entry_price"]) if is_long
        else (state["entry_price"] - est_fill)
    ) * state["position_amount"] * p.contract_size
    close_fee = state["position_amount"] * p.contract_size * est_fill * p.fee_rate
    net_pnl = gross - (state["open_fee"] + close_fee)

    record_trade(p.user_id,
                 "sell" if is_long else "buy",
                 est_fill,
                 state["position_amount"],
                 p.symbol, "被动平仓(SL/TP)",
                 net_pnl, fill_src)

    record_strategy_performance(
        p.user_id, state.get("strategy_name", ""), net_pnl
    )

    current_balance = get_swap_usdt(p.ex)
    p.rm.notify_trade_result(net_pnl, current_balance)
    persist_risk_state(p.user_id, p.rm)

    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
    p.logger.info(
        f"{p.tag} 仓位已被动平仓，净盈亏={net_pnl:+.2f}U ({fill_src})"
    )
    _side_label = "多" if is_long else "空"
    p.notify(
        f"{pnl_emoji} <b>{p.username} 平仓（托管单触发）</b>\n"
        f"品种: {p.symbol} | 杠杆: {p.leverage}x | 方向: {_side_label}\n"
        f"入场价: {state['entry_price']:.2f} → 出场价: {est_fill:.2f}\n"
        f"止损: {state['active_sl']:.2f} | 止盈: {state['active_tp1']:.2f}\n"
        f"净盈亏: <b>{net_pnl:+.2f} U</b>\n"
        f"来源: {fill_src}"
    )
    if p.rm.is_fused:
        p.notify(
            f"🚨 <b>{p.username} 风控熔断！</b>\n"
            f"连续亏损 {p.rm.consecutive_losses} 次，Bot 已暂停。\n"
            f"恢复请在控制台点击「恢复熔断」。"
        )
    clear_state(p.user_id)
    return load_state(p.user_id)


# ── 子流程：Regime 切换旧仓管理 ───────────────────────────────────────────────

def _handle_regime_transition(
    p: _RunParams, state: dict, regime_result: dict,
    current_price: float,
) -> dict:
    """
    V4.0 Regime 切换旧仓管理：方向性切换时收紧止损或平掉旧仓。
    返回可能更新后的 state。
    """
    transition_action = regime_result.get("transition_action")
    transition_urgency = regime_result.get("transition_urgency", 0.0)
    if not transition_action or state["position_amount"] <= 0:
        return state

    is_long = state["position_side"] == "long"
    should_close = False

    if transition_action == "close_long" and is_long:
        should_close = True
    elif transition_action == "close_short" and not is_long:
        should_close = True
    elif transition_action == "tighten_sl":
        state = _tighten_sl_on_regime_switch(p, state, is_long)
        return state

    if not should_close:
        return state

    close_side = "sell" if is_long else "buy"
    pos_side = "long" if is_long else "short"
    cancel_all_algo(p.ex, p.symbol, logger=p.logger, notify=p.notify, tag=p.tag)

    close_params = {"tdMode": "cross", "reduceOnly": True}
    if p.cached_pos_mode == "hedge":
        close_params["posSide"] = pos_side

    try:
        order = p.ex.create_order(
            p.symbol, "market", close_side,
            state["position_amount"], params=close_params
        )
        fill_price = float(order.get("average") or order.get("price") or current_price)
        entry = state["entry_price"]
        gross = (
            (fill_price - entry) if is_long
            else (entry - fill_price)
        ) * state["position_amount"] * p.contract_size
        close_fee = state["position_amount"] * p.contract_size * fill_price * p.fee_rate
        net_pnl = gross - (state["open_fee"] + close_fee)

        record_trade(p.user_id, close_side, fill_price,
                     state["position_amount"], p.symbol, "平仓",
                     net_pnl, f"Regime切换: {transition_action}")
        record_strategy_performance(
            p.user_id, state.get("strategy_name", ""), net_pnl
        )
        usdt_free = get_swap_usdt(p.ex)
        p.rm.notify_trade_result(net_pnl, usdt_free)
        persist_risk_state(p.user_id, p.rm)

        pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
        _side_label = "多" if is_long else "空"
        p.logger.info(
            f"{p.tag} ⚡ Regime切换平仓: {transition_action}, "
            f"净盈亏={net_pnl:+.2f}U, 紧急度={transition_urgency:.2f}"
        )
        p.notify(
            f"{pnl_emoji} <b>{p.username} Regime切换平仓</b>\n"
            f"品种: {p.symbol} | 方向: {_side_label}\n"
            f"入场价: {entry:.2f} → 出场价: {fill_price:.2f}\n"
            f"净盈亏: <b>{net_pnl:+.2f} U</b>\n"
            f"操作: {transition_action} | 紧急度: {transition_urgency:.0%}"
        )
        clear_state(p.user_id)
        state = load_state(p.user_id)
    except Exception as e:
        p.logger.error(f"{p.tag} ⚠️ Regime切换平仓失败: {e}")
        p.notify(
            f"⚠️ <b>{p.username} Regime切换平仓失败</b>\n"
            f"操作: {transition_action}\n错误: {str(e)[:200]}"
        )

    return state


def _tighten_sl_on_regime_switch(p: _RunParams, state: dict, is_long: bool) -> dict:
    """Regime 切换时收紧止损：将 SL 向入场价方向移动。"""
    if state["active_sl"] <= 0 or state["entry_price"] <= 0:
        return state

    old_sl = state["active_sl"]
    entry = state["entry_price"]

    if is_long:
        new_sl = old_sl + abs(entry - old_sl) * _SL_TIGHTEN_RATIO
        should_update = new_sl > old_sl
        close_side, pos_side = "sell", "long"
    else:
        new_sl = old_sl - abs(old_sl - entry) * _SL_TIGHTEN_RATIO
        should_update = new_sl < old_sl
        close_side, pos_side = "buy", "short"

    if not should_update:
        return state

    cancel_all_algo(p.ex, p.symbol, logger=p.logger, notify=p.notify, tag=p.tag)
    sl_ord = place_algo(p.ex, p.symbol, close_side, state["position_amount"],
                        new_sl, pos_side, "sl")
    tp_price = state.get("active_tp1", 0)
    tp_ord = None
    if tp_price > 0:
        tp_ord = place_algo(p.ex, p.symbol, close_side, state["position_amount"],
                            tp_price, pos_side, "tp")
    state["active_sl"] = new_sl
    state["exchange_order_ids"] = {
        "sl_order": sl_ord.get("id") if sl_ord else None,
        "tp_order": tp_ord.get("id") if tp_ord else None,
    }
    save_state(p.user_id, state)
    p.logger.info(f"{p.tag} ⚡ Regime切换收紧SL: {old_sl:.2f} → {new_sl:.2f}")
    return state


# ── 子流程：开仓 ──────────────────────────────────────────────────────────────

def _handle_open_position(
    p: _RunParams, state: dict, signal: dict,
    current_price: float, usdt_free: float, df,
) -> dict | None:
    """
    空仓时根据信号开仓。
    返回更新后的 state；若未开仓则返回 None。

    V5.1: 拒绝理由透明化 — 所有跳过开仓的分支都输出具体数值对比。
    """
    action = signal["action"]
    if state["position_amount"] != 0 or action not in ("BUY", "SELL"):
        return None

    reason    = signal["reason"]
    target_sl = signal["sl"]
    target_tp = signal["tp1"]

    price_risk = abs(current_price - target_sl) * p.contract_size
    fee_risk   = (current_price + abs(target_sl)) * p.contract_size * p.fee_rate
    risk_per   = price_risk + fee_risk
    if risk_per <= 0:
        p.logger.warning(f"{p.tag} 风险计算异常（risk_per={risk_per}），跳过")
        return None

    # V4.0: 动态风险比例
    effective_risk = p.rm.get_effective_risk_pct(p.risk_pct)
    contracts = int(math.floor(usdt_free * effective_risk / risk_per))

    if effective_risk != p.risk_pct:
        p.logger.info(
            f"{p.tag} 📊 动态风险: base={p.risk_pct*100:.2f}% → "
            f"effective={effective_risk*100:.2f}% "
            f"(回撤×{p.rm.drawdown_scale:.2f} "
            f"equity×{p.rm.equity_curve_scale:.2f} "
            f"regime×{p.rm.regime_scale:.2f})"
        )

    # V4.0: 信号质量仓位缩放
    contracts = _apply_signal_quality_scaling(p, contracts)
    if contracts == 0:
        return None  # 信号质量太低，跳过（日志已在子函数中输出）

    # V1.5: 策略过渡期半仓
    if p.use_auto and p.selector is not None and p.selector.in_transition:
        contracts = max(1, contracts // 2)
        p.logger.info(f"{p.tag} ⚠️ 策略过渡期，半仓试探: {contracts} 张")

    # max_trade_amount 金额上限换算为张数
    denom = current_price * p.contract_size / p.leverage
    max_contracts_by_amount = int(math.floor(
        p.rm.max_trade_amount / denom
    )) if denom > 0 else contracts
    contracts = min(contracts, max_contracts_by_amount)

    if contracts < 1:
        p.logger.info(
            f"{p.tag} ❌ 拒绝开仓: 仓位计算 <1 张 "
            f"(余额={usdt_free:.2f}U, risk_per={risk_per:.4f}, "
            f"effective_risk={effective_risk*100:.2f}%, SL距离={abs(current_price - target_sl):.2f})"
        )
        return None

    if not p.rm.check_order(p.symbol, action.lower(), contracts):
        # V5.1: 风控拒绝透明化
        p.logger.info(
            f"{p.tag} ❌ 拒绝开仓（风控）: {action} {contracts}张 {p.symbol} "
            f"| 熔断={p.rm.is_fused}, 连亏={p.rm.consecutive_losses}, "
            f"日亏={getattr(p.rm, '_daily_loss', 0):.2f}U, "
            f"日交易次数={getattr(p.rm, '_daily_trade_count', 0)}"
        )
        return None

    open_side  = "buy"  if action == "BUY"  else "sell"
    pos_side   = "long" if action == "BUY"  else "short"
    close_side = "sell" if action == "BUY"  else "buy"

    open_params = {"tdMode": "cross"}
    if p.cached_pos_mode == "hedge":
        open_params["posSide"] = pos_side

    order = p.ex.create_order(
        p.symbol, "market", open_side, contracts, params=open_params
    )
    if not order:
        p.logger.error(f"{p.tag} 开仓失败")
        return None

    fill_price = float(order.get("average") or order.get("price") or current_price)

    tp1_contracts = contracts // 2 if contracts >= 2 else contracts
    sl_ord = place_algo(p.ex, p.symbol, close_side, contracts,
                        target_sl, pos_side, "sl")
    tp_ord = place_algo(p.ex, p.symbol, close_side, tp1_contracts,
                        target_tp, pos_side, "tp")

    if not sl_ord:
        # SL 挂单失败 → 紧急平仓
        _emergency_rollback(p, close_side, pos_side, contracts, state)
        return load_state(p.user_id)

    notional    = contracts * p.contract_size * fill_price
    open_fee    = notional * p.fee_rate
    margin_used = notional / p.leverage

    state.update({
        "position_amount":  contracts,
        "position_side":    pos_side,
        "entry_price":      fill_price,
        "active_sl":        target_sl,
        "active_tp1":       target_tp,
        "active_tp2":       signal.get("tp2", 0.0),
        "margin_used":      margin_used,
        "open_fee":         open_fee,
        "strategy_name":    p.strategy.name,
        "signal_reason":    reason,
        "entry_time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exchange_order_ids": {
            "sl_order": sl_ord.get("id") if sl_ord else None,
            "tp_order": tp_ord.get("id") if tp_ord else None,
        },
    })
    save_state(p.user_id, state)
    record_trade(p.user_id, open_side, fill_price, contracts,
                 p.symbol, "开仓", 0.0, reason)

    emoji = "🟢" if action == "BUY" else "🔴"
    p.notify(
        f"{emoji} <b>{p.username} {'开多' if action=='BUY' else '开空'}</b>\n"
        f"品种: {p.symbol} | 杠杆: {p.leverage}x\n"
        f"入场价: {fill_price:.2f} | 数量: {contracts}张\n"
        f"止损: {target_sl:.2f} | 止盈: {target_tp:.2f}\n"
        f"保证金: ~{margin_used:.2f} U | 风险: ~{usdt_free * p.risk_pct:.2f} U\n"
        f"策略: {p.strategy.name} | 原因: {reason}"
    )
    return state


def _apply_signal_quality_scaling(p: _RunParams, contracts: int) -> int:
    """根据信号质量和策略胜率缩放仓位，返回 0 表示跳过开仓。"""
    if not (p.dynamic_position_enable and p.use_auto and p.selector is not None):
        return contracts

    signal_quality = getattr(p.selector, '_signal_quality_score', 100)
    quality_detail = getattr(p.selector, 'last_signal_quality', {})
    if signal_quality < _LOW_SIGNAL_QUALITY_SKIP:
        # V5.1: 拒绝理由透明化 — 输出信号质量明细
        p.logger.info(
            f"{p.tag} ❌ 拒绝开仓（信号质量不足）: "
            f"信号分 {signal_quality:.0f} < 门槛 {_LOW_SIGNAL_QUALITY_SKIP} | "
            f"明细: tech={quality_detail.get('tech', 0)}, "
            f"extra={quality_detail.get('extra', 0)}, "
            f"news={quality_detail.get('news', 0)}, "
            f"mtf={quality_detail.get('mtf', 0)}, "
            f"consistency={quality_detail.get('consistency', 0)}, "
            f"volatility={quality_detail.get('volatility', 0)} | "
            f"缺失源={quality_detail.get('unknown_sources', [])}"
        )
        return 0
    elif signal_quality < _MID_SIGNAL_QUALITY:
        sq_scale = max(_MIN_SIGNAL_QUALITY_SCALE, signal_quality / 100.0)
        contracts = max(1, int(contracts * sq_scale))
        p.logger.info(
            f"{p.tag} 📊 信号质量={signal_quality:.0f}（中等, 门槛={_MID_SIGNAL_QUALITY}），"
            f"仓位缩放={sq_scale:.2f} → {contracts} 张"
        )

    # V2.5: 策略绩效降权
    strat_wr = get_strategy_win_rate(p.user_id, p.strategy.name)
    if strat_wr < _LOW_WIN_RATE_THRESHOLD:
        contracts = max(1, int(contracts * _LOW_WIN_RATE_SCALE))
        p.logger.info(
            f"{p.tag} ⚠️ 策略 {p.strategy.name} 近期胜率低 "
            f"({strat_wr:.0%})，降权仓位: {contracts} 张"
        )
    return contracts


def _emergency_rollback(
    p: _RunParams, close_side: str, pos_side: str,
    contracts: int, state: dict,
):
    """SL 挂单失败后紧急平仓。"""
    p.logger.error(f"{p.tag} 🚨 SL 挂单失败！回滚平仓！")
    p.notify(f"🚨 <b>{p.username}</b> SL 挂单失败，已紧急平仓！")
    cancel_all_algo(p.ex, p.symbol, logger=p.logger, notify=p.notify, tag=p.tag)

    try:
        p.ex.create_order(
            p.symbol, "market", close_side, contracts,
            params={
                "tdMode": "cross", "reduceOnly": True,
                **({"posSide": pos_side} if p.cached_pos_mode == "hedge" else {}),
            }
        )
        p.logger.info(f"{p.tag} 紧急平仓已执行")
    except Exception as rollback_err:
        p.logger.error(f"{p.tag} 🚨🚨 紧急平仓也失败！请立即人工处理！{rollback_err}")
        p.notify(
            f"🚨🚨 <b>{p.username} 紧急平仓失败！</b>\n"
            f"SL 挂单失败且平仓指令也报错，请立即人工检查持仓！\n"
            f"错误：{str(rollback_err)[:200]}"
        )
        state["position_side"]   = "unknown_rollback_failed"
        state["position_amount"] = contracts
        save_state(p.user_id, state)


# ── 子流程：持仓管理（TP1分批、反转、追踪止损、时间止损）──────────────────────

def _handle_active_position(
    p: _RunParams, state: dict, signal: dict,
    current_price: float, df,
) -> dict | None:
    """
    有仓位时的管理逻辑。
    返回更新后的 state；若仅正常持仓不需要特殊处理则返回 None。
    """
    if state["position_amount"] <= 0:
        return None

    is_long      = state["position_side"] == "long"
    close_side   = "sell" if is_long else "buy"
    pos_side_str = "long" if is_long else "short"
    entry        = state["entry_price"]
    action       = signal["action"]
    reason       = signal["reason"]

    # ── TP1 分批止盈 ──────────────────────────────────────────────────────
    if not state.get("has_taken_partial_profit") and state["position_amount"] >= 2:
        partial_result = _check_tp1_partial(p, state, is_long, close_side, pos_side_str, entry)
        if partial_result is not None:
            return partial_result

    # ── 判断是否需要平仓 ──────────────────────────────────────────────────
    close_reason = ""
    if is_long and action == "SELL":
        close_reason = f"策略反转: {reason}"
    if not is_long and action == "BUY":
        close_reason = f"策略反转: {reason}"

    # ── V2.5: Trailing Stop ───────────────────────────────────────────────
    if p.trailing_stop_enable and not close_reason and state["position_amount"] > 0:
        _do_trailing_stop(
            state, current_price, is_long, entry, df,
            p.trailing_stop_trigger, p.trailing_stop_distance,
            p.ex, p.symbol, close_side, pos_side_str,
            p.logger, p.notify, p.tag, p.user_id,
        )

    # ── V2.5: 时间止损 ───────────────────────────────────────────────────
    if p.time_stop_enable and not close_reason and state["position_amount"] > 0:
        close_reason = _check_time_stop(state, is_long, entry, current_price, p)

    if not close_reason:
        return None

    # ── 执行平仓 ──────────────────────────────────────────────────────────
    return _execute_close(p, state, is_long, close_side, pos_side_str,
                          entry, current_price, close_reason)


def _check_tp1_partial(
    p: _RunParams, state: dict, is_long: bool,
    close_side: str, pos_side_str: str, entry: float,
) -> dict | None:
    """检测 TP1 是否已触发（交易所实际仓位减少），执行分批止盈。"""
    total_amt = state["position_amount"]
    live_amt_check = live_position_amount(p.ex, p.symbol)
    tp1_amt = total_amt // 2
    remaining = total_amt - tp1_amt

    if not (0 < live_amt_check <= remaining):
        return None

    p.logger.info(f"{p.tag} TP1已触发，移SL至保本，挂TP2")
    cancel_all_algo(p.ex, p.symbol, logger=p.logger, notify=p.notify, tag=p.tag)

    breakeven_sl = entry
    new_sl = place_algo(p.ex, p.symbol, close_side, live_amt_check,
                        breakeven_sl, pos_side_str, "sl")
    tp2_price = state.get("active_tp2", 0.0)
    new_tp = None
    if tp2_price > 0:
        new_tp = place_algo(p.ex, p.symbol, close_side, live_amt_check,
                            tp2_price, pos_side_str, "tp")

    state["has_taken_partial_profit"] = True
    state["has_moved_to_breakeven"]   = True
    state["position_amount"]          = live_amt_check
    state["active_sl"]                = breakeven_sl
    state["exchange_order_ids"] = {
        "sl_order": new_sl.get("id") if new_sl else None,
        "tp_order": new_tp.get("id") if new_tp else None,
    }
    save_state(p.user_id, state)

    p.notify(
        f"✂️ <b>{p.username} TP1已触发，分批止盈</b>\n"
        f"剩余仓位: {live_amt_check}张 | SL已移至保本: {breakeven_sl:.2f}\n"
        f"TP2目标: {tp2_price:.2f}"
    )
    return state


def _check_time_stop(
    state: dict, is_long: bool, entry: float,
    current_price: float, p: _RunParams,
) -> str:
    """检查时间止损条件，返回平仓原因（空字符串 = 不平仓）。"""
    state["entry_bar_count"] = state.get("entry_bar_count", 0) + 1
    bar_count = state["entry_bar_count"]

    close_reason = ""
    if bar_count >= p.time_stop_bars:
        if is_long and current_price >= entry:
            close_reason = f"时间止损: 持仓{bar_count}根K线"
        elif not is_long and current_price <= entry:
            close_reason = f"时间止损: 持仓{bar_count}根K线"
        elif bar_count >= p.time_stop_bars * _TIME_STOP_FORCE_MULT:
            close_reason = f"强制时间止损: 持仓{bar_count}根K线"

    save_state(p.user_id, state)
    return close_reason


def _execute_close(
    p: _RunParams, state: dict, is_long: bool,
    close_side: str, pos_side_str: str,
    entry: float, current_price: float, close_reason: str,
) -> dict:
    """执行主动平仓（策略反转 / 时间止损等）。"""
    cancel_all_algo(p.ex, p.symbol, logger=p.logger, notify=p.notify, tag=p.tag)

    close_params = {"tdMode": "cross", "reduceOnly": True}
    if p.cached_pos_mode == "hedge":
        close_params["posSide"] = pos_side_str

    order = p.ex.create_order(
        p.symbol, "market", close_side,
        state["position_amount"], params=close_params
    )
    fill_price = float(
        order.get("average") or order.get("price") or current_price
    )

    gross = (
        (fill_price - entry) if is_long
        else (entry - fill_price)
    ) * state["position_amount"] * p.contract_size
    close_fee = state["position_amount"] * p.contract_size * fill_price * p.fee_rate
    net_pnl   = gross - (state["open_fee"] + close_fee)

    record_trade(p.user_id, close_side, fill_price,
                 state["position_amount"], p.symbol, "平仓",
                 net_pnl, close_reason)
    record_strategy_performance(
        p.user_id, state.get("strategy_name", ""), net_pnl
    )

    usdt_free = get_swap_usdt(p.ex)
    p.rm.notify_trade_result(net_pnl, usdt_free)
    persist_risk_state(p.user_id, p.rm)

    if p.rm.is_fused:
        p.notify(
            f"🚨 <b>{p.username} 风控熔断！</b>\n"
            f"连续亏损 {p.rm.consecutive_losses} 次，Bot 已暂停。\n"
            f"恢复请在控制台点击「恢复熔断」。"
        )

    clear_state(p.user_id)
    new_state = load_state(p.user_id)

    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
    _side_label = "多" if is_long else "空"
    p.notify(
        f"{pnl_emoji} <b>{p.username} 平仓</b>\n"
        f"品种: {p.symbol} | 杠杆: {p.leverage}x | 方向: {_side_label}\n"
        f"入场价: {entry:.2f} → 出场价: {fill_price:.2f}\n"
        f"净盈亏: <b>{net_pnl:+.2f} U</b>\n"
        f"原因: {close_reason}"
    )
    return new_state


# ── K 线拉取 & DataFrame 构建 ─────────────────────────────────────────────────

def _fetch_kline_df(p: _RunParams, strategy):
    """拉取 K 线并构建 DataFrame，返回 (df, current_price)。"""
    kline_limit = max(
        _KLINE_MIN_LIMIT,
        getattr(strategy, "warmup_bars", 50) * _KLINE_WARMUP_MULTIPLIER + _KLINE_WARMUP_EXTRA
    )
    ohlcv = fetch_ohlcv_safe(p.ex, p.symbol, p.timeframe, limit=kline_limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    current_price = float(df.iloc[-1]["close"])
    return df, current_price


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run_user_bot(bot_state, override_strategy: str = None):
    """
    :param bot_state: UserBotState 实例
                      属性：user_id, username, risk_manager, stop_event
    :param override_strategy: 启动时临时覆盖策略名（不保存到 DB），None 则用用户配置
    """
    p = _RunParams()
    p.user_id  = bot_state.user_id
    p.username = bot_state.username
    p.rm       = bot_state.risk_manager
    p.stop_ev  = bot_state.stop_event

    from utils.logger import get_user_logger
    p.logger = get_user_logger(p.username)
    p.tag    = f"[{p.username}]"

    # ── 加载有效配置 ──────────────────────────────────────────────────────
    p.cfg = resolve_config(p.user_id)

    if override_strategy:
        p.cfg["strategy_name"] = override_strategy.upper()

    p.symbol        = p.cfg["symbol"]
    p.timeframe     = p.cfg["timeframe"]
    p.leverage      = p.cfg["leverage"]
    p.contract_size = p.cfg["contract_size"]
    p.fee_rate      = p.cfg["taker_fee_rate"]
    p.interval      = p.cfg["check_interval"]
    p.risk_pct      = p.cfg["risk_pct"]

    # ── 策略初始化 ────────────────────────────────────────────────────────
    global_cfg    = get_config()
    strategy_name = p.cfg["strategy_name"]
    p.use_auto    = (strategy_name.upper() == "AUTO")

    p.selector = None
    if p.use_auto:
        from strategy.selector import MarketRegimeSelector
        p.selector = MarketRegimeSelector(global_cfg)
        p.logger.info(f"{p.tag} 自动策略选择器已启用")
        try:
            from core.user_bot import manager as _mgr
            _mgr.register_user_selector(p.user_id, p.selector)
        except Exception:
            pass

    p.strategy = get_strategy(
        "PA_5S" if p.use_auto else strategy_name,
        **({} if p.use_auto else p.cfg["strategy_params"])
    )

    # 同步风控参数
    p.rm.max_consecutive_losses = p.cfg["max_consecutive_losses"]
    p.rm.daily_loss_limit_pct   = p.cfg["daily_loss_limit_pct"]
    p.rm.max_trade_amount       = p.cfg["max_trade_amount"]

    try:
        p.ex = get_user_exchange(p.user_id)
    except Exception as e:
        p.logger.error(f"{p.tag} 无法构建 exchange：{e}")
        return

    restore_risk_state(p.user_id, p.rm)
    p.logger.info(
        f"{p.tag} 风控状态已恢复：连亏={p.rm._consecutive_losses}次，熔断={p.rm.is_fused}"
    )

    # ── 启动时主动查询余额作为日内基准 ────────────────────────────────────
    try:
        startup_balance = get_swap_usdt(p.ex)
        if startup_balance > 0:
            p.rm.set_daily_start_balance(startup_balance)
            record_balance(p.user_id, startup_balance)
    except Exception:
        pass

    p.notify = load_notifier(p.user_id, p.username, p.logger)

    p.logger.info(f"{p.tag} Bot 启动，策略={p.cfg['strategy_name']}，品种={p.symbol}")

    # V2.5: 高级风控参数
    v25_cfg = global_cfg.get("risk_v25", {})
    p.trailing_stop_enable   = v25_cfg.get("trailing_stop_enable", True)
    p.trailing_stop_trigger  = v25_cfg.get("trailing_stop_trigger", 0.5)
    p.trailing_stop_distance = v25_cfg.get("trailing_stop_distance", 0.8)
    p.time_stop_bars         = v25_cfg.get("time_stop_bars", 24)
    p.time_stop_enable       = v25_cfg.get("time_stop_enable", True)
    p.dynamic_position_enable = v25_cfg.get("dynamic_position_enable", True)

    p.notify(
        f"🚀 <b>{p.username} 的 Bot 已启动</b>\n"
        f"策略: {p.cfg['strategy_name']} | 品种: {p.symbol} | 杠杆: {p.leverage}x"
    )

    # 设置杠杆
    try:
        p.ex.set_leverage(p.leverage, p.symbol, params={"mgnMode": "cross"})
        p.logger.info(f"{p.tag} 杠杆已设为 {p.leverage}x")
    except Exception as e:
        msg = str(e)
        if 'NoneType' in msg and '+' in msg:
            p.logger.warning(f"{p.tag} 设置杠杆遇到 OKX/ccxt 兼容异常（已跳过）: {msg}")
        else:
            p.logger.warning(f"{p.tag} 设置杠杆失败（可能已设置）: {e}")

    state = load_state(p.user_id)
    p.cached_pos_mode = detect_pos_mode(p.ex)
    p.logger.info(f"{p.tag} 持仓模式: {p.cached_pos_mode}")

    # ── 启动时订单对账 ────────────────────────────────────────────────────
    state = _reconcile_orders(p.ex, p.symbol, state, p.logger, p.notify, p.tag)
    if state["position_amount"] > 0:
        save_state(p.user_id, state)

    _current_date = datetime.now().strftime('%Y-%m-%d')
    _pos_query_fail_count = 0
    _zero_bal_count = 0
    _rate_limit_until = 0.0

    while not p.stop_ev.is_set():
        try:
            # ── 限频退避 ──────────────────────────────────────────────────
            now_ts = time.time()
            if now_ts < _rate_limit_until:
                wait_sec = int(_rate_limit_until - now_ts)
                p.logger.warning(f"{p.tag} 限频退避中，等待 {wait_sec}s")
                p.stop_ev.wait(min(wait_sec, p.interval))
                continue

            # ── 跨日检测 ──────────────────────────────────────────────────
            today = datetime.now().strftime('%Y-%m-%d')
            if today != _current_date:
                _current_date = today
                current_bal = get_swap_usdt(p.ex)
                p.rm.reset_daily(current_bal if current_bal > 0 else None)
                persist_risk_state(p.user_id, p.rm)
                p.logger.info(f"{p.tag} 跨日重置日亏状态，新日期: {today}")
                p.notify(
                    f"📅 <b>{p.username}</b> 新的一天开始，日内风控已重置。\n"
                    f"起始余额: {current_bal:.2f} U"
                )

            # ── 熔断检查 ──────────────────────────────────────────────────
            if p.rm.is_fused:
                p.logger.warning(f"{p.tag} 🚨 风控熔断中，跳过本轮")
                p.stop_ev.wait(p.interval)
                continue

            # ── 余额 ─────────────────────────────────────────────────────
            usdt_free = get_swap_usdt(p.ex)
            if usdt_free > 0:
                record_balance(p.user_id, usdt_free)
                _zero_bal_count = 0
            else:
                _zero_bal_count += 1
                p.logger.warning(f"{p.tag} ⚠️ 余额获取为 0（连续第 {_zero_bal_count} 次）")
                if _zero_bal_count == _ZERO_BALANCE_ALERT_ROUNDS:
                    alert_key = f"{p.user_id}:zero_balance"
                    if should_alert(alert_key, _ALERT_COOLDOWN_LONG_SEC):
                        p.notify(
                            f"⚠️ <b>{p.username}</b> 连续 {_zero_bal_count} 轮余额为 0，"
                            f"无法开仓。\n请检查：\n"
                            f"• 模拟盘/实盘 Key 是否匹配\n"
                            f"• 合约账户是否有 USDT\n"
                            f"• API Key 权限是否包含「读取」"
                        )

            # ── 仓位核对（检测被动平仓）────────────────────────────────────
            if state["position_amount"] > 0:
                live_amt = live_position_amount(p.ex, p.symbol, logger=p.logger, tag=p.tag)

                if live_amt == -1.0:
                    _pos_query_fail_count += 1
                    p.logger.warning(
                        f"{p.tag} 持仓查询失败（连续第{_pos_query_fail_count}次）"
                    )
                    if _pos_query_fail_count >= _POS_QUERY_FAIL_ALERT_THRESHOLD:
                        alert_key = f"{p.user_id}:pos_query_fail"
                        if should_alert(alert_key, _ALERT_COOLDOWN_DEFAULT_SEC * 2):
                            p.notify(
                                f"⚠️ <b>{p.username}</b> 持仓查询连续失败 "
                                f"{_pos_query_fail_count} 次，请检查网络或 API 状态"
                            )
                    p.stop_ev.wait(p.interval)
                    continue

                _pos_query_fail_count = 0

                new_state = _handle_passive_close(p, state, live_amt, usdt_free)
                if new_state is not None:
                    state = new_state
                    p.stop_ev.wait(p.interval)
                    continue

            # ── K 线 & 信号 ───────────────────────────────────────────────
            df, current_price = _fetch_kline_df(p, p.strategy)

            # ── AUTO 模式：每轮评估市场状态 ───────────────────────────────
            if p.use_auto and p.selector is not None:
                new_strategy, regime_result = p.selector.get_strategy(df, p.symbol)

                # V4.0: 风控 Regime 感知
                p.rm.set_regime_context(
                    regime_result.get("regime", "unknown"),
                    regime_result.get("confidence", 0.5),
                    regime_result.get("transition_action"),
                )

                # V4.0: Regime 切换旧仓管理
                state = _handle_regime_transition(p, state, regime_result, current_price)

                # V5.2: WAIT 不再硬跳过开仓
                # 如果信号质量 >= 15 且策略给出了信号，允许降仓尝试开仓
                # 只有信号质量极低（< 15）时才真正跳过
                if regime_result.get("regime") == "wait" and state["position_amount"] == 0:
                    sq = regime_result.get("signal_quality", 0)
                    if sq < _LOW_SIGNAL_QUALITY_SKIP:
                        p.logger.info(
                            f"{p.tag} 📋 WAIT 观望（质量={sq:.0f} < {_LOW_SIGNAL_QUALITY_SKIP}），"
                            f"跳过开仓 ({regime_result['reason']})"
                        )
                        p.stop_ev.wait(p.interval)
                        continue
                    else:
                        p.logger.info(
                            f"{p.tag} 📋 WAIT 但信号质量={sq:.0f}≥{_LOW_SIGNAL_QUALITY_SKIP}，"
                            f"允许降仓尝试 ({regime_result['reason']})"
                        )

                # V4.0: 日内交易次数检查
                if p.rm.daily_trades_exhausted and state["position_amount"] == 0:
                    p.logger.info(f"{p.tag} 📋 日内交易次数已达上限，跳过开仓")
                    p.stop_ev.wait(p.interval)
                    continue

                if new_strategy is not None:
                    old_name = p.strategy.name
                    p.strategy = new_strategy
                    df, current_price = _fetch_kline_df(p, p.strategy)
                    p.logger.info(
                        f"{p.tag} 策略热切换: {old_name} → {p.strategy.name} "
                        f"({regime_result['reason']})"
                    )
                    if state["position_amount"] == 0:
                        p.notify(
                            f"🔄 <b>{p.username} 策略已切换</b>\n"
                            f"新策略: <b>{p.strategy.name}</b>\n"
                            f"市场状态: {regime_result['regime'].upper()}\n"
                            f"技术面: {regime_result['tech_regime']} | "
                            f"新闻面: {regime_result['news_regime']}\n"
                            f"置信度: {regime_result['confidence']:.0%}\n"
                            f"信号质量: {regime_result.get('signal_quality', 0):.0f}/100"
                        )

            signal = p.strategy.generate_signal(df)

            p.logger.info(
                f"{p.tag} 信号={signal['action']} 价格={current_price:.2f} "
                f"仓位={state['position_side'] or '空仓'}/{state['position_amount']}张"
            )

            # ══════════════ 空仓 → 开仓 ══════════════════════════════════
            open_result = _handle_open_position(
                p, state, signal, current_price, usdt_free, df
            )
            if open_result is not None:
                state = open_result

            # ══════════════ 有仓 → 持仓管理 ══════════════════════════════
            else:
                active_result = _handle_active_position(
                    p, state, signal, current_price, df
                )
                if active_result is not None:
                    state = active_result

        except ccxt.RateLimitExceeded as e:
            wait_sec = min(
                _RATE_LIMIT_MAX_WAIT_SEC,
                _RATE_LIMIT_MIN_WAIT_SEC * (2 ** getattr(e, 'retry_after', 1))
            )
            _rate_limit_until = time.time() + wait_sec
            p.logger.warning(f"{p.tag} API 限频，退避 {wait_sec}s")
            alert_key = f"{p.user_id}:rate_limit"
            if should_alert(alert_key, _ALERT_COOLDOWN_DEFAULT_SEC * 2):
                p.notify(f"⏱️ <b>{p.username}</b> API 限频，已自动退避 {wait_sec}s")
            p.stop_ev.wait(min(wait_sec, p.interval))
            continue

        except ccxt.AuthenticationError as e:
            p.logger.error(f"{p.tag} 🚨 API Key 认证失败，Bot 停止: {e}")
            p.notify(
                f"🚨 <b>{p.username} API Key 认证失败，Bot 已停止</b>\n"
                f"请检查：\n"
                f"• <b>模拟盘/实盘是否一致</b>：在 OKX 模拟盘创建的 Key 需勾选「使用模拟盘」，实盘 Key 勿勾选。\n"
                f"• <b>IP 白名单</b>：若 OKX 绑定了 IP，请将本服务器 IP 加入白名单。\n"
                f"在设置页重新配置后，可先点「验证 API Key」再启动 Bot。"
            )
            p.stop_ev.set()
            return

        except ccxt.ExchangeNotAvailable as e:
            p.logger.warning(f"{p.tag} 交易所维护中，等待 {_MAINTENANCE_WAIT_SEC}s: {e}")
            alert_key = f"{p.user_id}:maintenance"
            if should_alert(alert_key, _MAINTENANCE_ALERT_COOLDOWN_SEC):
                p.notify(f"🔧 <b>{p.username}</b> 交易所维护中，Bot 暂停，将自动恢复")
            p.stop_ev.wait(_MAINTENANCE_WAIT_SEC)
            continue

        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            p.logger.warning(f"{p.tag} 网络异常，等待重试: {e}")
            alert_key = f"{p.user_id}:network_error"
            if should_alert(alert_key, _ALERT_COOLDOWN_DEFAULT_SEC):
                p.notify(f"📡 <b>{p.username}</b> 网络异常，Bot 将自动重试")
            p.stop_ev.wait(_NETWORK_ERROR_WAIT_SEC)
            continue

        except Exception as e:
            tb_str = traceback.format_exc()
            p.logger.error(f"{p.tag} 运行异常: {e}\n{tb_str}")
            alert_key = f"{p.user_id}:generic_error:{type(e).__name__}"
            if should_alert(alert_key, _ALERT_COOLDOWN_DEFAULT_SEC):
                tb_lines = tb_str.strip().split('\n')
                short_tb = '\n'.join(tb_lines[-_ERROR_TRACEBACK_LINES:]) if len(tb_lines) > _ERROR_TRACEBACK_LINES else tb_str
                p.notify(
                    f"⚠️ <b>{p.username} Bot 异常</b>\n"
                    f"{str(e)[:_ERROR_MSG_MAX_LEN]}\n\n"
                    f"<pre>{short_tb[:_SHORT_TB_MAX_LEN]}</pre>"
                )
            p.stop_ev.wait(_GENERIC_ERROR_WAIT_SEC)
            continue

        p.stop_ev.wait(p.interval)

    p.logger.info(f"{p.tag} Bot 已停止")
    p.notify(f"🛑 <b>{p.username} 的 Bot 已停止</b>")
    try:
        from core.user_bot import manager as _mgr
        _mgr.unregister_user_selector(p.user_id)
    except Exception:
        pass
