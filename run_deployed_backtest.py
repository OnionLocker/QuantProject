# -*- coding: utf-8 -*-
"""
Backtest the exact deployed strategy:
  - TrendMomentum V2 entry logic
  - EMA(100) trailing exit (activates after breakeven)
  - Breakeven SL at +1R (no scale-out)
  - 120-bar time stop
  - 5x leverage
  - 6 years: 2020-03-25 to 2026-03-25
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
from data.market_data import fetch_history_range
from strategy.trend_momentum import TrendMomentumStrategy


def run_deployed_backtest(
    symbol='BTC/USDT',
    timeframe='1h',
    start_date='2020-03-25',
    end_date='2026-03-25',
    initial_capital=5000.0,
    leverage=5,
    risk_pct=0.01,
    fee_rate=0.0006,
    slippage=0.0003,
    exit_ema_period=100,
    time_stop_bars=120,
):
    strat = TrendMomentumStrategy(
        channel_period=24, fast_ema=20, trend_ema=120,
        atr_sl_mult=3.0, rr_min=3.5, cooldown=16,
        max_atr_mult=1.3, breakout_margin=0.0, exit_ema=exit_ema_period,
    )

    print(f"Fetching {symbol} {timeframe} data: {start_date} -> {end_date} ...")
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) < strat.warmup_bars + 50:
        return {"status": "error", "error": "insufficient data"}
    print(f"Data loaded: {len(df)} bars, from {df.index[0]} to {df.index[-1]}")

    df = strat._calc_indicators(df)

    # Precompute EMA for exit
    df['exit_ema'] = df['close'].ewm(span=exit_ema_period, adjust=False).mean()

    C = df['close'].values
    O = df['open'].values
    H = df['high'].values
    L = df['low'].values
    ATR = df['atr'].values
    CH = df['channel_high'].values
    CL = df['channel_low'].values
    FEMA = df['fast_ema'].values
    TEMA = df['trend_ema'].values
    VOL = df['volume'].values
    VOL_MA = df['vol_ma'].values
    ATR_MA = df['atr_ma'].values
    EXIT_EMA = df['exit_ema'].values

    balance = initial_capital
    peak_balance = balance
    max_dd = 0.0
    trades = []
    cooldown_until = 0
    equity_curve = []

    pos_side = None
    pos_entry = 0.0
    pos_size = 0.0
    pos_sl = 0.0
    original_sl = 0.0
    pos_entry_bar = 0
    breakeven_activated = False

    for i in range(strat.warmup_bars, len(df)):
        price = C[i]

        if balance > peak_balance:
            peak_balance = balance
        dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
        if dd > max_dd:
            max_dd = dd

        equity_curve.append({"ts": str(df.index[i]), "balance": round(balance, 2)})

        if pos_side is not None:
            bars_held = i - pos_entry_bar
            is_long = (pos_side == 'long')

            # 1) SL hit check (intrabar with H/L)
            sl_hit = (L[i] <= pos_sl) if is_long else (H[i] >= pos_sl)
            if sl_hit:
                exit_price = pos_sl
                pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                gross_pnl = pnl_per_unit * pos_size * leverage
                fee = abs(pos_size * exit_price * leverage) * fee_rate
                net_pnl = gross_pnl - fee
                balance += net_pnl
                sl_type = "SL (breakeven)" if breakeven_activated else "SL"
                trades.append({
                    "entry_ts": str(df.index[pos_entry_bar]),
                    "exit_ts": str(df.index[i]),
                    "side": pos_side,
                    "entry": round(pos_entry, 2),
                    "exit": round(exit_price, 2),
                    "pnl": round(net_pnl, 2),
                    "pnl_pct": round(net_pnl / initial_capital * 100, 2),
                    "exit_reason": sl_type,
                    "bars": bars_held,
                })
                pos_side = None
                cooldown_until = i + strat.cooldown
                continue

            # 2) Breakeven check: move SL to entry when +1R reached
            if not breakeven_activated:
                sl_dist = abs(pos_entry - original_sl)
                be_trigger = (pos_entry + sl_dist) if is_long else (pos_entry - sl_dist)
                reached = (H[i] >= be_trigger) if is_long else (L[i] <= be_trigger)
                if reached:
                    pos_sl = pos_entry
                    breakeven_activated = True

            # 3) EMA trailing exit (only after breakeven activated, use completed bar)
            if breakeven_activated:
                prev_close = C[i - 1]
                prev_ema = EXIT_EMA[i - 1]
                ema_exit = False
                if is_long and prev_close < prev_ema:
                    ema_exit = True
                elif not is_long and prev_close > prev_ema:
                    ema_exit = True

                if ema_exit:
                    exit_price = O[i]  # exit at open of next bar
                    pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                    gross_pnl = pnl_per_unit * pos_size * leverage
                    fee = abs(pos_size * exit_price * leverage) * fee_rate
                    net_pnl = gross_pnl - fee
                    balance += net_pnl
                    trades.append({
                        "entry_ts": str(df.index[pos_entry_bar]),
                        "exit_ts": str(df.index[i]),
                        "side": pos_side,
                        "entry": round(pos_entry, 2),
                        "exit": round(exit_price, 2),
                        "pnl": round(net_pnl, 2),
                        "pnl_pct": round(net_pnl / initial_capital * 100, 2),
                        "exit_reason": f"EMA({exit_ema_period}) trail",
                        "bars": bars_held,
                    })
                    pos_side = None
                    cooldown_until = i + strat.cooldown
                    continue

            # 4) Time stop
            if bars_held >= time_stop_bars:
                exit_price = C[i]
                pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
                gross_pnl = pnl_per_unit * pos_size * leverage
                fee = abs(pos_size * exit_price * leverage) * fee_rate
                net_pnl = gross_pnl - fee
                balance += net_pnl
                trades.append({
                    "entry_ts": str(df.index[pos_entry_bar]),
                    "exit_ts": str(df.index[i]),
                    "side": pos_side,
                    "entry": round(pos_entry, 2),
                    "exit": round(exit_price, 2),
                    "pnl": round(net_pnl, 2),
                    "pnl_pct": round(net_pnl / initial_capital * 100, 2),
                    "exit_reason": "time stop",
                    "bars": bars_held,
                })
                pos_side = None
                cooldown_until = i + strat.cooldown
                continue

        # --- Entry ---
        if pos_side is None and i > cooldown_until:
            j = i - 1
            action = strat._check_entry(
                j, C, ATR, CH, CL, FEMA, TEMA, VOL, VOL_MA, ATR_MA
            )
            if action is not None and not np.isnan(ATR[j]):
                entry_price = O[i] * (1 + slippage if action == 'BUY' else 1 - slippage)
                sl_dist = ATR[j] * strat.atr_sl_mult

                risk_amount = balance * risk_pct
                pos_size = risk_amount / (sl_dist * leverage)

                open_fee = pos_size * entry_price * leverage * fee_rate
                balance -= open_fee

                pos_side = 'long' if action == 'BUY' else 'short'
                pos_entry = entry_price
                original_sl = (entry_price - sl_dist) if action == 'BUY' else (entry_price + sl_dist)
                pos_sl = original_sl
                pos_entry_bar = i
                breakeven_activated = False

    # Close open position at end
    if pos_side is not None:
        exit_price = C[-1]
        is_long = pos_side == 'long'
        pnl_per_unit = (exit_price - pos_entry) if is_long else (pos_entry - exit_price)
        gross_pnl = pnl_per_unit * pos_size * leverage
        fee = abs(pos_size * exit_price * leverage) * fee_rate
        net_pnl = gross_pnl - fee
        balance += net_pnl
        trades.append({
            "entry_ts": str(df.index[pos_entry_bar]),
            "exit_ts": str(df.index[-1]),
            "side": pos_side,
            "entry": round(pos_entry, 2),
            "exit": round(exit_price, 2),
            "pnl": round(net_pnl, 2),
            "pnl_pct": round(net_pnl / initial_capital * 100, 2),
            "exit_reason": "end of data",
            "bars": len(df) - 1 - pos_entry_bar,
        })

    # Final DD
    if balance > peak_balance:
        peak_balance = balance
    dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
    if dd > max_dd:
        max_dd = dd

    # --- Metrics ---
    total = len(trades)
    winners = [t for t in trades if t['pnl'] > 0]
    losers = [t for t in trades if t['pnl'] <= 0]
    win_count = len(winners)
    avg_win = np.mean([t['pnl'] for t in winners]) if winners else 0
    avg_loss = np.mean([abs(t['pnl']) for t in losers]) if losers else 0
    pf = (sum(t['pnl'] for t in winners) / sum(abs(t['pnl']) for t in losers)) if losers and sum(abs(t['pnl']) for t in losers) > 0 else 0
    win_rate = win_count / total * 100 if total > 0 else 0
    roi = (balance - initial_capital) / initial_capital * 100

    # Sharpe
    daily_pnls = {}
    for t in trades:
        day = t['exit_ts'][:10]
        daily_pnls[day] = daily_pnls.get(day, 0) + t['pnl']
    if daily_pnls:
        rets = np.array(list(daily_pnls.values()))
        sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    else:
        sharpe = 0

    # Exit reasons
    exit_reasons = {}
    for t in trades:
        r = t['exit_reason']
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Max consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for t in trades:
        if t['pnl'] <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # Avg bars held
    avg_bars = np.mean([t['bars'] for t in trades]) if trades else 0

    # Yearly breakdown
    yearly = {}
    for t in trades:
        year = t['exit_ts'][:4]
        if year not in yearly:
            yearly[year] = {"pnl": 0, "trades": 0, "wins": 0}
        yearly[year]["pnl"] += t['pnl']
        yearly[year]["trades"] += 1
        if t['pnl'] > 0:
            yearly[year]["wins"] += 1

    return {
        "status": "done",
        "roi_pct": round(roi, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_trades": total,
        "win_count": win_count,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(pf, 3),
        "sharpe_ratio": round(sharpe, 3),
        "final_balance": round(balance, 2),
        "initial_capital": initial_capital,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_consecutive_losses": max_consec_loss,
        "avg_bars_held": round(avg_bars, 1),
        "exit_reasons": exit_reasons,
        "yearly": yearly,
        "trades": trades,
        "equity_curve": equity_curve,
    }


if __name__ == "__main__":
    result = run_deployed_backtest()

    if result["status"] != "done":
        print(f"Error: {result.get('error')}")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("  TrendMomentum V2 + EMA(100) Trail + Breakeven@1R  |  6-Year Backtest")
    print("=" * 70)
    print(f"  Period:           2020-03-25 ~ 2026-03-25 (6 years)")
    print(f"  Symbol:           BTC/USDT 1h")
    print(f"  Leverage:         5x")
    print(f"  Risk per trade:   1%")
    print(f"  Initial Capital:  ${result['initial_capital']:,.0f}")
    print("-" * 70)
    print(f"  Final Balance:    ${result['final_balance']:,.2f}")
    print(f"  ROI:              {result['roi_pct']:+.2f}%")
    print(f"  Max Drawdown:     {result['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe Ratio:     {result['sharpe_ratio']:.3f}")
    print(f"  Profit Factor:    {result['profit_factor']:.3f}")
    print("-" * 70)
    print(f"  Total Trades:     {result['total_trades']}")
    print(f"  Win Rate:         {result['win_rate_pct']:.1f}%  ({result['win_count']}/{result['total_trades']})")
    print(f"  Avg Win:          ${result['avg_win']:.2f}")
    print(f"  Avg Loss:         ${result['avg_loss']:.2f}")
    print(f"  Max Consec Loss:  {result['max_consecutive_losses']}")
    print(f"  Avg Bars Held:    {result['avg_bars_held']:.1f} bars")
    print("-" * 70)
    print("  Exit Reasons:")
    for reason, count in sorted(result['exit_reasons'].items(), key=lambda x: -x[1]):
        pct = count / result['total_trades'] * 100
        print(f"    {reason:30s} {count:4d}  ({pct:.1f}%)")
    print("-" * 70)
    print("  Yearly Breakdown:")
    for year in sorted(result['yearly'].keys()):
        y = result['yearly'][year]
        wr = y['wins'] / y['trades'] * 100 if y['trades'] > 0 else 0
        print(f"    {year}:  PnL ${y['pnl']:+8.2f}  |  {y['trades']:3d} trades  |  WR {wr:.0f}%")
    print("=" * 70)
