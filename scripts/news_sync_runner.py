#!/usr/bin/env python3
"""
标准化新闻同步执行入口。

设计目标：
1. 作为 OpenClaw cron 的固定执行单元，避免 cron prompt 自由发挥。
2. 每次执行流程保持一致：读取配置 -> 判断是否启用 -> 抓取新闻 -> 分析 -> 写回 news_summary。
3. 当前实现先复用项目现有 news_fetcher，确保稳定可运行；后续可再替换为真正的 OpenClaw 网页分析模式。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_URL = "http://127.0.0.1:8080/api/news-sync/config"


def main() -> int:
    try:
        with urllib.request.urlopen(CONFIG_URL, timeout=10) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "config", "error": str(e)}, ensure_ascii=False))
        return 1

    if not cfg.get("enabled_by_weight"):
        print(json.dumps({"ok": True, "skipped": True, "reason": "news_weight_is_zero"}, ensure_ascii=False))
        return 0

    try:
        from news.news_fetcher import analyze_news
        result = analyze_news(force=True)
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "analyze_news", "error": str(e)}, ensure_ascii=False))
        return 2

    payload = {
        "provider": "openclaw",
        "symbol": "BTC/USDT:USDT",
        "lookback_hours": cfg.get("news_sync", {}).get("lookback_hours", 12),
        "interval_hours": cfg.get("news_sync", {}).get("interval_hours", 4),
        "regime_hint": result.get("regime_hint", "unknown"),
        "confidence": max(0.0, min(1.0, abs(float(result.get("combined_score", 0.0))))),
        "combined_score": float(result.get("combined_score", 0.0)),
        "crypto_score": float(result.get("crypto_score", 0.0)),
        "macro_score": float(result.get("macro_score", 0.0)),
        "summary_text": result.get("summary_text", ""),
        "headlines": result.get("headlines", []),
    }

    try:
        import requests
        r = requests.post("http://127.0.0.1:8080/api/news-sync/ingest", json=payload, timeout=20)
        print(json.dumps({"ok": r.ok, "mode": "internal_standardized_runner", "status_code": r.status_code, "response": r.text[:500], "payload": payload}, ensure_ascii=False))
        return 0 if r.ok else 3
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "post_ingest", "error": str(e), "payload": payload}, ensure_ascii=False))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
