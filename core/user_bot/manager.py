"""
core/user_bot/manager.py - 多用户 Bot 进程管理器

每个用户拥有独立的 Bot 线程 + RiskManager + 持仓状态，互相隔离。
"""
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from utils.logger import bot_logger
from risk.risk_manager import RiskManager
from utils.config_loader import get_config


class UserBotState:
    """单个用户的 Bot 运行时状态"""
    def __init__(self, user_id: int, username: str):
        self.user_id = user_id
        self.username = username

        cfg = get_config()
        rc = cfg.get("risk", {})
        self.risk_manager = RiskManager(
            max_trade_amount=rc.get("max_trade_amount", 1000),
            max_consecutive_losses=rc.get("max_consecutive_losses", 3),
            daily_loss_limit_pct=rc.get("daily_loss_limit_pct", 0.05),
        )

        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.started_at: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


# ── 全局注册表：user_id -> UserBotState ──────────────────────────────────────
_bots: Dict[int, UserBotState] = {}
_lock = threading.Lock()


def get_or_create(user_id: int, username: str) -> UserBotState:
    with _lock:
        if user_id not in _bots:
            _bots[user_id] = UserBotState(user_id, username)
        return _bots[user_id]


def get_bot(user_id: int) -> Optional[UserBotState]:
    return _bots.get(user_id)


def start_bot(user_id: int, username: str) -> dict:
    state = get_or_create(user_id, username)
    if state.is_running:
        return {"status": "already_running"}

    state.stop_event.clear()
    state.last_error = None

    def _run():
        from core.user_bot.runner import run_user_bot
        try:
            run_user_bot(state)
        except Exception as e:
            state.last_error = str(e)
            bot_logger.error(f"[User:{username}] Bot 异常退出: {e}")

    state.thread = threading.Thread(
        target=_run, daemon=True, name=f"Bot-{username}"
    )
    state.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.thread.start()
    return {"status": "started"}


def stop_bot(user_id: int) -> dict:
    state = _bots.get(user_id)
    if not state or not state.is_running:
        return {"status": "not_running"}
    state.stop_event.set()
    return {"status": "stop_signal_sent"}


def bot_status(user_id: int) -> dict:
    state = _bots.get(user_id)
    if not state:
        return {"running": False, "fused": False, "consecutive_losses": 0}
    return {
        "running":             state.is_running,
        "started_at":          state.started_at,
        "last_error":          state.last_error,
        "fused":               state.risk_manager.is_fused,
        "consecutive_losses":  state.risk_manager.consecutive_losses,
    }
