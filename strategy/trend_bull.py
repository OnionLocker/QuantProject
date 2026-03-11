"""
strategy/trend_bull.py - 牛市趋势策略

核心思路：牛市中只做多（趋势追踪），但允许做空作为短线反转保护。
使用 EMA 金叉 + ADX 趋势确认 + 动量过滤，配合 ATR 动态止损。

信号体系：
  做多（主）：
    B1: EMA快线上穿慢线 + ADX > 阈值（金叉趋势启动）
    B2: 价格回踩EMA快线 + RSI从超卖区回升（趋势中的回调买点）
    B3: 突破近期高点 + 成交量放大（动量突破）

  做空（辅）：
    S1: EMA快线下穿慢线 + 价格大幅偏离后的急速回调（趋势末端反转保护）
    S2: RSI超买区背离 + 价格跌破EMA快线（短线见顶）

适用行情：价格在EMA慢线上方，ADX持续 > 25 的上升趋势。
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class TrendBullStrategy(BaseStrategy):
    """
    牛市趋势跟踪策略（支持多空，以做多为主）
    """

    PARAMS = [
        {
            "key": "ema_fast", "label": "快速EMA周期",
            "type": "int", "default": 8, "min": 5, "max": 30, "step": 1,
            "tip": "快线，用于金叉/死叉信号（BTC 1h 推荐 8，更灵敏）",
        },
        {
            "key": "ema_slow", "label": "慢速EMA周期",
            "type": "int", "default": 21, "min": 10, "max": 60, "step": 1,
            "tip": "慢线，趋势方向基准",
        },
        {
            "key": "ema_trend", "label": "趋势EMA周期",
            "type": "int", "default": 40, "min": 20, "max": 200, "step": 10,
            "tip": "大趋势参考线（BTC 1h 推荐 40 ≈ 不到2天，比 50 更灵敏）",
        },
        {
            "key": "adx_period", "label": "ADX周期",
            "type": "int", "default": 14, "min": 7, "max": 28, "step": 1,
            "tip": "趋势强度过滤，ADX > 阈值才入场",
        },
        {
            "key": "adx_threshold", "label": "ADX阈值",
            "type": "int", "default": 22, "min": 15, "max": 40, "step": 5,
            "tip": "趋势强度阈值（BTC 1h 推荐 22，加密市场 ADX 普遍偏低）",
        },
        {
            "key": "rsi_period", "label": "RSI周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
            "tip": "RSI用于超买超卖过滤",
        },
        {
            "key": "rsi_ob", "label": "RSI超买线",
            "type": "int", "default": 70, "min": 60, "max": 80, "step": 5,
            "tip": "RSI超过此值视为超买",
        },
        {
            "key": "rsi_os", "label": "RSI超卖线",
            "type": "int", "default": 38, "min": 20, "max": 50, "step": 5,
            "tip": "RSI低于此值视为超卖（BTC 1h 推荐 38，牛市回调不会太深）",
        },
        {
            "key": "atr_period", "label": "ATR周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
            "tip": "ATR用于计算动态止损",
        },
        {
            "key": "atr_sl_mult", "label": "ATR止损倍数",
            "type": "float", "default": 1.2, "min": 0.5, "max": 3.0, "step": 0.1,
            "tip": "止损 = 入场价 ± ATR × 倍数（BTC 1h 推荐 1.2，止损更紧凑）",
        },
        {
            "key": "rr1", "label": "止盈倍数 (TP1)",
            "type": "float", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "TP1 = 入场价 ± 止损距离 × 倍数（BTC 1h 推荐 1.5，加快止盈）",
        },
        {
            "key": "cooldown", "label": "信号冷却期",
            "type": "int", "default": 4, "min": 3, "max": 20, "step": 1,
            "tip": "两次信号之间最少间隔K线数（BTC 1h 推荐 4 ≈ 4小时）",
        },
    ]

    def __init__(
        self,
        ema_fast:      int   = 8,
        ema_slow:      int   = 21,
        ema_trend:     int   = 40,
        adx_period:    int   = 14,
        adx_threshold: int   = 22,
        rsi_period:    int   = 14,
        rsi_ob:        int   = 70,
        rsi_os:        int   = 38,
        atr_period:    int   = 14,
        atr_sl_mult:   float = 1.2,
        rr1:           float = 1.5,
        cooldown:      int   = 4,
    ):
        super().__init__(name="BULL_趋势跟踪牛市策略")
        self.ema_fast      = ema_fast
        self.ema_slow      = ema_slow
        self.ema_trend     = ema_trend
        self.adx_period    = adx_period
        self.adx_threshold = adx_threshold
        self.rsi_period    = rsi_period
        self.rsi_ob        = rsi_ob
        self.rsi_os        = rsi_os
        self.atr_period    = atr_period
        self.atr_sl_mult   = atr_sl_mult
        self.rr1           = rr1
        self.cooldown      = cooldown
        self.warmup_bars   = max(ema_trend, adx_period, rsi_period) + 20

    # ── 指标计算 ─────────────────────────────────────────────────────────────

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        c = df['close']
        h = df['high']
        l = df['low']

        df['ema_fast']  = c.ewm(span=self.ema_fast,  adjust=False).mean()
        df['ema_slow']  = c.ewm(span=self.ema_slow,  adjust=False).mean()
        df['ema_trend'] = c.ewm(span=self.ema_trend, adjust=False).mean()

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

        # ADX (简化版 Wilder)
        up_move   = h.diff()
        down_move = -l.diff()
        plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        atr_s     = tr.ewm(alpha=1/self.adx_period, adjust=False).mean()
        safe_atr  = atr_s.replace(0, np.nan)
        plus_di   = 100 * pd.Series(plus_dm,  index=df.index).ewm(
                        alpha=1/self.adx_period, adjust=False).mean() / safe_atr
        minus_di  = 100 * pd.Series(minus_dm, index=df.index).ewm(
                        alpha=1/self.adx_period, adjust=False).mean() / safe_atr
        di_sum    = plus_di + minus_di
        dx        = (100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan))
        df['adx'] = dx.ewm(alpha=1/self.adx_period, adjust=False).mean()

        return df

    # ── 预计算（回测高性能路径）──────────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._calc_indicators(df)

        EF  = df['ema_fast'].values
        ES  = df['ema_slow'].values
        ET  = df['ema_trend'].values
        RSI = df['rsi'].values
        ADX = df['adx'].values
        ATR = df['atr'].values
        H   = df['high'].values
        L   = df['low'].values
        C   = df['close'].values
        O   = df['open'].values
        n   = len(df)

        actions = ['HOLD'] * n
        sig_sl  = np.zeros(n)
        sig_tp1 = np.zeros(n)
        sig_tp2 = np.zeros(n)
        reasons = ['观望'] * n
        last_sig_i = -999

        for i in range(self.warmup_bars, n - 1):
            j = i - 1   # 已完结信号棒
            if i - last_sig_i < self.cooldown:
                continue
            if np.isnan(ATR[j]) or ATR[j] == 0 or np.isnan(ADX[j]):
                continue

            sl_dist = ATR[j] * self.atr_sl_mult
            trending = ADX[j] > self.adx_threshold

            # ── B1: EMA金叉 + ADX趋势确认 + 价格在趋势线上 ──────────────────
            if (j >= 1 and trending and
                    EF[j] > ES[j] and EF[j-1] <= ES[j-1] and
                    C[j] > ET[j]):
                actions[i] = 'BUY'
                sl = C[j] - sl_dist
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] + sl_dist * self.rr1
                sig_tp2[i] = C[j] + sl_dist * self.rr1 * 2
                reasons[i] = '🟢 B1: EMA金叉趋势启动'
                last_sig_i = i
                continue

            # ── B2: 趋势中回踩快线 + RSI超卖回升 ────────────────────────────
            if (trending and EF[j] > ET[j] and
                    L[j] <= EF[j] * 1.005 and C[j] > EF[j] and
                    j >= 1 and RSI[j] > RSI[j-1] and RSI[j-1] < self.rsi_os):
                actions[i] = 'BUY'
                sl = L[j] - ATR[j] * 0.5
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] + abs(C[j] - sl) * self.rr1
                sig_tp2[i] = C[j] + abs(C[j] - sl) * self.rr1 * 2
                reasons[i] = '🟢 B2: 趋势回踩EMA超卖回升'
                last_sig_i = i
                continue

            # ── B3: 突破近期高点 + 动量强劲 ─────────────────────────────────
            if j >= 20:
                recent_high = np.max(H[j-20:j])
                if (C[j] > recent_high and
                        C[j] > O[j] and
                        (C[j] - O[j]) > ATR[j] * 0.8 and
                        C[j] > ET[j]):
                    actions[i] = 'BUY'
                    sl = recent_high - ATR[j] * 0.5
                    sig_sl[i]  = sl
                    sig_tp1[i] = C[j] + abs(C[j] - sl) * self.rr1
                    sig_tp2[i] = C[j] + abs(C[j] - sl) * self.rr1 * 2
                    reasons[i] = '🟢 B3: 动量突破近期高点'
                    last_sig_i = i
                    continue

            # ── S1: 死叉 + 趋势转弱（保护性做空）────────────────────────────
            if (j >= 1 and
                    EF[j] < ES[j] and EF[j-1] >= ES[j-1] and
                    C[j] < ET[j] and ADX[j] > self.adx_threshold):
                actions[i] = 'SELL'
                sl = C[j] + sl_dist
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] - sl_dist * self.rr1
                sig_tp2[i] = C[j] - sl_dist * self.rr1 * 2
                reasons[i] = '🔴 S1: EMA死叉趋势转弱'
                last_sig_i = i
                continue

            # ── S2: RSI超买 + 跌破快线（短线见顶）───────────────────────────
            if (j >= 1 and RSI[j-1] > self.rsi_ob and
                    C[j] < EF[j] and C[j-1] >= EF[j-1]):
                actions[i] = 'SELL'
                sl = H[j] + ATR[j] * 0.5
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] - abs(sl - C[j]) * self.rr1
                sig_tp2[i] = C[j] - abs(sl - C[j]) * self.rr1 * 2
                reasons[i] = '🔴 S2: RSI超买跌破快线'
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
        EF  = df['ema_fast'].values
        ES  = df['ema_slow'].values
        ET  = df['ema_trend'].values
        RSI = df['rsi'].values
        ADX = df['adx'].values
        ATR = df['atr'].values
        H   = df['high'].values
        L   = df['low'].values
        C   = df['close'].values
        O   = df['open'].values
        if np.isnan(ATR[j]) or ATR[j] == 0:
            return sig

        sl_dist  = ATR[j] * self.atr_sl_mult
        trending = ADX[j] > self.adx_threshold
        entry    = df['open'].iloc[-1]

        # B1
        if (j >= 1 and trending and EF[j] > ES[j] and EF[j-1] <= ES[j-1] and C[j] > ET[j]):
            sl = C[j] - sl_dist
            sig.update({"action": "BUY", "entry": entry, "sl": sl,
                        "tp1": entry + sl_dist * self.rr1,
                        "tp2": entry + sl_dist * self.rr1 * 2,
                        "reason": "🟢 B1: EMA金叉趋势启动"})
            return sig
        # B2
        if (trending and EF[j] > ET[j] and L[j] <= EF[j] * 1.005 and C[j] > EF[j] and
                j >= 1 and RSI[j] > RSI[j-1] and RSI[j-1] < self.rsi_os):
            sl = L[j] - ATR[j] * 0.5
            risk = abs(entry - sl)
            sig.update({"action": "BUY", "entry": entry, "sl": sl,
                        "tp1": entry + risk * self.rr1,
                        "tp2": entry + risk * self.rr1 * 2,
                        "reason": "🟢 B2: 趋势回踩EMA超卖回升"})
            return sig
        # B3
        if j >= 20:
            recent_high = np.max(H[j-20:j])
            if (C[j] > recent_high and C[j] > O[j] and
                    (C[j] - O[j]) > ATR[j] * 0.8 and C[j] > ET[j]):
                sl = recent_high - ATR[j] * 0.5
                risk = abs(entry - sl)
                sig.update({"action": "BUY", "entry": entry, "sl": sl,
                            "tp1": entry + risk * self.rr1,
                            "tp2": entry + risk * self.rr1 * 2,
                            "reason": "🟢 B3: 动量突破近期高点"})
                return sig
        # S1
        if (j >= 1 and EF[j] < ES[j] and EF[j-1] >= ES[j-1] and
                C[j] < ET[j] and trending):
            sl = C[j] + sl_dist
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": entry - sl_dist * self.rr1,
                        "tp2": entry - sl_dist * self.rr1 * 2,
                        "reason": "🔴 S1: EMA死叉趋势转弱"})
            return sig
        # S2
        if (j >= 1 and RSI[j-1] > self.rsi_ob and C[j] < EF[j] and C[j-1] >= EF[j-1]):
            sl = H[j] + ATR[j] * 0.5
            risk = abs(sl - entry)
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": entry - risk * self.rr1,
                        "tp2": entry - risk * self.rr1 * 2,
                        "reason": "🔴 S2: RSI超买跌破快线"})
            return sig
        return sig
