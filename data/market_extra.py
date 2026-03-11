"""
data/market_extra.py - 资金费率 & 持仓量(OI) 数据获取模块 (V2.0)

功能：
  1. 从 OKX 公开 API 获取当前/历史资金费率 (Funding Rate)
  2. 从 OKX 公开 API 获取持仓量 (Open Interest)
  3. 内存缓存 + SQLite 持久化，避免频繁请求
  4. 提供归一化的市场情绪指标供 selector.py 使用

设计原则：
  - 使用 OKX 公开端点，无需 API Key
  - 失败静默降级，不影响 Bot 主循环
  - 缓存 TTL 可配置（资金费率 5min，OI 2min）

OKX API 参考：
  - 资金费率: GET /api/v5/public/funding-rate
  - 历史资金费率: GET /api/v5/public/funding-rate-history
  - 持仓量: GET /api/v5/public/open-interest
  - 持仓量历史: GET /api/v5/rubik/stat/contracts/open-interest-history
"""

import time
import json
import logging
import sqlite3
import os
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

logger = logging.getLogger("market_extra")

_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_DIR)
_DB_PATH = os.path.join(_PROJECT_ROOT, "trading_data.db")

# OKX 公开 API base
_OKX_BASE = "https://www.okx.com"


# ── 内存缓存 ─────────────────────────────────────────────────────────────────

_cache: dict = {}  # key -> {"data": ..., "ts": float}


def _get_cached(key: str, ttl_sec: float) -> Optional[dict]:
    """从内存缓存获取数据，超时返回 None。"""
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_sec:
        return entry["data"]
    return None


def _set_cached(key: str, data: dict):
    """写入内存缓存。"""
    _cache[key] = {"data": data, "ts": time.time()}


# ── DB 持久化 ─────────────────────────────────────────────────────────────────

def _get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_tables():
    """创建市场数据扩展表（首次调用时执行）。"""
    conn = _get_db_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_funding_rate (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                inst_id     TEXT NOT NULL,
                funding_rate REAL NOT NULL,
                next_funding_rate REAL,
                funding_time TEXT,
                fetched_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_open_interest (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                inst_id     TEXT NOT NULL,
                oi_value    REAL NOT NULL,
                oi_ccy      TEXT,
                fetched_at  TEXT NOT NULL
            )
        """)
        # 只保留最近 500 条记录的清理索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_funding_fetched
            ON market_funding_rate(fetched_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_oi_fetched
            ON market_open_interest(fetched_at)
        """)
        conn.commit()
    finally:
        conn.close()


_tables_ensured = False


def _auto_ensure_tables():
    global _tables_ensured
    if not _tables_ensured:
        _ensure_tables()
        _tables_ensured = True


# ── HTTP 请求辅助 ─────────────────────────────────────────────────────────────

def _okx_get(path: str, params: dict = None) -> Optional[dict]:
    """
    发送 GET 请求到 OKX 公开 API。
    返回 JSON 响应（成功）或 None（失败）。
    """
    url = f"{_OKX_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if qs:
            url += f"?{qs}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "QuantBot/2.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("code") == "0":
            return data
        else:
            logger.warning(f"OKX API 返回错误: {data.get('code')} - {data.get('msg')}")
            return None
    except urllib.error.HTTPError as e:
        logger.warning(f"OKX HTTP 错误 {e.code}: {path}")
        return None
    except Exception as e:
        logger.warning(f"OKX 请求失败: {path} - {e}")
        return None


# ── 资金费率 ──────────────────────────────────────────────────────────────────

def fetch_funding_rate(inst_id: str = "BTC-USDT-SWAP",
                       cache_ttl: float = 300) -> Optional[dict]:
    """
    获取当前资金费率。

    :param inst_id: OKX 合约ID，如 "BTC-USDT-SWAP"
    :param cache_ttl: 缓存有效期（秒），默认 5 分钟
    :return: {
        "funding_rate":      float,  # 当前资金费率（如 0.0001 = 0.01%）
        "next_funding_rate": float,  # 预测下期资金费率
        "funding_time":      str,    # 下次结算时间（ms 时间戳）
        "signal":            str,    # "bullish" / "bearish" / "neutral"
        "strength":          float,  # 信号强度 [0, 1]
    }
    """
    cache_key = f"funding:{inst_id}"
    cached = _get_cached(cache_key, cache_ttl)
    if cached:
        return cached

    resp = _okx_get("/api/v5/public/funding-rate", {"instId": inst_id})
    if not resp or not resp.get("data"):
        return None

    d = resp["data"][0]
    rate = float(d.get("fundingRate", 0))
    next_rate = float(d.get("nextFundingRate", 0) or 0)
    funding_time = d.get("fundingTime", "")

    # 解读资金费率信号
    # 正费率 = 多头付费给空头 = 市场做多情绪过热 = 潜在做空信号
    # 负费率 = 空头付费给多头 = 市场做空情绪过热 = 潜在做多信号
    # 极端费率（>0.05% 或 <-0.05%）= 强信号
    signal, strength = _interpret_funding_rate(rate)

    result = {
        "funding_rate": rate,
        "next_funding_rate": next_rate,
        "funding_time": funding_time,
        "signal": signal,
        "strength": strength,
        "fetched_at": datetime.now().isoformat(),
    }

    _set_cached(cache_key, result)

    # 写入 DB 持久化
    try:
        _auto_ensure_tables()
        conn = _get_db_conn()
        try:
            conn.execute(
                "INSERT INTO market_funding_rate "
                "(inst_id, funding_rate, next_funding_rate, funding_time, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (inst_id, rate, next_rate, funding_time, result["fetched_at"])
            )
            # 只保留最近 500 条
            conn.execute(
                "DELETE FROM market_funding_rate WHERE id NOT IN "
                "(SELECT id FROM market_funding_rate ORDER BY id DESC LIMIT 500)"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"资金费率写入DB失败: {e}")

    return result


def _interpret_funding_rate(rate: float) -> tuple[str, float]:
    """
    解读资金费率，返回 (signal, strength)。
    
    正费率高 → 做多拥挤 → 看跌 (bearish)
    负费率高 → 做空拥挤 → 看涨 (bullish)
    接近0    → 中性 (neutral)
    """
    abs_rate = abs(rate)

    # 阈值定义（基于 BTC 永续合约历史统计）
    if abs_rate < 0.0001:    # < 0.01%
        return "neutral", 0.0
    elif abs_rate < 0.0005:  # 0.01% ~ 0.05%
        strength = abs_rate / 0.0005
        signal = "bearish" if rate > 0 else "bullish"
        return signal, strength * 0.5
    elif abs_rate < 0.001:   # 0.05% ~ 0.1%
        strength = 0.5 + (abs_rate - 0.0005) / 0.0005 * 0.3
        signal = "bearish" if rate > 0 else "bullish"
        return signal, strength
    else:                    # > 0.1% 极端费率
        strength = min(1.0, 0.8 + (abs_rate - 0.001) / 0.002 * 0.2)
        signal = "bearish" if rate > 0 else "bullish"
        return signal, strength


def fetch_funding_rate_history(inst_id: str = "BTC-USDT-SWAP",
                               limit: int = 48) -> list[dict]:
    """
    获取历史资金费率（最近 N 期）。
    
    :param limit: 返回条数，最大 100
    :return: [{"rate": float, "time": str}, ...]
    """
    cache_key = f"funding_hist:{inst_id}:{limit}"
    cached = _get_cached(cache_key, 600)  # 10分钟缓存
    if cached:
        return cached

    resp = _okx_get("/api/v5/public/funding-rate-history", {
        "instId": inst_id,
        "limit": str(min(limit, 100)),
    })
    if not resp or not resp.get("data"):
        return []

    result = []
    for d in resp["data"]:
        result.append({
            "rate": float(d.get("fundingRate", 0)),
            "time": d.get("fundingTime", ""),
            "realized_rate": float(d.get("realizedRate", 0) or 0),
        })

    _set_cached(cache_key, result)
    return result


# ── 持仓量 (Open Interest) ────────────────────────────────────────────────────

def fetch_open_interest(inst_id: str = "BTC-USDT-SWAP",
                        cache_ttl: float = 120) -> Optional[dict]:
    """
    获取当前持仓量 (OI)。

    :param inst_id: OKX 合约ID
    :param cache_ttl: 缓存有效期（秒），默认 2 分钟
    :return: {
        "oi":          float,  # 持仓量（张数）
        "oi_ccy":      float,  # 持仓量（币本位）
        "signal":      str,    # "rising" / "falling" / "stable"
        "change_pct":  float,  # 相比上次的变化百分比
    }
    """
    cache_key = f"oi:{inst_id}"
    cached = _get_cached(cache_key, cache_ttl)
    if cached:
        return cached

    resp = _okx_get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst_id})
    if not resp or not resp.get("data"):
        return None

    d = resp["data"][0]
    oi = float(d.get("oi", 0))
    oi_ccy = float(d.get("oiCcy", 0) or 0)

    # 与上次 OI 对比计算变化
    prev_oi = _get_prev_oi(inst_id)
    if prev_oi and prev_oi > 0:
        change_pct = (oi - prev_oi) / prev_oi
    else:
        change_pct = 0.0

    # OI 变化信号
    if change_pct > 0.05:      # OI 增加 >5%
        signal = "rising"
    elif change_pct < -0.05:   # OI 减少 >5%
        signal = "falling"
    else:
        signal = "stable"

    result = {
        "oi": oi,
        "oi_ccy": oi_ccy,
        "signal": signal,
        "change_pct": round(change_pct, 4),
        "fetched_at": datetime.now().isoformat(),
    }

    _set_cached(cache_key, result)

    # 写入 DB 持久化
    try:
        _auto_ensure_tables()
        conn = _get_db_conn()
        try:
            conn.execute(
                "INSERT INTO market_open_interest "
                "(inst_id, oi_value, oi_ccy, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (inst_id, oi, oi_ccy, result["fetched_at"])
            )
            conn.execute(
                "DELETE FROM market_open_interest WHERE id NOT IN "
                "(SELECT id FROM market_open_interest ORDER BY id DESC LIMIT 500)"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"OI写入DB失败: {e}")

    return result


def _get_prev_oi(inst_id: str) -> Optional[float]:
    """从 DB 获取上一次记录的 OI 值，用于计算变化率。"""
    try:
        _auto_ensure_tables()
        conn = _get_db_conn()
        try:
            row = conn.execute(
                "SELECT oi_value FROM market_open_interest "
                "WHERE inst_id=? ORDER BY id DESC LIMIT 1",
                (inst_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


# ── 综合市场情绪指标 ──────────────────────────────────────────────────────────

def get_market_extra_signals(symbol: str = "BTC/USDT:USDT") -> dict:
    """
    获取综合的市场额外信号（资金费率 + OI），供 selector.py 使用。

    :param symbol: ccxt 格式的交易对
    :return: {
        "funding": {...} or None,
        "oi": {...} or None,
        "composite_signal":  str,    # "bullish" / "bearish" / "neutral"
        "composite_score":   float,  # [-1, 1]，正=看涨，负=看跌
        "available":         bool,   # 是否有有效数据
    }
    """
    # 将 ccxt 格式转为 OKX inst_id
    inst_id = _symbol_to_inst_id(symbol)

    funding = fetch_funding_rate(inst_id)
    oi = fetch_open_interest(inst_id)

    score = 0.0
    weight_sum = 0.0

    # 资金费率信号（权重 0.6）
    if funding:
        fr_score = funding["strength"]
        if funding["signal"] == "bearish":
            fr_score = -fr_score
        elif funding["signal"] == "neutral":
            fr_score = 0.0
        score += fr_score * 0.6
        weight_sum += 0.6

    # OI 信号（权重 0.4）
    # OI 上升 + 价格上升 = 强趋势确认（但我们这里只看 OI 本身变化作为辅助）
    # OI 大幅下降 = 去杠杆/平仓潮 = 趋势可能反转
    if oi:
        oi_change = oi["change_pct"]
        if oi["signal"] == "rising":
            # OI 上升 = 新资金进入，趋势可能延续（但方向需结合价格）
            # 这里给一个轻微的中性偏向，让技术面决定方向
            oi_score = min(oi_change * 2, 0.3)
        elif oi["signal"] == "falling":
            # OI 下降 = 平仓/去杠杆，可能是趋势反转或盘整前兆
            oi_score = max(oi_change * 2, -0.3)
        else:
            oi_score = 0.0
        score += oi_score * 0.4
        weight_sum += 0.4

    if weight_sum > 0:
        composite = score / weight_sum
    else:
        composite = 0.0

    # 最终信号
    if composite > 0.15:
        signal = "bullish"
    elif composite < -0.15:
        signal = "bearish"
    else:
        signal = "neutral"

    return {
        "funding": funding,
        "oi": oi,
        "composite_signal": signal,
        "composite_score": round(composite, 4),
        "available": funding is not None or oi is not None,
    }


def _symbol_to_inst_id(symbol: str) -> str:
    """
    将 ccxt 格式交易对转为 OKX instId。
    "BTC/USDT:USDT" → "BTC-USDT-SWAP"
    "ETH/USDT:USDT" → "ETH-USDT-SWAP"
    """
    # 去掉 :USDT 后缀
    base_pair = symbol.split(":")[0]
    # BTC/USDT → BTC-USDT
    inst = base_pair.replace("/", "-")
    return f"{inst}-SWAP"


# ── 供外部读取最新数据的便捷接口 ──────────────────────────────────────────────

def get_latest_funding_rate(inst_id: str = "BTC-USDT-SWAP") -> Optional[float]:
    """返回最新的资金费率数值，None 表示无数据。"""
    data = fetch_funding_rate(inst_id)
    return data["funding_rate"] if data else None


def get_latest_oi(inst_id: str = "BTC-USDT-SWAP") -> Optional[float]:
    """返回最新的持仓量（张数），None 表示无数据。"""
    data = fetch_open_interest(inst_id)
    return data["oi"] if data else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== 资金费率 ===")
    fr = fetch_funding_rate()
    print(json.dumps(fr, indent=2, ensure_ascii=False) if fr else "无数据")

    print("\n=== 历史资金费率 (最近5期) ===")
    hist = fetch_funding_rate_history(limit=5)
    for h in hist:
        print(f"  {h['time']}: {h['rate']:.6f}")

    print("\n=== 持仓量 ===")
    oi = fetch_open_interest()
    print(json.dumps(oi, indent=2, ensure_ascii=False) if oi else "无数据")

    print("\n=== 综合信号 ===")
    signals = get_market_extra_signals()
    print(json.dumps(signals, indent=2, ensure_ascii=False))
