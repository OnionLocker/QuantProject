"""
scripts/run_baselines.py — 跑三条基准曲线

所有基准都用 3x 杠杆、0.5% 单笔风险，和目标策略对齐，便于对比：
  1. BuyHold 3x  — 一开仓就持有到底（会爆仓）
  2. Donchian 20/10  — 短周期海龟
  3. EMA 20/50 cross  — 经典移动均线交叉

每个基准的报告落到 reports/<stamp>_baseline_<name>/。
"""
from __future__ import annotations
import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import pandas as pd
import numpy as np

from backtest.engine_v2 import run_backtest, BacktestConfig
from backtest.report import save_report


DATA_PATH = os.path.join(project_root, "data", "cache", "BTC-USDT-USDT_4h.parquet")


# ─────────── 策略实现 ───────────

class BuyHoldStrategy:
    """第一根就开多，不主动出，依赖引擎的强平/止损。"""
    warmup_bars = 100
    atr_period = 14
    _opened = False

    def generate_signal(self, df: pd.DataFrame) -> dict:
        if not self._opened:
            self._opened = True
            c = float(df["close"].iat[-1])
            return {"action": "BUY", "sl": c * 0.1, "reason": "buyhold"}
        return {"action": "HOLD"}


class DonchianBaseline:
    """Donchian 20/10：20 根最高突破开多、最低突破开空；10 根反向突破平仓。"""
    warmup_bars = 30
    atr_period = 14
    entry_period = 20
    exit_period  = 10
    atr_sl_mult  = 2.0

    # 外部可读的当前仓位方向（引擎不关心，这里只作内部状态给 exit 判断）
    _side: str = ""   # '' | 'long' | 'short'

    def generate_signal(self, df: pd.DataFrame) -> dict:
        if len(df) < max(self.entry_period, self.exit_period) + 2:
            return {"action": "HOLD"}

        # 用前一根收盘判断（避免未来函数）— 引擎调用时 df 含本根收盘，我们需要"上一根及之前"的通道
        prev = df.iloc[-1]           # 最新一根
        hist = df.iloc[:-1]          # 之前的

        entry_hi = hist["high"].rolling(self.entry_period).max().iat[-1]
        entry_lo = hist["low"].rolling(self.entry_period).min().iat[-1]
        exit_hi  = hist["high"].rolling(self.exit_period).max().iat[-1]
        exit_lo  = hist["low"].rolling(self.exit_period).min().iat[-1]

        c    = float(prev["close"])
        # ATR 近似（同引擎的计算）
        tr_df = df.tail(self.atr_period + 1)
        prev_close = tr_df["close"].shift(1)
        tr = pd.concat([
            (tr_df["high"] - tr_df["low"]).abs(),
            (tr_df["high"] - prev_close).abs(),
            (tr_df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1.0 / self.atr_period, adjust=False).mean().iat[-1])

        # 如果持有仓位，优先判断退出
        if self._side == "long" and c < exit_lo:
            self._side = ""
            return {"action": "EXIT", "reason": "donchian_exit_long"}
        if self._side == "short" and c > exit_hi:
            self._side = ""
            return {"action": "EXIT", "reason": "donchian_exit_short"}

        # 无仓位：判断开仓
        if self._side == "":
            if c > entry_hi:
                self._side = "long"
                return {"action": "BUY",  "sl": c - self.atr_sl_mult * atr,
                        "reason": f"donchian_long {self.entry_period}"}
            if c < entry_lo:
                self._side = "short"
                return {"action": "SELL", "sl": c + self.atr_sl_mult * atr,
                        "reason": f"donchian_short {self.entry_period}"}
        return {"action": "HOLD"}


class EMACrossBaseline:
    """EMA 20/50 交叉：快上穿慢开多，快下穿慢平/开空。"""
    warmup_bars = 100
    atr_period = 14
    fast = 20
    slow = 50
    atr_sl_mult = 2.5

    _side: str = ""

    def generate_signal(self, df: pd.DataFrame) -> dict:
        if len(df) < self.slow + 2:
            return {"action": "HOLD"}

        ema_fast = df["close"].ewm(span=self.fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=self.slow, adjust=False).mean()

        prev_fast = float(ema_fast.iat[-2])
        prev_slow = float(ema_slow.iat[-2])
        cur_fast  = float(ema_fast.iat[-1])
        cur_slow  = float(ema_slow.iat[-1])

        c = float(df["close"].iat[-1])
        # ATR
        tr_df = df.tail(self.atr_period + 1)
        prev_close = tr_df["close"].shift(1)
        tr = pd.concat([
            (tr_df["high"] - tr_df["low"]).abs(),
            (tr_df["high"] - prev_close).abs(),
            (tr_df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1.0 / self.atr_period, adjust=False).mean().iat[-1])

        up_cross   = prev_fast <= prev_slow and cur_fast > cur_slow
        down_cross = prev_fast >= prev_slow and cur_fast < cur_slow

        # 持仓中遇反向交叉：先 EXIT，下一根再开反向（两步法，匹配实盘）
        if self._side == "long" and down_cross:
            self._side = ""
            return {"action": "EXIT", "reason": "ema_down_cross_exit"}
        if self._side == "short" and up_cross:
            self._side = ""
            return {"action": "EXIT", "reason": "ema_up_cross_exit"}

        if self._side == "" and up_cross:
            self._side = "long"
            return {"action": "BUY",  "sl": c - self.atr_sl_mult * atr, "reason": "ema_up_cross"}
        if self._side == "" and down_cross:
            self._side = "short"
            return {"action": "SELL", "sl": c + self.atr_sl_mult * atr, "reason": "ema_down_cross"}

        return {"action": "HOLD"}


# ─────────── 主流程 ───────────

def load_data() -> pd.DataFrame:
    if not os.path.exists(DATA_PATH):
        print(f"[run_baselines] 未找到 {DATA_PATH}，请先上传数据", file=sys.stderr)
        sys.exit(1)
    df = pd.read_parquet(DATA_PATH)
    print(f"[run_baselines] 加载 {len(df)} 根 K 线 {df.index.min()} → {df.index.max()}")
    return df


def main() -> int:
    df = load_data()
    cfg = BacktestConfig(
        initial_capital    = 10_000.0,
        leverage           = 3,
        risk_per_trade_pct = 0.005,    # 0.5%
        taker_fee_rate     = 0.0005,
        slippage_base      = 0.0002,
        slippage_max       = 0.003,
        funding_rate_8h    = 0.0,      # 缺少真实历史 funding；保守设 0
        allow_short        = True,
    )

    baselines = [
        ("buyhold_3x",     BuyHoldStrategy()),
        ("donchian_20_10", DonchianBaseline()),
        ("ema_20_50",      EMACrossBaseline()),
    ]

    for name, strat in baselines:
        print("\n" + "=" * 60)
        print(f"  跑基准: {name}")
        print("=" * 60)
        result = run_backtest(strat, df, cfg)
        save_report(result, note=f"baseline_{name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
