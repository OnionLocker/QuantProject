"""
api/routes/data.py - 交易记录 / 余额历史 / 策略列表 / 回测

回测参数优先读取用户的 DB 个性化配置，fallback 到 config.yaml。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from api.auth.jwt_handler import get_current_user
from execution.db_handler import (
    get_conn, load_user_config,
    save_backtest_history, load_backtest_history, load_backtest_history_detail,
)
from utils.config_loader import get_config
from strategy.registry import list_strategies
from backtest.engine import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES
import threading

router = APIRouter(prefix="/api/data", tags=["data"])

_backtest_lock = threading.Lock()     # 保护下面两个共享 dict 的并发读写
_backtest_results: dict = {}   # user_id -> result (内存缓存，运行中也写DB)
_backtest_running: dict = {}   # user_id -> bool


@router.get("/trades", summary="我的历史交易")
def get_trades(limit: int = 50, user=Depends(get_current_user)):
    conn = get_conn()
    try:
        # 新版表结构：entry/exit 模型
        rows = conn.execute(
            "SELECT id, entry_time, symbol, side, status, entry_price, amount, pnl, fee "
            "FROM trade_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user["id"], limit)
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0],
                "timestamp": r[1],
                "symbol": r[2],
                "side": r[3],
                "action": r[4],
                "price": r[5],
                "amount": r[6],
                "pnl": r[7],
                "reason": f"fee={r[8]}" if r[8] is not None else "",
            })
        return result
    except Exception:
        # 兼容旧版表结构
        rows = conn.execute(
            "SELECT id,timestamp,symbol,side,action,price,amount,pnl,reason "
            "FROM trade_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user["id"], limit)
        ).fetchall()
        keys = ["id", "timestamp", "symbol", "side", "action", "price", "amount", "pnl", "reason"]
        return [dict(zip(keys, r)) for r in rows]


@router.get("/balance", summary="我的每日余额历史")
def get_balance(limit: int = 90, user=Depends(get_current_user)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, balance FROM daily_balance WHERE user_id=? ORDER BY date DESC LIMIT ?",
        (user["id"], limit)
    ).fetchall()
    return [{"date": r[0], "balance": r[1]} for r in rows]


@router.get("/strategies", summary="已注册策略列表")
def get_strategies():
    return list_strategies()


@router.get("/backtest/options", summary="回测可选参数（品种、周期）")
def get_backtest_options():
    return {
        "symbols":    SUPPORTED_SYMBOLS,
        "timeframes": SUPPORTED_TIMEFRAMES,
        "strategies": list_strategies(),
    }


class BacktestBody(BaseModel):
    strategy_name:    str   = ""      # 空则读用户DB配置，再 fallback config.yaml
    symbol:           str   = ""
    timeframe:        str   = ""
    start_date:       str   = ""
    end_date:         str   = ""
    initial_capital:  float = 5000.0
    # 执行层参数（0 表示"未指定，用默认"）
    leverage:         float = 0.0
    risk_pct:         float = 0.0
    fee_rate:         float = 0.0
    slippage:         float = 0.0
    # 策略层参数（key-value，透传给策略 __init__）
    strategy_params:  dict  = {}


@router.post("/backtest/run", summary="触发回测（后台异步执行）")
def run_backtest(body: BacktestBody, user=Depends(get_current_user)):
    uid = user["id"]
    with _backtest_lock:
        if _backtest_running.get(uid):
            return {"status": "already_running"}
        _backtest_running[uid] = True
        _backtest_results[uid] = {"status": "running"}

    def _do():
        try:
            from backtest.engine import run_backtest as _engine
            from strategy.registry import get_strategy

            # ── 参数优先级：请求体 > 用户DB配置 > config.yaml ───────────────
            global_cfg = get_config()
            bc = global_cfg.get("bot", {})
            rc = global_cfg.get("risk", {})
            sc = global_cfg.get("strategy", {})
            user_cfg = load_user_config(uid)

            strategy_name = (
                body.strategy_name.strip() or
                user_cfg.get("strategy_name") or
                sc.get("name", "PA_5S")
            )
            symbol = (
                body.symbol.strip() or
                user_cfg.get("symbol") or
                bc.get("symbol", "BTC/USDT:USDT")
            ).split(":")[0]   # 去掉 :USDT 后缀，回测用裸对
            timeframe = (
                body.timeframe.strip() or
                user_cfg.get("timeframe") or
                bc.get("timeframe", "1h")
            )
            leverage = body.leverage or user_cfg.get("leverage") or bc.get("leverage", 3)
            risk_pct = body.risk_pct or user_cfg.get("risk_pct") or rc.get("risk_per_trade_pct", 0.01)
            fee_rate = body.fee_rate or bc.get("taker_fee_rate", 0.0005)
            slippage = body.slippage or 0.0002

            # 策略参数：请求体 > 用户DB > config.yaml
            strategy_params = (
                body.strategy_params or
                user_cfg.get("strategy_params") or
                sc.get("params", {})
            )

            strategy = get_strategy(strategy_name, **strategy_params)

            # 进度回调：引擎每完成 10% K线更新一次内存状态
            def _progress_cb(pct: int):
                with _backtest_lock:
                    _backtest_results[uid] = {"status": "running", "progress_pct": pct}

            result = _engine(
                strategy        = strategy,
                symbol          = symbol,
                timeframe       = timeframe,
                start_date      = body.start_date or None,
                end_date        = body.end_date   or None,
                initial_capital = body.initial_capital,
                leverage        = leverage,
                risk_pct        = risk_pct,
                fee_rate        = fee_rate,
                slippage        = slippage,
                silent          = True,
                progress_cb     = _progress_cb,
            )
            with _backtest_lock:
                _backtest_results[uid] = result or {"status": "done"}
            # 回测成功则持久化历史
            if result and result.get("status") == "done":
                try:
                    save_backtest_history(uid, result)
                except Exception:
                    pass
        except Exception as e:
            with _backtest_lock:
                _backtest_results[uid] = {"status": "error", "error": str(e)}
        finally:
            with _backtest_lock:
                _backtest_running[uid] = False

    threading.Thread(target=_do, daemon=True).start()
    return {"status": "running"}


@router.get("/backtest/result", summary="获取最近一次回测结果")
def get_backtest_result(user=Depends(get_current_user)):
    uid = user["id"]
    with _backtest_lock:
        result = _backtest_results.get(uid)
    # 内存有结果（含 running 状态）直接返回
    if result:
        return result
    # 内存为空（服务重启后）：fallback 读数据库最新一条
    history = load_backtest_history(uid)
    if history:
        detail = load_backtest_history_detail(uid, history[0]["id"])
        if detail:
            return detail
    return {"status": "no_result"}


@router.get("/backtest/history", summary="获取历史回测列表（最近20条摘要）")
def get_backtest_history(user=Depends(get_current_user)):
    return load_backtest_history(user["id"])


@router.get("/backtest/history/{history_id}", summary="获取某条历史回测的完整结果")
def get_backtest_history_detail(history_id: int, user=Depends(get_current_user)):
    result = load_backtest_history_detail(user["id"], history_id)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return result
