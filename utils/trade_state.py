"""
utils/trade_state.py - 持仓状态管理（SQLite 版）

⚠️ 已弃用：此模块为旧版单用户遗留代码，多用户版使用 core/user_bot/runner.py 中的
   _load_state/_save_state（基于 user_id 主键），不再使用本模块。
   保留仅用于向后兼容 legacy JSON 数据迁移。请勿新增引用。

多用户版状态管理在 core/user_bot/runner.py 中内联实现，不使用本模块。
避免循环导入：本模块直接使用 sqlite3 + DB_PATH，不导入 db_handler 中的函数。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Optional

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

# 直接定义 DB_PATH，避免与 db_handler 循环引用
DB_PATH = os.path.join(project_root, "trading_data.db")

# 保留 JSON 文件路径，仅用于首次迁移旧数据
_LEGACY_JSON = os.path.join(project_root, 'trade_state.json')


def _init_state_table() -> None:
    """在 SQLite 中建立 bot_state 表（若不存在）"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()


def _migrate_legacy():
    """一次性迁移：如果 trade_state.json 存在，读入并写到 SQLite，然后重命名备份"""
    if not os.path.exists(_LEGACY_JSON):
        return
    try:
        with open(_LEGACY_JSON, 'r', encoding='utf-8') as f:
            old_state = json.load(f)
        save_state(old_state)
        os.rename(_LEGACY_JSON, _LEGACY_JSON + '.migrated')
        print("📦 trade_state.json 已自动迁移至 SQLite，原文件重命名为 .migrated")
    except Exception as e:
        print(f"⚠️ 迁移 trade_state.json 失败（将继续使用 SQLite 空状态）: {e}")


_initialized = False


def _ensure_initialized() -> None:
    """延迟初始化：首次调用 load/save 时才建表和迁移，避免 import 时副作用。"""
    global _initialized
    if _initialized:
        return
    _init_state_table()
    _migrate_legacy()
    _initialized = True


def get_empty_state() -> Dict[str, Any]:
    """返回标准化的空状态字典"""
    return {
        "position_side": None,
        "position_amount": 0,
        "entry_price": 0.0,
        "active_sl": 0.0,
        "active_tp1": 0.0,
        "active_tp2": 0.0,
        "open_fee": 0.0,
        "margin_used": 0.0,
        "strategy_name": "",
        "signal_reason": "",
        "entry_time": "",
        "has_moved_to_breakeven": False,
        "has_taken_partial_profit": False,
        "exchange_order_ids": {
            "sl_order": None,
            "tp_order": None
        }
    }


def load_state() -> Dict[str, Any]:
    """从 SQLite 读取持仓状态；若无记录则返回空状态"""
    _ensure_initialized()
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key='trade_state'"
        ).fetchone()
        conn.close()
        if row:
            state = json.loads(row[0])
            empty = get_empty_state()
            for k, v in empty.items():
                if k not in state:
                    state[k] = v
            return state
    except Exception as e:
        print(f"❌ 读取持仓状态失败: {e}，将返回空状态。")
    return get_empty_state()


def save_state(state: dict):
    """原子化写入持仓状态到 SQLite"""
    _ensure_initialized()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('trade_state', ?)",
            (json.dumps(state, ensure_ascii=False),)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ 保存持仓状态失败: {e}")


def clear_state() -> None:
    """清空持仓状态（平仓后调用）"""
    save_state(get_empty_state())
