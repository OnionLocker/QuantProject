"""
core/user_bot/bot_context.py - Bot 运行上下文

将原先散落在 runner.py 顶层的各种辅助函数和状态集中封装，
为每个用户的 Bot 提供统一的上下文对象。

包含：
  - 配置解析（DB 优先，fallback config.yaml）
  - 持仓状态读写（SQLite bot_state 表）
  - Telegram 通知（加载 + fallback + noop）
  - 告警去重（同类错误 N 秒内只推一次）
  - 风控状态持久化
  - 零余额连续计数
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
    """同类告警在 cooldown_sec 秒内只触发一次。"""
    now = time.time()
    last = _last_alert_time.get(key, 0)
    if now - last >= cooldown_sec:
        _last_alert_time[key] = now
        return True
    return False


# ── 持仓状态 ─────────────────────────────────────────────────────────────────

def empty_state() -> dict:
    """返回空仓位状态模板。"""
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


def load_state(user_id: int) -> dict:
    """从 SQLite 加载持仓状态，缺失字段用默认值补全。"""
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
    """持仓状态写入 SQLite。"""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (user_id, value) VALUES (?, ?)",
        (user_id, json.dumps(state, ensure_ascii=False))
    )
    conn.commit()


def clear_state(user_id: int):
    """清空持仓状态（平仓后调用）。"""
    save_state(user_id, empty_state())


# ── 风控持久化 ────────────────────────────────────────────────────────────────

def persist_risk_state(user_id: int, rm: RiskManager):
    """将风控运行时状态持久化到 SQLite。"""
    save_risk_state(
        user_id,
        consecutive_losses=rm._consecutive_losses,
        daily_start_balance=rm._daily_start_balance,
        daily_loss_triggered=rm._daily_loss_triggered,
        last_date=datetime.now().strftime('%Y-%m-%d'),
    )


def restore_risk_state(user_id: int, rm: RiskManager):
    """从 SQLite 恢复风控状态，跨日自动重置。"""
    data = load_risk_state(user_id)
    rm._consecutive_losses   = data["consecutive_losses"]
    rm._daily_start_balance  = data["daily_start_balance"]
    rm._daily_loss_triggered = data["daily_loss_triggered"]
    today = datetime.now().strftime('%Y-%m-%d')
    if data.get("last_date") != today:
        rm._daily_start_balance  = None
        rm._daily_loss_triggered = False
    # Bug fix: 服务重启后若连亏次数已达上限，同步恢复熔断状态
    if (rm._consecutive_losses >= rm.max_consecutive_losses
            or rm._daily_loss_triggered):
        rm.is_trading_allowed = False


# ── 配置解析 ─────────────────────────────────────────────────────────────────

def resolve_config(user_id: int) -> dict:
    """
    读取用户 DB 配置，未设置的字段 fallback 到 config.yaml。
    返回完整运行参数 dict。
    """
    global_cfg = get_config()
    bc = global_cfg.get("bot", {})
    rc = global_cfg.get("risk", {})
    sc = global_cfg.get("strategy", {})

    user_cfg = load_user_config(user_id)

    def _pick(user_val, fallback_val):
        """用户配置优先（允许 0/空字符串），仅 None 时 fallback。"""
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
    加载用户 Telegram 通知函数，按优先级回退：
      1. 用户 DB 配置 → 2. 全局 .env → 3. noop（跳过通知）
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
                logger.warning(f"{tag} ⚠️ Telegram 配置解密后为空，请重新保存配置")
        else:
            logger.warning(
                f"{tag} ⚠️ 用户未配置 Telegram 通知！开仓/平仓消息将不会推送。"
                f"请在设置页面配置 Telegram Bot Token 和 Chat ID。"
            )
    except Exception as e:
        logger.error(f"{tag} ⚠️ Telegram 配置加载失败: {e}")

    # 2) 尝试全局 .env 后备
    from utils.notifier import _GLOBAL_TOKEN, _GLOBAL_CHAT_ID
    if _GLOBAL_TOKEN and _GLOBAL_CHAT_ID:
        logger.info(f"{tag} 已 fallback 到全局 .env Telegram 配置")
        return send_telegram_msg

    # 3) 空操作
    def _noop_notify(msg: str) -> bool:
        logger.debug(f"{tag} [通知跳过] {msg[:80]}...")
        return False

    logger.warning(f"{tag} 全局 Telegram 也未配置，所有通知将被跳过！")
    return _noop_notify


# ── 策略绩效追踪 ──────────────────────────────────────────────────────────────

def record_strategy_performance(user_id: int, strategy_name: str, pnl: float):
    """记录每笔交易对应策略的绩效，用于策略自动降权。"""
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
            "策略绩效记录失败（表可能尚未创建）: %s", e
        )


def get_strategy_win_rate(
    user_id: int,
    strategy_name: str,
    lookback: int = 20,
) -> float:
    """
    获取某策略最近 N 笔交易的胜率。
    无数据返回 0.5（默认中性）。
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
    每用户 Bot 运行上下文，集中管理：
      - logger / notifier / tag
      - 配置参数
      - 风控管理器
      - 零余额连续计数
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
        """初始化配置、通知器、风控状态。在主循环前调用。"""
        self.cfg = resolve_config(self.user_id)
        self.notify = load_notifier(
            self.user_id, self.username, self.logger
        )
        restore_risk_state(self.user_id, self.rm)
        self.logger.info(
            f"{self.tag} 风控状态已恢复："
            f"连亏={self.rm._consecutive_losses}次，熔断={self.rm.is_fused}"
        )

    def on_balance_ok(self):
        """余额获取成功时重置零余额计数。"""
        self.zero_balance_count = 0

    def on_balance_zero(self):
        """余额获取为 0 时累计计数，达阈值推送告警。"""
        self.zero_balance_count += 1
        self.logger.warning(
            f"{self.tag} ⚠️ 余额获取为 0（连续第 {self.zero_balance_count} 次）"
        )
        if self.zero_balance_count == self.ZERO_BALANCE_ALERT_ROUNDS:
            alert_key = f"{self.user_id}:zero_balance"
            if should_alert(alert_key, self.ZERO_BALANCE_ALERT_COOLDOWN_SEC):
                self.notify(
                    f"⚠️ <b>{self.username}</b> 连续 {self.zero_balance_count} 轮余额为 0，"
                    f"无法开仓。\n请检查：\n"
                    f"• 模拟盘/实盘 Key 是否匹配\n"
                    f"• 合约账户是否有 USDT\n"
                    f"• API Key 权限是否包含「读取」"
                )

    def check_cross_day(self) -> bool:
        """跨日检测，返回 True 表示跨日已处理。"""
        today = datetime.now().strftime('%Y-%m-%d')
        if today == self.current_date:
            return False
        self.current_date = today
        return True
