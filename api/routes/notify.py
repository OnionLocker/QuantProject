"""
api/routes/notify.py - 每用户 Telegram 通知配置管理
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.auth.jwt_handler import get_current_user
from api.auth.crypto import encrypt, decrypt
from execution.db_handler import save_tg_config, load_tg_config
from utils.notifier import test_notify

router = APIRouter(prefix="/api/notify", tags=["notify"])


class TgConfigBody(BaseModel):
    tg_bot_token: str
    tg_chat_id:   str


@router.post("/telegram/save", summary="保存用户的 Telegram Bot 配置")
def save_telegram_config(body: TgConfigBody, user=Depends(get_current_user)):
    if not body.tg_bot_token.strip() or not body.tg_chat_id.strip():
        raise HTTPException(status_code=400, detail="Token 和 Chat ID 不能为空")
    save_tg_config(
        user["id"],
        encrypt(body.tg_bot_token.strip()),
        encrypt(body.tg_chat_id.strip()),
    )
    return {"status": "saved"}


@router.get("/telegram/status", summary="查询是否已配置 Telegram")
def get_telegram_status(user=Depends(get_current_user)):
    raw = load_tg_config(user["id"])
    configured = bool(raw["tg_bot_token_enc"] and raw["tg_chat_id_enc"])
    return {"configured": configured}


@router.post("/telegram/test", summary="测试当前 Telegram 配置是否可用")
def test_telegram_config(user=Depends(get_current_user)):
    raw = load_tg_config(user["id"])
    if not raw["tg_bot_token_enc"] or not raw["tg_chat_id_enc"]:
        raise HTTPException(status_code=400, detail="尚未配置 Telegram，请先保存配置")
    try:
        token   = decrypt(raw["tg_bot_token_enc"])
        chat_id = decrypt(raw["tg_chat_id_enc"])
    except Exception:
        raise HTTPException(status_code=500, detail="配置解密失败，请重新保存")

    success, msg = test_notify(token, chat_id)
    if not success:
        raise HTTPException(status_code=502, detail=f"发送失败：{msg}")
    return {"status": "ok", "message": msg}


@router.delete("/telegram/clear", summary="清除 Telegram 配置")
def clear_telegram_config(user=Depends(get_current_user)):
    save_tg_config(user["id"], "", "")
    return {"status": "cleared"}
