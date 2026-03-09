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
    uid = user["id"]
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT api_key_enc, secret_enc, passphrase_enc FROM user_api_keys WHERE user_id=?",
            (uid,)
        ).fetchone()

        def _enc_or_keep(field_key: str, body_val: str) -> str:
            if (body_val or "").strip():
                return encrypt((body_val or "").strip())
            if existing and (existing[field_key] or "").strip():
                return existing[field_key]
            return ""

        api_key_enc    = _enc_or_keep("api_key_enc", body.api_key)
        secret_enc     = _enc_or_keep("secret_enc", body.secret)
        passphrase_enc = _enc_or_keep("passphrase_enc", body.passphrase)

        if not api_key_enc or not secret_enc or not passphrase_enc:
            raise HTTPException(
                status_code=400,
                detail="请完整填写 API Key、Secret Key 和 Passphrase（留空仅在不修改时使用）"
            )

        conn.execute('''
            INSERT OR REPLACE INTO user_api_keys
              (user_id, api_key_enc, secret_enc, passphrase_enc, is_simulate, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        ''', (uid, api_key_enc, secret_enc, passphrase_enc, int(body.is_simulate)))
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


@router.get("/validate", summary="验证当前 API Key 是否有效（调用 OKX 余额接口）")
def validate_key(user=Depends(get_current_user)):
    """验证时若当前模式认证失败，会尝试另一模式并提示「模拟盘/实盘」是否选错。"""
    import ccxt
    uid = user["id"]
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT api_key_enc, secret_enc, passphrase_enc, is_simulate FROM user_api_keys WHERE user_id=?",
            (uid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="请先配置 API Key")

    api_key = decrypt(row["api_key_enc"])
    secret = decrypt(row["secret_enc"])
    password = decrypt(row["passphrase_enc"])
    is_simulate = bool(row["is_simulate"])

    def _build(sandbox: bool):
        ex = ccxt.okx({
            "apiKey": api_key, "secret": secret, "password": password,
            "enableRateLimit": True,
        })
        ex.set_sandbox_mode(sandbox)
        return ex

    # 先用当前配置验证
    ex = _build(is_simulate)
    try:
        ex.fetch_balance()
        return {"valid": True, "message": "API Key 有效"}
    except ccxt.AuthenticationError:
        pass
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"验证请求失败：{e}")

    # 当前模式认证失败：尝试另一模式，用于提示用户是否选错模拟盘/实盘
    ex_other = _build(not is_simulate)
    try:
        ex_other.fetch_balance()
        if is_simulate:
            raise HTTPException(
                status_code=400,
                detail="当前勾选了「使用模拟盘」，但您的 Key 属于实盘。请取消勾选「使用模拟盘」后保存再试。"
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="当前未勾选「使用模拟盘」，但您的 Key 属于模拟盘。请在设置中勾选「使用模拟盘」后保存再试。"
            )
    except HTTPException:
        raise
    except ccxt.AuthenticationError as e:
        raise HTTPException(status_code=400, detail=f"API Key 认证失败（模拟盘/实盘均失败）：{e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"验证请求失败：{e}")


@router.get("/live-balance", summary="从 OKX 实时拉取账户余额")
def live_balance(user=Depends(get_current_user)):
    """直接调用 OKX 接口获取最新 USDT 余额，不依赖数据库历史记录。"""
    ex = get_user_exchange(user["id"])
    try:
        bal = ex.fetch_balance()
        usdt = bal.get("USDT", {})
        return {
            "total": usdt.get("total", 0),
            "free":  usdt.get("free",  0),
            "used":  usdt.get("used",  0),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取余额失败：{e}")


@router.delete("/reset", summary="清除 OKX API Key 配置")
def reset_keys(user=Depends(get_current_user)):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM user_api_keys WHERE user_id=?", (user["id"],))
        conn.commit()
    finally:
        conn.close()
    return {"status": "cleared"}


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
