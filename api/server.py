"""
api/server.py - FastAPI 应用入口（多用户版）

启动：
  python3.11 -m uvicorn api.server:app --host 0.0.0.0 --port 8080

前端静态文件部署：
  将 React build 产物放入 frontend/dist/，FastAPI 自动托管。
"""
from __future__ import annotations

import logging
import os
import sys
import asyncio
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

current_dir: str = os.path.dirname(os.path.abspath(__file__))
project_root: str = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime

from api.routes.auth        import router as auth_router
from api.routes.keys        import router as keys_router
from api.routes.bot         import router as bot_router
from api.routes.data        import router as data_router
from api.routes.notify      import router as notify_router
from api.routes.user_config import router as user_config_router
from api.routes.market      import router as market_router
from api.routes.news_sync   import router as news_sync_router
from api.auth.jwt_handler import get_current_user
from core.user_bot import manager as bot_mgr
from execution.db_handler import init_db
from utils.config_loader import get_config

_server_logger: logging.Logger = logging.getLogger("QuantBot.server")


# ── 价格缓存（多 WebSocket 连接共享，避免重复拉取交易所价格）───────────────────
_price_cache: Dict[str, Dict[str, Any]] = {}   # symbol -> {"price": float, "ts": float}
_PRICE_CACHE_TTL: float = 3.0    # 缓存有效期（秒）


def _get_cached_price(ex: Any, symbol: str) -> Optional[float]:
    """从缓存获取价格，超时才真正调用交易所 API。"""
    now: float = time.time()
    cached = _price_cache.get(symbol)
    if cached and (now - cached["ts"]) < _PRICE_CACHE_TTL:
        return cached["price"]
    try:
        ticker = ex.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("close") or 0) or None
        if price:
            _price_cache[symbol] = {"price": price, "ts": now}
        return price
    except Exception:
        return cached["price"] if cached else None


# ── 安全：JWT 密钥启动检查 ────────────────────────────────────────────────────
def _check_jwt_secret() -> None:
    """检查 JWT_SECRET 是否为默认值，生产环境下必须修改。"""
    from api.auth.jwt_handler import SECRET_KEY
    if SECRET_KEY == "change-me-in-production-please":
        _server_logger.warning(
            "⚠️  JWT_SECRET 使用默认值，存在安全风险！"
            "请在 .env 中设置强随机密钥：JWT_SECRET=$(python3 -c \"import secrets; print(secrets.token_hex(32))\")"
        )


# ── 速率限制中间件（基于 IP 的简易令牌桶）─────────────────────────────────────
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    简易速率限制：
    - 普通 API：每 IP 每分钟 120 次
    - 认证端点（登录/注册）：每 IP 每分钟 10 次（防暴力破解）
    """

    def __init__(self, app: Any, general_rpm: int = 120, auth_rpm: int = 10) -> None:
        super().__init__(app)
        self._general_rpm: int = general_rpm
        self._auth_rpm: int = auth_rpm
        # IP -> [(timestamp, ...)]
        self._general_hits: Dict[str, list] = defaultdict(list)
        self._auth_hits: Dict[str, list] = defaultdict(list)

    def _clean_old(self, hits: list, window: float = 60.0) -> list:
        """清理超出时间窗口的记录。"""
        now = time.time()
        return [t for t in hits if now - t < window]

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # 从反向代理头获取真实 IP（nginx 配置了 X-Real-IP）
        client_ip: str = (
            request.headers.get("X-Real-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        path: str = request.url.path

        # 认证端点特殊限流
        is_auth_path: bool = path in ("/api/auth/login", "/api/auth/register")

        if is_auth_path:
            self._auth_hits[client_ip] = self._clean_old(self._auth_hits[client_ip])
            if len(self._auth_hits[client_ip]) >= self._auth_rpm:
                _server_logger.warning(f"🚫 速率限制触发 [AUTH] IP={client_ip} path={path}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后再试"},
                    headers={"Retry-After": "60"},
                )
            self._auth_hits[client_ip].append(time.time())

        # 通用 API 限流（跳过静态文件和健康检查）
        if path.startswith("/api/"):
            self._general_hits[client_ip] = self._clean_old(self._general_hits[client_ip])
            if len(self._general_hits[client_ip]) >= self._general_rpm:
                _server_logger.warning(f"🚫 速率限制触发 [GENERAL] IP={client_ip} path={path}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后再试"},
                    headers={"Retry-After": "60"},
                )
            self._general_hits[client_ip].append(time.time())

        response = await call_next(request)
        return response


# ── 安全响应头中间件 ──────────────────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为所有响应添加安全头。"""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        # 防止 MIME 类型嗅探
        response.headers["X-Content-Type-Options"] = "nosniff"
        # 防止点击劫持
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        # XSS 保护
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # 控制 referrer 信息泄漏
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # 禁止浏览器缓存 API 响应中的敏感数据
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


# ── Lifespan（替代已弃用的 @app.on_event）─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """服务启动时初始化数据库 + 安全检查，关闭时可做清理。"""
    init_db()
    _check_jwt_secret()
    yield
    # shutdown 清理（目前无需特殊操作）


app = FastAPI(
    title="QuantBot",
    version="4.1-multiuser",
    lifespan=lifespan,
    # 生产环境禁用交互式文档（防止未授权访问）
    docs_url="/api/docs" if os.getenv("QUANTBOT_ENV", "production") == "development" else None,
    redoc_url=None,
)


# ── CORS：从 config.yaml 读取，生产环境严格限制 ──────────────────────────────
_api_cfg: Dict[str, Any] = get_config().get("api", {})
_cors_origins: list = _api_cfg.get("cors_origins", ["*"])

# 安全警告：检测到通配符
if "*" in _cors_origins:
    _server_logger.warning(
        "⚠️  CORS 配置为 allow_origins=['*']，存在安全风险！"
        "生产环境请在 config.yaml → api.cors_origins 中指定实际域名，"
        "例如: [\"https://your-domain.com\"]"
    )

# 根据是否有通配符决定 credentials 策略
# 注意：allow_credentials=True 和 allow_origins=["*"] 不能同时使用
_allow_credentials: bool = "*" not in _cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    # 仅允许实际使用的 HTTP 方法
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    # 仅允许必要的请求头
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
    # 预检请求缓存时间（秒），减少 OPTIONS 请求
    max_age=600,
)

# ── 安全中间件（注意：中间件按添加的反序执行）─────────────────────────────────
# 速率限制
_rate_cfg: Dict[str, Any] = _api_cfg.get("rate_limit", {})
app.add_middleware(
    RateLimitMiddleware,
    general_rpm=_rate_cfg.get("general_rpm", 120),
    auth_rpm=_rate_cfg.get("auth_rpm", 10),
)

# 安全响应头
app.add_middleware(SecurityHeadersMiddleware)

# ── API 路由 ─────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(keys_router)
app.include_router(bot_router)
app.include_router(data_router)
app.include_router(notify_router)
app.include_router(user_config_router)
app.include_router(market_router)
app.include_router(news_sync_router)


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


# ── WebSocket：每用户实时状态推送 ─────────────────────────────────────────────
@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket, token: str = ""):
    """ws://host:8080/ws/status?token=<jwt>"""
    await websocket.accept()
    # 校验 token
    try:
        from api.auth.jwt_handler import decode_token
        payload = decode_token(token)
        user_id  = int(payload["sub"])
        username = payload["username"]
    except Exception:
        await websocket.send_json({"error": "unauthorized"})
        await websocket.close()
        return

    # ── 同步阻塞操作打包到线程池，避免阻塞 asyncio 事件循环 ───────────────
    def _sync_gather_ws_data(uid: int):
        """在线程池中执行所有同步 IO（SQLite + ccxt HTTP），返回数据 dict。"""
        from api.routes.keys import get_user_exchange
        from execution.db_handler import get_conn

        bs = bot_mgr.bot_status(uid)

        # 读持仓（使用连接池）
        conn = get_conn()
        row = conn.execute("SELECT value FROM bot_state WHERE user_id=?", (uid,)).fetchone()
        ps = json.loads(row[0]) if row else {}

        # 拉取实时价格（使用共享缓存）
        current_price = None
        try:
            ex = get_user_exchange(uid)
            symbol = ps.get("symbol") or "BTC/USDT:USDT"
            current_price = _get_cached_price(ex, symbol)
        except Exception:
            pass

        # 浮动盈亏计算（CONTRACT_SIZE 从配置读取）
        unrealized_pnl = None
        if current_price and ps.get("position_amount", 0) > 0 and ps.get("entry_price", 0) > 0:
            is_long = ps.get("position_side") == "long"
            _bot_cfg = get_config().get("bot", {})
            CONTRACT_SIZE = _bot_cfg.get("contract_size", 0.01)
            gross = ((current_price - ps["entry_price"]) if is_long
                     else (ps["entry_price"] - current_price))
            unrealized_pnl = round(gross * ps["position_amount"] * CONTRACT_SIZE, 2)

        # 今日盈亏 & 今日交易次数
        today_pnl    = 0.0
        today_trades = 0
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            row2 = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM trade_history "
                "WHERE user_id=? AND entry_time LIKE ? AND status='closed'",
                (uid, today_str + "%")
            ).fetchone()
            if row2:
                today_trades = row2[0]
                today_pnl    = round(float(row2[1]), 2)
        except Exception:
            pass

        # 运行时长
        uptime_str = ""
        if bs.get("started_at"):
            try:
                started = datetime.strptime(bs["started_at"], "%Y-%m-%d %H:%M:%S")
                delta   = datetime.now() - started
                h, rem  = divmod(int(delta.total_seconds()), 3600)
                m       = rem // 60
                uptime_str = f"{h}h {m}m"
            except Exception:
                pass

        # V2.0: regime 状态详情
        regime_detail = {}
        try:
            selector = bot_mgr.get_user_selector(uid)
            if selector and hasattr(selector, 'last_regime_detail'):
                regime_detail = selector.last_regime_detail or {}
        except Exception:
            pass

        return {
            "ts":                datetime.now().isoformat(),
            "running":           bs.get("running", False),
            "fused":             bs.get("fused", False),
            "consecutive_losses":bs.get("consecutive_losses", 0),
            "crash_count":       bs.get("crash_count", 0),
            "started_at":        bs.get("started_at"),
            "uptime":            uptime_str,
            "last_error":        bs.get("last_error"),
            "position_side":     ps.get("position_side"),
            "position_amount":   ps.get("position_amount", 0),
            "entry_price":       ps.get("entry_price", 0),
            "active_sl":         ps.get("active_sl", 0),
            "active_tp1":        ps.get("active_tp1", 0),
            "active_tp2":        ps.get("active_tp2", 0),
            "entry_time":        ps.get("entry_time", ""),
            "strategy_name":     ps.get("strategy_name", ""),
            "signal_reason":     ps.get("signal_reason", ""),
            "current_price":     current_price,
            "unrealized_pnl":    unrealized_pnl,
            "today_trades":      today_trades,
            "today_pnl":         today_pnl,
            "contract_size":     get_config().get("bot", {}).get("contract_size", 0.01),
            "regime_detail":     regime_detail,
            # V7.0: 冷静期 & 保护状态
            "cooldown_active":      ps.get("cooldown_bars_remaining", 0) > 0,
            "cooldown_bars_remaining": ps.get("cooldown_bars_remaining", 0),
            "last_close_time":      ps.get("last_close_time", ""),
            "last_close_reason":    ps.get("last_close_reason", ""),
            "last_close_pnl":       ps.get("last_close_pnl", 0),
            "last_close_side":      ps.get("last_close_side", ""),
            "spike_cooldown_until": ps.get("spike_cooldown_until", ""),
            "signal_quality":       regime_detail.get("signal_quality"),
        }

    try:
        while True:
            data = await asyncio.to_thread(_sync_gather_ws_data, user_id)
            await websocket.send_json(data)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ── 托管前端静态文件（React build 产物）──────────────────────────────────────
_FRONTEND_DIST = os.path.join(project_root, "frontend", "dist")

if os.path.isdir(_FRONTEND_DIST):
    # 托管 /assets 等静态资源
    app.mount("/assets", StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """所有非 /api 请求都返回 index.html（SPA 路由）"""
        index = os.path.join(_FRONTEND_DIST, "index.html")
        return FileResponse(index)
