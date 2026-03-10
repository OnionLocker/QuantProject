"""
strategy/selector.py - 市场状态判断 + 策略自动选择器

综合「技术面」+「新闻面」得出当前市场所处阶段：
  - bull   (牛市)  → TrendBullStrategy
  - bear   (熊市)  → TrendBearStrategy
  - ranging(震荡)  → RangeOscillatorStrategy
  - (可在 config.yaml 覆盖各阶段策略)

技术面评分（RegimeDetector）：
  - ADX > 25 且 EMA 向上排列 → 牛市信号
  - ADX > 25 且 EMA 向下排列 → 熊市信号
  - ADX < 20                 → 震荡信号
  - 布林带宽度收窄             → 震荡信号加分

新闻面评分（news_fetcher）：
  - 情绪分 > 阈值              → 看涨倾向
  - 情绪分 < 阈值              → 看跌倾向
  - 无新闻数据/超时             → 忽略，纯靠技术面

最终决策：技术面权重 70% + 新闻面权重 30%（可在 config.yaml 调整）
切换保护：状态连续 N 根 K 线确认才切换，防抖。
"""

import logging
import time
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
        self.adx_bull_thresh  = sc.get("adx_bull_thresh",  25)   # ADX > 此值 = 有趋势
        self.adx_range_thresh = sc.get("adx_range_thresh", 20)   # ADX < 此值 = 震荡
        self.ema_short        = sc.get("ema_short",        20)
        self.ema_mid          = sc.get("ema_mid",          50)
        self.ema_long         = sc.get("ema_long",        200)
        self.bb_period        = sc.get("bb_period",        20)
        self.bb_squeeze_pct   = sc.get("bb_squeeze_pct",   0.03) # 带宽/中轨 < 此值 = 挤压

        # 新闻面参数
        self.news_weight      = sc.get("news_weight",      0.3)   # 新闻权重
        self.tech_weight      = sc.get("tech_weight",      0.7)   # 技术面权重
        self.news_max_age_min = sc.get("news_max_age_min", 120)   # 超过此分钟数视为过期

        # 切换保护：连续 N 根K线同一 regime 才正式切换
        self.confirm_bars     = sc.get("confirm_bars",     3)

        # 各 regime 对应的策略名（可在 config.yaml 覆盖）
        self.strategy_map = {
            REGIME_BULL:     sc.get("strategy_bull",     "BULL"),
            REGIME_BEAR:     sc.get("strategy_bear",     "BEAR"),
            REGIME_RANGING:  sc.get("strategy_ranging",  "RANGE"),
            REGIME_BREAKOUT: sc.get("strategy_breakout", "BIG_CANDLE"),
        }

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

        # ADX 趋势强度
        if adx_val > self.adx_bull_thresh:
            adx_strength = min((adx_val - self.adx_bull_thresh) / 20, 1.0)
            # +DI 和 -DI 决定方向
            if pdi_val > mdi_val:
                score += adx_strength * 2.0
            else:
                score -= adx_strength * 2.0
        else:
            # ADX 低 → 偏向震荡
            range_strength = min((self.adx_bull_thresh - adx_val) / 15, 1.0)
            score *= (1 - range_strength * 0.5)

        # EMA 排列
        if es_val > em_val > el_val and close_j > el_val:
            # 多头排列：强烈看涨
            score += 1.5
        elif es_val < em_val < el_val and close_j < el_val:
            # 空头排列：强烈看跌
            score -= 1.5
        elif abs(es_val - em_val) / em_val < 0.01:
            # EMA 几乎重合：震荡
            score *= 0.5

        # 价格与长期EMA的关系
        if close_j > el_val * 1.02:
            score += 0.5
        elif close_j < el_val * 0.98:
            score -= 0.5

        # 布林带挤压 → 震荡信号
        if bw_val < self.bb_squeeze_pct:
            score *= 0.3   # 挤压期信号衰减

        # 决策
        confidence = min(abs(score) / 4.0, 1.0)
        # ADX 极强（>40）且价格在长期均线上方 → 强势突破 regime
        if adx_val > 40 and score >= 1.5:
            return REGIME_BREAKOUT, min(confidence * 1.2, 1.0)
        if score >= 1.5:
            return REGIME_BULL, confidence
        elif score <= -1.5:
            return REGIME_BEAR, confidence
        else:
            return REGIME_RANGING, confidence

    # ── 新闻面情绪 ────────────────────────────────────────────────────────────

    def _get_news_regime(self) -> tuple[str, float]:
        """
        读取数据库最新新闻情绪，返回 (regime_hint, confidence)。
        超时/无数据返回 (UNKNOWN, 0.0)。
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
            return regime, conf
        except Exception as e:
            logger.warning(f"读取新闻情绪失败: {e}")
            return REGIME_UNKNOWN, 0.0

    # ── 综合决策 ──────────────────────────────────────────────────────────────

    def evaluate(self, df: pd.DataFrame) -> dict:
        """
        综合技术面 + 新闻面，输出当前市场状态判断。

        返回：{
            "regime":      str,   # bull/bear/ranging/unknown
            "confidence":  float, # [0,1]
            "tech_regime": str,
            "news_regime": str,
            "reason":      str,   # 可读说明
            "strategy_name": str, # 推荐策略名
        }
        """
        # 技术面（带缓存，1分钟内不重算）
        now = time.time()
        if now - self._last_tech_calc_time > self._tech_cache_seconds:
            self._last_tech_regime, self._last_tech_conf = self._calc_tech_regime(df)
            self._last_tech_calc_time = now
        tech_regime = self._last_tech_regime
        tech_conf   = self._last_tech_conf

        # 新闻面（news_weight=0 时直接跳过，避免无意义的 import 和 DB 查询）
        if self.news_weight > 0:
            news_regime, news_conf = self._get_news_regime()
        else:
            news_regime, news_conf = REGIME_UNKNOWN, 0.0

        # ── 加权投票 ─────────────────────────────────────────────────────────
        votes = {REGIME_BULL: 0.0, REGIME_BEAR: 0.0, REGIME_RANGING: 0.0, REGIME_BREAKOUT: 0.0}

        # 技术面投票
        if tech_regime != REGIME_UNKNOWN:
            votes[tech_regime] += self.tech_weight * (0.5 + tech_conf * 0.5)

        # 新闻面投票
        if news_regime != REGIME_UNKNOWN:
            votes[news_regime] += self.news_weight * (0.5 + news_conf * 0.5)

        # 无投票时返回 unknown
        total_votes = sum(votes.values())
        if total_votes == 0:
            regime    = REGIME_UNKNOWN
            confidence = 0.0
        else:
            regime    = max(votes, key=votes.get)
            confidence = votes[regime] / total_votes

        # ── 防抖保护：连续 confirm_bars 根K线确认才正式切换 ─────────────────
        if regime == self._pending_regime:
            self._pending_count += 1
        else:
            self._pending_regime = regime
            self._pending_count  = 1

        if self._pending_count >= self.confirm_bars:
            if regime != self._confirmed_regime:
                logger.info(
                    f"🔄 市场状态切换: {self._confirmed_regime} → {regime} "
                    f"(置信度={confidence:.2f}, 技术={tech_regime}, 新闻={news_regime})"
                )
            self._confirmed_regime = regime

        final_regime = self._confirmed_regime
        strategy_name = self.strategy_map.get(final_regime, "")

        reason = (
            f"技术面={tech_regime}(conf={tech_conf:.2f}) "
            f"新闻面={news_regime}(conf={news_conf:.2f}) "
            f"→ 确认={final_regime} 策略={strategy_name or '未配置'}"
        )

        return {
            "regime":        final_regime,
            "confidence":    round(confidence, 3),
            "tech_regime":   tech_regime,
            "news_regime":   news_regime,
            "reason":        reason,
            "strategy_name": strategy_name,
        }

    def get_strategy(self, df: pd.DataFrame):
        """
        直接返回当前推荐的策略实例（懒加载，按需切换）。
        如果状态未变化则复用旧实例，避免重复实例化。
        """
        from strategy.registry import get_strategy
        from utils.config_loader import get_config

        result = self.evaluate(df)
        name   = result["strategy_name"]

        if not name:
            logger.warning("选择器没有推荐策略，使用上一个策略")
            return None, result

        if name == self._current_strategy_name:
            return None, result   # 策略未变，返回 None 表示无需切换

        self._current_strategy_name = name

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
