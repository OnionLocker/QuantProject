import logging
import os
from datetime import datetime

# 1. 精准定位项目大本营
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

# 2. 拼接 tradelog 文件夹路径，如果不存在则自动创建
log_dir = os.path.join(project_root, 'tradelog')
os.makedirs(log_dir, exist_ok=True)  # exist_ok=True 是关键，存在就不管，不存在就新建

# 3. 动态获取今天的日期，拼接出类似 20260228_trade_log.log 的文件名
current_date = datetime.now().strftime('%Y%m%d')
log_filename = f"{current_date}_trade_log.log"
log_filepath = os.path.join(log_dir, log_filename)

# 4. 初始化全局机器人专属 Logger
bot_logger = logging.getLogger("QuantBot")
bot_logger.setLevel(logging.INFO)

# 避免重复绑定导致日志打印多次
if not bot_logger.handlers:
    # 定义日志长什么样
    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Handler A: 往终端屏幕输出 (让你用 tail -f 时能看到)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    bot_logger.addHandler(console_handler)

    # Handler B: 往当天的专属日志文件里写 (支持中文的 utf-8 编码)
    file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
    file_handler.setFormatter(formatter)
    bot_logger.addHandler(file_handler)