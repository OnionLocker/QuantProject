# -*- coding: utf-8 -*-
"""
strategy/trend_momentum_v3.py - TrendMomentum V3 (Squeeze Breakout)

V3 enhancements over V2:
  - Squeeze filter: BB(20,2) must have been fully inside KC(20,1.5)
    at least once in the last 10 bars (volatility compression)
  - Strong candle confirmation: close must be in top 25% of bar range
    (for longs) or bottom 25% (for shorts)
  - Scale-out exit: close 50% at 1R profit, move SL to breakeven,
    trail remainder with EMA(50)
  - EMA(50) dynamic trailing for remaining position

Entry (LONG, all must be true):
  A) Close > EMA(120) AND EMA(20) > EMA(120)
  B) Squeeze detected in last 10 bars (BB inside KC)
  C) Close > Donchian(24) upper AND close in top 25% of bar range
  D) ATR(14) < 1.5 * SMA(ATR, 30)

Exit:
  - Initial SL: entry - ATR * 3.0
  - Scale out 50% at +1R (ATR * 3.0 profit)
  - After scale-out: SL moves to breakeven
  - Remaining 50%: exit when close < EMA(50) OR time stop at 72 bars
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class TrendMomentumV3Strategy(BaseStrategy):

    PARAMS = [
        {"key": "channel_period", "label": "Donchian period",
         "type": "int", "default": 24, "min": 12, "max": 72, "step": 4},
        {"key": "fast_ema", "label": "Fast EMA",
         "type": "int", "default": 20, "min": 10, "max": 60, "step": 5},
        {"key": "trend_ema", "label": "Slow/Trend EMA",
         "type": "int", "default": 120, "min": 50, "max": 200, "step": 10},
        {"key": "exit_ema", "label": "Exit EMA (trailing)",
         "type": "int", "default": 50, "min": 20, "max": 100, "step": 5},
        {"key": "atr_sl_mult", "label": "SL ATR mult",
         "type": "float", "default": 3.0, "min": 1.5, "max": 5.0, "step": 0.5},
        {"key": "cooldown", "label": "Signal cooldown",
         "type": "int", "default": 16, "min": 4, "max": 30, "step": 2},
    ]

    def __init__(
        self,
        channel_period: int   = 24,
        fast_ema:       int   = 20,
        trend_ema:      int   = 120,
        exit_ema:       int   = 50,
        atr_period:     int   = 14,
        atr_sl_mult:    float = 3.0,
        max_atr_mult:   float = 1.5,
        bb_period:      int   = 20,
        bb_std:         float = 2.0,
        kc_period:      int   = 20,
        kc_mult:        float = 1.5,
        squeeze_lookback: int = 10,
        candle_strength: float = 0.25,
        cooldown:       int   = 16,
        time_stop_bars: int   = 72,
        scale_out_r:    float = 1.0,
        scale_out_pct:  float = 0.5,
    ):
        super().__init__(name="TM_V3_Squeeze")
        self.channel_period   = channel_period
        self.fast_ema         = fast_ema
        self.trend_ema        = trend_ema
        self.exit_ema         = exit_ema
        self.atr_period       = atr_period
        self.atr_sl_mult      = atr_sl_mult
        self.max_atr_mult     = max_atr_mult
        self.bb_period        = bb_period
        self.bb_std           = bb_std
        self.kc_period        = kc_period
        self.kc_mult          = kc_mult
        self.squeeze_lookback = squeeze_lookback
        self.candle_strength  = candle_strength
        self.cooldown         = cooldown
        self.time_stop_bars   = time_stop_bars
        self.scale_out_r      = scale_out_r
        self.scale_out_pct    = scale_out_pct
        self.warmup_bars      = max(channel_period, trend_ema, bb_period,
                                    kc_period, atr_period, exit_ema) + 40

    def calc_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        H, L, C, V = df['high'], df['low'], df['close'], df['volume']
        prev_c = C.shift(1)
        tr = pd.concat([
            H - L, (H - prev_c).abs(), (L - prev_c).abs(),
        ], axis=1).max(axis=1)

        df['atr']          = tr.rolling(self.atr_period).mean()
        df['atr_ma']       = df['atr'].rolling(30).mean()
        df['fast_ema']     = C.ewm(span=self.fast_ema, adjust=False).mean()
        df['trend_ema']    = C.ewm(span=self.trend_ema, adjust=False).mean()
        df['exit_ema']     = C.ewm(span=self.exit_ema, adjust=False).mean()
        df['channel_high'] = H.rolling(self.channel_period).max().shift(1)
        df['channel_low']  = L.rolling(self.channel_period).min().shift(1)

        # Bollinger Bands
        bb_ma  = C.rolling(self.bb_period).mean()
        bb_std = C.rolling(self.bb_period).std()
        df['bb_upper'] = bb_ma + self.bb_std * bb_std
        df['bb_lower'] = bb_ma - self.bb_std * bb_std

        # Keltner Channel
        kc_ma = C.ewm(span=self.kc_period, adjust=False).mean()
        kc_atr = tr.rolling(self.kc_period).mean()
        df['kc_upper'] = kc_ma + self.kc_mult * kc_atr
        df['kc_lower'] = kc_ma - self.kc_mult * kc_atr

        # Squeeze: BB fully inside KC
        df['squeeze'] = (df['bb_upper'] < df['kc_upper']) & \
                        (df['bb_lower'] > df['kc_lower'])
        # Squeeze in last N bars
        df['squeeze_recent'] = df['squeeze'].rolling(
            self.squeeze_lookback, min_periods=1
        ).max().astype(bool)

        # Candle strength: where close sits in the H-L range (0=low, 1=high)
        bar_range = H - L
        bar_range = bar_range.replace(0, np.nan)
        df['candle_pos'] = (C - L) / bar_range

        return df

    def check_entry(self, df: pd.DataFrame, j: int) -> str | None:
        """Check V3 entry conditions at completed bar j."""
        C  = df['close'].values
        H  = df['high'].values
        L  = df['low'].values
        ATR = df['atr'].values
        CH  = df['channel_high'].values
        CL  = df['channel_low'].values
        FEMA = df['fast_ema'].values
        TEMA = df['trend_ema'].values
        ATR_MA = df['atr_ma'].values
        SQZ  = df['squeeze_recent'].values
        CPOS = df['candle_pos'].values

        if np.isnan(ATR[j]) or ATR[j] <= 0:
            return None
        if np.isnan(CH[j]) or np.isnan(CL[j]):
            return None

        # D: ATR filter (no exhaustion candles)
        if ATR_MA[j] > 0 and ATR[j] > ATR_MA[j] * self.max_atr_mult:
            return None

        # B: Squeeze must have occurred recently
        if not SQZ[j]:
            return None

        # --- LONG ---
        if (C[j] > TEMA[j]                         # A: above trend
                and FEMA[j] > TEMA[j]               # A: fast > slow
                and C[j] > CH[j]                    # C: Donchian breakout
                and CPOS[j] >= (1 - self.candle_strength)  # C: strong candle
                ):
            return 'BUY'

        # --- SHORT ---
        if (C[j] < TEMA[j]
                and FEMA[j] < TEMA[j]
                and C[j] < CL[j]
                and CPOS[j] <= self.candle_strength
                ):
            return 'SELL'

        return None

    # -- Live trading interface (compatible with runner) --------------------

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {
            "action": "HOLD", "entry": 0.0, "sl": 0.0,
            "tp1": 0.0, "tp2": 0.0, "risk_r": 0.0,
            "reason": "wait", "meta": {},
        }
        need = self.warmup_bars + 5
        if df is None or len(df) < need:
            return sig

        df = self.calc_all_indicators(df.iloc[-need:].copy())
        j = len(df) - 2
        if j < 1:
            return sig

        action = self.check_entry(df, j)
        if action is None:
            return sig

        atr = df['atr'].values[j]
        sl_dist = atr * self.atr_sl_mult
        entry = float(df['open'].iloc[-1])

        if action == 'BUY':
            sig.update({
                "action": "BUY", "entry": entry,
                "sl": entry - sl_dist,
                "tp1": entry + sl_dist * self.scale_out_r,
                "reason": f"LONG squeeze breakout | C={df['close'].values[j]:.0f}",
            })
        else:
            sig.update({
                "action": "SELL", "entry": entry,
                "sl": entry + sl_dist,
                "tp1": entry - sl_dist * self.scale_out_r,
                "reason": f"SHORT squeeze breakout | C={df['close'].values[j]:.0f}",
            })
        return sig


# =========================================================================
# Custom backtest with scale-out support
# =========================================================================

def run_v3_backtest(
    symbol:    str   = 'BTC/USDT',
    timeframe: str   = '1h',
    start_date: str  = '2020-01-01',
    end_date:   str  = '2026-03-25',
    initial_capital: float = 5000.0,
    leverage:  int   = 5,
    risk_pct:  float = 0.01,
    fee_rate:  float = 0.0006,
    slippage:  float = 0.0003,
    silent:    bool  = True,
    **strategy_params,
) -> dict:
    """Full V3 backtest with scale-out, EMA trailing, time stop."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from data.market_data import fetch_history_range

    strat = TrendMomentumV3Strategy(**strategy_params)
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) < strat.warmup_bars + 50:
        return {"status": "error", "error": "insufficient data"}

    df = strat.calc_all_indicators(df)

    balance = initial_capital
    peak_balance = balance
    max_dd = 0.0
    trades = []
    cooldown_until = 0

    # Position state
    pos_side = None       # 'long' or 'short' or None
    pos_entry = 0.0
    pos_size_full = 0.0   # full position in contracts (notional)
    pos_size_cur = 0.0    # current remaining
    pos_sl = 0.0
    pos_entry_bar = 0
    scaled_out = False
    scale_out_pnl = 0.0

    C  = df['close'].values
    O  = df['open'].values
    H  = df['high'].values
    L  = df['low'].values
    ATR = df['atr'].values
    EXIT_EMA = df['exit_ema'].values

    for i in range(strat.warmup_bars, len(df)):
        price = C[i]

        # Update drawdown
        if balance > peak_balance:
            peak_balance = balance
        dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # --- In position: check exits ---
        if pos_side is not None:
            bars_held = i - pos_entry_bar
            is_long = (pos_side == 'long')

            # Check SL hit (using high/low of current bar)
            sl_hit = (L[i] <= pos_sl) if is_long else (H[i] >= pos_sl)
            if sl_hit:
                exit_price = pos_sl
                pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                gross_pnl = pnl_per_unit * pos_size_cur * leverage
                fee = abs(pos_size_cur * exit_price * leverage) * fee_rate
                net_pnl = gross_pnl - fee + scale_out_pnl
                balance += net_pnl
                trades.append({
                    "entry_ts": str(df.index[pos_entry_bar]),
                    "exit_ts": str(df.index[i]),
                    "side": pos_side,
                    "entry": pos_entry, "exit": exit_price,
                    "pnl": round(net_pnl, 2),
                    "exit_reason": "SL" + (" (breakeven)" if scaled_out else ""),
                    "bars": bars_held,
                })
                pos_side = None
                cooldown_until = i + strat.cooldown
                scale_out_pnl = 0.0
                continue

            # Check scale-out at 1R (if not done yet)
            if not scaled_out:
                sl_dist = abs(pos_entry - pos_sl)
                tp_level = (pos_entry + sl_dist) if is_long else (pos_entry - sl_dist)
                tp_hit = (H[i] >= tp_level) if is_long else (L[i] <= tp_level)
                if tp_hit:
                    exit_qty = pos_size_full * strat.scale_out_pct
                    pnl_per_unit = (tp_level - pos_entry) if is_long else (pos_entry - tp_level)
                    gross_pnl = pnl_per_unit * exit_qty * leverage
                    fee = abs(exit_qty * tp_level * leverage) * fee_rate
                    scale_out_pnl += gross_pnl - fee
                    pos_size_cur -= exit_qty
                    # Move SL to breakeven
                    pos_sl = pos_entry
                    scaled_out = True

            # After scale-out: EMA trailing exit (close below EMA50 for long)
            if scaled_out:
                ema_exit = False
                if is_long and C[i] < EXIT_EMA[i]:
                    ema_exit = True
                elif not is_long and C[i] > EXIT_EMA[i]:
                    ema_exit = True

                if ema_exit:
                    exit_price = C[i]
                    pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                    gross_pnl = pnl_per_unit * pos_size_cur * leverage
                    fee = abs(pos_size_cur * exit_price * leverage) * fee_rate
                    net_pnl = gross_pnl - fee + scale_out_pnl
                    balance += net_pnl
                    trades.append({
                        "entry_ts": str(df.index[pos_entry_bar]),
                        "exit_ts": str(df.index[i]),
                        "side": pos_side,
                        "entry": pos_entry, "exit": exit_price,
                        "pnl": round(net_pnl, 2),
                        "exit_reason": f"EMA({strat.exit_ema}) trail",
                        "bars": bars_held,
                    })
                    pos_side = None
                    cooldown_until = i + strat.cooldown
                    scale_out_pnl = 0.0
                    continue

            # Time stop
            if bars_held >= strat.time_stop_bars:
                exit_price = C[i]
                pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                gross_pnl = pnl_per_unit * pos_size_cur * leverage
                fee = abs(pos_size_cur * exit_price * leverage) * fee_rate
                net_pnl = gross_pnl - fee + scale_out_pnl
                balance += net_pnl
                trades.append({
                    "entry_ts": str(df.index[pos_entry_bar]),
                    "exit_ts": str(df.index[i]),
                    "side": pos_side,
                    "entry": pos_entry, "exit": exit_price,
                    "pnl": round(net_pnl, 2),
                    "exit_reason": "time stop",
                    "bars": bars_held,
                })
                pos_side = None
                cooldown_until = i + strat.cooldown
                scale_out_pnl = 0.0
                continue

        # --- No position: check entry ---
        if pos_side is None and i > cooldown_until:
            j = i - 1
            action = strat.check_entry(df, j)
            if action is not None and not np.isnan(ATR[j]):
                entry_price = O[i] * (1 + slippage if action == 'BUY' else 1 - slippage)
                sl_dist = ATR[j] * strat.atr_sl_mult

                risk_amount = balance * risk_pct
                pos_size_full = risk_amount / (sl_dist * leverage)
                pos_size_cur = pos_size_full

                open_fee = pos_size_full * entry_price * leverage * fee_rate
                balance -= open_fee

                pos_side = 'long' if action == 'BUY' else 'short'
                pos_entry = entry_price
                pos_sl = (entry_price - sl_dist) if action == 'BUY' else (entry_price + sl_dist)
                pos_entry_bar = i
                scaled_out = False
                scale_out_pnl = 0.0

    # Close any open position at end
    if pos_side is not None:
        exit_price = C[-1]
        is_long = pos_side == 'long'
        pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
        gross_pnl = pnl_per_unit * pos_size_cur * leverage
        fee = abs(pos_size_cur * exit_price * leverage) * fee_rate
        net_pnl = gross_pnl - fee + scale_out_pnl
        balance += net_pnl
        trades.append({
            "entry_ts": str(df.index[pos_entry_bar]),
            "exit_ts": str(df.index[-1]),
            "side": pos_side,
            "entry": pos_entry, "exit": exit_price,
            "pnl": round(net_pnl, 2),
            "exit_reason": "end of data",
            "bars": len(df) - 1 - pos_entry_bar,
        })

    # Compute metrics
    total = len(trades)
    winners = [t for t in trades if t['pnl'] > 0]
    losers  = [t for t in trades if t['pnl'] <= 0]
    win_count = len(winners)
    avg_win = np.mean([t['pnl'] for t in winners]) if winners else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losers]) if losers else 0
    pf = avg_win / avg_loss if avg_loss > 0 else 0
    win_rate = win_count / total * 100 if total > 0 else 0

    roi = (balance - initial_capital) / initial_capital * 100

    # Final DD update
    if balance > peak_balance:
        peak_balance = balance
    dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
    if dd > max_dd:
        max_dd = dd

    # Daily returns for Sharpe
    daily_pnls = {}
    for t in trades:
        day = t['exit_ts'][:10]
        daily_pnls[day] = daily_pnls.get(day, 0) + t['pnl']
    if daily_pnls:
        rets = np.array(list(daily_pnls.values()))
        sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    else:
        sharpe = 0

    return {
        "status": "done",
        "roi_pct": round(roi, 2),
        "max_drawdown_pct": round(max_dd, 1),
        "total_trades": total,
        "win_count": win_count,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(pf, 3),
        "sharpe_ratio": round(sharpe, 3),
        "final_balance": round(balance, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "trades": trades,
        "exit_reasons": _count_exits(trades),
    }


def _count_exits(trades):
    reasons = {}
    for t in trades:
        r = t['exit_reason']
        reasons[r] = reasons.get(r, 0) + 1
    return reasons
