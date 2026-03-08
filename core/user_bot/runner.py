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

def _load_user_notifier(user_id: int):
    try:
        raw = load_tg_config(user_id)
        if not raw["tg_bot_token_enc"] or not raw["tg_chat_id_enc"]:
            return None
        token   = decrypt(raw["tg_bot_token_enc"])
        chat_id = decrypt(raw["tg_chat_id_enc"])
        return make_notifier(token, chat_id)
    except Exception:
        return None


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

    return {
        "symbol":          user_cfg.get("symbol")        or bc.get("symbol",           "BTC/USDT:USDT"),
        "timeframe":       user_cfg.get("timeframe")     or bc.get("timeframe",         "1h"),
        "leverage":        user_cfg.get("leverage")      or bc.get("leverage",           3),
        "risk_pct":        user_cfg.get("risk_pct")      or rc.get("risk_per_trade_pct", 0.01),
        "strategy_name":   user_cfg.get("strategy_name") or sc.get("name",              "PA_5S"),
        "strategy_params": user_cfg.get("strategy_params") or sc.get("params",          {}),
        "contract_size":   bc.get("contract_size",    0.01),
        "taker_fee_rate":  bc.get("taker_fee_rate",   0.0005),
        "check_interval":  bc.get("check_interval",   300),
        "max_trade_amount":       rc.get("max_trade_amount",       1000),
        "max_consecutive_losses": rc.get("max_consecutive_losses", 3),
        "daily_loss_limit_pct":   rc.get("daily_loss_limit_pct",   0.05),
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


def _cancel_all_algo(ex, symbol: str):
    try:
        ex.cancel_all_orders(symbol, params={"stop": True})
    except Exception as e:
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

    # 每用户独立 logger
    logger = get_user_logger(username)
    tag    = f"[{username}]"

    # ── 加载有效配置（DB 优先，fallback 到 config.yaml）──────────────────────
    cfg = _resolve_config(user_id)

    SYMBOL        = cfg["symbol"]
    TIMEFRAME     = cfg["timeframe"]
    LEVERAGE      = cfg["leverage"]
    CONTRACT_SIZE = cfg["contract_size"]
    FEE_RATE      = cfg["taker_fee_rate"]
    INTERVAL      = cfg["check_interval"]
    RISK_PCT      = cfg["risk_pct"]

    strategy = get_strategy(cfg["strategy_name"], **cfg["strategy_params"])

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
    # 现在在 Bot 启动时立即查询余额作为基准，确保日亏熔断计算准确。
    try:
        _startup_balance = _get_swap_usdt(ex)
        if _startup_balance > 0:
            rm.set_daily_start_balance(_startup_balance)
    except Exception:
        pass

    notify = _load_user_notifier(user_id)
    if notify is None:
        notify = send_telegram_msg
        logger.info(f"{tag} 用户未配置 Telegram，使用全局后备配置")

    logger.info(f"{tag} Bot 启动，策略={cfg['strategy_name']}，品种={SYMBOL}")
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

                    current_balance = _get_swap_usdt(ex)
                    rm.notify_trade_result(net_pnl, current_balance)
                    _save_risk_state(user_id, rm)

                    pnl_emoji = "🎉" if net_pnl > 0 else "🩸"
                    logger.info(
                        f"{tag} 仓位已被动平仓，净盈亏={net_pnl:+.2f}U ({fill_src})"
                    )
                    notify(
                        f"{pnl_emoji} <b>{username} 仓位已闭合（托管单触发）</b>\n"
                        f"成交价: {est_fill:.2f} | 净盈亏: {net_pnl:+.2f} U\n"
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
                    _cancel_all_algo(ex, SYMBOL)
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

                # ── TP1分批止盈 ────────────────────────────────────────────────
                if not state.get("has_taken_partial_profit") and total_amt >= 2:
                    live_amt_check = _live_position_amount(ex, SYMBOL)
                    tp1_amt = total_amt // 2
                    remaining = total_amt - tp1_amt
                    if 0 < live_amt_check <= remaining:
                        logger.info(f"{tag} TP1已触发，移SL至保本，挂TP2")
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

                # ── 策略反转强制平仓 ───────────────────────────────────────────
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
                f"请检查 API Key 是否有效或已过期，然后在设置页面重新配置。"
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
