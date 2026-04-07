# -*- coding: utf-8 -*-
"""
strategy/trend_pullback.py - TrendPullback V5

V4 baseline: PF 1.13, ROI +85%, MaxDD 33%, WR 16%, 14 params

V5 upgrades:
  P0: Reduced from 14 to 9 tunable params (hardcode stable ones)
  P1: Multi-timeframe 闁炽儻鎷� 4H EMA(50) direction filter
  P2: Volume confirmation 闁炽儻鎷� reversal candle vol > 20-period MA
  P4: ADX(14) > threshold 闁炽儻鎷� only trade trending markets
  P5: Exit EMA(30) replaces EMA(20) 闁炽儻鎷� "fast entry, slow exit"

Exit: full-position trend exit (NO split 闁炽儻鎷� split killed avg_win in testing)
"""
import numpy as np
import pandas as pd
from strategy.base import BaseStrategy

# 闁冲厜鍋撻柍鍏夊亾 Hardcoded constants (stable, not worth tuning) 闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋�
_ATR_PERIOD        = 14
_PULLBACK_LOOKBACK = 5
_MAX_TREND_DIST    = 6.0
_TREND_SLOPE_BARS  = 10
_MIN_BODY_PCT      = 0.55
_MIN_BODY_ATR      = 0.5
_MTF_EMA_PERIOD    = 50
_VOL_MA_PERIOD     = 20
_GRACE_BARS        = 12


def _calc_adx(H, L, C, period=14):
    """Wilder's ADX."""
    plus_dm = H.diff().clip(lower=0)
    minus_dm = (-L.diff()).clip(lower=0)

    both = (plus_dm > 0) & (minus_dm > 0)
    plus_dm_a = plus_dm.copy()
    minus_dm_a = minus_dm.copy()
    plus_dm_a[both & (plus_dm <= minus_dm)] = 0
    minus_dm_a[both & (minus_dm <= plus_dm)] = 0

    prev_c = C.shift(1)
    tr = pd.concat([H - L, (H - prev_c).abs(), (L - prev_c).abs()], axis=1).max(axis=1)

    alpha = 1.0 / period
    atr_s    = tr.ewm(alpha=alpha, adjust=False).mean()
    sm_plus  = plus_dm_a.ewm(alpha=alpha, adjust=False).mean()
    sm_minus = minus_dm_a.ewm(alpha=alpha, adjust=False).mean()

    plus_di  = 100 * sm_plus / atr_s
    minus_di = 100 * sm_minus / atr_s
    denom = plus_di + minus_di
    dx = (100 * (plus_di - minus_di).abs() / denom).replace([np.inf, -np.inf], np.nan)
    return dx.ewm(alpha=alpha, adjust=False).mean()


class TrendPullbackStrategy(BaseStrategy):

    PARAMS = [
        {"key": "trend_ema", "label": "Trend EMA",
         "type": "int", "default": 120, "min": 50, "max": 200, "step": 10},
        {"key": "value_ema", "label": "Value EMA (entry)",
         "type": "int", "default": 20, "min": 10, "max": 50, "step": 5},
        {"key": "exit_ema", "label": "Exit EMA (slower)",
         "type": "int", "default": 30, "min": 20, "max": 60, "step": 5,
         "tip": "Trend exit uses this slower EMA"},
        {"key": "atr_sl_mult", "label": "SL ATR multiplier",
         "type": "float", "default": 2.0, "min": 1.0, "max": 4.0, "step": 0.5},
        {"key": "be_activation_r", "label": "Breakeven activation (R)",
         "type": "float", "default": 0.7, "min": 0.3, "max": 2.0, "step": 0.1},
        {"key": "exit_confirm_bars", "label": "Trend exit confirm bars",
         "type": "int", "default": 4, "min": 2, "max": 8, "step": 1},
        {"key": "cooldown", "label": "Signal cooldown",
         "type": "int", "default": 12, "min": 4, "max": 30, "step": 2},
        {"key": "adx_min", "label": "ADX minimum",
         "type": "float", "default": 20.0, "min": 10.0, "max": 35.0, "step": 5.0,
         "tip": "Only trade when ADX > this"},
        {"key": "vol_mult", "label": "Volume multiplier",
         "type": "float", "default": 1.2, "min": 0.5, "max": 2.0, "step": 0.1,
         "tip": "Reversal bar vol must exceed vol_MA * this"},
    ]

    def __init__(
        self,
        trend_ema:         int   = 120,
        value_ema:         int   = 20,
        exit_ema:          int   = 30,
        atr_sl_mult:       float = 2.0,
        be_activation_r:   float = 0.7,
        exit_confirm_bars: int   = 4,
        cooldown:          int   = 12,
        adx_min:           float = 20.0,
        vol_mult:          float = 1.2,
    ):
        super().__init__(name="TrendPullback")
        self.trend_ema         = trend_ema
        self.value_ema         = value_ema
        self.exit_ema          = exit_ema
        self.atr_sl_mult       = atr_sl_mult
        self.be_activation_r   = be_activation_r
        self.exit_confirm_bars = exit_confirm_bars
        self.cooldown          = cooldown
        self.adx_min           = adx_min
        self.vol_mult          = vol_mult
        self.warmup_bars       = trend_ema + 50

    # ------------------------------------------------------------------
    def calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        C, H, L, V = df['close'], df['high'], df['low'], df['volume']
        prev_c = C.shift(1)
        tr = pd.concat([
            H - L, (H - prev_c).abs(), (L - prev_c).abs(),
        ], axis=1).max(axis=1)

        df['atr']       = tr.rolling(_ATR_PERIOD).mean()
        df['trend_ema'] = C.ewm(span=self.trend_ema, adjust=False).mean()
        df['value_ema'] = C.ewm(span=self.value_ema, adjust=False).mean()
        df['exit_ema']  = C.ewm(span=self.exit_ema, adjust=False).mean()
        df['adx']       = _calc_adx(H, L, C, _ATR_PERIOD)
        df['vol_ma']    = V.rolling(_VOL_MA_PERIOD).mean()

        # MTF: 4H EMA 闁炽儻鎷� shift(1) to prevent look-ahead
        df_4h = df.resample('4h').agg({
            'open': 'first', 'high': 'max', 'low': 'min',
            'close': 'last', 'volume': 'sum',
        }).dropna(subset=['close'])
        df_4h['ema_4h'] = df_4h['close'].ewm(span=_MTF_EMA_PERIOD, adjust=False).mean()
        df_4h['ema_4h'] = df_4h['ema_4h'].shift(1)
        df['ema_4h'] = df_4h['ema_4h'].reindex(df.index, method='ffill')

        return df

    # ------------------------------------------------------------------
    def check_entry(self, df: pd.DataFrame, j: int) -> str | None:
        C    = df['close'].values
        O    = df['open'].values
        H    = df['high'].values
        L    = df['low'].values
        TEMA = df['trend_ema'].values
        VEMA = df['value_ema'].values
        ATR  = df['atr'].values
        ADX  = df['adx'].values
        VOL  = df['volume'].values
        VOLMA = df['vol_ma'].values
        EMA4H = df['ema_4h'].values

        if j < _TREND_SLOPE_BARS or np.isnan(TEMA[j]) or np.isnan(ATR[j]):
            return None
        if np.isnan(ADX[j]) or ADX[j] < self.adx_min:
            return None

        atr  = ATR[j]
        body = abs(C[j] - O[j])
        rng  = H[j] - L[j]
        sb   = _TREND_SLOPE_BARS

        # --- LONG ---
        if (VEMA[j] > TEMA[j]
                and C[j] > TEMA[j]
                and TEMA[j] > TEMA[j - sb]):

            if not np.isnan(EMA4H[j]) and C[j] < EMA4H[j]:
                return None

            if abs(C[j] - TEMA[j]) > atr * _MAX_TREND_DIST:
                return None

            had_pullback = False
            start = max(0, j - _PULLBACK_LOOKBACK)
            for k in range(start, j):
                if C[k] < VEMA[k]:
                    had_pullback = True
                    break
            if not had_pullback:
                return None

            if not np.isnan(VOLMA[j]) and VOLMA[j] > 0:
                if VOL[j] < VOLMA[j] * self.vol_mult:
                    return None

            if (C[j] > VEMA[j] and C[j] > O[j]
                    and rng > 0 and body / rng >= _MIN_BODY_PCT
                    and body >= atr * _MIN_BODY_ATR):
                return 'BUY'

        # --- SHORT ---
        if (VEMA[j] < TEMA[j]
                and C[j] < TEMA[j]
                and TEMA[j] < TEMA[j - sb]):

            if not np.isnan(EMA4H[j]) and C[j] > EMA4H[j]:
                return None

            if abs(C[j] - TEMA[j]) > atr * _MAX_TREND_DIST:
                return None

            had_pullback = False
            start = max(0, j - _PULLBACK_LOOKBACK)
            for k in range(start, j):
                if C[k] > VEMA[k]:
                    had_pullback = True
                    break
            if not had_pullback:
                return None

            if not np.isnan(VOLMA[j]) and VOLMA[j] > 0:
                if VOL[j] < VOLMA[j] * self.vol_mult:
                    return None

            if (C[j] < VEMA[j] and C[j] < O[j]
                    and rng > 0 and body / rng >= _MIN_BODY_PCT
                    and body >= atr * _MIN_BODY_ATR):
                return 'SELL'

        return None

    # ------------------------------------------------------------------
    def generate_signal(self, df: pd.DataFrame) -> dict:
        sig = {
            "action": "HOLD", "entry": 0.0, "sl": 0.0,
            "tp1": 0.0, "tp2": 0.0, "risk_r": 0.0,
            "reason": "wait", "meta": {},
        }
        need = self.warmup_bars + 10
        if df is None or len(df) < need:
            return sig

        df = self.calc_indicators(df.iloc[-need:].copy())
        j = len(df) - 2
        if j < 1:
            return sig

        action = self.check_entry(df, j)
        if action is None:
            return sig

        atr = df['atr'].values[j]
        if np.isnan(atr) or atr <= 0:
            return sig

        sl_dist = atr * self.atr_sl_mult
        entry = float(df['open'].iloc[-1])

        if action == 'BUY':
            sig.update({
                "action": "BUY", "entry": entry,
                "sl": entry - sl_dist,
                "tp1": 0.0, "tp2": 0.0,
                "risk_r": sl_dist,
                "reason": "LONG pullback | ADX={:.0f}".format(df['adx'].values[j]),
            })
        else:
            sig.update({
                "action": "SELL", "entry": entry,
                "sl": entry + sl_dist,
                "tp1": 0.0, "tp2": 0.0,
                "risk_r": sl_dist,
                "reason": "SHORT pullback | ADX={:.0f}".format(df['adx'].values[j]),
            })
        return sig


# =========================================================================
# Backtest 闁炽儻鎷� full-position trend exit via EMA(30)
# =========================================================================

def run_pullback_backtest(
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
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from data.market_data import fetch_history_range

    strat = TrendPullbackStrategy(**strategy_params)
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) < strat.warmup_bars + 50:
        return {"status": "error", "error": "insufficient data"}

    df = strat.calc_indicators(df)

    balance = initial_capital
    peak_balance = balance
    max_dd = 0.0
    trades: list[dict] = []
    cooldown_until = 0

    pos_side: str | None = None
    pos_entry      = 0.0
    pos_size       = 0.0
    pos_sl         = 0.0
    pos_entry_bar  = 0
    sl_dist_at_entry = 0.0
    pos_open_fee   = 0.0
    at_breakeven   = False
    wrong_side_cnt = 0

    C    = df['close'].values
    O    = df['open'].values
    H    = df['high'].values
    L    = df['low'].values
    ATR  = df['atr'].values
    EEMA = df['exit_ema'].values

    def _close_pos(idx, exit_px, reason):
        nonlocal balance, pos_side, cooldown_until
        is_long = (pos_side == 'long')
        pnl_per = (exit_px - pos_entry) if is_long else (pos_entry - exit_px)
        gross = pnl_per * pos_size * leverage
        close_fee = abs(pos_size * exit_px * leverage) * fee_rate
        net = gross - close_fee - pos_open_fee
        balance += net
        trades.append({
            "entry_ts": str(df.index[pos_entry_bar]),
            "exit_ts":  str(df.index[idx]),
            "side": pos_side,
            "entry": round(pos_entry, 2),
            "exit":  round(exit_px, 2),
            "pnl":   round(net, 2),
            "exit_reason": reason,
            "bars": idx - pos_entry_bar,
        })
        pos_side = None
        cooldown_until = idx + strat.cooldown

    for i in range(strat.warmup_bars, len(df)):
        if balance > peak_balance:
            peak_balance = balance
        dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # =============================================================
        # IN POSITION
        # =============================================================
        if pos_side is not None:
            is_long = (pos_side == 'long')
            bars_held = i - pos_entry_bar

            # --- Hard SL (intrabar) ---
            sl_hit = (L[i] <= pos_sl) if is_long else (H[i] >= pos_sl)
            if sl_hit:
                reason = "breakeven SL" if at_breakeven else "initial SL"
                _close_pos(i, pos_sl, reason)
                continue

            # --- Breakeven move ---
            if not at_breakeven and sl_dist_at_entry > 0:
                if is_long:
                    unrealised_r = (H[i] - pos_entry) / sl_dist_at_entry
                else:
                    unrealised_r = (pos_entry - L[i]) / sl_dist_at_entry
                if unrealised_r >= strat.be_activation_r:
                    at_breakeven = True
                    pos_sl = pos_entry

            # --- Trend exit via exit_ema (after grace period) ---
            if bars_held >= _GRACE_BARS:
                if is_long:
                    on_wrong_side = (C[i] < EEMA[i])
                else:
                    on_wrong_side = (C[i] > EEMA[i])

                if on_wrong_side:
                    wrong_side_cnt += 1
                else:
                    wrong_side_cnt = 0

                if wrong_side_cnt >= strat.exit_confirm_bars:
                    _close_pos(i, C[i], "trend exit")
                    continue

            continue

        # =============================================================
        # NO POSITION
        # =============================================================
        if i <= cooldown_until:
            continue

        j = i - 1
        action = strat.check_entry(df, j)
        if action is None:
            continue

        atr_val = ATR[j]
        if np.isnan(atr_val) or atr_val <= 0:
            continue

        sl_dist = atr_val * strat.atr_sl_mult
        entry_price = O[i] * (1 + slippage if action == 'BUY' else 1 - slippage)

        risk_amount = balance * risk_pct
        pos_size = risk_amount / (sl_dist * leverage)

        pos_open_fee = pos_size * entry_price * leverage * fee_rate

        pos_side = 'long' if action == 'BUY' else 'short'
        pos_entry = entry_price
        sl_dist_at_entry = sl_dist
        if action == 'BUY':
            pos_sl = entry_price - sl_dist
        else:
            pos_sl = entry_price + sl_dist
        pos_entry_bar  = i
        at_breakeven   = False
        wrong_side_cnt = 0

    if pos_side is not None:
        _close_pos(len(df) - 1, C[-1], "end of data")

    # --- Metrics ---
    total = len(trades)
    winners = [t for t in trades if t['pnl'] > 0]
    losers  = [t for t in trades if t['pnl'] <= 0]
    win_count = len(winners)
    avg_win  = np.mean([t['pnl'] for t in winners]) if winners else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losers]) if losers else 0
    pf = (sum(t['pnl'] for t in winners) / sum(abs(t['pnl']) for t in losers)
          ) if losers and sum(abs(t['pnl']) for t in losers) > 0 else 0
    win_rate = win_count / total * 100 if total > 0 else 0
    roi = (balance - initial_capital) / initial_capital * 100

    if balance > peak_balance:
        peak_balance = balance
    dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
    if dd > max_dd:
        max_dd = dd

    daily_pnls: dict[str, float] = {}
    for t in trades:
        day = t['exit_ts'][:10]
        daily_pnls[day] = daily_pnls.get(day, 0) + t['pnl']
    if daily_pnls:
        rets = np.array(list(daily_pnls.values()))
        sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    else:
        sharpe = 0

    exit_reasons: dict[str, int] = {}
    for t in trades:
        r = t['exit_reason']
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

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
        "exit_reasons": exit_reasons,
    }
