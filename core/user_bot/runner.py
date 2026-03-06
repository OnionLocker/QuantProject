"""
core/user_bot/runner.py - 每用户 Bot 主循环（多用户版）

每个用户拥有独立的 exchange 实例、持仓状态、风控模块，互相隔离。
"""
import time
import math
import json
from datetime import datetime

from utils.logger import bot_logger
from utils.notifier import send_telegram_msg
from utils.config_loader import get_config
from strategy.registry import get_strategy
from execution.db_handler import get_conn, record_balance, record_trade
from api.routes.keys import get_user_exchange
from risk.risk_manager import RiskManager


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

    tag = f"[{username}]"
    bot_logger.info(f"{tag} Bot 启动，策略={sc['name']}，品种={SYMBOL}")
    send_telegram_msg(
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

    while not stop_ev.is_set():
        try:
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
                    bot_logger.info(f"{tag} 检测到仓位已平（托管单触发），清理本地状态")
                    send_telegram_msg(f"🔔 <b>{username}</b> 仓位已闭合（托管单触发）")
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
                pos_mode   = _detect_pos_mode(ex)

                open_params = {"tdMode": "cross"}
                if pos_mode == "hedge":
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
                sl_ord = _place_algo(ex, SYMBOL, close_side, contracts,
                                     target_sl, pos_side, "sl")
                tp_ord = _place_algo(ex, SYMBOL, close_side, contracts,
                                     target_tp, pos_side, "tp")

                if not sl_ord:
                    # 止损挂单失败 → 立即平仓保命
                    bot_logger.error(f"{tag} 🚨 SL 挂单失败！回滚平仓！")
                    send_telegram_msg(f"🚨 <b>{username}</b> SL 挂单失败，已紧急平仓！")
                    _cancel_all_algo(ex, SYMBOL)
                    ex.create_order(SYMBOL, "market", close_side, contracts,
                                    params={"tdMode":"cross","reduceOnly":True,
                                            **({"posSide":pos_side} if pos_mode=="hedge" else {})})
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
                send_telegram_msg(
                    f"{emoji} <b>{username} {'开多' if action=='BUY' else '开空'}</b>\n"
                    f"价格: {fill_price:.2f} | 数量: {contracts}张\n"
                    f"SL: {target_sl:.2f} | TP: {target_tp:.2f}\n"
                    f"预估风险: ~{usdt_free * RISK_PCT:.2f} U\n原因: {reason}"
                )

            # ══════════════ 有仓 → 备用平仓（主要靠交易所托管单）═══════════
            elif state["position_amount"] > 0:
                is_long     = state["position_side"] == "long"
                close_reason = ""

                if is_long  and action == "SELL": close_reason = f"策略反转: {reason}"
                if not is_long and action == "BUY":  close_reason = f"策略反转: {reason}"

                if close_reason:
                    close_side   = "sell" if is_long else "buy"
                    pos_side_str = "long" if is_long else "short"
                    pos_mode     = _detect_pos_mode(ex)

                    _cancel_all_algo(ex, SYMBOL)

                    close_params = {"tdMode": "cross", "reduceOnly": True}
                    if pos_mode == "hedge":
                        close_params["posSide"] = pos_side_str

                    order = ex.create_order(
                        SYMBOL, "market", close_side,
                        state["position_amount"], params=close_params
                    )
                    fill_price = float(
                        order.get("average") or order.get("price") or current_price
                    )

                    gross = (
                        (fill_price - state["entry_price"]) if is_long
                        else (state["entry_price"] - fill_price)
                    ) * state["position_amount"] * CONTRACT_SIZE

                    close_fee = state["position_amount"] * CONTRACT_SIZE * fill_price * FEE_RATE
                    net_pnl   = gross - (state["open_fee"] + close_fee)

                    record_trade(user_id, close_side, fill_price,
                                 state["position_amount"], SYMBOL, "平仓",
                                 net_pnl, close_reason)

                    usdt_free = _get_swap_usdt(ex)
                    rm.notify_trade_result(net_pnl, usdt_free)

                    if rm.is_fused:
                        send_telegram_msg(
                            f"🚨 <b>{username} 风控熔断！</b>\n"
                            f"连续亏损 {rm.consecutive_losses} 次，Bot 已暂停。\n"
                            f"恢复请在控制台点击「恢复熔断」。"
                        )

                    _clear_state(user_id)
                    state = _load_state(user_id)

                    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
                    send_telegram_msg(
                        f"{pnl_emoji} <b>{username} 平仓</b>\n"
                        f"价格: {fill_price:.2f} | 净盈亏: {net_pnl:+.2f} U\n"
                        f"原因: {close_reason}"
                    )

        except Exception as e:
            bot_logger.error(f"{tag} 运行异常: {e}")
            send_telegram_msg(f"⚠️ <b>{username} Bot 异常</b>\n{str(e)[:200]}")
            stop_ev.wait(10)
            continue

        stop_ev.wait(INTERVAL)

    bot_logger.info(f"{tag} Bot 已停止")
    send_telegram_msg(f"🛑 <b>{username} 的 Bot 已停止</b>")
