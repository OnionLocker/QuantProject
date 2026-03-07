"""
utils/config_loader.py - 配置文件加载（支持热更新）

get_config() 每次调用都会检测文件是否修改，若有变化则重新加载。
Bot 主循环每轮调用 get_config() 即可获取最新配置，无需重启服务。
"""
import os
import yaml
import threading

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
CONFIG_PATH = os.path.join(project_root, "config.yaml")

_lock       = threading.Lock()
_cache      = None
_last_mtime = 0.0


def get_config() -> dict:
    """
    读取 config.yaml 并缓存，仅在文件修改时重新加载（热更新）。
    线程安全。
    """
    global _cache, _last_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = 0.0

    with _lock:
        if _cache is None or mtime > _last_mtime:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache = yaml.safe_load(f)
            _last_mtime = mtime
        return _cache
