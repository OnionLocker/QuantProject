"""
api/routes/keys.py - OKX API Key 管理（加密存取）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.auth.jwt_handler import get_current_user
from api.auth.crypto import encrypt, decrypt
from execution.db_handler import get_conn

router = APIRouter(prefix="/api/keys", tags=["keys"])


class ApiKeyBody(BaseModel):
    api_key: str
    secret: str
    passphrase: str
    is_simulate: bool = False


@router.post("/save", summary="保存 OKX API Key")
def save_keys(body: ApiKeyBody, user=Depends(get_current_user)):
    conn = get_conn()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO user_api_keys
              (user_id, api_key_enc, secret_enc, passphrase_enc, is_simulate, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        ''', (
            user["id"],
            encrypt(body.api_key),
            encrypt(body.secret),
            encrypt(body.passphrase),
            int(body.is_simulate)
        ))
        conn.commit()
    finally:
        conn.close()
    return {"status": "saved"}


@router.get("/status", summary="检查是否已配置 API Key")
def key_status(user=Depends(get_current_user)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT is_simulate, updated_at FROM user_api_keys WHERE user_id=?",
            (user["id"],)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "is_simulate": bool(row["is_simulate"]),
        "updated_at": row["updated_at"]
    }


def get_user_exchange(user_id: int):
    """根据用户 ID 从数据库取出 API Key，构建 ccxt OKX 实例（不缓存，每次新建）"""
    import ccxt
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT api_key_enc, secret_enc, passphrase_enc, is_simulate FROM user_api_keys WHERE user_id=?",
            (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="请先配置 OKX API Key")
    ex = ccxt.okx({
        "apiKey":    decrypt(row["api_key_enc"]),
        "secret":    decrypt(row["secret_enc"]),
        "password":  decrypt(row["passphrase_enc"]),
        "enableRateLimit": True,
    })
    ex.set_sandbox_mode(bool(row["is_simulate"]))
    return ex
