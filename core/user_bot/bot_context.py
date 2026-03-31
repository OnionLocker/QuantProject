"""
core/user_bot/bot_context.py - Bot 运行上下斄1�71ￄ1�77

将原先散落在 runner.py 顶层的各种辅助函数和状��集中封装，
为每个用户的 Bot 提供统一的上下文对象〄1�71ￄ1�77

包含＄1�71ￄ1�77
  - 配置解析（DB 优先，fallback config.yaml＄1�71ￄ1�77
  - 持仓状��读写（SQLite bot_state 表）
  - Telegram 通知（加轄1�71ￄ1�77 + fallback + noop＄1�71ￄ1�77
  - 告警去重（同类错评1�71ￄ1�77 N 秒内只推丢�次）
  - 风控状��持久化
  - 零余额连续计敄1�71ￄ1�77
"""
from __future__ import annotations

import json
import time
import logging
from datetime import datetime
from typing import Optional, Callable, Dict, Any

from utils.logger import get_user_logger
from utils.notifier import make_notifier, send_telegram_msg
from utils.config_loader import get_config
from execution.db_handler import (
    get_conn, save_risk_state, load_risk_state,
    load_tg_config, load_user_config,
)
from api.auth.crypto import decrypt
from risk.risk_manager import RiskManager


# ── 全局告警去重时间戳 ─────────────────────────────────────────────────────────
_last_alert_time: Dict[str, float] = {}


def should_alert(key: str, cooldown_sec: int = 300) -> bool:
    """同类告警圄1�71ￄ1�77 cooldown_sec 秒内只触发一次�ￄ1�71ￄ1�77"""
    now = time.time()
    last = _last_alert_time.get(key, 0)
    if now - last >= cooldown_sec:
        _last_alert_time[key] = now
        return True
    return False


# ── 持仓状态 ─────────────────────────────────────────────────────────────────

def empty_state() -> dict:
    """返回空仓位状态模板�ￄ1�71ￄ1�77"""
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
        # V7.0: 平仓后冷静期
        "last_close_time": "",          # 上次平仓时间
        "last_close_reason": "",        # 上次平仓原因
        "last_close_pnl": 0.0,         # 上次平仓盈亏
        "last_close_side": "",          # 上次平仓方向 ("long" / "short")
        "cooldown_until": "",           # 冷静期截止时间
        "cooldown_bars_remaining": 0,   # 冷静期剩余 K 线数
        "spike_detected_time": "",      # 最近插针检测时间
        "spike_cooldown_until": "",     # 插针冷静期截止时间
    }


def load_state(user_id: int) -> dict:
    """仄1�71ￄ1�77 SQLite 加载持仓状��，缺失字段用默认��补全�ￄ1�71ￄ1�77"""
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM bot_state WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        s = json.loads(row[0])
        template = empty_state()
        for k, v in template.items():
            if k not in s:
                s[k] = v
        return s
    return empty_state()


def save_state(user_id: int, state: dict):
    """持仓状��写兄1�71ￄ1�77 SQLite〄1�71ￄ1�77"""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (user_id, value) VALUES (?, ?)",
        (user_id, json.dumps(state, ensure_ascii=False))
    )
    conn.commit()


def clear_state(user_id: int):
    """清空持仓状��（平仓后调用）〄1�71ￄ1�77"""
    save_state(user_id, empty_state())


# ── 风控持久化 ────────────────────────────────────────────────────────────────

def persist_risk_state(user_id: int, rm: RiskManager):
    """将风控运行时状��持久化刄1�71ￄ1�77 SQLite〄1�71ￄ1�77"""
    save_risk_state(
        user_id,
        consecutive_losses=rm._consecutive_losses,
        daily_start_balance=rm._daily_start_balance,
        daily_loss_triggered=rm._daily_loss_triggered,
        last_date=datetime.now().strftime('%Y-%m-%d'),
    )


def restore_risk_state(user_id: int, rm: RiskManager):
    """
    仄1�71ￄ1�77 SQLite 恢复风控状��，跨日自动重置〄1�71ￄ1�77
    V8.0: 跨日时连亏衰减（而非原样恢复），避免 Bot 无限期停工�ￄ1�71ￄ1�77
    """
    data = load_risk_state(user_id)
    rm._consecutive_losses   = data["consecutive_losses"]
    rm._daily_start_balance  = data["daily_start_balance"]
    rm._daily_loss_triggered = data["daily_loss_triggered"]
    today = datetime.now().strftime('%Y-%m-%d')
    if data.get("last_date") != today:
        rm._daily_start_balance  = None
        rm._daily_loss_triggered = False
        old_losses = rm._consecutive_losses
        rm._consecutive_losses = max(0, rm._consecutive_losses - 2)
        if old_losses != rm._consecutive_losses:
            logger.info(
                "[风控] 跨日连亏衰减: %d ↄ1�71ￄ1�77 %d", old_losses, rm._consecutive_losses
            )
    if rm._daily_loss_triggered:
        rm.is_trading_allowed = False
    elif rm._consecutive_losses >= rm.max_consecutive_losses:
        rm.is_trading_allowed = False
    else:
        rm.is_trading_allowed = True


# ── 配置解析 ─────────────────────────────────────────────────────────────────

def resolve_config(user_id: int) -> dict:
    """
    读取用户 DB 配置，未设置的字殄1�71ￄ1�77 fallback 刄1�71ￄ1�77 config.yaml〄1�71ￄ1�77
    返回完整运行参数 dict〄1�71ￄ1�77
    """
    global_cfg = get_config()
    bc = global_cfg.get("bot", {})
    rc = global_cfg.get("risk", {})
    sc = global_cfg.get("strategy", {})

    user_cfg = load_user_config(user_id)

    def _pick(user_val, fallback_val):
        """用户配置优先（允讄1�71ￄ1�77 0/空字符串），仄1�71ￄ1�77 None 旄1�71ￄ1�77 fallback〄1�71ￄ1�77"""
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


# ── Telegram 通知加载 ────────────────────────────────────────────────────────

def load_notifier(
    user_id: int,
    username: str,
    logger: logging.Logger,
) -> Callable[[str], bool]:
    """
    加载用户 Telegram 通知函数，按优先级回逢�＄1�71ￄ1�77
      1. 用户 DB 配置 ↄ1�71ￄ1�77 2. 全局 .env ↄ1�71ￄ1�77 3. noop（跳过��知＄1�71ￄ1�77
    """
    tag = f"[{username}]"

    # 1) 尝试用户 DB 配置
    try:
        raw = load_tg_config(user_id)
        if raw["tg_bot_token_enc"] and raw["tg_chat_id_enc"]:
            token   = decrypt(raw["tg_bot_token_enc"])
            chat_id = decrypt(raw["tg_chat_id_enc"])
            if token and chat_id:
                return make_notifier(token, chat_id)
            else:
                logger.warning(f"{tag} ⚠️ Telegram 配置解密后为空，请重新保存配罄1�71ￄ1�77")
        else:
            logger.warning(
                f"{tag} ⚠️ 用户未配罄1�71ￄ1�77 Telegram 通知！开仄1�71ￄ1�77/平仓消息将不会推送�ￄ1�71ￄ1�77"
                f"请在设置页面配置 Telegram Bot Token 咄1�71ￄ1�77 Chat ID〄1�71ￄ1�77"
            )
    except Exception as e:
        logger.error(f"{tag} ⚠️ Telegram 配置加载失败: {e}")

    # 2) 尝试全局 .env 后备
    from utils.notifier import _GLOBAL_TOKEN, _GLOBAL_CHAT_ID
    if _GLOBAL_TOKEN and _GLOBAL_CHAT_ID:
        logger.info(f"{tag} 巄1�71ￄ1�77 fallback 到全屢� .env Telegram 配置")
        return send_telegram_msg

    # 3) 空操作
    def _noop_notify(msg: str) -> bool:
        logger.debug(f"{tag} [通知跳过] {msg[:80]}...")
        return False

    logger.warning(f"{tag} 全局 Telegram 也未配置，所有��知将被跳过＄1�71ￄ1�77")
    return _noop_notify


# ── 策略绩效追踪 ──────────────────────────────────────────────────────────────

def record_strategy_performance(user_id: int, strategy_name: str, pnl: float):
    """记录每笔交易对应策略的绩效，用于策略自动降权〄1�71ￄ1�77"""
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO strategy_performance
              (user_id, strategy_name, pnl, recorded_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (user_id, strategy_name, pnl))
        conn.commit()
    except Exception as e:
        logging.getLogger("bot_context").debug(
            "策略绩效记录失败（表可能尚未创建＄1�71ￄ1�77: %s", e
        )


def get_strategy_win_rate(
    user_id: int,
    strategy_name: str,
    lookback: int = 20,
) -> float:
    """
    获取某策略最迄1�71ￄ1�77 N 笔交易的胜率〄1�71ￄ1�77
    无数据返囄1�71ￄ1�77 0.5（默认中性）〄1�71ￄ1�77
    """
    try:
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


class BotContext:
    """
    每用戄1�71ￄ1�77 Bot 运行上下文，集中管理＄1�71ￄ1�77
      - logger / notifier / tag
      - 配置参数
      - 风控管理噄1�71ￄ1�77
      - 零余额连续计敄1�71ￄ1�77
      - 持仓查询失败计数
    """

    ZERO_BALANCE_ALERT_ROUNDS: int = 5        # 连续零余额 N 轮后告警
    ZERO_BALANCE_ALERT_COOLDOWN_SEC: int = 3600  # 零余额告警冷却期（1小时）

    def __init__(
        self,
        user_id: int,
        username: str,
        rm: RiskManager,
        stop_event,
    ):
        self.user_id  = user_id
        self.username = username
        self.rm       = rm
        self.stop_ev  = stop_event
        self.tag      = f"[{username}]"

        # 日志 & 通知
        self.logger = get_user_logger(username)
        self.notify: Callable[[str], bool] = lambda msg: False  # 初始化后替换

        # 配置
        self.cfg: dict = {}

        # 运行时计数器
        self.zero_balance_count: int = 0
        self.pos_query_fail_count: int = 0
        self.current_date: str = datetime.now().strftime('%Y-%m-%d')

        # 限频退避
        self.rate_limit_until: float = 0.0

    def init(self):
        """初始化配置����知器��风控状态��在主循环前调用〄1�71ￄ1�77"""
        self.cfg = resolve_config(self.user_id)
        self.notify = load_notifier(
            self.user_id, self.username, self.logger
        )
        restore_risk_state(self.user_id, self.rm)
        self.logger.info(
            f"{self.tag} 风控状��已恢复＄1�71ￄ1�77"
            f"连亏={self.rm._consecutive_losses}次，熔断={self.rm.is_fused}"
        )

    def on_balance_ok(self):
        """余额获取成功时重置零余额计数〄1�71ￄ1�77"""
        self.zero_balance_count = 0

    def on_balance_zero(self):
        """余额获取丄1�71ￄ1�77 0 时累计计数，达阈值推送告警�ￄ1�71ￄ1�77"""
        self.zero_balance_count += 1
        self.logger.warning(
            f"{self.tag} ⚠️ 余额获取丄1�71ￄ1�77 0（连续第 {self.zero_balance_count} 次）"
        )
        if self.zero_balance_count == self.ZERO_BALANCE_ALERT_ROUNDS:
            alert_key = f"{self.user_id}:zero_balance"
            if should_alert(alert_key, self.ZERO_BALANCE_ALERT_COOLDOWN_SEC):
                self.notify(
                    f"⚠️ <b>{self.username}</b> 连续 {self.zero_balance_count} 轮余额为 0＄1�71ￄ1�77"
                    f"无法弢�仓��\n请检查：\n"
                    f" 1�71ￄ1�77 模拟盄1�71ￄ1�77/实盘 Key 是否匹配\n"
                    f" 1�71ￄ1�77 合约账户是否朄1�71ￄ1�77 USDT\n"
                    f" 1�71ￄ1�77 API Key 权限是否包含「读取�ￄ1�71ￄ1�77"
                )

    def check_cross_day(self) -> bool:
        """跨日棢�测，返回 True 表示跨日已处理�ￄ1�71ￄ1�77"""
        today = datetime.now().strftime('%Y-%m-%d')
        if today == self.current_date:
            return False
        self.current_date = today
        return True
