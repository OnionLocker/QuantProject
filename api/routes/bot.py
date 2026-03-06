"""
api/routes/bot.py - Bot 启停 / 状态 / 风控
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from api.auth.jwt_handler import get_current_user
from core.user_bot import manager as bot_mgr
from utils.trade_state import load_state  # 兼容单用户版（多用户用 _load_state）
import json
from execution.db_handler import DB_PATH
import sqlite3

router = APIRouter(prefix="/api/bot", tags=["bot"])


def _load_user_state(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM bot_state WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return {}


@router.post("/start", summary="启动我的 Bot")
def start(user=Depends(get_current_user)):
    result = bot_mgr.start_bot(user["id"], user["username"])
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
    return {"status": "resumed"}
