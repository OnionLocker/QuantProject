# QuantBot - OKX 量化交易系统

> 多用户 · 多策略自动切换 · 带 WebUI 的永续合约量化交易平台

---

## 📦 版本变更日志

| 版本 | 内容 |
|------|------|
| **V3.5** | 回测引擎大升级：AUTO 模式回测、追踪止损、时间止损、动态仓位、策略切换明细、per-strategy 统计；前端回测页高级功能面板 |
| **V3.0** | 前端可视化 + 市场数据 API：Dashboard 市场情绪面板、5 个 `/api/market/*` 端点、WebSocket 推送 regime 详情 |
| **V2.5** | 高级风控：Trailing Stop、时间止损、动态仓位、策略绩效追踪表、DB migration v3 |
| **V2.0** | 资金费率 + OI 链上数据增强、AI 情绪分析客户端（OpenAI 兼容）、选择器多维评分 |
| **V1.5** | 策略参数优化（BTC/USDT 1h）、WAIT 观望状态、波动率快速通道、策略切换过渡期 |
| **V1.0** | 基础交易系统：6 策略 + AUTO 选择器、风控熔断、WebUI、回测、Telegram 通知 |

---

## ⚙️ AI 运维指南

> **[Instruction for AI Models]**
> 本文档是系统的完整运维手册，面向 AI 助手阅读。当你被要求运维、排障、修改或扩展本系统时，请先通读此文档理解整体架构，再结合具体文件进行操作。

---

## 1. 系统架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                          用户浏览器                                  │
│  React 19 + Vite 7 前端 (frontend/dist/ 由 FastAPI 托管)           │
│  页面：登录 → 控制台 → 交易记录 → 资产 → 回测 → 设置              │
│  实时通信：WebSocket (/ws/status?token=<jwt>)                       │
└────────────┬───────────────┬────────────────────────────────────────┘
             │ HTTP REST     │ WebSocket
             ▼               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     FastAPI 后端 (api/server.py)                    │
│  端口 8080 | uvicorn 单 worker                                      │
│  路由：/api/auth  /api/keys  /api/bot  /api/data                   │
│        /api/notify  /api/user-config  /api/market  /api/health     │
│  认证：JWT (Bearer Token) | 加密：Fernet AES-256                   │
│  CORS：config.yaml → api.cors_origins                              │
└────────┬──────────────────────────────────────────────────────────┘
         │
    ┌────┴────────────────────────────────────────────┐
    ▼                                                  ▼
┌──────────────────────┐    ┌──────────────────────────────────────┐
│  Bot Manager         │    │  SQLite 数据库 (trading_data.db)     │
│  (core/user_bot/     │    │  WAL 模式 | 线程局部连接缓存         │
│   manager.py)        │    │  表：users, user_api_keys,           │
│                      │    │       user_config, trade_history,     │
│  每用户独立线程      │    │       daily_balance, bot_state,       │
│  + RiskManager       │    │       risk_state, user_settings,      │
│  + Watchdog 守护     │    │       backtest_history,               │
│  (60s 检测+指数退避) │    │       schema_version,                 │
│                      │    │       strategy_performance (V2.5)     │
└──────────┬───────────┘    └──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│             Bot Runner (core/user_bot/runner.py)                  │
│  主循环：拉K线 → 策略信号 → 风控检查 → 下单 → 监控持仓 → 通知  │
│  V2.5：Trailing Stop + 时间止损 + 动态仓位 + 策略绩效追踪       │
│  网络分级：rate_limit / maintenance / auth_error / network       │
│  持仓状态：bot_state 表 (JSON)                                   │
│  被动平仓：优先拉交易所真实成交记录                              │
│  订单对账：启动时核对 SL/TP 条件单                               │
└──────────────┬───────────────────────────────────────────────────┘
               │
    ┌──────────┼──────────────┬──────────────────┐
    ▼          ▼              ▼                    ▼
┌────────┐ ┌──────────┐ ┌──────────────────┐ ┌──────────────────┐
│ OKX    │ │ Strategy │ │ Risk Manager     │ │ Market Data      │
│ (ccxt) │ │ Layer    │ │ (risk/           │ │ (V2.0)           │
│        │ │ 6 策略   │ │  risk_manager.py)│ │ 资金费率 + OI    │
│ 实/模  │ │ + AUTO   │ │ 连亏熔断         │ │ (market_extra.py)│
│ 盘切换 │ │ 选择器   │ │ 日亏熔断         │ │ AI 情绪分析      │
│        │ │          │ │ 仓位计算         │ │ (ai_client.py)   │
└────────┘ └──────────┘ └──────────────────┘ └──────────────────┘
```

### 1.1 进程模型

```
uvicorn (主进程，端口 8080)
├── FastAPI 异步事件循环（HTTP + WebSocket）
├── Bot 线程池（每用户 1 个 daemon Thread）
│   ├── Bot-alice (runner.run_user_bot)
│   ├── Bot-bob   (runner.run_user_bot)
│   └── ...
├── Watchdog 线程（BotWatchdog，60s 轮询，首次 start_bot 时启动）
└── 回测后台线程（每用户最多 1 个并发）
```

- **单进程单 worker**：uvicorn `--workers 1`（SQLite 不支持多进程写）
- **并发模型**：FastAPI async + 同步 Bot 线程 + `asyncio.to_thread()` 桥接
- **WebSocket 数据刷新**：每 5 秒推送一次（server.py `asyncio.sleep(5)`）

### 1.2 技术栈

| 层 | 技术 |
|---|---|
| 交易所 | OKX（via ccxt） |
| 后端 | Python 3.11+ / FastAPI / SQLite（WAL 并发） |
| 认证 | JWT（HS256）+ bcrypt + Fernet AES-256 |
| 前端 | React 19 + Vite 7 + Recharts + Lightweight Charts |
| 部署 | systemd + Nginx + Let's Encrypt |
| 定时任务 | systemd timer（新闻抓取 / 数据库备份） |

---

## 2. 目录结构

```
QuantProject/
├── config.yaml                 # 全局配置（策略/风控/品种/选择器参数）⚡热更新
├── .env                        # 环境变量（ENCRYPT_KEY, JWT_SECRET, TG_*）🔒 chmod 600
├── .env.example                # 环境变量模板
├── trading_data.db             # SQLite 主数据库（运行时自动创建）
├── trade_state.json            # ⚠️ 已弃用遗留文件，多用户版不使用
├── deploy.sh                   # 一键部署脚本（Ubuntu VPS）
├── startbot.sh / stopbot.sh    # 手动启停脚本
│
├── api/                        # FastAPI 后端
│   ├── server.py               # 应用入口 + WebSocket + SPA 托管
│   ├── auth/
│   │   ├── jwt_handler.py      # JWT 签发/验证 + get_current_user 依赖
│   │   └── crypto.py           # Fernet 加解密（单例缓存）
│   └── routes/
│       ├── auth.py             # POST /api/auth/register, /api/auth/login
│       ├── keys.py             # POST /api/keys/save, GET /validate, /live-balance
│       ├── bot.py              # POST /api/bot/start, /stop, GET /status, POST /risk/resume
│       ├── data.py             # GET /api/data/trades, /balance, /strategies
│       │                       # POST /backtest/run, GET /backtest/result, /history
│       ├── market.py           # 🆕 V3.0: 市场数据 API（regime/funding/OI/sentiment/绩效）
│       ├── notify.py           # POST /api/notify/telegram/save, /test, /clear
│       └── user_config.py      # GET /api/user-config, POST /save, DELETE /reset
│
├── core/
│   ├── okx_client.py           # OKX ccxt 封装（网络重试 + 指数退避）
│   └── user_bot/
│       ├── manager.py          # 多用户 Bot 注册表 + Watchdog 守护 + selector 注册
│       └── runner.py           # 每用户 Bot 主循环（核心交易逻辑 + V2.5 高级风控）
│
├── strategy/
│   ├── base.py                 # 策略基类 BaseStrategy
│   ├── registry.py             # 策略注册表 _REGISTRY（热插拔）
│   ├── selector.py             # 🔄 市场状态判断 + 策略自动选择器 (AUTO)
│   ├── regime_detector.py      # 市场 regime 检测辅助
│   ├── pa_setups.py            # PA_5S：Price Action 五种形态
│   ├── adaptive.py             # ADAPTIVE：自适应混合
│   ├── trend_bull.py           # BULL：EMA 趋势跟踪（多头）
│   ├── trend_bear.py           # BEAR：EMA 趋势跟踪（空头）
│   ├── range_oscillator.py     # RANGE：布林带 + RSI 震荡
│   ├── big_candle.py           # BIG_CANDLE：大阳/大阴线突破
│   ├── price_action.py         # 早期 PA 策略（保留兼容）
│   └── STRATEGY_GUIDE.md       # 📖 策略开发规范（AI 必读）
│
├── risk/
│   └── risk_manager.py         # 连亏熔断 + 日亏熔断 + Fixed Fractional Sizing
│
├── backtest/
│   └── engine.py               # 回测引擎 V3.5（AUTO 模式 / Trailing Stop / 时间止损 / 动态仓位）
│
├── execution/
│   └── db_handler.py           # SQLite 数据库（连接池 + migration + CRUD）
│
├── news/
│   ├── news_fetcher.py         # 新闻抓取 + 情绪分析（keyword/AI/hybrid）
│   └── news_sources.yaml       # 新闻源配置（RSS/JSON API）
│
├── data/
│   ├── market_data.py          # 行情数据获取与缓存
│   ├── market_extra.py         # 🆕 V2.0: 资金费率 + OI 链上数据（OKX 公开 API）
│   └── *.csv                   # 回测用历史数据
│
├── utils/
│   ├── config_loader.py        # YAML 配置加载（线程安全热更新）
│   ├── logger.py               # 统一日志（全局 bot_logger + per-user logger）
│   ├── notifier.py             # Telegram + Webhook 通知（多渠道组合）
│   ├── ai_client.py            # 🆕 V2.0: AI 情绪分析客户端（OpenAI 兼容）
│   └── trade_state.py          # ⚠️ 已弃用，遗留单用户模块
│
├── scripts/
│   ├── backup_db.py            # 数据库备份（SQLite backup API + integrity_check）
│   ├── fetch_news.py           # 新闻手动抓取 / 状态检查
│   └── fix_database.py         # 数据库迁移修复
│
├── frontend/                   # React 前端（Vite）
│   └── src/
│       ├── App.jsx             # 路由 + 全局 ErrorBoundary + 401 拦截
│       ├── api.js              # Axios 封装（自动 Bearer Token）
│       └── pages/
│           ├── AuthPage.jsx    # 登录/注册
│           ├── Dashboard.jsx   # 控制台（启停Bot、实时WebSocket状态、浮动盈亏）
│           ├── TradesPage.jsx  # 历史交易记录（分页、统计）
│           ├── BalancePage.jsx # 资产概览（历史曲线 + 实时余额）
│           ├── BacktestPage.jsx# 回测触发与结果展示（K线图 + 权益曲线）
│           └── SettingsPage.jsx# OKX API Key + 策略/风控参数 + TG 通知配置
│
├── nginx/
│   └── quantbot.conf           # Nginx 反向代理 + HTTPS 配置
│
└── systemd/
    ├── quantbot.service        # 主服务
    ├── quantbot-backup.service # 数据库备份
    ├── quantbot-backup.timer   # 每日 00:05 自动备份
    ├── quantbot-news.service   # 新闻抓取
    └── quantbot-news.timer     # 每 30 分钟抓取新闻
```

---

## 3. 数据流详解

### 3.1 交易主循环（runner.py `run_user_bot()`）

```
每轮循环（默认 300 秒间隔）：
    │
    ├─ 1. get_config() → 热加载 config.yaml
    │
    ├─ 2. 拉取 K 线数据（ccxt.fetch_ohlcv）
    │     └── data/market_data.py 缓存层
    │
    ├─ 3. 策略信号生成
    │     ├─ AUTO 模式：selector.pick_strategy(df) → 动态切换策略
    │     └─ 固定模式：strategy.generate_signal(df) → ("BUY"/"SELL"/"HOLD", reason)
    │
    ├─ 4. 信号处理
    │     ├─ HOLD：跳过
    │     ├─ BUY/SELL + 空仓：
    │     │   ├── 风控前置检查（rm.check_order）
    │     │   ├── 仓位计算（rm.calculate_position_size）
    │     │   ├── 设置杠杆 → 市价开仓
    │     │   ├── 挂 SL/TP 条件单（_place_algo）
    │     │   ├── 记录交易（record_trade）
    │     │   ├── 保存状态（_save_state）
    │     │   └── Telegram 通知
    │     └─ BUY/SELL + 反向持仓（策略反转）：
    │         ├── 取消旧条件单 → 市价平仓 → 计算 PnL
    │         ├── 记录 + 风控 notify_trade_result
    │         └── 如果未熔断，立即按新方向开仓
    │
    ├─ 5. 持仓监控（已有持仓时）
    │     ├── 查询交易所实际持仓（_live_position_amount）
    │     │   └── 本地有仓但交易所为 0 → 被动平仓
    │     │       └── _fetch_passive_fill_price（优先真实成交 > 条件单 > 估算）
    │     ├── 浮动止盈管理（移动保本、部分止盈等）
    │     └── 余额快照（record_balance）
    │
    ├─ 6. 风控状态持久化（_save_risk_state）
    │
    └─ 7. sleep(check_interval) 等待下一轮
         └── 期间响应 stop_event.is_set() 退出
```

### 3.2 配置优先级（三级 fallback）

```
用户 Web 设置 (DB: user_config) > config.yaml 全局配置 > 代码硬编码默认值
```

`runner.py::_resolve_config(user_id)` 负责合并，使用 `is not None` 判断（允许用户配 0 值）。

支持的用户自定义字段：
- `symbol`（交易对）、`timeframe`（K线周期）、`leverage`（杠杆）
- `risk_pct`（单笔风险比例）、`strategy_name`、`strategy_params`
- `max_consecutive_losses`（连亏熔断次数）、`daily_loss_limit_pct`（日亏上限）、`max_trade_amount`（单笔金额上限）

### 3.3 WebSocket 实时数据流

```
前端 Dashboard → ws://host:8080/ws/status?token=<jwt>
                          │
                          ▼ asyncio.to_thread (线程池执行同步 IO)
                    ┌─────────────────────────────────────────┐
                    │ 1. bot_mgr.bot_status(uid) → 运行状态   │
                    │ 2. bot_state 表 → 持仓信息 (JSON)       │
                    │ 3. ccxt.fetch_ticker → 实时价格          │
                    │    └── 价格缓存 _price_cache (TTL 3s)   │
                    │ 4. 计算浮动盈亏 (unrealized_pnl)        │
                    │ 5. trade_history → 今日PnL + 交易次数   │
                    │ 6. 运行时长计算                          │
                    └─────────────────────────────────────────┘
                          │ 每 5 秒推送一次 JSON
                          ▼
                    前端状态更新（Dashboard 实时刷新）
```

---

## 4. 数据库表结构

数据库文件：`trading_data.db`（项目根目录，运行时自动创建）

| 表名 | 主键 | 用途 |
|------|------|------|
| `schema_version` | `id=1` | 数据库 migration 版本号 |
| `users` | `id` (自增) | 用户账号（username + hashed_password） |
| `user_api_keys` | `user_id` | OKX API Key（Fernet 加密存储） |
| `user_config` | `user_id` | 每用户个性化配置（策略/品种/杠杆/风控） |
| `user_settings` | `user_id` | 每用户 Telegram 配置（token/chat_id 加密） |
| `trade_history` | `id` (自增) | 历史交易记录（带 user_id） |
| `daily_balance` | `(user_id, date)` | 每日余额快照 |
| `bot_state` | `user_id` | 持仓状态 JSON（仓位方向、入场价、SL/TP 订单ID 等） |
| `risk_state` | `user_id` | 风控持久化（连亏次数、日初余额、熔断标志） |
| `backtest_history` | `id` (自增) | 回测历史（每用户最多 20 条，含完整结果 JSON） |
| `strategy_performance` | `id` (自增) | 🆕 V2.5: 策略绩效追踪（每笔交易按策略记录 PnL） |

### 4.1 `bot_state.value` JSON 结构

```json
{
  "position_side": "long" | "short" | null,
  "position_amount": 10,
  "entry_price": 65000.0,
  "active_sl": 64000.0,
  "active_tp1": 67000.0,
  "active_tp2": 0.0,
  "open_fee": 0.325,
  "margin_used": 216.67,
  "strategy_name": "BULL",
  "signal_reason": "🟢 EMA 多头排列 + ADX>25",
  "entry_time": "2026-03-10 14:30:00",
  "has_moved_to_breakeven": false,
  "has_taken_partial_profit": false,
  "exchange_order_ids": {
    "sl_order": "12345678",
    "tp_order": "12345679"
  }
}
```

### 4.2 数据库迁移

- 由 `db_handler.py::init_db()` 的 `_MIGRATIONS` 列表管理
- 每个迁移是 `(version, description, sql_list)` 三元组
- 新增迁移只需在列表尾部追加，启动时自动执行
- 当前版本：**v3**（V2.5 新增 `strategy_performance` 表）

---

## 5. API 接口清单

所有 API 前缀 `/api/`，需 JWT Bearer Token（除 auth 外）。

### 5.1 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册（username + password） |
| POST | `/api/auth/login` | 登录，返回 JWT |

### 5.2 OKX API Key

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/keys/save` | 保存 API Key（Fernet 加密入库） |
| GET | `/api/keys/status` | 检查是否已配置 + 模拟盘/实盘标记 |
| GET | `/api/keys/validate` | 验证 API Key 有效性（优先用当前模式，必要时探测另一模式） |
| GET | `/api/keys/live-balance` | 实时拉取 OKX 资产（优先 OKX 原始账户接口 totalEq，其次回退 ccxt balance） |
| DELETE | `/api/keys/reset` | 清除 API Key |

### 5.3 Bot 控制

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/bot/start` | 启动 Bot（可选 strategy_name 覆盖） |
| POST | `/api/bot/stop` | 停止 Bot |
| GET | `/api/bot/status` | Bot 运行状态 + 持仓信息 |
| POST | `/api/bot/risk/resume` | 手动恢复熔断（清零连亏 + 重启 Bot） |

### 5.4 数据 & 回测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/data/trades?limit=50` | 历史交易记录 |
| GET | `/api/data/balance?limit=90` | 每日余额历史 |
| GET | `/api/data/strategies` | 已注册策略列表 |
| GET | `/api/data/backtest/options` | 回测可选参数 |
| POST | `/api/data/backtest/run` | 触发回测（后台异步） |
| GET | `/api/data/backtest/result` | 获取回测结果（轮询） |
| GET | `/api/data/backtest/history` | 历史回测列表 |
| GET | `/api/data/backtest/history/{id}` | 单条历史详情 |

### 5.5 通知配置

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/notify/telegram/save` | 保存 TG Bot Token + Chat ID |
| GET | `/api/notify/telegram/status` | 是否已配置 |
| POST | `/api/notify/telegram/test` | 发送测试消息 |
| DELETE | `/api/notify/telegram/clear` | 清除 TG 配置 |

### 5.6 用户配置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/user-config` | 获取用户个性化配置 + 可选项 |
| POST | `/api/user-config/save` | 保存配置（部分字段更新） |
| DELETE | `/api/user-config/reset` | 重置为全局默认 |
| GET | `/api/user-config/strategy-params/{name}` | 策略参数元数据 |

### 5.7 市场数据（V3.0）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/market/regime` | 当前 regime 状态详情（技术面/资金费率/OI 评分） |
| GET | `/api/market/funding-rate?symbol=` | 资金费率实时 + 最近 24 期历史 |
| GET | `/api/market/open-interest?symbol=` | 持仓量 (OI) 实时 |
| GET | `/api/market/sentiment?symbol=` | 综合市场情绪面板（资金费率+OI+新闻+AI） |
| GET | `/api/market/strategy-performance` | 各策略绩效统计（胜率/盈亏比/总PnL） |

### 5.8 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| WS | `/ws/status?token=<jwt>` | WebSocket 实时状态推送 |

---

## 6. 策略体系

### 6.1 内置策略

| 策略名 | 类名 | 文件 | 适用行情 | 核心逻辑 |
|--------|------|------|----------|----------|
| `PA_5S` | PriceActionSetups | pa_setups.py | 通用 | Price Action 五种形态（吞没、Pin Bar 等） |
| `BULL` | TrendBullStrategy | trend_bull.py | 牛市趋势 | EMA 多头排列 + ADX 趋势确认 |
| `BEAR` | TrendBearStrategy | trend_bear.py | 熊市趋势 | EMA 空头排列 + ADX 趋势确认 |
| `RANGE` | RangeOscillatorStrategy | range_oscillator.py | 震荡 | 布林带通道 + RSI 超买超卖 |
| `BIG_CANDLE` | BigCandleStrategy | big_candle.py | 突破 | 大阳/大阴线 + 成交量确认 |
| `ADAPTIVE` | AdaptiveStrategy | adaptive.py | 自适应 | 多指标融合自动调参 |

### 6.2 AUTO 模式（策略自动选择器）

当 `config.yaml` 中 `strategy.name` 设为 `"AUTO"` 或用户在 Web 设置中选择 AUTO 时启用。

选择器流程（`strategy/selector.py::MarketRegimeSelector`）：

1. **技术面分析**（权重 `selector.tech_weight`，默认 1.0）：
   - ADX 趋势强度（>22 有趋势，<18 震荡，>40 强势突破）
   - EMA 14/40/120 排列方向
   - 布林带宽度（squeeze_pct < 3% 认为挤压）
   - ATR 突变检测（波动率快速通道）
2. **资金费率 + OI 信号**（V2.0，权重 `funding_weight=0.15` + `oi_weight=0.10`）：
   - OKX 公开 API 实时获取，内存缓存 + SQLite 持久化
   - 资金费率：正费率高 → 多头拥挤 → bearish，负费率高 → 空头拥挤 → bullish
   - OI 变化：上升 = 新资金入场，下降 = 去杠杆
3. **新闻 + AI 情绪分析**（权重 `selector.news_weight`，默认 0.0）：
   - 多源新闻抓取 + 关键词/AI 情绪评分
   - 支持 OpenAI / DeepSeek 等兼容接口
   - 动态权重：根据新闻新鲜度、数量、AI 可用性自动调整
4. **WAIT 观望状态**（V1.5）：
   - ADX 模糊区间 + ATR 突变无方向 → 不交易
   - 策略切换过渡期前 3 根 K 线半仓试探
5. **加权投票** → 判定 regime：`bull` / `bear` / `ranging` / `breakout` / `wait`
6. **防抖保护**：连续 `confirm_bars`（默认 3）根 K 线确认才正式切换策略
7. **策略映射**：根据 `selector.strategy_bull/bear/ranging/breakout` 映射到具体策略

### 6.3 添加新策略

详细规范参见 [`strategy/STRATEGY_GUIDE.md`](strategy/STRATEGY_GUIDE.md)（**AI 编写策略前必读**）。

步骤：
1. 在 `strategy/` 新建文件，继承 `BaseStrategy`
2. 实现 `generate_signal(self, df)` → 返回 `("BUY"/"SELL"/"HOLD", reason_str)`
3. （可选）实现 `precompute()` + `signal_from_row()` 高性能回测路径
4. 声明 `PARAMS` 类变量（前端策略参数表单自动渲染）
5. 在 `strategy/registry.py` 的 `_REGISTRY` 中注册
6. 重启服务即生效

---

## 7. 风控引擎

### 7.1 风控规则

| 规则 | 配置项 | 默认值 | 触发行为 |
|------|--------|--------|----------|
| 连亏熔断 | `max_consecutive_losses` | 3 次 | `is_trading_allowed = False` → Bot 停止开仓 |
| 日亏熔断 | `daily_loss_limit_pct` | 5% | `is_trading_allowed = False` → Bot 停止开仓 |
| 单笔金额上限 | `max_trade_amount` | 1000 USDT | 订单前置拦截 |
| 仓位计算 | `risk_per_trade_pct` | 1% | Fixed Fractional Sizing（含手续费扣减） |

### 7.2 V2.5 高级风控

| 功能 | 配置项 | 说明 |
|------|--------|------|
| **追踪止损 (Trailing Stop)** | `risk_v25.trailing_stop_*` | 盈利达 ATR×0.5 后激活，价格回撤 ATR×0.8 上移 SL；只上移不回退 |
| **时间止损 (Time Stop)** | `risk_v25.time_stop_*` | 持仓超 24 根 K 线（1h=24h）且不亏损时平仓；超 36 根强制平仓 |
| **动态仓位 (Dynamic Position)** | `risk_v25.dynamic_position_enable` | regime 置信度 < 0.7 时按比例缩减仓位；策略近期胜率 < 35% 降权 60% |
| **策略绩效追踪** | DB: `strategy_performance` | 每笔交易记录对应策略的 PnL，用于动态仓位决策 |

### 7.3 风控生命周期

```
Bot 启动 → _restore_risk_state()（从 risk_state 表恢复）
         → 若连亏 >= 上限 或 日亏已触发 → is_trading_allowed = False
每次平仓 → rm.notify_trade_result(pnl, balance)
         → 更新连亏计数 / 日亏计算 / 判断是否熔断
         → _save_risk_state() 持久化
每日轮转 → rm.reset_daily(new_balance)（连亏不重置，跨日继续累计）
用户恢复 → POST /api/bot/risk/resume → manual_resume()（清零 + 重启）
```

### 7.4 仓位计算公式

```python
# Fixed Fractional Sizing（risk/risk_manager.py::calculate_position_size）
max_loss_allowed = balance × risk_pct
price_risk_per_contract = |entry_price - sl_price| × contract_size
total_risk_per_contract = price_risk + open_fee + close_fee
target_contracts = floor(max_loss_allowed / total_risk_per_contract)
# 受 max_trade_amount 和实际可用余额二次约束
```

---

## 8. 配置文件详解 (config.yaml)

```yaml
bot:
  symbol: "BTC/USDT:USDT"      # 默认交易对（用户 DB 覆盖优先）
  timeframe: "1h"               # 默认 K 线周期
  leverage: 3                   # 默认杠杆倍数
  contract_size: 0.01           # OKX BTC 合约面值（0.01 BTC/张）
  taker_fee_rate: 0.0005        # Taker 手续费率
  check_interval: 300           # 主循环间隔（秒）

risk:
  risk_per_trade_pct: 0.01      # 单笔最大亏损 1%
  max_trade_amount: 1000        # 单笔金额上限（USDT）
  max_consecutive_losses: 3     # 连亏熔断次数
  daily_loss_limit_pct: 0.05    # 日亏 5% 熔断

strategy:
  name: "AUTO"                  # AUTO = 选择器模式；或直接填策略名
  params: {}                    # 固定策略时的参数覆盖

selector:                       # AUTO 模式选择器参数
  tech_weight: 1.0              # 技术面权重
  news_weight: 0.0              # 新闻面权重（AI 未接入时设 0）
  confirm_bars: 3               # 防抖确认根数
  enable_market_extra: true     # 🆕 V2.0: 是否启用资金费率/OI 数据增强
  funding_weight: 0.15          # 🆕 V2.0: 资金费率权重
  oi_weight: 0.10               # 🆕 V2.0: OI 权重
  # ... 详细参数见 config.yaml 文件注释

ai:                             # 🆕 V2.0: AI 情绪分析客户端
  api_key: ""                   # 填入 API Key 后自动启用
  base_url: "https://api.openai.com/v1"   # 或 DeepSeek / 零一万物
  model: "gpt-4o-mini"          # 推荐：gpt-4o-mini 或 deepseek-chat

risk_v25:                       # 🆕 V2.5: 高级风控参数
  trailing_stop_enable: true    # 追踪止损
  trailing_stop_trigger: 0.5    # 盈利达 ATR*此值后激活
  trailing_stop_distance: 0.8   # 回撤 ATR*此值更新 SL
  time_stop_enable: true        # 时间止损
  time_stop_bars: 24            # 最多持仓 N 根 K 线
  dynamic_position_enable: true # 动态仓位

api:
  host: "0.0.0.0"
  port: 8080
  cors_origins: ["*"]           # 生产环境改为实际域名
```

**热更新**：`config.yaml` 修改后无需重启，`config_loader.py` 会在下一次 `get_config()` 调用时检测文件 mtime 变化并自动重载。但注意：Bot 主循环中的部分参数（如 `SYMBOL`、`LEVERAGE`）在启动时读取一次，**不会**随热更新变化，需要重启 Bot。

---

## 9. 环境变量 (.env)

| 变量 | 必填 | 来源 | 说明 |
|------|------|------|------|
| `TG_BOT_TOKEN` | 可选 | 手动填写 | 全局 Telegram Bot Token（旧版兼容后备） |
| `TG_CHAT_ID` | 可选 | 手动填写 | 全局 Telegram Chat ID |
| `ENCRYPT_KEY` | ✅ | `deploy.sh` 自动生成 | Fernet 对称加密密钥（保护 API Key） |
| `JWT_SECRET` | ✅ | `deploy.sh` 自动生成 | JWT 签名密钥 |
| `AI_API_KEY` | 可选 | 手动填写或 config.yaml | AI 情绪分析 API Key（V2.0） |
| `AI_BASE_URL` | 可选 | 手动填写或 config.yaml | AI API 地址（默认 OpenAI） |
| `AI_MODEL` | 可选 | 手动填写或 config.yaml | AI 模型名称 |

> ⚠️ **安全提醒**：`.env` 文件权限应为 600。`ENCRYPT_KEY` 丢失将导致所有已加密的 API Key 无法解密！

---

## 10. 部署与运维

### 10.1 首次部署（Ubuntu VPS）

```bash
git clone <repo> && cd QuantProject
cp .env.example .env && nano .env   # 填 TG_BOT_TOKEN + TG_CHAT_ID（可选）
bash deploy.sh
```

`deploy.sh` 自动完成：
- 安装 Python 依赖 (`requirements.txt`)
- 生成 `ENCRYPT_KEY` + `JWT_SECRET`（首次）
- 构建 React 前端 (`npm run build`)
- 安装 systemd 服务 + 定时器
- 启动服务

### 10.2 日常运维命令

```bash
# 服务管理
bash startbot.sh               # 启动（等价于 systemctl start quantbot）
bash stopbot.sh                # 停止
systemctl restart quantbot     # 重启
systemctl status quantbot      # 查看状态

# 日志查看
journalctl -u quantbot -f      # systemd 日志（实时跟踪）
tail -f logs/server.log         # 文件日志
tail -f tradelog/<username>.log # 每用户交易日志

# 数据库
python3 scripts/backup_db.py               # 手动备份
python3 scripts/fix_database.py            # 迁移修复
sqlite3 trading_data.db ".tables"          # 查看表
sqlite3 trading_data.db "SELECT * FROM schema_version"  # 查看版本

# 新闻系统
python3 scripts/fetch_news.py --status     # 查看新闻抓取状态
python3 scripts/fetch_news.py --force      # 强制立即抓取

# 定时器状态
systemctl list-timers | grep quantbot      # 查看所有定时器
```

### 10.3 更新代码

```bash
git pull
bash deploy.sh   # 会自动重新构建前端 + 重启服务
```

### 10.4 Nginx + HTTPS（生产环境推荐）

```bash
apt install certbot python3-certbot-nginx
cp nginx/quantbot.conf /etc/nginx/sites-available/
ln -s /etc/nginx/sites-available/quantbot.conf /etc/nginx/sites-enabled/
# 编辑 quantbot.conf，替换 your-domain.com
certbot --nginx -d your-domain.com
nginx -s reload
```

生产环境还应修改 `config.yaml`：
```yaml
api:
  cors_origins: ["https://your-domain.com"]
```

### 10.5 本地开发

```bash
# 后端
pip install -r requirements.txt
python -m uvicorn api.server:app --reload --port 8080

# 前端（另开终端）
cd frontend && npm install && npm run dev
# Vite 默认 5173 端口，代理到 8080
```

---

## 11. 故障排除

### 11.1 Bot 不开仓

| 检查项 | 操作 |
|--------|------|
| API Key 有效？ | 设置页 → 「验证 API Key」 |
| 风控熔断？ | 控制台显示「已熔断」→ 点「恢复熔断」 |
| 策略返回 HOLD？ | 查看 `tradelog/<username>.log` 中策略信号 |
| 合约账户余额？ | 设置页 → 实时余额，确保 swap 账户有 USDT |
| 杠杆设置失败？ | 日志中搜 "设置杠杆失败"，OKX 可能有持仓时不允许改杠杆 |
| 品种是否带 `:USDT`？ | 永续合约格式必须是 `BTC/USDT:USDT`（非 `BTC/USDT`） |

### 11.2 被动平仓检测

Bot 每轮查询交易所实际持仓：
1. 本地有仓但交易所返回 0 → 判定为 SL/TP 被触发
2. 获取成交价优先级：`fetch_my_trades` > `fetch_order`(条件单) > SL/TP 价格估算
3. 如果 `_live_position_amount` 返回 -1（查询失败），**不会误判为平仓**

### 11.3 数据库锁定

```
sqlite3.OperationalError: database is locked
```

- 确认 WAL 模式：`sqlite3 trading_data.db "PRAGMA journal_mode"`（应返回 `wal`）
- 确认 `busy_timeout`：所有连接都通过 `db_handler.get_conn()` 创建（自带 5s timeout）
- 确认单 worker：`uvicorn --workers 1`（多 worker 写同一 SQLite 会锁死）

### 11.4 旧库结构兼容问题

如果看到类似：

```python
sqlite3.OperationalError: no such column: user_id
sqlite3.OperationalError: no such column: timestamp
```

通常说明数据库已经处于“新旧结构混合态”：
- `daily_balance` 可能是旧单用户结构
- `trade_history` 可能已经升级为新多用户结构
- 代码若仍按旧字段查询，会导致页面局部加载失败

当前项目已对以下接口做兼容处理：
- `/api/data/trades`
- `/api/data/balance`

运维建议：
- 优先让 `execution/db_handler.py` 的 migration 成为唯一 schema 来源
- 不要长期依赖手工补列；手工修复后应同步整理 migration
- 如果要切换模拟盘/实盘，最好使用不同账号，避免历史记录混杂

### 11.4 Watchdog 自动重启

- Watchdog 线程首次 `start_bot()` 时启动，60 秒轮询
- 崩溃退避：30s → 60s → 120s → 300s（上限）
- 用户主动 `stop_bot` 的不会被 Watchdog 重启（记录在 `_manually_stopped`）
- Watchdog 会通过 Telegram 发送崩溃/重启通知

### 11.5 ENCRYPT_KEY 丢失

如果 `.env` 中 `ENCRYPT_KEY` 丢失或更改，**所有已加密的 API Key 和 TG 配置将无法解密**。

恢复方案：
1. 从备份中恢复 `.env` 文件
2. 或者：所有用户需重新在 Web 设置页面填写 API Key 和 TG 配置

### 11.6 网络错误分级

runner.py 内置网络错误分类（`_classify_error`）：

| 错误类型 | 处理 | 说明 |
|----------|------|------|
| `rate_limit` | 退避等待 60s | OKX 限频 |
| `maintenance` | 暂停 5 分钟 | 交易所维护 |
| `auth_error` | **立即停止 Bot** | API Key 无效或过期 |
| `network` | 正常重试 | 网络临时故障 |
| `unknown` | 记录日志，继续 | 未知错误 |

连续查询失败 3 次会触发 Telegram 告警（同类错误 5 分钟内去重）。

---

## 12. 安全机制

| 机制 | 实现 |
|------|------|
| 密码存储 | bcrypt 哈希 |
| JWT 认证 | HS256，每个 API 请求校验 |
| API Key 加密 | Fernet AES-256 对称加密，明文不落盘 |
| TG 配置加密 | 同上 Fernet |
| .env 权限 | `chmod 600`（deploy.sh 自动设置） |
| CORS | 可配置允许源列表 |
| 输入校验 | Pydantic model + 业务层校验（杠杆 1~125、风险 0.1%~10%） |
| 告警去重 | 同类错误 5 分钟冷却 |

---

## 13. 通知系统

### 13.1 通知渠道

| 渠道 | 函数 | 说明 |
|------|------|------|
| Telegram（每用户） | `make_notifier(token, chat_id)` | 用户在 Web 设置页配置 |
| Telegram（全局后备） | `send_telegram_msg()` | `.env` 中 TG_BOT_TOKEN/TG_CHAT_ID |
| Webhook | `make_webhook_notifier(url)` | 支持 Discord/企业微信等 |
| 组合通知 | `make_multi_notifier(tg, webhook)` | 任一成功即视为成功 |

### 13.2 通知事件

- 🚀 Bot 启动（策略、品种、杠杆）
- 📈 开仓（方向、价格、数量、保证金、策略名）
- 📉 平仓（方向、入场→出场价、PnL、原因）
- 🔄 策略反转（平旧仓 + 开新仓详情）
- 🚨 风控熔断（连亏/日亏）
- ⚠️ 异常告警（网络、条件单失败、订单对账异常）
- 🔄 Watchdog 崩溃重启
- ⛔ Bot 停止

### 13.3 Fallback 链

```
用户 TG 配置 → 全局 .env TG → 空操作（日志记录，不崩溃）
```

### 13.4 OKX 资产口径说明（重要）

前端“OKX 实时余额”当前优先采用 **OKX 原始账户接口** 的 `totalEq` 作为“账户总资产”显示口径，而不是单纯的 `USDT.total`。

这样做的原因：
- `ccxt.fetch_balance()` 在 OKX 模拟盘环境下可能出现异常返回（例如 `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`）
- 单纯使用 `USDT total/free/used` 会低估账户页面展示的总资产
- `totalEq` 更接近 OKX 网页端“总权益 / 总资产”的显示逻辑

当前返回字段含义：
- `total`：优先取 `totalEq`（更接近 OKX 页面总资产）
- `free`：优先取 USDT 明细中的 `availBal`
- `used`：近似按 `total - free` 计算，用于前端展示“占用/保证金”概念

注意：
- `used` 是展示近似值，不一定与 OKX 页面每个子账户拆分值严格一致
- 若要做严谨风控或对账，请优先使用 OKX 原始字段而不是前端展示聚合值

---

## 14. 回测引擎

`backtest/engine.py::run_backtest()` — **V3.5**

### 14.1 基础能力
- **滑点模拟**：开仓/平仓/反转均含滑点（方向相关）
- **手续费**：逐笔计算 taker_fee
- **熔断模拟**：连亏/日亏触发后暂停交易
- **风险指标**：Sharpe / Sortino / Calmar / MaxDD / 胜率 / 盈亏比
- **进度回调**：每 10% K 线更新一次（前端轮询显示进度条）
- **异步执行**：后台 Thread 运行，通过 `_backtest_results` + `_backtest_lock` 安全通信
- **历史持久化**：每用户最多保留 20 条历史回测结果

### 14.2 V3.5 新增功能

| 功能 | 说明 |
|------|------|
| **AUTO 模式回测** | `strategy="AUTO"` 时内置 MarketRegimeSelector，每 5 根 K 线评估 regime，自动切换策略（纯技术面驱动，禁用新闻/链上数据） |
| **追踪止损** | 盈利达 ATR × trigger 后激活，最优价回撤 ATR × distance 时上移 SL |
| **时间止损** | 持仓超 N 根 K 线且不亏损时平仓，超 1.5N 强制平仓 |
| **动态仓位** | AUTO 模式下 regime 置信度 < 0.7 按比例缩减仓位；策略近期胜率 < 35% 降权 60% |
| **策略切换明细** | 记录每次切换的 bar_idx / time / from→to / regime / confidence / reason |
| **per-strategy 统计** | 回测结果按策略分别统计交易数、胜率、盈亏 |
| **WAIT 观望** | AUTO 模式下 WAIT 状态跳过开仓 |

### 14.3 回测结果字段

```json
{
  "trades": [...],              // 每笔交易（新增 strategy 字段）
  "equity_curve": [...],        // 权益曲线
  "sharpe_ratio": 1.5,
  "max_drawdown_pct": 12.3,
  "is_auto_mode": true,         // V3.5: 是否 AUTO 模式
  "strategy_switches": [...],   // V3.5: 策略切换明细列表
  "per_strategy_stats": {...},  // V3.5: 各策略统计
  "features": {                 // V3.5: 启用的高级功能
    "trailing_stop": true,
    "time_stop": true,
    "dynamic_pos": false
  }
}
```

---

## 15. 前端页面功能

| 页面 | 路由 | 功能 |
|------|------|------|
| AuthPage | `/login` | 登录/注册切换 |
| Dashboard | `/` | Bot 启停、实时 WebSocket 状态、持仓卡片、浮动盈亏、熔断恢复；🆕 V3.0: 市场状态面板（资金费率+OI+新闻情绪+综合信号）、Regime 状态标签 |
| TradesPage | `/trades` | 历史交易记录分页、胜率/总PnL统计 |
| BalancePage | `/balance` | 资产曲线图（Recharts）+ OKX 实时余额 |
| BacktestPage | `/backtest` | 策略/品种/参数选择 → 触发回测 → K线图 + 权益曲线 + 交易列表；🆕 V3.5: AUTO 模式、高级功能开关、策略切换明细表、各策略绩效对比 |
| SettingsPage | `/settings` | OKX API Key 管理、策略/品种/杠杆/风控配置、TG 通知配置 |

前端特性：
- JWT 自动注入（Axios 拦截器）
- 401 自动跳转登录（token 过期处理）
- WebSocket 自动重连（断开后重试）
- ErrorBoundary 防止组件崩溃白屏
- 骨架屏加载状态

---

## 16. 关键设计决策

| 决策 | 原因 |
|------|------|
| SQLite 而非 PostgreSQL | 单 VPS 部署、低运维成本、WAL 足够并发需求 |
| 单 worker | SQLite 不支持多进程写；单进程 + 多线程足够支撑几十个用户 |
| 线程而非 asyncio Bot | ccxt 是同步库；用 Thread + `asyncio.to_thread` 桥接 FastAPI |
| Fernet 而非数据库加密 | 密钥与数据物理分离（.env vs .db）；密钥丢失可通过 .env 备份恢复 |
| 策略层纯信号 | 策略不碰资金/仓位/交易所，易于回测和测试 |
| config.yaml 热更新 | 修改风控参数无需重启服务（但 Bot 启动时读取的参数需重启 Bot 生效） |
| 每用户独立线程 | 用户间完全隔离：独立 RiskManager、exchange 实例、持仓状态 |

---

## License

MIT
