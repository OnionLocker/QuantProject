"""
core/user_bot/runner.py - 每用户 Bot 主循环（多用户版）

每个用户拥有独立的 exchange 实例、持仓状态、风控模块，互相隔离。
"""
import time
import math
import json
from datetime import datetime

from utils.logger import bot_logger
from utils.notifier import make_notifier, send_telegram_msg
from utils.config_loader import get_config
from strategy.registry import get_strategy
from execution.db_handler import (get_conn, record_balance, record_trade,
                                   save_risk_state, load_risk_state,
                                   load_tg_config)
from api.auth.crypto import decrypt
from api.routes.keys import get_user_exchange
from risk.risk_manager import RiskManager


# ── 每用户 Telegram 通知 ────────────────────────────────────────────────────────

def _load_user_notifier(user_id: int):
    """
    从数据库加载该用户的 Telegram 配置，返回绑定好凭证的发送函数。
    若用户未配置则返回 None（调用方使用全局后备）。
    """
    try:
        raw = load_tg_config(user_id)
        if not raw["tg_bot_token_enc"] or not raw["tg_chat_id_enc"]:
            return None
        token   = decrypt(raw["tg_bot_token_enc"])
        chat_id = decrypt(raw["tg_chat_id_enc"])
        return make_notifier(token, chat_id)
    except Exception:
        return None


# ── 风控状态持久化辅助 ──────────────────────────────────────────────────────────

def _save_risk_state(user_id: int, rm: RiskManager):
    """将 RiskManager 内存状态同步到 SQLite，以便 Bot 重启后恢复。"""
    save_risk_state(
        user_id,
        consecutive_losses=rm._consecutive_losses,
        daily_start_balance=rm._daily_start_balance,
        daily_loss_triggered=rm._daily_loss_triggered,
        last_date=datetime.now().strftime('%Y-%m-%d'),
    )


def _restore_risk_state(user_id: int, rm: RiskManager):
    """Bot 启动时从 SQLite 恢复上次的风控状态。"""
    data = load_risk_state(user_id)
    rm._consecutive_losses   = data["consecutive_losses"]
    rm._daily_start_balance  = data["daily_start_balance"]
    rm._daily_loss_triggered = data["daily_loss_triggered"]
    # 若上次记录日期不是今天，重置日内状态
    today = datetime.now().strftime('%Y-%m-%d')
    if data.get("last_date") != today:
        rm._daily_start_balance  = None
        rm._daily_loss_triggered = False


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
    }


def _load_state(user_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
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
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (user_id, value) VALUES (?, ?)",
            (user_id, json.dumps(state, ensure_ascii=False))
        )
        conn.commit()
    finally:
        conn.close()


def _clear_state(user_id: int):
    _save_state(user_id, _empty_state())


# ── OKX 工具函数（per-user exchange 实例）───────────────────────────────────

def _get_swap_usdt(ex) -> float:
    """获取 OKX 永续合约账户可用 USDT（兼容统一账户和分开账户）"""
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
    """检测 OKX 持仓模式：hedge 或 net"""
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
    """
    下 OKX 条件单（止损 / 止盈）。

    ccxt 4.x OKX 行为（已通过源码验证）：
    - params 里有 stopLossPrice → 自动设 ordType=conditional，走 /trade/order-algo
    - slOrdPx=-1 表示触发后以市价成交
    - 同理 takeProfitPrice + tpOrdPx=-1
    """
    pos_mode = _detect_pos_mode(ex)
    params = {"reduceOnly": True, "tdMode": margin_mode}

    if algo_type == "sl":
        params["stopLossPrice"] = trigger_price
        params["slOrdPx"]       = -1           # 触发后市价成交
        params["slTriggerPxType"] = "last"
    else:
        params["takeProfitPrice"] = trigger_price
        params["tpOrdPx"]         = -1         # 触发后市价成交
        params["tpTriggerPxType"] = "last"

    if pos_mode == "hedge":
        params["posSide"] = pos_side

    type_str = "SL" if algo_type == "sl" else "TP"
    try:
        order = ex.create_order(
            symbol=symbol, type="market", side=side,
            amount=amount, price=None, params=params
        )
        bot_logger.info(f"✅ {type_str} 条件单成功，id={order.get('id')}")
        return order
    except Exception as e:
        bot_logger.error(f"❌ {type_str} 条件单失败: {e}")
        return None


def _cancel_all_algo(ex, symbol: str):
    try:
        ex.cancel_all_orders(symbol, params={"stop": True})
    except Exception as e:
        bot_logger.warning(f"撤销条件单失败（可忽略）: {e}")


def _live_position_amount(ex, symbol: str) -> float:
    """查询交易所当前持仓合约张数"""
    try:
        positions = ex.fetch_positions([symbol])
        return sum(
            float(p.get("contracts") or 0)
            for p in positions
            if p.get("symbol") == symbol and float(p.get("contracts") or 0) > 0
        )
    except Exception:
        return -1.0   # -1 表示查询失败，不能误判为空仓


def _estimate_passive_fill(ex, symbol: str, state: dict) -> tuple[float, str]:
    """
    估算被动平仓（SL/TP托管单触发）的成交价格。
    优先通过交易所最近成交记录获取真实价格，
    失败则根据持仓状态智能推断使用 SL 或 TP 价格。
    返回: (fill_price, 来源说明)
    """
    # 尝试从交易所获取最近成交记录
    try:
        since_ms = None
        entry_time_str = state.get("entry_time", "")
        if entry_time_str:
            from datetime import datetime as _dt
            dt = _dt.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S")
            since_ms = int(dt.timestamp() * 1000)

        trades = ex.fetch_my_trades(symbol, since=since_ms, limit=10)
        close_trades = [t for t in trades if t.get("info", {}).get("reduceOnly") or
                        t.get("reduceOnly") or
                        float(t.get("amount", 0)) > 0]
        if close_trades:
            latest = sorted(close_trades, key=lambda x: x.get("timestamp", 0))[-1]
            price = float(latest.get("price") or latest.get("average") or 0)
            if price > 0:
                return price, "交易所真实成交"
    except Exception:
        pass

    # 回退：根据SL/TP价格智能推断
    is_long = state["position_side"] == "long"
    entry = state["entry_price"]
    active_sl = state["active_sl"]
    active_tp1 = state["active_tp1"]

    if is_long:
        # 多单：价格跌破SL→止损；涨过TP→止盈
        if active_sl > 0 and active_tp1 > 0:
            return (active_tp1, "推断为止盈价(TP1)") if active_tp1 > entry else (active_sl, "推断为止损价(SL)")
        return (active_sl if active_sl > 0 else entry, "推断为止损价(SL)")
    else:
        if active_sl > 0 and active_tp1 > 0:
            return (active_tp1, "推断为止盈价(TP1)") if active_tp1 < entry else (active_sl, "推断为止损价(SL)")
        return (active_sl if active_sl > 0 else entry, "推断为止损价(SL)")


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run_user_bot(bot_state):
    """
    :param bot_state: UserBotState 实例
                      属性：user_id, username, risk_manager, stop_event
    """
    user_id  = bot_state.user_id
    username = bot_state.username
    rm: RiskManager = bot_state.risk_manager
    stop_ev  = bot_state.stop_event

    cfg  = get_config()
    bc   = cfg["bot"]
    rc   = cfg["risk"]
    sc   = cfg["strategy"]

    SYMBOL        = bc["symbol"]
    TIMEFRAME     = bc["timeframe"]
    LEVERAGE      = bc["leverage"]
    CONTRACT_SIZE = bc["contract_size"]
    FEE_RATE      = bc["taker_fee_rate"]
    INTERVAL      = bc["check_interval"]
    RISK_PCT      = rc["risk_per_trade_pct"]

    strategy = get_strategy(sc["name"], **sc.get("params", {}))

    try:
        ex = get_user_exchange(user_id)
    except Exception as e:
        bot_logger.error(f"[{username}] 无法构建 exchange：{e}")
        return

    # 从数据库恢复上次的风控状态（重启后不丢失连亏计数等）
    _restore_risk_state(user_id, rm)
    bot_logger.info(
        f"[{username}] 风控状态已恢复：连亏={rm._consecutive_losses}次，"
        f"熔断={rm.is_fused}"
    )

    # 加载每用户专属 Telegram 配置，构建独立通知函数
    # 若用户未配置，回退到全局 .env 配置（send_telegram_msg）
    notify = _load_user_notifier(user_id)
    if notify is None:
        notify = send_telegram_msg
        bot_logger.info(f"{tag} 用户未配置 Telegram，使用全局后备配置")

    tag = f"[{username}]"
    bot_logger.info(f"{tag} Bot 启动，策略={sc['name']}，品种={SYMBOL}")
    notify(
        f"🚀 <b>{username} 的 Bot 已启动</b>\n"
        f"策略: {sc['name']} | 品种: {SYMBOL} | 杠杆: {LEVERAGE}x"
    )

    # 设置杠杆（失败不阻断启动）
    try:
        ex.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode": "cross"})
        bot_logger.info(f"{tag} 杠杆已设为 {LEVERAGE}x")
    except Exception as e:
        bot_logger.warning(f"{tag} 设置杠杆失败（可能已设置）: {e}")

    import pandas as pd
    state = _load_state(user_id)

    # 缓存持仓模式（net/hedge），Bot运行期间几乎不变，避免每轮API调用
    cached_pos_mode = _detect_pos_mode(ex)
    bot_logger.info(f"{tag} 持仓模式: {cached_pos_mode}")

    # 跟踪当前日期，用于每日自动重置日亏状态
    _current_date = datetime.now().strftime('%Y-%m-%d')

    while not stop_ev.is_set():
        try:
            # ── 跨日检测：自动重置日亏状态 ──────────────────────────────────
            today = datetime.now().strftime('%Y-%m-%d')
            if today != _current_date:
                _current_date = today
                current_bal_for_reset = _get_swap_usdt(ex)
                rm.reset_daily(current_bal_for_reset if current_bal_for_reset > 0 else None)
                _save_risk_state(user_id, rm)
                bot_logger.info(f"{tag} 跨日重置日亏状态，新日期: {today}")
                notify(
                    f"📅 <b>{username}</b> 新的一天开始，日内风控已重置。\n"
                    f"起始余额: {current_bal_for_reset:.2f} U"
                )

            # ── 熔断检查 ───────────────────────────────────────────────────
            if rm.is_fused:
                bot_logger.warning(f"{tag} 🚨 风控熔断中，跳过本轮")
                stop_ev.wait(INTERVAL)
                continue

            # ── 余额 ────────────────────────────────────────────────────────
            usdt_free = _get_swap_usdt(ex)
            if usdt_free > 0:
                record_balance(user_id, usdt_free)

            # ── 仓位核对（检测被动平仓）────────────────────────────────────
            if state["position_amount"] > 0:
                live_amt = _live_position_amount(ex, SYMBOL)
                if live_amt == 0.0:
                    # 托管止损/止盈单已触发，补全PnL计算并通知风控
                    est_fill, fill_src = _estimate_passive_fill(
                        ex, SYMBOL, state
                    )
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

                    current_balance = _get_swap_usdt(ex)
                    rm.notify_trade_result(net_pnl, current_balance)
                    _save_risk_state(user_id, rm)

                    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
                    bot_logger.info(
                        f"{tag} 仓位已被动平仓，净盈亏={net_pnl:+.2f}U ({fill_src})"
                    )
                    notify(
                        f"{pnl_emoji} <b>{username} 仓位已闭合（托管单触发）</b>\n"
                        f"估算价: {est_fill:.2f} | 净盈亏: {net_pnl:+.2f} U\n"
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
                # live_amt == -1 表示查询失败，跳过本轮但不清除状态

            # ── K 线 & 信号 ─────────────────────────────────────────────────
            ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
            df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)

            current_price = float(df.iloc[-1]["close"])
            signal    = strategy.generate_signal(df)
            action    = signal["action"]
            reason    = signal["reason"]
            target_sl = signal["sl"]
            target_tp = signal["tp1"]

            bot_logger.info(
                f"{tag} 信号={action} 价格={current_price:.2f} "
                f"仓位={state['position_side'] or '空仓'}/{state['position_amount']}张"
            )

            # ══════════════ 空仓 → 开仓 ══════════════════════════════════
            if state["position_amount"] == 0 and action in ("BUY", "SELL"):
                # 计算仓位张数
                price_risk = abs(current_price - target_sl) * CONTRACT_SIZE
                fee_risk   = (current_price + abs(target_sl)) * CONTRACT_SIZE * FEE_RATE
                risk_per   = price_risk + fee_risk
                if risk_per <= 0:
                    bot_logger.warning(f"{tag} 风险计算异常（risk_per={risk_per}），跳过")
                    stop_ev.wait(INTERVAL)
                    continue

                contracts = int(math.floor(usdt_free * RISK_PCT / risk_per))
                contracts = min(contracts, rm.max_trade_amount)

                if contracts < 1:
                    bot_logger.info(f"{tag} 仓位计算 <1 张（止损太宽或余额不足），跳过")
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
                    bot_logger.error(f"{tag} 开仓失败")
                    stop_ev.wait(INTERVAL)
                    continue

                fill_price = float(order.get("average") or order.get("price") or current_price)

                # 挂止损止盈
                # 若合约数≥2，TP1只平一半，剩余一半待TP2；否则全仓TP1
                tp1_contracts = contracts // 2 if contracts >= 2 else contracts

                sl_ord = _place_algo(ex, SYMBOL, close_side, contracts,
                                     target_sl, pos_side, "sl")
                tp_ord = _place_algo(ex, SYMBOL, close_side, tp1_contracts,
                                     target_tp, pos_side, "tp")

                if not sl_ord:
                    # 止损挂单失败 → 立即平仓保命
                    bot_logger.error(f"{tag} 🚨 SL 挂单失败！回滚平仓！")
                    notify(f"🚨 <b>{username}</b> SL 挂单失败，已紧急平仓！")
                    _cancel_all_algo(ex, SYMBOL)
                    ex.create_order(SYMBOL, "market", close_side, contracts,
                                    params={"tdMode":"cross","reduceOnly":True,
                                            **({"posSide":pos_side} if cached_pos_mode=="hedge" else {})})
                    stop_ev.wait(INTERVAL)
                    continue

                notional   = contracts * CONTRACT_SIZE * fill_price
                open_fee   = notional * FEE_RATE
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
                    f"价格: {fill_price:.2f} | 数量: {contracts}张\n"
                    f"SL: {target_sl:.2f} | TP: {target_tp:.2f}\n"
                    f"预估风险: ~{usdt_free * RISK_PCT:.2f} U\n原因: {reason}"
                )

            # ══════════════ 有仓 → TP1到达后移SL到保本 & 检测部分平仓 ══════════
            elif state["position_amount"] > 0:
                is_long      = state["position_side"] == "long"
                close_side   = "sell" if is_long else "buy"
                pos_side_str = "long" if is_long else "short"
                total_amt    = state["position_amount"]
                entry        = state["entry_price"]

                # ── TP1分批止盈：检测是否已有部分仓位被TP1托管单平掉 ──────────
                if not state.get("has_taken_partial_profit") and total_amt >= 2:
                    live_amt_check = _live_position_amount(ex, SYMBOL)
                    tp1_amt = total_amt // 2  # TP1平掉一半
                    remaining = total_amt - tp1_amt
                    if 0 < live_amt_check <= remaining:
                        # TP1已触发，取消旧SL，以保本价重新挂SL，再挂TP2
                        bot_logger.info(f"{tag} TP1已触发，移SL至保本，挂TP2")
                        _cancel_all_algo(ex, SYMBOL)

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

                # ── 策略反转强制平仓 ──────────────────────────────────────────
                close_reason = ""
                if is_long  and action == "SELL": close_reason = f"策略反转: {reason}"
                if not is_long and action == "BUY":  close_reason = f"策略反转: {reason}"

                if close_reason:
                    _cancel_all_algo(ex, SYMBOL)

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
                    notify(
                        f"{pnl_emoji} <b>{username} 平仓</b>\n"
                        f"价格: {fill_price:.2f} | 净盈亏: {net_pnl:+.2f} U\n"
                        f"原因: {close_reason}"
                    )

        except Exception as e:
            bot_logger.error(f"{tag} 运行异常: {e}")
            notify(f"⚠️ <b>{username} Bot 异常</b>\n{str(e)[:200]}")
            stop_ev.wait(10)
            continue

        stop_ev.wait(INTERVAL)

    bot_logger.info(f"{tag} Bot 已停止")
    notify(f"🛑 <b>{username} 的 Bot 已停止</b>")
