"""
strategy/adaptive.py - 自适应市场状态路由策略

核心思路：
  1. 用 RegimeDetector 对每根K线判断市场状态（牛/震/熊）
  2. 根据状态路由到不同的交易方向：
       牛市  → 只做多（S1L~S5L）
       熊市  → 只做空（S1S~S5S）
       震荡  → 双向（以反转信号 S1/S5 为主，顺势 S3 自然淘汰）
  3. 信号 reason 中携带当前状态标签，便于回测分析

继承 PriceActionSetups，复用所有信号检测逻辑。
"""
import numpy as np
import pandas as pd

from strategy.pa_setups import PriceActionSetups
from strategy.regime_detector import RegimeDetector, BULL, RANGING, BEAR


class AdaptiveStrategy(PriceActionSetups):
    """
    自适应市场状态路由策略（ADAPTIVE）

    在 PA_5Setups 的全套信号之上，增加：
    - 技术面市场状态检测（ADX + EMA斜率 + 确认期）
    - 按状态路由交易方向（牛市做多 / 熊市做空 / 震荡双向）
    - 回测结果中包含状态分布统计
    """

    PARAMS = [
        # ── 市场状态检测参数 ─────────────────────────────────────────────
        {
            "key": "adx_period", "label": "ADX 周期",
            "type": "int", "default": 14, "min": 7, "max": 28, "step": 1,
            "tip": "趋势强度指标周期，越大越平滑",
        },
        {
            "key": "ema_slow", "label": "慢速 EMA 周期",
            "type": "int", "default": 40, "min": 20, "max": 200, "step": 10,
            "tip": "判断价格所处大结构位置（BTC 1h 推荐 40）",
        },
        {
            "key": "adx_threshold", "label": "ADX 趋势阈值",
            "type": "int", "default": 22, "min": 15, "max": 40, "step": 5,
            "tip": "ADX > 阈值 = 有方向趋势（BTC 1h 推荐 22）",
        },
        {
            "key": "confirm_bars", "label": "状态确认K线数",
            "type": "int", "default": 4, "min": 2, "max": 20, "step": 1,
            "tip": "连续 N 根K线同状态才切换（BTC 1h 推荐 4）",
        },
        # ── 继承 PA_5Setups 的所有信号参数 ──────────────────────────────
        {
            "key": "rr1", "label": "止盈倍数 (TP1)",
            "type": "float", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "止盈 = 入场价 ± 止损距离 × 倍数（BTC 1h 推荐 1.5）",
        },
        {
            "key": "ema_period", "label": "EMA 周期（信号过滤）",
            "type": "int", "default": 14, "min": 5, "max": 50, "step": 1,
            "tip": "S2/S3 信号层的趋势过滤均线（BTC 1h 推荐 14）",
        },
        {
            "key": "trend_bars", "label": "趋势判断周期",
            "type": "int", "default": 14, "min": 10, "max": 50, "step": 5,
            "tip": "S3 回调策略的趋势判断回看数",
        },
        {
            "key": "spring_bars", "label": "前高/低回看",
            "type": "int", "default": 14, "min": 10, "max": 50, "step": 5,
            "tip": "S5 假突破策略的近期高低点窗口",
        },
        {
            "key": "cooldown", "label": "信号冷却期",
            "type": "int", "default": 5, "min": 3, "max": 20, "step": 1,
            "tip": "两次信号之间的最少间隔K线数（BTC 1h 推荐 5）",
        },
    ]

    def __init__(
        self,
        # 市场状态检测参数
        adx_period:    int   = 14,
        ema_slow:      int   = 40,
        adx_threshold: int   = 22,
        confirm_bars:  int   = 4,
        # PA_5Setups 信号参数（透传给父类）
        rr1:           float = 1.5,
        ema_period:    int   = 14,
        trend_bars:    int   = 14,
        spring_bars:   int   = 14,
        atr_period:    int   = 14,
        cooldown:      int   = 5,
    ):
        # 父类 direction 设为 'both'，路由由本类控制
        super().__init__(
            rr1         = rr1,
            ema_period  = ema_period,
            trend_bars  = trend_bars,
            spring_bars = spring_bars,
            atr_period  = atr_period,
            cooldown    = cooldown,
            direction   = 'both',
        )
        self.name = "ADAPTIVE_自适应市场状态路由"

        self.regime_detector = RegimeDetector(
            adx_period    = adx_period,
            ema_fast      = ema_period,       # 快线与信号层EMA保持一致
            ema_slow      = ema_slow,
            slope_window  = 5,
            adx_threshold = adx_threshold,
            confirm_bars  = confirm_bars,
        )

        # 预热K线：取检测器和父类各自需求的最大值
        self.warmup_bars = max(
            self.warmup_bars,
            self.regime_detector.warmup_bars,
        )

    # ── 核心：带状态路由的预计算 ──────────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        在父类所有指标的基础上，叠加市场状态检测与方向路由。

        额外写入列：
            regime      - 每根K线的市场状态 ('bull'/'ranging'/'bear')
            sig_action  - 路由后的交易信号
            sig_reason  - 信号原因（含状态标签前缀）
        """
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
        n     = len(df)

        # ── 1. 计算全量市场状态 ──────────────────────────────────────────
        regimes = self.regime_detector.compute(df)

        # ── 2. 初始化输出数组 ────────────────────────────────────────────
        actions  = ['HOLD'] * n
        sig_sl   = np.zeros(n)
        sig_tp1  = np.zeros(n)
        sig_tp2  = np.zeros(n)
        reasons  = ['观望'] * n

        last_signal_i = -999
        COOLDOWN      = self.cooldown

        # ── 3. 逐K线路由信号 ─────────────────────────────────────────────
        for i in range(self.warmup_bars, n - 1):
            j      = i - 1          # 信号K线（已完结）
            regime = regimes[j]     # 该K线的市场状态

            # 冷却期过滤
            if i - last_signal_i < COOLDOWN:
                continue

            # 根据市场状态选择检测方向
            if regime == BULL:
                # 牛市：只做多
                action, sl, reason = self._detect_long(
                    H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)

            elif regime == BEAR:
                # 熊市：只做空
                action, sl, reason = self._detect_short(
                    H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)

            else:
                # 震荡：双向，以反转信号为主（S3顺势自然因ADX不足而不触发）
                long_sig  = self._detect_long( H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)
                short_sig = self._detect_short(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)
                if long_sig[0]  == 'BUY':  action, sl, reason = long_sig
                elif short_sig[0] == 'SELL': action, sl, reason = short_sig
                else:                         action, sl, reason = 'HOLD', 0.0, ''

            # 写入信号（附带状态标签）
            if action == 'BUY':
                entry = O[i]
                risk  = entry - sl
                if risk > entry * 0.001:
                    label         = {'bull': '🐂牛市', 'ranging': '📦震荡', 'bear': '🐻熊市'}[regime]
                    actions[i]    = 'BUY'
                    sig_sl[i]     = sl
                    sig_tp1[i]    = entry + risk * self.rr1
                    sig_tp2[i]    = entry + risk * self.rr1 * 2
                    reasons[i]    = f'[{label}] {reason}'
                    last_signal_i = i

            elif action == 'SELL':
                entry = O[i]
                risk  = sl - entry
                if risk > entry * 0.001:
                    label         = {'bull': '🐂牛市', 'ranging': '📦震荡', 'bear': '🐻熊市'}[regime]
                    actions[i]    = 'SELL'
                    sig_sl[i]     = sl
                    sig_tp1[i]    = entry - risk * self.rr1
                    sig_tp2[i]    = entry - risk * self.rr1 * 2
                    reasons[i]    = f'[{label}] {reason}'
                    last_signal_i = i

        df['regime']     = regimes
        df['sig_action'] = actions
        df['sig_sl']     = sig_sl
        df['sig_tp1']    = sig_tp1
        df['sig_tp2']    = sig_tp2
        df['sig_reason'] = reasons
        return df

    # ── 状态分布统计（供引擎写入回测结果）────────────────────────────────────

    @staticmethod
    def regime_stats(df: pd.DataFrame) -> dict:
        """从预计算好的 df 中统计各状态的K线占比。"""
        if 'regime' not in df.columns:
            return {}
        total = max(len(df), 1)
        counts = df['regime'].value_counts()
        return {
            'bull_pct':    round(counts.get(BULL,    0) / total * 100, 1),
            'ranging_pct': round(counts.get(RANGING, 0) / total * 100, 1),
            'bear_pct':    round(counts.get(BEAR,    0) / total * 100, 1),
        }

    def generate_signal(self, df: pd.DataFrame) -> dict:
        """实盘接口：检测当前市场状态，再调用对应方向的信号检测。"""
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}

        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig

        df_slice = self._add_cols(df.iloc[-need:].copy())
        regime   = self.regime_detector.compute(df_slice)[-1]

        H     = df_slice['high'].values
        L     = df_slice['low'].values
        O     = df_slice['open'].values
        C     = df_slice['close'].values
        E     = df_slice['ema'].values
        ATR   = df_slice['atr'].values
        BODY  = df_slice['body'].values
        TOTAL = df_slice['total_len'].values
        LTAIL = df_slice['lower_tail'].values
        UTAIL = df_slice['upper_tail'].values
        j     = len(df_slice) - 2   # 最近完结的信号棒

        if regime == BULL:
            action, sl, reason = self._detect_long( H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)
        elif regime == BEAR:
            action, sl, reason = self._detect_short(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)
        else:
            long_sig  = self._detect_long( H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)
            short_sig = self._detect_short(H, L, O, C, E, ATR, BODY, TOTAL, LTAIL, UTAIL, j)
            if long_sig[0]   == 'BUY':  action, sl, reason = long_sig
            elif short_sig[0] == 'SELL': action, sl, reason = short_sig
            else:                         action, sl, reason = 'HOLD', 0.0, ''

        label = {'bull': '🐂牛市', 'ranging': '📦震荡', 'bear': '🐻熊市'}.get(regime, '')

        if action == 'BUY':
            entry = df_slice['open'].iloc[-1]
            risk  = entry - sl
            if risk > entry * 0.001:
                sig.update({"action": "BUY", "entry": entry, "sl": sl,
                            "tp1": entry + risk * self.rr1,
                            "tp2": entry + risk * self.rr1 * 2,
                            "risk_r": risk,
                            "reason": f'[{label}] {reason}',
                            "meta": {"regime": regime}})
        elif action == 'SELL':
            entry = df_slice['open'].iloc[-1]
            risk  = sl - entry
            if risk > entry * 0.001:
                sig.update({"action": "SELL", "entry": entry, "sl": sl,
                            "tp1": entry - risk * self.rr1,
                            "tp2": entry - risk * self.rr1 * 2,
                            "risk_r": risk,
                            "reason": f'[{label}] {reason}',
                            "meta": {"regime": regime}})
        return sig
