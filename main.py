"""
main.py - ⚠️ 单用户遗留入口，已废弃

多用户版请使用：
  python3 -m uvicorn api.server:app --host 0.0.0.0 --port 8080

此文件保留仅作历史参考，请勿在生产环境使用。
"""
raise RuntimeError(
    "main.py 已废弃。请使用多用户版：\n"
    "  python3 -m uvicorn api.server:app --host 0.0.0.0 --port 8080\n"
    "详见 README.md"
)
