"""
bot/strategy.py — Donchian 55/20 海龟简化策略

设计：
  - 入场：当前收盘突破前 N 根 K 线最高 → BUY（反之 SELL）
  - 出场：当前收盘反向突破前 M 根 K 线最低/最高 → EXIT
  - 止损：入场时固定 2×ATR（引擎强制执行）
  - 参数硬编码（不允许 grid search）。用户不可见不可调。

验证约束（阶段 1 必须通过）：
  - MaxDD < 35%
  - Calmar > 0.8
  - 盈亏比 > 1.8
  - 9 组参数敏感性（55±15 × atr_mult 1.5/2.0/2.5）MaxDD 标准差 < 5%
  - 4 段 walk-forward 无爆仓

本策略只负责"看 K 线产生信号"，风控/仓位/下单由 bot/risk.py 和 bot/broker.py 完成。
"""
from __future__ import annotations
from typing import Any
import pandas as pd


class DonchianStrategy:
    """Donchian 海龟 55/20 + ATR 2.0 止损。"""

    # 硬编码参数（不暴露给用户）
    entry_period: int = 55
    exit_period:  int = 20
    atr_period:   int = 14
    atr_sl_mult:  float = 2.0

    # 引擎读取
    warmup_bars:  int = 60     # 至少需要 entry_period+5 根

    # 私有状态（引擎跨 bar 调同一实例）
    _side: str = ""            # '' | 'long' | 'short'

    def __init__(self, entry_period: int | None = None,
                 exit_period:  int | None = None,
                 atr_sl_mult:  float | None = None):
        # 允许测试/敏感性分析时传参，实盘永远不传
        if entry_period is not None:
            self.entry_period = int(entry_period)
        if exit_period is not None:
            self.exit_period = int(exit_period)
        if atr_sl_mult is not None:
            self.atr_sl_mult = float(atr_sl_mult)
        self.warmup_bars = max(self.entry_period + 5, 60)
        self._side = ""

    # 供外部重置（新回测开始时）
    def reset(self) -> None:
        self._side = ""

    def generate_signal(self, df: pd.DataFrame) -> dict[str, Any]:
        """
        df: 包含到"本根收盘"为止的所有 K 线；索引 timestamp，列 open/high/low/close。
        返回：{"action": "BUY"|"SELL"|"EXIT"|"HOLD", "sl": float, "reason": str}
        """
        n_need = max(self.entry_period, self.exit_period, self.atr_period) + 2
        if len(df) < n_need:
            return {"action": "HOLD", "reason": "warming_up"}

        # 使用"前一根及之前"的通道判断，避免未来函数
        # 本根 i 的 entry_hi = max(high[i-entry_period:i])
        prev_slice = df.iloc[:-1]
        entry_hi = float(prev_slice["high"].rolling(self.entry_period).max().iat[-1])
        entry_lo = float(prev_slice["low"].rolling(self.entry_period).min().iat[-1])
        exit_hi  = float(prev_slice["high"].rolling(self.exit_period).max().iat[-1])
        exit_lo  = float(prev_slice["low"].rolling(self.exit_period).min().iat[-1])

        c = float(df["close"].iat[-1])

        # ATR (Wilder's)
        atr = self._compute_atr(df)

        # 1) 持仓中：优先判断反向突破平仓
        if self._side == "long":
            if c < exit_lo:
                self._side = ""
                return {"action": "EXIT",
                        "reason": f"close<{self.exit_period}d_low({exit_lo:.2f})"}
            return {"action": "HOLD", "reason": "long_holding"}

        if self._side == "short":
            if c > exit_hi:
                self._side = ""
                return {"action": "EXIT",
                        "reason": f"close>{self.exit_period}d_high({exit_hi:.2f})"}
            return {"action": "HOLD", "reason": "short_holding"}

        # 2) 空仓：判断开仓
        if c > entry_hi:
            self._side = "long"
            return {"action": "BUY",
                    "sl": c - self.atr_sl_mult * atr,
                    "reason": f"close>{self.entry_period}d_high({entry_hi:.2f})"}
        if c < entry_lo:
            self._side = "short"
            return {"action": "SELL",
                    "sl": c + self.atr_sl_mult * atr,
                    "reason": f"close<{self.entry_period}d_low({entry_lo:.2f})"}

        return {"action": "HOLD", "reason": "no_breakout"}

    def _compute_atr(self, df: pd.DataFrame) -> float:
        """计算最近 atr_period 的 Wilder ATR（只取最后一个值）。"""
        tail = df.tail(self.atr_period * 3 + 1)   # 足够的 warmup
        high, low, close = tail["high"], tail["low"], tail["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_series = tr.ewm(alpha=1.0 / self.atr_period, adjust=False).mean()
        return float(atr_series.iat[-1])
