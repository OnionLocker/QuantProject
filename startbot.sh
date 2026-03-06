#!/bin/bash

# 自动切换到脚本所在的目录，确保读取 .env 和 log 文件的路径绝对正确
cd "$(dirname "$0")"

# 检查是否已经有 main.py 在后台运行
if pgrep -f "python -u main.py" > /dev/null
then
    echo "⚠️ 警告：检测到机器人已经在后台运行中！"
    echo "💡 如果你想重启，请先执行 ./stopbot.sh"
else
    echo "🚀 正在后台启动 V2 量化机器人..."
    # 核心启动指令，依然是之前那套防挂断的黑洞流写法
    nohup python -u main.py > /dev/null 2>&1 &
    
    # 稍等 1 秒，确认进程是否成功挂起
    sleep 1
    if pgrep -f "python -u main.py" > /dev/null
    then
        echo "✅ 启动成功！机器人已在后台稳如泰山地运行。"
        echo "👀 实时查看运行日志请敲击命令: tail -f trading_bot.log"
    else
        echo "❌ 启动失败，请检查 main.py 是否有报错。"
    fi
fi