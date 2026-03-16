from datetime import datetime
from typing import Any, List, Optional
import sqlite3
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from utils.config_loader import get_config

router = APIRouter(prefix="/api/news-sync", tags=["news-sync"])
logger = logging.getLogger("news_sync")


class NewsHeadline(BaseModel):
    title: str
    url: Optional[str] = None
    source: Optional[str] = None
    published_at: Optional[str] = None


class NewsSyncPayload(BaseModel):
    provider: str = Field(default="openclaw")
    symbol: str = Field(default="BTC/USDT:USDT")
    lookback_hours: int = Field(default=12, ge=1, le=168)
    interval_hours: int = Field(default=4, ge=1, le=168)
    regime_hint: str = Field(description="bull|bear|ranging|unknown")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    combined_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    crypto_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    macro_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    summary_text: str
    headlines: List[NewsHeadline] = Field(default_factory=list)
    fetched_at: Optional[str] = None


_DEF_DB = "/root/QuantProject/trading_data.db"


def _db_path() -> str:
    cfg = get_config() or {}
    return cfg.get("database", {}).get("path", _DEF_DB)


@router.get("/config")
def get_news_sync_config() -> dict[str, Any]:
    cfg = get_config() or {}
    selector = cfg.get("selector", {}) or {}
    news_sync = cfg.get("news_sync", {}) or {}
    return {
        "enabled_by_weight": (selector.get("news_weight", 0.0) or 0.0) > 0,
        "news_weight": selector.get("news_weight", 0.0),
        "news_sync": news_sync,
    }


@router.post("/ingest")
def ingest_news_sync(payload: NewsSyncPayload) -> dict[str, Any]:
    cfg = get_config() or {}
    news_sync = cfg.get("news_sync", {}) or {}
    if not news_sync.get("enable", False):
        raise HTTPException(status_code=403, detail="news_sync disabled")

    if payload.regime_hint not in {"bull", "bear", "ranging", "unknown"}:
        raise HTTPException(status_code=400, detail="invalid regime_hint")

    created_at = payload.fetched_at or datetime.now().isoformat()
    headline_lines = []
    for h in payload.headlines[:20]:
        source = f" [{h.source}]" if h.source else ""
        url = f" ({h.url})" if h.url else ""
        headline_lines.append(f"- {h.title}{source}{url}")

    summary = (
        f"[OpenClaw News Sync]\n"
        f"provider={payload.provider} | symbol={payload.symbol} | "
        f"lookback={payload.lookback_hours}h | interval={payload.interval_hours}h | "
        f"confidence={payload.confidence:.2f}\n"
        f"{payload.summary_text.strip()}"
    )
    if headline_lines:
        summary += "\n\nHeadlines:\n" + "\n".join(headline_lines)

    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                crypto_score REAL,
                macro_score REAL,
                combined_score REAL,
                regime_hint TEXT,
                summary_text TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO news_summary (created_at, crypto_score, macro_score, combined_score, regime_hint, summary_text) VALUES (?,?,?,?,?,?)",
            (
                created_at,
                payload.crypto_score,
                payload.macro_score,
                payload.combined_score,
                payload.regime_hint,
                summary,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "[OpenClaw News Sync] 写入成功 created_at=%s regime=%s score=%.3f headlines=%s",
        created_at,
        payload.regime_hint,
        payload.combined_score,
        len(payload.headlines),
    )

    return {
        "ok": True,
        "created_at": created_at,
        "regime_hint": payload.regime_hint,
        "combined_score": payload.combined_score,
        "headline_count": len(payload.headlines),
    }


@router.post("/run")
def run_news_sync_now() -> dict[str, Any]:
    cfg = get_config() or {}
    news_sync = cfg.get("news_sync", {}) or {}
    if not news_sync.get("enable", False):
        raise HTTPException(status_code=403, detail="news_sync disabled")

    try:
        import subprocess
        import os
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        py = os.path.join(project_root, 'venv', 'bin', 'python')
        runner = os.path.join(project_root, 'scripts', 'news_sync_runner.py')
        proc = subprocess.run(
            [py, runner],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or '')[:2000],
            "stderr": (proc.stderr or '')[:2000],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
def get_news_sync_status() -> dict[str, Any]:
    cfg = get_config() or {}
    selector = cfg.get("selector", {}) or {}
    news_sync = cfg.get("news_sync", {}) or {}

    conn = sqlite3.connect(_db_path())
    try:
        row = conn.execute(
            "SELECT created_at, crypto_score, macro_score, combined_score, regime_hint, summary_text FROM news_summary ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    latest = None
    age_minutes = None
    if row:
        latest = {
            "created_at": row[0],
            "crypto_score": row[1],
            "macro_score": row[2],
            "combined_score": row[3],
            "regime_hint": row[4],
            "summary_text": row[5],
        }
        try:
            age_minutes = round((datetime.now() - datetime.fromisoformat(row[0])).total_seconds() / 60, 1)
        except Exception:
            age_minutes = None

    return {
        "enabled_by_weight": (selector.get("news_weight", 0.0) or 0.0) > 0,
        "news_weight": selector.get("news_weight", 0.0),
        "news_sync": news_sync,
        "latest": latest,
        "age_minutes": age_minutes,
    }
