import sys
import os
sys.path.insert(0, '.')
os.chdir('/root/QuantProject')
from strategy.trend_momentum_v3 import run_v3_backtest

kw = dict(symbol='BTC/USDT', timeframe='1h', initial_capital=5000.0,
    squeeze_lookback=99999, candle_strength=1.0,
    atr_sl_mult=3.0, exit_ema=100, time_stop_bars=120,
    scale_out_r=1.0, scale_out_pct=0.0)

r = run_v3_backtest(start_date='2021-06-01', end_date='2023-01-01', leverage=5, **kw)
print('2021H2-2022:', r.get('status'), r.get('roi_pct'), r.get('total_trades'), r.get('error'))

for lev in [3, 5, 7, 10]:
    r = run_v3_backtest(start_date='2020-01-01', end_date='2026-03-25', leverage=lev, **kw)
    s = r.get('status')
    if s == 'done':
        print('%dx: ROI=%+.2f%% DD=%.1f%% Sharpe=%.3f Final=%.0fU' % (lev, r['roi_pct'], r['max_drawdown_pct'], r['sharpe_ratio'], r['final_balance']))
    else:
        print('%dx: %s' % (lev, str(r)))
