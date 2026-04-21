"""
backtest/metrics.py — 业绩指标计算（纯函数）

所有指标基于：
  - equity_curve: pd.Series (index=timestamp, values=account_equity)
  - trades: list[Trade]
  - initial_capital
  - bar_seconds（用于年化换算：4h=14400, 1h=3600, 1d=86400）
"""
from __future__ import annotations
from typing import Any, Iterable
import numpy as np
import pandas as pd


SECONDS_PER_YEAR = 365.25 * 24 * 3600


def compute_all(
    equity_curve: pd.Series,
    trades: Iterable[Any],
    initial_capital: float,
    bar_seconds: int,
) -> dict:
    """一次性计算所有关键指标。返回 dict，纯数字，便于 JSON 序列化。"""
    trades = list(trades)
    if equity_curve is None or len(equity_curve) == 0:
        return _empty_metrics()

    ec = equity_curve.astype(float)
    final = float(ec.iloc[-1])
    total_return = (final / initial_capital) - 1.0

    # 时间跨度（年）
    span_sec = (ec.index[-1] - ec.index[0]).total_seconds()
    years = max(span_sec / SECONDS_PER_YEAR, 1e-9)

    # CAGR
    if final <= 0:
        cagr = -1.0
    else:
        cagr = (final / initial_capital) ** (1.0 / years) - 1.0

    # 回撤
    peak = ec.cummax()
    drawdown = (ec - peak) / peak
    max_dd = float(drawdown.min())  # 负数或 0
    max_dd_abs = abs(max_dd)

    # 最长新高间隔（bar 数）
    longest_no_new_high = _longest_stretch_without_new_high(ec)
    longest_no_new_high_days = longest_no_new_high * bar_seconds / 86400.0

    # 按 bar 的收益率
    ret = ec.pct_change().fillna(0.0)
    # 年化波动
    bars_per_year = SECONDS_PER_YEAR / bar_seconds
    vol_annual = float(ret.std() * math_sqrt(bars_per_year))
    # Sharpe（无风险利率视为 0）
    mean_bar = float(ret.mean())
    sharpe = float((mean_bar * bars_per_year) / (vol_annual + 1e-12)) if vol_annual > 0 else 0.0
    # Sortino：只用下行波动
    downside = ret[ret < 0]
    dvol_annual = float(downside.std() * math_sqrt(bars_per_year)) if len(downside) > 1 else 0.0
    sortino = float((mean_bar * bars_per_year) / (dvol_annual + 1e-12)) if dvol_annual > 0 else 0.0
    # Calmar
    calmar = float(cagr / max_dd_abs) if max_dd_abs > 0 else 0.0

    # 交易层指标
    n = len(trades)
    wins   = [t for t in trades if getattr(t, "pnl", 0) > 0]
    losses = [t for t in trades if getattr(t, "pnl", 0) < 0]
    win_rate = (len(wins) / n) if n > 0 else 0.0
    avg_win  = float(np.mean([t.pnl for t in wins]))   if wins   else 0.0
    avg_loss = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    payoff   = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0
    total_pnl = float(sum(getattr(t, "pnl", 0) for t in trades))
    total_fee = float(sum(getattr(t, "fee", 0) for t in trades))
    total_funding = float(sum(getattr(t, "funding", 0) for t in trades))

    return {
        "final_equity":              final,
        "initial_capital":           initial_capital,
        "total_return_pct":          total_return * 100,
        "cagr_pct":                  cagr * 100,
        "max_drawdown_pct":          max_dd * 100,
        "vol_annual_pct":            vol_annual * 100,
        "sharpe":                    sharpe,
        "sortino":                   sortino,
        "calmar":                    calmar,
        "longest_no_new_high_bars":  int(longest_no_new_high),
        "longest_no_new_high_days":  float(longest_no_new_high_days),
        "trades":                    n,
        "win_rate_pct":              win_rate * 100,
        "avg_win":                   avg_win,
        "avg_loss":                  avg_loss,
        "payoff_ratio":              payoff,
        "total_pnl":                 total_pnl,
        "total_fee":                 total_fee,
        "total_funding":             total_funding,
        "years":                     years,
    }


def _longest_stretch_without_new_high(ec: pd.Series) -> int:
    """返回最长的"未创新高"连续 bar 数。"""
    peak = ec.cummax()
    below = (ec < peak).astype(int).to_numpy()
    if below.sum() == 0:
        return 0
    # 最长连续 1 的长度
    max_run = 0
    run = 0
    for v in below:
        if v:
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return int(max_run)


def math_sqrt(x: float) -> float:
    return float(np.sqrt(x))


def _empty_metrics() -> dict:
    return {k: 0 for k in [
        "final_equity", "initial_capital", "total_return_pct", "cagr_pct",
        "max_drawdown_pct", "vol_annual_pct", "sharpe", "sortino", "calmar",
        "longest_no_new_high_bars", "longest_no_new_high_days",
        "trades", "win_rate_pct", "avg_win", "avg_loss", "payoff_ratio",
        "total_pnl", "total_fee", "total_funding", "years",
    ]}
