# QuantBot - OKX 量化交易系统

> 多用户 · 模块化 · 带 WebUI 的永续合约量化交易平台

## 功能

- **多用户系统**：注册/登录，每用户独立 Bot 线程、独立风控
- **OKX API Key 加密存储**（Fernet AES，明文不落盘）
- **策略注册表**：通过 `config.yaml` 切换策略，无需改代码
- **完整风控**：连亏熔断 + 日亏熔断 + 网络自动重试
- **Web 控制台**：React 前端，实时 WebSocket 状态推送
- **策略回测**：Web 触发，结果页面展示
- **Telegram 告警**：开平仓、熔断、异常全推送

## 技术栈

| 层 | 技术 |
|---|---|
| 交易所 | OKX (via ccxt) |
| 后端 | FastAPI + SQLite |
| 认证 | JWT + bcrypt |
| 前端 | React + Vite |
| 部署 | Nginx + Let's Encrypt |

## 目录结构

```
QuantProject/
├── main.py                  # 单用户版入口（兼容保留）
├── config.yaml              # 全局配置（策略/风控/品种）
├── .env.example             # 环境变量模板（复制为 .env 后填写）
├── deploy.sh                # 一键部署脚本
├── startbot.sh / stopbot.sh # 手动启停脚本
├── api/                     # FastAPI 后端
│   ├── server.py            # 应用入口
│   ├── auth/                # JWT + Fernet 加密
│   └── routes/              # auth / keys / bot / data
├── core/
│   ├── okx_client.py        # OKX 连接（含网络重试）
│   └── user_bot/
│       ├── manager.py       # 多用户 Bot 注册表
│       └── runner.py        # 每用户 Bot 主循环
├── strategy/
│   ├── base.py              # 策略基类
│   ├── registry.py          # 策略注册表
│   └── price_action_v2.py   # PA_V2 策略（见下方说明）
├── risk/
│   └── risk_manager.py      # 连亏熔断 + 日亏熔断
├── backtest/
│   └── engine.py            # 回测引擎
├── execution/
│   ├── order_manager.py     # OKX 订单执行
│   └── db_handler.py        # SQLite 数据库
├── data/                    # 行情数据缓存
├── utils/                   # 工具函数
├── frontend/                # React 前端
│   └── src/pages/
│       ├── AuthPage.jsx     # 登录/注册
│       ├── Dashboard.jsx    # 控制台（启停 Bot、实时状态）
│       ├── TradesPage.jsx   # 历史交易记录
│       ├── BalancePage.jsx  # 资产概览
│       ├── BacktestPage.jsx # 回测触发与结果展示
│       └── SettingsPage.jsx # OKX API Key 配置
├── nginx/
│   └── quantbot.conf        # Nginx 反向代理 + HTTPS 配置
└── systemd/                 # systemd 服务配置文件
```

> **`trade_state.json`**：运行时自动生成，记录当前持仓状态（仓位方向、入场价、止损止盈订单 ID 等）。无需手动创建，不需要提交到版本库。

## 当前策略：PA_V2 市场结构与假突破

策略文件：`strategy/price_action_v2.py`

基于 **1H K 线**（模拟 4H 级别结构），包含两个交易模型：

### 模型 A：顺势 BOS 回踩
- 检测价格突破近期摆动高/低点（BOS，结构突破）
- 在回踩确认位出现**吞没形态**时入场
- 做多条件：上升趋势 + 回踩前高附近 + 看涨吞没
- 做空条件：下降趋势 + 回踩前低附近 + 看跌吞没

### 模型 B：逆势假突破（Fakeout / 扫流动性）
- 检测价格刺破前高/前低后迅速收回（假突破）
- 结合**波动率过滤**（突破 K 线振幅需超近期均值 1.2 倍）
- 向上假突破 → 做空；向下假突破 → 做多

**风险管理**：ATR 动态缓冲带 + 固定 RR（TP1=1R，TP2=2.5R）

## 快速部署（VPS）

### 1. 克隆项目

```bash
git clone https://github.com/OnionLocker/QuantProject.git
cd QuantProject
```

### 2. 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env，填写 Telegram Bot Token 和 Chat ID
# OKX API Key 通过网页界面填写，不需要写在 .env 里
```

`.env` 最小配置：
```
TG_BOT_TOKEN=your_telegram_bot_token
TG_CHAT_ID=your_chat_id
```

`ENCRYPT_KEY` 和 `JWT_SECRET` 由 `deploy.sh` 自动生成，无需手填。

### 3. 一键部署

```bash
bash deploy.sh
```

脚本会自动：
- 安装 Python 依赖
- 生成 `ENCRYPT_KEY`（API Key 加密密钥）和 `JWT_SECRET`
- 构建 React 前端
- 启动 FastAPI 服务（端口 8080）

### 4. 配置 Nginx + HTTPS（可选但推荐）

```bash
# 安装 certbot
apt install certbot python3-certbot-nginx

# 复制 nginx 配置（修改 your-domain.com）
cp nginx/quantbot.conf /etc/nginx/sites-available/quantbot.conf
ln -s /etc/nginx/sites-available/quantbot.conf /etc/nginx/sites-enabled/

# 申请证书
certbot --nginx -d your-domain.com

# 重启 nginx
nginx -s reload
```

### 5. 使用

1. 浏览器访问 `http://YOUR_VPS_IP:8080`
2. 注册账号
3. 进入「⚙️ 设置」填写 OKX API Key
4. 回到「🏠 控制台」点击「启动 Bot」

## 本地开发

```bash
# 后端
pip install -r requirements.txt
python3 -m uvicorn api.server:app --reload --port 8080

# 前端（另开终端）
cd frontend
npm install
npm run dev   # 默认 5173 端口，自动代理到 8080
```

## 添加新策略

1. 在 `strategy/` 新建策略文件，继承 `BaseStrategy`
2. 在 `strategy/registry.py` 的 `_REGISTRY` 中注册
3. 修改 `config.yaml` 中的 `strategy.name` 切换

## 风控配置

编辑 `config.yaml`：

```yaml
risk:
  risk_per_trade_pct: 0.01   # 单笔最大亏损 1%
  max_consecutive_losses: 3  # 连亏 3 次熔断
  daily_loss_limit_pct: 0.05 # 日亏 5% 熔断
```

熔断后在 Web 控制台点击「恢复熔断」恢复交易。
