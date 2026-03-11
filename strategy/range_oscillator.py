"""
strategy/range_oscillator.py - 震荡市策略 V2（收缩等突破）

核心思路：
  BTC 的震荡行情 ≈ 蓄力阶段，价格在窄幅区间内积蓄能量，
  一旦突破方向明确，往往产生强势单边行情。

  策略逻辑：在布林带收窄期间保持观望，只在满足突破确认条件时入场。

信号体系：
  做多（突破向上）：
    R1: 布林带收缩后放大 + 收盘突破上轨 + 成交量放大（收缩突破做多）
    R2: 价格突破近期区间高点 + ADX 从低位开始上升 + RSI 动量确认（区间突破做多）

  做空（突破向下）：
    R3: 布林带收缩后放大 + 收盘跌破下轨 + 成交量放大（收缩突破做空）
    R4: 价格跌破近期区间低点 + ADX 从低位开始上升 + RSI 动量确认（区间突破做空）

  过滤（防假突破）：
    - 布林带宽度必须先收缩到阈值以下（确认是蓄力阶段）
    - 突破K线实体占比 > 阈值（过滤影线突破）
    - 突破后的第1根K线才入场（不追突破K线本身）
    - RSI 不能处于极端区（排除已经走完一波的假突破回测）

止盈目标：突破方向 1.5R / 3R，止损：突破区间的对侧 + 0.5×ATR
适用行情：ADX < 25 的低趋势环境，布林带收窄后首次突破。
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class RangeOscillatorStrategy(BaseStrategy):
    """
    震荡市收缩突破策略 V2（布林带收缩 + 区间突破 + 成交量/ADX确认）

    设计哲学：震荡期不交易，只在突破确认后的第一时间顺势入场。
    相比 V1 的均值回归策略，V2 避免了 BTC 「假震荡→真突破」的最大亏损场景。
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
            "key": "squeeze_pct", "label": "收缩判定阈值",
            "type": "float", "default": 0.04, "min": 0.02, "max": 0.08, "step": 0.01,
            "tip": "布林带宽度(归一化) < 此值认为收缩（BTC 1h 推荐 0.04）",
        },
        {
            "key": "squeeze_lookback", "label": "收缩回看周期",
            "type": "int", "default": 10, "min": 5, "max": 20, "step": 1,
            "tip": "最近 N 根K线内必须存在收缩状态",
        },
        {
            "key": "range_lookback", "label": "区间高低点回看",
            "type": "int", "default": 20, "min": 10, "max": 40, "step": 5,
            "tip": "用于确定近期区间的高低点窗口",
        },
        {
            "key": "rsi_period", "label": "RSI周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "adx_period", "label": "ADX周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "adx_rise_thresh", "label": "ADX上升阈值",
            "type": "float", "default": 2.0, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "ADX 在回看窗口内上升此值才确认趋势启动",
        },
        {
            "key": "vol_mult", "label": "成交量倍数",
            "type": "float", "default": 1.3, "min": 1.0, "max": 2.5, "step": 0.1,
            "tip": "突破K线成交量 > 近期均量 × 此倍数",
        },
        {
            "key": "body_ratio_min", "label": "最小实体占比(%)",
            "type": "float", "default": 55.0, "min": 40.0, "max": 80.0, "step": 5.0,
            "tip": "突破K线实体占总长度的最低比例（过滤影线假突破）",
        },
        {
            "key": "atr_period", "label": "ATR周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
        },
        {
            "key": "atr_sl_mult", "label": "ATR止损倍数",
            "type": "float", "default": 1.0, "min": 0.5, "max": 2.0, "step": 0.1,
            "tip": "止损 = 区间对侧 - ATR×此倍数（BTC 1h 推荐 1.0）",
        },
        {
            "key": "rr1", "label": "止盈1倍数",
            "type": "float", "default": 1.5, "min": 1.0, "max": 3.0, "step": 0.5,
            "tip": "止盈1 = 入场价 + 止损距离 × 此倍数",
        },
        {
            "key": "rr2", "label": "止盈2倍数",
            "type": "float", "default": 3.0, "min": 2.0, "max": 5.0, "step": 0.5,
            "tip": "止盈2 = 入场价 + 止损距离 × 此倍数",
        },
        {
            "key": "cooldown", "label": "信号冷却期",
            "type": "int", "default": 6, "min": 3, "max": 15, "step": 1,
            "tip": "两次信号之间最少间隔K线数（BTC 1h 推荐 6，突破后给行情发展空间）",
        },
    ]

    def __init__(
        self,
        bb_period:        int   = 20,
        bb_std:           float = 2.0,
        squeeze_pct:      float = 0.04,
        squeeze_lookback: int   = 10,
        range_lookback:   int   = 20,
        rsi_period:       int   = 14,
        adx_period:       int   = 14,
        adx_rise_thresh:  float = 2.0,
        vol_mult:         float = 1.3,
        body_ratio_min:   float = 55.0,
        atr_period:       int   = 14,
        atr_sl_mult:      float = 1.0,
        rr1:              float = 1.5,
        rr2:              float = 3.0,
        cooldown:         int   = 6,
    ):
        super().__init__(name="RANGE_收缩突破策略V2")
        self.bb_period        = bb_period
        self.bb_std           = bb_std
        self.squeeze_pct      = squeeze_pct
        self.squeeze_lookback = squeeze_lookback
        self.range_lookback   = range_lookback
        self.rsi_period       = rsi_period
        self.adx_period       = adx_period
        self.adx_rise_thresh  = adx_rise_thresh
        self.vol_mult         = vol_mult
        self.body_ratio_min   = body_ratio_min
        self.atr_period       = atr_period
        self.atr_sl_mult      = atr_sl_mult
        self.rr1              = rr1
        self.rr2              = rr2
        self.cooldown         = cooldown
        self.warmup_bars      = max(bb_period, rsi_period, adx_period * 3,
                                    range_lookback) + 25

    # ── 指标计算 ──────────────────────────────────────────────────────────────

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        c = df['close']
        h = df['high']
        l = df['low']
        o = df['open']

        # 布林带
        df['bb_mid']   = c.rolling(self.bb_period).mean()
        bb_std_val     = c.rolling(self.bb_period).std()
        df['bb_upper'] = df['bb_mid'] + self.bb_std * bb_std_val
        df['bb_lower'] = df['bb_mid'] - self.bb_std * bb_std_val
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid'].replace(0, np.nan)

        # 布林带宽度是否处于收缩状态
        df['is_squeeze'] = (df['bb_width'] < self.squeeze_pct).astype(int)

        # 近期有无收缩（回看 squeeze_lookback 根内至少有 3 根处于收缩）
        df['recent_squeeze'] = df['is_squeeze'].rolling(self.squeeze_lookback).sum()

        # V4.0: 收缩持续时间（连续收缩根数，持续越久突破越可靠）
        is_sq = df['is_squeeze'].values
        squeeze_duration = np.zeros(len(df))
        for idx in range(1, len(df)):
            if is_sq[idx] == 1:
                squeeze_duration[idx] = squeeze_duration[idx - 1] + 1
            else:
                squeeze_duration[idx] = 0
        df['squeeze_duration'] = squeeze_duration

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

        # ADX（简化版，用于检测趋势启动）
        up_move   = h.diff()
        down_move = -l.diff()
        plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        alpha = 1.0 / self.adx_period
        safe_atr = df['atr'].replace(0, np.nan)
        plus_di  = 100 * pd.Series(plus_dm, index=df.index).ewm(
                    alpha=alpha, adjust=False).mean() / safe_atr
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
                    alpha=alpha, adjust=False).mean() / safe_atr
        dx = (100 * (plus_di - minus_di).abs() /
              (plus_di + minus_di).replace(0, np.nan))
        df['adx'] = dx.ewm(alpha=alpha, adjust=False).mean()

        # 成交量均线
        if 'volume' in df.columns:
            df['vol_ma'] = df['volume'].rolling(self.bb_period).mean()
        else:
            df['vol_ma'] = np.nan

        # K线实体信息
        df['body']      = (c - o).abs()
        df['total_len'] = (h - l).replace(0, np.nan)
        df['body_pct']  = (df['body'] / df['total_len'] * 100).fillna(0)

        # 区间高低点
        df['range_high'] = h.rolling(self.range_lookback).max()
        df['range_low']  = l.rolling(self.range_lookback).min()

        return df

    # ── precompute（回测高性能路径）─────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._calc_indicators(df)

        BB_U  = df['bb_upper'].values
        BB_L  = df['bb_lower'].values
        BB_W  = df['bb_width'].values
        RSI   = df['rsi'].values
        ATR   = df['atr'].values
        ADX   = df['adx'].values
        H     = df['high'].values
        L     = df['low'].values
        C     = df['close'].values
        O     = df['open'].values
        BODY_PCT = df['body_pct'].values
        RNG_H = df['range_high'].values
        RNG_L = df['range_low'].values
        REC_SQ = df['recent_squeeze'].values
        SQ_DUR = df['squeeze_duration'].values  # V4.0: 收缩持续时间
        n     = len(df)

        # 成交量
        has_vol = 'volume' in df.columns and not df['vol_ma'].isna().all()
        if has_vol:
            VOL    = df['volume'].values
            VOL_MA = df['vol_ma'].values
        else:
            VOL = VOL_MA = np.zeros(n)

        actions  = ['HOLD'] * n
        sig_sl   = np.zeros(n)
        sig_tp1  = np.zeros(n)
        sig_tp2  = np.zeros(n)
        reasons  = ['观望'] * n
        last_sig_i = -999

        for i in range(self.warmup_bars, n - 1):
            j = i - 1   # 信号K线（已完结）
            if i - last_sig_i < self.cooldown:
                continue
            if np.isnan(ATR[j]) or ATR[j] == 0 or np.isnan(BB_U[j]):
                continue
            if np.isnan(ADX[j]) or np.isnan(RSI[j]):
                continue

            sl_pad = ATR[j] * self.atr_sl_mult

            # ── 核心条件：最近必须存在收缩（蓄力确认）───────────────
            had_squeeze = (not np.isnan(REC_SQ[j])) and REC_SQ[j] >= 3

            # V4.0: 收缩持续时间加权
            # 收缩 >= 8 根 K线 = 高质量蓄力，可降低量能门槛
            squeeze_dur = SQ_DUR[j] if not np.isnan(SQ_DUR[j]) else 0
            long_squeeze = squeeze_dur >= 8  # 长时间收缩

            # ── ADX 从低位上升（趋势启动信号）─────────────────────
            adx_rising = False
            if j >= 5:
                adx_base = np.nanmin(ADX[j-5:j])
                adx_rising = (ADX[j] - adx_base) > self.adx_rise_thresh

            # ── 成交量放大 ────────────────────────────────────────
            vol_ok = True  # 默认通过（如果没有 volume 数据）
            if has_vol and not np.isnan(VOL_MA[j]) and VOL_MA[j] > 0:
                # V4.0: 长时间收缩后量能门槛降低（蓄力越久突破越可靠）
                vol_mult_eff = self.vol_mult * 0.85 if long_squeeze else self.vol_mult
                vol_ok = VOL[j] > VOL_MA[j] * vol_mult_eff

            # ── 实体占比过滤 ──────────────────────────────────────
            body_ok = BODY_PCT[j] >= self.body_ratio_min

            # ═══════════════════════════════════════════════════════
            # R1: 收缩后布林带突破向上
            # V4.0: 止损改为突破K线最低价 + 0.5×ATR（更紧凑）
            # ═══════════════════════════════════════════════════════
            if (had_squeeze and C[j] > BB_U[j] and C[j] > O[j]
                    and body_ok and vol_ok and RSI[j] < 75):
                entry = O[i]
                # V4.0: 止损 = 突破K线最低价 - 0.5×ATR（而非区间下沿）
                sl = L[j] - ATR[j] * 0.5
                risk = entry - sl
                if risk > entry * 0.002:
                    actions[i]  = 'BUY'
                    sig_sl[i]   = sl
                    sig_tp1[i]  = entry + risk * self.rr1
                    sig_tp2[i]  = entry + risk * self.rr2
                    reasons[i]  = '🟢 R1: 收缩突破做多(BB上轨)'
                    last_sig_i  = i
                    continue

            # ═══════════════════════════════════════════════════════
            # R2: 区间高点突破 + ADX 启动
            # ═══════════════════════════════════════════════════════
            if (j >= self.range_lookback and
                    C[j] > RNG_H[j-1] and C[j] > O[j]
                    and adx_rising and body_ok
                    and 45 < RSI[j] < 72):
                entry = O[i]
                sl = RNG_L[j] - sl_pad  # 止损放在区间低点
                risk = entry - sl
                if risk > entry * 0.002:
                    actions[i]  = 'BUY'
                    sig_sl[i]   = sl
                    sig_tp1[i]  = entry + risk * self.rr1
                    sig_tp2[i]  = entry + risk * self.rr2
                    reasons[i]  = '🟢 R2: 区间高点突破做多(ADX启动)'
                    last_sig_i  = i
                    continue

            # ═══════════════════════════════════════════════════════
            # R3: 收缩后布林带突破向下
            # V4.0: 止损改为突破K线最高价 + 0.5×ATR（更紧凑）
            # ═══════════════════════════════════════════════════════
            if (had_squeeze and C[j] < BB_L[j] and C[j] < O[j]
                    and body_ok and vol_ok and RSI[j] > 25):
                entry = O[i]
                # V4.0: 止损 = 突破K线最高价 + 0.5×ATR
                sl = H[j] + ATR[j] * 0.5
                risk = sl - entry
                if risk > entry * 0.002:
                    actions[i]  = 'SELL'
                    sig_sl[i]   = sl
                    sig_tp1[i]  = entry - risk * self.rr1
                    sig_tp2[i]  = entry - risk * self.rr2
                    reasons[i]  = '🔴 R3: 收缩突破做空(BB下轨)'
                    last_sig_i  = i
                    continue

            # ═══════════════════════════════════════════════════════
            # R4: 区间低点突破 + ADX 启动
            # ═══════════════════════════════════════════════════════
            if (j >= self.range_lookback and
                    C[j] < RNG_L[j-1] and C[j] < O[j]
                    and adx_rising and body_ok
                    and 28 < RSI[j] < 55):
                entry = O[i]
                sl = RNG_H[j] + sl_pad  # 止损放在区间高点
                risk = sl - entry
                if risk > entry * 0.002:
                    actions[i]  = 'SELL'
                    sig_sl[i]   = sl
                    sig_tp1[i]  = entry - risk * self.rr1
                    sig_tp2[i]  = entry - risk * self.rr2
                    reasons[i]  = '🔴 R4: 区间低点突破做空(ADX启动)'
                    last_sig_i  = i

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

    # ── generate_signal（实盘接口）────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}
        need = self.warmup_bars + 10
        if df is None or len(df) < need:
            return sig
        df = self._calc_indicators(df.iloc[-need:].copy())

        j = len(df) - 2  # 最近完结的信号棒
        entry = df['open'].iloc[-1]

        BB_U  = df['bb_upper'].values
        BB_L  = df['bb_lower'].values
        RSI   = df['rsi'].values
        ATR   = df['atr'].values
        ADX   = df['adx'].values
        H     = df['high'].values
        L     = df['low'].values
        C     = df['close'].values
        O     = df['open'].values
        BODY_PCT = df['body_pct'].values
        RNG_H = df['range_high'].values
        RNG_L = df['range_low'].values
        REC_SQ = df['recent_squeeze'].values
        SQ_DUR = df['squeeze_duration'].values  # V4.0

        if np.isnan(ATR[j]) or ATR[j] == 0 or np.isnan(BB_U[j]):
            return sig
        if np.isnan(ADX[j]) or np.isnan(RSI[j]):
            return sig

        sl_pad = ATR[j] * self.atr_sl_mult

        # 收缩确认
        had_squeeze = (not np.isnan(REC_SQ[j])) and REC_SQ[j] >= 3

        # V4.0: 收缩持续时间加权
        squeeze_dur = SQ_DUR[j] if not np.isnan(SQ_DUR[j]) else 0
        long_squeeze = squeeze_dur >= 8

        # ADX 启动
        adx_rising = False
        if j >= 5:
            adx_base = np.nanmin(ADX[j-5:j])
            adx_rising = (ADX[j] - adx_base) > self.adx_rise_thresh

        # 成交量
        has_vol = 'volume' in df.columns
        vol_ok = True
        if has_vol:
            vol_ma = df['vol_ma'].values
            vol = df['volume'].values
            if not np.isnan(vol_ma[j]) and vol_ma[j] > 0:
                vol_mult_eff = self.vol_mult * 0.85 if long_squeeze else self.vol_mult
                vol_ok = vol[j] > vol_ma[j] * vol_mult_eff

        body_ok = BODY_PCT[j] >= self.body_ratio_min

        # ── R1: 收缩突破做多 ─────────────────────────────────────
        # V4.0: 止损 = 突破K线最低价 - 0.5×ATR
        if (had_squeeze and C[j] > BB_U[j] and C[j] > O[j]
                and body_ok and vol_ok and RSI[j] < 75):
            sl = L[j] - ATR[j] * 0.5
            risk = entry - sl
            if risk > entry * 0.002:
                sig.update({"action": "BUY", "entry": entry, "sl": sl,
                            "tp1": entry + risk * self.rr1,
                            "tp2": entry + risk * self.rr2,
                            "risk_r": risk,
                            "reason": "🟢 R1: 收缩突破做多(BB上轨)"})
                return sig

        # ── R2: 区间高点突破 ─────────────────────────────────────
        if (j >= self.range_lookback and
                C[j] > RNG_H[j-1] and C[j] > O[j]
                and adx_rising and body_ok
                and 45 < RSI[j] < 72):
            sl = RNG_L[j] - sl_pad
            risk = entry - sl
            if risk > entry * 0.002:
                sig.update({"action": "BUY", "entry": entry, "sl": sl,
                            "tp1": entry + risk * self.rr1,
                            "tp2": entry + risk * self.rr2,
                            "risk_r": risk,
                            "reason": "🟢 R2: 区间高点突破做多(ADX启动)"})
                return sig

        # ── R3: 收缩突破做空 ─────────────────────────────────────
        # V4.0: 止损 = 突破K线最高价 + 0.5×ATR
        if (had_squeeze and C[j] < BB_L[j] and C[j] < O[j]
                and body_ok and vol_ok and RSI[j] > 25):
            sl = H[j] + ATR[j] * 0.5
            risk = sl - entry
            if risk > entry * 0.002:
                sig.update({"action": "SELL", "entry": entry, "sl": sl,
                            "tp1": entry - risk * self.rr1,
                            "tp2": entry - risk * self.rr2,
                            "risk_r": risk,
                            "reason": "🔴 R3: 收缩突破做空(BB下轨)"})
                return sig

        # ── R4: 区间低点突破 ─────────────────────────────────────
        if (j >= self.range_lookback and
                C[j] < RNG_L[j-1] and C[j] < O[j]
                and adx_rising and body_ok
                and 28 < RSI[j] < 55):
            sl = RNG_H[j] + sl_pad
            risk = sl - entry
            if risk > entry * 0.002:
                sig.update({"action": "SELL", "entry": entry, "sl": sl,
                            "tp1": entry - risk * self.rr1,
                            "tp2": entry - risk * self.rr2,
                            "risk_r": risk,
                            "reason": "🔴 R4: 区间低点突破做空(ADX启动)"})
                return sig

        return sig
