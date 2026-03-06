# 🚀 QuantProject: 模块化加密货币量化交易系统

> **[Instruction for AI Context]**
> 如果你是被要求协助开发、调试或重构本项目的 AI 助手，请仔细阅读本项目的架构设计与解耦逻辑。本项目采用高度模块化的设计，实盘执行、历史回测与策略大脑完全分离。请在提供代码时，严格遵守各模块的职责边界，**不要跨模块污染代码**。

## 1. 📖 项目简介 (Overview)
这是一个基于 OKX 永续合约的专业级量化交易框架。
系统核心优势在于：
- **真正的摩擦成本计算**：实盘与回测均精确扣除 Taker 手续费（0.05%），拒绝“模拟盘战神”。
- **极端防呆与安全设计**：内置启动时的“防失忆”持仓同步功能，以及平仓时的 `reduceOnly=True`（只减仓）保护，杜绝网络延迟导致的灾难性反向开仓。
- **环境隔离**：通过 `.env` 文件一键无缝切换 OKX 模拟盘与真实实盘。
- **沙盒回测引擎**：本地回测引擎与实盘主程序共享同一个策略对象（Strategy），做到真正的“所测即所得”。

## 2. 📂 核心架构与目录职责 (Architecture & Directory Structure)

本项目坚持“高内聚、低耦合”，各司其职，互不干涉：

* **`main.py` (总司令 / 实盘入口)**
  - 职责：串联所有模块。负责每隔 N 秒请求数据、调用策略大脑获取信号、计算动态仓位、执行开平仓、记录详细的真实财务账单，并通过 Telegram 播报。
* **`backtest/` (时光机 / 回测引擎)**
  - `engine.py`: 职责同 `main.py`，但它不连接真实交易所，而是遍历本地 CSV 历史数据，模拟真实的开平仓扣费逻辑，最终输出带有“最大回撤”和“净收益率(ROI)”的硬核成绩单。
* **`strategy/` (大脑 / 策略模块)**
  - `base.py`: 策略插座的基类。
  - `price_action.py`: 具体的策略实现（如 1H 级别的 PA 突破 + EMA50 趋势过滤）。
  - **⚠️ AI 须知**：策略层绝不接触账户资金、持仓状态或网络请求。它只接收 DataFrame，并吐出 `("BUY"|"SELL"|"HOLD", "原因")`。
* **`core/` (通信兵 / API 核心)**
  - `okx_client.py`: 单例模式封装 CCXT 客户端。负责读取 `.env` 并自动识别连接模拟盘或实盘，包含 `fetch_position_state`（查岗防失忆功能）。
* **`data/` (档案管理员 / 数据模块)**
  - `market_data.py`: 负责实时拉取 K 线（`fetch_kline_data`），以及突破限制批量下载多年历史数据并存为 CSV（`download_history_to_csv`）。
* **`execution/` (刽子手 / 订单执行)**
  - `order_manager.py`: 负责下发真实的 Market Order，自动识别净持仓/双向持仓模式，处理杠杆设置和 USDT 余额查询。
* **`risk/` (护卫 / 风控模块)**
  - `risk_manager.py`: 下单前的最后一道安全门，拦截异常的巨量订单或在极端情况下全局熔断。
* **`utils/` (后勤 / 工具箱)**
  - `logger.py`: 规范化本地日志输出（双写控制台和 `.log` 文件）。
  - `notifier.py`: Telegram 消息推流机器人。

## 3. ⚙️ 环境配置与启动 (Quick Start)

### 3.1 环境变量设定
复制根目录的 `.env.example` 为 `.env`，并配置以下参数。系统会**优先识别 SIMULATE 配置**，若存在则进入模拟盘；若将其注释，则进入实盘。

```env
# --- OKX 模拟盘 API (如果解除注释，系统强制为模拟盘) ---
SIMULATE_OKX_API_KEY=xxx
SIMULATE_OKX_SECRET_KEY=xxx
SIMULATE_OKX_PASSPHRASE=xxx

# --- OKX 真实实盘 API (注释掉上面的 SIMULATE 才会生效) ---
# OKX_API_KEY=xxx
# OKX_SECRET_KEY=xxx
# OKX_PASSPHRASE=xxx

# --- Telegram 报警机器人 ---
TG_BOT_TOKEN=xxx
TG_CHAT_ID=xxx