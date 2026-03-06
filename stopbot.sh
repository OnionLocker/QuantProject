#!/bin/bash

echo "🛑 正在准备停止 V2 量化机器人..."

# 检查有没有正在运行的进程
if pgrep -f "python -u main.py" > /dev/null
then
    # 精准刺杀对应的进程
    pkill -f "python -u main.py"
    echo "✅ 机器人已成功停止休眠！"
else
    echo "🤷‍♂️ 当前没有发现正在运行的机器人进程，无需停止。"
fi