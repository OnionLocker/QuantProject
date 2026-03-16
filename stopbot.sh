#!/bin/bash
# =============================================================================
# stopbot.sh - QuantBot 一键停止脚本
# =============================================================================
cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }

echo "🛑 正在停止 QuantBot..."

STOPPED=0

# 优先通过 systemd 停止
if command -v systemctl &>/dev/null && systemctl is-active --quiet quantbot 2>/dev/null; then
  systemctl stop quantbot
  ok "systemd 服务已停止"
  STOPPED=1
fi

# 兜底：直接 kill uvicorn 进程（兼容系统 Python / venv 启动）
if pgrep -f "uvicorn api.server:app" > /dev/null; then
  pkill -f "uvicorn api.server:app"
  sleep 1
  ok "uvicorn 进程已停止"
  STOPPED=1
fi

# 再兜底：匹配项目目录下的 uvicorn 命令行
if pgrep -f "/root/QuantProject/.*/uvicorn api.server:app" > /dev/null; then
  pkill -f "/root/QuantProject/.*/uvicorn api.server:app"
  sleep 1
  ok "项目 uvicorn 进程已停止"
  STOPPED=1
fi

if [ $STOPPED -eq 0 ]; then
  warn "未检测到正在运行的 QuantBot 进程"
fi
