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
"""
import time
import math
import json
from datetime import datetime

from utils.logger import bot_logger, get_user_logger
from utils.notifier import make_notifier, send_telegram_msg
from utils.config_loader import get_config
from strategy.registry import get_strategy
from execution.db_handler import (get_conn, record_balance, record_trade,
                                   save_risk_state, load_risk_state,
                                   load_tg_config, load_user_config)
from api.auth.crypto import decrypt
from api.routes.keys import get_user_exchange
from risk.risk_manager import RiskManager


# ── 每用户 Telegram 通知 ────────────────────────────────────────────────────────

def _load_user_notifier(user_id: int, logger=None):
    """
    加载用户的 Telegram 通知函数。
    返回 (notifier_func, error_msg)：
      - 成功：(func, None)
      - 未配置：(None, "not_configured")
      - 解密失败：(None, "decrypt_error: ...")
    """
    try:
        raw = load_tg_config(user_id)
        if not raw["tg_bot_token_enc"] or not raw["tg_chat_id_enc"]:
            return None, "not_configured"
        token   = decrypt(raw["tg_bot_token_enc"])
        chat_id = decrypt(raw["tg_chat_id_enc"])
        if not token or not chat_id:
            return None, "decrypted_empty"
        notifier = make_notifier(token, chat_id)
        return notifier, None
    except Exception as e:
        err = f"decrypt_error: {e}"
        if logger:
            logger.error(f"[user_id={user_id}] Telegram 配置加载失败: {err}")
        return None, err


# ── 告警去重（同类错误 5 分钟内只推一次）────────────────────────────────────────

_last_alert_time: dict = {}   # key -> timestamp

def _should_alert(key: str, cooldown_sec: int = 300) -> bool:
    now = time.time()
    last = _last_alert_time.get(key, 0)
    if now - last >= cooldown_sec:
        _last_alert_time[key] = now
        return True
    return False


# ── 风控状态持久化辅助 ──────────────────────────────────────────────────────────

def _save_risk_state(user_id: int, rm: RiskManager):
    save_risk_state(
        user_id,
        consecutive_losses=rm._consecutive_losses,
        daily_start_balance=rm._daily_start_balance,
        daily_loss_triggered=rm._daily_loss_triggered,
        last_date=datetime.now().strftime('%Y-%m-%d'),
    )


def _restore_risk_state(user_id: int, rm: RiskManager):
    data = load_risk_state(user_id)
    rm._consecutive_losses   = data["consecutive_losses"]
    rm._daily_start_balance  = data["daily_start_balance"]
    rm._daily_loss_triggered = data["daily_loss_triggered"]
    today = datetime.now().strftime('%Y-%m-%d')
    if data.get("last_date") != today:
        rm._daily_start_balance  = None
        rm._daily_loss_triggered = False
    # ── Bug fix: 恢复熔断状态 ────────────────────────────────────────────────
    # 服务重启后仅恢复计数是不够的：若连亏次数已达上限，必须同步恢复熔断开关，
    # 否则 is_trading_allowed 默认 True，导致重启后熔断失效。
    if (rm._consecutive_losses >= rm.max_consecutive_losses
            or rm._daily_loss_triggered):
        rm.is_trading_allowed = False


# ── 持仓状态（per-user，读写 SQLite bot_state 表）────────────────────────────

def _empty_state() -> dict:
    return {
        "position_side": None, "position_amount": 0,
        "entry_price": 0.0, "active_sl": 0.0,
        "active_tp1": 0.0, "active_tp2": 0.0,
        "open_fee": 0.0, "margin_used": 0.0,
        "strategy_name": "", "signal_reason": "",
        "entry_time": "",
        "has_moved_to_breakeven": False,
        "has_taken_partial_profit": False,
        "exchange_order_ids": {"sl_order": None, "tp_order": None},
        # V2.5: Trailing Stop 状态
        "trailing_stop_active": False,
        "trailing_stop_best_price": 0.0,
        # V2.5: 时间止损
        "entry_bar_count": 0,
    }


def _load_state(user_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM bot_state WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        s = json.loads(row[0])
        empty = _empty_state()
        for k, v in empty.items():
            if k not in s:
                s[k] = v
        return s
    return _empty_state()


def _save_state(user_id: int, state: dict):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (user_id, value) VALUES (?, ?)",
        (user_id, json.dumps(state, ensure_ascii=False))
    )
    conn.commit()


def _clear_state(user_id: int):
    _save_state(user_id, _empty_state())


# ── 每用户有效配置（DB 优先，fallback 到 config.yaml）────────────────────────

def _resolve_config(user_id: int) -> dict:
    """
    读取用户在 DB 里的个性化配置，未设置的字段 fallback 到全局 config.yaml。
    返回完整的运行参数 dict。
    """
    global_cfg = get_config()
    bc = global_cfg.get("bot", {})
    rc = global_cfg.get("risk", {})
    sc = global_cfg.get("strategy", {})

    user_cfg = load_user_config(user_id)

    # Fix: 使用 is not None 替代 or，避免用户配置 0 值时被 fallback 覆盖
    def _pick(user_val, fallback_val):
        """用户配置优先（允许 0/空字符串等 falsy 值），仅当 None 时 fallback。"""
        return user_val if user_val is not None else fallback_val

    max_consecutive_losses = _pick(
        user_cfg.get("max_consecutive_losses"),
        rc.get("max_consecutive_losses", 3),
    )
    daily_loss_limit_pct = _pick(
        user_cfg.get("daily_loss_limit_pct"),
        rc.get("daily_loss_limit_pct", 0.05),
    )
    max_trade_amount = _pick(
        user_cfg.get("max_trade_amount"),
        rc.get("max_trade_amount", 1000),
    )

    return {
        "symbol":          _pick(user_cfg.get("symbol"),          bc.get("symbol",           "BTC/USDT:USDT")),
        "timeframe":       _pick(user_cfg.get("timeframe"),       bc.get("timeframe",         "1h")),
        "leverage":        _pick(user_cfg.get("leverage"),        bc.get("leverage",           3)),
        "risk_pct":        _pick(user_cfg.get("risk_pct"),        rc.get("risk_per_trade_pct", 0.01)),
        "strategy_name":   _pick(user_cfg.get("strategy_name"),   sc.get("name",              "PA_5S")),
        "strategy_params": _pick(user_cfg.get("strategy_params"), sc.get("params",            {})),
        "contract_size":   bc.get("contract_size",    0.01),
        "taker_fee_rate":  bc.get("taker_fee_rate",   0.0005),
        "check_interval":  bc.get("check_interval",   300),
        "max_trade_amount":       max_trade_amount,
        "max_consecutive_losses": max_consecutive_losses,
        "daily_loss_limit_pct":   daily_loss_limit_pct,
    }


# ── OKX 工具函数（per-user exchange 实例）───────────────────────────────────

def _get_swap_usdt(ex) -> float:
    for acc_type in ("swap", "trading", "future"):
        try:
            bal = ex.fetch_balance(params={"type": acc_type})
            v = float(bal.get("USDT", {}).get("free", 0))
            if v > 0:
                return v
        except Exception:
            continue
    try:
        bal = ex.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))
    except Exception:
        return 0.0


def _detect_pos_mode(ex) -> str:
    try:
        cfg = ex.private_get_account_config()
        data = (cfg or {}).get("data") or []
        mode = (data[0] or {}).get("posMode", "")
        return "hedge" if mode == "long_short_mode" else "net"
    except Exception:
        return "net"


def _place_algo(ex, symbol: str, side: str, amount: float,
                trigger_price: float, pos_side: str,
                algo_type: str, margin_mode: str = "cross") -> dict | None:
    pos_mode = _detect_pos_mode(ex)
    params = {"reduceOnly": True, "tdMode": margin_mode}

    if algo_type == "sl":
        params["stopLossPrice"]    = trigger_price
        params["slOrdPx"]         = -1
        params["slTriggerPxType"] = "last"
    else:
        params["takeProfitPrice"]  = trigger_price
        params["tpOrdPx"]         = -1
        params["tpTriggerPxType"] = "last"

    if pos_mode == "hedge":
        params["posSide"] = pos_side

    type_str = "SL" if algo_type == "sl" else "TP"
    try:
        order = ex.create_order(
            symbol=symbol, type="market", side=side,
            amount=amount, price=None, params=params
        )
        return order
    except Exception as e:
        return None


def _cancel_all_algo(ex, symbol: str, logger=None, notify=None, tag: str = ""):
    """取消所有条件单。失败时记录日志并发出告警，避免旧 SL/TP 残留。"""
    try:
        ex.cancel_all_orders(symbol, params={"stop": True})
    except Exception as e:
        err_msg = f"{tag} ⚠️ 取消条件单失败: {e}"
        if logger:
            logger.error(err_msg)
        else:
            bot_logger.error(err_msg)
        if notify:
            try:
                notify(f"⚠️ <b>取消条件单失败</b>\n{str(e)[:200]}\n请人工检查是否有残留条件单！")
            except Exception:
                pass


def _live_position_amount(ex, symbol: str) -> float:
    """
    查询交易所当前持仓合约张数。
    返回 >=0 表示实际持仓，-1 表示查询失败（不能误判为空仓）。
    """
    try:
        positions = ex.fetch_positions([symbol])
        return sum(
            float(p.get("contracts") or 0)
            for p in positions
            if p.get("symbol") == symbol and float(p.get("contracts") or 0) > 0
        )
    except Exception:
        return -1.0


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
        # 过滤出减仓成交（reduceOnly 或 info 里有标记）
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
        for order_id in filter(None, [tp_id, sl_id]):   # 先查 TP，再查 SL
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

    # 根据当前价格方向判断哪个被触发（比原先的推断更保守）
    if active_sl > 0 and active_tp > 0:
        if is_long:
            # 多单：TP在上方，SL在下方
            fill = active_tp if active_tp > entry else active_sl
        else:
            # 空单：TP在下方，SL在上方
            fill = active_tp if active_tp < entry else active_sl
    else:
        fill = active_sl if active_sl > 0 else entry

    return fill, "⚠️ 近似估算（建议人工核实）"


# ── 订单对账：Bot 启动时核对 SL/TP 条件单是否仍存在 ─────────────────────────────

def _reconcile_orders(ex, symbol: str, state: dict, logger, notify, tag: str):
    """
    Bot 启动时调用：若本地记录了持仓但条件单可能已失效，重新核查并补挂。
    返回修正后的 state（如条件单已失效则补挂并更新 order ids）。
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
        new_sl = _place_algo(ex, symbol, close_side, amount,
                             state["active_sl"], pos_side, "sl")
        if new_sl:
            state["exchange_order_ids"]["sl_order"] = new_sl.get("id")
            logger.info(f"{tag} SL 条件单已补挂")
        else:
            logger.error(f"{tag} ⚠️ SL 补挂失败！请人工检查！")
            notify(f"🚨 <b>{tag} SL 补挂失败</b>，请立即人工检查持仓！")

    if not tp_alive and state["active_tp1"] > 0:
        new_tp = _place_algo(ex, symbol, close_side, amount,
                             state["active_tp1"], pos_side, "tp")
        if new_tp:
            state["exchange_order_ids"]["tp_order"] = new_tp.get("id")
            logger.info(f"{tag} TP 条件单已补挂")

    return state


# ── 网络错误分类 ──────────────────────────────────────────────────────────────

import ccxt

def _classify_error(e: Exception) -> str:
    """
    将 ccxt 异常分类，返回处理策略字符串：
      - 'rate_limit'  : 被限频，需要退避等待
      - 'maintenance' : 交易所维护中
      - 'auth_error'  : API Key 无效，需要立即停止
      - 'network'     : 网络临时故障，正常重试
      - 'unknown'     : 未知错误
    """
    err_str = str(e).lower()
    if isinstance(e, ccxt.RateLimitExceeded):
        return 'rate_limit'
    if isinstance(e, ccxt.AuthenticationError):
        return 'auth_error'
    if isinstance(e, ccxt.ExchangeNotAvailable) or 'maintenance' in err_str:
        return 'maintenance'
    if isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout)):
        return 'network'
    return 'unknown'


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
    import pandas as pd
    import numpy as np

    # 计算当前 ATR
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

    # 计算当前盈利（点数）
    if is_long:
        profit_points = current_price - entry
    else:
        profit_points = entry - current_price

    # 激活条件：盈利 > ATR * trigger
    trigger_level = current_atr * trigger_mult
    if profit_points < trigger_level:
        # 尚未达到激活条件，不追踪
        state["trailing_stop_active"] = False
        return

    # 激活追踪
    if not state.get("trailing_stop_active"):
        state["trailing_stop_active"] = True
        state["trailing_stop_best_price"] = current_price
        logger.info(f"{tag} ✅ Trailing Stop 已激活，盈利={profit_points:.2f} > 触发={trigger_level:.2f}")

    # 更新最优价格
    best = state.get("trailing_stop_best_price", entry)
    if is_long:
        if current_price > best:
            state["trailing_stop_best_price"] = current_price
            best = current_price
    else:
        if current_price < best:
            state["trailing_stop_best_price"] = current_price
            best = current_price

    # 计算新的追踪止损价
    trail_distance = current_atr * distance_mult
    if is_long:
        new_sl = best - trail_distance
    else:
        new_sl = best + trail_distance

    # 只上移 SL，不下移（多头 SL 只能往上调，空头 SL 只能往下调）
    old_sl = state.get("active_sl", 0.0)
    should_update = False
    if is_long and new_sl > old_sl:
        should_update = True
    elif not is_long and (old_sl == 0 or new_sl < old_sl):
        should_update = True

    if should_update:
        try:
            # 取消旧 SL 条件单，挂新的
            _cancel_all_algo(ex, symbol, logger=logger)

            sl_ord = _place_algo(
                ex, symbol, close_side, state["position_amount"],
                new_sl, pos_side_str, "sl"
            )

            # 如果还有 TP，也重新挂
            tp_price = state.get("active_tp1", 0) or state.get("active_tp2", 0)
            tp_ord = None
            if tp_price > 0:
                tp_ord = _place_algo(
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


# ── V2.5: 策略绩效追踪 ──────────────────────────────────────────────────────

def _record_strategy_performance(user_id: int, strategy_name: str, pnl: float):
    """
    记录每笔交易对应策略的绩效，用于策略自动降权。
    """
    try:
        from execution.db_handler import get_conn
        conn = get_conn()
        conn.execute("""
            INSERT INTO strategy_performance
              (user_id, strategy_name, pnl, recorded_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (user_id, strategy_name, pnl))
        conn.commit()
    except Exception:
        pass  # 表可能尚未创建，静默失败


def _get_strategy_win_rate(user_id: int, strategy_name: str,
                           lookback: int = 20) -> float:
    """
    获取某策略最近 N 笔交易的胜率。
    返回 [0, 1]，无数据返回 0.5（默认中性）。
    """
    try:
        from execution.db_handler import get_conn
        conn = get_conn()
        rows = conn.execute(
            "SELECT pnl FROM strategy_performance "
            "WHERE user_id=? AND strategy_name=? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, strategy_name, lookback)
        ).fetchall()
        if not rows or len(rows) < 3:
            return 0.5
        wins = sum(1 for r in rows if r[0] > 0)
        return wins / len(rows)
    except Exception:
        return 0.5


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run_user_bot(bot_state, override_strategy: str = None):
    """
    :param bot_state: UserBotState 实例
                      属性：user_id, username, risk_manager, stop_event
    :param override_strategy: 启动时临时覆盖策略名（不保存到 DB），None 则用用户配置
    """
    user_id  = bot_state.user_id
    username = bot_state.username
    rm: RiskManager = bot_state.risk_manager
    stop_ev  = bot_state.stop_event

    # 每用户独立 logger
    logger = get_user_logger(username)
    tag    = f"[{username}]"

    # ── 加载有效配置（DB 优先，fallback 到 config.yaml）──────────────────────
    cfg = _resolve_config(user_id)

    # 启动时临时覆盖策略（优先级最高，不写入 DB）
    if override_strategy:
        cfg["strategy_name"] = override_strategy.upper()

    SYMBOL        = cfg["symbol"]
    TIMEFRAME     = cfg["timeframe"]
    LEVERAGE      = cfg["leverage"]
    CONTRACT_SIZE = cfg["contract_size"]
    FEE_RATE      = cfg["taker_fee_rate"]
    INTERVAL      = cfg["check_interval"]
    RISK_PCT      = cfg["risk_pct"]

    # ── 策略初始化：AUTO 模式启用选择器，否则固定策略 ──────────────────────
    global_cfg    = get_config()
    strategy_name = cfg["strategy_name"]
    use_auto      = (strategy_name.upper() == "AUTO")

    selector = None
    if use_auto:
        from strategy.selector import MarketRegimeSelector
        selector = MarketRegimeSelector(global_cfg)
        logger.info(f"{tag} 自动策略选择器已启用")
        # V2.0: 注册 selector 到 manager，供 API 读取 regime 详情
        try:
            from core.user_bot import manager as _mgr
            _mgr.register_user_selector(user_id, selector)
        except Exception:
            pass

    strategy = get_strategy(
        "PA_5S" if use_auto else strategy_name,
        **({} if use_auto else cfg["strategy_params"])
    )

    # 同步更新 RiskManager 的风控参数（支持 DB 里自定义）
    rm.max_consecutive_losses = cfg["max_consecutive_losses"]
    rm.daily_loss_limit_pct   = cfg["daily_loss_limit_pct"]
    rm.max_trade_amount       = cfg["max_trade_amount"]

    try:
        ex = get_user_exchange(user_id)
    except Exception as e:
        logger.error(f"{tag} 无法构建 exchange：{e}")
        return

    _restore_risk_state(user_id, rm)
    logger.info(
        f"{tag} 风控状态已恢复：连亏={rm._consecutive_losses}次，熔断={rm.is_fused}"
    )

    # ── 弱点修复：启动时主动设置当日起始余额 ─────────────────────────────────
    # 原先 daily_start_balance 在第一笔亏损时才初始化，若首笔已亏则基准偏低。
    # 现在在 Bot 启动时立即查询余额作为基准，确保日亏熔断计算准确；并写入 daily_balance 以便资产曲线立即有数据。
    try:
        _startup_balance = _get_swap_usdt(ex)
        if _startup_balance > 0:
            rm.set_daily_start_balance(_startup_balance)
            record_balance(user_id, _startup_balance)
    except Exception:
        pass

    notify, tg_err = _load_user_notifier(user_id, logger=logger)
    if notify is None:
        if tg_err == "not_configured":
            logger.warning(
                f"{tag} ⚠️ 用户未配置 Telegram 通知！开仓/平仓消息将不会推送。"
                f"请在设置页面配置 Telegram Bot Token 和 Chat ID。"
            )
        elif tg_err == "decrypted_empty":
            logger.warning(f"{tag} ⚠️ Telegram 配置解密后为空，请重新保存配置")
        else:
            logger.error(f"{tag} ⚠️ Telegram 配置加载失败: {tg_err}")

        # 尝试使用全局 .env 后备（旧版单用户部署兼容）
        from utils.notifier import _GLOBAL_TOKEN, _GLOBAL_CHAT_ID
        if _GLOBAL_TOKEN and _GLOBAL_CHAT_ID:
            notify = send_telegram_msg
            logger.info(f"{tag} 已 fallback 到全局 .env Telegram 配置")
        else:
            # 定义一个空操作 notifier，避免后续调用时 NoneType 崩溃
            def _noop_notify(msg: str) -> bool:
                logger.debug(f"{tag} [通知跳过] {msg[:80]}...")
                return False
            notify = _noop_notify
            logger.warning(f"{tag} 全局 Telegram 也未配置，所有通知将被跳过！")

    logger.info(f"{tag} Bot 启动，策略={cfg['strategy_name']}，品种={SYMBOL}")

    # V2.5: 从 config 加载高级风控参数
    v25_cfg = global_cfg.get("risk_v25", {})
    TRAILING_STOP_ENABLE = v25_cfg.get("trailing_stop_enable", True)
    TRAILING_STOP_TRIGGER = v25_cfg.get("trailing_stop_trigger", 0.5)   # 盈利达 ATR*0.5 后激活
    TRAILING_STOP_DISTANCE = v25_cfg.get("trailing_stop_distance", 0.8) # 回撤 ATR*0.8 触发
    TIME_STOP_BARS = v25_cfg.get("time_stop_bars", 24)                  # 最多持仓 24 根K线
    TIME_STOP_ENABLE = v25_cfg.get("time_stop_enable", True)
    DYNAMIC_POSITION_ENABLE = v25_cfg.get("dynamic_position_enable", True)

    notify(
        f"🚀 <b>{username} 的 Bot 已启动</b>\n"
        f"策略: {cfg['strategy_name']} | 品种: {SYMBOL} | 杠杆: {LEVERAGE}x"
    )

    # 设置杠杆
    try:
        ex.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode": "cross"})
        logger.info(f"{tag} 杠杆已设为 {LEVERAGE}x")
    except Exception as e:
        logger.warning(f"{tag} 设置杠杆失败（可能已设置）: {e}")

    import pandas as pd
    state = _load_state(user_id)

    cached_pos_mode = _detect_pos_mode(ex)
    logger.info(f"{tag} 持仓模式: {cached_pos_mode}")

    # ── 启动时订单对账 ────────────────────────────────────────────────────────
    state = _reconcile_orders(ex, SYMBOL, state, logger, notify, tag)
    if state["position_amount"] > 0:
        _save_state(user_id, state)

    _current_date = datetime.now().strftime('%Y-%m-%d')

    # 连续查询失败计数（用于网络持续抖动告警）
    _pos_query_fail_count = 0
    _POS_QUERY_FAIL_ALERT = 5   # 连续失败 N 次后告警

    # 限频退避状态
    _rate_limit_until = 0.0

    while not stop_ev.is_set():
        try:
            # ── 限频退避等待 ─────────────────────────────────────────────────
            now_ts = time.time()
            if now_ts < _rate_limit_until:
                wait_sec = int(_rate_limit_until - now_ts)
                logger.warning(f"{tag} 限频退避中，等待 {wait_sec}s")
                stop_ev.wait(min(wait_sec, INTERVAL))
                continue

            # ── 跨日检测 ─────────────────────────────────────────────────────
            today = datetime.now().strftime('%Y-%m-%d')
            if today != _current_date:
                _current_date = today
                current_bal_for_reset = _get_swap_usdt(ex)
                rm.reset_daily(current_bal_for_reset if current_bal_for_reset > 0 else None)
                _save_risk_state(user_id, rm)
                logger.info(f"{tag} 跨日重置日亏状态，新日期: {today}")
                notify(
                    f"📅 <b>{username}</b> 新的一天开始，日内风控已重置。\n"
                    f"起始余额: {current_bal_for_reset:.2f} U"
                )

            # ── 熔断检查 ─────────────────────────────────────────────────────
            if rm.is_fused:
                logger.warning(f"{tag} 🚨 风控熔断中，跳过本轮")
                stop_ev.wait(INTERVAL)
                continue

            # ── 余额 ─────────────────────────────────────────────────────────
            usdt_free = _get_swap_usdt(ex)
            if usdt_free > 0:
                record_balance(user_id, usdt_free)

            # ── 仓位核对（检测被动平仓）──────────────────────────────────────
            if state["position_amount"] > 0:
                live_amt = _live_position_amount(ex, SYMBOL)

                if live_amt == -1.0:
                    # 查询失败，累计计数并按需告警
                    _pos_query_fail_count += 1
                    logger.warning(
                        f"{tag} 持仓查询失败（连续第{_pos_query_fail_count}次）"
                    )
                    if _pos_query_fail_count >= _POS_QUERY_FAIL_ALERT:
                        alert_key = f"{user_id}:pos_query_fail"
                        if _should_alert(alert_key, 600):
                            notify(
                                f"⚠️ <b>{username}</b> 持仓查询连续失败 "
                                f"{_pos_query_fail_count} 次，请检查网络或 API 状态"
                            )
                    stop_ev.wait(INTERVAL)
                    continue

                _pos_query_fail_count = 0   # 查询成功，重置计数

                if live_amt == 0.0:
                    # 托管止损/止盈单已触发，拉取真实成交价
                    est_fill, fill_src = _fetch_passive_fill_price(ex, SYMBOL, state)

                    is_long = state["position_side"] == "long"
                    gross = (
                        (est_fill - state["entry_price"]) if is_long
                        else (state["entry_price"] - est_fill)
                    ) * state["position_amount"] * CONTRACT_SIZE
                    close_fee = state["position_amount"] * CONTRACT_SIZE * est_fill * FEE_RATE
                    net_pnl = gross - (state["open_fee"] + close_fee)

                    record_trade(user_id,
                                 "sell" if is_long else "buy",
                                 est_fill,
                                 state["position_amount"],
                                 SYMBOL, "被动平仓(SL/TP)",
                                 net_pnl,
                                 fill_src)

                    # V2.5: 记录策略绩效
                    _record_strategy_performance(
                        user_id, state.get("strategy_name", ""), net_pnl
                    )

                    current_balance = _get_swap_usdt(ex)
                    rm.notify_trade_result(net_pnl, current_balance)
                    _save_risk_state(user_id, rm)

                    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
                    logger.info(
                        f"{tag} 仓位已被动平仓，净盈亏={net_pnl:+.2f}U ({fill_src})"
                    )
                    _entry = state["entry_price"]
                    _side_label = "多" if is_long else "空"
                    notify(
                        f"{pnl_emoji} <b>{username} 平仓（托管单触发）</b>\n"
                        f"品种: {SYMBOL} | 杠杆: {LEVERAGE}x | 方向: {_side_label}\n"
                        f"入场价: {_entry:.2f} → 出场价: {est_fill:.2f}\n"
                        f"止损: {state['active_sl']:.2f} | 止盈: {state['active_tp1']:.2f}\n"
                        f"净盈亏: <b>{net_pnl:+.2f} U</b>\n"
                        f"来源: {fill_src}"
                    )
                    if rm.is_fused:
                        notify(
                            f"🚨 <b>{username} 风控熔断！</b>\n"
                            f"连续亏损 {rm.consecutive_losses} 次，Bot 已暂停。\n"
                            f"恢复请在控制台点击「恢复熔断」。"
                        )
                    _clear_state(user_id)
                    state = _load_state(user_id)
                    stop_ev.wait(INTERVAL)
                    continue

            # ── K 线 & 信号 ──────────────────────────────────────────────────
            # Fix: limit 动态适配策略预热期，确保实盘信号窗口与回测一致
            kline_limit = max(200, getattr(strategy, "warmup_bars", 50) * 2 + 10)
            ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=kline_limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)

            current_price = float(df.iloc[-1]["close"])

            # ── AUTO模式：每轮评估市场状态，按需热切换策略 ──────────────────
            if use_auto and selector is not None:
                new_strategy, regime_result = selector.get_strategy(df, SYMBOL)

                # V1.5: WAIT 观望状态 → 空仓时跳过本轮，有仓位继续监控
                if regime_result.get("regime") == "wait" and state["position_amount"] == 0:
                    logger.info(
                        f"{tag} 📋 WAIT 观望，跳过开仓 ({regime_result['reason']})"
                    )
                    stop_ev.wait(INTERVAL)
                    continue

                if new_strategy is not None:
                    old_name = strategy.name
                    strategy = new_strategy
                    # 新策略可能需要更多K线，重新拉取
                    kline_limit = max(200, getattr(strategy, "warmup_bars", 50) * 2 + 10)
                    ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=kline_limit)
                    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    df.set_index("timestamp", inplace=True)
                    current_price = float(df.iloc[-1]["close"])
                    logger.info(
                        f"{tag} 策略热切换: {old_name} → {strategy.name} "
                        f"({regime_result['reason']})"
                    )
                    # 空仓时才推送切换通知，有仓位时等平仓后生效
                    if state["position_amount"] == 0:
                        notify(
                            f"🔄 <b>{username} 策略已切换</b>\n"
                            f"新策略: <b>{strategy.name}</b>\n"
                            f"市场状态: {regime_result['regime'].upper()}\n"
                            f"技术面: {regime_result['tech_regime']} | "
                            f"新闻面: {regime_result['news_regime']}\n"
                            f"置信度: {regime_result['confidence']:.0%}"
                        )
            signal    = strategy.generate_signal(df)
            action    = signal["action"]
            reason    = signal["reason"]
            target_sl = signal["sl"]
            target_tp = signal["tp1"]

            logger.info(
                f"{tag} 信号={action} 价格={current_price:.2f} "
                f"仓位={state['position_side'] or '空仓'}/{state['position_amount']}张"
            )

            # ══════════════ 空仓 → 开仓 ══════════════════════════════════════
            if state["position_amount"] == 0 and action in ("BUY", "SELL"):
                price_risk = abs(current_price - target_sl) * CONTRACT_SIZE
                fee_risk   = (current_price + abs(target_sl)) * CONTRACT_SIZE * FEE_RATE
                risk_per   = price_risk + fee_risk
                if risk_per <= 0:
                    logger.warning(f"{tag} 风险计算异常（risk_per={risk_per}），跳过")
                    stop_ev.wait(INTERVAL)
                    continue

                contracts = int(math.floor(usdt_free * RISK_PCT / risk_per))

                # V2.5: 动态仓位 - 根据 regime 置信度调整
                if DYNAMIC_POSITION_ENABLE and use_auto and selector is not None:
                    regime_conf = getattr(selector, 'last_regime_detail', {}).get('confidence', 1.0)
                    # 置信度高 (>0.7) → 全仓，低 (0.3~0.7) → 按比例缩减
                    if regime_conf < 0.7:
                        conf_scale = max(0.4, regime_conf / 0.7)
                        contracts = max(1, int(contracts * conf_scale))
                        logger.info(
                            f"{tag} 📊 动态仓位: 置信度={regime_conf:.2f} "
                            f"缩放={conf_scale:.2f} → {contracts} 张"
                        )

                    # V2.5: 策略绩效降权 - 最近胜率低的策略减仓
                    strat_wr = _get_strategy_win_rate(user_id, strategy.name)
                    if strat_wr < 0.35:  # 胜率低于 35%
                        contracts = max(1, int(contracts * 0.6))
                        logger.info(
                            f"{tag} ⚠️ 策略 {strategy.name} 近期胜率低 "
                            f"({strat_wr:.0%})，降权仓位: {contracts} 张"
                        )

                # V1.5: 策略切换过渡期，使用半仓试探
                if use_auto and selector is not None and selector.in_transition:
                    contracts = max(1, contracts // 2)
                    logger.info(f"{tag} ⚠️ 策略过渡期，半仓试探: {contracts} 张")

                # Fix: max_trade_amount 是金额(USDT)上限，需换算为张数上限后再比较
                max_contracts_by_amount = int(math.floor(
                    rm.max_trade_amount / (current_price * CONTRACT_SIZE / LEVERAGE)
                )) if (current_price * CONTRACT_SIZE / LEVERAGE) > 0 else contracts
                contracts = min(contracts, max_contracts_by_amount)

                if contracts < 1:
                    logger.info(f"{tag} 仓位计算 <1 张（止损太宽或余额不足），跳过")
                    stop_ev.wait(INTERVAL)
                    continue

                if not rm.check_order(SYMBOL, action.lower(), contracts):
                    stop_ev.wait(INTERVAL)
                    continue

                open_side  = "buy"  if action == "BUY"  else "sell"
                pos_side   = "long" if action == "BUY"  else "short"
                close_side = "sell" if action == "BUY"  else "buy"

                open_params = {"tdMode": "cross"}
                if cached_pos_mode == "hedge":
                    open_params["posSide"] = pos_side

                order = ex.create_order(
                    SYMBOL, "market", open_side, contracts, params=open_params
                )
                if not order:
                    logger.error(f"{tag} 开仓失败")
                    stop_ev.wait(INTERVAL)
                    continue

                fill_price = float(order.get("average") or order.get("price") or current_price)

                tp1_contracts = contracts // 2 if contracts >= 2 else contracts

                sl_ord = _place_algo(ex, SYMBOL, close_side, contracts,
                                     target_sl, pos_side, "sl")
                tp_ord = _place_algo(ex, SYMBOL, close_side, tp1_contracts,
                                     target_tp, pos_side, "tp")

                if not sl_ord:
                    # SL 挂单失败 → 立即平仓保命
                    logger.error(f"{tag} 🚨 SL 挂单失败！回滚平仓！")
                    notify(f"🚨 <b>{username}</b> SL 挂单失败，已紧急平仓！")
                    _cancel_all_algo(ex, SYMBOL, logger=logger, notify=notify, tag=tag)
                    # ── Bug fix: 紧急平仓加 try/except ──────────────────────
                    # 若紧急平仓也失败，必须告警并标记持仓状态，防止下轮误重复开仓。
                    try:
                        ex.create_order(
                            SYMBOL, "market", close_side, contracts,
                            params={
                                "tdMode": "cross", "reduceOnly": True,
                                **({"posSide": pos_side} if cached_pos_mode == "hedge" else {}),
                            }
                        )
                        logger.info(f"{tag} 紧急平仓已执行")
                    except Exception as rollback_err:
                        logger.error(f"{tag} 🚨🚨 紧急平仓也失败！请立即人工处理！{rollback_err}")
                        notify(
                            f"🚨🚨 <b>{username} 紧急平仓失败！</b>\n"
                            f"SL 挂单失败且平仓指令也报错，请立即人工检查持仓！\n"
                            f"错误：{str(rollback_err)[:200]}"
                        )
                        # 标记为"未知持仓"，阻止下一轮开新仓，等人工确认
                        state["position_side"]   = "unknown_rollback_failed"
                        state["position_amount"] = contracts
                        _save_state(user_id, state)
                    stop_ev.wait(INTERVAL)
                    continue

                notional    = contracts * CONTRACT_SIZE * fill_price
                open_fee    = notional * FEE_RATE
                margin_used = notional / LEVERAGE

                state.update({
                    "position_amount":  contracts,
                    "position_side":    pos_side,
                    "entry_price":      fill_price,
                    "active_sl":        target_sl,
                    "active_tp1":       target_tp,
                    "active_tp2":       signal.get("tp2", 0.0),
                    "margin_used":      margin_used,
                    "open_fee":         open_fee,
                    "strategy_name":    strategy.name,
                    "signal_reason":    reason,
                    "entry_time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "exchange_order_ids": {
                        "sl_order": sl_ord.get("id") if sl_ord else None,
                        "tp_order": tp_ord.get("id") if tp_ord else None,
                    },
                })
                _save_state(user_id, state)
                record_trade(user_id, open_side, fill_price, contracts,
                             SYMBOL, "开仓", 0.0, reason)

                emoji = "🟢" if action == "BUY" else "🔴"
                notify(
                    f"{emoji} <b>{username} {'开多' if action=='BUY' else '开空'}</b>\n"
                    f"品种: {SYMBOL} | 杠杆: {LEVERAGE}x\n"
                    f"入场价: {fill_price:.2f} | 数量: {contracts}张\n"
                    f"止损: {target_sl:.2f} | 止盈: {target_tp:.2f}\n"
                    f"保证金: ~{margin_used:.2f} U | 风险: ~{usdt_free * RISK_PCT:.2f} U\n"
                    f"策略: {strategy.name} | 原因: {reason}"
                )

            # ══════════════ 有仓 → TP1到达后移SL到保本 & 检测部分平仓 ══════════
            elif state["position_amount"] > 0:
                is_long      = state["position_side"] == "long"
                close_side   = "sell" if is_long else "buy"
                pos_side_str = "long" if is_long else "short"
                total_amt    = state["position_amount"]
                entry        = state["entry_price"]

                # ── TP1分批止盈 ────────────────────────────────────────────────
                if not state.get("has_taken_partial_profit") and total_amt >= 2:
                    live_amt_check = _live_position_amount(ex, SYMBOL)
                    tp1_amt = total_amt // 2
                    remaining = total_amt - tp1_amt
                    if 0 < live_amt_check <= remaining:
                        logger.info(f"{tag} TP1已触发，移SL至保本，挂TP2")
                        _cancel_all_algo(ex, SYMBOL, logger=logger, notify=notify, tag=tag)

                        breakeven_sl = entry
                        new_sl = _place_algo(ex, SYMBOL, close_side, live_amt_check,
                                             breakeven_sl, pos_side_str, "sl")
                        tp2_price = state.get("active_tp2", 0.0)
                        new_tp = None
                        if tp2_price > 0:
                            new_tp = _place_algo(ex, SYMBOL, close_side, live_amt_check,
                                                 tp2_price, pos_side_str, "tp")

                        state["has_taken_partial_profit"] = True
                        state["has_moved_to_breakeven"]   = True
                        state["position_amount"]          = live_amt_check
                        state["active_sl"]                = breakeven_sl
                        state["exchange_order_ids"] = {
                            "sl_order": new_sl.get("id") if new_sl else None,
                            "tp_order": new_tp.get("id") if new_tp else None,
                        }
                        _save_state(user_id, state)

                        notify(
                            f"✂️ <b>{username} TP1已触发，分批止盈</b>\n"
                            f"剩余仓位: {live_amt_check}张 | SL已移至保本: {breakeven_sl:.2f}\n"
                            f"TP2目标: {tp2_price:.2f}"
                        )
                        stop_ev.wait(INTERVAL)
                        continue

                # ── 策略反转强制平仓 ───────────────────────────────────────────
                close_reason = ""
                if is_long  and action == "SELL": close_reason = f"策略反转: {reason}"
                if not is_long and action == "BUY":  close_reason = f"策略反转: {reason}"

                # ── V2.5: Trailing Stop (动态追踪止损) ────────────────────────
                if TRAILING_STOP_ENABLE and not close_reason and state["position_amount"] > 0:
                    _do_trailing_stop(
                        state, current_price, is_long, entry, df,
                        TRAILING_STOP_TRIGGER, TRAILING_STOP_DISTANCE,
                        ex, SYMBOL, close_side, pos_side_str,
                        logger, notify, tag, user_id,
                    )

                # ── V2.5: 时间止损（持仓超 N 根K线强制平仓）────────────────
                if TIME_STOP_ENABLE and not close_reason and state["position_amount"] > 0:
                    state["entry_bar_count"] = state.get("entry_bar_count", 0) + 1
                    if state["entry_bar_count"] >= TIME_STOP_BARS:
                        # 只在不亏损时执行时间止损（避免亏损放大）
                        if is_long and current_price >= entry:
                            close_reason = f"时间止损: 持仓{state['entry_bar_count']}根K线"
                        elif not is_long and current_price <= entry:
                            close_reason = f"时间止损: 持仓{state['entry_bar_count']}根K线"
                        elif state["entry_bar_count"] >= TIME_STOP_BARS * 1.5:
                            # 超时1.5倍，无论盈亏都平仓
                            close_reason = f"强制时间止损: 持仓{state['entry_bar_count']}根K线"
                    _save_state(user_id, state)

                if close_reason:
                    _cancel_all_algo(ex, SYMBOL, logger=logger, notify=notify, tag=tag)

                    close_params = {"tdMode": "cross", "reduceOnly": True}
                    if cached_pos_mode == "hedge":
                        close_params["posSide"] = pos_side_str

                    order = ex.create_order(
                        SYMBOL, "market", close_side,
                        state["position_amount"], params=close_params
                    )
                    fill_price = float(
                        order.get("average") or order.get("price") or current_price
                    )

                    gross = (
                        (fill_price - entry) if is_long
                        else (entry - fill_price)
                    ) * state["position_amount"] * CONTRACT_SIZE

                    close_fee = state["position_amount"] * CONTRACT_SIZE * fill_price * FEE_RATE
                    net_pnl   = gross - (state["open_fee"] + close_fee)

                    record_trade(user_id, close_side, fill_price,
                                 state["position_amount"], SYMBOL, "平仓",
                                 net_pnl, close_reason)

                    # V2.5: 记录策略绩效
                    _record_strategy_performance(
                        user_id, state.get("strategy_name", ""), net_pnl
                    )

                    usdt_free = _get_swap_usdt(ex)
                    rm.notify_trade_result(net_pnl, usdt_free)
                    _save_risk_state(user_id, rm)

                    if rm.is_fused:
                        notify(
                            f"🚨 <b>{username} 风控熔断！</b>\n"
                            f"连续亏损 {rm.consecutive_losses} 次，Bot 已暂停。\n"
                            f"恢复请在控制台点击「恢复熔断」。"
                        )

                    _clear_state(user_id)
                    state = _load_state(user_id)

                    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
                    _side_label = "多" if is_long else "空"
                    notify(
                        f"{pnl_emoji} <b>{username} 平仓</b>\n"
                        f"品种: {SYMBOL} | 杠杆: {LEVERAGE}x | 方向: {_side_label}\n"
                        f"入场价: {entry:.2f} → 出场价: {fill_price:.2f}\n"
                        f"净盈亏: <b>{net_pnl:+.2f} U</b>\n"
                        f"原因: {close_reason}"
                    )

        except ccxt.RateLimitExceeded as e:
            # 限频：指数退避，等 30s~120s
            wait_sec = min(120, 30 * (2 ** getattr(e, 'retry_after', 1)))
            _rate_limit_until = time.time() + wait_sec
            logger.warning(f"{tag} API 限频，退避 {wait_sec}s")
            alert_key = f"{user_id}:rate_limit"
            if _should_alert(alert_key, 600):
                notify(f"⏱️ <b>{username}</b> API 限频，已自动退避 {wait_sec}s")
            stop_ev.wait(min(wait_sec, INTERVAL))
            continue

        except ccxt.AuthenticationError as e:
            # API Key 无效：立即停止，不重试
            logger.error(f"{tag} 🚨 API Key 认证失败，Bot 停止: {e}")
            notify(
                f"🚨 <b>{username} API Key 认证失败，Bot 已停止</b>\n"
                f"请检查：\n"
                f"• <b>模拟盘/实盘是否一致</b>：在 OKX 模拟盘创建的 Key 需勾选「使用模拟盘」，实盘 Key 勿勾选。\n"
                f"• <b>IP 白名单</b>：若 OKX 绑定了 IP，请将本服务器 IP 加入白名单。\n"
                f"在设置页重新配置后，可先点「验证 API Key」再启动 Bot。"
            )
            stop_ev.set()
            return

        except ccxt.ExchangeNotAvailable as e:
            # 交易所维护：等待较长时间后重试
            logger.warning(f"{tag} 交易所维护中，等待 120s: {e}")
            alert_key = f"{user_id}:maintenance"
            if _should_alert(alert_key, 1800):   # 30 分钟内只推一次
                notify(f"🔧 <b>{username}</b> 交易所维护中，Bot 暂停，将自动恢复")
            stop_ev.wait(120)
            continue

        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            # 网络临时故障：短暂等待后正常重试
            logger.warning(f"{tag} 网络异常，等待重试: {e}")
            alert_key = f"{user_id}:network_error"
            if _should_alert(alert_key, 300):
                notify(f"📡 <b>{username}</b> 网络异常，Bot 将自动重试")
            stop_ev.wait(15)
            continue

        except Exception as e:
            logger.error(f"{tag} 运行异常: {e}")
            alert_key = f"{user_id}:generic_error:{type(e).__name__}"
            if _should_alert(alert_key, 300):
                notify(f"⚠️ <b>{username} Bot 异常</b>\n{str(e)[:200]}")
            stop_ev.wait(10)
            continue

        stop_ev.wait(INTERVAL)

    logger.info(f"{tag} Bot 已停止")
    notify(f"🛑 <b>{username} 的 Bot 已停止</b>")
    # V2.0: 清理 selector 注册
    try:
        from core.user_bot import manager as _mgr
        _mgr.unregister_user_selector(user_id)
    except Exception:
        pass
