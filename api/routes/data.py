"""
api/routes/data.py - 交易记录 / 余额历史 / 策略列表 / 回测
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from api.auth.jwt_handler import get_current_user
from execution.db_handler import get_conn
from strategy.registry import list_strategies
import threading

router = APIRouter(prefix="/api/data", tags=["data"])

_backtest_results: dict = {}   # user_id -> result


@router.get("/trades", summary="我的历史交易")
def get_trades(limit: int = 50, user=Depends(get_current_user)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,timestamp,symbol,side,action,price,amount,pnl,reason "
            "FROM trade_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user["id"], limit)
        ).fetchall()
    finally:
        conn.close()
    keys = ["id","timestamp","symbol","side","action","price","amount","pnl","reason"]
    return [dict(zip(keys, r)) for r in rows]


@router.get("/balance", summary="我的每日余额历史")
def get_balance(limit: int = 90, user=Depends(get_current_user)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT date, balance FROM daily_balance WHERE user_id=? ORDER BY date DESC LIMIT ?",
            (user["id"], limit)
        ).fetchall()
    finally:
        conn.close()
    return [{"date": r[0], "balance": r[1]} for r in rows]


@router.get("/strategies", summary="已注册策略列表")
def get_strategies():
    return list_strategies()


@router.post("/backtest/run", summary="触发回测（异步）")
def run_backtest(strategy_name: str = "PA_V2", user=Depends(get_current_user)):
    def _do():
        try:
            from backtest.engine import run_backtest as _engine
            from strategy.registry import get_strategy
            result = _engine(get_strategy(strategy_name), silent=True)
            _backtest_results[user["id"]] = result or {"status": "done"}
        except Exception as e:
            _backtest_results[user["id"]] = {"status": "error", "error": str(e)}

    threading.Thread(target=_do, daemon=True).start()
    return {"status": "running", "strategy": strategy_name}


@router.get("/backtest/result", summary="获取最近一次回测结果")
def get_backtest_result(user=Depends(get_current_user)):
    result = _backtest_results.get(user["id"])
    if not result:
        return {"status": "no_result"}
    return result
