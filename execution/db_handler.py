"""
execution/db_handler.py - 数据库初始化 & 工具函数（多用户版，兼容单用户）

表结构：
  users            - 用户账号
  user_api_keys    - OKX API Key（加密存储）
  trade_history    - 历史交易（带 user_id；单用户 user_id=0）
  daily_balance    - 每日余额快照（带 user_id；单用户 user_id=0）
  bot_state        - 持仓状态（带 user_id）
  risk_state       - 风控状态持久化（连亏次数、日初余额等）
"""
import sqlite3
import os
import time
import json
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
DB_PATH = os.path.join(project_root, "trading_data.db")


def get_conn() -> sqlite3.Connection:
    """获取数据库连接，启用WAL模式以支持多线程并发读写，写入冲突自动重试。"""
    for attempt in range(5):
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT    UNIQUE NOT NULL,
            hashed_password TEXT    NOT NULL,
            created_at      TEXT    DEFAULT (datetime('now'))
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS user_api_keys (
            user_id        INTEGER PRIMARY KEY REFERENCES users(id),
            api_key_enc    TEXT,
            secret_enc     TEXT,
            passphrase_enc TEXT,
            is_simulate    INTEGER DEFAULT 0,
            updated_at     TEXT    DEFAULT (datetime('now'))
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS trade_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT,
            symbol    TEXT,
            side      TEXT,
            action    TEXT,
            price     REAL,
            amount    REAL,
            pnl       REAL DEFAULT 0.0,
            reason    TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_balance (
            user_id  INTEGER NOT NULL DEFAULT 0,
            date     TEXT    NOT NULL,
            balance  REAL,
            PRIMARY KEY (user_id, date)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            user_id  INTEGER PRIMARY KEY DEFAULT 0,
            value    TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS risk_state (
            user_id              INTEGER PRIMARY KEY DEFAULT 0,
            consecutive_losses   INTEGER DEFAULT 0,
            daily_start_balance  REAL    DEFAULT NULL,
            daily_loss_triggered INTEGER DEFAULT 0,
            last_date            TEXT    DEFAULT NULL,
            updated_at           TEXT    DEFAULT (datetime('now'))
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id          INTEGER PRIMARY KEY REFERENCES users(id),
            tg_bot_token_enc TEXT    DEFAULT NULL,
            tg_chat_id_enc   TEXT    DEFAULT NULL,
            updated_at       TEXT    DEFAULT (datetime('now'))
        )
    ''')

    conn.commit()
    conn.close()


# ── Telegram 用户配置存取 ──────────────────────────────────────────────────────

def save_tg_config(user_id: int, tg_bot_token_enc: str, tg_chat_id_enc: str):
    conn = get_conn()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO user_settings
              (user_id, tg_bot_token_enc, tg_chat_id_enc, updated_at)
            VALUES (?, ?, ?, datetime('now'))
        ''', (user_id, tg_bot_token_enc, tg_chat_id_enc))
        conn.commit()
    finally:
        conn.close()


def load_tg_config(user_id: int) -> dict:
    """返回加密存储的 token/chat_id，调用方负责解密。未配置则返回空字符串。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT tg_bot_token_enc, tg_chat_id_enc FROM user_settings WHERE user_id=?",
            (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {
            "tg_bot_token_enc": row["tg_bot_token_enc"] or "",
            "tg_chat_id_enc":   row["tg_chat_id_enc"]   or "",
        }
    return {"tg_bot_token_enc": "", "tg_chat_id_enc": ""}


# ── 风控状态持久化 ──────────────────────────────────────────────────────────────

def save_risk_state(user_id: int, consecutive_losses: int,
                    daily_start_balance, daily_loss_triggered: bool,
                    last_date: str = None):
    today = last_date or datetime.now().strftime('%Y-%m-%d')
    conn = get_conn()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO risk_state
              (user_id, consecutive_losses, daily_start_balance,
               daily_loss_triggered, last_date, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        ''', (user_id, consecutive_losses,
              daily_start_balance,
              1 if daily_loss_triggered else 0,
              today))
        conn.commit()
    finally:
        conn.close()


def load_risk_state(user_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM risk_state WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {
            "consecutive_losses":   int(row["consecutive_losses"]),
            "daily_start_balance":  row["daily_start_balance"],
            "daily_loss_triggered": bool(row["daily_loss_triggered"]),
            "last_date":            row["last_date"],
        }
    return {
        "consecutive_losses":   0,
        "daily_start_balance":  None,
        "daily_loss_triggered": False,
        "last_date":            None,
    }


# ── record_balance（单/多用户兼容）────────────────────────────────────────────

def record_balance(user_id_or_balance, balance: float = None):
    """
    多用户版：record_balance(user_id: int, balance: float)
    单用户版：record_balance(balance: float)   ← user_id 自动为 0
    """
    if balance is None:
        _user_id = 0
        _balance = float(user_id_or_balance)
    else:
        _user_id = int(user_id_or_balance)
        _balance = float(balance)

    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_balance (user_id, date, balance) VALUES (?, ?, ?)",
            (_user_id, today, _balance)
        )
        conn.commit()
    finally:
        conn.close()


# ── record_trade（单/多用户兼容）─────────────────────────────────────────────

def record_trade(user_id=None, side=None, price=None, amount=None,
                 symbol: str = "BTC/USDT", action: str = "未知",
                 pnl: float = 0.0, reason: str = ""):
    """
    多用户版：record_trade(user_id=1, side='buy', price=..., amount=..., ...)
    单用户版：record_trade(side='buy', price=..., amount=..., ...)
              ← user_id 不传，自动为 0

    注意：两种调用都请使用关键字参数，避免歧义。
    """
    _user_id = 0 if (user_id is None) else int(user_id)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()
    try:
        conn.execute('''
            INSERT INTO trade_history
              (user_id, timestamp, symbol, side, action, price, amount, pnl, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (_user_id, now, symbol, side, action,
              float(price or 0), float(amount or 0), pnl, reason))
        conn.commit()
    finally:
        conn.close()


init_db()
