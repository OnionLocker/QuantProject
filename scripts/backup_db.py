#!/usr/bin/env python3
"""
scripts/backup_db.py - 每日数据库备份脚本

功能：
  1. 将 trading_data.db 备份到 backups/ 目录，文件名含日期时间戳
  2. 保留最近 N 份备份，自动清理旧文件
  3. 备份完成后验证文件完整性（SQLite integrity_check）
  4. 输出备份结果日志

用法：
  手动：python3 scripts/backup_db.py
  定时：cron / systemd timer 调用，建议每天 00:05 执行一次

  crontab 示例（每天凌晨0点5分）：
  5 0 * * * cd /path/to/QuantProject && python3 scripts/backup_db.py >> logs/backup.log 2>&1
"""
import os
import sys
import shutil
import sqlite3
import glob
from datetime import datetime

# ── 配置 ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH      = os.path.join(PROJECT_ROOT, "trading_data.db")
BACKUP_DIR   = os.path.join(PROJECT_ROOT, "backups")
KEEP_COPIES  = 7   # 保留最近 N 份，超出自动删除


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_backup():
    # 确保备份目录存在
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # 检查源数据库
    if not os.path.exists(DB_PATH):
        log(f"❌ 数据库不存在: {DB_PATH}")
        return False

    db_size = os.path.getsize(DB_PATH)
    log(f"📦 开始备份: {DB_PATH} ({db_size / 1024:.1f} KB)")

    # 备份文件名：trading_data_2026-03-09_000500.db
    ts_str  = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst     = os.path.join(BACKUP_DIR, f"trading_data_{ts_str}.db")

    # 使用 SQLite online backup API（安全备份，支持写操作中途备份）
    try:
        src_conn = sqlite3.connect(DB_PATH)
        dst_conn = sqlite3.connect(dst)
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
    except Exception as e:
        log(f"❌ 备份失败（SQLite backup API）: {e}")
        # fallback: 直接文件复制
        try:
            shutil.copy2(DB_PATH, dst)
            log(f"⚠️  使用文件复制 fallback")
        except Exception as e2:
            log(f"❌ 文件复制也失败: {e2}")
            return False

    # 完整性验证
    try:
        check_conn = sqlite3.connect(dst)
        result = check_conn.execute("PRAGMA integrity_check").fetchone()
        check_conn.close()
        if result and result[0] == "ok":
            dst_size = os.path.getsize(dst)
            log(f"✅ 备份成功: {os.path.basename(dst)} ({dst_size / 1024:.1f} KB) integrity=ok")
        else:
            log(f"⚠️  备份文件完整性检查失败: {result}")
            os.remove(dst)
            return False
    except Exception as e:
        log(f"⚠️  完整性验证异常: {e}")

    # 清理旧备份，只保留最近 KEEP_COPIES 份
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "trading_data_*.db")))
    if len(backups) > KEEP_COPIES:
        to_delete = backups[:len(backups) - KEEP_COPIES]
        for old in to_delete:
            try:
                os.remove(old)
                log(f"🗑️  清理旧备份: {os.path.basename(old)}")
            except Exception as e:
                log(f"⚠️  清理失败: {e}")

    remaining = glob.glob(os.path.join(BACKUP_DIR, "trading_data_*.db"))
    log(f"📂 当前备份数量: {len(remaining)} / {KEEP_COPIES}")
    return True


if __name__ == "__main__":
    ok = run_backup()
    sys.exit(0 if ok else 1)
