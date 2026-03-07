# 数据库迁移说明

## 2026-03-07: 修复trade_history表结构

### 问题
API服务器崩溃，错误：`sqlite3.OperationalError: no such column: pnl`

### 原因
代码期望的数据库表结构与实际不符：
- 代码期望的列：user_id, symbol, side, amount, entry_price, exit_price, pnl, fee, entry_time, exit_time, status
- 实际存在的列：id, timestamp, symbol, side, action, price, amount, reason

### 修复
重建了正确的trade_history表结构：

```sql
CREATE TABLE trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    pnl REAL,
    fee REAL,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    status TEXT DEFAULT 'open',
    FOREIGN KEY (user_id) REFERENCES users (id)
)
```

### 执行方法
```bash
python3 scripts/fix_database.py
```

