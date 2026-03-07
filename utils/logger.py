"""
utils/logger.py - 日志系统（支持全局 logger 和每用户独立 logger）

- bot_logger: 全局日志（系统启动、Watchdog、无用户上下文的事件）
- get_user_logger(username): 每用户独立 logger，写入 tradelog/{username}.log
"""
import logging
import os
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

log_dir = os.path.join(project_root, 'tradelog')
os.makedirs(log_dir, exist_ok=True)

# ── 全局 Bot Logger（系统级事件，兼容旧版调用）────────────────────────────────

current_date = datetime.now().strftime('%Y%m%d')
log_filename = f"{current_date}_trade_log.log"
log_filepath = os.path.join(log_dir, log_filename)

bot_logger = logging.getLogger("QuantBot")
bot_logger.setLevel(logging.INFO)

if not bot_logger.handlers:
    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    bot_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
    file_handler.setFormatter(formatter)
    bot_logger.addHandler(file_handler)


# ── 每用户独立 Logger ─────────────────────────────────────────────────────────

_user_loggers: dict = {}


def get_user_logger(username: str) -> logging.Logger:
    """
    返回指定用户的独立 Logger。
    - 日志写入 tradelog/{username}.log（追加模式）
    - 同时输出到终端（带 [username] 前缀方便区分）
    - 同一个 username 只初始化一次（缓存复用）
    """
    if username in _user_loggers:
        return _user_loggers[username]

    logger_name = f"QuantBot.{username}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不向上传播到 bot_logger，避免重复输出

    if not logger.handlers:
        formatter = logging.Formatter(
            f'[%(asctime)s][{username}] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # 终端输出
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 用户专属日志文件：tradelog/{username}.log
        user_log_path = os.path.join(log_dir, f"{username}.log")
        file_handler = logging.FileHandler(user_log_path, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _user_loggers[username] = logger
    return logger
