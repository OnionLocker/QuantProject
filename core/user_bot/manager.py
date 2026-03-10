"""
core/user_bot/manager.py - 多用户 Bot 进程管理器

每个用户拥有独立的 Bot 线程 + RiskManager + 持仓状态，互相隔离。
Watchdog 后台线程每60秒检测一次：若某 Bot 意外崩溃则自动重启（带退避）。
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
        self.user_id  = user_id
        self.username = username

        cfg = get_config()
        rc  = cfg.get("risk", {})
        self.risk_manager = RiskManager(
            max_trade_amount      = rc.get("max_trade_amount",       1000),
            max_consecutive_losses= rc.get("max_consecutive_losses", 3),
            daily_loss_limit_pct  = rc.get("daily_loss_limit_pct",   0.05),
        )

        self.thread:     Optional[threading.Thread] = None
        self.stop_event: threading.Event            = threading.Event()
        self.started_at: Optional[str]              = None
        self.last_error: Optional[str]              = None

        # Watchdog 退避：连续崩溃时逐步增加重启间隔
        self._crash_count:      int   = 0
        self._last_restart_at:  float = 0.0

    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


# ── 全局注册表：user_id -> UserBotState ──────────────────────────────────────
_bots: Dict[int, UserBotState] = {}
_lock = threading.Lock()

# 记录哪些 user_id 是用户主动停止的，主动停止的不自动重启
_manually_stopped: set = set()


def get_or_create(user_id: int, username: str) -> UserBotState:
    with _lock:
        if user_id not in _bots:
            _bots[user_id] = UserBotState(user_id, username)
        return _bots[user_id]


def get_bot(user_id: int) -> Optional[UserBotState]:
    return _bots.get(user_id)


def start_bot(user_id: int, username: str, strategy_name: str = None) -> dict:
    _ensure_watchdog()   # 首次 start_bot 时启动 Watchdog
    state = get_or_create(user_id, username)
    if state.is_running:
        return {"status": "already_running"}

    _manually_stopped.discard(user_id)
    state.stop_event.clear()
    state.last_error = None

    def _run():
        from core.user_bot.runner import run_user_bot
        try:
            run_user_bot(state, override_strategy=strategy_name)
        except Exception as e:
            state.last_error = str(e)
            bot_logger.error(f"[User:{username}] Bot 异常退出: {e}")

    state.thread    = threading.Thread(target=_run, daemon=True, name=f"Bot-{username}")
    state.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.thread.start()
    bot_logger.info(f"[Manager] Bot[{username}] 已启动" + (f"，策略覆盖={strategy_name}" if strategy_name else ""))
    return {"status": "started"}


def stop_bot(user_id: int) -> dict:
    state = _bots.get(user_id)
    if not state or not state.is_running:
        return {"status": "not_running"}
    _manually_stopped.add(user_id)
    state.stop_event.set()
    bot_logger.info(f"[Manager] Bot[{state.username}] 已发送停止信号")
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
        "crash_count":         state._crash_count,
    }


# ── Watchdog：Bot 意外崩溃后自动重启（带指数退避）────────────────────────────

def _get_notifier_for_user(user_id: int, username: str):
    """尽力加载用户的 Telegram notifier，失败则回退到全局。"""
    try:
        from execution.db_handler import load_tg_config
        from api.auth.crypto import decrypt
        from utils.notifier import make_notifier, send_telegram_msg, _GLOBAL_TOKEN, _GLOBAL_CHAT_ID
        raw = load_tg_config(user_id)
        if raw["tg_bot_token_enc"] and raw["tg_chat_id_enc"]:
            token   = decrypt(raw["tg_bot_token_enc"])
            chat_id = decrypt(raw["tg_chat_id_enc"])
            if token and chat_id:
                fn = make_notifier(token, chat_id)
                if fn:
                    return fn
    except Exception as e:
        bot_logger.warning(f"[Watchdog] 加载用户 {username} 的 TG 配置失败: {e}")

    # 回退到全局 .env 配置
    from utils.notifier import send_telegram_msg, _GLOBAL_TOKEN, _GLOBAL_CHAT_ID
    if _GLOBAL_TOKEN and _GLOBAL_CHAT_ID:
        return send_telegram_msg

    # 全局也无配置，返回空操作
    def _noop(msg: str) -> bool:
        bot_logger.debug(f"[Watchdog][{username}] 通知跳过: {msg[:80]}...")
        return False
    return _noop


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
            if bot_state.is_running:
                # Bot 运行中，崩溃计数归零
                bot_state._crash_count = 0
                continue

            # Bot 停止了（非主动），判断是否应该重启
            bot_state._crash_count += 1
            crash_count = bot_state._crash_count

            # 退避延迟：1次=30s，2次=60s，3次=120s，4次及以上=300s
            backoff_sec = min(300, 30 * (2 ** (crash_count - 1)))
            now = time.time()
            if now - bot_state._last_restart_at < backoff_sec:
                bot_logger.info(
                    "[Watchdog] Bot[%s] 崩溃第%d次，退避中（剩余%.0fs）",
                    bot_state.username, crash_count,
                    backoff_sec - (now - bot_state._last_restart_at)
                )
                continue

            bot_logger.warning(
                "[Watchdog] Bot[%s] 已停止（崩溃第%d次），尝试重启...",
                bot_state.username, crash_count
            )

            notify = _get_notifier_for_user(uid, bot_state.username)
            try:
                notify(
                    f"🔄 <b>{bot_state.username} Bot 已崩溃（第{crash_count}次）</b>\n"
                    f"Watchdog 正在自动重启..."
                )
                bot_state._last_restart_at = time.time()
                result = start_bot(uid, bot_state.username)
                bot_logger.info("[Watchdog] 重启结果: %s", result)

                if result.get("status") == "started":
                    notify(f"✅ <b>{bot_state.username} Bot 已自动重启</b>")
                else:
                    notify(
                        f"⚠️ <b>{bot_state.username} Bot 重启异常</b>\n"
                        f"状态: {result.get('status')}"
                    )
            except Exception as e:
                bot_logger.error("[Watchdog] 重启失败: %s", e)
                try:
                    notify(
                        f"🚨 <b>{bot_state.username} Bot Watchdog 重启失败</b>\n{str(e)[:200]}"
                    )
                except Exception:
                    pass


def _start_watchdog():
    t = threading.Thread(
        target=_watchdog_loop, kwargs={"check_interval": 60},
        daemon=True, name="BotWatchdog"
    )
    t.start()
    return t


# 延迟启动 Watchdog：首次 start_bot 时才启动，避免 import 时副作用
_watchdog_thread = None
_watchdog_started = False


def _ensure_watchdog():
    """确保 Watchdog 已启动（线程安全，只启动一次）。"""
    global _watchdog_thread, _watchdog_started
    if _watchdog_started:
        return
    with _lock:
        if not _watchdog_started:
            _watchdog_thread = _start_watchdog()
            _watchdog_started = True
