"""
utils/config_loader.py - 配置文件加载（支持热更新）

get_config() 每次调用都会检测文件是否修改，若有变化则重新加载。
Bot 主循环每轮调用 get_config() 即可获取最新配置，无需重启服务。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

import yaml

current_dir: str = os.path.dirname(os.path.abspath(__file__))
project_root: str = os.path.dirname(current_dir)
CONFIG_PATH: str = os.path.join(project_root, "config.yaml")

_lock: threading.Lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None
_last_mtime: float = 0.0


def get_config() -> Dict[str, Any]:
    """
    读取 config.yaml 并缓存，仅在文件修改时重新加载（热更新）。
    线程安全。
    """
    global _cache, _last_mtime
    try:
        mtime: float = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = 0.0

    with _lock:
        if _cache is None or mtime > _last_mtime:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache = yaml.safe_load(f)
            _last_mtime = mtime
        return _cache  # type: ignore[return-value]
