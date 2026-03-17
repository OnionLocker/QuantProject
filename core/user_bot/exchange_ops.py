"""
core/user_bot/exchange_ops.py - 交易所操作封装

将原 runner.py 中所有 OKX / ccxt 相关的底层操作集中到此模块：
  - 余额获取（多级回退，兼容模拟盘）
  - K 线拉取（ccxt → OKX 原始接口回退）
  - 条件单操作（SL/TP 挂单、取消）
  - 持仓查询
  - 持仓模式检测
  - 符号 / timeframe 转换
"""
from __future__ import annotations

import logging
from typing import Optional, Callable, List

import ccxt


# ── 工具转换 ──────────────────────────────────────────────────────────────────

def symbol_to_okx_inst_id(symbol: str) -> str:
    """将 ccxt 格式（BTC/USDT:USDT）转为 OKX instId（BTC-USDT-SWAP）。"""
    s = (symbol or '').upper()
    if ':' in s:
        s = s.split(':', 1)[0]
    s = s.replace('/', '-')
    if not s.endswith('-SWAP'):
        s = s + '-SWAP'
    return s


def timeframe_to_okx_bar(tf: str) -> str:
    """将 ccxt timeframe（'1h'）转为 OKX bar 参数（'1H'）。"""
    mp = {
        '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m', '30m': '30m',
        '1h': '1H', '2h': '2H', '4h': '4H', '6h': '6H', '12h': '12H',
        '1d': '1D', '1w': '1W'
    }
    return mp.get((tf or '1h').lower(), '1H')


# ── 余额获取（多级回退）──────────────────────────────────────────────────────

def get_swap_usdt(ex) -> float:
    """
    获取合约账户可用 USDT 余额。

    回退策略：
      1) OKX 原始账户接口 → totalEq/adjEq → details[USDT]
      2) ccxt fetch_balance（swap/trading/future）
      3) ccxt fetch_accounts

    已知 Bug：ccxt 在 OKX 模拟盘下 fetch_balance() 可能抛出
    TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'
    """
    def _f(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    # 1) 优先走 OKX 原始账户接口
    try:
        raw = ex.privateGetAccountBalance()
        data = (raw or {}).get('data') or []
        if data:
            d0 = data[0] or {}
            total_eq = _f(d0.get('totalEq')) or _f(d0.get('adjEq'))
            if total_eq > 0:
                details = d0.get('details') or []
                for item in details:
                    if (item or {}).get('ccy') == 'USDT':
                        v = _f((item or {}).get('availBal'))
                        if v > 0:
                            return v
                        v = _f((item or {}).get('eq')) or _f((item or {}).get('cashBal'))
                        if v > 0:
                            return v
                # 没有单独 USDT 条目时，用总权益近似
                return total_eq
    except Exception:
        pass

    # 2) 回退到 ccxt balance（已知模拟盘可能触发 TypeError）
    for acc_type in ("swap", "trading", "future"):
        try:
            bal = ex.fetch_balance(params={"type": acc_type})
            v = float(bal.get("USDT", {}).get("free", 0))
            if v > 0:
                return v
        except Exception:
            continue
    try:
        bal = ex.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))
    except Exception:
        pass

    # 3) 最后手段：fetch_accounts
    try:
        accounts = ex.fetch_accounts()
        for acc in (accounts or []):
            if acc.get("currency") == "USDT":
                v = _f(acc.get("free") or acc.get("total"))
                if v > 0:
                    return v
    except Exception:
        pass

    return 0.0


# ── 持仓模式检测 ─────────────────────────────────────────────────────────────

def detect_pos_mode(ex) -> str:
    """检测 OKX 持仓模式：'hedge'（双向）或 'net'（单向）。"""
    try:
        if hasattr(ex, 'privateGetAccountConfig'):
            cfg = ex.privateGetAccountConfig()
        else:
            cfg = ex.private_get_account_config()
        data = (cfg or {}).get("data") or []
        mode = (data[0] or {}).get("posMode", "")
        return "hedge" if mode == "long_short_mode" else "net"
    except Exception:
        return "net"


# ── K 线拉取（带回退）────────────────────────────────────────────────────────

def fetch_ohlcv_safe(
    ex, symbol: str, timeframe: str, limit: int = 200
) -> List[list]:
    """
    优先 ccxt.fetch_ohlcv；若 demo 环境异常，
    回退到 OKX 原始 K 线接口。
    """
    try:
        return ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception:
        try:
            inst_id = symbol_to_okx_inst_id(symbol)
            bar = timeframe_to_okx_bar(timeframe)
            resp = ex.publicGetMarketCandles(
                {'instId': inst_id, 'bar': bar, 'limit': str(limit)}
            )
            data = (resp or {}).get('data') or []
            rows = []
            for r in reversed(data):
                rows.append([
                    int(r[0]), float(r[1]), float(r[2]),
                    float(r[3]), float(r[4]), float(r[5])
                ])
            return rows
        except Exception:
            raise


# ── 条件单操作 ────────────────────────────────────────────────────────────────

def place_algo(
    ex,
    symbol: str,
    side: str,
    amount: float,
    trigger_price: float,
    pos_side: str,
    algo_type: str,
    margin_mode: str = "cross",
) -> Optional[dict]:
    """
    挂 SL 或 TP 条件单。

    :param algo_type: 'sl' 或 'tp'
    :return: 订单 dict 或 None（失败）
    """
    pos_mode = detect_pos_mode(ex)
    params = {"reduceOnly": True, "tdMode": margin_mode}

    if algo_type == "sl":
        params["stopLossPrice"]    = trigger_price
        params["slOrdPx"]         = -1
        params["slTriggerPxType"] = "last"
    else:
        params["takeProfitPrice"]  = trigger_price
        params["tpOrdPx"]         = -1
        params["tpTriggerPxType"] = "last"

    if pos_mode == "hedge":
        params["posSide"] = pos_side

    try:
        order = ex.create_order(
            symbol=symbol, type="market", side=side,
            amount=amount, price=None, params=params
        )
        return order
    except Exception as e:
        logging.getLogger("exchange_ops").warning(
            "%s 挂单失败 (%s side=%s price=%.4f): %s",
            algo_type.upper(), symbol, side, trigger_price, e,
        )
        return None


def cancel_all_algo(
    ex,
    symbol: str,
    logger: Optional[logging.Logger] = None,
    notify: Optional[Callable] = None,
    tag: str = "",
):
    """取消所有条件单。失败时记录日志并告警。"""
    try:
        ex.cancel_all_orders(symbol, params={"stop": True})
    except Exception as e:
        err_msg = f"{tag} ⚠️ 取消条件单失败: {e}"
        if logger:
            logger.error(err_msg)
        if notify:
            try:
                notify(
                    f"⚠️ <b>取消条件单失败</b>\n"
                    f"{str(e)[:200]}\n请人工检查是否有残留条件单！"
                )
            except Exception:
                pass


# ── 持仓查询 ─────────────────────────────────────────────────────────────────

def live_position_amount(
    ex,
    symbol: str,
    logger: Optional[logging.Logger] = None,
    tag: str = "",
) -> float:
    """
    查询交易所当前持仓合约张数。

    :return: >=0 实际持仓，-1 查询失败（不可误判为空仓）
    """
    try:
        positions = ex.fetch_positions([symbol])
        return sum(
            float(p.get("contracts") or 0)
            for p in positions
            if p.get("symbol") == symbol and float(p.get("contracts") or 0) > 0
        )
    except Exception as e:
        if logger:
            logger.warning(f"{tag} 持仓查询异常: {e}")
        return -1.0


# ── 网络错误分类 ─────────────────────────────────────────────────────────────

def classify_error(e: Exception) -> str:
    """
    将 ccxt 异常分类，返回处理策略：
      - 'rate_limit'  : 被限频
      - 'maintenance' : 交易所维护
      - 'auth_error'  : API Key 无效
      - 'network'     : 网络临时故障
      - 'unknown'     : 未知
    """
    err_str = str(e).lower()
    if isinstance(e, ccxt.RateLimitExceeded):
        return 'rate_limit'
    if isinstance(e, ccxt.AuthenticationError):
        return 'auth_error'
    if isinstance(e, ccxt.ExchangeNotAvailable) or 'maintenance' in err_str:
        return 'maintenance'
    if isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout)):
        return 'network'
    return 'unknown'
