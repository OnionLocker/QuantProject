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
        _patch_parse_market(ex)  # V5.2b: 防御 base=None
        return ex

    def _probe(ex):
        # 先尝试最常规余额接口；若 OKX/ccxt 在某些环境下返回异常，再降级试账户配置
        try:
            ex.fetch_balance({"type": "swap"})
            return True, None
        except ccxt.AuthenticationError as e:
            return False, e
        except Exception as e:
            msg = str(e)
            # 某些模拟盘环境 fetch_balance 会因 ccxt/返回结构问题抛 TypeError，但认证其实已通过
            if 'NoneType' in msg and '+' in msg:
                try:
                    ex.fetch_accounts()
                    return True, None
                except ccxt.AuthenticationError as e2:
                    return False, e2
                except Exception as e2:
                    return False, e2
            try:
                ex.fetch_accounts()
                return True, None
            except ccxt.AuthenticationError as e2:
                return False, e2
            except Exception as e2:
                return False, e2

    ok, err = _probe(_build(is_simulate))
    if ok:
        return {"valid": True, "message": "API Key 有效"}

    ok_other, err_other = _probe(_build(not is_simulate))
    if ok_other:
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

    raise HTTPException(status_code=400, detail=f"API Key 验证失败：{err}")


@router.get("/live-balance", summary="从 OKX 实时拉取账户余额")
def live_balance(user=Depends(get_current_user)):
    """直接调用 OKX 接口获取最新资产。
    优先使用 OKX 原始账户接口获取总权益（更接近网页显示口径），失败时再回退到 ccxt balance。
    """
    import ccxt
    ex = get_user_exchange(user["id"])

    def _f(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    # 1) 优先走 OKX 原始账户接口，拿总权益/可用/占用，更贴近 OKX 页面口径
    try:
        raw = ex.privateGetAccountBalance()
        data = (raw or {}).get('data') or []
        if data:
            d0 = data[0] or {}
            total = _f(d0.get('totalEq') or d0.get('adjEq') or d0.get('isoEq'))
            details = d0.get('details') or []
            usdt_detail = None
            for item in details:
                if (item or {}).get('ccy') == 'USDT':
                    usdt_detail = item
                    break
            free = _f((usdt_detail or {}).get('availBal'))
            # 占用 = 总权益 - 可用（近似口径；比单纯 used 更接近页面“总资产”）
            used = max(total - free, 0.0)
            return {
                'total': round(total, 4),
                'free': round(free, 4),
                'used': round(used, 4),
                'account_type': 'okx_total_equity',
            }
    except ccxt.AuthenticationError:
        raise HTTPException(status_code=400, detail='OKX 认证失败：请检查 API Key / Secret / Passphrase，以及模拟盘开关是否正确')
    except ccxt.NetworkError as e:
        raise HTTPException(status_code=502, detail=f'连接 OKX 失败：{e}')
    except Exception:
        pass

    # 2) 回退到 ccxt balance（兼容旧逻辑）
    try:
        bal = ex.fetch_balance({'type': 'swap'})
        usdt = bal.get('USDT', {})
        total = usdt.get('total') or 0
        free  = usdt.get('free')  or 0
        used  = usdt.get('used')  or 0
        if total == 0:
            bal_spot = ex.fetch_balance({'type': 'spot'})
            usdt_spot = bal_spot.get('USDT', {})
            spot_total = usdt_spot.get('total') or 0
            if spot_total > 0:
                return {
                    'total': round(float(spot_total), 4),
                    'free': round(float(usdt_spot.get('free') or 0), 4),
                    'used': round(float(usdt_spot.get('used') or 0), 4),
                    'account_type': 'spot',
                }
        return {
            'total': round(float(total), 4),
            'free': round(float(free), 4),
            'used': round(float(used), 4),
            'account_type': 'swap',
        }
    except ccxt.AuthenticationError:
        raise HTTPException(status_code=400, detail='OKX 认证失败：请检查 API Key / Secret / Passphrase，以及模拟盘开关是否正确')
    except ccxt.NetworkError as e:
        raise HTTPException(status_code=502, detail=f'连接 OKX 失败：{e}')
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'获取余额失败：{e}')


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

    V5.2b fix: 永久 monkey-patch parse_market，全生命周期防御 base=None。
    """
    import ccxt
    import logging as _logging

    _log = _logging.getLogger("get_user_exchange")

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

    # ── V5.2b: 永久 patch parse_market，全生命周期防御 ──────────────────────
    _patch_parse_market(ex)

    # ── 预加载 markets（patch 后不会再崩）──────────────────────────────────
    try:
        ex.load_markets()
        _log.info(f"load_markets 成功: {len(ex.markets)} 个交易对")
    except Exception as e:
        _log.warning(f"load_markets 预加载失败（后续调用将自动重试）: {e}")

    with _exchange_cache_lock:
        _exchange_cache[user_id] = {"ex": ex, "ts": now}

    return ex


def _patch_parse_market(ex):
    """
    永久 monkey-patch ccxt exchange 实例的 parse_market 方法。

    问题：ccxt 4.x OKX 适配中，某些交易对（已退市/测试合约/OPTION 类）
    的 base 或 quote 解析为 None，导致 parse_market() 中
    `symbol = base + '/' + quote` 抛出 TypeError。

    方案：包装 parse_market，捕获 TypeError 返回 None，
    同时包装 fetch_markets/load_markets 过滤掉 None 结果。
    这是永久 patch，不恢复原始方法，确保 create_order/fetch_ticker 等
    后续操作中如果触发 re-parse 也不会崩溃。
    """
    import logging as _logging
    _log = _logging.getLogger("patch_parse_market")

    # 避免重复 patch
    if getattr(ex, '_parse_market_patched', False):
        return
    ex._parse_market_patched = True

    _original_parse_market = ex.parse_market

    def _safe_parse_market(market, *args, **kwargs):
        try:
            result = _original_parse_market(market, *args, **kwargs)
            return result
        except TypeError as e:
            if "NoneType" in str(e):
                inst_id = (market or {}).get("instId", "unknown") if isinstance(market, dict) else "unknown"
                _log.debug(f"parse_market 跳过无效交易对 {inst_id}: {e}")
                return None
            raise

    ex.parse_market = _safe_parse_market

    # ── 同时 patch fetch_markets，过滤掉 parse_market 返回的 None ──────────
    _original_fetch_markets = getattr(ex, 'fetch_markets', None)
    if _original_fetch_markets:
        def _safe_fetch_markets(*args, **kwargs):
            markets = _original_fetch_markets(*args, **kwargs)
            if isinstance(markets, list):
                before = len(markets)
                markets = [m for m in markets if m is not None]
                skipped = before - len(markets)
                if skipped > 0:
                    _log.info(f"fetch_markets: 过滤了 {skipped} 个无效交易对")
            return markets
        ex.fetch_markets = _safe_fetch_markets
