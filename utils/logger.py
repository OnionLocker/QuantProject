"""
utils/logger.py - 日志系统（支持全局 logger 和每用户独立 logger）

- bot_logger: 全局日志（系统启动、Watchdog、无用户上下文的事件）
- get_user_logger(username): 每用户独立 logger，写入 tradelog/{username}.log

改进：使用 TimedRotatingFileHandler 按天轮转，保留 30 天历史日志。
"""
import logging
import logging.handlers
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

log_dir = os.path.join(project_root, 'tradelog')
os.makedirs(log_dir, exist_ok=True)

# ── 全局 Bot Logger（系统级事件，兼容旧版调用）────────────────────────────────

log_filepath = os.path.join(log_dir, "bot.log")

bot_logger = logging.getLogger("QuantBot")
bot_logger.setLevel(logging.INFO)

if not bot_logger.handlers:
    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    bot_logger.addHandler(console_handler)

    # 按天轮转，保留 30 天历史，午夜切换
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_filepath, when='midnight', interval=1,
        backupCount=30, encoding='utf-8'
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.setFormatter(formatter)
    bot_logger.addHandler(file_handler)


# ── 每用户独立 Logger ─────────────────────────────────────────────────────────

_user_loggers: dict = {}


def get_user_logger(username: str) -> logging.Logger:
    """
    返回指定用户的独立 Logger。
    - 日志写入 tradelog/{username}.log（按天轮转，保留30天）
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

        # 用户专属日志文件：tradelog/{username}.log（按天轮转）
        user_log_path = os.path.join(log_dir, f"{username}.log")
        file_handler = logging.handlers.TimedRotatingFileHandler(
            user_log_path, when='midnight', interval=1,
            backupCount=30, encoding='utf-8'
        )
        file_handler.suffix = "%Y%m%d"
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _user_loggers[username] = logger
    return logger
