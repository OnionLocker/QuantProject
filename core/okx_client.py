import ccxt
import os
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

_exchange = None

def get_exchange():
    global _exchange
    if _exchange is not None:
        return _exchange

    # 优先尝试读取模拟盘配置
    sim_api_key = os.getenv("SIMULATE_OKX_API_KEY")
    sim_secret_key = os.getenv("SIMULATE_OKX_SECRET_KEY")
    sim_password = os.getenv("SIMULATE_OKX_PASSPHRASE")

    is_simulate = False

    # 逻辑判断：如果配置了模拟盘参数，就用模拟盘
    if sim_api_key and sim_secret_key and sim_password:
        api_key = sim_api_key
        secret_key = sim_secret_key
        password = sim_password
        is_simulate = True
        print("⚠️ 检测到 SIMULATE 配置，当前连接：【OKX 模拟盘】")
    else:
        # 否则读取实盘参数
        api_key = os.getenv("OKX_API_KEY")
        secret_key = os.getenv("OKX_SECRET_KEY")
        password = os.getenv("OKX_PASSPHRASE")
        print("🔥 未检测到 SIMULATE 配置，当前连接：【OKX 真实实盘】！")

    if not api_key or not secret_key or not password:
        raise ValueError("❌ 找不到有效的 API Key，请检查 .env 文件配置。")

    exchange = ccxt.okx({
        "apiKey": api_key,
        "secret": secret_key,
        "password": password,
        "enableRateLimit": True,
    })

    # 根据配置自动开启或关闭沙盒(模拟)模式
    exchange.set_sandbox_mode(is_simulate)

    _exchange = exchange
    return _exchange

def fetch_position_state(symbol: str):
    """
    读取交易所当前仓位快照（用于“启动同步防失忆”）
    返回 dict:
      - status: 'empty' | 'ok' | 'both' | 'error'
      - side: 'long'/'short'/None
      - amount: 合约张数(contracts)
      - entry_price: 持仓均价
    """
    ex = get_exchange()
    try:
        if not ex.has.get("fetchPositions", False):
            return {"status": "error", "error": "ccxt 当前版本不支持 fetch_positions"}

        positions = ex.fetch_positions([symbol])  # OKX swap 通常能用
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
                # hedge 模式下 posSide 常在 info 里
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

    except Exception as e:
        return {"status": "error", "error": str(e)}