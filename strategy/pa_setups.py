"""
strategy/pa_setups.py - 经典价格行为五法（双向：做多 + 做空）

做多 Setup（看涨形态）：
  S1L: Pin Bar 探底反转（长下影线）
  S2L: 孕线突破做多（母线包含子线，上升趋势）
  S3L: 均线深度回调顺势做多
  S4L: 看涨吞没形态
  S5L: 假突破/破底翻（Spring）

做空 Setup（看跌形态，与做多完全对称）：
  S1S: Pin Bar 顶部反转（长上影线）
  S2S: 孕线突破做空（母线包含子线，下降趋势）
  S3S: 均线反弹做空（死猫跳）
  S4S: 看跌吞没形态
  S5S: 假突破/顶部翻（Upthrust）

信号规则：
  - 同一根K线同时触发多个做多 Setup，按优先级取第一个
  - 同一根K线同时触发多个做空 Setup，按优先级取第一个
  - 做多信号优先：若同时有多空信号，取做多（保守策略，可通过参数关闭做多优先）
  - 信号方向过滤：若 direction="long" 则只做多，"short" 只做空，"both" 双向
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
            "type": "float", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "止盈 = 入场价 ± 止损距离 × 倍数（BTC 1h 推荐 1.5，太大则持仓时间过长）",
        },
        {
            "key": "ema_period", "label": "EMA 周期",
            "type": "int", "default": 14, "min": 5, "max": 50, "step": 1,
            "tip": "S2/S3 趋势过滤均线（BTC 1h 推荐 14，响应更快）",
        },
        {
            "key": "trend_bars", "label": "趋势判断周期",
            "type": "int", "default": 14, "min": 10, "max": 50, "step": 5,
            "tip": "S3: 趋势判断回看K线数（BTC 1h 推荐 14 ≈ 半天+）",
        },
        {
            "key": "spring_bars", "label": "前高/低回看周期",
            "type": "int", "default": 14, "min": 10, "max": 50, "step": 5,
            "tip": "S5: 寻找近期高低点的窗口（BTC 1h 推荐 14）",
        },
        {
            "key": "atr_period", "label": "ATR 周期",
            "type": "int", "default": 14, "min": 5, "max": 30, "step": 1,
            "tip": "ATR 用于计算 Tick 缓冲大小",
        },
        {
            "key": "cooldown", "label": "信号冷却期（K线数）",
            "type": "int", "default": 5, "min": 3, "max": 20, "step": 1,
            "tip": "两次信号之间最少间隔多少根K线（BTC 1h 推荐 5 ≈ 5小时）",
        },
        {
            "key": "direction", "label": "交易方向",
            "type": "str", "default": "both",
            "tip": "long=只做多, short=只做空, both=双向",
        },
    ]

    def __init__(
        self,
        rr1:         float = 1.5,
        ema_period:  int   = 14,
        trend_bars:  int   = 14,
        spring_bars: int   = 14,
        atr_period:  int   = 14,
        cooldown:    int   = 5,
        direction:   str   = "both",
    ):
        super().__init__(name="PA_5Setups_双向价格行为")
        self.rr1         = rr1
        self.ema_period  = ema_period
        self.trend_bars  = trend_bars
        self.spring_bars = spring_bars
        self.atr_period  = atr_period
        self.cooldown    = cooldown
        self.direction   = direction.lower()  # "long" | "short" | "both"
        self.warmup_bars = max(ema_period + trend_bars, spring_bars, atr_period) + 15

    # ── 指标计算 ─────────────────────────────────────────────────────────────

    def _calc_atr(self, df: pd.DataFrame) -> pd.Series:
        hl  = df['high'] - df['low']
        hc  = (df['high'] - df['close'].shift()).abs()
        lc  = (df['low']  - df['close'].shift()).abs()
        return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(self.atr_period).mean()

    def _add_cols(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['ema']        = df['close'].ewm(span=self.ema_period, adjust=False).mean()
        df['atr']        = self._calc_atr(df)
        df['body']       = (df['close'] - df['open']).abs()
        df['total_len']  = df['high'] - df['low']
        df['lower_tail'] = df[['open', 'close']].min(axis=1) - df['low']
        df['upper_tail'] = df['high'] - df[['open', 'close']].max(axis=1)
        return df

    # ── 核心检测：返回 (action, sl, reason) ──────────────────────────────────

    def _detect_long(self, H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j) -> tuple:
        """检测 j 处是否触发做多信号，返回 ('BUY', sl, reason) 或 ('HOLD', 0, '')"""
        if np.isnan(ATR[j]) or ATR[j] == 0:
            return 'HOLD', 0.0, ''

        tick  = ATR[j] * 0.08   # 稍大的缓冲，减少因噪音触发止损
        totj  = TOTAL[j]

        # ── 全局过滤1：K线最小尺寸（过滤噪音小K线）────────────────────────
        if totj < ATR[j] * 1.0:   # 至少1倍ATR，严格过滤小K线
            return 'HOLD', 0.0, ''

        # ── 全局过滤2：做多须在EMA上方 + EMA方向向上（双重趋势确认）──────
        ema_rising = j >= 5 and E[j] > E[j - 5]   # EMA5根内方向向上
        in_uptrend = C[j] > E[j] and ema_rising

        # ── S1L: Pin Bar 探底反转（长下影线）────────────────────────────────
        if totj > 0 and in_uptrend:
            is_pin = (
                LTAIL[j]  > totj * 0.66 and   # 下影线 > 66% 总长（标准定义）
                BODY[j]   < totj * 0.33 and   # 实体 < 33% 总长
                UTAIL[j]  < totj * 0.20 and   # 上影线很短（收盘强势）
                C[j]      >= H[j] - totj * 0.25
            )
            if j >= 5:
                ctx = C[j] <= np.percentile(C[j - 5:j], 40)
            else:
                ctx = True
            if is_pin and ctx:
                return 'BUY', L[j] - tick, '🟢 S1L: Pin Bar 探底反转'

        # ── S2L: 孕线突破做多 ────────────────────────────────────────────────
        if j >= 1 and in_uptrend:
            m = j - 1
            mother_range = TOTAL[m]
            inside_range = totj
            # 严格：子线 < 母线45%（真正的蓄能收缩），母线须为阳线（顺势母线）
            is_inside    = H[j] < H[m] and L[j] > L[m]
            tight_inside = mother_range > 0 and inside_range < mother_range * 0.45
            mother_bull  = C[m] > O[m]  # 母线阳线，与趋势方向一致
            if is_inside and tight_inside and mother_bull:
                return 'BUY', L[j] - tick, '🟢 S2L: 孕线突破做多'

        # ── S3L: 均线深度回调顺势做多 ────────────────────────────────────────
        if j >= self.trend_bars + 5:
            ts = j - self.trend_bars
            pct_above    = np.mean(C[ts:j + 1] > E[ts:j + 1])
            is_bull_trend = pct_above >= 0.65   # 提高趋势要求

            pb_start = max(ts + 5, j - 10)
            touched, pb_low = False, np.inf
            for k in range(pb_start, j + 1):
                if L[k] <= E[k] * 1.005:
                    touched = True
                if touched:
                    pb_low = min(pb_low, L[k])

            is_strong = (
                C[j] > O[j] and totj > 0 and
                C[j] >= H[j] - totj * 0.25 and   # 收盘更接近最高（强势确认）
                j >= 1 and C[j] > H[j - 1]
            )
            if is_bull_trend and touched and is_strong and pb_low < np.inf:
                return 'BUY', pb_low - tick, '🟢 S3L: 均线回调顺势做多'

        # ── S4L: 看涨吞没形态 ────────────────────────────────────────────────
        if j >= 1 and in_uptrend:
            m = j - 1
            engulf = (
                C[m] < O[m] and C[j] > O[j] and
                O[j] <= C[m] and C[j] >= O[m]
            )
            # 更严格的动量过滤：实体 > 1.5x 近期均值
            avg_body = np.mean(BODY[max(0, j - 10):j]) if j >= 5 else BODY[j]
            momentum = BODY[j] > avg_body * 1.5
            if engulf and momentum:
                return 'BUY', L[j] - tick, '🟢 S4L: 看涨吞没'

        # ── S5L: 假突破/破底翻（Spring）─────────────────────────────────────
        if j >= self.spring_bars:
            recent_low  = np.min(L[j - self.spring_bars:j])
            broke_below = L[j] < recent_low * 0.999   # 明显跌破（0.1%以上）
            reclaimed   = C[j] > recent_low
            upper_third = totj > 0 and C[j] >= L[j] + totj * 0.66  # 收在上1/3（更严格）
            if broke_below and reclaimed and upper_third:
                return 'BUY', L[j] - tick, '🟢 S5L: 假突破破底翻'

        return 'HOLD', 0.0, ''

    def _detect_short(self, H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j) -> tuple:
        """检测 j 处是否触发做空信号，返回 ('SELL', sl, reason) 或 ('HOLD', 0, '')"""
        if np.isnan(ATR[j]) or ATR[j] == 0:
            return 'HOLD', 0.0, ''

        tick  = ATR[j] * 0.08
        totj  = TOTAL[j]

        # ── 全局过滤1：K线最小尺寸 ───────────────────────────────────────────
        if totj < ATR[j] * 1.0:
            return 'HOLD', 0.0, ''

        # ── 全局过滤2：做空须在EMA下方 + EMA方向向下 ─────────────────────
        ema_falling  = j >= 5 and E[j] < E[j - 5]
        in_downtrend = C[j] < E[j] and ema_falling

        # ── S1S: Pin Bar 顶部反转（长上影线）────────────────────────────────
        if totj > 0 and in_downtrend:
            is_pin = (
                UTAIL[j]  > totj * 0.66 and
                BODY[j]   < totj * 0.33 and
                LTAIL[j]  < totj * 0.20 and
                C[j]      <= L[j] + totj * 0.25
            )
            if j >= 5:
                ctx = C[j] >= np.percentile(C[j - 5:j], 60)
            else:
                ctx = True
            if is_pin and ctx:
                return 'SELL', H[j] + tick, '🔴 S1S: Pin Bar 顶部反转'

        # ── S2S: 孕线突破做空 ────────────────────────────────────────────────
        if j >= 1 and in_downtrend:
            m = j - 1
            mother_range = TOTAL[m]
            inside_range = totj
            is_inside    = H[j] < H[m] and L[j] > L[m]
            tight_inside = mother_range > 0 and inside_range < mother_range * 0.45
            mother_bear  = C[m] < O[m]
            if is_inside and tight_inside and mother_bear:
                return 'SELL', H[j] + tick, '🔴 S2S: 孕线突破做空'

        # ── S3S: 均线反弹做空（死猫跳）──────────────────────────────────────
        if j >= self.trend_bars + 5:
            ts = j - self.trend_bars
            pct_below    = np.mean(C[ts:j + 1] < E[ts:j + 1])
            is_bear_trend = pct_below >= 0.6

            pb_start = max(ts + 5, j - 10)
            touched, pb_high = False, -np.inf
            for k in range(pb_start, j + 1):
                if H[k] >= E[k] * 0.995:
                    touched = True
                if touched:
                    pb_high = max(pb_high, H[k])

            is_strong = (
                C[j] < O[j] and totj > 0 and
                C[j] <= L[j] + totj * 0.3 and
                j >= 1 and C[j] < L[j - 1]
            )
            if is_bear_trend and touched and is_strong and pb_high > -np.inf:
                return 'SELL', pb_high + tick, '🔴 S3S: 均线反弹做空'

        # ── S4S: 看跌吞没形态 ────────────────────────────────────────────────
        if j >= 1 and in_downtrend:
            m = j - 1
            engulf = (
                C[m] > O[m] and C[j] < O[j] and
                O[j] >= C[m] and C[j] <= O[m]
            )
            avg_body = np.mean(BODY[max(0, j - 10):j]) if j >= 5 else BODY[j]
            momentum = BODY[j] > avg_body * 1.5
            if engulf and momentum:
                return 'SELL', H[j] + tick, '🔴 S4S: 看跌吞没'

        # ── S5S: 假突破/顶部翻（Upthrust）───────────────────────────────────
        if j >= self.spring_bars:
            recent_high = np.max(H[j - self.spring_bars:j])
            broke_above = H[j] > recent_high * 1.001  # 明显突破
            reclaimed   = C[j] < recent_high
            lower_third = totj > 0 and C[j] <= H[j] - totj * 0.66  # 收在下1/3
            if broke_above and reclaimed and lower_third:
                return 'SELL', H[j] + tick, '🔴 S5S: 假突破顶部翻'

        return 'HOLD', 0.0, ''

    def _detect(self, H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j) -> tuple:
        """根据 direction 参数调用对应方向检测，返回 (action, sl, reason)"""
        long_sig  = ('HOLD', 0.0, '')
        short_sig = ('HOLD', 0.0, '')

        if self.direction in ('both', 'long'):
            long_sig = self._detect_long(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)

        if self.direction in ('both', 'short'):
            short_sig = self._detect_short(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)

        # 双向时：若同时触发，做多优先（更保守；实盘一般只有一个方向有信号）
        if long_sig[0]  == 'BUY':  return long_sig
        if short_sig[0] == 'SELL': return short_sig
        return ('HOLD', 0.0, '')

    # ── 向量化预计算（回测高性能路径）────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._add_cols(df)

        H     = df['high'].values
        L     = df['low'].values
        O     = df['open'].values
        C     = df['close'].values
        E     = df['ema'].values
        ATR   = df['atr'].values
        BODY  = df['body'].values
        TOTAL = df['total_len'].values
        LTAIL = df['lower_tail'].values
        UTAIL = df['upper_tail'].values

        n       = len(df)
        actions = ['HOLD'] * n
        sig_sl  = np.zeros(n)
        sig_tp1 = np.zeros(n)
        sig_tp2 = np.zeros(n)
        reasons = ['观望'] * n

        last_signal_i = -999
        COOLDOWN = self.cooldown

        for i in range(self.warmup_bars, n - 1):
            j = i - 1  # 信号棒（已完结）

            # 冷却期：距上次信号不足 COOLDOWN 根则跳过
            if i - last_signal_i < COOLDOWN:
                continue

            action, sl, reason = self._detect(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)

            if action == 'BUY':
                entry = O[i]
                risk  = entry - sl
                if risk > entry * 0.001:   # 最小风险提高到 0.1%
                    actions[i] = 'BUY'
                    sig_sl[i]  = sl
                    sig_tp1[i] = entry + risk * self.rr1
                    sig_tp2[i] = entry + risk * self.rr1 * 2
                    reasons[i] = reason
                    last_signal_i = i

            elif action == 'SELL':
                entry = O[i]
                risk  = sl - entry
                if risk > entry * 0.001:
                    actions[i] = 'SELL'
                    sig_sl[i]  = sl
                    sig_tp1[i] = entry - risk * self.rr1
                    sig_tp2[i] = entry - risk * self.rr1 * 2
                    reasons[i] = reason
                    last_signal_i = i

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
        """实盘接口：分析最近已完结的K线，返回信号。"""
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}

        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig

        df = self._add_cols(df.iloc[-need:])

        H     = df['high'].values
        L     = df['low'].values
        O     = df['open'].values
        C     = df['close'].values
        E     = df['ema'].values
        ATR   = df['atr'].values
        BODY  = df['body'].values
        TOTAL = df['total_len'].values
        LTAIL = df['lower_tail'].values
        UTAIL = df['upper_tail'].values

        j      = len(df) - 2  # 最近完结的信号棒
        action, sl, reason = self._detect(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)

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

        elif action == 'SELL':
            entry = df['open'].iloc[-1]
            risk  = sl - entry
            if risk > entry * 0.0005:
                sig.update({
                    "action": "SELL",
                    "entry":  entry,
                    "sl":     sl,
                    "tp1":    entry - risk * self.rr1,
                    "tp2":    entry - risk * self.rr1 * 2,
                    "risk_r": risk,
                    "reason": reason,
                })

        return sig
