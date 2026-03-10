"""
api/routes/bot.py - Bot 启停 / 状态 / 风控
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from api.auth.jwt_handler import get_current_user
from core.user_bot import manager as bot_mgr
import json
from execution.db_handler import get_conn

router = APIRouter(prefix="/api/bot", tags=["bot"])


class StartBotBody(BaseModel):
    strategy_name: Optional[str] = None


def _load_user_state(user_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT value FROM bot_state WHERE user_id=?", (user_id,)).fetchone()
    if row:
        return json.loads(row[0])
    return {}


@router.post("/start", summary="启动我的 Bot")
def start(body: StartBotBody = StartBotBody(), user=Depends(get_current_user)):
    result = bot_mgr.start_bot(user["id"], user["username"],
                               strategy_name=body.strategy_name or None)
    return result


@router.post("/stop", summary="停止我的 Bot")
def stop(user=Depends(get_current_user)):
    return bot_mgr.stop_bot(user["id"])


@router.get("/status", summary="Bot + 持仓状态")
def status(user=Depends(get_current_user)):
    bs = bot_mgr.bot_status(user["id"])
    ps = _load_user_state(user["id"])
    return {
        "bot": bs,
        "position": {
            "side":        ps.get("position_side"),
            "amount":      ps.get("position_amount", 0),
            "entry_price": ps.get("entry_price", 0),
            "active_sl":   ps.get("active_sl", 0),
            "active_tp1":  ps.get("active_tp1", 0),
            "entry_time":  ps.get("entry_time", ""),
            "strategy":    ps.get("strategy_name", ""),
            "reason":      ps.get("signal_reason", ""),
        }
    }


@router.post("/risk/resume", summary="手动恢复熔断")
def resume_risk(user=Depends(get_current_user)):
    state = bot_mgr.get_bot(user["id"])
    if not state:
        return JSONResponse(status_code=400, content={"error": "Bot 未初始化"})
    state.risk_manager.manual_resume()
    # 熔断后 Bot 线程可能已退出，需要持久化清零并重启
    from execution.db_handler import save_risk_state
    save_risk_state(user["id"], 0, state.risk_manager._daily_start_balance, False)
    # 若 Bot 线程不再运行，调用 resume_bot 重启
    if not state.is_running:
        result = bot_mgr.resume_bot(user["id"], user["username"])
        return {"status": "resumed_and_restarted", **result}
    return {"status": "resumed"}
