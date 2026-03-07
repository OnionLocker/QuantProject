#!/bin/bash
# =============================================================================
# startbot.sh - QuantBot 一键启动脚本
# 用法：bash startbot.sh
#
# 启动逻辑：
#   1. 优先使用 systemd（VPS 推荐，崩溃自动重启）
#   2. 若无 systemd，降级为 nohup 后台启动
#   3. 日志输出到 logs/server.log
# =============================================================================
set -e
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

# ── 颜色 ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
err()  { echo -e "${RED}❌ $*${NC}"; }

echo "============================================"
echo "  🚀 QuantBot 启动脚本"
echo "============================================"

# ── 1. 环境检查 ──────────────────────────────────────────────────────────────
# 检查 .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
  err ".env 文件不存在！请先运行 bash deploy.sh 完成首次部署"
  exit 1
fi
ok ".env 配置文件存在"

# 检查前端构建产物
if [ ! -d "$PROJECT_DIR/frontend/dist" ]; then
  warn "frontend/dist 不存在，前端页面无法访问"
  warn "请先运行 bash deploy.sh 构建前端"
fi

# 检查 Python
if command -v python3.11 &>/dev/null; then
  PYTHON=python3.11
elif command -v python3 &>/dev/null; then
  PYTHON=python3
else
  err "未找到 Python3，请先安装"
  exit 1
fi
ok "Python: $($PYTHON --version)"

# 确保 logs 目录存在
mkdir -p "$PROJECT_DIR/logs"

# ── 2. 启动服务 ──────────────────────────────────────────────────────────────

# ── 模式 A：systemd（首选）──────────────────────────────────────────────────
if command -v systemctl &>/dev/null && [ -f /etc/systemd/system/quantbot.service ]; then
  if systemctl is-active --quiet quantbot; then
    warn "quantbot 服务已在运行中"
    echo ""
    echo "  如需重启请执行：systemctl restart quantbot"
    echo "  查看日志请执行：journalctl -u quantbot -f"
  else
    systemctl start quantbot
    sleep 2
    if systemctl is-active --quiet quantbot; then
      ok "systemd 服务启动成功"
    else
      err "systemd 启动失败，查看详情："
      journalctl -u quantbot --no-pager -n 20
      exit 1
    fi
  fi

# ── 模式 B：nohup 降级（无 systemd 或未安装 service）──────────────────────
else
  if pgrep -f "uvicorn api.server:app" > /dev/null; then
    warn "QuantBot 已在后台运行（nohup 模式）"
    echo ""
    echo "  如需重启请执行：bash stopbot.sh && bash startbot.sh"
    echo "  查看日志请执行：tail -f logs/server.log"
  else
    echo "⚙️  使用 nohup 模式启动（未检测到 systemd service）..."
    nohup $PYTHON -m uvicorn api.server:app \
      --host 0.0.0.0 \
      --port 8080 \
      --workers 1 \
      --log-level warning \
      >> "$PROJECT_DIR/logs/server.log" 2>&1 &

    sleep 3

    if pgrep -f "uvicorn api.server:app" > /dev/null; then
      ok "nohup 启动成功（PID: $(pgrep -f 'uvicorn api.server:app')）"
    else
      err "启动失败，查看日志："
      tail -20 "$PROJECT_DIR/logs/server.log"
      exit 1
    fi
  fi
fi

# ── 3. 验证服务可访问 ─────────────────────────────────────────────────────────
echo ""
echo "⏳ 等待服务就绪..."
sleep 2
HEALTH=$(curl -s --max-time 5 http://localhost:8080/api/health 2>/dev/null || echo "")
if echo "$HEALTH" | grep -q '"ok"'; then
  ok "API 健康检查通过"
else
  warn "API 健康检查未响应（可能仍在初始化，稍后再试）"
fi

# ── 4. 显示访问信息 ───────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -s --max-time 4 ifconfig.me 2>/dev/null \
            || curl -s --max-time 4 icanhazip.com 2>/dev/null \
            || echo "YOUR_VPS_IP")

echo ""
echo "============================================"
ok "QuantBot 已启动！"
echo ""
echo "  🌐 前端访问地址：http://${PUBLIC_IP}:8080"
echo "       或（本地）：http://localhost:8080"
echo ""
echo "  📋 查看运行日志："
if command -v systemctl &>/dev/null && systemctl is-active --quiet quantbot 2>/dev/null; then
  echo "       journalctl -u quantbot -f"
else
  echo "       tail -f logs/server.log"
fi
echo ""
echo "  🛑 停止服务：bash stopbot.sh"
echo "============================================"
