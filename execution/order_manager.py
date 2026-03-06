"""
execution/order_manager.py - OKX 订单执行模块（单用户版，供 main.py 使用）

多用户版请使用 core/user_bot/runner.py 中的内联逻辑。
"""
import sys
import os
import functools
from typing import Optional, Dict, Any

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from core.okx_client import get_exchange, retry_on_network_error
from utils.logger import bot_logger


def _detect_okx_position_mode(exchange) -> str:
    """检测 OKX 账户持仓模式：hedge（双向）或 net（单向）"""
    try:
        cfg = exchange.private_get_account_config()
        data = (cfg or {}).get("data") or []
        pos_mode = (data[0] or {}).get("posMode", "")
        if pos_mode == "long_short_mode":
            return "hedge"
        if pos_mode == "net_mode":
            return "net"
    except Exception:
        pass
    return "net"


@retry_on_network_error(max_retries=3, base_delay=2.0)
def set_leverage(symbol: str, leverage: int,
                 margin_mode: str = "cross",
                 pos_side: Optional[str] = None) -> bool:
    exchange = get_exchange()
    try:
        params = {"marginMode": margin_mode}
        if pos_side:
            params["posSide"] = pos_side
        exchange.set_leverage(leverage, symbol, params=params)
        bot_logger.info(f"✅ 成功将 {symbol} 的杠杆设置为: {leverage} 倍！")
        return True
    except Exception as e:
        bot_logger.error(f"❌ 设置杠杆失败: {e}")
        return False


@retry_on_network_error(max_retries=3, base_delay=2.0)
def place_market_order(symbol: str, side: str, amount: float, *,
                       reduce_only: bool = False,
                       pos_side: Optional[str] = None,
                       margin_mode: str = "cross") -> Optional[Dict[str, Any]]:
    exchange = get_exchange()
    try:
        pos_mode = _detect_okx_position_mode(exchange)
        params: Dict[str, Any] = {"tdMode": margin_mode}

        if reduce_only:
            params["reduceOnly"] = True

        if pos_mode == "hedge":
            if not pos_side:
                if reduce_only:
                    raise ValueError("hedge 模式平仓必须传 pos_side")
                pos_side = "long" if side == "buy" else "short"
            params["posSide"] = pos_side

        bot_logger.info(f"⚠️  发送市价单 side={side} amount={amount} reduceOnly={reduce_only}")
        order = exchange.create_order(
            symbol=symbol, type="market", side=side, amount=amount, params=params
        )
        bot_logger.info(f"✅ 市价单成功，订单号: {order.get('id')}")
        return order
    except Exception as e:
        bot_logger.error(f"❌ 市价单失败: {e}")
        return None


@retry_on_network_error(max_retries=3, base_delay=2.0)
def place_algo_order(symbol: str, side: str, amount: float,
                     trigger_price: float, pos_side: str,
                     algo_type: str = "sl",
                     margin_mode: str = "cross") -> Optional[Dict[str, Any]]:
    """
    下 OKX 条件单（止损 / 止盈）。

    ccxt 4.x OKX 行为（已通过源码验证）：
    - params 里有 stopLossPrice → 自动设 ordType=conditional，走 /trade/order-algo
    - slOrdPx=-1 / tpOrdPx=-1 表示触发后以市价成交
    """
    exchange = get_exchange()
    try:
        pos_mode = _detect_okx_position_mode(exchange)
        params: Dict[str, Any] = {
            "reduceOnly": True,
            "tdMode": margin_mode,
        }
        if algo_type == "sl":
            params["stopLossPrice"]   = trigger_price
            params["slOrdPx"]         = -1        # 触发后市价成交
            params["slTriggerPxType"] = "last"
        else:
            params["takeProfitPrice"] = trigger_price
            params["tpOrdPx"]         = -1        # 触发后市价成交
            params["tpTriggerPxType"] = "last"

        if pos_mode == "hedge":
            params["posSide"] = pos_side

        type_str = "止损(SL)" if algo_type == "sl" else "止盈(TP)"
        bot_logger.info(f"⚠️  发送 {type_str} 条件单 | 触发价: {trigger_price}")
        order = exchange.create_order(
            symbol=symbol, type="market", side=side, amount=amount,
            price=None, params=params
        )
        bot_logger.info(f"✅ {type_str} 条件单成功，订单号: {order.get('id')}")
        return order
    except Exception as e:
        bot_logger.error(f"❌ {type_str} 条件单失败: {e}")
        return None


@retry_on_network_error(max_retries=3, base_delay=2.0)
def cancel_all_algo_orders(symbol: str) -> bool:
    exchange = get_exchange()
    try:
        exchange.cancel_all_orders(symbol, params={"stop": True})
        bot_logger.info(f"🧹 已清理 {symbol} 所有条件挂单")
        return True
    except Exception as e:
        bot_logger.error(f"❌ 清理条件挂单失败: {e}")
        return False


@retry_on_network_error(max_retries=3, base_delay=2.0)
def get_available_usdt() -> float:
    """获取合约账户可用 USDT（指定 swap 账户类型）"""
    exchange = get_exchange()
    try:
        # OKX 永续合约余额在 unified 或 trading 账户下
        balance = exchange.fetch_balance(params={"type": "swap"})
        return float(balance.get("USDT", {}).get("free", 0))
    except Exception:
        try:
            # 回退到 trading 账户
            balance = exchange.fetch_balance(params={"type": "trading"})
            return float(balance.get("USDT", {}).get("free", 0))
        except Exception as e:
            bot_logger.error(f"❌ 获取余额失败: {e}")
            return 0.0
