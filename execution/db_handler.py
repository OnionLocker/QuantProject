import sqlite3
import os
from datetime import datetime

# 定位到项目根目录，也就是存放 main.py 的地方
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
DB_PATH = os.path.join(project_root, "trading_data.db")

def init_db():
    """初始化数据库，创建所需的表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. 每日余额表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_balance (
            date TEXT PRIMARY KEY,
            balance REAL
        )
    ''')
    
    # 2. 交易详情表（包含了 pnl 字段记录盈亏）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            action TEXT,
            price REAL,
            amount REAL,
            pnl REAL DEFAULT 0.0,
            reason TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

def record_balance(balance):
    """记录当日余额"""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("REPLACE INTO daily_balance (date, balance) VALUES (?, ?)", (today, balance))
        conn.commit()
    except Exception as e:
        print(f"写入余额失败: {e}")
    finally:
        conn.close()

def record_trade(side, price, amount, symbol="BTC/USDT", action="未知", pnl=0.0, reason=""):
    """记录具体的交易动作和盈亏"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO trade_history (timestamp, symbol, side, action, price, amount, pnl, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now, symbol, side, action, price, amount, pnl, reason))
        conn.commit()
    except Exception as e:
        print(f"写入交易记录失败: {e}")
    finally:
        conn.close()

init_db()