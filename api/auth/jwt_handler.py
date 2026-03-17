"""
api/auth/jwt_handler.py - JWT 签发与校验
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
load_dotenv(os.path.join(project_root, ".env"))

SECRET_KEY: str = os.getenv("JWT_SECRET", "change-me-in-production-please")  # type: ignore[assignment]
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7   # 7 天

# 安全检查：启动时验证密钥强度
if len(SECRET_KEY) < 32 or SECRET_KEY == "change-me-in-production-please":
    import logging as _logging
    _logging.getLogger("QuantBot.security").warning(
        "⚠️  JWT_SECRET 未设置或强度不足（建议至少 32 字符随机字符串）。"
        "运行: python3 -c \"import secrets; print(secrets.token_hex(32))\" 生成。"
    )

oauth2_scheme: OAuth2PasswordBearer = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def create_access_token(user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(token: str = Depends(oauth2_scheme)) -> Dict[str, Any]:
    """FastAPI 依赖注入：从 Bearer token 解析当前用户"""
    payload = decode_token(token)
    return {"id": int(payload["sub"]), "username": payload["username"]}
