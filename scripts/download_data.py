"""
scripts/download_data.py - 两用脚本

在本地开发机上：
    不能直连 OKX，此脚本只负责读取 data/cache/*.parquet 并打印摘要。
    真正的数据拉取在云服务器上完成，见 scripts/fetch_okx_cloud.py。

在云服务器上：
    python scripts/download_data.py --mode fetch --symbol BTC/USDT:USDT --timeframe 4h --start 2019-04-01
    拉取后生成 data/cache/<safe_symbol>_<tf>.parquet，用户手动下载回本地。

用法：
    python scripts/download_data.py                    # 默认：检查本地 parquet 存在性并打印摘要
    python scripts/download_data.py --mode fetch       # 云上拉取模式
    python scripts/download_data.py --mode inspect     # 本地查看
"""
import argparse
import os
import sys
from datetime import datetime

current_dir  = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

CACHE_DIR = os.path.join(project_root, "data", "cache")


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


def parquet_path(symbol: str, timeframe: str) -> str:
    return os.path.join(CACHE_DIR, f"{safe_symbol(symbol)}_{timeframe}.parquet")


def inspect(symbol: str, timeframe: str) -> int:
    """本地查看模式：检查 parquet 是否存在并打印摘要。"""
    import pandas as pd

    path = parquet_path(symbol, timeframe)
    if not os.path.exists(path):
        print(f"[inspect] 未找到 {path}", file=sys.stderr)
        print(f"[inspect] 本地环境无法直连 OKX，请在云服务器上执行：", file=sys.stderr)
        print(f"           python scripts/download_data.py --mode fetch "
              f"--symbol {symbol!r} --timeframe {timeframe}", file=sys.stderr)
        print(f"           然后将生成的 parquet 上传到 {path}", file=sys.stderr)
        return 1

    df = pd.read_parquet(path)
    size_kb = os.path.getsize(path) / 1024
    print(f"[inspect] 文件：{path} ({size_kb:.1f} KB)")
    print(f"[inspect] 行数：{len(df)}")
    print(f"[inspect] 范围：{df.index.min()} → {df.index.max()}")
    print(f"[inspect] 列  ：{list(df.columns)}")
    print("\n首 3 行：")
    print(df.head(3).to_string())
    print("\n末 3 行：")
    print(df.tail(3).to_string())
    return 0


def fetch(symbol: str, timeframe: str, start: str, end: str | None) -> int:
    """云上拉取模式：调用 ccxt 直连 OKX。"""
    try:
        import ccxt
        import pandas as pd
    except ImportError as e:
        print(f"[fetch] 缺少依赖：{e}", file=sys.stderr)
        print(f"[fetch] 请 pip install ccxt pandas pyarrow", file=sys.stderr)
        return 2

    import time
    from datetime import timezone

    end = end or datetime.utcnow().strftime("%Y-%m-%d")
    print(f"[fetch] {symbol} {timeframe} [{start} → {end}]")

    ex = ccxt.okx({"enableRateLimit": True})
    try:
        ex.load_markets()
    except Exception as e:
        print(f"[fetch] load_markets 失败（可能网络被封）：{e}", file=sys.stderr)
        return 3

    since_ms = int(datetime.strptime(start, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    until_ms = int((datetime.strptime(end, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() + 86400) * 1000)

    BATCH = 300
    rows = []
    cursor = since_ms
    errors = 0
    while cursor < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=BATCH)
            errors = 0
            if not batch:
                break
            rows.extend(batch)
            last = batch[-1][0]
            if len(batch) < BATCH or last >= until_ms:
                break
            cursor = last + 1
            time.sleep(0.15)
            if len(rows) % (BATCH * 10) == 0:
                ts = datetime.fromtimestamp(last / 1000, tz=timezone.utc)
                print(f"[fetch] ... {len(rows)} bars, last={ts}")
        except Exception as e:
            errors += 1
            print(f"[fetch err {errors}/5] {e}", file=sys.stderr)
            if errors >= 5:
                print(f"[fetch] 连续 5 次失败，放弃", file=sys.stderr)
                return 4
            time.sleep(3)

    if not rows:
        print(f"[fetch] 未获得任何数据", file=sys.stderr)
        return 5

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.drop_duplicates(subset=["timestamp"]).set_index("timestamp").sort_index()

    os.makedirs(CACHE_DIR, exist_ok=True)
    out = parquet_path(symbol, timeframe)
    df.to_parquet(out, compression="snappy")
    size_kb = os.path.getsize(out) / 1024
    print(f"\n[fetch] 完成：{len(df)} 根 K 线 → {out} ({size_kb:.1f} KB)")
    print(f"[fetch] 范围：{df.index.min()} → {df.index.max()}")
    print("\n首 3 行：")
    print(df.head(3).to_string())
    print("\n末 3 行：")
    print(df.tail(3).to_string())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="历史 K 线下载/查看（两用）")
    ap.add_argument("--mode", choices=["inspect", "fetch"], default="inspect",
                    help="inspect（本地查看，默认）或 fetch（云上拉取）")
    ap.add_argument("--symbol",    default="BTC/USDT:USDT")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--start",     default="2019-04-01",
                    help="起始日期，fetch 模式使用（OKX 永续约从 2019-04 开始）")
    ap.add_argument("--end",       default=None, help="结束日期，fetch 模式使用")
    args = ap.parse_args()

    if args.mode == "inspect":
        return inspect(args.symbol, args.timeframe)
    else:
        return fetch(args.symbol, args.timeframe, args.start, args.end)


if __name__ == "__main__":
    sys.exit(main())
