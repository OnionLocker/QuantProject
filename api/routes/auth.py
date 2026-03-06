"""
api/routes/auth.py - 注册 / 登录
"""
from fastapi import APIRouter, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Depends
from pydantic import BaseModel, constr
from passlib.context import CryptContext
from execution.db_handler import get_conn
from api.auth.jwt_handler import create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


class RegisterBody(BaseModel):
    username: constr(min_length=3, max_length=32)
    password: constr(min_length=6)


@router.post("/register", summary="注册账号")
def register(body: RegisterBody):
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE username=?", (body.username,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")
        hashed = pwd_ctx.hash(body.password)
        conn.execute(
            "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
            (body.username, hashed)
        )
        conn.commit()
        user_id = conn.execute(
            "SELECT id FROM users WHERE username=?", (body.username,)
        ).fetchone()["id"]
    finally:
        conn.close()
    token = create_access_token(user_id, body.username)
    return {"access_token": token, "token_type": "bearer", "username": body.username}


@router.post("/login", summary="登录")
def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, hashed_password FROM users WHERE username=?", (form.username,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not pwd_ctx.verify(form.password, row["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )
    token = create_access_token(row["id"], form.username)
    return {"access_token": token, "token_type": "bearer", "username": form.username}
