"""
strategy/big_candle.py - 大阳线/大阴线策略（BTC 永续合约版）

理论来源：A股大阳线战法移植，核心逻辑不变，参数适配加密货币特性：
  - 大阳线实体涨幅阈值：≥1.5%（BTC 日波动比股票小，1.5% ≈ 股票 5%）
  - 量能验证：成交量 ≥ 近 20 根 K 线均量 × 1.5 倍
  - 位置过滤：近 60 根 K 线涨幅若已 ≥ 40%，视为高位，不开多（避免高位诱多）
  - 支持做空：大阴线（实体跌幅 ≥ 阈值）镜像逻辑

三大入场场景（映射原始战法）：
  M1: 低位反转大阳线 - 超卖区域（RSI<40）放量大阳 + 价格站上均线
  M2: 趋势中加速大阳线 - 沿 EMA20 上行，放量大阳突破近期高点
  M3: 平台突破大阳线 - 前 N 根 K 线低波动（布林带收窄），放量大阳突破

次K验证加分（信号质量评分）：
  - 当前 K 实体占比 ≥ 70%（光头光脚加分）
  - 上影线占比 < 20%（无压力加分）

止损规则（忠实原版）：
  - 默认止损：大阳线开盘价（原版"有效跌破开盘价立即离场"）
  - 紧止损：大阳线最低价（极强行情用）
  - 可选 ATR 兜底

止盈：
  TP1 = 入场价 + 风险距离 × rr1（默认 2.0）
  TP2 = 入场价 + 风险距离 × rr1 × 2

适用行情：趋势启动 / 加速阶段，震荡盘整效果差（配合 AUTO 模式使用效果最佳）
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class BigCandleStrategy(BaseStrategy):
    """
    大阳线/大阴线突破策略（做多做空双向）
    """

    PARAMS = [
        {
            "key": "body_pct", "label": "大阳线实体涨幅阈值 (%)",
            "type": "float", "default": 1.2, "min": 0.5, "max": 5.0, "step": 0.1,
            "tip": "K线实体涨幅 ≥ 此值才认定为大阳线（BTC 1h 推荐 1.2%，低波动期也能触发）",
        },
        {
            "key": "vol_mult", "label": "成交量倍数",
            "type": "float", "default": 1.3, "min": 1.0, "max": 4.0, "step": 0.1,
            "tip": "成交量需 ≥ 近20根K线均量 × 此倍数（BTC 1h 推荐 1.3）",
        },
        {
            "key": "vol_ma", "label": "均量参考周期",
            "type": "int", "default": 20, "min": 10, "max": 60, "step": 5,
            "tip": "计算均量的回溯K线数",
        },
        {
            "key": "body_ratio_min", "label": "最小实体占比 (%)",
            "type": "float", "default": 55.0, "min": 30.0, "max": 90.0, "step": 5.0,
            "tip": "实体占整根K线的比例（BTC 1h 推荐 55%，稍微放宽以捕获更多信号）",
        },
        {
            "key": "high_pos_pct", "label": "高位过滤：近N根涨幅上限 (%)",
            "type": "float", "default": 35.0, "min": 20.0, "max": 100.0, "step": 5.0,
            "tip": "近期已涨超此值不追多（BTC 1h 推荐 35%）",
        },
        {
            "key": "high_pos_lookback", "label": "高位过滤回溯K线数",
            "type": "int", "default": 48, "min": 20, "max": 120, "step": 10,
            "tip": "计算高位涨幅的回溯K线数（BTC 1h 推荐 48 = 2天）",
        },
        {
            "key": "rsi_period", "label": "RSI周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
            "tip": "用于低位反转场景(RSI超卖)识别",
        },
        {
            "key": "rsi_os", "label": "超卖线 (M1场景)",
            "type": "int", "default": 40, "min": 20, "max": 50, "step": 5,
            "tip": "M1场景：RSI < 此值才认为是低位超卖区域",
        },
        {
            "key": "ema_trend", "label": "趋势EMA周期",
            "type": "int", "default": 20, "min": 10, "max": 60, "step": 5,
            "tip": "M2场景：价格在此均线上方 + 放量大阳突破近期高点",
        },
        {
            "key": "breakout_lookback", "label": "突破高点回溯K线数",
            "type": "int", "default": 16, "min": 10, "max": 50, "step": 2,
            "tip": "M2/M3场景：突破此范围内的最高价才触发（BTC 1h 推荐 16 ≈ 16小时）",
        },
        {
            "key": "bb_period", "label": "布林带周期 (M3场景)",
            "type": "int", "default": 20, "min": 10, "max": 40, "step": 5,
            "tip": "M3场景：布林带收窄(横盘)判断",
        },
        {
            "key": "bb_squeeze_pct", "label": "布林带收窄阈值 (%)",
            "type": "float", "default": 3.0, "min": 1.0, "max": 8.0, "step": 0.5,
            "tip": "布林带宽度(上轨-下轨)/中轨 < 此值，认为是平台整理",
        },
        {
            "key": "sl_mode", "label": "止损模式 (0=开盘价, 1=最低价, 2=ATR)",
            "type": "int", "default": 1, "min": 0, "max": 2, "step": 1,
            "tip": "1=大阳线最低价(推荐，更紧凑), 0=开盘价(原版), 2=ATR动态",
        },
        {
            "key": "atr_period", "label": "ATR周期",
            "type": "int", "default": 14, "min": 7, "max": 21, "step": 1,
            "tip": "sl_mode=2时使用",
        },
        {
            "key": "atr_sl_mult", "label": "ATR止损倍数",
            "type": "float", "default": 1.2, "min": 0.5, "max": 3.0, "step": 0.1,
            "tip": "sl_mode=2时: 止损 = 入场价 - ATR × 此倍数（BTC 1h 推荐 1.2）",
        },
        {
            "key": "rr1", "label": "盈亏比 (TP1)",
            "type": "float", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.5,
            "tip": "TP1 = 入场价 + 风险距离 × 此值（BTC 1h 推荐 1.5）",
        },
        {
            "key": "cooldown", "label": "信号冷却K线数",
            "type": "int", "default": 4, "min": 2, "max": 20, "step": 1,
            "tip": "两次信号之间最少间隔K线数（BTC 1h 推荐 4）",
        },
        {
            "key": "enable_short", "label": "启用大阴线做空",
            "type": "int", "default": 1, "min": 0, "max": 1, "step": 1,
            "tip": "1=启用大阴线镜像信号(做空), 0=仅做多",
        },
    ]

    def __init__(
        self,
        body_pct:           float = 1.2,
        vol_mult:           float = 1.3,
        vol_ma:             int   = 20,
        body_ratio_min:     float = 55.0,
        high_pos_pct:       float = 35.0,
        high_pos_lookback:  int   = 48,
        rsi_period:         int   = 14,
        rsi_os:             int   = 40,
        ema_trend:          int   = 20,
        breakout_lookback:  int   = 16,
        bb_period:          int   = 20,
        bb_squeeze_pct:     float = 3.0,
        sl_mode:            int   = 1,
        atr_period:         int   = 14,
        atr_sl_mult:        float = 1.2,
        rr1:                float = 1.5,
        cooldown:           int   = 4,
        enable_short:       int   = 1,
    ):
        super().__init__(name="BIG_CANDLE_大阳线策略")
        self.body_pct           = body_pct / 100.0
        self.vol_mult           = vol_mult
        self.vol_ma             = vol_ma
        self.body_ratio_min     = body_ratio_min / 100.0
        self.high_pos_pct       = high_pos_pct / 100.0
        self.high_pos_lookback  = high_pos_lookback
        self.rsi_period         = rsi_period
        self.rsi_os             = rsi_os
        self.ema_trend          = ema_trend
        self.breakout_lookback  = breakout_lookback
        self.bb_period          = bb_period
        self.bb_squeeze_pct     = bb_squeeze_pct / 100.0
        self.sl_mode            = sl_mode
        self.atr_period         = atr_period
        self.atr_sl_mult        = atr_sl_mult
        self.rr1                = rr1
        self.cooldown           = cooldown
        self.enable_short       = bool(enable_short)
        self.warmup_bars        = max(high_pos_lookback, bb_period, rsi_period, ema_trend, vol_ma) + 10

    # ── 指标预计算 ─────────────────────────────────────────────────────────────

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        c = df['close']
        h = df['high']
        l = df['low']
        v = df['volume']

        # EMA 趋势线
        df['ema_trend'] = c.ewm(span=self.ema_trend, adjust=False).mean()

        # 均量
        df['vol_ma'] = v.rolling(self.vol_ma).mean()

        # RSI
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, np.nan)
        df['rsi'] = 100 - (100 / (1 + rs))

        # ATR
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        df['atr'] = tr.rolling(self.atr_period).mean()

        # 布林带
        df['bb_mid'] = c.rolling(self.bb_period).mean()
        bb_std       = c.rolling(self.bb_period).std(ddof=0)
        df['bb_up']  = df['bb_mid'] + 2 * bb_std
        df['bb_low'] = df['bb_mid'] - 2 * bb_std
        df['bb_width'] = (df['bb_up'] - df['bb_low']) / df['bb_mid'].replace(0, np.nan)

        return df

    # ── 单根 K 线的大阳/大阴判断 ──────────────────────────────────────────────

    @staticmethod
    def _candle_stats(o, h, l, c, vol, vol_ma_val):
        """
        返回 (is_big_bull, is_big_bear, body_ratio, has_vol)
        """
        total_range = h - l
        body        = c - o  # 正=阳线，负=阴线
        body_pct    = body / o if o > 0 else 0.0
        body_ratio  = abs(body) / total_range if total_range > 0 else 0.0
        has_vol     = (vol_ma_val > 0) and (vol >= vol_ma_val)
        return body, body_pct, body_ratio, has_vol

    # ── 计算止损价 ─────────────────────────────────────────────────────────────

    def _sl_long(self, open_price, low_price, atr_val):
        if self.sl_mode == 0:
            return open_price                          # 大阳线开盘价（原版）
        elif self.sl_mode == 1:
            return low_price                           # 大阳线最低价（紧）
        else:
            return open_price - atr_val * self.atr_sl_mult  # ATR 动态

    def _sl_short(self, open_price, high_price, atr_val):
        if self.sl_mode == 0:
            return open_price                          # 大阴线开盘价
        elif self.sl_mode == 1:
            return high_price                          # 大阴线最高价
        else:
            return open_price + atr_val * self.atr_sl_mult

    # ── 预计算（回测高性能路径）──────────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._calc_indicators(df)

        O    = df['open'].values
        H    = df['high'].values
        L    = df['low'].values
        C    = df['close'].values
        V    = df['volume'].values
        VMA  = df['vol_ma'].values
        RSI  = df['rsi'].values
        ATR  = df['atr'].values
        ET   = df['ema_trend'].values
        BBW  = df['bb_width'].values
        BBM  = df['bb_mid'].values
        n    = len(df)

        actions = ['HOLD'] * n
        sig_sl  = np.zeros(n)
        sig_tp1 = np.zeros(n)
        sig_tp2 = np.zeros(n)
        reasons = ['观望'] * n
        last_sig_i = -999

        lb  = self.breakout_lookback
        hlb = self.high_pos_lookback

        for i in range(self.warmup_bars, n - 1):
            j = i - 1   # 已完结的信号棒
            if i - last_sig_i < self.cooldown:
                continue
            if np.isnan(ATR[j]) or VMA[j] <= 0 or np.isnan(RSI[j]):
                continue

            body, body_pct, body_ratio, has_vol = self._candle_stats(
                O[j], H[j], L[j], C[j], V[j], VMA[j] * self.vol_mult)

            # ── 大阳线做多 ────────────────────────────────────────────────────
            if body_pct >= self.body_pct and body_ratio >= self.body_ratio_min and has_vol:
                # 高位过滤：近 hlb 根内低点
                look_start = max(0, j - hlb)
                past_low   = np.min(L[look_start:j])
                already_up = (C[j] - past_low) / past_low if past_low > 0 else 0
                if already_up >= self.high_pos_pct:
                    reasons[i] = '⚠️ 大阳线高位过滤，跳过'
                    continue

                sl = self._sl_long(O[j], L[j], ATR[j])
                risk = max(C[j] - sl, ATR[j] * 0.5)  # 保底 risk 避免 tp=entry

                scene = ''

                # M1: 低位反转 —— RSI超卖 + 大阳站上趋势线
                if RSI[j] < self.rsi_os and C[j] > ET[j]:
                    scene = 'M1: 低位反转大阳'

                # M2: 趋势加速突破近期高点
                elif (C[j] > ET[j] and j >= lb and
                      C[j] > np.max(H[j - lb:j])):
                    scene = 'M2: 趋势加速突破高点'

                # M3: 平台整理后突破（布林带收窄）
                elif (j >= lb and not np.isnan(BBW[j - lb]) and
                      np.min(BBW[max(0, j - lb):j]) < self.bb_squeeze_pct and
                      C[j] > np.max(H[max(0, j - lb):j])):
                    scene = 'M3: 平台突破大阳'

                # 通用大阳线（满足量价但不满足具体场景时降级触发）
                elif body_ratio >= 0.75:
                    scene = 'GEN: 强大阳线（光头光脚）'

                if scene:
                    actions[i] = 'BUY'
                    sig_sl[i]  = sl
                    sig_tp1[i] = C[j] + risk * self.rr1
                    sig_tp2[i] = C[j] + risk * self.rr1 * 2
                    reasons[i] = f'🟢 {scene} | 实体{body_pct*100:.1f}% 量{V[j]/VMA[j]*self.vol_mult:.1f}x'
                    last_sig_i = i
                    continue

            # ── 大阴线做空 ────────────────────────────────────────────────────
            if self.enable_short and body_pct <= -self.body_pct and body_ratio >= self.body_ratio_min and has_vol:
                # 低位过滤：已跌很多就不追空
                look_start = max(0, j - hlb)
                past_high  = np.max(H[look_start:j])
                already_dn = (past_high - C[j]) / past_high if past_high > 0 else 0
                if already_dn >= self.high_pos_pct:
                    reasons[i] = '⚠️ 大阴线低位过滤，跳过'
                    continue

                sl = self._sl_short(O[j], H[j], ATR[j])
                risk = max(sl - C[j], ATR[j] * 0.5)

                scene = ''

                # 镜像 M1: 高位反转大阴
                if RSI[j] > (100 - self.rsi_os) and C[j] < ET[j]:
                    scene = 'M1↓: 高位反转大阴'

                # 镜像 M2: 趋势加速跌破近期低点
                elif (C[j] < ET[j] and j >= lb and
                      C[j] < np.min(L[j - lb:j])):
                    scene = 'M2↓: 趋势加速跌破低点'

                # 镜像 M3: 平台向下突破
                elif (j >= lb and not np.isnan(BBW[j - lb]) and
                      np.min(BBW[max(0, j - lb):j]) < self.bb_squeeze_pct and
                      C[j] < np.min(L[max(0, j - lb):j])):
                    scene = 'M3↓: 平台向下突破大阴'

                elif body_ratio >= 0.75:
                    scene = 'GEN↓: 强大阴线（光头光脚）'

                if scene:
                    actions[i] = 'SELL'
                    sig_sl[i]  = sl
                    sig_tp1[i] = C[j] - risk * self.rr1
                    sig_tp2[i] = C[j] - risk * self.rr1 * 2
                    reasons[i] = f'🔴 {scene} | 实体{abs(body_pct)*100:.1f}% 量{V[j]/VMA[j]*self.vol_mult:.1f}x'
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
            "action": row['sig_action'],
            "entry":  row['open'],
            "sl":     row['sig_sl'],
            "tp1":    row['sig_tp1'],
            "tp2":    row['sig_tp2'],
            "risk_r": 0.0,
            "reason": row['sig_reason'],
            "meta":   {},
        }

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0,
               "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}
        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig

        df = self._calc_indicators(df.iloc[-need:].copy())
        j  = len(df) - 2   # 已完结 K 线

        O   = df['open'].values;  H = df['high'].values
        L   = df['low'].values;   C = df['close'].values
        V   = df['volume'].values; VMA = df['vol_ma'].values
        RSI = df['rsi'].values;   ATR  = df['atr'].values
        ET  = df['ema_trend'].values
        BBW = df['bb_width'].values
        entry = float(df['open'].iloc[-1])

        if np.isnan(ATR[j]) or VMA[j] <= 0:
            return sig

        body, body_pct, body_ratio, has_vol = self._candle_stats(
            O[j], H[j], L[j], C[j], V[j], VMA[j] * self.vol_mult)

        lb  = self.breakout_lookback
        hlb = self.high_pos_lookback

        # ── 做多 ──────────────────────────────────────────────────────────────
        if body_pct >= self.body_pct and body_ratio >= self.body_ratio_min and has_vol:
            look_start = max(0, j - hlb)
            past_low   = np.min(L[look_start:j])
            already_up = (C[j] - past_low) / past_low if past_low > 0 else 0
            if already_up < self.high_pos_pct:
                sl   = self._sl_long(O[j], L[j], ATR[j])
                risk = max(entry - sl, ATR[j] * 0.5)
                scene = ''
                if RSI[j] < self.rsi_os and C[j] > ET[j]:
                    scene = 'M1: 低位反转大阳'
                elif C[j] > ET[j] and j >= lb and C[j] > np.max(H[j - lb:j]):
                    scene = 'M2: 趋势加速突破高点'
                elif (j >= lb and not np.isnan(BBW[j - lb]) and
                      np.min(BBW[max(0, j - lb):j]) < self.bb_squeeze_pct and
                      C[j] > np.max(H[max(0, j - lb):j])):
                    scene = 'M3: 平台突破大阳'
                elif body_ratio >= 0.75:
                    scene = 'GEN: 强大阳线'
                if scene:
                    sig.update({
                        "action": "BUY", "entry": entry, "sl": sl,
                        "tp1": entry + risk * self.rr1,
                        "tp2": entry + risk * self.rr1 * 2,
                        "reason": f'🟢 {scene} | 实体{body_pct*100:.1f}%',
                    })
                    return sig

        # ── 做空 ──────────────────────────────────────────────────────────────
        if self.enable_short and body_pct <= -self.body_pct and body_ratio >= self.body_ratio_min and has_vol:
            look_start = max(0, j - hlb)
            past_high  = np.max(H[look_start:j])
            already_dn = (past_high - C[j]) / past_high if past_high > 0 else 0
            if already_dn < self.high_pos_pct:
                sl   = self._sl_short(O[j], H[j], ATR[j])
                risk = max(sl - entry, ATR[j] * 0.5)
                scene = ''
                if RSI[j] > (100 - self.rsi_os) and C[j] < ET[j]:
                    scene = 'M1↓: 高位反转大阴'
                elif C[j] < ET[j] and j >= lb and C[j] < np.min(L[j - lb:j]):
                    scene = 'M2↓: 趋势加速跌破低点'
                elif (j >= lb and not np.isnan(BBW[j - lb]) and
                      np.min(BBW[max(0, j - lb):j]) < self.bb_squeeze_pct and
                      C[j] < np.min(L[max(0, j - lb):j])):
                    scene = 'M3↓: 平台向下突破大阴'
                elif body_ratio >= 0.75:
                    scene = 'GEN↓: 强大阴线'
                if scene:
                    sig.update({
                        "action": "SELL", "entry": entry, "sl": sl,
                        "tp1": entry - risk * self.rr1,
                        "tp2": entry - risk * self.rr1 * 2,
                        "reason": f'🔴 {scene} | 实体{abs(body_pct)*100:.1f}%',
                    })
                    return sig

        return sig
