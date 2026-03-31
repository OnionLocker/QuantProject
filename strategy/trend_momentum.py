# -*- coding: utf-8 -*-
"""
strategy/trend_momentum.py - TrendMomentum V2.0

BTC/USDT 1h real-money strategy.

Entry (all conditions must be met for LONG, mirror for SHORT):
  1. Previous close > Donchian channel upper + margin
  2. Previous close > slow EMA (trend direction)
  3. Fast EMA > slow EMA (crossover confirmation)
  4. ATR < ATR_MA * max_atr_mult (filter high-vol false breakouts)
  5. Volume > vol_MA * vol_filter (not dead market)

Exit:
  SL = ATR * atr_sl_mult
  TP = SL * rr_min
  Trailing stop disabled (proven to hurt returns in backtests).
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class TrendMomentumStrategy(BaseStrategy):

    PARAMS = [
        {"key": "channel_period", "label": "Donchian period",
         "type": "int", "default": 24, "min": 12, "max": 72, "step": 4,
         "tip": "Donchian channel lookback in bars (24 = 1 day on 1h)"},
        {"key": "fast_ema", "label": "Fast EMA",
         "type": "int", "default": 20, "min": 10, "max": 60, "step": 5,
         "tip": "Fast EMA for crossover confirmation"},
        {"key": "trend_ema", "label": "Slow EMA",
         "type": "int", "default": 120, "min": 50, "max": 200, "step": 10,
         "tip": "Slow EMA for trend direction"},
        {"key": "atr_sl_mult", "label": "SL ATR mult",
         "type": "float", "default": 3.0, "min": 1.5, "max": 5.0, "step": 0.5,
         "tip": "Stop loss distance = ATR * this value"},
        {"key": "rr_min", "label": "Min R:R",
         "type": "float", "default": 3.5, "min": 1.5, "max": 8.0, "step": 0.5,
         "tip": "Take profit = SL distance * this value"},
        {"key": "cooldown", "label": "Signal cooldown",
         "type": "int", "default": 16, "min": 4, "max": 30, "step": 2,
         "tip": "Minimum bars between signals"},
    ]

    def __init__(
        self,
        channel_period: int   = 24,
        fast_ema:       int   = 20,
        trend_ema:      int   = 120,
        atr_period:     int   = 14,
        atr_sl_mult:    float = 3.0,
        rr_min:         float = 3.5,
        cooldown:       int   = 16,
        vol_filter:     float = 0.7,
        atr_filter:     float = 0.5,
        max_atr_mult:   float = 1.3,
        breakout_margin: float = 0.0,
        exit_ema:       int   = 0,
    ):
        super().__init__(name="TM_TrendMomentum")
        self.channel_period  = channel_period
        self.fast_ema        = fast_ema
        self.trend_ema       = trend_ema
        self.atr_period      = atr_period
        self.atr_sl_mult     = atr_sl_mult
        self.rr_min          = rr_min
        self.cooldown        = cooldown
        self.vol_filter      = vol_filter
        self.atr_filter      = atr_filter
        self.max_atr_mult    = max_atr_mult
        self.breakout_margin = breakout_margin
        self.exit_ema        = exit_ema
        self.warmup_bars     = max(channel_period, trend_ema, atr_period,
                                   exit_ema if exit_ema > 0 else 0) + 30

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        H, L, C = df['high'], df['low'], df['close']
        prev_c = C.shift(1)
        tr = pd.concat([
            H - L,
            (H - prev_c).abs(),
            (L - prev_c).abs(),
        ], axis=1).max(axis=1)

        df['atr']          = tr.rolling(self.atr_period).mean()
        df['fast_ema']     = C.ewm(span=self.fast_ema, adjust=False).mean()
        df['trend_ema']    = C.ewm(span=self.trend_ema, adjust=False).mean()
        df['channel_high'] = H.rolling(self.channel_period).max().shift(1)
        df['channel_low']  = L.rolling(self.channel_period).min().shift(1)
        df['vol_ma']       = df['volume'].rolling(20).mean()
        df['atr_ma']       = df['atr'].rolling(30).mean()
        return df

    def _check_entry(self, j, C, ATR, CH, CL, FEMA, TEMA, VOL, VOL_MA, ATR_MA):
        """Check entry conditions at completed bar j. Returns 'BUY', 'SELL', or None."""
        if np.isnan(ATR[j]) or ATR[j] <= 0:
            return None
        if np.isnan(CH[j]) or np.isnan(CL[j]):
            return None
        if np.isnan(FEMA[j]) or np.isnan(TEMA[j]):
            return None

        if VOL_MA[j] > 0 and VOL[j] < VOL_MA[j] * self.vol_filter:
            return None
        if ATR_MA[j] > 0 and ATR[j] < ATR_MA[j] * self.atr_filter:
            return None
        if ATR_MA[j] > 0 and ATR[j] > ATR_MA[j] * self.max_atr_mult:
            return None

        margin = ATR[j] * self.breakout_margin

        if (C[j] > CH[j] + margin
                and C[j] > TEMA[j]
                and FEMA[j] > TEMA[j]):
            return 'BUY'

        if (C[j] < CL[j] - margin
                and C[j] < TEMA[j]
                and FEMA[j] < TEMA[j]):
            return 'SELL'

        return None

    # -- Backtest fast path -------------------------------------------------

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._calc_indicators(df)
        n = len(df)

        sig_action = np.full(n, 'HOLD', dtype=object)
        sig_sl     = np.zeros(n)
        sig_tp1    = np.zeros(n)
        sig_tp2    = np.zeros(n)
        sig_reason = np.full(n, 'wait', dtype=object)

        C      = df['close'].values
        O      = df['open'].values
        ATR    = df['atr'].values
        CH     = df['channel_high'].values
        CL     = df['channel_low'].values
        FEMA   = df['fast_ema'].values
        TEMA   = df['trend_ema'].values
        VOL    = df['volume'].values
        VOL_MA = df['vol_ma'].values
        ATR_MA = df['atr_ma'].values

        last_sig_i = -self.cooldown - 1

        for i in range(self.warmup_bars, n):
            if i - last_sig_i <= self.cooldown:
                continue

            j = i - 1
            action = self._check_entry(
                j, C, ATR, CH, CL, FEMA, TEMA, VOL, VOL_MA, ATR_MA
            )
            if action is None:
                continue

            sl_dist = ATR[j] * self.atr_sl_mult
            entry   = O[i]

            if action == 'BUY':
                sig_action[i] = 'BUY'
                sig_sl[i]     = entry - sl_dist
                sig_tp1[i]    = entry + sl_dist * self.rr_min
                sig_reason[i] = (
                    f"LONG breakout | "
                    f"C={C[j]:.0f}>H={CH[j]:.0f}, "
                    f"FEMA>{TEMA[j]:.0f}"
                )
            else:
                sig_action[i] = 'SELL'
                sig_sl[i]     = entry + sl_dist
                sig_tp1[i]    = entry - sl_dist * self.rr_min
                sig_reason[i] = (
                    f"SHORT breakout | "
                    f"C={C[j]:.0f}<L={CL[j]:.0f}, "
                    f"FEMA<{TEMA[j]:.0f}"
                )
            last_sig_i = i

        df['sig_action'] = sig_action
        df['sig_sl']     = sig_sl
        df['sig_tp1']    = sig_tp1
        df['sig_tp2']    = sig_tp2
        df['sig_reason'] = sig_reason
        return df

    def signal_from_row(self, df: pd.DataFrame, i: int) -> dict:
        row = df.iloc[i]
        return {
            "action": row['sig_action'],
            "entry":  float(row['open']),
            "sl":     float(row['sig_sl']),
            "tp1":    float(row['sig_tp1']),
            "tp2":    float(row.get('sig_tp2', 0.0)),
            "risk_r": 0.0,
            "reason": row['sig_reason'],
            "meta":   {},
        }

    # -- Live trading interface ---------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {
            "action": "HOLD", "entry": 0.0, "sl": 0.0,
            "tp1": 0.0, "tp2": 0.0, "risk_r": 0.0,
            "reason": "wait", "meta": {},
        }

        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig

        df = self._calc_indicators(df.iloc[-need:].copy())
        j = len(df) - 2
        if j < 1:
            return sig

        C    = df['close'].values
        ATR  = df['atr'].values
        CH   = df['channel_high'].values
        CL   = df['channel_low'].values
        FEMA = df['fast_ema'].values
        TEMA = df['trend_ema'].values
        VOL  = df['volume'].values
        VOL_MA = df['vol_ma'].values
        ATR_MA = df['atr_ma'].values

        action = self._check_entry(
            j, C, ATR, CH, CL, FEMA, TEMA, VOL, VOL_MA, ATR_MA
        )
        if action is None:
            return sig

        sl_dist = ATR[j] * self.atr_sl_mult
        entry   = float(df['open'].iloc[-1])

        use_ema_exit = self.exit_ema > 0
        tp1_long  = 0.0 if use_ema_exit else entry + sl_dist * self.rr_min
        tp1_short = 0.0 if use_ema_exit else entry - sl_dist * self.rr_min

        if action == 'BUY':
            sig.update({
                "action": "BUY",
                "entry":  entry,
                "sl":     entry - sl_dist,
                "tp1":    tp1_long,
                "reason": (
                    f"LONG breakout | "
                    f"C={C[j]:.0f}>H={CH[j]:.0f}, "
                    f"FEMA>{TEMA[j]:.0f}"
                ),
                "meta": {"exit_ema": self.exit_ema,
                         "breakeven_r": 1.0} if use_ema_exit else {},
            })
        else:
            sig.update({
                "action": "SELL",
                "entry":  entry,
                "sl":     entry + sl_dist,
                "tp1":    tp1_short,
                "reason": (
                    f"SHORT breakout | "
                    f"C={C[j]:.0f}<L={CL[j]:.0f}, "
                    f"FEMA<{TEMA[j]:.0f}"
                ),
                "meta": {"exit_ema": self.exit_ema,
                         "breakeven_r": 1.0} if use_ema_exit else {},
            })

        return sig
