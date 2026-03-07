#!/usr/bin/env python3
"""
数据库修复脚本
修复trade_history表结构不匹配问题
"""

import sqlite3
import os

def fix_database():
    db_path = 'trading_data.db'
    print(f'修复数据库: {db_path}')
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 备份旧数据
    cursor.execute('SELECT * FROM trade_history')
    old_data = cursor.fetchall()
    print(f'备份 {len(old_data)} 条旧记录')
    
    # 删除旧表
    cursor.execute('DROP TABLE IF EXISTS trade_history_old')
    cursor.execute('CREATE TABLE trade_history_old AS SELECT * FROM trade_history')
    cursor.execute('DROP TABLE trade_history')
    
    # 创建新表（正确的结构）
    cursor.execute('''
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
    ''')
    
    print('✅ trade_history表重建完成')
    
    conn.commit()
    conn.close()
    print('✅ 数据库修复完成')

if __name__ == '__main__':
    fix_database()
