"""
execution/order_manager.py - ⚠️ 已废弃，请勿使用

此文件是单用户版遗留代码，其逻辑已完全内联到多用户版：
  core/user_bot/runner.py

保留本文件仅为防止旧代码 import 时 ImportError。
新功能请在 runner.py 中修改，不要在此文件添加代码。
"""

import warnings as _warnings

_warnings.warn(
    "order_manager.py 已废弃，请使用 core/user_bot/runner.py 中的下单逻辑。",
    DeprecationWarning,
    stacklevel=2,
)
