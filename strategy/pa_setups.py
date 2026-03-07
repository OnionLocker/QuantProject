"""
strategy/pa_setups.py - 经典价格行为五法（全部做多）

Setup 1: Pin Bar 探底反转
Setup 2: 孕线突破 (Inside Bar Breakout)
Setup 3: 均线深度回调顺势 (Pullback to MA)
Setup 4: 吞没形态 (Bullish Engulfing)
Setup 5: 假突破/破底翻 (False Breakout / Spring)

信号互斥：同一根K线同时触发多个Setup时按优先级取第一个，不重复开仓。
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)


class PriceActionSetups(BaseStrategy):

    PARAMS = [
        {
            "key": "rr1", "label": "止盈倍数 (TP1)",
            "type": "float", "default": 2.0, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "止盈 = 入场价 + 止损距离 × 倍数",
        },
        {
            "key": "ema_period", "label": "EMA 周期",
            "type": "int", "default": 20, "min": 5, "max": 50, "step": 1,
            "tip": "Setup2/3 趋势过滤均线",
        },
        {
            "key": "trend_bars", "label": "趋势判断周期",
            "type": "int", "default": 20, "min": 10, "max": 50, "step": 5,
            "tip": "Setup3: 多头趋势判断的回看K线数",
        },
        {
            "key": "spring_bars", "label": "前低回看周期",
            "type": "int", "default": 20, "min": 10, "max": 50, "step": 5,
            "tip": "Setup5: 寻找 Recent_Low 的窗口",
        },
        {
            "key": "atr_period", "label": "ATR 周期",
            "type": "int", "default": 14, "min": 5, "max": 30, "step": 1,
            "tip": "ATR 用于模拟 Tick 缓冲大小",
        },
    ]

    def __init__(
        self,
        rr1:         float = 2.0,
        ema_period:  int   = 20,
        trend_bars:  int   = 20,
        spring_bars: int   = 20,
        atr_period:  int   = 14,
    ):
        super().__init__(name="PA_5Setups_价格行为五法")
        self.rr1         = rr1
        self.ema_period  = ema_period
        self.trend_bars  = trend_bars
        self.spring_bars = spring_bars
        self.atr_period  = atr_period
        self.warmup_bars = max(ema_period + trend_bars, spring_bars, atr_period) + 15

    # ── 指标计算 ─────────────────────────────────────────────────────────────

    def _calc_atr(self, df: pd.DataFrame) -> pd.Series:
        hl  = df['high'] - df['low']
        hc  = (df['high'] - df['close'].shift()).abs()
        lc  = (df['low']  - df['close'].shift()).abs()
        return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(self.atr_period).mean()

    def _add_cols(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加所有策略需要的衍生列。"""
        df['ema']        = df['close'].ewm(span=self.ema_period, adjust=False).mean()
        df['atr']        = self._calc_atr(df)
        df['body']       = (df['close'] - df['open']).abs()
        df['total_len']  = df['high'] - df['low']
        df['lower_tail'] = df[['open', 'close']].min(axis=1) - df['low']
        return df

    # ── 五个 Setup 的检测函数（共用，numpy 数组版）───────────────────────────

    def _detect(self, H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, j) -> tuple:
        """
        检测 j 处（已完结信号棒）是否触发任一 Setup。
        返回 (action, sl_price, reason)，未触发返回 ('HOLD', 0.0, '')。
        入场在 j+1 的开盘价。
        """
        if np.isnan(ATR[j]) or ATR[j] == 0:
            return 'HOLD', 0.0, ''

        tick   = ATR[j] * 0.05   # 用 ATR 的 5% 模拟一个 Tick 缓冲
        totj   = TOTAL[j]

        # ── Setup 1: Pin Bar 探底反转 ─────────────────────────────────────
        if totj > 0:
            is_pin = (
                LTAIL[j]  > totj * 0.66 and    # 下影线 > 66% 总长
                BODY[j]   < totj * 0.33 and    # 实体 < 33% 总长
                C[j]     >= H[j] - totj * 0.25  # 收盘位于顶部 25% 区域
            )
            # 背景：信号棒收盘位于过去 5 根的偏低分位
            if j >= 5:
                ctx = C[j] <= np.percentile(C[j - 5:j], 40)
            else:
                ctx = True

            if is_pin and ctx:
                return 'BUY', L[j] - tick, '🟢 S1: Pin Bar 探底反转'

        # ── Setup 2: 孕线突破 ─────────────────────────────────────────────
        if j >= 1:
            m = j - 1   # 母线索引
            is_inside  = H[j] < H[m] and L[j] > L[m]
            is_uptrend = C[j] > E[j]
            if is_inside and is_uptrend:
                return 'BUY', L[j] - tick, '🟢 S2: 孕线突破'

        # ── Setup 3: 均线深度回调顺势 ────────────────────────────────────
        if j >= self.trend_bars + 5:
            ts = j - self.trend_bars
            pct_above    = np.mean(C[ts:j + 1] > E[ts:j + 1])
            is_bull_trend = pct_above >= 0.6

            # 近 10 根内是否触碰过 EMA（允许 0.5% 容差）
            pb_start = max(ts + 5, j - 10)
            touched  = False
            pb_low   = np.inf
            for k in range(pb_start, j + 1):
                if L[k] <= E[k] * 1.005:
                    touched = True
                if touched:
                    pb_low = min(pb_low, L[k])

            # 信号棒：强劲阳线，收盘接近最高价，且突破前一根最高价
            is_strong = (
                C[j] > O[j] and
                totj > 0 and
                C[j] >= H[j] - totj * 0.3 and
                C[j] > H[j - 1]
            )

            if is_bull_trend and touched and is_strong and pb_low < np.inf:
                return 'BUY', pb_low - tick, '🟢 S3: 均线回调顺势'

        # ── Setup 4: 吞没形态 ────────────────────────────────────────────
        if j >= 1:
            m = j - 1
            engulf = (
                C[m] < O[m] and          # 前一根阴线
                C[j] > O[j] and          # 当前阳线
                O[j] <= C[m] and         # 开盘 ≤ 前收（完全覆盖）
                C[j] >= O[m]             # 收盘 ≥ 前开
            )
            # 动能过滤：实体 > 过去 10 根均值
            if j >= 11:
                momentum = BODY[j] > np.mean(BODY[j - 10:j])
            else:
                momentum = True

            if engulf and momentum:
                return 'BUY', L[j] - tick, '🟢 S4: 吞没形态'

        # ── Setup 5: 假突破/破底翻 ────────────────────────────────────────
        if j >= self.spring_bars:
            recent_low  = np.min(L[j - self.spring_bars:j])
            broke_below = L[j] < recent_low
            reclaimed   = C[j] > recent_low
            upper_half  = totj > 0 and C[j] >= L[j] + totj * 0.5

            if broke_below and reclaimed and upper_half:
                return 'BUY', L[j] - tick, '🟢 S5: 假突破/破底翻'

        return 'HOLD', 0.0, ''

    # ── 向量化预计算（回测高性能路径）────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._add_cols(df.copy())

        H     = df['high'].values
        L     = df['low'].values
        O     = df['open'].values
        C     = df['close'].values
        E     = df['ema'].values
        ATR   = df['atr'].values
        BODY  = df['body'].values
        TOTAL = df['total_len'].values
        LTAIL = df['lower_tail'].values

        n       = len(df)
        actions = ['HOLD'] * n
        sig_sl  = np.zeros(n)
        sig_tp1 = np.zeros(n)
        sig_tp2 = np.zeros(n)
        reasons = ['观望'] * n

        for i in range(self.warmup_bars, n - 1):
            j = i - 1   # 信号棒（已完结）
            action, sl, reason = self._detect(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, j)

            if action == 'BUY':
                entry = O[i]
                risk  = entry - sl
                # 风险至少是价格的 0.05%，过滤无效信号
                if risk > entry * 0.0005:
                    actions[i] = 'BUY'
                    sig_sl[i]  = sl
                    sig_tp1[i] = entry + risk * self.rr1
                    sig_tp2[i] = entry + risk * self.rr1 * 2
                    reasons[i] = reason

        df['sig_action'] = actions
        df['sig_sl']     = sig_sl
        df['sig_tp1']    = sig_tp1
        df['sig_tp2']    = sig_tp2
        df['sig_reason'] = reasons
        return df

    def signal_from_row(self, df: pd.DataFrame, i: int) -> dict:
        """直接读取预计算列，O(1)。"""
        row = df.iloc[i]
        return {
            "action": row['sig_action'],
            "sl":     row['sig_sl'],
            "tp1":    row['sig_tp1'],
            "tp2":    row['sig_tp2'],
            "risk_r": 0.0,
            "reason": row['sig_reason'],
            "entry":  row['open'],
            "meta":   {},
        }

    # ── 实盘接口 ─────────────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> dict:
        """实盘/兼容接口：分析最近已完结的K线，返回信号。"""
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}

        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig

        df = self._add_cols(df.iloc[-need:].copy())

        H     = df['high'].values
        L     = df['low'].values
        O     = df['open'].values
        C     = df['close'].values
        E     = df['ema'].values
        ATR   = df['atr'].values
        BODY  = df['body'].values
        TOTAL = df['total_len'].values
        LTAIL = df['lower_tail'].values

        j      = len(df) - 2   # 最近完结的信号棒
        action, sl, reason = self._detect(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, j)

        if action == 'BUY':
            entry = df['open'].iloc[-1]
            risk  = entry - sl
            if risk > entry * 0.0005:
                sig.update({
                    "action": "BUY",
                    "entry":  entry,
                    "sl":     sl,
                    "tp1":    entry + risk * self.rr1,
                    "tp2":    entry + risk * self.rr1 * 2,
                    "risk_r": risk,
                    "reason": reason,
                })
        return sig
