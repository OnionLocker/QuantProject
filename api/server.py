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
from api.auth.jwt_handler import get_current_user
from core.user_bot import manager as bot_mgr
from execution.db_handler import DB_PATH, init_db
import sqlite3

app = FastAPI(title="QuantBot", version="3.0-multiuser")


@app.on_event("startup")
def on_startup():
    """服务启动时初始化数据库（只在这里调用，避免 import db_handler 时自动执行）"""
    init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

    try:
        while True:
            bs = bot_mgr.bot_status(user_id)

            # 读持仓
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT value FROM bot_state WHERE user_id=?", (user_id,)).fetchone()
            conn.close()
            ps = json.loads(row[0]) if row else {}

            # 拉取实时价格（用于浮动盈亏计算）
            current_price = None
            try:
                from api.routes.keys import get_user_exchange
                ex = get_user_exchange(user_id)
                symbol = ps.get("symbol") or "BTC/USDT:USDT"
                ticker = ex.fetch_ticker(symbol)
                current_price = float(ticker.get("last") or ticker.get("close") or 0) or None
            except Exception:
                pass

            # 浮动盈亏计算
            unrealized_pnl = None
            if current_price and ps.get("position_amount", 0) > 0 and ps.get("entry_price", 0) > 0:
                is_long = ps.get("position_side") == "long"
                CONTRACT_SIZE = 0.01
                gross = ((current_price - ps["entry_price"]) if is_long
                         else (ps["entry_price"] - current_price))
                unrealized_pnl = round(gross * ps["position_amount"] * CONTRACT_SIZE, 2)

            # 今日盈亏 & 今日交易次数
            today_pnl    = 0.0
            today_trades = 0
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                conn2 = sqlite3.connect(DB_PATH)
                row2  = conn2.execute(
                    "SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM trade_history "
                    "WHERE user_id=? AND timestamp LIKE ? AND action='平仓'",
                    (user_id, today_str + "%")
                ).fetchone()
                conn2.close()
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

            await websocket.send_json({
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
            })
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
