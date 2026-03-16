"""
api/server.py - FastAPI 应用入口（多用户版）

启动：
  python3.11 -m uvicorn api.server:app --host 0.0.0.0 --port 8080

前端静态文件部署：
  将 React build 产物放入 frontend/dist/，FastAPI 自动托管。
"""
import os
import sys
import asyncio
import json
import time
from contextlib import asynccontextmanager

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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


# ── 价格缓存（多 WebSocket 连接共享，避免重复拉取交易所价格）───────────────────
_price_cache: dict = {}   # symbol -> {"price": float, "ts": float}
_PRICE_CACHE_TTL = 3.0    # 缓存有效期（秒）


def _get_cached_price(ex, symbol: str):
    """从缓存获取价格，超时才真正调用交易所 API。"""
    now = time.time()
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


# ── Lifespan（替代已弃用的 @app.on_event）─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动时初始化数据库，关闭时可做清理。"""
    init_db()
    yield
    # shutdown 清理（目前无需特殊操作）


app = FastAPI(title="QuantBot", version="3.0-multiuser", lifespan=lifespan)


# ── CORS：从 config.yaml 读取允许的源，默认开发模式全放 ──────────────────────
_api_cfg = get_config().get("api", {})
_cors_origins = _api_cfg.get("cors_origins", ["*"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
