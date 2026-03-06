import pandas as pd
import sys
import os
import time
from datetime import datetime, timedelta
import glob # 👈 新增引入 glob，用于文件匹配查找

# 这三行是为了让程序能跨文件夹找到我们刚才写的 core 模块
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from core.okx_client import get_exchange

def fetch_kline_data(symbol='BTC/USDT', timeframe='15m', limit=100):
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