import pandas as pd
from strategy.base import BaseStrategy

class PriceActionStrategy(BaseStrategy):
    def __init__(self, lookback=10, ema_period=50):
        # 默认看过去 10 根 K 线的高低点，并且用 50 周期均线判断大趋势
        super().__init__(name="1小时(1H)顺势突破策略(PA+EMA)")
        self.lookback = lookback
        self.ema_period = ema_period

    def generate_signal(self, df):
        # 为了不影响原始数据，我们复制一份来计算
        df = df.copy()
        
        # 需要足够的数据来计算 EMA 和找前高低
        if df is None or len(df) < max(self.lookback + 2, self.ema_period):
            return "HOLD", "数据不足，继续观望。"

        # 📈 计算 EMA (指数移动平均线)
        df['ema'] = df['close'].ewm(span=self.ema_period, adjust=False).mean()

        last_closed_candle = df.iloc[-2]
        
        current_open = last_closed_candle['open']
        current_close = last_closed_candle['close']
        current_high = last_closed_candle['high']
        current_low = last_closed_candle['low']
        current_ema = last_closed_candle['ema']

        # 找出近期的天花板(前高)和地板(前低)
        previous_candles = df.iloc[-(self.lookback + 2):-2]
        recent_high = previous_candles['high'].max()
        recent_low = previous_candles['low'].min()

        candle_range = current_high - current_low
        if candle_range == 0:
            return "HOLD", "十字星，没有交易信号。"

        # ==========================================
        # 🛡️ 新增：大趋势判断 (Trend Filter)
        # ==========================================
        is_uptrend = current_close > current_ema
        is_downtrend = current_close < current_ema

        # ==========================================
        # 🟢 1. 做多逻辑 (向上突破 + 必须在均线之上)
        # ==========================================
        is_bull_candle = current_close > current_open
        close_in_upper_half = (current_high - current_close) < (candle_range / 2)
        is_strong_bull_bar = is_bull_candle and close_in_upper_half

        if is_strong_bull_bar and (current_close > recent_high):
            if is_uptrend:
                return "BUY", f"🟢 顺势做多！多头突破前高，且在 EMA{self.ema_period} 之上。"
            else:
                return "HOLD", f"⚠️ 拒绝做多：虽然突破前高，但处于EMA{self.ema_period}之下的大跌趋势中(防骗炮)。"

        # ==========================================
        # 🔴 2. 做空逻辑 (向下突破 + 必须在均线之下)
        # ==========================================
        is_bear_candle = current_close < current_open
        close_in_lower_half = (current_close - current_low) < (candle_range / 2)
        is_strong_bear_bar = is_bear_candle and close_in_lower_half

        if is_strong_bear_bar and (current_close < recent_low):
            if is_downtrend:
                return "SELL", f"🔴 顺势做空！空头砸穿前低，且在 EMA{self.ema_period} 之下。"
            else:
                return "HOLD", f"⚠️ 拒绝做空：虽然跌破前低，但处于EMA{self.ema_period}之上的大涨趋势中(防骗炮)。"

        # ==========================================
        # ⚪ 3. 震荡观望
        # ==========================================
        return "HOLD", "⚪ 价格仍在区间内震荡，或未触发顺势条件。"