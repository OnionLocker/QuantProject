#!/bin/bash
# deploy.sh - Ubuntu VPS 一键部署/更新脚本
# 首次部署：bash deploy.sh
# 更新代码后重新部署：git pull && bash deploy.sh
set -e

echo "========================================"
echo "  QuantBot 部署脚本 (Ubuntu)"
echo "  首次部署：bash deploy.sh"
echo "  更新代码后重新部署：git pull && bash deploy.sh"
echo "========================================"

# ── 0. 检测 Python（优先用 python3.11，其次 python3）────────────────────────
if command -v python3.11 &>/dev/null; then
  PYTHON=python3.11
  PIP="python3.11 -m pip"
elif command -v python3 &>/dev/null; then
  PYTHON=python3
  PIP="python3 -m pip"
else
  echo "❌ 未找到 Python3，请先安装：sudo apt install python3 python3-pip"
  exit 1
fi
echo "✅ Python: $($PYTHON --version)"

# ── 1. 确保必要目录存在 ───────────────────────────────────────────────────────
mkdir -p logs data/cache
echo "✅ 目录已就绪（logs/ data/cache/）"

# ── 2. 安装 Python 依赖 ────────────────────────────────────────────────────────
echo "📦 安装 Python 依赖..."
$PIP install -r requirements.txt -q --break-system-packages 2>/dev/null \
  || $PIP install -r requirements.txt -q

# ── 3. 首次部署：生成加密密钥 ──────────────────────────────────────────────────
if [ ! -f .env ]; then
  echo "⚠️  未找到 .env，正在从模板创建..."
  cp .env.example .env
  echo ""
  echo "════════════════════════════════════════"
  echo "  ⚠️  请先编辑 .env 填写 Telegram 配置！"
  echo "  命令：nano .env"
  echo "════════════════════════════════════════"
  read -p "填好后按回车继续..."
fi

if ! grep -q "^ENCRYPT_KEY=" .env 2>/dev/null; then
  echo "🔑 生成加密密钥..."
  $PYTHON -m api.auth.crypto
else
  echo "✅ ENCRYPT_KEY 已存在"
fi

if ! grep -q "^JWT_SECRET=" .env 2>/dev/null; then
  JWT_SECRET=$($PYTHON -c "import secrets; print(secrets.token_hex(32))")
  echo "JWT_SECRET=${JWT_SECRET}" >> .env
  echo "🔐 JWT_SECRET 已写入 .env"
else
  echo "✅ JWT_SECRET 已存在"
fi

# Fix: 限制 .env 文件权限，仅当前用户可读，防止其他用户窃取密钥
chmod 600 .env
echo "🔒 .env 权限已设为 600（仅当前用户可读）"

# ── 4. 检测 Node.js ────────────────────────────────────────────────────────────
if ! command -v node &>/dev/null; then
  echo "❌ 未找到 Node.js，请先安装："
  echo "   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
  echo "   sudo apt install -y nodejs"
  exit 1
fi
echo "✅ Node: $(node --version)"

# ── 5. 构建前端 ────────────────────────────────────────────────────────────────
echo "🔨 构建前端..."
cd frontend
npm install -q
npm run build
cd ..
echo "✅ 前端构建完成 → frontend/dist/"

# ── 6. 停止旧进程 ─────────────────────────────────────────────────────────────
echo "🛑 停止旧服务（如有）..."
systemctl stop quantbot 2>/dev/null || pkill -f "uvicorn api.server:app" 2>/dev/null || true
sleep 2

# ── 7. 安装/更新 systemd service（推荐，VPS 重启自动恢复）─────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="$PROJECT_DIR/systemd/quantbot.service"
SYSTEMD_FILE="/etc/systemd/system/quantbot.service"

if [ -f "$SERVICE_FILE" ] && command -v systemctl &>/dev/null; then
  # 替换 WorkingDirectory 为实际路径
  sed "s|/root/QuantProject|$PROJECT_DIR|g" "$SERVICE_FILE" > "$SYSTEMD_FILE"
  # 替换 ExecStart 中的 python3 为实际路径
  sed -i "s|/usr/bin/python3|$(which $PYTHON)|g" "$SYSTEMD_FILE"
  systemctl daemon-reload
  systemctl enable quantbot
  systemctl start quantbot
  echo "✅ systemd service 已安装并启动"

  # 安装数据库备份定时器
  BACKUP_SERVICE="$PROJECT_DIR/systemd/quantbot-backup.service"
  BACKUP_TIMER="$PROJECT_DIR/systemd/quantbot-backup.timer"
  if [ -f "$BACKUP_SERVICE" ] && [ -f "$BACKUP_TIMER" ]; then
    sed "s|/root/QuantProject|$PROJECT_DIR|g" "$BACKUP_SERVICE" > /etc/systemd/system/quantbot-backup.service
    sed -i "s|/usr/bin/python3|$(which $PYTHON)|g" /etc/systemd/system/quantbot-backup.service
    cp "$BACKUP_TIMER" /etc/systemd/system/quantbot-backup.timer
    systemctl daemon-reload
    systemctl enable quantbot-backup.timer
    systemctl start quantbot-backup.timer
    echo "✅ 数据库备份定时器已安装（每天 00:05 执行）"
  fi
else
  # 降级到 nohup
  echo "⚠️  未检测到 systemd，使用 nohup 启动..."
  nohup $PYTHON -m uvicorn api.server:app \
    --host 0.0.0.0 --port 8080 --workers 1 --log-level info \
    > logs/server.log 2>&1 &
fi

sleep 3

# ── 8. 验证启动成功 ──────────────────────────────────────────────────────────
if systemctl is-active --quiet quantbot 2>/dev/null || pgrep -f "uvicorn api.server:app" > /dev/null; then
  PUBLIC_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "YOUR_VPS_IP")
  echo ""
  echo "========================================"
  echo "✅ 首次部署完成！"
  echo ""
  echo "   🌐 访问地址：http://${PUBLIC_IP}:8080"
  echo ""
  echo "   ─── 日常运维命令 ────────────────────"
  echo "   🚀 启动：bash startbot.sh"
  echo "   🛑 停止：bash stopbot.sh"
  echo "   📋 日志：journalctl -u quantbot -f"
  echo "          或：tail -f logs/server.log"
  echo "   🔄 重启：systemctl restart quantbot"
  echo "   💾 备份：python3 scripts/backup_db.py"
  echo "          备份文件位置：backups/"
  echo "========================================"
else
  echo ""
  echo "❌ 服务启动失败，查看错误："
  tail -20 logs/server.log
  exit 1
fi
