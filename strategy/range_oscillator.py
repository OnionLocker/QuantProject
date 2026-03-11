"""
strategy/range_oscillator.py - 震荡市策略

核心思路：震荡行情中用布林带 + RSI 做均值回归，双向交易。
价格触及布林带边缘 + RSI 超买/超卖 → 反向入场，中轨止盈。

信号体系：
  做多（均值回归）：
    R1: 价格触及下轨 + RSI超卖 + K线收盘反弹（布林带下轨反弹）
    R2: 价格在下轨附近 + KDJ金叉 + RSI未超卖（动能转向做多）

  做空（均值回归）：
    R3: 价格触及上轨 + RSI超买 + K线收盘回落（布林带上轨回落）
    R4: 价格在上轨附近 + KDJ死叉 + RSI未超买（动能转向做空）

止盈目标：中轨（均值），止损：突破上/下轨外 0.5×ATR
适用行情：ADX < 25 的低趋势环境，价格在布林带内震荡。
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class RangeOscillatorStrategy(BaseStrategy):
    """
    震荡市均值回归策略（布林带 + RSI + KDJ，双向交易）
    """

    PARAMS = [
        {
            "key": "bb_period", "label": "布林带周期",
            "type": "int", "default": 20, "min": 10, "max": 40, "step": 5,
            "tip": "布林带中轨SMA周期",
        },
        {
            "key": "bb_std", "label": "布林带标准差倍数",
            "type": "float", "default": 2.0, "min": 1.5, "max": 3.0, "step": 0.5,
            "tip": "上下轨 = 中轨 ± N倍标准差",
        },
        {
            "key": "rsi_period", "label": "RSI周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "rsi_ob", "label": "RSI超买线",
            "type": "int", "default": 65, "min": 55, "max": 75, "step": 5,
            "tip": "震荡行情中适当降低超买阈值",
        },
        {
            "key": "rsi_os", "label": "RSI超卖线",
            "type": "int", "default": 35, "min": 25, "max": 45, "step": 5,
        },
        {
            "key": "kdj_period", "label": "KDJ周期",
            "type": "int", "default": 9, "min": 5, "max": 14, "step": 1,
            "tip": "KDJ随机指标周期",
        },
        {
            "key": "atr_period", "label": "ATR周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "atr_sl_mult", "label": "ATR止损倍数",
            "type": "float", "default": 0.8, "min": 0.3, "max": 2.0, "step": 0.1,
            "tip": "震荡策略止损更紧（BTC 1h 推荐 0.8，快进快出）",
        },
        {
            "key": "cooldown", "label": "信号冷却期",
            "type": "int", "default": 4, "min": 3, "max": 15, "step": 1,
            "tip": "两次信号之间最少间隔K线数（BTC 1h 推荐 4）",
        },
    ]

    def __init__(
        self,
        bb_period:   int   = 20,
        bb_std:      float = 2.0,
        rsi_period:  int   = 14,
        rsi_ob:      int   = 65,
        rsi_os:      int   = 35,
        kdj_period:  int   = 9,
        atr_period:  int   = 14,
        atr_sl_mult: float = 0.8,
        cooldown:    int   = 4,
    ):
        super().__init__(name="RANGE_震荡均值回归策略")
        self.bb_period   = bb_period
        self.bb_std      = bb_std
        self.rsi_period  = rsi_period
        self.rsi_ob      = rsi_ob
        self.rsi_os      = rsi_os
        self.kdj_period  = kdj_period
        self.atr_period  = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.cooldown    = cooldown
        self.warmup_bars = max(bb_period, rsi_period, kdj_period * 3) + 20

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        c = df['close']
        h = df['high']
        l = df['low']

        # 布林带
        df['bb_mid']   = c.rolling(self.bb_period).mean()
        bb_std         = c.rolling(self.bb_period).std()
        df['bb_upper'] = df['bb_mid'] + self.bb_std * bb_std
        df['bb_lower'] = df['bb_mid'] - self.bb_std * bb_std
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid']  # 带宽（归一化）

        # RSI
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, np.nan)
        df['rsi'] = 100 - (100 / (1 + rs))

        # ATR
        prev_c = c.shift(1)
        tr = pd.concat([
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs(),
        ], axis=1).max(axis=1)
        df['atr'] = tr.rolling(self.atr_period).mean()

        # KDJ（随机指标）
        period = self.kdj_period
        low_min  = l.rolling(period).min()
        high_max = h.rolling(period).max()
        rsv = 100 * (c - low_min) / (high_max - low_min).replace(0, np.nan)
        df['kdj_k'] = rsv.ewm(com=2, adjust=False).mean()   # K线
        df['kdj_d'] = df['kdj_k'].ewm(com=2, adjust=False).mean()  # D线
        df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']

        return df

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._calc_indicators(df)

        BB_U = df['bb_upper'].values
        BB_L = df['bb_lower'].values
        BB_M = df['bb_mid'].values
        RSI  = df['rsi'].values
        ATR  = df['atr'].values
        KK   = df['kdj_k'].values
        KD   = df['kdj_d'].values
        H    = df['high'].values
        L    = df['low'].values
        C    = df['close'].values
        n    = len(df)

        actions = ['HOLD'] * n
        sig_sl  = np.zeros(n)
        sig_tp1 = np.zeros(n)
        sig_tp2 = np.zeros(n)
        reasons = ['观望'] * n
        last_sig_i = -999

        for i in range(self.warmup_bars, n - 1):
            j = i - 1
            if i - last_sig_i < self.cooldown:
                continue
            if np.isnan(ATR[j]) or ATR[j] == 0 or np.isnan(BB_U[j]):
                continue

            sl_dist = ATR[j] * self.atr_sl_mult

            # ── R1: 布林带下轨 + RSI超卖 + 阳线收盘 ─────────────────────────
            if (L[j] <= BB_L[j] and C[j] > BB_L[j] and
                    RSI[j] < self.rsi_os and C[j] > df['open'].values[j]):
                actions[i] = 'BUY'
                sl = BB_L[j] - sl_dist
                sig_sl[i]  = sl
                sig_tp1[i] = BB_M[j]                    # 止盈1：中轨
                sig_tp2[i] = BB_M[j] + (BB_M[j] - sl)  # 止盈2：对称中轨以上
                reasons[i] = '🟢 R1: 布林下轨RSI超卖反弹'
                last_sig_i = i
                continue

            # ── R2: 下轨附近 + KDJ金叉 ───────────────────────────────────────
            if (j >= 1 and C[j] < BB_M[j] and C[j] > BB_L[j] and
                    KK[j] > KD[j] and KK[j-1] <= KD[j-1] and
                    KK[j] < 40):   # KDJ在低位金叉
                actions[i] = 'BUY'
                sl = BB_L[j] - sl_dist
                risk = abs(C[j] - sl)
                sig_sl[i]  = sl
                sig_tp1[i] = BB_M[j]
                sig_tp2[i] = BB_U[j]
                reasons[i] = '🟢 R2: KDJ低位金叉做多'
                last_sig_i = i
                continue

            # ── R3: 布林带上轨 + RSI超买 + 阴线收盘 ─────────────────────────
            if (H[j] >= BB_U[j] and C[j] < BB_U[j] and
                    RSI[j] > self.rsi_ob and C[j] < df['open'].values[j]):
                actions[i] = 'SELL'
                sl = BB_U[j] + sl_dist
                sig_sl[i]  = sl
                sig_tp1[i] = BB_M[j]
                sig_tp2[i] = BB_M[j] - (sl - BB_M[j])
                reasons[i] = '🔴 R3: 布林上轨RSI超买回落'
                last_sig_i = i
                continue

            # ── R4: 上轨附近 + KDJ死叉 ───────────────────────────────────────
            if (j >= 1 and C[j] > BB_M[j] and C[j] < BB_U[j] and
                    KK[j] < KD[j] and KK[j-1] >= KD[j-1] and
                    KK[j] > 60):   # KDJ在高位死叉
                actions[i] = 'SELL'
                sl = BB_U[j] + sl_dist
                risk = abs(sl - C[j])
                sig_sl[i]  = sl
                sig_tp1[i] = BB_M[j]
                sig_tp2[i] = BB_L[j]
                reasons[i] = '🔴 R4: KDJ高位死叉做空'
                last_sig_i = i

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

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}
        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig
        df = self._calc_indicators(df.iloc[-need:].copy())
        j = len(df) - 2
        BB_U = df['bb_upper'].values
        BB_L = df['bb_lower'].values
        BB_M = df['bb_mid'].values
        RSI  = df['rsi'].values
        ATR  = df['atr'].values
        KK   = df['kdj_k'].values
        KD   = df['kdj_d'].values
        H    = df['high'].values
        L    = df['low'].values
        C    = df['close'].values
        O    = df['open'].values
        if np.isnan(ATR[j]) or ATR[j] == 0 or np.isnan(BB_U[j]):
            return sig

        sl_dist = ATR[j] * self.atr_sl_mult
        entry   = df['open'].iloc[-1]

        if (L[j] <= BB_L[j] and C[j] > BB_L[j] and RSI[j] < self.rsi_os and C[j] > O[j]):
            sl = BB_L[j] - sl_dist
            sig.update({"action": "BUY", "entry": entry, "sl": sl,
                        "tp1": BB_M[j], "tp2": BB_M[j] + (BB_M[j] - sl),
                        "reason": "🟢 R1: 布林下轨RSI超卖反弹"})
            return sig
        if (j >= 1 and C[j] < BB_M[j] and C[j] > BB_L[j] and
                KK[j] > KD[j] and KK[j-1] <= KD[j-1] and KK[j] < 40):
            sl = BB_L[j] - sl_dist
            sig.update({"action": "BUY", "entry": entry, "sl": sl,
                        "tp1": BB_M[j], "tp2": BB_U[j],
                        "reason": "🟢 R2: KDJ低位金叉做多"})
            return sig
        if (H[j] >= BB_U[j] and C[j] < BB_U[j] and RSI[j] > self.rsi_ob and C[j] < O[j]):
            sl = BB_U[j] + sl_dist
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": BB_M[j], "tp2": BB_M[j] - (sl - BB_M[j]),
                        "reason": "🔴 R3: 布林上轨RSI超买回落"})
            return sig
        if (j >= 1 and C[j] > BB_M[j] and C[j] < BB_U[j] and
                KK[j] < KD[j] and KK[j-1] >= KD[j-1] and KK[j] > 60):
            sl = BB_U[j] + sl_dist
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": BB_M[j], "tp2": BB_L[j],
                        "reason": "🔴 R4: KDJ高位死叉做空"})
            return sig
        return sig
