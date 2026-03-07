"""
core/user_bot/manager.py - 多用户 Bot 进程管理器

每个用户拥有独立的 Bot 线程 + RiskManager + 持仓状态，互相隔离。
Watchdog 后台线程每60秒检测一次：若某 Bot 意外崩溃则自动重启。
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

    _manually_stopped.discard(user_id)  # 用户主动启动，解除停止标记
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
    _manually_stopped.add(user_id)   # 标记为主动停止，Watchdog 不重启
    state.stop_event.set()
    return {"status": "stop_signal_sent"}


def resume_bot(user_id: int, username: str) -> dict:
    """用户手动恢复（熔断后点击恢复），清除主动停止标记并重启 Bot。"""
    _manually_stopped.discard(user_id)
    return start_bot(user_id, username)


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


# ── Watchdog：Bot 意外崩溃后自动重启 ─────────────────────────────────────────

# 记录哪些 user_id 是用户主动停止的，主动停止的不自动重启
_manually_stopped: set = set()


def _watchdog_loop(check_interval: int = 60):
    """后台守护线程，定期检测所有 Bot 是否存活，崩溃的自动重启。"""
    bot_logger.info("[Watchdog] 已启动，检测间隔 %ds", check_interval)
    while True:
        time.sleep(check_interval)
        with _lock:
            targets = list(_bots.items())
        for uid, bot_state in targets:
            if uid in _manually_stopped:
                continue
            if not bot_state.is_running:
                bot_logger.warning(
                    "[Watchdog] Bot[%s] 已停止（非主动），尝试自动重启...",
                    bot_state.username
                )
                try:
                    from utils.notifier import send_telegram_msg
                    send_telegram_msg(
                        f"🔄 <b>{bot_state.username} Bot 已崩溃，Watchdog 正在自动重启...</b>"
                    )
                    result = start_bot(uid, bot_state.username)
                    bot_logger.info("[Watchdog] 重启结果: %s", result)
                    send_telegram_msg(
                        f"✅ <b>{bot_state.username} Bot 已自动重启</b>"
                    )
                except Exception as e:
                    bot_logger.error("[Watchdog] 重启失败: %s", e)


def _start_watchdog():
    t = threading.Thread(
        target=_watchdog_loop, kwargs={"check_interval": 60},
        daemon=True, name="BotWatchdog"
    )
    t.start()
    return t


# 应用启动时自动运行 Watchdog
_watchdog_thread = _start_watchdog()
