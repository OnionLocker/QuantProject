"""scripts/donchian_param_scan.py — 把结果直接写 csv"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from backtest.engine_v2 import run_backtest, BacktestConfig
from bot.strategy import DonchianStrategy


def scan(df, base_cfg, allow_short, entries, exits, atrs):
    rows = []
    for e in entries:
        for x in exits:
            for a in atrs:
                strat = DonchianStrategy(entry_period=e, exit_period=x, atr_sl_mult=a)
                cfg = BacktestConfig(**{**base_cfg, 'allow_short': allow_short})
                r = run_backtest(strat, df, cfg)
                m = r['metrics']
                rows.append({
                    'direction': 'both' if allow_short else 'long-only',
                    'entry': e, 'exit': x, 'atr': a,
                    'return_pct': round(m['total_return_pct'], 2),
                    'cagr_pct':   round(m['cagr_pct'], 2),
                    'max_dd_pct': round(m['max_drawdown_pct'], 2),
                    'calmar':     round(m['calmar'], 2),
                    'sharpe':     round(m['sharpe'], 2),
                    'payoff':     round(m['payoff_ratio'], 2),
                    'trades':     m['trades'],
                    'win_rate':   round(m['win_rate_pct'], 1),
                    'liquidated': r['liquidated'],
                })
    return rows


def main():
    df = pd.read_parquet('data/cache/BTC-USDT-USDT_4h.parquet')
    base_cfg = dict(initial_capital=10000.0, leverage=3, risk_per_trade_pct=0.01,
                    taker_fee_rate=0.0005, slippage_base=0.0002, slippage_max=0.003,
                    funding_rate_8h=0.0)

    entries = [40, 55, 70]
    exits   = [10, 20]
    atrs    = [1.5, 2.0, 3.0]

    rows = []
    rows += scan(df, base_cfg, allow_short=True,  entries=entries, exits=exits, atrs=atrs)
    rows += scan(df, base_cfg, allow_short=False, entries=entries, exits=exits, atrs=atrs)

    out = pd.DataFrame(rows)
    out_path = 'reports/donchian_scan.csv'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"saved: {out_path} ({len(out)} rows)")

    # 按 direction 分别打印 top-5 by calmar
    for d in ['long-only', 'both']:
        sub = out[out.direction == d].sort_values('calmar', ascending=False).head(10)
        print(f"\n=== TOP 10 by Calmar ({d}) ===")
        print(sub.to_string(index=False))


if __name__ == '__main__':
    main()
