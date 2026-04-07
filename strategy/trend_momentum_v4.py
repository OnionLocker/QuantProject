# -*- coding: utf-8 -*-
"""
strategy/trend_momentum_v4.py - TrendMomentum V4 (PA-Filtered Pullback)

V4 upgrades over V3:
  1. Climax & Wick filters: reject exhaustion breakouts via EMA deviation,
     climax candle body size, and adverse wick ratio checks.
  2. TBTL pullback entry: instead of entering on breakout bar, wait for
     price to pull back to fast EMA then print a continuation candle.
     Max observation window before timeout.
  3. Chandelier Exit: pure ATR-based trailing stop that ratchets in the
     profit direction once price moves beyond 1.5R. Replaces V3's
     scale-out + EMA trailing logic.

Entry flow:
  Phase 1 - Squeeze breakout detection (V3 core, relaxed):
    A) Close vs trend_ema alignment + fast_ema vs trend_ema
    B) Squeeze detected in recent N bars (BB inside KC, kc_mult=2.0)
    C) Donchian breakout + candle in 40% strength zone
  Phase 1b - PA quality filters:
    E) EMA deviation < max_ema_deviation_pct (no climax chase)
    F) Candle body < climax_atr_mult * atr_ma (no exhaustion bar)
    G) Adverse wick ratio < max_wick_ratio (clean breakout candle)
  Phase 2 - Pullback confirmation (TBTL):
    H) Within pullback_bars window, price touches pullback_ema
    I) After touch, a continuation candle prints in the breakout direction
    J) Abort if price invalidates the breakout origin

Exit (backtest only - Chandelier Exit):
  - Initial SL: entry +/- ATR * atr_sl_mult
  - Trail activation: unrealized profit > 1.5 * initial risk
  - Trail line: extreme price -/+ ATR * trail_atr_mult (ratchet only)
  - Time stop: max time_stop_bars in position
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy


class TrendMomentumV4Strategy(BaseStrategy):

    PARAMS = [
        # --- Channel / Trend ---
        {"key": "channel_period", "label": "Donchian period",
         "type": "int", "default": 24, "min": 12, "max": 72, "step": 4,
         "tip": "Donchian channel lookback (24 = 1 day on 1h)"},
        {"key": "fast_ema", "label": "Fast EMA",
         "type": "int", "default": 20, "min": 10, "max": 60, "step": 5},
        {"key": "trend_ema", "label": "Slow/Trend EMA",
         "type": "int", "default": 120, "min": 50, "max": 200, "step": 10},
        # --- SL / Risk ---
        {"key": "atr_sl_mult", "label": "SL ATR multiplier",
         "type": "float", "default": 3.0, "min": 1.5, "max": 5.0, "step": 0.5,
         "tip": "Initial stop-loss = ATR * this value"},
        {"key": "cooldown", "label": "Signal cooldown (bars)",
         "type": "int", "default": 16, "min": 4, "max": 30, "step": 2},
        # --- PA Filters (NEW) ---
        {"key": "max_ema_deviation_pct", "label": "Max EMA deviation %",
         "type": "float", "default": 0.08, "min": 0.03, "max": 0.15, "step": 0.01,
         "tip": "Reject breakout if price deviates >N% from trend EMA"},
        {"key": "climax_atr_mult", "label": "Climax candle ATR mult",
         "type": "float", "default": 2.5, "min": 1.5, "max": 4.0, "step": 0.5,
         "tip": "Reject if candle body > N * ATR_MA(30)"},
        {"key": "max_wick_ratio", "label": "Max adverse wick ratio",
         "type": "float", "default": 0.4, "min": 0.2, "max": 0.6, "step": 0.05,
         "tip": "Reject if adverse wick / bar range > this"},
        # --- Pullback Entry (NEW) ---
        {"key": "pullback_bars", "label": "Pullback window (bars)",
         "type": "int", "default": 10, "min": 5, "max": 20, "step": 1,
         "tip": "Max bars to wait for pullback confirmation"},
        {"key": "pullback_ema", "label": "Pullback EMA",
         "type": "int", "default": 20, "min": 10, "max": 50, "step": 5,
         "tip": "EMA to touch during pullback"},
        # --- Chandelier Exit (NEW) ---
        {"key": "trail_atr_mult", "label": "Trail ATR multiplier",
         "type": "float", "default": 2.0, "min": 1.0, "max": 4.0, "step": 0.5,
         "tip": "Chandelier trailing distance = ATR * this"},
    ]

    def __init__(
        self,
        channel_period: int    = 24,
        fast_ema:       int    = 20,
        trend_ema:      int    = 120,
        atr_period:     int    = 14,
        atr_sl_mult:    float  = 3.0,
        max_atr_mult:   float  = 1.5,
        bb_period:      int    = 20,
        bb_std:         float  = 2.0,
        kc_period:      int    = 20,
        kc_mult:        float  = 2.0,
        squeeze_lookback: int  = 5,
        candle_strength: float = 0.4,
        cooldown:       int    = 16,
        time_stop_bars: int    = 120,
        # V4 PA filters
        max_ema_deviation_pct: float = 0.08,
        climax_atr_mult:       float = 2.5,
        max_wick_ratio:        float = 0.4,
        # V4 pullback entry
        pullback_bars:  int    = 10,
        pullback_ema:   int    = 20,
        # V4 chandelier exit
        trail_atr_mult: float  = 2.0,
    ):
        super().__init__(name="TM_V4_Pullback")
        self.channel_period        = channel_period
        self.fast_ema              = fast_ema
        self.trend_ema             = trend_ema
        self.atr_period            = atr_period
        self.atr_sl_mult           = atr_sl_mult
        self.max_atr_mult          = max_atr_mult
        self.bb_period             = bb_period
        self.bb_std                = bb_std
        self.kc_period             = kc_period
        self.kc_mult               = kc_mult
        self.squeeze_lookback      = squeeze_lookback
        self.candle_strength       = candle_strength
        self.cooldown              = cooldown
        self.time_stop_bars        = time_stop_bars
        self.max_ema_deviation_pct = max_ema_deviation_pct
        self.climax_atr_mult       = climax_atr_mult
        self.max_wick_ratio        = max_wick_ratio
        self.pullback_bars         = pullback_bars
        self.pullback_ema          = pullback_ema
        self.trail_atr_mult        = trail_atr_mult

        self.warmup_bars = max(channel_period, trend_ema, bb_period,
                               kc_period, atr_period, pullback_ema) + 50

        # Pullback state machine (used by generate_signal for live trading)
        self._pending_dir: str | None = None   # 'BUY' or 'SELL'
        self._pending_bar: int = 0             # bar index when breakout detected
        self._pending_origin_low: float = 0.0  # breakout bar Low (invalidation for long)
        self._pending_origin_high: float = 0.0 # breakout bar High (invalidation for short)
        self._pullback_touched: bool = False   # whether price has touched pullback EMA
        self._live_bar_counter: int = 0        # monotonic counter for generate_signal calls

    # ------------------------------------------------------------------
    # Indicator computation (shared by live + backtest)
    # ------------------------------------------------------------------

    def calc_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        H, L, C, O = df['high'], df['low'], df['close'], df['open']
        prev_c = C.shift(1)
        tr = pd.concat([
            H - L, (H - prev_c).abs(), (L - prev_c).abs(),
        ], axis=1).max(axis=1)

        df['atr']          = tr.rolling(self.atr_period).mean()
        df['atr_ma']       = df['atr'].rolling(30).mean()
        df['fast_ema']     = C.ewm(span=self.fast_ema, adjust=False).mean()
        df['trend_ema']    = C.ewm(span=self.trend_ema, adjust=False).mean()
        df['pullback_ema'] = C.ewm(span=self.pullback_ema, adjust=False).mean()
        df['channel_high'] = H.rolling(self.channel_period).max().shift(1)
        df['channel_low']  = L.rolling(self.channel_period).min().shift(1)

        # Bollinger Bands
        bb_ma  = C.rolling(self.bb_period).mean()
        bb_std = C.rolling(self.bb_period).std()
        df['bb_upper'] = bb_ma + self.bb_std * bb_std
        df['bb_lower'] = bb_ma - self.bb_std * bb_std

        # Keltner Channel
        kc_ma  = C.ewm(span=self.kc_period, adjust=False).mean()
        kc_atr = tr.rolling(self.kc_period).mean()
        df['kc_upper'] = kc_ma + self.kc_mult * kc_atr
        df['kc_lower'] = kc_ma - self.kc_mult * kc_atr

        # Squeeze: BB fully inside KC
        df['squeeze'] = (df['bb_upper'] < df['kc_upper']) & \
                        (df['bb_lower'] > df['kc_lower'])
        df['squeeze_recent'] = df['squeeze'].rolling(
            self.squeeze_lookback, min_periods=1
        ).max().astype(bool)

        # Candle position: where close sits in the H-L range (0=low, 1=high)
        bar_range = H - L
        bar_range = bar_range.replace(0, np.nan)
        df['candle_pos'] = (C - L) / bar_range

        return df

    # ------------------------------------------------------------------
    # Phase 1: Squeeze breakout detection (V3 core + V4 PA filters)
    # ------------------------------------------------------------------

    def check_breakout(self, df: pd.DataFrame, j: int) -> str | None:
        """
        Check if completed bar j qualifies as a high-quality squeeze breakout.
        Returns 'BUY', 'SELL', or None.
        Does NOT trigger entry directly; the caller must feed this into the
        pullback state machine.
        """
        C    = df['close'].values
        H    = df['high'].values
        L    = df['low'].values
        O    = df['open'].values
        ATR  = df['atr'].values
        CH   = df['channel_high'].values
        CL   = df['channel_low'].values
        FEMA = df['fast_ema'].values
        TEMA = df['trend_ema'].values
        ATR_MA = df['atr_ma'].values
        SQZ  = df['squeeze_recent'].values
        CPOS = df['candle_pos'].values

        if np.isnan(ATR[j]) or ATR[j] <= 0:
            return None
        if np.isnan(CH[j]) or np.isnan(CL[j]):
            return None
        if np.isnan(ATR_MA[j]) or ATR_MA[j] <= 0:
            return None

        # B: Squeeze must have occurred recently
        if not SQZ[j]:
            return None

        bar_range = H[j] - L[j]
        if bar_range <= 0:
            return None
        body = abs(O[j] - C[j])

        # --- LONG candidate ---
        long_ok = (
            C[j] > TEMA[j]
            and FEMA[j] > TEMA[j]
            and C[j] > CH[j]
            and CPOS[j] >= (1 - self.candle_strength)
        )
        # --- SHORT candidate ---
        short_ok = (
            C[j] < TEMA[j]
            and FEMA[j] < TEMA[j]
            and C[j] < CL[j]
            and CPOS[j] <= self.candle_strength
        )

        if not long_ok and not short_ok:
            return None

        # =============================================
        # V4 PA FILTERS (applied to the breakout bar)
        # =============================================

        # E: EMA deviation filter
        ema_dev = abs(C[j] - TEMA[j]) / TEMA[j]
        if ema_dev > self.max_ema_deviation_pct:
            return None

        # F: Climax candle body filter
        if body > self.climax_atr_mult * ATR_MA[j]:
            return None

        # G: Adverse wick filter
        if long_ok:
            adverse_wick = min(O[j], C[j]) - L[j]
            if adverse_wick / bar_range > self.max_wick_ratio:
                return None
            return 'BUY'
        else:
            adverse_wick = H[j] - max(O[j], C[j])
            if adverse_wick / bar_range > self.max_wick_ratio:
                return None
            return 'SELL'

    # ------------------------------------------------------------------
    # Phase 2: Pullback confirmation (TBTL state machine)
    # ------------------------------------------------------------------

    def check_pullback_entry(self, df: pd.DataFrame, j: int,
                             pending_dir: str, breakout_bar: int,
                             origin_low: float, origin_high: float,
                             touched: bool) -> tuple[str | None, bool, bool]:
        """
        Check pullback conditions on completed bar j.

        Returns: (action, pullback_touched, should_cancel)
          action: 'BUY'/'SELL' if entry confirmed, else None
          pullback_touched: updated touch state
          should_cancel: True if the pending breakout should be abandoned
        """
        C  = df['close'].values
        H  = df['high'].values
        L  = df['low'].values
        O  = df['open'].values
        PB_EMA = df['pullback_ema'].values

        bars_waiting = j - breakout_bar
        if bars_waiting > self.pullback_bars:
            return None, touched, True  # timeout

        is_long = (pending_dir == 'BUY')

        # Invalidation: price breaks below origin for long / above origin for short
        if is_long and L[j] < origin_low:
            return None, touched, True
        if not is_long and H[j] > origin_high:
            return None, touched, True

        # Step 1: Check if pullback touches the EMA
        if not touched:
            if is_long and L[j] <= PB_EMA[j]:
                touched = True
            elif not is_long and H[j] >= PB_EMA[j]:
                touched = True
            return None, touched, False

        # Step 2: After touch, look for continuation candle
        if is_long:
            if C[j] > O[j] and j >= 1 and C[j] > H[j - 1]:
                return 'BUY', touched, False
        else:
            if C[j] < O[j] and j >= 1 and C[j] < L[j - 1]:
                return 'SELL', touched, False

        return None, touched, False

    # ------------------------------------------------------------------
    # Live trading interface (generate_signal) - stateful
    # ------------------------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {
            "action": "HOLD", "entry": 0.0, "sl": 0.0,
            "tp1": 0.0, "tp2": 0.0, "risk_r": 0.0,
            "reason": "wait", "meta": {},
        }
        need = self.warmup_bars + self.pullback_bars + 10
        if df is None or len(df) < need:
            return sig

        df = self.calc_all_indicators(df.iloc[-need:].copy())
        j = len(df) - 2  # last completed bar
        if j < 1:
            return sig

        self._live_bar_counter += 1

        # --- If we have a pending breakout, check pullback ---
        if self._pending_dir is not None:
            bars_since = self._live_bar_counter - self._pending_bar
            if bars_since > self.pullback_bars:
                self._pending_dir = None
            else:
                action, self._pullback_touched, cancel = self.check_pullback_entry(
                    df, j, self._pending_dir, j - bars_since,
                    self._pending_origin_low, self._pending_origin_high,
                    self._pullback_touched,
                )
                if cancel:
                    self._pending_dir = None
                elif action is not None:
                    atr = df['atr'].values[j]
                    sl_dist = atr * self.atr_sl_mult
                    entry = float(df['open'].iloc[-1])

                    if action == 'BUY':
                        sig.update({
                            "action": "BUY", "entry": entry,
                            "sl": entry - sl_dist,
                            "tp1": entry + sl_dist * 3.5,
                            "risk_r": sl_dist,
                            "reason": f"LONG pullback confirmed | C={C_val(df,j):.0f}",
                        })
                    else:
                        sig.update({
                            "action": "SELL", "entry": entry,
                            "sl": entry + sl_dist,
                            "tp1": entry - sl_dist * 3.5,
                            "risk_r": sl_dist,
                            "reason": f"SHORT pullback confirmed | C={C_val(df,j):.0f}",
                        })
                    self._pending_dir = None
                    return sig
            if self._pending_dir is not None:
                sig["reason"] = f"pullback wait ({self._pending_dir})"
                return sig

        # --- No pending: scan for new breakout ---
        breakout = self.check_breakout(df, j)
        if breakout is not None:
            self._pending_dir = breakout
            self._pending_bar = self._live_bar_counter
            self._pullback_touched = False
            self._pending_origin_low = float(df['low'].values[j])
            self._pending_origin_high = float(df['high'].values[j])
            sig["reason"] = f"breakout detected ({breakout}), awaiting pullback"
            sig["meta"] = {"pending": breakout}

        return sig


def C_val(df: pd.DataFrame, j: int) -> float:
    return float(df['close'].values[j])


# =========================================================================
# V4 Backtest with Chandelier Exit
# =========================================================================

def run_v4_backtest(
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
    """
    Full V4 backtest with:
      - PA-filtered squeeze breakout detection
      - TBTL pullback entry confirmation
      - Chandelier trailing stop (replaces scale-out + EMA trail)
      - Time stop
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from data.market_data import fetch_history_range

    strat = TrendMomentumV4Strategy(**strategy_params)
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) < strat.warmup_bars + 50:
        return {"status": "error", "error": "insufficient data"}

    df = strat.calc_all_indicators(df)

    balance = initial_capital
    peak_balance = balance
    max_dd = 0.0
    trades: list[dict] = []
    cooldown_until = 0

    # --- Position state ---
    pos_side: str | None = None   # 'long' / 'short' / None
    pos_entry   = 0.0
    pos_size    = 0.0
    pos_sl      = 0.0
    pos_entry_bar = 0
    initial_risk  = 0.0           # |entry - initial_sl|, for trail activation
    trail_active  = False
    trail_sl      = 0.0
    extreme_price = 0.0           # best price seen during hold (high for long, low for short)

    # --- Pullback state (backtest-local, independent of strategy instance) ---
    pending_dir: str | None = None
    pending_breakout_bar = 0
    pending_origin_low   = 0.0
    pending_origin_high  = 0.0
    pb_touched = False

    C  = df['close'].values
    O  = df['open'].values
    H  = df['high'].values
    L  = df['low'].values
    ATR = df['atr'].values

    for i in range(strat.warmup_bars, len(df)):
        price = C[i]

        # --- Drawdown tracking ---
        if balance > peak_balance:
            peak_balance = balance
        dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # =============================================================
        # IN POSITION: check exits
        # =============================================================
        if pos_side is not None:
            bars_held = i - pos_entry_bar
            is_long = (pos_side == 'long')

            # Update extreme price for Chandelier calculation
            if is_long:
                if H[i] > extreme_price:
                    extreme_price = H[i]
            else:
                if L[i] < extreme_price:
                    extreme_price = L[i]

            # --- Chandelier trailing activation & update ---
            if not trail_active:
                unrealized = (extreme_price - pos_entry) if is_long else (pos_entry - extreme_price)
                if unrealized > 1.5 * initial_risk:
                    trail_active = True
                    current_atr = ATR[i] if not np.isnan(ATR[i]) else initial_risk / strat.atr_sl_mult
                    if is_long:
                        trail_sl = extreme_price - current_atr * strat.trail_atr_mult
                    else:
                        trail_sl = extreme_price + current_atr * strat.trail_atr_mult
                    # Only upgrade from initial SL
                    if is_long:
                        trail_sl = max(trail_sl, pos_sl)
                    else:
                        trail_sl = min(trail_sl, pos_sl)
                    pos_sl = trail_sl

            if trail_active:
                current_atr = ATR[i] if not np.isnan(ATR[i]) else initial_risk / strat.atr_sl_mult
                if is_long:
                    new_trail = extreme_price - current_atr * strat.trail_atr_mult
                    if new_trail > pos_sl:   # ratchet: only moves up
                        pos_sl = new_trail
                else:
                    new_trail = extreme_price + current_atr * strat.trail_atr_mult
                    if new_trail < pos_sl:   # ratchet: only moves down
                        pos_sl = new_trail

            # --- Check SL hit ---
            sl_hit = (L[i] <= pos_sl) if is_long else (H[i] >= pos_sl)
            if sl_hit:
                exit_price = pos_sl
                pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                gross_pnl = pnl_per_unit * pos_size * leverage
                fee = abs(pos_size * exit_price * leverage) * fee_rate
                net_pnl = gross_pnl - fee
                balance += net_pnl

                reason = "trailing SL" if trail_active else "initial SL"
                trades.append({
                    "entry_ts": str(df.index[pos_entry_bar]),
                    "exit_ts":  str(df.index[i]),
                    "side": pos_side,
                    "entry": round(pos_entry, 2),
                    "exit":  round(exit_price, 2),
                    "pnl":   round(net_pnl, 2),
                    "exit_reason": reason,
                    "bars": bars_held,
                })
                pos_side = None
                cooldown_until = i + strat.cooldown
                pending_dir = None
                continue

            # --- Time stop ---
            if bars_held >= strat.time_stop_bars:
                exit_price = C[i]
                pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                gross_pnl = pnl_per_unit * pos_size * leverage
                fee = abs(pos_size * exit_price * leverage) * fee_rate
                net_pnl = gross_pnl - fee
                balance += net_pnl
                trades.append({
                    "entry_ts": str(df.index[pos_entry_bar]),
                    "exit_ts":  str(df.index[i]),
                    "side": pos_side,
                    "entry": round(pos_entry, 2),
                    "exit":  round(exit_price, 2),
                    "pnl":   round(net_pnl, 2),
                    "exit_reason": "time stop",
                    "bars": bars_held,
                })
                pos_side = None
                cooldown_until = i + strat.cooldown
                pending_dir = None
                continue

        # =============================================================
        # NO POSITION: entry pipeline
        # =============================================================
        if pos_side is not None:
            continue

        if i <= cooldown_until:
            continue

        j = i - 1  # signal bar = last completed bar

        # --- If pending breakout, check pullback ---
        if pending_dir is not None:
            action, pb_touched, cancel = strat.check_pullback_entry(
                df, j, pending_dir, pending_breakout_bar,
                pending_origin_low, pending_origin_high, pb_touched,
            )
            if cancel:
                pending_dir = None
            elif action is not None:
                # ENTRY CONFIRMED
                entry_price = O[i] * (1 + slippage if action == 'BUY' else 1 - slippage)
                atr_val = ATR[j]
                if np.isnan(atr_val) or atr_val <= 0:
                    pending_dir = None
                    continue

                sl_dist = atr_val * strat.atr_sl_mult
                risk_amount = balance * risk_pct
                pos_size = risk_amount / (sl_dist * leverage)

                open_fee = pos_size * entry_price * leverage * fee_rate
                balance -= open_fee

                pos_side = 'long' if action == 'BUY' else 'short'
                pos_entry = entry_price
                pos_sl = (entry_price - sl_dist) if action == 'BUY' else (entry_price + sl_dist)
                pos_entry_bar = i
                initial_risk = sl_dist
                trail_active = False
                trail_sl = 0.0
                extreme_price = H[i] if action == 'BUY' else L[i]

                pending_dir = None
            continue  # whether we entered or are still waiting, move to next bar

        # --- No pending: scan for new breakout ---
        breakout = strat.check_breakout(df, j)
        if breakout is not None:
            pending_dir = breakout
            pending_breakout_bar = j
            pending_origin_low  = float(L[j])
            pending_origin_high = float(H[j])
            pb_touched = False

    # --- Close open position at end ---
    if pos_side is not None:
        exit_price = C[-1]
        is_long = (pos_side == 'long')
        pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
        gross_pnl = pnl_per_unit * pos_size * leverage
        fee = abs(pos_size * exit_price * leverage) * fee_rate
        net_pnl = gross_pnl - fee
        balance += net_pnl
        trades.append({
            "entry_ts": str(df.index[pos_entry_bar]),
            "exit_ts":  str(df.index[-1]),
            "side": pos_side,
            "entry": round(pos_entry, 2),
            "exit":  round(exit_price, 2),
            "pnl":   round(net_pnl, 2),
            "exit_reason": "end of data",
            "bars": len(df) - 1 - pos_entry_bar,
        })

    # --- Metrics ---
    total = len(trades)
    winners = [t for t in trades if t['pnl'] > 0]
    losers  = [t for t in trades if t['pnl'] <= 0]
    win_count = len(winners)
    avg_win  = np.mean([t['pnl'] for t in winners]) if winners else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losers]) if losers else 0
    pf = avg_win / avg_loss if avg_loss > 0 else 0
    win_rate = win_count / total * 100 if total > 0 else 0

    roi = (balance - initial_capital) / initial_capital * 100

    if balance > peak_balance:
        peak_balance = balance
    dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
    if dd > max_dd:
        max_dd = dd

    # Sharpe from daily PnL
    daily_pnls: dict[str, float] = {}
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


def _count_exits(trades: list[dict]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for t in trades:
        r = t['exit_reason']
        reasons[r] = reasons.get(r, 0) + 1
    return reasons
