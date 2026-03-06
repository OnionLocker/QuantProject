import pandas as pd
import numpy as np
from strategy.base import BaseStrategy
import warnings  # 👈 新增：引入警告控制模块
warnings.simplefilter(action='ignore', category=FutureWarning)
pd.set_option('future.no_silent_downcasting', True) # 顺便满足它的底层强迫症

class PriceActionV2(BaseStrategy):
    def __init__(self, swing_l=8, atr_period=14, buffer_atr_mult=0.1, rr1=1.0, rr2=2.5):
        # swing_l=8 是为了在 1H 级别图表上模拟出 4H 级别的结构 (4H高低点需要更长时间跨度)
        super().__init__(name="PA_V2_市场结构与假突破")
        self.swing_l = swing_l
        self.atr_period = atr_period
        self.buffer_mult = buffer_atr_mult
        self.rr1 = rr1
        self.rr2 = rr2

    def calculate_atr(self, df):
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(self.atr_period).mean()

    def identify_swings(self, df):
        highs = df['high']
        lows = df['low']
        # 严格使用过去的窗口，中心对齐找极值，再向后平移以消除未来函数
        swing_highs = highs == highs.rolling(window=self.swing_l * 2 + 1, center=True).max()
        swing_lows = lows == lows.rolling(window=self.swing_l * 2 + 1, center=True).min()
        
        # 💡 修复报错点：平移后产生的 NaN 必须用 fillna(False) 填补，否则 pandas 无法做布尔筛选
        return swing_highs.shift(self.swing_l).fillna(False).astype(bool), swing_lows.shift(self.swing_l).fillna(False).astype(bool)

    def generate_signal(self, df):
        # 默认空信号 (HOLD)
        sig = {"action": "HOLD", "entry": 0.0, "sl": 0.0, "tp1": 0.0, "tp2": 0.0, "risk_r": 0.0, "reason": "观望", "meta": {}}

        if df is None or len(df) < max(self.atr_period, self.swing_l * 2 + 5):
            return sig

        df = df.copy()
        df['atr'] = self.calculate_atr(df)
        df['swing_high'], df['swing_low'] = self.identify_swings(df)

        # 严格提取：当前正在跳动的K线(-1) 和 上一根已彻底收盘的K线(-2)
        current_k = df.iloc[-1]
        prev_k = df.iloc[-2]
        
        current_atr = prev_k['atr'] # 使用已收盘的ATR
        buffer = current_atr * self.buffer_mult

        recent_highs = df[df['swing_high']].dropna()
        recent_lows = df[df['swing_low']].dropna()

        if len(recent_highs) < 2 or len(recent_lows) < 2:
            return sig

        last_high = recent_highs['high'].iloc[-1]
        last_low = recent_lows['low'].iloc[-1]

        # 波动率过滤：突破K线(prev_k)的振幅必须大于近期均值1.2倍
        avg_amplitude = df['high'].iloc[-6:-1].sub(df['low'].iloc[-6:-1]).mean()
        is_high_volatility = (prev_k['high'] - prev_k['low']) > (avg_amplitude * 1.2)

        # ==========================================
        # 模型 B: 逆势假突破 (Fakeout) - 扫流动性
        # ==========================================
        # 向上假突破：刺破前高+缓冲带，但最终收盘跌回前高之下
        fakeout_up = prev_k['high'] > (last_high + buffer) and prev_k['close'] < last_high
        # 向下假突破：刺破前低-缓冲带，但最终收盘涨回前低之上
        fakeout_down = prev_k['low'] < (last_low - buffer) and prev_k['close'] > last_low

        if fakeout_up and is_high_volatility:
            entry = current_k['open'] # 确认跌破后准备入场
            sl = prev_k['high'] + buffer
            risk = sl - entry
            if risk > 0:
                sig.update({"action": "SELL", "entry": entry, "sl": sl, "tp1": entry - risk*self.rr1, "tp2": entry - risk*self.rr2, "risk_r": risk, "reason": "🔴 模型B: 向上假突破前高 (扫流动性)"})
                return sig

        if fakeout_down and is_high_volatility:
            entry = current_k['open']
            sl = prev_k['low'] - buffer
            risk = entry - sl
            if risk > 0:
                sig.update({"action": "BUY", "entry": entry, "sl": sl, "tp1": entry + risk*self.rr1, "tp2": entry + risk*self.rr2, "risk_r": risk, "reason": "🟢 模型B: 向下假突破前低 (扫流动性)"})
                return sig

        # ==========================================
        # 模型 A: 顺势 BOS + 回踩 (简化判定版)
        # ==========================================
        # 定义上升趋势：收盘突破前高
        is_uptrend = prev_k['close'] > (last_high + buffer)
        is_downtrend = prev_k['close'] < (last_low - buffer)

        # 看涨吞没 (Bull Engulfing)
        bull_engulf = (df.iloc[-3]['close'] < df.iloc[-3]['open']) and (prev_k['close'] > prev_k['open']) and (prev_k['close'] > df.iloc[-3]['open'])

        # 看跌吞没 (Bear Engulfing)
        bear_engulf = (df.iloc[-3]['close'] > df.iloc[-3]['open']) and (prev_k['close'] < prev_k['open']) and (prev_k['close'] < df.iloc[-3]['open'])

        # 顺势做多：处于上升结构中，回踩前高附近，且出现看涨吞没
        if is_uptrend and (last_high - buffer * 2 <= prev_k['low'] <= last_high + buffer * 2) and bull_engulf:
            entry = current_k['open']
            sl = prev_k['low'] - buffer
            risk = entry - sl
            if risk > 0:
                sig.update({"action": "BUY", "entry": entry, "sl": sl, "tp1": entry + risk*self.rr1, "tp2": entry + risk*self.rr2, "risk_r": risk, "reason": "🟢 模型A: 上升趋势 BOS 回踩确认"})
                return sig

        # 顺势做空：处于下降结构中，回踩前低附近，且出现看跌吞没
        if is_downtrend and (last_low - buffer * 2 <= prev_k['high'] <= last_low + buffer * 2) and bear_engulf:
            entry = current_k['open']
            sl = prev_k['high'] + buffer
            risk = sl - entry
            if risk > 0:
                sig.update({"action": "SELL", "entry": entry, "sl": sl, "tp1": entry - risk*self.rr1, "tp2": entry - risk*self.rr2, "risk_r": risk, "reason": "🔴 模型A: 下降趋势 BOS 回踩确认"})
                return sig

        return sig