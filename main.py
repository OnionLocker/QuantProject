import time
from datetime import datetime
from data.market_data import fetch_kline_data
from execution.order_manager import place_market_order, place_algo_order, cancel_all_algo_orders, set_leverage, get_available_usdt
from strategy.price_action_v2 import PriceActionV2
from risk.risk_manager import RiskManager
from utils.logger import bot_logger
from utils.notifier import send_telegram_msg
from core.okx_client import fetch_position_state
from utils.trade_state import load_state, save_state, clear_state, get_empty_state
from execution.db_handler import record_balance, record_trade

SYMBOL = 'BTC/USDT:USDT'
TIMEFRAME = '1h'
RISK_PER_TRADE_PCT = 0.01   # 单笔最大亏本金 1%
LEVERAGE = 3
CONTRACT_SIZE = 0.01
TAKER_FEE_RATE = 0.0005
CHECK_INTERVAL = 300

my_strategy = PriceActionV2(swing_l=8)
my_risk_manager = RiskManager(
    max_trade_amount=1000,
    is_trading_allowed=True,
    max_consecutive_losses=3,    # 连续亏损 3 次自动熔断
    daily_loss_limit_pct=0.05,   # 单日亏损超过 5% 自动熔断
)


def run_bot():
    start_msg = (f"🚀 <b>V2 量化机器人已启动 (固定风险 {RISK_PER_TRADE_PCT*100}% 版)</b>\n"
                 f"交易对: {SYMBOL}\n杠杆: {LEVERAGE}倍\n"
                 f"风控: 连亏熔断={my_risk_manager.max_consecutive_losses}次 | "
                 f"日亏限制={my_risk_manager.daily_loss_limit_pct*100:.0f}%")
    bot_logger.info("V2 量化机器人已启动...")
    send_telegram_msg(start_msg)

    set_leverage(SYMBOL, LEVERAGE)
    bot_logger.info("-" * 40)

    state = load_state()
    pos = fetch_position_state(SYMBOL)

    if pos["status"] == "both":
        warn = f"⚠️ 检测到双向持仓。请先手动处理后再启动机器人。pos={pos}"
        bot_logger.error(warn); send_telegram_msg(warn); raise RuntimeError(warn)

    elif pos["status"] == "ok":
        if state["position_amount"] == 0:
            msg = "🚨 对账失败：交易所存在仓位，本地无记录！已挂起。"
            bot_logger.error(msg); send_telegram_msg(msg); raise RuntimeError(msg)
        elif abs(state["position_amount"] - pos["amount"]) > 0.0001 or state["position_side"] != pos["side"]:
            msg = "🚨 对账失败：两端数量/方向不符！已挂起。"
            bot_logger.error(msg); send_telegram_msg(msg); raise RuntimeError(msg)
        else:
            msg = f"🔄 启动同步：完美对账！找回本地精准状态: {state['position_side']} | 张数: {state['position_amount']}"
            bot_logger.info(msg)
            cancel_all_algo_orders(SYMBOL)
            close_side = "sell" if state["position_side"] == "long" else "buy"
            sl_order = place_algo_order(SYMBOL, close_side, state["position_amount"], state["active_sl"], state["position_side"], "sl")
            tp_order = place_algo_order(SYMBOL, close_side, state["position_amount"], state["active_tp1"], state["position_side"], "tp")
            if sl_order: state["exchange_order_ids"]["sl_order"] = sl_order.get("id")
            if tp_order: state["exchange_order_ids"]["tp_order"] = tp_order.get("id")
            save_state(state)
            send_telegram_msg(msg + "\n✅ 交易所保护单已按本地记录重新下发！")

    elif pos["status"] == "empty":
        if state["position_amount"] > 0:
            msg = "🚨 对账失败：本地有持仓，交易所为空！请人工排查后执行 clear_state() 重启。"
            bot_logger.error(msg); send_telegram_msg(msg); raise RuntimeError(msg)
        else:
            cancel_all_algo_orders(SYMBOL)
            bot_logger.info("🔄 启动同步：环境干净，准备开仓。")

    while True:
        try:
            # ── 熔断检查 ──────────────────────────────────────────────────
            if my_risk_manager.is_fused:
                bot_logger.warning("🚨 [熔断中] 风控已熔断，跳过本轮信号，等待人工恢复。")
                time.sleep(CHECK_INTERVAL)
                continue

            bot_logger.info("正在执行新一轮扫描...")

            available_usdt = get_available_usdt()
            if available_usdt > 0:
                record_balance(available_usdt)

            real_pos = fetch_position_state(SYMBOL)
            if state["position_amount"] > 0 and real_pos["status"] == "empty":
                bot_logger.warning("👀 发现实际仓位已空！清理本地状态中...")
                send_telegram_msg("🔔 <b>仓位闭合 (托管单触发)</b>\n检测到仓位已闭合！清理本地状态中...")
                cancel_all_algo_orders(SYMBOL)
                clear_state()
                state = load_state()

            df = fetch_kline_data(SYMBOL, TIMEFRAME, limit=100)
            if df is not None:
                current_price = df.iloc[-1]['close']
                signal = my_strategy.generate_signal(df)
                action = signal["action"]
                msg = signal["reason"]
                target_sl = signal["sl"]
                target_tp = signal["tp1"]

                status_text = ("空仓观望中" if state["position_amount"] == 0
                               else f"持有 {state['position_side']} 单 ({state['position_amount']}张)")
                bot_logger.info(f"🤖 策略诊断: {msg} (市价: {current_price} | 状态: {status_text})")

                # ══════════════ 场景 1: 空仓 -> 开仓 ══════════════
                if state["position_amount"] == 0:
                    if action in ["BUY", "SELL"]:
                        dynamic_trade_amount = my_risk_manager.calculate_position_size(
                            balance=available_usdt,
                            entry_price=current_price,
                            sl_price=target_sl,
                            risk_pct=RISK_PER_TRADE_PCT,
                            contract_size=CONTRACT_SIZE,
                            fee_rate=TAKER_FEE_RATE,
                            leverage=LEVERAGE
                        )
                        if dynamic_trade_amount < 1:
                            bot_logger.info("❌ 计算出的开仓数量小于 1 张，止损空间太大或余额不足，放弃该信号！")
                            action = "HOLD"

                    if action in ["BUY", "SELL"]:
                        open_side = "buy" if action == "BUY" else "sell"
                        pos_side  = "long" if action == "BUY" else "short"
                        close_side = "sell" if action == "BUY" else "buy"

                        if my_risk_manager.check_order(SYMBOL, open_side, dynamic_trade_amount):
                            order = place_market_order(SYMBOL, open_side, dynamic_trade_amount,
                                                       reduce_only=False, pos_side=pos_side)
                            if order:
                                fill_price = float(order.get("average") or order.get("price") or current_price)

                                sl_order = place_algo_order(SYMBOL, close_side, dynamic_trade_amount,
                                                            target_sl, pos_side, "sl")
                                tp_order = place_algo_order(SYMBOL, close_side, dynamic_trade_amount,
                                                            target_tp, pos_side, "tp")

                                if not sl_order or not tp_order:
                                    bot_logger.error("🚨 保护单挂载失败！回滚强平！")
                                    send_telegram_msg("🚨 <b>紧急避险机制触发</b>\n正在强平保命，强制关闭机器人！")
                                    cancel_all_algo_orders(SYMBOL)
                                    place_market_order(SYMBOL, close_side, dynamic_trade_amount,
                                                       reduce_only=True, pos_side=pos_side)
                                    import sys
                                    sys.exit("❌ 挂单严重异常，安全停机。")

                                state["position_amount"]  = dynamic_trade_amount
                                state["position_side"]    = pos_side
                                state["entry_price"]      = fill_price
                                state["active_sl"]        = target_sl
                                state["active_tp1"]       = target_tp
                                state["active_tp2"]       = signal.get("tp2", 0.0)
                                state["margin_used"]      = (dynamic_trade_amount * CONTRACT_SIZE * fill_price) / LEVERAGE
                                state["open_fee"]         = (dynamic_trade_amount * CONTRACT_SIZE * fill_price) * TAKER_FEE_RATE
                                state["strategy_name"]    = my_strategy.name
                                state["signal_reason"]    = msg
                                state["entry_time"]       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                state["exchange_order_ids"]["sl_order"] = sl_order.get("id")
                                state["exchange_order_ids"]["tp_order"] = tp_order.get("id")
                                save_state(state)

                                record_trade(side=open_side, price=fill_price,
                                             amount=dynamic_trade_amount, symbol=SYMBOL,
                                             action="开仓", pnl=0.0, reason=msg)

                                emoji = "🟢" if action == "BUY" else "🔴"
                                type_name = "开多" if action == "BUY" else "开空"
                                est_risk_u = available_usdt * RISK_PER_TRADE_PCT
                                tg_msg = (f"{emoji} <b>{type_name} (固定风险法)</b>\n成交: {fill_price:.2f}\n"
                                          f"数量: {dynamic_trade_amount}张\n硬盘SL: {target_sl:.2f}\n"
                                          f"硬盘TP: {target_tp:.2f}\n预估风险: ~{est_risk_u:.2f} U\n原因: {msg}")
                                send_telegram_msg(tg_msg)
                                bot_logger.info(tg_msg.replace('\n', ' | ').replace('<b>', '').replace('</b>', ''))

                # ══════════════ 场景 2: 平多 ══════════════
                elif state["position_amount"] > 0 and state["position_side"] == 'long':
                    sell_reason = ""
                    if action == "SELL":                              sell_reason = f"策略反转 ({msg})"
                    elif current_price <= state['active_sl']:        sell_reason = "备用触及止损"
                    elif current_price >= state['active_tp1']:       sell_reason = "备用触及止盈"

                    if sell_reason and my_risk_manager.check_order(SYMBOL, 'sell', state["position_amount"]):
                        cancel_all_algo_orders(SYMBOL)
                        order = place_market_order(SYMBOL, "sell", state["position_amount"],
                                                   reduce_only=True, pos_side="long")
                        if order:
                            fill_price = float(order.get("average") or order.get("price") or current_price)
                            net_profit = _log_close_trade("🎉", "平多单", sell_reason, fill_price, state)
                            # ── 通知风控模块本次交易结果 ──
                            available_usdt = get_available_usdt()
                            my_risk_manager.notify_trade_result(net_profit, available_usdt)
                            _check_and_alert_fuse()
                            clear_state()
                            state = load_state()

                # ══════════════ 场景 3: 平空 ══════════════
                elif state["position_amount"] > 0 and state["position_side"] == 'short':
                    sell_reason = ""
                    if action == "BUY":                              sell_reason = f"策略反转 ({msg})"
                    elif current_price >= state['active_sl']:       sell_reason = "备用触及止损"
                    elif current_price <= state['active_tp1']:      sell_reason = "备用触及止盈"

                    if sell_reason and my_risk_manager.check_order(SYMBOL, 'buy', state["position_amount"]):
                        cancel_all_algo_orders(SYMBOL)
                        order = place_market_order(SYMBOL, "buy", state["position_amount"],
                                                   reduce_only=True, pos_side="short")
                        if order:
                            fill_price = float(order.get("average") or order.get("price") or current_price)
                            net_profit = _log_close_trade("🎉", "平空单", sell_reason, fill_price, state)
                            # ── 通知风控模块本次交易结果 ──
                            available_usdt = get_available_usdt()
                            my_risk_manager.notify_trade_result(net_profit, available_usdt)
                            _check_and_alert_fuse()
                            clear_state()
                            state = load_state()

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            bot_logger.error(f"❌ 运行报错: {e}")
            send_telegram_msg(f"⚠️ <b>Bot 运行异常</b>\n{str(e)[:200]}")
            time.sleep(10)


def _check_and_alert_fuse():
    """检查熔断状态，触发时发 Telegram 告警"""
    if my_risk_manager.is_fused:
        send_telegram_msg(
            f"🚨 <b>风控熔断触发！Bot 已暂停交易</b>\n"
            f"连续亏损次数: {my_risk_manager.consecutive_losses}\n"
            f"需人工确认后调用 manual_resume() 恢复。"
        )


def _log_close_trade(emoji, action_name, reason, current_price, state) -> float:
    """记录平仓日志并返回净盈亏（U）"""
    close_notional = state["position_amount"] * CONTRACT_SIZE * current_price
    close_fee = close_notional * TAKER_FEE_RATE
    total_fees = state["open_fee"] + close_fee

    is_long = "多" in action_name
    gross_profit = (
        (current_price - state["entry_price"]) * state["position_amount"] * CONTRACT_SIZE
        if is_long else
        (state["entry_price"] - current_price) * state["position_amount"] * CONTRACT_SIZE
    )
    net_profit = gross_profit - total_fees
    net_roi = (net_profit / state["margin_used"]) * 100 if state["margin_used"] > 0 else 0.0

    tg_msg = (f"{emoji if net_profit > 0 else '🩸'} <b>{action_name}</b>\n"
              f"价格: {current_price:.2f}\n净盈亏: {net_profit:+.2f} U\n"
              f"ROI: {net_roi:+.2f}%\n原因: {reason}")
    send_telegram_msg(tg_msg)

    close_side = "sell" if is_long else "buy"
    record_trade(side=close_side, price=current_price,
                 amount=state["position_amount"], symbol=SYMBOL,
                 action="平仓", pnl=net_profit, reason=reason)

    return net_profit


if __name__ == "__main__":
    run_bot()
