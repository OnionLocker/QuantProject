import pandas as pd
import sys
import os
import time
from datetime import datetime, timedelta
import glob

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from core.okx_client import get_exchange

# ── 公开行情客户端（不需要 API Key，用于回测下载）──────────────────────────────

def _get_public_exchange():
    """历史行情数据是公开的，不需要签名，无需读取 .env。"""
    import ccxt
    return ccxt.okx({"enableRateLimit": True})


# ── 带日期范围的智能缓存下载 ────────────────────────────────────────────────────

CACHE_DIR = os.path.join(project_root, "data", "cache")
CACHE_TTL_HOURS = 12   # 缓存有效期：12 小时


def _cache_path(symbol: str, timeframe: str) -> str:
    safe = symbol.replace("/", "-").replace(":", "-")
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{safe}_{timeframe}.csv")


def _cache_is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < CACHE_TTL_HOURS * 3600


def _download_range(symbol: str, timeframe: str,
                    since_ms: int, until_ms: int) -> pd.DataFrame:
    """从 OKX 下载指定时间段的 K 线，无需 API Key。"""
    ex = _get_public_exchange()
    all_ohlcv = []
    cursor = since_ms

    while cursor < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=300)
            if not batch:
                break
            all_ohlcv.extend(batch)
            last_ts = batch[-1][0]
            if last_ts >= until_ms or len(batch) < 300:
                break
            cursor = last_ts + 1
            time.sleep(0.12)
        except Exception as e:
            print(f"[market_data] 下载中断: {e}，2s 后重试...")
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


def fetch_history_range(symbol: str, timeframe: str,
                        start_date: str, end_date: str) -> pd.DataFrame:
    """
    获取指定交易对、周期、日期范围的历史 K 线。
    智能缓存策略：
      - 首次或缓存超过 12 小时 → 重新下载从 start_date 到 end_date 的全量数据并写入缓存
      - 缓存命中且覆盖所需范围 → 直接读缓存、按日期过滤返回
    :param symbol:     交易对，如 "BTC/USDT"
    :param timeframe:  周期，如 "1h" "4h" "1d"
    :param start_date: 开始日期字符串，如 "2023-01-01"
    :param end_date:   结束日期字符串，如 "2024-01-01"
    :return: 按 timestamp 索引的 OHLCV DataFrame
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt   = datetime.strptime(end_date,   "%Y-%m-%d") + timedelta(days=1)  # 含当天
    since_ms = int(start_dt.timestamp() * 1000)
    until_ms = int(end_dt.timestamp()   * 1000)

    cache_file = _cache_path(symbol, timeframe)

    # 检查缓存是否命中且覆盖所需范围
    if _cache_is_fresh(cache_file):
        try:
            cached = pd.read_csv(cache_file, index_col="timestamp", parse_dates=True)
            if len(cached) > 0:
                cache_start = cached.index.min()
                cache_end   = cached.index.max()
                if cache_start <= start_dt and cache_end >= end_dt - timedelta(days=2):
                    # 缓存完整覆盖请求范围
                    mask = (cached.index >= start_dt) & (cached.index < end_dt)
                    print(f"[market_data] 命中缓存 {os.path.basename(cache_file)}")
                    return cached[mask]
        except Exception:
            pass

    # 缓存不命中或范围不足 → 重新下载
    print(f"[market_data] 下载 {symbol} {timeframe} [{start_date} → {end_date}]...")
    df = _download_range(symbol, timeframe, since_ms, until_ms)
    if df.empty:
        return df

    # 写入缓存（如已有缓存则合并，扩充覆盖范围）
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
    """
    去交易所拿历史 K 线，并整理成整齐的表格
    symbol: 交易对，比如 'BTC/USDT'
    timeframe: K线周期，比如 '1m', '15m', '1h', '1d'
    limit: 拿多少根 K 线，默认 100 根
    """
    exchange = get_exchange()
    print(f"正在向 OKX 获取 {limit} 根 {symbol} 的 {timeframe} K线数据...")
    
    try:
        # 1. 拿原始数据 (格式：时间戳, 开盘价, 最高价, 最低价, 收盘价, 成交量)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # 2. 用 pandas 把数据装进表格里
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # 3. 把那一长串数字时间戳，变成我们人类能看懂的正常时间
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # 4. 把时间这一列作为表格的“目录”（索引），方便以后按时间查找
        df.set_index('timestamp', inplace=True)
        
        return df
        
    except Exception as e:
        print(f"获取数据失败，报错啦: {e}")
        return None

def download_history_to_csv(symbol='BTC/USDT', timeframe='1h', years=3, csv_path='history.csv'):
    """
    历史数据批量下载器 (突破交易所单次限制)
    每次运行前会删除旧文件，从指定年份前一直下载到此刻。
    """
    # 1. 删除旧的遗留数据
    if os.path.exists(csv_path):
        os.remove(csv_path)
        print(f"🗑️ 已清理旧的历史数据文件: {csv_path}")

    exchange = get_exchange()
    print(f"⏳ 准备向 OKX 发起循环请求，下载过去 {years} 年的 {symbol} ({timeframe}) K线...")

    # 2. 计算 3 年前的时间戳起点 (毫秒级)
    now = datetime.now()
    start_time = now - timedelta(days=years*365)
    since = int(start_time.timestamp() * 1000)

    all_ohlcv = []
    
    # 3. 开启循环“翻页”下载
    while True:
        try:
            # 每次最多拿 100 根
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=100)
            
            if not ohlcv or len(ohlcv) == 0:
                break
            
            all_ohlcv.extend(ohlcv)
            
            # 把拿到的最后一根 K 线时间戳 + 1毫秒，作为下一次请求的起点
            last_timestamp = ohlcv[-1][0]
            since = last_timestamp + 1
            
            # 在同一行动态刷新打印进度
            print(f"🔽 正在疯狂下载中... 已获取 {len(all_ohlcv)} 根 K 线", end='\r')
            
            # 如果某次拿到的数据少于 100，说明已经“追”到当前最新时间了
            if len(ohlcv) < 100:
                break
                
            # ⚠️ 极其重要：每次请求后休息 0.1 秒，防止被交易所当作恶意攻击封 IP
            time.sleep(0.1) 
            
        except Exception as e:
            print(f"\n❌ 下载中断，报错: {e}。休息 2 秒后自动重试...")
            time.sleep(2)

    print(f"\n✅ 数据扒取完毕！总共获取到 {len(all_ohlcv)} 根 K 线。")
    
    # 4. 把数据装进 Pandas 表格，处理格式并保存
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    # 去重（防抖）并把时间设为索引
    df.drop_duplicates(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)
    
    df.to_csv(csv_path)
    print(f"💾 数据已成功打包保存至: {csv_path}")
    
    return df

def download_history_to_csv(symbol='BTC/USDT', timeframe='1h', years=3, data_dir=None):
    """
    智能月度缓存版：历史数据批量下载器
    规则：当月只需下载一次，生成类似 data_202602.csv。下次运行直接读取缓存。
          如果跨月了，自动删除旧月份文件，重新拉取最新 3 年数据。
    """
    if data_dir is None:
        data_dir = os.path.join(project_root, 'data')
        
    # 确保 data 文件夹存在
    os.makedirs(data_dir, exist_ok=True)
    
    # 1. 确定当月的缓存文件名 (例如: data_202602.csv)
    current_ym = datetime.now().strftime('%Y%m')
    target_filename = f"data_{current_ym}.csv"
    target_path = os.path.join(data_dir, target_filename)
    
    # 2. 检查缓存是否命中
    if os.path.exists(target_path):
        print(f"📦 命中本月最新缓存文件 [{target_filename}]，直接读取，跳过 API 下载！")
        df = pd.read_csv(target_path, index_col='timestamp', parse_dates=True)
        return df
        
    # 3. 缓存未命中（说明是第一次运行，或者跨月了）
    # -> 先大扫除，清理掉 data 目录下所有的 data_XXXXXX.csv 历史遗留文件
    old_files = glob.glob(os.path.join(data_dir, "data_*.csv"))
    for f in old_files:
        try:
            os.remove(f)
            print(f"🗑️ 发现跨月或过期数据，已自动清理: {os.path.basename(f)}")
        except Exception as e:
            print(f"清理文件失败: {e}")

    # 4. 开始连环请求下载最新数据
    exchange = get_exchange()
    print(f"⏳ 缓存未命中，准备向 OKX 发起请求，疯狂拉取过去 {years} 年的 {symbol} ({timeframe}) K线...")

    now = datetime.now()
    start_time = now - timedelta(days=years*365)
    since = int(start_time.timestamp() * 1000)

    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=100)
            if not ohlcv or len(ohlcv) == 0:
                break
            
            all_ohlcv.extend(ohlcv)
            last_timestamp = ohlcv[-1][0]
            since = last_timestamp + 1
            
            print(f"🔽 正在下载中... 已获取 {len(all_ohlcv)} 根 K 线", end='\r')
            
            if len(ohlcv) < 100:
                break
                
            time.sleep(0.1) # 严格控制请求频率，防封 IP
            
        except Exception as e:
            print(f"\n❌ 下载中断，报错: {e}。休息 2 秒后自动重试...")
            time.sleep(2)

    print(f"\n✅ 数据扒取完毕！总共获取到 {len(all_ohlcv)} 根 K 线。")
    
    # 5. 数据装盘并保存为当月的专属 CSV
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.drop_duplicates(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)
    
    df.to_csv(target_path)
    print(f"💾 数据已成功打包保存至当月缓存: {target_path}")
    
    return df
    
# --- 下面是测试部分 ---
if __name__ == "__main__":
    # 我们试着拿一下比特币最近 5 根 15 分钟级别的 K 线
    df = fetch_kline_data('BTC/USDT', '15m', 5)
    
    if df is not None:
        print("\n✅ 数据获取成功！长这个样子：")
        print(df)