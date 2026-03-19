"""
execution/db_handler.py - 数据库初始化 & 工具函数（多用户版，兼容单用户）

表结构：
  users            - 用户账号
  user_api_keys    - OKX API Key（加密存储）
  user_config      - 每用户策略/品种/杠杆配置（独立于全局 config.yaml）
  trade_history    - 历史交易（带 user_id；单用户 user_id=0）
  daily_balance    - 每日余额快照（带 user_id；单用户 user_id=0）
  bot_state        - 持仓状态（带 user_id）
  risk_state       - 风控状态持久化（连亏次数、日初余额等）
  user_settings    - 每用户 Telegram 配置
"""
import sqlite3
import os
import time
import json
import logging
import threading
from datetime import datetime

logger = logging.getLogger("db_handler")

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
DB_PATH = os.path.join(project_root, "trading_data.db")

# ── 线程局部连接缓存（避免频繁创建/销毁连接）──────────────────────────────────
_thread_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """
    获取数据库连接，使用线程局部缓存复用连接。
    启用WAL模式以支持多线程并发读写。
    """
    conn = getattr(_thread_local, 'conn', None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")  # 验证连接可用
            return conn
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            # 连接已关闭或损坏，重建
            _thread_local.conn = None

    for attempt in range(5):
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            _thread_local.conn = conn
            return conn
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def close_thread_conn():
    """显式关闭当前线程的缓存连接（线程退出时可调用）。"""
    conn = getattr(_thread_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except (sqlite3.Error, OSError):
            pass
        _thread_local.conn = None


def init_db():
    """
    初始化数据库表结构，带版本号管理。
    每次新增迁移只需在 _MIGRATIONS 列表尾部追加即可。
    """
    conn = get_conn()
    c = conn.cursor()

    # ── Schema 版本管理表 ──────────────────────────────────────────────────
    c.execute('''
        CREATE TABLE IF NOT EXISTS schema_version (
            id      INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    ''')
    row = c.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    current_version = row[0] if row else 0
    if not row:
        c.execute("INSERT INTO schema_version (id, version) VALUES (1, 0)")

    # ── 迁移列表：每个元素是 (version_number, description, sql_list) ──────
    _MIGRATIONS = [
        (1, "初始表结构", [
            '''CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT    UNIQUE NOT NULL,
                hashed_password TEXT    NOT NULL,
                created_at      TEXT    DEFAULT (datetime('now'))
            )''',
            '''CREATE TABLE IF NOT EXISTS user_api_keys (
                user_id        INTEGER PRIMARY KEY REFERENCES users(id),
                api_key_enc    TEXT,
                secret_enc     TEXT,
                passphrase_enc TEXT,
                is_simulate    INTEGER DEFAULT 0,
                updated_at     TEXT    DEFAULT (datetime('now'))
            )''',
            '''CREATE TABLE IF NOT EXISTS user_config (
                user_id         INTEGER PRIMARY KEY REFERENCES users(id),
                symbol          TEXT    DEFAULT NULL,
                timeframe       TEXT    DEFAULT NULL,
                leverage        REAL    DEFAULT NULL,
                risk_pct        REAL    DEFAULT NULL,
                strategy_name   TEXT    DEFAULT NULL,
                strategy_params TEXT    DEFAULT NULL,
                risk_config     TEXT    DEFAULT NULL,
                updated_at      TEXT    DEFAULT (datetime('now'))
            )''',
            '''CREATE TABLE IF NOT EXISTS trade_history (
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
            )''',
            '''CREATE TABLE IF NOT EXISTS daily_balance (
                user_id  INTEGER NOT NULL DEFAULT 0,
                date     TEXT    NOT NULL,
                balance  REAL,
                PRIMARY KEY (user_id, date)
            )''',
            '''CREATE TABLE IF NOT EXISTS bot_state (
                user_id  INTEGER PRIMARY KEY DEFAULT 0,
                value    TEXT
            )''',
            '''CREATE TABLE IF NOT EXISTS risk_state (
                user_id              INTEGER PRIMARY KEY DEFAULT 0,
                consecutive_losses   INTEGER DEFAULT 0,
                daily_start_balance  REAL    DEFAULT NULL,
                daily_loss_triggered INTEGER DEFAULT 0,
                last_date            TEXT    DEFAULT NULL,
                updated_at           TEXT    DEFAULT (datetime('now'))
            )''',
            '''CREATE TABLE IF NOT EXISTS user_settings (
                user_id          INTEGER PRIMARY KEY REFERENCES users(id),
                tg_bot_token_enc TEXT    DEFAULT NULL,
                tg_chat_id_enc   TEXT    DEFAULT NULL,
                updated_at       TEXT    DEFAULT (datetime('now'))
            )''',
            '''CREATE TABLE IF NOT EXISTS backtest_history (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                created_at       TEXT    DEFAULT (datetime('now')),
                strategy         TEXT,
                symbol           TEXT,
                timeframe        TEXT,
                start_date       TEXT,
                end_date         TEXT,
                initial_capital  REAL,
                final_balance    REAL,
                roi_pct          REAL,
                win_rate_pct     REAL,
                total_trades     INTEGER,
                max_drawdown_pct REAL,
                full_result      TEXT
            )''',
        ]),
        (2, "兼容旧库: user_config 添加 risk_config 列", [
            "ALTER TABLE user_config ADD COLUMN risk_config TEXT DEFAULT NULL",
        ]),
        (3, "V2.5: 策略绩效追踪表", [
            '''CREATE TABLE IF NOT EXISTS strategy_performance (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                strategy_name TEXT    NOT NULL,
                pnl           REAL    NOT NULL,
                recorded_at   TEXT    DEFAULT (datetime('now'))
            )''',
            '''CREATE INDEX IF NOT EXISTS idx_strat_perf_user_strat
            ON strategy_performance(user_id, strategy_name)''',
        ]),
        (4, "V3.5: trade_history 升级为 entry/exit 模型", [
            "DROP TABLE IF EXISTS trade_history",
            '''CREATE TABLE IF NOT EXISTS trade_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL DEFAULT 0,
                symbol      TEXT,
                side        TEXT,
                amount      REAL,
                entry_price REAL,
                exit_price  REAL,
                pnl         REAL DEFAULT 0.0,
                fee         REAL DEFAULT 0.0,
                entry_time  TEXT,
                exit_time   TEXT,
                status      TEXT DEFAULT 'open'
            )''',
            '''CREATE INDEX IF NOT EXISTS idx_trade_user_time
            ON trade_history(user_id, entry_time)''',
        ]),
        (5, "V6.0: trade_history 对账体系 — 区分估算/真实盈亏", [
            "ALTER TABLE trade_history ADD COLUMN exit_reason TEXT DEFAULT NULL",
            "ALTER TABLE trade_history ADD COLUMN is_estimated INTEGER DEFAULT 0",
            "ALTER TABLE trade_history ADD COLUMN reconciled INTEGER DEFAULT 1",
            "ALTER TABLE trade_history ADD COLUMN fill_source TEXT DEFAULT NULL",
            "ALTER TABLE trade_history ADD COLUMN estimated_pnl REAL DEFAULT NULL",
            "ALTER TABLE trade_history ADD COLUMN exchange_trade_id TEXT DEFAULT NULL",
            "ALTER TABLE trade_history ADD COLUMN exchange_order_id TEXT DEFAULT NULL",
            "ALTER TABLE trade_history ADD COLUMN reconciled_at TEXT DEFAULT NULL",
        ]),
        # ── 未来新增迁移追加在这里 ──────────────────────────────────────────
        # (6, "xxx", ["ALTER TABLE ..."]),
    ]

    for version, desc, sqls in _MIGRATIONS:
        if version <= current_version:
            continue
        for sql in sqls:
            try:
                c.execute(sql)
            except Exception:
                pass  # CREATE IF NOT EXISTS / ALTER 已存在时忽略
        c.execute("UPDATE schema_version SET version=?, updated_at=datetime('now') WHERE id=1",
                  (version,))
        conn.commit()

    conn.commit()


# ── 回测历史存取 ──────────────────────────────────────────────────────────────

def save_backtest_history(user_id: int, result: dict):
    """保存一次回测结果，每用户最多保留最近 20 条。"""
    import json as _json
    conn = get_conn()
    conn.execute('''
        INSERT INTO backtest_history
          (user_id, strategy, symbol, timeframe,
           start_date, end_date, initial_capital, final_balance,
           roi_pct, win_rate_pct, total_trades, max_drawdown_pct, full_result)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        result.get("strategy"),
        result.get("symbol"),
        result.get("timeframe"),
        result.get("start_date"),
        result.get("end_date"),
        result.get("initial_capital"),
        result.get("final_balance"),
        result.get("roi_pct"),
        result.get("win_rate_pct"),
        result.get("total_trades"),
        result.get("max_drawdown_pct"),
        _json.dumps(result, ensure_ascii=False),
    ))
    # 超过 20 条时删除最旧的
    conn.execute('''
        DELETE FROM backtest_history
        WHERE user_id = ? AND id NOT IN (
            SELECT id FROM backtest_history
            WHERE user_id = ?
            ORDER BY id DESC LIMIT 20
        )
    ''', (user_id, user_id))
    conn.commit()


def load_backtest_history(user_id: int) -> list:
    """返回该用户最近 20 条回测摘要（不含 full_result）。"""
    conn = get_conn()
    rows = conn.execute('''
        SELECT id, created_at, strategy, symbol, timeframe,
               start_date, end_date, initial_capital, final_balance,
               roi_pct, win_rate_pct, total_trades, max_drawdown_pct
        FROM backtest_history
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 20
    ''', (user_id,)).fetchall()
    keys = ["id", "created_at", "strategy", "symbol", "timeframe",
            "start_date", "end_date", "initial_capital", "final_balance",
            "roi_pct", "win_rate_pct", "total_trades", "max_drawdown_pct"]
    return [dict(zip(keys, r)) for r in rows]


def load_backtest_history_detail(user_id: int, history_id: int) -> dict | None:
    """返回某条历史回测的完整结果（含 equity_curve）。"""
    import json as _json
    conn = get_conn()
    row = conn.execute(
        "SELECT full_result FROM backtest_history WHERE id=? AND user_id=?",
        (history_id, user_id)
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return _json.loads(row[0])
    except (ValueError, TypeError) as e:
        logger.warning("回测历史 JSON 解析失败 (id=%d): %s", history_id, e)
        return None


# ── 每用户策略/交易配置存取 ───────────────────────────────────────────────────

def save_user_config(user_id: int, config: dict):
    """
    保存用户的个性化配置（策略名、品种、杠杆等）。
    config 是一个 dict，key 为字段名，只更新传入的字段。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM user_config WHERE user_id=?", (user_id,)
    ).fetchone()

    strategy_params = config.get("strategy_params")
    if strategy_params is not None and isinstance(strategy_params, dict):
        strategy_params = json.dumps(strategy_params, ensure_ascii=False)

    _RISK_KEYS = ("max_consecutive_losses", "daily_loss_limit_pct", "max_trade_amount")
    if row:
        fields = []
        values = []
        for key in ("symbol", "timeframe", "leverage", "risk_pct", "strategy_name"):
            if key in config:
                fields.append(f"{key}=?")
                values.append(config[key])
        if strategy_params is not None:
            fields.append("strategy_params=?")
            values.append(strategy_params)
        # 风控参数：存入 JSON 字段（复用 strategy_params 思路，用单独字段更清晰）
        risk_cfg = {k: config[k] for k in _RISK_KEYS if k in config}
        if risk_cfg:
            # 读取已有 risk_config，合并后写回
            existing_risk = {}
            try:
                existing_risk = json.loads(row["risk_config"] or "{}") if "risk_config" in row.keys() else {}
            except Exception:
                pass
            existing_risk.update(risk_cfg)
            fields.append("risk_config=?")
            values.append(json.dumps(existing_risk))
        fields.append("updated_at=datetime('now')")
        values.append(user_id)
        conn.execute(
            f"UPDATE user_config SET {', '.join(fields)} WHERE user_id=?",
            values
        )
    else:
        risk_cfg = {k: config[k] for k in _RISK_KEYS if k in config}
        conn.execute('''
            INSERT INTO user_config
              (user_id, symbol, timeframe, leverage, risk_pct,
               strategy_name, strategy_params, risk_config, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ''', (
            user_id,
            config.get("symbol"),
            config.get("timeframe"),
            config.get("leverage"),
            config.get("risk_pct"),
            config.get("strategy_name"),
            strategy_params,
            json.dumps(risk_cfg) if risk_cfg else None,
        ))
    conn.commit()


def load_user_config(user_id: int) -> dict:
    """
    加载用户个性化配置，未配置的字段返回 None（调用方应 fallback 到 config.yaml）。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM user_config WHERE user_id=?", (user_id,)
    ).fetchone()

    if not row:
        return {}

    params = row["strategy_params"]
    if params:
        try:
            params = json.loads(params)
        except (ValueError, TypeError):
            params = {}
    else:
        params = {}

    # 读取风控配置
    risk_cfg = {}
    try:
        keys = [d[0] for d in row.description] if hasattr(row, 'description') else list(row.keys())
        if "risk_config" in keys and row["risk_config"]:
            risk_cfg = json.loads(row["risk_config"])
    except (ValueError, TypeError, AttributeError):
        pass

    return {
        "symbol":                  row["symbol"],
        "timeframe":               row["timeframe"],
        "leverage":                row["leverage"],
        "risk_pct":                row["risk_pct"],
        "strategy_name":           row["strategy_name"],
        "strategy_params":         params,
        "updated_at":              row["updated_at"],
        # 风控参数（若未配置则为 None，runner.py 会 fallback config.yaml）
        "max_consecutive_losses":  risk_cfg.get("max_consecutive_losses"),
        "daily_loss_limit_pct":    risk_cfg.get("daily_loss_limit_pct"),
        "max_trade_amount":        risk_cfg.get("max_trade_amount"),
    }


# ── Telegram 用户配置存取 ──────────────────────────────────────────────────────

def save_tg_config(user_id: int, tg_bot_token_enc: str, tg_chat_id_enc: str):
    conn = get_conn()
    conn.execute('''
        INSERT OR REPLACE INTO user_settings
          (user_id, tg_bot_token_enc, tg_chat_id_enc, updated_at)
        VALUES (?, ?, ?, datetime('now'))
    ''', (user_id, tg_bot_token_enc, tg_chat_id_enc))
    conn.commit()


def load_tg_config(user_id: int) -> dict:
    """返回加密存储的 token/chat_id，调用方负责解密。未配置则返回空字符串。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT tg_bot_token_enc, tg_chat_id_enc FROM user_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()
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


def load_risk_state(user_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM risk_state WHERE user_id=?", (user_id,)
    ).fetchone()
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
    conn.execute(
        "INSERT OR REPLACE INTO daily_balance (user_id, date, balance) VALUES (?, ?, ?)",
        (_user_id, today, _balance)
    )
    conn.commit()


# ── record_trade（单/多用户兼容）─────────────────────────────────────────────

def record_trade(user_id=None, side=None, price=None, amount=None,
                 symbol: str = "BTC/USDT", action: str = "未知",
                 pnl: float = 0.0, reason: str = "",
                 # ── V6.0 对账体系新参数 ──
                 is_estimated: bool = False,
                 fill_source: str = "",
                 exit_reason: str = "",
                 exchange_trade_id: str = "",
                 exchange_order_id: str = "",
                 fee: float = 0.0,
                 entry_price_override: float = None,
                 ) -> int:
    """
    写入交易记录，支持对账体系。

    V6.0 新增参数：
      - is_estimated:  True = 盈亏为估算值（待对账）
      - fill_source:   成交价来源（如 "交易所真实成交价", "⚠️ 近似估算"）
      - exit_reason:   退出原因（止损/止盈/追踪止损/策略反转/时间止损等）
      - exchange_trade_id / exchange_order_id: 交易所侧 ID
      - fee:           手续费
      - entry_price_override: 用于平仓记录中记录开仓价

    返回新插入记录的 id。
    """
    _user_id = 0 if (user_id is None) else int(user_id)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _side = str(side or '')
    _action = str(action or '')
    _price = float(price or 0)
    _amount = float(amount or 0)
    _status = 'closed' if ('平仓' in _action or _action.lower() == 'close') else 'open'
    _pnl = float(pnl or 0)
    _fee = float(fee or 0)
    _is_estimated = 1 if is_estimated else 0
    _reconciled = 0 if is_estimated else 1
    _entry_price = float(entry_price_override or _price)
    _exit_price = _price if _status == 'closed' else None

    conn = get_conn()
    try:
        cursor = conn.execute('''
            INSERT INTO trade_history
              (user_id, symbol, side, amount, entry_price, exit_price,
               pnl, fee, entry_time, exit_time, status,
               exit_reason, is_estimated, reconciled, fill_source,
               estimated_pnl, exchange_trade_id, exchange_order_id)
            VALUES (?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?)
        ''', (
            _user_id,
            symbol,
            _side,
            _amount,
            _entry_price,
            _exit_price,
            _pnl,
            _fee,
            now,
            now if _status == 'closed' else None,
            _status,
            exit_reason or None,
            _is_estimated,
            _reconciled,
            fill_source or None,
            _pnl if is_estimated else None,
            exchange_trade_id or None,
            exchange_order_id or None,
        ))
    except sqlite3.OperationalError:
        # 兼容旧版表结构（V6.0 迁移前）：回退到基础字段
        logger.debug("trade_history 新字段不存在，回退到旧版 INSERT")
        cursor = conn.execute('''
            INSERT INTO trade_history
              (user_id, symbol, side, amount, entry_price, exit_price,
               pnl, fee, entry_time, exit_time, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            _user_id, symbol, _side, _amount, _entry_price, _exit_price,
            _pnl, _fee, now, now if _status == 'closed' else None, _status,
        ))
    trade_id = cursor.lastrowid
    conn.commit()
    return trade_id


def reconcile_trade(trade_id: int, real_pnl: float, real_exit_price: float,
                    fill_source: str, exchange_trade_id: str = "",
                    fee: float = None):
    """
    V6.0: 对账回填 — 将一笔 estimated 交易更新为真实已对账结果。
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()

    # 先读取原始估算值保留到 estimated_pnl
    row = conn.execute(
        "SELECT pnl, is_estimated FROM trade_history WHERE id=?", (trade_id,)
    ).fetchone()
    if not row:
        return

    estimated_pnl = row[0] if row[1] else None

    update_fields = {
        "pnl": real_pnl,
        "exit_price": real_exit_price,
        "is_estimated": 0,
        "reconciled": 1,
        "fill_source": fill_source,
        "reconciled_at": now,
        "exchange_trade_id": exchange_trade_id or None,
    }
    if estimated_pnl is not None:
        update_fields["estimated_pnl"] = estimated_pnl
    if fee is not None:
        update_fields["fee"] = fee

    set_clause = ", ".join(f"{k}=?" for k in update_fields)
    values = list(update_fields.values()) + [trade_id]
    conn.execute(
        f"UPDATE trade_history SET {set_clause} WHERE id=?",
        values
    )
    conn.commit()
    logger.info("对账回填完成: trade_id=%d, 估算=%.2f → 真实=%.2f",
                trade_id, estimated_pnl or 0, real_pnl)


def get_pending_reconcile_trades(user_id: int, limit: int = 20) -> list:
    """
    V6.0: 获取待对账的交易记录（is_estimated=1 且 reconciled=0）。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, symbol, side, amount, entry_price, exit_price, pnl, "
        "       entry_time, exit_time, fill_source, exchange_order_id "
        "FROM trade_history "
        "WHERE user_id=? AND is_estimated=1 AND reconciled=0 "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    keys = ["id", "symbol", "side", "amount", "entry_price", "exit_price",
            "pnl", "entry_time", "exit_time", "fill_source", "exchange_order_id"]
    return [dict(zip(keys, r)) for r in rows]


# init_db() 已移至 api/server.py 的 startup 事件中显式调用，
# 避免 import 时自动执行造成测试污染或副作用。
