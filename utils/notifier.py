"""
utils/notifier.py - 消息推送工具（支持 Telegram + Webhook）

用法：
  - Telegram 全局：send_telegram_msg("消息")
  - Telegram 每用户：make_notifier(token, chat_id)
  - Webhook：make_webhook_notifier(url, headers) → 向指定 URL POST JSON
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


def _do_send(token: str, chat_id: str, message: str) -> tuple[bool, str]:
    """底层发送函数，不依赖任何全局状态。返回 (ok, detail)。"""
    if not token or not chat_id:
        return False, "Token 或 Chat ID 为空"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "ok"
        try:
            return False, resp.text[:500]
        except Exception:
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


def send_telegram_msg(message: str) -> bool:
    """使用全局 .env 配置发送消息（兼容旧版调用）。"""
    if not _GLOBAL_TOKEN or not _GLOBAL_CHAT_ID:
        print("⚠️ [Telegram] 未配置全局 Token/Chat ID，跳过推送")
        return False
    ok, detail = _do_send(_GLOBAL_TOKEN, _GLOBAL_CHAT_ID, message)
    if not ok:
        print(f"❌ [Telegram] 全局推送失败: {detail}")
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
        ok, detail = _do_send(token, chat_id, message)
        if not ok:
            print(f"❌ [Telegram] 推送失败 (chat_id={chat_id[:6]}...): {detail}")
        return ok

    return _send


# ── Webhook 通知渠道 ──────────────────────────────────────────────────────────

def _do_webhook_send(url: str, message: str, headers: dict = None) -> bool:
    """向指定 Webhook URL 发送 JSON 消息。"""
    if not url:
        return False
    payload = {
        "text": message,
        "content": message,   # 兼容 Discord / 企业微信等格式
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            headers=headers or {"Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def make_webhook_notifier(url: str, headers: dict = None):
    """
    工厂函数：返回一个绑定了 Webhook URL 的发送函数。

    用法：
        notify = make_webhook_notifier("https://hooks.slack.com/...")
        notify("消息内容")
    """
    if not url:
        return None

    def _send(message: str) -> bool:
        ok = _do_webhook_send(url, message, headers)
        if not ok:
            print(f"❌ [Webhook] 推送失败 (url={url[:30]}...)")
        return ok

    return _send


def make_multi_notifier(*notifiers):
    """
    组合多个通知器，按顺序依次发送（任一成功即视为成功）。

    用法：
        tg = make_notifier(token, chat_id)
        wh = make_webhook_notifier(url)
        notify = make_multi_notifier(tg, wh)
        notify("消息")
    """
    active = [n for n in notifiers if n is not None]
    if not active:
        return None

    def _send(message: str) -> bool:
        return any(n(message) for n in active)

    return _send


def test_notify(token: str, chat_id: str) -> tuple[bool, str]:
    """
    测试一个 token+chat_id 组合是否可用。
    返回 (success: bool, message: str)
    """
    if not token or not chat_id:
        return False, "Token 或 Chat ID 不能为空"
    ok, detail = _do_send(token, chat_id,
                  "🤖 <b>QuantBot 通知测试</b>\n✅ Telegram 配置成功！")
    if ok:
        return True, "发送成功"
    return False, f"发送失败：{detail}"


def test_webhook(url: str) -> tuple[bool, str]:
    """测试 Webhook URL 是否可用。"""
    if not url:
        return False, "Webhook URL 不能为空"
    ok = _do_webhook_send(url, "🤖 QuantBot Webhook 通知测试\n✅ 配置成功！")
    if ok:
        return True, "发送成功"
    return False, "发送失败，请检查 Webhook URL 是否正确"


# --- 本地测试 ---
if __name__ == "__main__":
    send_telegram_msg("🤖 <b>全局通知测试</b>\n✅ 配置正常！")
