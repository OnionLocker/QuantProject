"""
api/auth/crypto.py - OKX API Key 对称加密工具

使用 Fernet（AES-128-CBC + HMAC），密钥从环境变量 ENCRYPT_KEY 读取。
首次部署时运行：python3.11 -m api.auth.crypto  生成密钥并写入 .env
"""
import os
import base64
from cryptography.fernet import Fernet

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))

# ── 密钥加载（单例缓存，仅首次初始化）──────────────────────────────────────
_fernet_instance: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"))
    key = os.getenv("ENCRYPT_KEY")
    if not key:
        raise RuntimeError(
            "❌ 未找到 ENCRYPT_KEY。请运行 'python3.11 -m api.auth.crypto' 生成并写入 .env"
        )
    _fernet_instance = Fernet(key.encode())
    return _fernet_instance


def encrypt(plain: str) -> str:
    """加密明文字符串，返回 base64 密文"""
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt(cipher: str) -> str:
    """解密密文，返回明文字符串"""
    if not cipher:
        return ""
    return _get_fernet().decrypt(cipher.encode()).decode()


# ── 一键生成密钥 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    key = Fernet.generate_key().decode()
    env_path = os.path.join(project_root, ".env")

    # 追加写入 .env（若已有则提示）
    existing = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            existing = f.read()

    if "ENCRYPT_KEY" in existing:
        print("⚠️  .env 中已存在 ENCRYPT_KEY，未覆盖。")
    else:
        with open(env_path, "a") as f:
            f.write(f"\nENCRYPT_KEY={key}\n")
        # 安全：仅显示密钥前 8 位，避免终端历史泄露完整密钥
        print(f"✅ ENCRYPT_KEY 已写入 .env（前缀：{key[:8]}...）")
