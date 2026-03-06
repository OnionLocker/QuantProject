import os
import requests
from dotenv import load_dotenv

# 找准密码本的位置
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
env_path = os.path.join(project_root, '.env')

load_dotenv(env_path)

# 从密码本里把 Telegram 的配置拿出来
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '').strip("'\" ")
TG_CHAT_ID = os.getenv('TG_CHAT_ID', '').strip("'\" ")

def send_telegram_msg(message):
    """
    发送 Telegram 消息的专属工具
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 警告：未找到 Telegram 的 Token 或 Chat ID，无法发送消息。")
        return False
    
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML" # 支持加粗等简单排版
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("📣 Telegram 消息推送成功！")
            return True
        else:
            print(f"❌ Telegram 发送失败，错误代码: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Telegram 网络请求报错: {e}")
        return False

# --- 测试对讲机 ---
if __name__ == "__main__":
    test_msg = "🤖 <b>报告老板！</b>\n您的量化交易机器人 Telegram 通知测试成功！🎉"
    send_telegram_msg(test_msg)