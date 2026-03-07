"""
utils/notifier.py - Telegram 消息推送工具（支持全局配置和每用户独立配置）

用法：
  - 全局（单用户 / 后备）：send_telegram_msg("消息")
  - 每用户：make_notifier(token, chat_id) 返回绑定了凭证的发送函数
"""
import os
import requests
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
load_dotenv(os.path.join(project_root, '.env'))

# 全局后备凭证（来自 .env，兼容旧版单用户部署）
_GLOBAL_TOKEN   = os.getenv('TG_BOT_TOKEN', '').strip("'\" ")
_GLOBAL_CHAT_ID = os.getenv('TG_CHAT_ID',   '').strip("'\" ")

_TIMEOUT = 10   # 请求超时秒数


def _do_send(token: str, chat_id: str, message: str) -> bool:
    """底层发送函数，不依赖任何全局状态。"""
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_telegram_msg(message: str) -> bool:
    """使用全局 .env 配置发送消息（兼容旧版调用）。"""
    if not _GLOBAL_TOKEN or not _GLOBAL_CHAT_ID:
        print("⚠️ [Telegram] 未配置全局 Token/Chat ID，跳过推送")
        return False
    ok = _do_send(_GLOBAL_TOKEN, _GLOBAL_CHAT_ID, message)
    if not ok:
        print("❌ [Telegram] 全局推送失败")
    return ok


def make_notifier(token: str, chat_id: str):
    """
    工厂函数：返回一个绑定了指定 token/chat_id 的发送函数。
    若 token/chat_id 为空，返回 None（调用方应跳过推送）。

    用法：
        notify = make_notifier(user_token, user_chat_id)
        if notify:
            notify("消息内容")
    """
    if not token or not chat_id:
        return None

    def _send(message: str) -> bool:
        ok = _do_send(token, chat_id, message)
        if not ok:
            print(f"❌ [Telegram] 推送失败 (chat_id={chat_id[:6]}...)")
        return ok

    return _send


def test_notify(token: str, chat_id: str) -> tuple[bool, str]:
    """
    测试一个 token+chat_id 组合是否可用。
    返回 (success: bool, message: str)
    """
    if not token or not chat_id:
        return False, "Token 或 Chat ID 不能为空"
    ok = _do_send(token, chat_id,
                  "🤖 <b>QuantBot 通知测试</b>\n✅ Telegram 配置成功！")
    if ok:
        return True, "发送成功"
    return False, "发送失败，请检查 Token 和 Chat ID 是否正确"


# --- 本地测试 ---
if __name__ == "__main__":
    send_telegram_msg("🤖 <b>全局通知测试</b>\n✅ 配置正常！")
