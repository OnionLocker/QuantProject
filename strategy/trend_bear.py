"""
strategy/trend_bear.py - 熊市趋势策略

与 TrendBullStrategy 完全对称，熊市中以做空为主，做多作为反弹保护。

信号体系：
  做空（主）：
    S1: EMA快线下穿慢线 + ADX > 阈值（死叉趋势启动）
    S2: 价格反弹至EMA快线 + RSI从超买区回落（趋势中的反弹空点）
    S3: 跌破近期低点 + 动量强劲（动量跌破）

  做多（辅）：
    B1: EMA快线上穿慢线 + 价格偏离后强力反弹（短线底部反转保护）
    B2: RSI超卖区背离 + 价格收复EMA快线（短线见底）

适用行情：价格在EMA慢线下方，ADX持续 > 25 的下降趋势。
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class TrendBearStrategy(BaseStrategy):
    """
    熊市趋势跟踪策略（支持多空，以做空为主）
    """

    PARAMS = [
        {
            "key": "ema_fast", "label": "快速EMA周期",
            "type": "int", "default": 8, "min": 5, "max": 30, "step": 1,
            "tip": "快线，用于金叉/死叉信号（BTC 1h 推荐 8）",
        },
        {
            "key": "ema_slow", "label": "慢速EMA周期",
            "type": "int", "default": 21, "min": 10, "max": 60, "step": 1,
            "tip": "慢线，趋势方向基准",
        },
        {
            "key": "ema_trend", "label": "趋势EMA周期",
            "type": "int", "default": 40, "min": 20, "max": 200, "step": 10,
            "tip": "大趋势参考线（BTC 1h 推荐 40）",
        },
        {
            "key": "adx_period", "label": "ADX周期",
            "type": "int", "default": 14, "min": 7, "max": 28, "step": 1,
            "tip": "趋势强度过滤",
        },
        {
            "key": "adx_threshold", "label": "ADX阈值",
            "type": "int", "default": 22, "min": 15, "max": 40, "step": 5,
            "tip": "趋势强度阈值（BTC 1h 推荐 22）",
        },
        {
            "key": "rsi_period", "label": "RSI周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "rsi_ob", "label": "RSI超买线",
            "type": "int", "default": 62, "min": 55, "max": 80, "step": 5,
            "tip": "熊市中超买阈值适当降低（BTC 1h 推荐 62）",
        },
        {
            "key": "rsi_os", "label": "RSI超卖线",
            "type": "int", "default": 30, "min": 20, "max": 45, "step": 5,
        },
        {
            "key": "atr_period", "label": "ATR周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "atr_sl_mult", "label": "ATR止损倍数",
            "type": "float", "default": 1.2, "min": 0.5, "max": 3.0, "step": 0.1,
            "tip": "止损 = 入场价 ± ATR × 倍数（BTC 1h 推荐 1.2）",
        },
        {
            "key": "rr1", "label": "止盈倍数 (TP1)",
            "type": "float", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "TP1 = 入场价 ± 止损距离 × 倍数（BTC 1h 推荐 1.5）",
        },
        {
            "key": "cooldown", "label": "信号冷却期",
            "type": "int", "default": 4, "min": 3, "max": 20, "step": 1,
            "tip": "两次信号之间最少间隔K线数（BTC 1h 推荐 4）",
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
        rsi_ob:        int   = 62,
        rsi_os:        int   = 30,
        atr_period:    int   = 14,
        atr_sl_mult:   float = 1.2,
        rr1:           float = 1.5,
        cooldown:      int   = 4,
    ):
        super().__init__(name="BEAR_趋势跟踪熊市策略")
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

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        c = df['close']
        h = df['high']
        l = df['low']

        df['ema_fast']  = c.ewm(span=self.ema_fast,  adjust=False).mean()
        df['ema_slow']  = c.ewm(span=self.ema_slow,  adjust=False).mean()
        df['ema_trend'] = c.ewm(span=self.ema_trend, adjust=False).mean()

        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, np.nan)
        df['rsi'] = 100 - (100 / (1 + rs))

        prev_c = c.shift(1)
        tr = pd.concat([
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs(),
        ], axis=1).max(axis=1)
        df['atr'] = tr.rolling(self.atr_period).mean()

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

        # V4.0: 成交量均线（用于突破信号的量能确认）
        if 'volume' in df.columns:
            df['vol_ma'] = df['volume'].rolling(20).mean()
        else:
            df['vol_ma'] = np.nan

        return df

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

        # V4.0: 成交量确认
        has_vol = 'volume' in df.columns and not df['vol_ma'].isna().all()
        if has_vol:
            VOL    = df['volume'].values
            VOL_MA = df['vol_ma'].values
        else:
            VOL = VOL_MA = np.zeros(n)

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
            if np.isnan(ATR[j]) or ATR[j] == 0 or np.isnan(ADX[j]):
                continue

            sl_dist  = ATR[j] * self.atr_sl_mult
            trending = ADX[j] > self.adx_threshold

            # V5.0: 成交量比率
            vol_ratio = VOL[j] / VOL_MA[j] if has_vol and VOL_MA[j] > 0 else 1.0

            # ── S1: EMA死叉 (V5.1: vol_ratio改为加分项) ──────────────
            if (j >= 1 and trending and
                    EF[j] < ES[j] and EF[j-1] >= ES[j-1] and
                    C[j] < ES[j]):
                actions[i] = 'SELL'
                sl = C[j] + sl_dist
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] - sl_dist * self.rr1
                sig_tp2[i] = C[j] - sl_dist * self.rr1 * 2
                vol_tag = "（量能确认）" if vol_ratio > 0.6 else "（缩量）"
                reasons[i] = f'🔴 S1: EMA死叉趋势启动{vol_tag}'
                last_sig_i = i
                continue

            # ── S2: 反弹回踩 (V5.0: 放宽反弹范围到1.5%) ────────────────
            if (trending and EF[j] < ES[j] and
                    H[j] >= EF[j] * 0.985 and C[j] < EF[j] * 1.005 and
                    j >= 1 and RSI[j] < RSI[j-1] and RSI[j-1] > self.rsi_ob):
                actions[i] = 'SELL'
                sl = H[j] + ATR[j] * 0.5
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] - abs(sl - C[j]) * self.rr1
                sig_tp2[i] = C[j] - abs(sl - C[j]) * self.rr1 * 2
                reasons[i] = '🔴 S2: 趋势反弹EMA超买回落'
                last_sig_i = i
                continue

            # ── S3: 跌破近期低点 (V5.0: 实体0.5ATR, 放量1.0) ──────────
            if j >= 20:
                recent_low = np.min(L[j-20:j])
                if (C[j] < recent_low and
                        C[j] < O[j] and
                        (O[j] - C[j]) > ATR[j] * 0.5 and
                        C[j] < ES[j] and vol_ratio > 1.0):
                    actions[i] = 'SELL'
                    sl = recent_low + ATR[j] * 0.5
                    sig_sl[i]  = sl
                    sig_tp1[i] = C[j] - abs(sl - C[j]) * self.rr1
                    sig_tp2[i] = C[j] - abs(sl - C[j]) * self.rr1 * 2
                    reasons[i] = '🔴 S3: 动量跌破近期低点'
                    last_sig_i = i
                    continue

            # ── S4 (V5.0 新增): 均线空头+RSI顺势回落 ─────────────────
            if (j >= 2 and EF[j] < ES[j] and EF[j] < EF[j-2] and
                    C[j] < EF[j] and C[j] < O[j] and
                    35 < RSI[j] < 60 and RSI[j] < RSI[j-1]):
                actions[i] = 'SELL'
                sl = max(H[j], H[j-1]) + ATR[j] * 0.5
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] - abs(sl - C[j]) * self.rr1
                sig_tp2[i] = C[j] - abs(sl - C[j]) * self.rr1 * 2
                reasons[i] = '🔴 S4: 均线空头+RSI顺势回落'
                last_sig_i = i
                continue

            # ── B1: 金叉反转 (V5.1: vol_ratio改为加分项) ────────────────
            if (j >= 1 and
                    EF[j] > ES[j] and EF[j-1] <= ES[j-1] and
                    C[j] > ES[j] and trending):
                actions[i] = 'BUY'
                sl = C[j] - sl_dist
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] + sl_dist * self.rr1
                sig_tp2[i] = C[j] + sl_dist * self.rr1 * 2
                vol_tag = "（量能确认）" if vol_ratio > 0.6 else "（缩量）"
                reasons[i] = f'🟢 B1: EMA金叉趋势反转{vol_tag}'
                last_sig_i = i
                continue

            # ── B2: RSI超卖 + 收复快线（短线见底）───────────────────────────
            if (j >= 1 and RSI[j-1] < self.rsi_os and
                    C[j] > EF[j] and C[j-1] <= EF[j-1]):
                actions[i] = 'BUY'
                sl = L[j] - ATR[j] * 0.5
                risk = abs(C[j] - sl)
                sig_sl[i]  = sl
                sig_tp1[i] = C[j] + risk * self.rr1
                sig_tp2[i] = C[j] + risk * self.rr1 * 2
                reasons[i] = '🟢 B2: RSI超卖收复快线'
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

        # V5.0: 成交量比率
        vol_ratio = 1.0
        if 'volume' in df.columns and 'vol_ma' in df.columns:
            vol_ma_val = df['vol_ma'].values[j]
            if not np.isnan(vol_ma_val) and vol_ma_val > 0:
                vol_ratio = df['volume'].values[j] / vol_ma_val

        # S1: EMA死叉 (V5.1: vol_ratio改为加分项，无量也可入场)
        if (j >= 1 and trending and EF[j] < ES[j] and EF[j-1] >= ES[j-1]
                and C[j] < ES[j]):
            sl = C[j] + sl_dist
            vol_tag = "（量能确认）" if vol_ratio > 0.6 else "（缩量）"
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": entry - sl_dist * self.rr1,
                        "tp2": entry - sl_dist * self.rr1 * 2,
                        "reason": f"🔴 S1: EMA死叉趋势启动{vol_tag}"})
            return sig

        # S2: 反弹回踩 (V5.0: 放宽反弹范围到1.5%)
        if (trending and EF[j] < ES[j] and
                H[j] >= EF[j] * 0.985 and C[j] < EF[j] * 1.005 and
                j >= 1 and RSI[j] < RSI[j-1] and RSI[j-1] > self.rsi_ob):
            sl = H[j] + ATR[j] * 0.5
            risk = abs(sl - entry)
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": entry - risk * self.rr1,
                        "tp2": entry - risk * self.rr1 * 2,
                        "reason": "🔴 S2: 趋势反弹EMA超买回落"})
            return sig

        # S3: 跌破近期低点 (V5.0: 放量要求 1.2→1.0, 实体 0.8→0.5 ATR)
        if j >= 20:
            recent_low = np.min(L[j-20:j])
            if (C[j] < recent_low and C[j] < O[j] and
                    (O[j] - C[j]) > ATR[j] * 0.5 and C[j] < ES[j]
                    and vol_ratio > 1.0):
                sl = recent_low + ATR[j] * 0.5
                risk = abs(sl - entry)
                sig.update({"action": "SELL", "entry": entry, "sl": sl,
                            "tp1": entry - risk * self.rr1,
                            "tp2": entry - risk * self.rr1 * 2,
                            "reason": "🔴 S3: 动量跌破近期低点"})
                return sig

        # S4 (V5.0 新增): 均线空头+RSI顺势回落
        if (j >= 2 and EF[j] < ES[j] and EF[j] < EF[j-2] and
                C[j] < EF[j] and C[j] < O[j] and
                35 < RSI[j] < 60 and RSI[j] < RSI[j-1]):
            sl = max(H[j], H[j-1]) + ATR[j] * 0.5
            risk = abs(sl - entry)
            sig.update({"action": "SELL", "entry": entry, "sl": sl,
                        "tp1": entry - risk * self.rr1,
                        "tp2": entry - risk * self.rr1 * 2,
                        "reason": "🔴 S4: 均线空头+RSI顺势回落"})
            return sig

        # B1: 金叉反转 (V5.1: vol_ratio改为加分项)
        if (j >= 1 and EF[j] > ES[j] and EF[j-1] <= ES[j-1]
                and C[j] > ES[j] and trending):
            sl = C[j] - sl_dist
            vol_tag = "（量能确认）" if vol_ratio > 0.6 else "（缩量）"
            sig.update({"action": "BUY", "entry": entry, "sl": sl,
                        "tp1": entry + sl_dist * self.rr1,
                        "tp2": entry + sl_dist * self.rr1 * 2,
                        "reason": f"🟢 B1: EMA金叉趋势反转{vol_tag}"})
            return sig

        # B2: RSI超卖收复快线
        if (j >= 1 and RSI[j-1] < self.rsi_os and C[j] > EF[j] and C[j-1] <= EF[j-1]):
            sl = L[j] - ATR[j] * 0.5
            risk = abs(entry - sl)
            sig.update({"action": "BUY", "entry": entry, "sl": sl,
                        "tp1": entry + risk * self.rr1,
                        "tp2": entry + risk * self.rr1 * 2,
                        "reason": "🟢 B2: RSI超卖收复快线"})
            return sig

        return sig
