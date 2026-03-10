# 🤖 QuantProject 策略模块开发与接入规范 (AI 阅读版)

> **[Instruction for AI Models]**
> 当你（AI）被要求为本系统编写新的量化交易策略时，**必须且只能**遵循本文档中的所有架构规范、输入输出限制和最佳实践。请勿自行引入未经授权的第三方交易库（如 `backtrader`、`ccxt` 等），策略层只负责纯粹的数学与逻辑计算。

## 1. 架构定位 (Architecture Context)
本系统采用**高内聚、低耦合**的设计模式。
* **执行层 (`core/user_bot/runner.py` / `backtest/engine.py`)**：负责获取 K 线数据、管理资金、计算仓位、扣除手续费、发送交易请求、处理止盈止损。
* **策略层 (`strategy/*.py`)**：**仅作为“信号大脑”存在**。它不需要知道当前账户有多少钱、不需要知道当前是否持仓、也不需要调用任何交易所 API。它只负责接收一段 K 线历史，并吐出标准化指令。

## 2. 核心硬性规则 (Mandatory Rules)

### 2.1 继承标准基类
所有新策略**必须**继承 `strategy.base.BaseStrategy`。
在 `__init__` 方法中，必须调用 `super().__init__(name="你的策略名称")`，并将策略所需的所有超参数（如均线周期、RSI阈值等）暴露在 `__init__` 中，以便引擎在实例化时传参。

### 2.2 实现唯一公开方法
策略类**必须且只能**通过覆盖 `generate_signal(self, df)` 方法来输出信号。
* **输入参数 `df`**: 一个 Pandas DataFrame。包含历史 K 线切片。索引为时间戳（datetime 格式），包含的列严格为：`['open', 'high', 'low', 'close', 'volume']`。
* **返回格式**: 必须返回一个包含两个元素的元组：`(Action: str, Message: str)`。

### 2.3 标准化动作指令 (Action Enum)
返回的 `Action` 必须是以下三个纯大写字符串之一，绝不允许出现其他值：
* `"BUY"`：代表看多信号（引擎在空仓时会开多，在持空单时会平空）。
* `"SELL"`：代表看空信号（引擎在空仓时会开空，在持多单时会平多）。
* `"HOLD"`：代表观望、无明确信号、或数据不足。

## 3. 策略代码脚手架 (Boilerplate Template)

所有新策略请严格基于以下模板进行编写：

```python
import pandas as pd
from strategy.base import BaseStrategy

class YourCustomStrategy(BaseStrategy):
    def __init__(self, param1=14, param2=50):
        # 1. 强制初始化基类并命名
        super().__init__(name="你的自定义策略名称")
        # 2. 绑定策略参数
        self.param1 = param1
        self.param2 = param2

    def generate_signal(self, df):
        # 1. 数据量校验：防止数据不足导致计算报错
        if df is None or len(df) < max(self.param1, self.param2) + 2:
            return "HOLD", "数据不足，继续观望。"
            
        # 2. 保护原始数据：使用 copy 避免报 SettingWithCopyWarning
        df = df.copy()

        # 3. 计算技术指标 (例如 MA, RSI, MACD)
        # df['ma'] = df['close'].rolling(window=self.param1).mean()

        # 4. 提取最新状态（通常使用倒数第二根已经走完的闭合 K 线，即 df.iloc[-2]；
        #    如果策略需要基于当前未闭合的实时价格，则使用 df.iloc[-1]）
        current_candle = df.iloc[-1]  # 或 -2，取决于策略严格度
        current_close = current_candle['close']
        
        # 5. 核心逻辑判断
        is_buy_condition = False  # 替换为真实的看多条件
        is_sell_condition = False # 替换为真实的看空条件

        # 6. 返回标准化元组 (Action, Msg)
        if is_buy_condition:
            return "BUY", "🟢 触发做多条件：[说明具体原因]"
        
        elif is_sell_condition:
            return "SELL", "🔴 触发做空条件：[说明具体原因]"
        
        return "HOLD", "⚪ 震荡观望中..."