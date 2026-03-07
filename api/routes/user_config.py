"""
api/routes/user_config.py - 每用户策略/交易配置管理

允许每个用户独立配置：品种、周期、杠杆、风险比例、策略名称及参数。
未设置的字段 Bot 运行时自动 fallback 到全局 config.yaml。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from api.auth.jwt_handler import get_current_user
from execution.db_handler import save_user_config, load_user_config
from strategy.registry import list_strategies, get_strategy_params
from backtest.engine import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES

router = APIRouter(prefix="/api/user-config", tags=["user_config"])


class UserConfigBody(BaseModel):
    symbol:          Optional[str]   = None
    timeframe:       Optional[str]   = None
    leverage:        Optional[float] = None
    risk_pct:        Optional[float] = None
    strategy_name:   Optional[str]   = None
    strategy_params: Optional[dict]  = None


@router.get("", summary="获取当前用户的个性化配置")
def get_config(user=Depends(get_current_user)):
    cfg = load_user_config(user["id"])
    return {
        "config": cfg,
        "options": {
            "symbols":    SUPPORTED_SYMBOLS,
            "timeframes": SUPPORTED_TIMEFRAMES,
            "strategies": list_strategies(),
        }
    }


@router.post("/save", summary="保存用户个性化配置")
def save_config(body: UserConfigBody, user=Depends(get_current_user)):
    # 校验品种
    if body.symbol and body.symbol not in SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"不支持的品种: {body.symbol}")
    # 校验周期
    if body.timeframe and body.timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"不支持的周期: {body.timeframe}")
    # 校验杠杆
    if body.leverage is not None and not (1 <= body.leverage <= 125):
        raise HTTPException(status_code=400, detail="杠杆范围: 1~125")
    # 校验风险比例
    if body.risk_pct is not None and not (0.001 <= body.risk_pct <= 0.1):
        raise HTTPException(status_code=400, detail="风险比例范围: 0.1%~10%")
    # 校验策略名
    if body.strategy_name:
        known = [s["name"] for s in list_strategies()]
        if body.strategy_name not in known:
            raise HTTPException(status_code=400, detail=f"未知策略: {body.strategy_name}")

    config = {}
    if body.symbol          is not None: config["symbol"]          = body.symbol
    if body.timeframe       is not None: config["timeframe"]       = body.timeframe
    if body.leverage        is not None: config["leverage"]        = body.leverage
    if body.risk_pct        is not None: config["risk_pct"]        = body.risk_pct
    if body.strategy_name   is not None: config["strategy_name"]   = body.strategy_name
    if body.strategy_params is not None: config["strategy_params"] = body.strategy_params

    save_user_config(user["id"], config)
    return {"status": "saved", "config": config}


@router.delete("/reset", summary="重置为全局默认配置")
def reset_config(user=Depends(get_current_user)):
    """清空用户个性化配置，Bot 运行时将完全使用 config.yaml 的全局设置。"""
    save_user_config(user["id"], {
        "symbol": None, "timeframe": None, "leverage": None,
        "risk_pct": None, "strategy_name": None, "strategy_params": None,
    })
    return {"status": "reset"}


@router.get("/strategy-params/{strategy_name}", summary="获取指定策略的参数元数据")
def get_params(strategy_name: str):
    params = get_strategy_params(strategy_name)
    if not params:
        raise HTTPException(status_code=404, detail=f"策略 '{strategy_name}' 未找到")
    return {"strategy_name": strategy_name, "params": params}
