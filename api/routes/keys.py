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
    invalidate_user_exchange_cache(uid)
    return {"status": "saved"}


@router.get("/status", summary="检查是否已配置 API Key")
def key_status(user=Depends(get_current_user)):
    conn = get_conn()
    row = conn.execute(
        "SELECT is_simulate, updated_at FROM user_api_keys WHERE user_id=?",
        (user["id"],)
    ).fetchone()
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
    row = conn.execute(
        "SELECT api_key_enc, secret_enc, passphrase_enc, is_simulate FROM user_api_keys WHERE user_id=?",
        (uid,)
    ).fetchone()
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
    """直接调用 OKX 接口获取最新 USDT 余额，不依赖数据库历史记录。
    优先读合约账户（swap），fallback 现货账户。
    """
    import ccxt
    ex = get_user_exchange(user["id"])
    try:
        # OKX 合约账户余额（永续合约用这个）
        bal = ex.fetch_balance({"type": "swap"})
        usdt = bal.get("USDT", {})
        total = usdt.get("total") or 0
        free  = usdt.get("free")  or 0
        used  = usdt.get("used")  or 0

        # 合约账户余额为0时，尝试现货账户（可能用户只充值了现货）
        if total == 0:
            bal_spot = ex.fetch_balance({"type": "spot"})
            usdt_spot = bal_spot.get("USDT", {})
            spot_total = usdt_spot.get("total") or 0
            if spot_total > 0:
                return {
                    "total":        round(float(spot_total), 4),
                    "free":         round(float(usdt_spot.get("free") or 0), 4),
                    "used":         round(float(usdt_spot.get("used") or 0), 4),
                    "account_type": "spot",
                }

        return {
            "total":        round(float(total), 4),
            "free":         round(float(free),  4),
            "used":         round(float(used),  4),
            "account_type": "swap",
        }
    except ccxt.AuthenticationError:
        raise HTTPException(status_code=400, detail="OKX 认证失败：请检查 API Key / Secret / Passphrase，以及模拟盘开关是否正确")
    except ccxt.NetworkError as e:
        raise HTTPException(status_code=502, detail=f"连接 OKX 失败：{e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取余额失败：{e}")


@router.delete("/reset", summary="清除 OKX API Key 配置")
def reset_keys(user=Depends(get_current_user)):
    conn = get_conn()
    conn.execute("DELETE FROM user_api_keys WHERE user_id=?", (user["id"],))
    conn.commit()
    invalidate_user_exchange_cache(user["id"])
    return {"status": "cleared"}


# ── per-user ccxt 实例缓存（TTL 300 秒，避免每次请求都新建）─────────────────
import time as _time
import threading as _threading

_exchange_cache: dict = {}   # user_id -> {"ex": ccxt_instance, "ts": float}
_exchange_cache_lock = _threading.Lock()
_EXCHANGE_CACHE_TTL = 300    # 5 分钟后重建（API Key 更新时也会因 TTL 自然刷新）


def invalidate_user_exchange_cache(user_id: int):
    """用户更新 API Key 后调用，立即使缓存失效。"""
    with _exchange_cache_lock:
        _exchange_cache.pop(user_id, None)


def get_user_exchange(user_id: int):
    """
    根据用户 ID 从数据库取出 API Key，构建 ccxt OKX 实例。
    使用 per-user TTL 缓存复用实例，避免频繁创建 + 交易所限频风险。
    """
    import ccxt

    now = _time.time()
    with _exchange_cache_lock:
        cached = _exchange_cache.get(user_id)
        if cached and (now - cached["ts"]) < _EXCHANGE_CACHE_TTL:
            return cached["ex"]

    conn = get_conn()
    row = conn.execute(
        "SELECT api_key_enc, secret_enc, passphrase_enc, is_simulate FROM user_api_keys WHERE user_id=?",
        (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="请先配置 OKX API Key")

    ex = ccxt.okx({
        "apiKey":    decrypt(row["api_key_enc"]),
        "secret":    decrypt(row["secret_enc"]),
        "password":  decrypt(row["passphrase_enc"]),
        "enableRateLimit": True,
    })
    ex.set_sandbox_mode(bool(row["is_simulate"]))

    with _exchange_cache_lock:
        _exchange_cache[user_id] = {"ex": ex, "ts": now}

    return ex
