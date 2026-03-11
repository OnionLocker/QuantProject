# 🤖 QuantProject 策略模块开发与接入规范 (AI 阅读版)

> **[Instruction for AI Models]**
> 当你（AI）被要求为本系统编写新的量化交易策略时，**必须且只能**遵循本文档中的所有架构规范、输入输出限制和最佳实践。请勿自行引入未经授权的第三方交易库（如 `backtrader`、`ccxt` 等），策略层只负责纯粹的数学与逻辑计算。

## 1. 架构定位 (Architecture Context)
本系统采用**高内聚、低耦合**的设计模式。
* **执行层 (`core/user_bot/runner.py` / `backtest/engine.py`)**：负责获取 K 线数据、管理资金、计算仓位、扣除手续费、发送交易请求、处理止盈止损。
* **策略层 (`strategy/*.py`)**：**仅作为"信号大脑"存在**。它不需要知道当前账户有多少钱、不需要知道当前是否持仓、也不需要调用任何交易所 API。它只负责接收一段 K 线历史，并吐出标准化指令。

## 2. 核心硬性规则 (Mandatory Rules)

### 2.1 继承标准基类
所有新策略**必须**继承 `strategy.base.BaseStrategy`。
在 `__init__` 方法中，必须调用 `super().__init__(name="你的策略名称")`，并将策略所需的所有超参数暴露在 `__init__` 中。

### 2.2 实现 `generate_signal()` 方法（必须）
策略类**必须**覆盖 `generate_signal(self, df: pd.DataFrame) -> dict` 方法。

* **输入 `df`**: Pandas DataFrame，索引为时间戳，包含列：`['open', 'high', 'low', 'close', 'volume']`。
* **返回格式**: 必须返回标准 dict：

```python
{
    "action":  "BUY" | "SELL" | "HOLD",   # 标准化动作指令
    "entry":   float,                      # 建议入场价（通常为下一根K线开盘价）
    "sl":      float,                      # 止损价
    "tp1":     float,                      # 第一止盈价
    "tp2":     float,                      # 第二止盈价（可选，为0表示无）
    "risk_r":  float,                      # 风险距离（入场价与止损的差值）
    "reason":  str,                        # 可读的信号说明
    "meta":    dict,                       # 附加元数据（如 regime 状态等）
}
```

### 2.3 可选高性能接口（回测加速）
为了在回测中获得数量级的性能提升，策略可以额外实现两个方法：

* **`precompute(self, df: pd.DataFrame) -> pd.DataFrame`**: 对整个回测 DataFrame 一次性向量化计算所有指标和信号列（`sig_action`, `sig_sl`, `sig_tp1`, `sig_tp2`, `sig_reason`）。
* **`signal_from_row(self, df: pd.DataFrame, i: int) -> dict`**: 从预计算好的 DataFrame 中读取第 `i` 行的信号，O(1) 复杂度。

### 2.4 PARAMS 元数据声明
每个策略应声明 `PARAMS` 类变量，供前端动态渲染参数表单：

```python
PARAMS = [
    {
        "key":     "ema_fast",          # 与 __init__ 参数名一致
        "label":   "快速EMA周期",        # 前端显示名
        "type":    "int",               # int / float / str
        "default": 8,                   # 默认值
        "min":     5,                   # 最小值
        "max":     30,                  # 最大值
        "step":    1,                   # 调节步长
        "tip":     "BTC 1h 推荐 8",     # 参数说明
    },
    ...
]
```

### 2.5 标准化动作指令 (Action)
返回的 `action` 必须是以下三个纯大写字符串之一：
* `"BUY"`：看多信号
* `"SELL"`：看空信号
* `"HOLD"`：观望

## 3. 策略代码模板

```python
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class YourStrategy(BaseStrategy):
    PARAMS = [
        {"key": "ema_fast", "label": "快速EMA", "type": "int", "default": 8,
         "min": 5, "max": 30, "step": 1, "tip": "快线周期"},
        {"key": "atr_sl_mult", "label": "ATR止损倍数", "type": "float", "default": 1.2,
         "min": 0.5, "max": 3.0, "step": 0.1, "tip": "止损距离 = ATR × 倍数"},
    ]

    def __init__(self, ema_fast: int = 8, atr_sl_mult: float = 1.2):
        super().__init__(name="YOUR_策略名称")
        self.ema_fast = ema_fast
        self.atr_sl_mult = atr_sl_mult
        self.warmup_bars = ema_fast + 20  # 预热所需最少K线数

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}
        if df is None or len(df) < self.warmup_bars + 5:
            return sig

        # 1. 计算指标
        # 2. 提取最近已完结K线 (j = -2)
        # 3. 核心信号判断
        # 4. 计算 SL/TP，更新 sig 并返回
        return sig

    # 可选：precompute + signal_from_row（回测加速）
    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        # 向量化计算所有指标和信号列
        ...
        df['sig_action'] = actions
        df['sig_sl']     = sig_sl
        df['sig_tp1']    = sig_tp1
        df['sig_tp2']    = sig_tp2
        df['sig_reason'] = reasons
        return df

    def signal_from_row(self, df: pd.DataFrame, i: int) -> dict:
        row = df.iloc[i]
        return {
            "action": row['sig_action'], "sl": row['sig_sl'],
            "tp1": row['sig_tp1'], "tp2": row['sig_tp2'],
            "risk_r": 0.0, "reason": row['sig_reason'],
            "entry": row['open'], "meta": {},
        }
```

## 4. 注册新策略
在 `strategy/registry.py` 的 `_REGISTRY` 中追加一行即可，重启服务后前端自动生效：

```python
from strategy.your_file import YourStrategy
_REGISTRY["YOUR_NAME"] = YourStrategy
```

## 5. 已注册策略一览

| 名称 | 类 | 适用行情 | 核心指标 |
|------|----|----------|----------|
| `BULL` | TrendBullStrategy | 牛市趋势 | EMA金叉 + ADX + RSI + ATR |
| `BEAR` | TrendBearStrategy | 熊市趋势 | EMA死叉 + ADX + RSI + ATR |
| `RANGE` | RangeOscillatorStrategy | 震荡→突破 | 布林带收缩 + 区间突破 + ADX启动 + 成交量 |
| `BIG_CANDLE` | BigCandleStrategy | 趋势突破 | 大K线量价 + 布林带收窄 |
| `PA_5S` | PriceActionSetups | 通用（价格行为） | Pin Bar / 吞没 / 孕线 / Spring |
| `ADAPTIVE` | AdaptiveStrategy | 自适应路由 | PA_5S + RegimeDetector |
| `AUTO` | MarketRegimeSelector | 自动切换 | 技术面 + 新闻 + 资金费率/OI |

## 6. 最佳实践
- 使用 `df.iloc[-2]`（倒数第2根已完结K线）作为信号棒，避免用未完成的当前K线
- 止损/止盈必须基于 ATR 动态计算，不要硬编码固定百分比
- 冷却期 `cooldown` 参数防止信号过密（BTC 1h 推荐 4~5 根）
- K 线最小尺寸过滤（`total_len > ATR * N`）避免小K线噪音
- 所有策略只做信号计算，不访问交易所 API，不管理资金

## 7. AUTO 模式增强 (V3.0)

### 7.1 RANGE 策略重写：收缩等突破
V3.0 将 RANGE 策略从「均值回归」改为「收缩等突破」，解决 BTC 最大的亏损来源。

**核心改变：**
- 旧版：在布林带边缘做反转 → BTC 假突破→真突破时巨亏
- 新版：震荡期不交易，等布林带收缩后首次突破方向明确才入场

**信号体系：**
| 信号 | 方向 | 条件 | 特点 |
|------|------|------|------|
| R1 | 做多 | 布林带收缩→突破上轨 + 量价确认 | 蓄力后首突破，高确定性 |
| R2 | 做多 | 区间高点突破 + ADX 从低位上升 | 趋势启动型入场 |
| R3 | 做空 | 布林带收缩→跌破下轨 + 量价确认 | 蓄力后首突破，高确定性 |
| R4 | 做空 | 区间低点突破 + ADX 从低位上升 | 趋势启动型入场 |

### 7.2 动态 confirm_bars（置信度加权）
Regime 切换的确认根数不再固定，而是根据置信度自适应：

| 置信度 | 确认根数 | 场景 |
|--------|----------|------|
| > 0.7（高） | 2 根 | 信号明确，快速切换抓住行情 |
| 0.4~0.7 | 2~4 根（线性插值） | 信号一般，适度确认 |
| < 0.4（低） | 4 根 | 信号模糊，谨慎确认 |

### 7.3 资金费率否决权
当资金费率极端（>0.1%）且与技术面方向冲突时，强制进入 WAIT 状态：
- 正费率极端 + 技术面判牛 → 否决（多头拥挤，可能回调）
- 负费率极端 + 技术面判熊 → 否决（空头拥挤，可能反弹）
- 可通过 `funding_veto_contra: false` 改为极端费率无条件否决

## 8. V4.0 机构级升级

### 8.1 多时间框架确认 (MTF)
机构级策略的核心方法论：**Top-Down Analysis**。4h 高时间框架作为方向过滤器，1h 信号与 4h 方向一致时才具备高置信度。

**实现方式：**
- 将 1h K 线聚合为 4h K 线（`resample('4h')`）
- 计算 4h EMA（默认 50 周期）的方向和斜率
- 作为独立投票源参与加权投票（默认权重 15%）

| MTF 方向 | 1h 信号 | 结果 |
|----------|---------|------|
| 看涨 | 做多 | ✅ 强信号（MTF 加成） |
| 看涨 | 做空 | ⚠️ 信号质量扣分（逆大势） |
| 中性 | 任意 | 正常处理 |

### 8.2 成交量确认过滤 (Volume Confirmation)
V4.0 在 BULL/BEAR/RANGE 三个子策略中全面加入成交量确认：

| 策略 | 信号 | 要求 | 原理 |
|------|------|------|------|
| BULL | B1 金叉 | vol_ratio > 0.8 | 排除缩量假金叉 |
| BULL | B3 突破高点 | vol_ratio > 1.2 | 突破需放量确认 |
| BULL | S1 死叉 | vol_ratio > 0.8 | 排除缩量假死叉 |
| BEAR | S1 死叉 | vol_ratio > 0.8 | 同上 |
| BEAR | S3 跌破低点 | vol_ratio > 1.2 | 突破需放量确认 |
| BEAR | B1 金叉 | vol_ratio > 0.8 | 同上 |
| RANGE | R1/R3 收缩突破 | vol_ratio > vol_mult | 量能门槛（长收缩时降低 15%） |
| Selector | 技术面评分 | vol_ratio 参与评分 | 量价配合加分/缩量趋势减分 |

`vol_ratio = 当前成交量 / 20期成交量均线`

### 8.3 信号质量评分系统 (Signal Quality Score)
综合多维度评分 [0, 100]，用于动态调节仓位：

| 维度 | 分值 | 计算方式 |
|------|------|----------|
| 技术面置信度 | 0-30 | `tech_conf × 30` |
| 多源一致性 | 0-25 | 技术/链上/新闻/MTF 中与最终 regime 一致的比例 |
| 链上数据质量 | 0-20 | `extra_conf × 20`，方向一致加 10% |
| MTF 方向确认 | -5~15 | 一致=加分，冲突=扣 5 分 |
| 波动率环境 | 5-10 | 基础 5 分 + `tech_conf × 5` |

**仓位映射：**
- `quality >= 60`：满仓
- `40 <= quality < 60`：按比例缩仓（`quality / 100`）
- `quality < 40`：不开仓

### 8.4 动态否决权阈值 (Dynamic Veto)
替代 V3.0 的固定 0.1% 否决阈值：
- 基于最近 48 期费率绝对值的 **90th 百分位**
- 自动适应牛市/熊市不同的费率分布
- 保底：不低于固定阈值 50%，不高于 3 倍

### 8.5 OI 连续性分析 (OI Continuity)
区分「持续资金流入」和「单期暴增」：
- **持续上升（≥3 期）**：机构资金持续入场，增强趋势信号
- **持续下降（≥3 期）**：去杠杆阶段，增强看跌/观望信号
- **单期暴增**：短期投机，信号衰减（权重降为 0.3）

### 8.6 VWAP 偏离度 (VWAP Deviation)
Volume Weighted Average Price 偏离度作为均值回归压力指标：
- 价格远在 VWAP 上方（> 2%）时，做多信号打 9 折
- 价格远在 VWAP 下方（< -2%）时，做空信号打 9 折
- 防止在价格已大幅偏离均值时追单

### 8.7 Regime 切换旧仓管理
当 regime 发生方向性切换时，自动处理旧仓位：

| 切换方向 | 操作 | 紧急度 |
|----------|------|--------|
| BULL → BEAR | 立即平多 | 1.0 |
| BEAR → BULL | 立即平空 | 1.0 |
| BULL → RANGING | 平多但不紧急 | 0.5 |
| BEAR → RANGING | 平空但不紧急 | 0.5 |
| BULL/BEAR → WAIT | 收紧止损 50% | 0.3 |

### 8.8 RANGE 策略止损优化
R1/R3（布林带收缩突破）止损改进：
- **旧版**：止损 = 布林带对侧 - ATR×倍数（止损过远，RR 比差）
- **V4.0**：止损 = 突破K线最低/最高价 ± 0.5×ATR（更紧凑）
- 收缩持续 ≥ 8 根 K 线时，量能门槛降低 15%（长蓄力 = 高质量突破）

## 9. V4.0 风控升级

### 9.1 回撤保护 (Drawdown Protection)
三级回撤保护机制：

| 级别 | 回撤 | 仓位缩放 | 动作 |
|------|------|----------|------|
| 正常 | < 3% | 100% | 正常交易 |
| 警告 | 3-5% | 75% | 日志告警 |
| 减仓 | 5-8% | 50% | 自动降仓 |
| 熔断 | ≥ 8% | 0% | 停止交易 |

### 9.2 Equity Curve Trading
当账户余额跌破资金曲线 EMA 时，说明策略处于不利期：
- 仓位自动降至 60%
- EMA 周期 = 10（最近 10 笔交易）
- 余额回到 EMA 上方后恢复正常仓位

### 9.3 动态风险预算 (Dynamic Risk Budget)
简化 Kelly Criterion 方法：
- `risk_mult = max(0.5, min(1.5, win_rate × 2.0))`
- 连续亏损额外惩罚：每次连亏 -15%
- 最终 risk_pct = base_risk × kelly × drawdown_scale × equity_scale × regime_scale

### 9.4 每日交易次数限制
- 默认每日最多 8 次开仓
- 防止策略在震荡市过度交易（高频亏损）
- 熔断后手动恢复不重置当日计数
