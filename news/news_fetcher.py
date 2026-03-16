"""
news/news_fetcher.py - 新闻抓取与情绪分析模块

功能：
  1. 读取 news_sources.yaml 配置，抓取启用的 RSS/JSON 新闻源
  2. 关键词预匹配快速计分（不消耗 token）
  3. 超过阈值时调用 AI 进行深度情绪分析（hybrid 模式节省 token）
  4. 结果写入 SQLite（news_cache 表），供选择器读取
  5. 支持 TTL 缓存，同一来源短时间内不重复抓取

设计原则：
  - 完全解耦于策略层，策略选择器只读取分析结果，不关心来源
  - 失败静默降级：网络错误不影响 Bot 主循环
  - 可以由 OpenClaw cron 定时触发，也可由 Bot 主循环按需调用
"""

import os
import re
import json
import time
import logging
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import yaml

logger = logging.getLogger("news_fetcher")

# ── 路径 ─────────────────────────────────────────────────────────────────────
_DIR         = os.path.dirname(os.path.abspath(__file__))
_SOURCES_CFG = os.path.join(_DIR, "news_sources.yaml")
_PROJECT_ROOT = os.path.dirname(_DIR)
_DB_PATH     = os.path.join(_PROJECT_ROOT, "trading_data.db")


def _get_news_conn() -> sqlite3.Connection:
    """获取新闻模块专用的 SQLite 连接（WAL模式 + busy timeout）。
    独立于 db_handler 的连接池，避免循环导入。"""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ── DB：创建 news_cache 表 ────────────────────────────────────────────────────

def _ensure_table():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_sentiment (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at   TEXT    NOT NULL,
            source_name  TEXT,
            category     TEXT,
            headline     TEXT,
            sentiment    REAL,    -- [-1.0, 1.0]
            method       TEXT     -- 'keyword' | 'ai'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_summary (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT    NOT NULL,
            crypto_score REAL,   -- 加密新闻综合情绪 [-1, 1]
            macro_score  REAL,   -- 宏观新闻综合情绪 [-1, 1]
            combined_score REAL, -- 加权综合
            regime_hint  TEXT,   -- 'bull' | 'bear' | 'ranging' | 'unknown'
            summary_text TEXT    -- 可读摘要
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_fetch_cache (
            source_name  TEXT PRIMARY KEY,
            last_fetched TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ── 配置加载 ──────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(_SOURCES_CFG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── TTL 缓存检查 ──────────────────────────────────────────────────────────────

def _is_cache_fresh(source_name: str, ttl_min: int) -> bool:
    conn = sqlite3.connect(_DB_PATH)
    try:
        row = conn.execute(
            "SELECT last_fetched FROM news_fetch_cache WHERE source_name=?",
            (source_name,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    try:
        last = datetime.fromisoformat(row[0])
        elapsed = (datetime.now() - last).total_seconds() / 60
        return elapsed < ttl_min
    except Exception:
        return False


def _update_fetch_cache(source_name: str):
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO news_fetch_cache (source_name, last_fetched) VALUES (?, ?)",
            (source_name, datetime.now().isoformat())
        )
        conn.commit()
    finally:
        conn.close()


# ── RSS 抓取 ──────────────────────────────────────────────────────────────────

def _fetch_rss(url: str, max_items: int) -> list[str]:
    """抓取 RSS，返回标题列表。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "QuantBot/1.0 (news aggregator)"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        titles = []
        # 支持 RSS 2.0 和 Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        # RSS 2.0
        for item in root.findall(".//item")[:max_items]:
            t = item.findtext("title", "").strip()
            desc = item.findtext("description", "").strip()
            if t:
                # 拼接摘要（不超过200字符），方便情绪分析
                combined = t + (f" | {desc[:150]}" if desc else "")
                titles.append(combined[:300])
        # Atom
        if not titles:
            for entry in root.findall(".//atom:entry", ns)[:max_items]:
                t_el = entry.find("atom:title", ns)
                t = (t_el.text or "").strip() if t_el is not None else ""
                s_el = entry.find("atom:summary", ns)
                desc = (s_el.text or "").strip() if s_el is not None else ""
                if t:
                    titles.append((t + (f" | {desc[:150]}" if desc else ""))[:300])
        return titles
    except Exception as e:
        logger.warning(f"RSS抓取失败 {url}: {e}")
        return []


# ── JSON API 抓取 ─────────────────────────────────────────────────────────────

def _fetch_json_api(url: str, json_path: str, title_field: str,
                    summary_field: str, max_items: int) -> list[str]:
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "QuantBot/1.0"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # 按 json_path 取列表
        items = data
        for key in json_path.split("."):
            if key and isinstance(items, dict):
                items = items.get(key, [])
        titles = []
        for item in (items or [])[:max_items]:
            t = str(item.get(title_field, "")).strip()
            s = str(item.get(summary_field, "")).strip() if summary_field else ""
            if t:
                titles.append((t + (f" | {s[:150]}" if s else ""))[:300])
        return titles
    except Exception as e:
        logger.warning(f"JSON API抓取失败 {url}: {e}")
        return []


# ── 关键词情绪评分 ────────────────────────────────────────────────────────────

def _keyword_score(text: str, cfg: dict) -> float:
    """
    对单条新闻标题做关键词匹配，返回情绪分数（未归一化）。
    正数看涨，负数看跌。
    """
    score = 0.0
    text_lower = text.lower()
    for kw, weight in cfg.get("bullish_keywords", []):
        if kw.lower() in text_lower:
            score += weight
    for kw, weight in cfg.get("bearish_keywords", []):
        if kw.lower() in text_lower:
            score += weight   # bearish_keywords 的 weight 已经是负数
    return score


# ── AI 情绪分析（可选，hybrid模式超阈值时调用）────────────────────────────────

def _ai_sentiment_analysis(headlines: list[str], sentiment_cfg: dict = None) -> float:
    """
    调用 AI 对一批新闻标题进行情绪分析，返回 [-1.0, 1.0] 的综合情绪分数。
    失败时降级到关键词评分方法。

    此函数设计为可被 OpenClaw 的 AI 能力调用，
    也可以接入任何 OpenAI 兼容接口。
    """
    try:
        # 尝试导入项目内可能配置的 AI 客户端
        try:
            from utils.ai_client import analyze_sentiment
            return analyze_sentiment(headlines)
        except ImportError:
            pass

        # 降级：使用关键词分析（将所有标题聚合评分）
        if sentiment_cfg and headlines:
            total_score = 0.0
            for h in headlines:
                total_score += _keyword_score(h, sentiment_cfg)
            return _normalize(total_score / len(headlines))

        logger.info("AI客户端未配置且无关键词配置，返回中性情绪")
        return 0.0

    except Exception as e:
        logger.warning(f"AI情绪分析失败: {e}")
        return 0.0


# ── 情绪分数归一化 ────────────────────────────────────────────────────────────

def _normalize(score: float, max_abs: float = 5.0) -> float:
    """将原始分数压缩到 [-1.0, 1.0]。"""
    return max(-1.0, min(1.0, score / max_abs))


# ── 主函数：抓取所有源并写入DB ──────────────────────────────────────────────────

def analyze_news(force: bool = False) -> dict:
    """抓取并分析新闻，返回结果；逐条新闻仍写 news_sentiment，但不写 news_summary。"""
    _ensure_table()
    cfg = _load_config()
    sentiment_cfg = cfg.get("sentiment_config", {})
    mode          = sentiment_cfg.get("mode", "hybrid")
    ai_threshold  = sentiment_cfg.get("ai_trigger_threshold", 1.5)
    ai_max_items  = sentiment_cfg.get("ai_max_items", 8)
    bull_thresh   = sentiment_cfg.get("bullish_threshold", 0.3)
    bear_thresh   = sentiment_cfg.get("bearish_threshold", -0.3)

    all_sources = (
        cfg.get("crypto_sources", []) +
        cfg.get("macro_sources", [])
    )

    crypto_scores: list[tuple[float, float]] = []
    macro_scores:  list[tuple[float, float]] = []
    article_count = 0
    sampled_headlines: list[dict] = []

    for source in all_sources:
        if not source.get("enabled", False):
            continue
        name     = source["name"]
        ttl      = source.get("cache_ttl_min", 30)
        weight   = source.get("weight", 0.5)
        category = source.get("category", "crypto")

        if not force and _is_cache_fresh(name, ttl):
            logger.debug(f"跳过（缓存有效）: {name}")
            continue

        if source["type"] == "rss":
            headlines = _fetch_rss(source["url"], source.get("max_items", 10))
        elif source["type"] == "json_api":
            headlines = _fetch_json_api(
                source["url"],
                source.get("json_path", ""),
                source.get("title_field", "title"),
                source.get("summary_field", ""),
                source.get("max_items", 10)
            )
        else:
            continue

        if not headlines:
            continue

        _update_fetch_cache(name)
        article_count += len(headlines)

        source_scores = []
        ai_candidates = []
        for h in headlines:
            kw_score = _keyword_score(h, sentiment_cfg)
            if mode == "keyword":
                source_scores.append(kw_score)
            elif mode == "ai":
                ai_candidates.append(h)
            else:
                if abs(kw_score) >= ai_threshold:
                    ai_candidates.append(h)
                    source_scores.append(kw_score)
                else:
                    source_scores.append(kw_score)

            if len(sampled_headlines) < 12:
                sampled_headlines.append({"title": h[:200], "source": name})

        if ai_candidates and mode in ("ai", "hybrid"):
            ai_raw = _ai_sentiment_analysis(ai_candidates, sentiment_cfg)
            source_scores.append(ai_raw * 3.0)

        if source_scores:
            avg_raw = sum(source_scores) / len(source_scores)
            norm_score = _normalize(avg_raw)
            if category == "crypto":
                crypto_scores.append((norm_score, weight))
            else:
                macro_scores.append((norm_score, weight))

    def weighted_avg(scores_weights):
        if not scores_weights:
            return 0.0
        total_w = sum(w for _, w in scores_weights)
        if total_w == 0:
            return 0.0
        return sum(s * w for s, w in scores_weights) / total_w

    crypto_score = weighted_avg(crypto_scores)
    macro_score  = weighted_avg(macro_scores)

    if crypto_scores and macro_scores:
        combined = crypto_score * 0.6 + macro_score * 0.4
    elif crypto_scores:
        combined = crypto_score
    elif macro_scores:
        combined = macro_score
    else:
        combined = 0.0

    if combined >= bull_thresh:
        regime_hint = "bull"
    elif combined <= bear_thresh:
        regime_hint = "bear"
    else:
        regime_hint = "ranging"

    if not crypto_scores and not macro_scores:
        regime_hint = "unknown"

    summary_text = (
        f"新闻情绪: crypto={crypto_score:+.2f} macro={macro_score:+.2f} 综合={combined:+.2f} → 倾向={regime_hint} (共{article_count}条)"
    )
    logger.info(summary_text)

    return {
        "crypto_score": round(crypto_score, 3),
        "macro_score": round(macro_score, 3),
        "combined_score": round(combined, 3),
        "regime_hint": regime_hint,
        "summary_text": summary_text,
        "article_count": article_count,
        "headlines": sampled_headlines,
    }


def fetch_and_analyze(force: bool = False) -> dict:
    """兼容旧接口：先分析，再写入 news_summary。"""
    result = analyze_news(force=force)
    conn = _get_news_conn()
    try:
        conn.execute(
            "INSERT INTO news_summary (created_at, crypto_score, macro_score, combined_score, regime_hint, summary_text) VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(), result['crypto_score'], result['macro_score'], result['combined_score'], result['regime_hint'], result['summary_text'])
        )
        conn.execute(
            "DELETE FROM news_summary WHERE id NOT IN (SELECT id FROM news_summary ORDER BY id DESC LIMIT 200)"
        )
        conn.commit()
    finally:
        conn.close()
    return result


def get_latest_sentiment() -> Optional[dict]:
    """
    读取数据库中最新的情绪汇总记录。
    返回 None 表示没有任何记录（尚未抓取）。
    """
    _ensure_table()
    conn = sqlite3.connect(_DB_PATH)
    try:
        row = conn.execute(
            "SELECT created_at, crypto_score, macro_score, combined_score, "
            "regime_hint, summary_text FROM news_summary ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "created_at":     row[0],
        "crypto_score":   row[1],
        "macro_score":    row[2],
        "combined_score": row[3],
        "regime_hint":    row[4],
        "summary_text":   row[5],
    }


def get_sentiment_age_minutes() -> float:
    """返回最新情绪记录距现在有多少分钟，无记录返回 9999。"""
    s = get_latest_sentiment()
    if not s or not s.get("created_at"):
        return 9999.0
    try:
        last = datetime.fromisoformat(s["created_at"])
        return (datetime.now() - last).total_seconds() / 60
    except Exception:
        return 9999.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = fetch_and_analyze(force=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
