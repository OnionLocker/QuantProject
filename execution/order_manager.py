import sys
import os
from typing import Optional, Dict, Any

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from core.okx_client import get_exchange
from utils.logger import bot_logger  # 引入日志系统，抓取精准报错

# 引入我们刚刚写的轻量级 SQLite 记录工具
from execution.db_handler import record_balance, record_trade

def _detect_okx_position_mode(exchange) -> str:
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

def set_leverage(symbol: str, leverage: int, margin_mode: str = "cross", pos_side: Optional[str] = None) -> bool:
    exchange = get_exchange()
    try:
        params = {"marginMode": margin_mode}
        if pos_side:
            params["posSide"] = pos_side
        exchange.set_leverage(leverage, symbol, params=params)
        bot_logger.info(f"✅ 成功将 {symbol} 的杠杆设置为: {leverage} 倍！")
        return True
    except Exception as e:
        bot_logger.error(f"❌ 设置杠杆失败，报错: {e}")
        return False

def place_market_order(symbol: str, side: str, amount: float, *, reduce_only: bool = False, pos_side: Optional[str] = None, margin_mode: str = "cross") -> Optional[Dict[str, Any]]:
    exchange = get_exchange()
    try:
        pos_mode = _detect_okx_position_mode(exchange)
        params: Dict[str, Any] = {"tdMode": margin_mode}

        if reduce_only:
            params["reduceOnly"] = True

        if pos_mode == "hedge":
            if pos_side:
                params["posSide"] = pos_side
            else:
                if reduce_only:
                    raise ValueError("当前账户为双向持仓(hedge)，平仓必须传 pos_side")
            params["posSide"] = "long" if side == "buy" else "short"

        bot_logger.info(f"⚠️ [真实交易] 正在发送市价单... side={side}, amount={amount}, reduceOnly={reduce_only}")
        order = exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
        bot_logger.info(f"✅ 市价交易成功！订单号: {order.get('id')}")
        
        # ================= 新增：自动记录交易明细到 SQLite =================
        try:
            # 获取真实的成交均价，如果找不到则记录为 0.0
            exec_price = float(order.get('average') or order.get('price') or 0.0)
            action_type = "平仓" if reduce_only else "开仓"
            
            record_trade(
                side=side, 
                price=exec_price, 
                amount=amount, 
                symbol=symbol,
                action=action_type,
                reason="市价单执行"
            )
        except Exception as e:
            bot_logger.error(f"⚠️ SQLite 交易记录写入失败: {e}")
        # ==============================================================
        
        return order
    except Exception as e:
        bot_logger.error(f"❌ 市价交易失败，报错: {e}")
        return None

# ================= 交易所托管保护单模块 =================

def place_algo_order(symbol: str, side: str, amount: float, trigger_price: float, pos_side: str, algo_type: str = "sl", margin_mode: str = "cross") -> Optional[Dict[str, Any]]:
    exchange = get_exchange()
    try:
        pos_mode = _detect_okx_position_mode(exchange)
        
        params: Dict[str, Any] = {
            'reduceOnly': True,
            'tdMode': margin_mode 
        }
        
        if algo_type == "sl":
            params['stopLossPrice'] = trigger_price
        else:
            params['takeProfitPrice'] = trigger_price

        if pos_mode == "hedge":
            params["posSide"] = pos_side

        type_str = "止损(SL)" if algo_type == "sl" else "止盈(TP)"
        bot_logger.info(f"⚠️ 正在向 OKX 发送 {type_str} 挂单 | 方向: {side} | 触发价: {trigger_price}")
        
        order = exchange.create_order(symbol=symbol, type='market', side=side, amount=amount, price=None, params=params)
        bot_logger.info(f"✅ {type_str} 挂单成功！订单号: {order.get('id')}")
        return order
    except Exception as e:
        bot_logger.error(f"❌ {type_str} 保护单被交易所拒绝，详细原因: {str(e)}")
        return None

def cancel_all_algo_orders(symbol: str) -> bool:
    exchange = get_exchange()
    try:
        exchange.cancel_all_orders(symbol, params={'stop': True})
        bot_logger.info(f"🧹 成功清理 {symbol} 的所有残留条件挂单！")
        return True
    except Exception as e:
        bot_logger.error(f"❌ 清理残留条件挂单失败: {e}")
        return False

def get_available_usdt() -> float:
    exchange = get_exchange()
    try:
        balance = exchange.fetch_balance({"type": "trading"})
        available_usdt = balance.get("USDT", {}).get("free", 0)
        
        # ================= 新增：自动记录每日余额到 SQLite =================
        try:
            # 每次获取可用余额时自动记录。如果一天内获取多次，只会覆盖更新这一天的最终数据
            record_balance(float(available_usdt))
        except Exception as e:
            bot_logger.error(f"⚠️ SQLite 余额记录写入失败: {e}")
        # ==============================================================
        
        return float(available_usdt)
    except Exception as e:
        bot_logger.error(f"❌ 获取余额失败: {e}")
        return 0.0