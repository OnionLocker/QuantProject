"""
data/market_data.py - 历史 K 线数据获取与缓存

生产接口：
  fetch_history_range(symbol, timeframe, start_date, end_date) -> DataFrame

缓存策略：
  - 缓存目录：data/cache/
  - TTL：12 小时
  - 命中且覆盖所需范围 → 直接返回切片；否则重新下载并合并写回

注：历史行情数据是公开端点，无需 API Key。
"""
import os
import sys
import time
import pandas as pd
from datetime import datetime, timedelta

current_dir  = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

# ── 公开行情客户端（不需要 API Key）──────────────────────────────────────────

def _get_public_exchange():
    import ccxt
    return ccxt.okx({"enableRateLimit": True})


# ── 缓存辅助 ─────────────────────────────────────────────────────────────────

CACHE_DIR      = os.path.join(project_root, "data", "cache")
CACHE_TTL_HOURS = 12


def _cache_path(symbol: str, timeframe: str) -> str:
    safe = symbol.replace("/", "-").replace(":", "-")
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{safe}_{timeframe}.csv")


def _cache_is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < CACHE_TTL_HOURS * 3600


# ── 核心下载（带重试与超时）──────────────────────────────────────────────────

def _download_range(symbol: str, timeframe: str,
                    since_ms: int, until_ms: int) -> pd.DataFrame:
    """从 OKX 下载指定时间段的 K 线，无需 API Key。"""
    ex = _get_public_exchange()
    all_ohlcv        = []
    cursor           = since_ms
    consecutive_errors = 0
    MAX_ERRORS       = 5
    TIMEOUT_SEC      = 120
    deadline         = time.time() + TIMEOUT_SEC

    while cursor < until_ms:
        if time.time() > deadline:
            raise TimeoutError(
                f"数据下载超时（>{TIMEOUT_SEC}s），已获取 {len(all_ohlcv)} 根 K 线。"
                "请检查网络或稍后重试。"
            )
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=300)
            consecutive_errors = 0
            if not batch:
                break
            all_ohlcv.extend(batch)
            last_ts = batch[-1][0]
            if last_ts >= until_ms or len(batch) < 300:
                break
            cursor = last_ts + 1
            time.sleep(0.12)
        except Exception as e:
            consecutive_errors += 1
            print(f"[market_data] 下载出错({consecutive_errors}/{MAX_ERRORS}): {e}")
            if consecutive_errors >= MAX_ERRORS:
                raise RuntimeError(
                    f"连续 {MAX_ERRORS} 次请求失败，放弃下载 {symbol} 数据。"
                    f"最后错误: {e}"
                )
            time.sleep(2)

    if not all_ohlcv:
        return pd.DataFrame()

    df = pd.DataFrame(all_ohlcv,
                      columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    df.drop_duplicates(subset=["timestamp"], inplace=True)
    df.set_index("timestamp", inplace=True)
    return df


# ── 对外接口 ─────────────────────────────────────────────────────────────────

def fetch_history_range(symbol: str, timeframe: str,
                        start_date: str, end_date: str) -> pd.DataFrame:
    """
    获取指定交易对、周期、日期范围的历史 K 线。

    :param symbol:     交易对，如 "BTC/USDT"
    :param timeframe:  周期，如 "1h" "4h" "1d"
    :param start_date: 开始日期字符串，如 "2023-01-01"
    :param end_date:   结束日期字符串，如 "2024-01-01"
    :return:           按 timestamp 索引的 OHLCV DataFrame
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt   = datetime.strptime(end_date,   "%Y-%m-%d") + timedelta(days=1)
    since_ms = int(start_dt.timestamp() * 1000)
    until_ms = int(end_dt.timestamp()   * 1000)

    cache_file = _cache_path(symbol, timeframe)

    # 命中缓存且覆盖所需范围 → 直接切片返回
    if _cache_is_fresh(cache_file):
        try:
            cached = pd.read_csv(cache_file, index_col="timestamp", parse_dates=True)
            if len(cached) > 0:
                cache_start = cached.index.min()
                cache_end   = cached.index.max()
                if cache_start <= start_dt and cache_end >= end_dt - timedelta(days=2):
                    mask = (cached.index >= start_dt) & (cached.index < end_dt)
                    print(f"[market_data] 命中缓存 {os.path.basename(cache_file)}")
                    return cached[mask]
        except Exception:
            pass

    # 下载
    print(f"[market_data] 下载 {symbol} {timeframe} [{start_date} → {end_date}]...")
    df = _download_range(symbol, timeframe, since_ms, until_ms)
    if df.empty:
        return df

    # 合并写回缓存（扩充覆盖范围）
    if os.path.exists(cache_file):
        try:
            old = pd.read_csv(cache_file, index_col="timestamp", parse_dates=True)
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        except Exception:
            pass
    df.to_csv(cache_file)
    print(f"[market_data] 缓存已更新: {os.path.basename(cache_file)} ({len(df)} 根 K 线)")

    mask = (df.index >= start_dt) & (df.index < end_dt)
    return df[mask]
