#!/usr/bin/env python3
"""
scripts/fetch_news.py - 新闻抓取定时任务入口

由 systemd timer 每 30 分钟调用一次。
抓取 news_sources.yaml 中配置的所有新闻源，
写入 trading_data.db 供 AUTO 策略选择器读取。

用法：
  python3 scripts/fetch_news.py           # 正常执行（有缓存时跳过）
  python3 scripts/fetch_news.py --force   # 强制重新抓取所有源
  python3 scripts/fetch_news.py --status  # 仅输出最新情绪状态，不抓取
"""
import sys
import os
import json
import logging
from datetime import datetime

# 确保项目根目录在 Python path 里
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── 日志配置（写文件 + 控制台）────────────────────────────────────────────────
LOG_DIR  = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "news_fetcher.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("fetch_news")


def main():
    force  = "--force"  in sys.argv
    status = "--status" in sys.argv

    from news.news_fetcher import (
        fetch_and_analyze,
        get_latest_sentiment,
        get_sentiment_age_minutes,
    )

    if status:
        # 只查询当前状态
        s = get_latest_sentiment()
        if s:
            age = get_sentiment_age_minutes()
            print(json.dumps({**s, "age_minutes": round(age, 1)},
                              ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"status": "no_data", "message": "尚未执行过新闻抓取"}, indent=2))
        return 0

    # ── 执行抓取 ──────────────────────────────────────────────────────────────
    start_ts = datetime.now()
    logger.info(f"{'强制' if force else ''}新闻抓取开始...")

    try:
        result = fetch_and_analyze(force=force)
    except Exception as e:
        logger.error(f"新闻抓取异常: {e}", exc_info=True)
        return 1

    elapsed = (datetime.now() - start_ts).total_seconds()

    if result["article_count"] == 0:
        logger.info(f"所有源缓存有效，无需更新（耗时 {elapsed:.1f}s）")
    else:
        logger.info(
            f"抓取完成: {result['article_count']} 条新闻 | "
            f"crypto={result['crypto_score']:+.2f} "
            f"macro={result['macro_score']:+.2f} "
            f"综合={result['combined_score']:+.2f} "
            f"→ {result['regime_hint']} "
            f"(耗时 {elapsed:.1f}s)"
        )

    # 写入状态文件（供 HEARTBEAT 检查）
    status_file = os.path.join(PROJECT_ROOT, "data", "news_status.json")
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump({
                **result,
                "last_run":     start_ts.isoformat(),
                "elapsed_sec":  round(elapsed, 1),
                "forced":       force,
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写入状态文件失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
