"""
core/okx_client.py - OKX 交易所工具函数

提供：
  - retry_on_network_error: 网络请求指数退避重试装饰器
  - fetch_position_state: 读取交易所持仓快照（用于启动同步）

注意：多用户版使用 api.routes.keys.get_user_exchange() 获取每用户独立实例，
     本文件不再维护全局 exchange 单例。
"""
import ccxt
import time
import functools
from utils.logger import bot_logger


# ── 指数退避重试装饰器 ──────────────────────────────────────────────────────
def retry_on_network_error(max_retries=3, base_delay=2.0):
    """
    网络请求自动重试装饰器。
    对 ccxt 的 NetworkError / RequestTimeout / ExchangeNotAvailable 做指数退避重试。
    其他异常（如 InsufficientFunds）直接抛出，不重试。
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                    if attempt == max_retries:
                        bot_logger.error(f"❌ [{func.__name__}] 网络错误，已重试 {max_retries} 次，放弃。错误: {e}")
                        raise
                    bot_logger.warning(f"⚠️ [{func.__name__}] 网络抖动 (第{attempt}次)，{delay:.1f}s 后重试... 错误: {e}")
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)  # 指数退避，最长等 60s
        return wrapper
    return decorator


@retry_on_network_error(max_retries=3, base_delay=2.0)
def fetch_position_state(ex, symbol: str):
    """
    读取交易所当前仓位快照（用于"启动同步防失忆"）

    :param ex: ccxt exchange 实例（由调用方传入，支持多用户）
    :param symbol: 交易对，如 "BTC/USDT:USDT"
    返回 dict:
      - status: 'empty' | 'ok' | 'both' | 'error'
      - side: 'long'/'short'/None
      - amount: 合约张数(contracts)
      - entry_price: 持仓均价
    """
    if not ex.has.get("fetchPositions", False):
        return {"status": "error", "error": "ccxt 当前版本不支持 fetch_positions"}

    positions = ex.fetch_positions([symbol])
    long_amt = 0.0
    short_amt = 0.0
    long_entry = 0.0
    short_entry = 0.0

    for p in positions or []:
        if p.get("symbol") != symbol:
            continue

        contracts = float(p.get("contracts") or 0.0)
        if contracts <= 0:
            continue

        side = p.get("side")
        info = p.get("info") or {}
        if side not in ("long", "short"):
            side = info.get("posSide")

        entry = float(p.get("entryPrice") or info.get("avgPx") or 0.0)

        if side == "long":
            long_amt += contracts
            long_entry = entry or long_entry
        elif side == "short":
            short_amt += contracts
            short_entry = entry or short_entry

    if long_amt > 0 and short_amt > 0:
        return {
            "status": "both",
            "long": {"amount": long_amt, "entry_price": long_entry},
            "short": {"amount": short_amt, "entry_price": short_entry},
        }

    if long_amt > 0:
        return {"status": "ok", "side": "long", "amount": long_amt, "entry_price": long_entry}

    if short_amt > 0:
        return {"status": "ok", "side": "short", "amount": short_amt, "entry_price": short_entry}

    return {"status": "empty", "side": None, "amount": 0.0, "entry_price": 0.0}
