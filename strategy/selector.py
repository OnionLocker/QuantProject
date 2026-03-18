"""
strategy/selector.py - 市场状态判断 + 策略自动选择器 V5.1

V5.1 重构改进：
  - 信号质量评分动态权重归一化：UNKNOWN 数据源权重自动转移给技术面
  - 纯技术面模式下 tech_conf>0.8 即可达到 80+ 分
  - 突破模式快速通道 (Fast-Track)：BREAKOUT 跳过 K 线确认，立即切换
  - 待定切换日志：regime 变化但未达确认根数时输出日志

V4.0 机构级改进：
  - 多时间框架确认 (MTF)：4h 高时间框架作为方向过滤器
  - 成交量 Profile：用 VWAP 偏离度增强 regime 判断
  - 信号质量评分系统：综合多维度给信号打分 [0,100]
  - 三源一致性加分：技术+链上+新闻全一致时额外加成
  - 动态否决权阈值：基于近期费率分布百分位自适应
  - Regime 切换旧仓管理：切换时输出 close_old_position 指令

综合「技术面」+「新闻面」+「链上数据」得出当前市场所处阶段：
  - bull   (牛市)  → TrendBullStrategy
  - bear   (熊市)  → TrendBearStrategy
  - ranging(震荡)  → RangeOscillatorStrategy
  - breakout       → BigCandleStrategy
  - wait           → 不交易

技术面评分：
  - ADX + EMA排列 + 价格位置 + 布林带宽度 + 成交量确认
  - V4.0: VWAP 偏离度作为第五维度
  - V4.0: 多时间框架方向过滤

资金费率 + OI 链上数据：
  - V4.0: 动态否决权阈值（基于近期费率百分位）
  - V4.0: OI 连续性分析（持续上升 vs 单期暴增）
"""

import logging
import time
from collections import deque
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy_selector")

# 市场状态枚举
REGIME_BULL     = "bull"
REGIME_BEAR     = "bear"
REGIME_RANGING  = "ranging"
REGIME_UNKNOWN  = "unknown"
REGIME_BREAKOUT = "breakout"   # 强势突破（ADX极高+大K线）→ BIG_CANDLE
REGIME_WAIT     = "wait"       # 观望（信号模糊/高波动冲突）→ 不交易


class MarketRegimeSelector:
    """
    市场状态评估 + 策略自动选择。

    用法（在 BotRunner 中实例化一次，每轮主循环调用）：
        selector = MarketRegimeSelector(config)
        strategy = selector.get_strategy(df)
    """

    def __init__(self, config: dict):
        """
        :param config: 来自 config.yaml 的 selector 节点
        """
        sc = config.get("selector", {})

        # 技术面参数
        self.adx_period       = sc.get("adx_period",       14)
        self.adx_bull_thresh  = sc.get("adx_bull_thresh",  22)   # ADX > 此值 = 有趋势（BTC推荐22）
        self.adx_range_thresh = sc.get("adx_range_thresh", 18)   # ADX < 此值 = 震荡
        self.ema_short        = sc.get("ema_short",        14)
        self.ema_mid          = sc.get("ema_mid",          40)
        self.ema_long         = sc.get("ema_long",        80)    # V5.2: BTC 1h 推荐 80 ≈ 3.3天
        self.bb_period        = sc.get("bb_period",        20)
        self.bb_squeeze_pct   = sc.get("bb_squeeze_pct",   0.03) # 带宽/中轨 < 此值 = 挤压

        # 新闻面参数
        self.news_weight      = sc.get("news_weight",      0.3)   # 新闻权重
        self.tech_weight      = sc.get("tech_weight",      0.7)   # 技术面权重
        self.news_max_age_min = sc.get("news_max_age_min", 120)   # 超过此分钟数视为过期

        # 切换保护：连续 N 根K线同一 regime 才正式切换
        # V3.0: 动态 confirm_bars（根据置信度自适应）
        self.confirm_bars          = sc.get("confirm_bars",          3)    # 默认值（兼容旧配置）
        self.confirm_bars_fast     = sc.get("confirm_bars_fast",     2)    # 高置信度：快速确认
        self.confirm_bars_slow     = sc.get("confirm_bars_slow",     4)    # 低置信度：慢速确认
        self.confirm_fast_thresh   = sc.get("confirm_fast_thresh",   0.7)  # 置信度 > 此值用快速
        self.confirm_slow_thresh   = sc.get("confirm_slow_thresh",   0.4)  # 置信度 < 此值用慢速

        # 各 regime 对应的策略名（可在 config.yaml 覆盖）
        self.strategy_map = {
            REGIME_BULL:     sc.get("strategy_bull",     "BULL"),
            REGIME_BEAR:     sc.get("strategy_bear",     "BEAR"),
            REGIME_RANGING:  sc.get("strategy_ranging",  "RANGE"),
            REGIME_BREAKOUT: sc.get("strategy_breakout", "BIG_CANDLE"),
            # V5.2: WAIT 不再返回空字符串（完全停工），而是使用震荡策略低仓位试探
            REGIME_WAIT:     sc.get("strategy_wait", sc.get("strategy_ranging", "RANGE")),
        }

        # V1.5: 波动率快速检测参数
        self._atr_spike_mult  = sc.get("atr_spike_mult",  2.0)   # ATR 突变倍数
        self._vol_spike_mult  = sc.get("vol_spike_mult",  3.0)   # 成交量突变倍数
        self._atr_lookback    = sc.get("atr_lookback",    20)    # ATR 基准回看周期

        # V1.5: 策略切换过渡期（半仓试探）
        self._transition_bars = sc.get("transition_bars", 3)     # 切换后前 N 根K线半仓
        self._bars_since_switch: int = 999                       # 距上次切换的K线数
        self.in_transition:    bool  = False                     # 是否在过渡期

        # V2.0: 资金费率 + OI 数据参数
        self.funding_weight     = sc.get("funding_weight",     0.15)  # 资金费率权重
        self.oi_weight          = sc.get("oi_weight",          0.10)  # OI权重
        self.funding_extreme    = sc.get("funding_extreme",    0.0005)  # 极端费率阈值
        self.oi_spike_pct       = sc.get("oi_spike_pct",       0.10)  # OI 变化幅度阈值
        self.enable_market_extra = sc.get("enable_market_extra", True)  # 是否启用链上数据

        # V3.0: 资金费率否决权（极端费率时强制 WAIT）
        self.funding_veto_enable = sc.get("funding_veto_enable", True)   # 是否启用否决权
        self.funding_veto_mult   = sc.get("funding_veto_mult",   2.0)    # 费率 > extreme × 此倍数时触发
        self.funding_veto_contra = sc.get("funding_veto_contra", True)   # 是否只在与技术面方向冲突时否决

        # V2.0: 动态新闻权重
        self.dynamic_news_weight = sc.get("dynamic_news_weight", True)  # 是否启用动态权重

        # V2.0: 链上数据缓存
        self._last_extra_data:      dict  = {}
        self._last_extra_calc_time: float = 0.0
        self._extra_cache_seconds:  int   = 120   # 2分钟重算一次

        # V2.0: regime 评估详情（供 API/前端读取）
        self.last_regime_detail: dict = {}

        # 状态持久化（防抖）
        self._pending_regime:   Optional[str] = None
        self._pending_count:    int           = 0
        self._confirmed_regime: str           = REGIME_UNKNOWN
        self._current_strategy_name: str      = ""

        # 技术面 regime 评分缓存（避免每根K线都重算）
        self._last_tech_regime:      str   = REGIME_UNKNOWN
        self._last_tech_conf:        float = 0.0
        self._last_tech_calc_time:   float = 0.0
        self._tech_cache_seconds:    int   = 60   # 最多60秒重算一次

        # ── V4.0: 信号质量评分系统 ──────────────────────────────────────────
        self._signal_quality_score: float = 0.0   # 最新信号质量分 [0, 100]

        # V4.0: VWAP 偏离度参数
        self._vwap_period         = sc.get("vwap_period",         20)
        self._vwap_deviation_pct  = sc.get("vwap_deviation_pct",  0.02)   # 2% 偏离视为极端

        # V4.0: 多时间框架确认（高时间框架方向过滤）
        self._mtf_enable       = sc.get("mtf_enable",       True)
        self._mtf_ema_period   = sc.get("mtf_ema_period",   50)    # 4h 级别的 EMA
        self._mtf_weight       = sc.get("mtf_weight",       0.15)  # MTF 在总权重中的占比
        self._mtf_regime:      str   = REGIME_UNKNOWN
        self._mtf_conf:        float = 0.0
        self._mtf_calc_time:   float = 0.0
        self._mtf_cache_seconds: int = 300  # 5 分钟重算一次 MTF

        # V4.0: 动态否决权阈值（基于近期费率分布百分位）
        self._funding_rate_history: deque = deque(maxlen=48)  # 最近48期费率
        self._dynamic_veto_enable  = sc.get("dynamic_veto_enable",  True)
        self._dynamic_veto_pctile  = sc.get("dynamic_veto_pctile",  90)  # 90th百分位

        # V4.0: OI 连续性分析
        self._oi_history: deque = deque(maxlen=12)  # 最近12次OI变化

        # V4.0: Regime 切换旧仓管理
        self._prev_confirmed_regime: str = REGIME_UNKNOWN
        self.regime_transition_action: Optional[str] = None  # "close_long" | "close_short" | None
        self.regime_transition_urgency: float = 0.0          # 0~1, 1=立即平仓

        # V4.0: 信号质量评分明细
        self.last_signal_quality: dict = {}

    # ── 技术面：ADX + EMA 排列 + 布林带宽度 ────────────────────────────────────

    def _calc_tech_regime(self, df: pd.DataFrame) -> tuple[str, float]:
        """
        返回: (regime_str, confidence_score)
        confidence_score in [0, 1]，越高越确定
        """
        if df is None or len(df) < max(self.ema_long, self.adx_period) + 5:
            return REGIME_UNKNOWN, 0.0

        c = df['close']
        h = df['high']
        l = df['low']

        # EMA
        ema_s = c.ewm(span=self.ema_short, adjust=False).mean()
        ema_m = c.ewm(span=self.ema_mid,   adjust=False).mean()
        ema_l = c.ewm(span=self.ema_long,  adjust=False).mean()

        # ATR
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/self.adx_period, adjust=False).mean()

        # ADX
        up_move   = h.diff()
        down_move = -l.diff()
        plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        safe_atr  = atr.replace(0, np.nan)
        plus_di   = 100 * pd.Series(plus_dm, index=df.index).ewm(
                        alpha=1/self.adx_period, adjust=False).mean() / safe_atr
        minus_di  = 100 * pd.Series(minus_dm, index=df.index).ewm(
                        alpha=1/self.adx_period, adjust=False).mean() / safe_atr
        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx = dx.ewm(alpha=1/self.adx_period, adjust=False).mean()

        # 布林带宽度
        bb_mid   = c.rolling(self.bb_period).mean()
        bb_std   = c.rolling(self.bb_period).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_width = ((bb_upper - bb_lower) / bb_mid.replace(0, np.nan)).fillna(0.1)

        # 取最新值（倒数第2根已完结K线）
        j = -2
        adx_val  = adx.iloc[j]
        es_val   = ema_s.iloc[j]
        em_val   = ema_m.iloc[j]
        el_val   = ema_l.iloc[j]
        pdi_val  = plus_di.iloc[j]
        mdi_val  = minus_di.iloc[j]
        bw_val   = bb_width.iloc[j]
        close_j  = c.iloc[j]

        if any(pd.isna(x) for x in [adx_val, es_val, em_val, el_val]):
            return REGIME_UNKNOWN, 0.0

        score    = 0.0   # 正 → 牛，负 → 熊，接近0 → 震荡

        # ADX 趋势强度 (V5.2: BTC适配 - ADX 13~18 区间保留方向信号)
        if adx_val > self.adx_bull_thresh:
            adx_strength = min((adx_val - self.adx_bull_thresh) / 15, 1.0)  # 更快饱和
            # +DI 和 -DI 决定方向
            if pdi_val > mdi_val:
                score += adx_strength * 2.5   # 趋势方向贡献增大
            else:
                score -= adx_strength * 2.5
        elif adx_val > self.adx_range_thresh:
            # V5.2: ADX 在 range_thresh~bull_thresh 之间（13~18）
            # 不再压制 score，仅给一个较弱的方向信号
            weak_strength = (adx_val - self.adx_range_thresh) / (self.adx_bull_thresh - self.adx_range_thresh)
            if pdi_val > mdi_val:
                score += weak_strength * 0.8
            else:
                score -= weak_strength * 0.8
        else:
            # ADX 极低（< range_thresh）→ 轻微压制
            range_strength = min((self.adx_range_thresh - adx_val) / 20, 1.0)
            score *= (1 - range_strength * 0.3)

        # EMA 排列 (V5.2: 增加更多部分得分层级)
        if es_val > em_val > el_val and close_j > el_val:
            # 多头排列：强烈看涨
            score += 1.5
        elif es_val < em_val < el_val and close_j < el_val:
            # 空头排列：强烈看跌
            score -= 1.5
        elif es_val > em_val and close_j > em_val:
            # 不完美多头（快>中 + 价格在中线上）：部分看涨
            score += 0.8
        elif es_val < em_val and close_j < em_val:
            # 不完美空头（快<中 + 价格在中线下）：部分看跌
            score -= 0.8
        elif es_val > em_val and close_j > es_val:
            # V5.2: 快线上方（价格站上快线，方向偏多）
            score += 0.5
        elif es_val < em_val and close_j < es_val:
            # V5.2: 快线下方（价格跌破快线，方向偏空）
            score -= 0.5
        elif abs(es_val - em_val) / em_val < 0.005:
            # EMA 几乎重合（收紧判定到0.5%）：震荡
            score *= 0.6

        # 价格与长期EMA的关系
        if close_j > el_val * 1.02:
            score += 0.5
        elif close_j < el_val * 0.98:
            score -= 0.5

        # V4.0: 成交量确认维度 ──────────────────────────────────────────
        # 趋势若有成交量支撑，信号更可靠
        if 'volume' in df.columns and len(df) > 20:
            vol = df['volume']
            vol_ma = vol.rolling(20).mean()
            vol_ratio = vol.iloc[j] / vol_ma.iloc[j] if vol_ma.iloc[j] > 0 else 1.0
            if not pd.isna(vol_ratio):
                if vol_ratio > 1.5 and abs(score) > 1.0:
                    # 量价配合：趋势有量支撑，加分
                    score *= 1.15
                elif vol_ratio < 0.5 and abs(score) > 1.0:
                    # 缩量趋势：可能是假趋势，减分
                    score *= 0.85

        # V4.0: VWAP 偏离度维度 ──────────────────────────────────────────
        # 价格偏离 VWAP 过远 = 均值回归压力
        if 'volume' in df.columns and len(df) > self._vwap_period:
            try:
                typical_price = (df['high'] + df['low'] + df['close']) / 3
                vol = df['volume']
                vwap = (typical_price * vol).rolling(self._vwap_period).sum() / \
                       vol.rolling(self._vwap_period).sum().replace(0, np.nan)
                vwap_val = vwap.iloc[j]
                if not pd.isna(vwap_val) and vwap_val > 0:
                    vwap_dev = (close_j - vwap_val) / vwap_val
                    # 价格远在 VWAP 上方时，做多信号打折（均值回归压力）
                    if vwap_dev > self._vwap_deviation_pct and score > 1.0:
                        score *= 0.9
                    elif vwap_dev < -self._vwap_deviation_pct and score < -1.0:
                        score *= 0.9
            except Exception:
                pass

        # 布林带挤压 → 震荡信号 (V5.0: 衰减从0.3提高到0.6，减少误杀)
        if bw_val < self.bb_squeeze_pct:
            score *= 0.6   # 挤压期信号衰减（原0.3太激进，BTC挤压后常直接突破）

        # 决策
        confidence = min(abs(score) / 3.0, 1.0)  # V5.2: 从4.0降到3.0，更容易产生较高 confidence

        # V1.5: 波动率快速检测通道 ─────────────────────────────────────────
        # ATR 突然放大 = 市场状态可能在转换，优先识别
        atr_lookback = min(self._atr_lookback, len(df) - 2)
        if atr_lookback > 5:
            atr_recent = atr.iloc[j]
            atr_baseline = atr.iloc[j - atr_lookback:j].mean()
            if atr_baseline > 0 and atr_recent / atr_baseline > self._atr_spike_mult:
                # ATR 突变 + 明确方向 = 强势突破
                if score >= 1.0:
                    return REGIME_BREAKOUT, min(confidence * 1.3, 1.0)
                elif score <= -1.0:
                    return REGIME_BEAR, min(confidence * 1.2, 1.0)
                # ATR 突变但方向不明 = 不确定，观望
                else:
                    return REGIME_WAIT, 0.3

        # V5.2: WAIT 仅在真正无信号时触发（ADX 极低 + score 近零）─────────
        # 不再把 ADX 模糊区间(13~18)判为 WAIT，改为 RANGING（可交易）
        # WAIT 仅在 ADX < range_thresh 且 score 几乎为零时触发
        if adx_val <= self.adx_range_thresh and abs(score) < 0.3:
            return REGIME_WAIT, 0.15

        # ADX 极强（>40）且价格在长期均线上方 → 强势突破 regime
        if adx_val > 40 and score >= 1.5:
            return REGIME_BREAKOUT, min(confidence * 1.2, 1.0)
        # V5.2: BULL/BEAR 门槛从 1.0 降到 0.6
        # BTC 1h ADX 普遍偏低，score 很难到 1.0，导致长期 RANGING/WAIT
        if score >= 0.6:
            return REGIME_BULL, confidence
        elif score <= -0.6:
            return REGIME_BEAR, confidence
        else:
            return REGIME_RANGING, confidence

    # ── V4.0: 多时间框架确认 ────────────────────────────────────────────────
    def _calc_mtf_regime(self, df: pd.DataFrame) -> tuple[str, float]:
        """
        用 4h 等效数据（聚合 1h K 线）做高时间框架方向确认。
        原理：机构级策略通常要求高时间框架方向一致才入场（top-down analysis）。
        """
        if not self._mtf_enable or df is None or len(df) < self._mtf_ema_period * 4 + 10:
            return REGIME_UNKNOWN, 0.0

        now = time.time()
        if now - self._mtf_calc_time < self._mtf_cache_seconds:
            return self._mtf_regime, self._mtf_conf

        try:
            # 从 1h K 线聚合出 4h K 线
            df_4h = df.resample('4h').agg({
                'open': 'first', 'high': 'max', 'low': 'min',
                'close': 'last', 'volume': 'sum'
            }).dropna()

            if len(df_4h) < self._mtf_ema_period + 5:
                return REGIME_UNKNOWN, 0.0

            c4 = df_4h['close']
            ema_mtf = c4.ewm(span=self._mtf_ema_period, adjust=False).mean()

            j = -2
            close_4h = c4.iloc[j]
            ema_4h = ema_mtf.iloc[j]

            if pd.isna(close_4h) or pd.isna(ema_4h):
                return REGIME_UNKNOWN, 0.0

            # EMA 斜率（5 根 4h = 20h 方向）
            if len(ema_mtf) > 5:
                slope = ema_mtf.iloc[j] - ema_mtf.iloc[j - 5]
            else:
                slope = 0.0

            if close_4h > ema_4h and slope > 0:
                regime = REGIME_BULL
                conf = min(abs(close_4h - ema_4h) / ema_4h / 0.02, 1.0)
            elif close_4h < ema_4h and slope < 0:
                regime = REGIME_BEAR
                conf = min(abs(close_4h - ema_4h) / ema_4h / 0.02, 1.0)
            else:
                regime = REGIME_RANGING
                conf = 0.3

            self._mtf_regime = regime
            self._mtf_conf = conf
            self._mtf_calc_time = now
            return regime, conf

        except Exception as e:
            logger.debug(f"MTF 计算失败: {e}")
            return REGIME_UNKNOWN, 0.0

    # ── V5.1: 信号质量评分系统（动态权重归一化）──────────────────────────────
    def _calc_signal_quality(self, tech_regime: str, tech_conf: float,
                              extra_regime: str, extra_conf: float,
                              news_regime: str, news_conf: float,
                              mtf_regime: str, mtf_conf: float,
                              final_regime: str) -> float:
        """
        综合评分系统 [0, 100]，衡量信号的可靠程度。

        V5.1 动态权重归一化重构：
          核心改动：当链上/新闻/MTF 数据源缺失（UNKNOWN）时，不再直接扣分，
          而是将 UNKNOWN 源的权重动态分配给 tech_conf（技术面），并做归一化。
          确保：只要技术面趋势足够强（tech_conf > 0.8），纯技术面总分也能 80+。

        基础权重池（满分100）：
          - tech:        40 分
          - extra:       15 分
          - news:        10 分
          - mtf:         15 分
          - consistency: 10 分
          - volatility:  10 分
        当某数据源 UNKNOWN 时，其权重池按比例转移给 tech。
        """
        q = {}

        # ── Step 1: 识别各数据源可用性 ─────────────────────────────────────
        base_weights = {
            "tech":        40.0,
            "extra":       15.0,
            "news":        10.0,
            "mtf":         15.0,
            "consistency": 10.0,
            "volatility":  10.0,
        }

        unknown_pool = 0.0   # UNKNOWN 源释放出的权重
        if extra_regime == REGIME_UNKNOWN:
            unknown_pool += base_weights["extra"]
            base_weights["extra"] = 0.0
        if news_regime == REGIME_UNKNOWN:
            unknown_pool += base_weights["news"]
            base_weights["news"] = 0.0
        if mtf_regime == REGIME_UNKNOWN:
            unknown_pool += base_weights["mtf"]
            base_weights["mtf"] = 0.0

        # 将 UNKNOWN 源的权重全部分配给 tech
        effective_tech_weight = base_weights["tech"] + unknown_pool

        # ── Step 2: 各维度评分（按有效权重计算）──────────────────────────────

        # 1. 技术面置信度：effective_tech_weight × tech_conf
        tech_score = effective_tech_weight * tech_conf
        q["tech"] = round(tech_score, 1)
        q["tech_weight"] = round(effective_tech_weight, 1)

        # 2. 链上数据
        if base_weights["extra"] > 0:
            extra_score = base_weights["extra"] * extra_conf
            if extra_regime == final_regime:
                extra_score = min(base_weights["extra"], extra_score * 1.15)
            elif extra_regime != REGIME_UNKNOWN and extra_regime != final_regime:
                extra_score *= 0.5   # 方向冲突打5折
        else:
            extra_score = 0.0
        q["extra"] = round(extra_score, 1)

        # 3. 新闻面
        if base_weights["news"] > 0:
            news_score = base_weights["news"] * news_conf
            if news_regime == final_regime:
                news_score = min(base_weights["news"], news_score * 1.1)
            elif news_regime != REGIME_UNKNOWN and news_regime != final_regime:
                news_score *= 0.5
        else:
            news_score = 0.0
        q["news"] = round(news_score, 1)

        # 4. MTF 方向确认
        if base_weights["mtf"] > 0:
            # V5.2: 即使 final_regime != mtf_regime，只要 MTF 有方向就给部分分
            # （WAIT/RANGING 不应浪费 MTF 的方向信号）
            if mtf_regime == final_regime:
                mtf_score = base_weights["mtf"] * mtf_conf
            elif final_regime in (REGIME_WAIT, REGIME_RANGING):
                # WAIT/RANGING 模式下，MTF 有方向信号本身就是正面信息
                mtf_score = base_weights["mtf"] * mtf_conf * 0.6
            else:
                # MTF 方向冲突：不给分
                mtf_score = 0.0
        else:
            mtf_score = 0.0
        q["mtf"] = round(mtf_score, 1)

        # 5. 多源一致性
        sources = [tech_regime, extra_regime, news_regime, mtf_regime]
        valid_sources = [s for s in sources if s != REGIME_UNKNOWN]
        n_valid = len(valid_sources)
        if n_valid >= 2:
            # V5.2: 如果 final_regime 是 WAIT/RANGING，用"有方向的多数源"做一致性
            compare_regime = final_regime
            if final_regime in (REGIME_WAIT, REGIME_RANGING):
                # 找有方向的源中最多的方向
                directional = [s for s in valid_sources if s in (REGIME_BULL, REGIME_BEAR, REGIME_BREAKOUT)]
                if directional:
                    from collections import Counter
                    compare_regime = Counter(directional).most_common(1)[0][0]
            agree_count = sum(1 for s in valid_sources if s == compare_regime)
            agreement_ratio = agree_count / n_valid
            consistency_score = base_weights["consistency"] * agreement_ratio
            if agree_count >= 3:
                consistency_score = min(base_weights["consistency"],
                                       consistency_score * 1.3)
        else:
            # 只有1个有效源或0个：直接给满分（不因缺少数据源惩罚）
            consistency_score = base_weights["consistency"]
        q["consistency"] = round(consistency_score, 1)

        # 6. 波动率环境：基础 5 分 + tech_conf 加成
        vol_score = 5.0 + (tech_conf * 5.0)
        vol_score = min(base_weights["volatility"], vol_score)
        q["volatility"] = round(vol_score, 1)

        # ── Step 3: 汇总 & 归一化 ─────────────────────────────────────────
        raw_total = (tech_score + extra_score + news_score +
                     mtf_score + consistency_score + vol_score)
        total = max(0, min(100, raw_total))

        q["total"] = round(total, 1)
        q["unknown_sources"] = [
            name for name, regime in
            [("extra", extra_regime), ("news", news_regime), ("mtf", mtf_regime)]
            if regime == REGIME_UNKNOWN
        ]
        self.last_signal_quality = q
        self._signal_quality_score = total
        return total

    # ── V4.0: 动态否决权阈值 ─────────────────────────────────────────────────
    def _get_dynamic_veto_threshold(self) -> float:
        """
        基于近期费率分布的百分位计算动态否决阈值。
        原理：固定 0.1% 阈值在牛市/熊市表现不同，动态百分位更适应。
        """
        if not self._dynamic_veto_enable or len(self._funding_rate_history) < 8:
            # 数据不足，使用固定阈值
            return self.funding_extreme * self.funding_veto_mult

        rates = [abs(r) for r in self._funding_rate_history]
        dynamic_thresh = float(np.percentile(rates, self._dynamic_veto_pctile))
        # 保底：不低于固定阈值的 50%，也不高于 3x
        fixed_thresh = self.funding_extreme * self.funding_veto_mult
        return max(fixed_thresh * 0.5, min(dynamic_thresh, fixed_thresh * 3.0))

    # ── V4.0: OI 连续性分析 ──────────────────────────────────────────────────
    def _analyze_oi_continuity(self) -> tuple[str, float]:
        """
        分析 OI 的连续变化方向。
        连续 3 期以上同方向变化 = 强信号（机构资金持续流入/流出）
        单期暴增 = 可能是短期投机，信号衰减。
        """
        if len(self._oi_history) < 3:
            return "neutral", 0.0

        recent = list(self._oi_history)[-6:]  # 最近 6 期
        rising_count = sum(1 for x in recent if x > 0.02)
        falling_count = sum(1 for x in recent if x < -0.02)

        if rising_count >= 3:
            strength = min(1.0, rising_count / 5.0)
            return "sustained_rise", strength
        elif falling_count >= 3:
            strength = min(1.0, falling_count / 5.0)
            return "sustained_fall", strength
        elif len(recent) > 0 and abs(recent[-1]) > 0.1:
            # 单期暴增/暴跌
            return "spike", 0.3
        return "neutral", 0.0

    # ── V4.0: Regime 切换旧仓管理 ─────────────────────────────────────────────
    def _check_regime_transition(self, old_regime: str, new_regime: str,
                                   confidence: float) -> None:
        """
        当 regime 发生切换时，评估是否需要平掉旧 regime 方向的仓位。

        规则（参考机构做法）：
          - BULL→BEAR: 紧急平多，urgency=1.0
          - BULL→RANGING: 建议平多但不紧急，urgency=0.5
          - BULL→WAIT: 保持但收紧止损，urgency=0.3
          - BEAR→BULL: 紧急平空，urgency=1.0
          - BEAR→RANGING: 建议平空但不紧急，urgency=0.5
          - 其他切换: urgency=0.0 (不干预)
        """
        self.regime_transition_action = None
        self.regime_transition_urgency = 0.0

        if old_regime == new_regime or old_regime == REGIME_UNKNOWN:
            return

        transitions = {
            (REGIME_BULL, REGIME_BEAR):    ("close_long",  1.0),
            (REGIME_BULL, REGIME_RANGING): ("close_long",  0.5),
            (REGIME_BULL, REGIME_WAIT):    ("tighten_sl",  0.3),
            (REGIME_BEAR, REGIME_BULL):    ("close_short", 1.0),
            (REGIME_BEAR, REGIME_RANGING): ("close_short", 0.5),
            (REGIME_BEAR, REGIME_WAIT):    ("tighten_sl",  0.3),
        }

        action_info = transitions.get((old_regime, new_regime))
        if action_info:
            action, base_urgency = action_info
            # 高置信度切换 = 更紧急
            urgency = min(1.0, base_urgency * (0.5 + confidence * 0.5))
            self.regime_transition_action = action
            self.regime_transition_urgency = urgency
            logger.warning(
                f"⚡ Regime 切换处理: {old_regime}→{new_regime}, "
                f"操作={action}, 紧急度={urgency:.2f}"
            )

    # ── 新闻面情绪 ────────────────────────────────────────────────────────────

    def _get_news_regime(self) -> tuple[str, float]:
        """
        读取数据库最新新闻情绪，返回 (regime_hint, confidence)。
        超时/无数据返回 (UNKNOWN, 0.0)。
        V2.0: 支持动态权重计算。
        """
        try:
            from news.news_fetcher import get_latest_sentiment, get_sentiment_age_minutes
            age = get_sentiment_age_minutes()
            if age > self.news_max_age_min:
                logger.debug(f"新闻情绪过期（{age:.0f}分钟），忽略新闻面")
                return REGIME_UNKNOWN, 0.0
            sentiment = get_latest_sentiment()
            if not sentiment:
                return REGIME_UNKNOWN, 0.0
            score   = sentiment.get("combined_score", 0.0)
            regime  = sentiment.get("regime_hint", REGIME_UNKNOWN)
            # 置信度：分数绝对值越大越确定
            conf    = min(abs(score) / 0.6, 1.0)

            # V2.0: 动态新闻权重
            if self.dynamic_news_weight:
                try:
                    from utils.ai_client import calculate_dynamic_news_weight, is_ai_configured
                    dyn_weight = calculate_dynamic_news_weight(
                        base_weight=self.news_weight,
                        age_minutes=age,
                        article_count=sentiment.get("article_count", 5),
                        ai_available=is_ai_configured(),
                    )
                    # 将动态权重信息保存到 detail 供前端展示
                    self.last_regime_detail["dynamic_news_weight"] = round(dyn_weight, 3)
                    self.last_regime_detail["news_age_min"] = round(age, 1)
                except ImportError:
                    pass

            return regime, conf
        except Exception as e:
            logger.warning(f"读取新闻情绪失败: {e}")
            return REGIME_UNKNOWN, 0.0

    # ── V2.0: 资金费率 + OI 链上数据 ──────────────────────────────────────────

    def _get_market_extra(self, symbol: str = "BTC/USDT:USDT") -> tuple[str, float]:
        """
        获取资金费率 + OI 综合信号。
        返回: (regime_hint, confidence)
        """
        if not self.enable_market_extra:
            return REGIME_UNKNOWN, 0.0

        # 带缓存
        now = time.time()
        if (now - self._last_extra_calc_time) < self._extra_cache_seconds and self._last_extra_data:
            data = self._last_extra_data
        else:
            try:
                from data.market_extra import get_market_extra_signals
                data = get_market_extra_signals(symbol)
                self._last_extra_data = data
                self._last_extra_calc_time = now
            except Exception as e:
                logger.warning(f"获取链上数据失败: {e}")
                return REGIME_UNKNOWN, 0.0

        if not data.get("available"):
            return REGIME_UNKNOWN, 0.0

        composite = data.get("composite_score", 0.0)
        signal = data.get("composite_signal", "neutral")

        # 保存详情供前端展示
        self.last_regime_detail["funding"] = data.get("funding")
        self.last_regime_detail["oi"] = data.get("oi")
        self.last_regime_detail["market_extra_signal"] = signal
        self.last_regime_detail["market_extra_score"] = composite

        # 转换为 regime hint
        if signal == "bullish":
            regime = REGIME_BULL
        elif signal == "bearish":
            regime = REGIME_BEAR
        else:
            regime = REGIME_RANGING

        conf = min(abs(composite) / 0.5, 1.0)
        return regime, conf

    # ── 综合决策 ──────────────────────────────────────────────────────────────

    def evaluate(self, df: pd.DataFrame, symbol: str = "BTC/USDT:USDT") -> dict:
        """
        综合技术面 + 新闻面 + 链上数据 + MTF + 信号质量评分，输出当前市场状态判断。

        V4.0 新增返回字段：
            "signal_quality":       float,  # [0,100] 信号质量评分
            "mtf_regime":           str,    # 多时间框架方向
            "transition_action":    str,    # 旧仓管理指令
            "transition_urgency":   float,  # 旧仓管理紧急度
        """
        # 清理上次 detail
        self.last_regime_detail = {}

        # 技术面（带缓存，1分钟内不重算）
        now = time.time()
        if now - self._last_tech_calc_time > self._tech_cache_seconds:
            self._last_tech_regime, self._last_tech_conf = self._calc_tech_regime(df)
            self._last_tech_calc_time = now
        tech_regime = self._last_tech_regime
        tech_conf   = self._last_tech_conf

        # V4.0: 多时间框架确认
        mtf_regime, mtf_conf = self._calc_mtf_regime(df)

        # 新闻面（news_weight=0 时直接跳过，避免无意义的 import 和 DB 查询）
        if self.news_weight > 0:
            news_regime, news_conf = self._get_news_regime()
        else:
            news_regime, news_conf = REGIME_UNKNOWN, 0.0

        # V2.0: 资金费率 + OI 链上数据
        extra_regime, extra_conf = self._get_market_extra(symbol)

        # V4.0: 更新费率和OI历史（用于动态否决权和OI连续性分析）
        if self._last_extra_data:
            funding_data = self._last_extra_data.get("funding")
            if funding_data:
                fr = funding_data.get("funding_rate", 0)
                self._funding_rate_history.append(fr)
            oi_data = self._last_extra_data.get("oi")
            if oi_data:
                self._oi_history.append(oi_data.get("change_pct", 0))

        # V4.0: OI 连续性分析增强
        oi_continuity_signal, oi_continuity_strength = self._analyze_oi_continuity()

        # ── 加权投票 ─────────────────────────────────────────────────────────
        votes = {REGIME_BULL: 0.0, REGIME_BEAR: 0.0, REGIME_RANGING: 0.0,
                 REGIME_BREAKOUT: 0.0, REGIME_WAIT: 0.0}

        # 计算实际权重（V4.0: MTF 分走部分权重）
        total_extra_weight = self.funding_weight + self.oi_weight
        effective_tech_weight = self.tech_weight
        effective_news_weight = self.news_weight
        effective_mtf_weight = self._mtf_weight if self._mtf_enable else 0.0

        # 确保权重总和 ≤ 1.0
        if self.enable_market_extra and total_extra_weight > 0:
            weight_sum = (effective_tech_weight + effective_news_weight +
                         total_extra_weight + effective_mtf_weight)
            if weight_sum > 1.0:
                scale = 1.0 / weight_sum
                effective_tech_weight *= scale
                effective_news_weight *= scale
                effective_mtf_weight *= scale

        # 技术面投票
        # V5.2: WAIT 不直接投票（避免 WAIT 黑洞），转为 RANGING 投票
        if tech_regime != REGIME_UNKNOWN:
            vote_regime = REGIME_RANGING if tech_regime == REGIME_WAIT else tech_regime
            votes[vote_regime] += effective_tech_weight * (0.5 + tech_conf * 0.5)

        # 新闻面投票（V2.0: 可能使用动态权重）
        actual_news_weight = self.last_regime_detail.get("dynamic_news_weight", effective_news_weight)
        if news_regime != REGIME_UNKNOWN:
            votes[news_regime] += actual_news_weight * (0.5 + news_conf * 0.5)

        # V2.0: 资金费率 + OI 投票
        if extra_regime != REGIME_UNKNOWN:
            extra_vote = total_extra_weight * (0.5 + extra_conf * 0.5)
            votes[extra_regime] += extra_vote

            # 特殊规则：资金费率极端时，给 WAIT 一点投票（市场可能要反转）
            funding_data = self._last_extra_data.get("funding")
            if funding_data and abs(funding_data.get("funding_rate", 0)) > self.funding_extreme * 2:
                votes[REGIME_WAIT] += 0.1  # 轻微的观望偏向

        # V4.0: 多时间框架投票
        if mtf_regime != REGIME_UNKNOWN:
            votes[mtf_regime] += effective_mtf_weight * (0.5 + mtf_conf * 0.5)

        # V4.0: OI 连续性加成投票
        if oi_continuity_signal == "sustained_rise" and tech_regime in (REGIME_BULL, REGIME_BREAKOUT):
            votes[tech_regime] += 0.05 * oi_continuity_strength
        elif oi_continuity_signal == "sustained_fall" and tech_regime in (REGIME_BEAR, REGIME_WAIT):
            votes[tech_regime] += 0.05 * oi_continuity_strength

        # 无投票时返回 unknown
        total_votes = sum(votes.values())
        if total_votes == 0:
            regime    = REGIME_UNKNOWN
            confidence = 0.0
        else:
            regime    = max(votes, key=votes.get)
            confidence = votes[regime] / total_votes

        # ── V4.0: 动态否决权阈值 ─────────────────────────────────────────
        funding_vetoed = False
        if self.funding_veto_enable and self._last_extra_data:
            funding_data = self._last_extra_data.get("funding")
            if funding_data:
                fr = funding_data.get("funding_rate", 0)
                veto_threshold = self._get_dynamic_veto_threshold()
                if abs(fr) > veto_threshold:
                    if self.funding_veto_contra:
                        contra = ((fr > 0 and regime == REGIME_BULL) or
                                  (fr < 0 and regime == REGIME_BEAR))
                        if contra:
                            funding_vetoed = True
                            logger.warning(
                                f"⛔ 动态否决: fr={fr:.6f} 与 regime={regime} 冲突，"
                                f"强制 WAIT（动态阈值={veto_threshold:.6f}）"
                            )
                    else:
                        funding_vetoed = True
                        logger.warning(
                            f"⛔ 动态否决: fr={fr:.6f} 极端，"
                            f"强制 WAIT（动态阈值={veto_threshold:.6f}）"
                        )
                    if funding_vetoed:
                        regime = REGIME_WAIT
                        confidence = 0.3

        # ── 防抖保护：动态 confirm_bars（V3.0 置信度加权）─────────────────
        # V5.1 突破快速通道 (Fast-Track)：BREAKOUT 不等待确认，立即切换
        if regime == REGIME_BREAKOUT:
            dynamic_confirm = 0
            logger.info(
                f"⚡ Breakout Fast-Track: regime={regime}, conf={confidence:.2f}, "
                f"跳过 K 线确认，立即切换"
            )
        elif confidence >= self.confirm_fast_thresh:
            dynamic_confirm = self.confirm_bars_fast
        elif confidence <= self.confirm_slow_thresh:
            dynamic_confirm = self.confirm_bars_slow
        else:
            ratio = ((confidence - self.confirm_slow_thresh)
                     / max(self.confirm_fast_thresh - self.confirm_slow_thresh, 0.01))
            dynamic_confirm = round(
                self.confirm_bars_slow - ratio * (self.confirm_bars_slow - self.confirm_bars_fast)
            )
            dynamic_confirm = max(self.confirm_bars_fast, min(self.confirm_bars_slow, dynamic_confirm))

        if regime == self._pending_regime:
            self._pending_count += 1
        else:
            self._pending_regime = regime
            self._pending_count  = 1

        # V5.1: dynamic_confirm=0 表示立即确认（Breakout Fast-Track）
        if self._pending_count >= max(dynamic_confirm, 1) or dynamic_confirm == 0:
            if regime != self._confirmed_regime:
                # V4.0: Regime 切换旧仓管理
                self._prev_confirmed_regime = self._confirmed_regime
                self._check_regime_transition(
                    self._prev_confirmed_regime, regime, confidence
                )
                logger.info(
                    f"🔄 市场状态切换: {self._confirmed_regime} → {regime} "
                    f"(置信度={confidence:.2f}, 确认根数={dynamic_confirm}, "
                    f"技术={tech_regime}, 新闻={news_regime}, "
                    f"链上={extra_regime}, MTF={mtf_regime})"
                )
            self._confirmed_regime = regime
        else:
            # V5.1: 待定切换日志 — regime 已变但未达确认根数
            if regime != self._confirmed_regime:
                logger.info(
                    f"⏳ 待定切换: {self._confirmed_regime} → {regime} "
                    f"(已累计 {self._pending_count}/{dynamic_confirm} 根, "
                    f"置信度={confidence:.2f})"
                )

        final_regime = self._confirmed_regime
        strategy_name = self.strategy_map.get(final_regime, "")

        # V4.0: 信号质量评分
        signal_quality = self._calc_signal_quality(
            tech_regime, tech_conf, extra_regime, extra_conf,
            news_regime, news_conf, mtf_regime, mtf_conf, final_regime
        )

        reason = (
            f"技术面={tech_regime}(conf={tech_conf:.2f}) "
            f"新闻面={news_regime}(conf={news_conf:.2f}) "
            f"链上={extra_regime}(conf={extra_conf:.2f}) "
            f"MTF={mtf_regime}(conf={mtf_conf:.2f}) "
            f"质量={signal_quality:.0f} "
            f"→ 确认={final_regime} 策略={strategy_name or '未配置'}"
        )

        # 保存详细评估结果
        self.last_regime_detail.update({
            "regime": final_regime,
            "confidence": round(confidence, 3),
            "tech_regime": tech_regime,
            "tech_conf": round(tech_conf, 3),
            "news_regime": news_regime,
            "news_conf": round(news_conf, 3),
            "extra_regime": extra_regime,
            "extra_conf": round(extra_conf, 3),
            "mtf_regime": mtf_regime,
            "mtf_conf": round(mtf_conf, 3),
            "votes": {k: round(v, 3) for k, v in votes.items()},
            "strategy_name": strategy_name,
            "in_transition": self.in_transition,
            "dynamic_confirm_bars": dynamic_confirm,
            "funding_vetoed": funding_vetoed,
            "signal_quality": round(signal_quality, 1),
            "signal_quality_detail": self.last_signal_quality,
            "oi_continuity": oi_continuity_signal,
            "transition_action": self.regime_transition_action,
            "transition_urgency": round(self.regime_transition_urgency, 2),
        })

        return {
            "regime":              final_regime,
            "confidence":          round(confidence, 3),
            "tech_regime":         tech_regime,
            "news_regime":         news_regime,
            "extra_regime":        extra_regime,
            "mtf_regime":          mtf_regime,
            "reason":              reason,
            "strategy_name":       strategy_name,
            "signal_quality":      round(signal_quality, 1),
            "transition_action":   self.regime_transition_action,
            "transition_urgency":  round(self.regime_transition_urgency, 2),
        }

    def get_strategy(self, df: pd.DataFrame, symbol: str = "BTC/USDT:USDT"):
        """
        直接返回当前推荐的策略实例（懒加载，按需切换）。
        如果状态未变化则复用旧实例，避免重复实例化。

        V1.5 新增：
        - WAIT 状态返回 None（不交易）
        - 策略切换后进入过渡期，self.in_transition = True
        """
        from strategy.registry import get_strategy
        from utils.config_loader import get_config

        result = self.evaluate(df, symbol)
        name   = result["strategy_name"]

        # V1.5: 更新过渡期计数
        self._bars_since_switch += 1
        self.in_transition = (self._bars_since_switch <= self._transition_bars)

        # WAIT 状态或无策略推荐 → V5.2: WAIT 有策略名了，仅 name 为空才跳过
        if not name:
            logger.info(f"📋 观望状态: {result['reason']}")
            return None, result

        if name == self._current_strategy_name:
            return None, result   # 策略未变，返回 None 表示无需切换

        self._current_strategy_name = name
        # V1.5: 策略切换，重置过渡期计数
        self._bars_since_switch = 0
        self.in_transition = True

        # 读取策略参数（从 config.yaml 的 selector.strategy_params 节）
        cfg      = get_config()
        s_params = cfg.get("selector", {}).get("strategy_params", {})
        params   = s_params.get(name, {})
        try:
            strategy = get_strategy(name, **params)
            return strategy, result
        except KeyError as e:
            logger.error(f"策略 {name} 未注册: {e}")
            return None, result
