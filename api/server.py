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

from api.routes.auth import router as auth_router
from api.routes.keys import router as keys_router
from api.routes.bot  import router as bot_router
from api.routes.data import router as data_router
from api.auth.jwt_handler import get_current_user
from core.user_bot import manager as bot_mgr
from execution.db_handler import DB_PATH
import sqlite3

app = FastAPI(title="QuantBot", version="3.0-multiuser")

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

            await websocket.send_json({
                "ts":             datetime.now().isoformat(),
                "running":        bs.get("running", False),
                "fused":          bs.get("fused", False),
                "position_side":  ps.get("position_side"),
                "position_amount":ps.get("position_amount", 0),
                "entry_price":    ps.get("entry_price", 0),
                "active_sl":      ps.get("active_sl", 0),
                "active_tp1":     ps.get("active_tp1", 0),
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
