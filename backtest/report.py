"""
backtest/report.py — 回测报告生成器

生成到 reports/YYYYMMDD_HHMMSS_<note>/：
  - metrics.json      全部指标
  - trades.csv        每笔交易明细
  - equity.csv        权益曲线（timestamp, equity）
  - equity.png        权益曲线图（matplotlib）
  - config.json       BacktestConfig 快照
  - summary.txt       人类可读的简报
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from typing import Any

import pandas as pd


def save_report(
    result: dict,
    note: str = "run",
    reports_dir: str | None = None,
) -> str:
    """把 run_backtest 返回的 result 落地到一个时间戳目录，返回目录路径。"""
    if reports_dir is None:
        reports_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "reports",
        )
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_note = note.replace("/", "_").replace(" ", "_")
    out_dir = os.path.join(reports_dir, f"{stamp}_{safe_note}")
    os.makedirs(out_dir, exist_ok=True)

    # metrics.json
    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(result["metrics"], f, indent=2, ensure_ascii=False)

    # config.json
    with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(result.get("config", {}), f, indent=2, ensure_ascii=False)

    # trades.csv
    trades = result.get("trades", [])
    if trades:
        pd.DataFrame(trades).to_csv(os.path.join(out_dir, "trades.csv"),
                                    index=False, encoding="utf-8")
    else:
        # 创建空文件，便于目录完整性
        open(os.path.join(out_dir, "trades.csv"), "w", encoding="utf-8").close()

    # equity.csv
    ec: pd.Series = result["equity_curve"]
    ec.to_csv(os.path.join(out_dir, "equity.csv"), header=["equity"])

    # equity.png
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7),
                                       gridspec_kw={"height_ratios": [3, 1]})
        ec.plot(ax=ax1, color="#1f77b4", linewidth=1.0)
        ax1.set_title(f"Equity Curve — {safe_note}")
        ax1.set_ylabel("Equity (USDT)")
        ax1.grid(alpha=0.3)

        peak = ec.cummax()
        dd = (ec - peak) / peak * 100
        dd.plot(ax=ax2, color="#d62728", linewidth=0.8)
        ax2.fill_between(dd.index, dd.values, 0, color="#d62728", alpha=0.2)
        ax2.set_title("Drawdown (%)")
        ax2.grid(alpha=0.3)

        plt.tight_layout()
        plt.savefig(os.path.join(out_dir, "equity.png"), dpi=100)
        plt.close(fig)
    except Exception as e:
        print(f"[report] equity.png 绘制失败: {e}")

    # summary.txt（人类可读）
    m = result["metrics"]
    cfg = result.get("config", {})
    summary = _format_summary(safe_note, m, cfg, liquidated=result.get("liquidated", False))
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary)
    print(summary)
    print(f"\n[report] 全部产物 → {out_dir}")
    return out_dir


def _format_summary(note: str, m: dict, cfg: dict, liquidated: bool) -> str:
    lev = cfg.get("leverage", "?")
    risk = cfg.get("risk_per_trade_pct", 0) * 100
    lines = [
        "=" * 64,
        f"  Backtest Report: {note}",
        "=" * 64,
        f"  Initial Capital   : {m.get('initial_capital', 0):>12,.2f} USDT",
        f"  Final Equity      : {m.get('final_equity',   0):>12,.2f} USDT",
        f"  Total Return      : {m.get('total_return_pct', 0):>12,.2f} %",
        f"  CAGR              : {m.get('cagr_pct', 0):>12,.2f} %",
        f"  Max Drawdown      : {m.get('max_drawdown_pct', 0):>12,.2f} %",
        f"  Annual Volatility : {m.get('vol_annual_pct', 0):>12,.2f} %",
        f"  Sharpe            : {m.get('sharpe',  0):>12,.2f}",
        f"  Sortino           : {m.get('sortino', 0):>12,.2f}",
        f"  Calmar            : {m.get('calmar',  0):>12,.2f}",
        f"  Longest No-NewHigh: {m.get('longest_no_new_high_days', 0):>12,.1f} days",
        "-" * 64,
        f"  Trades            : {m.get('trades', 0):>12}",
        f"  Win Rate          : {m.get('win_rate_pct', 0):>12,.2f} %",
        f"  Payoff Ratio      : {m.get('payoff_ratio', 0):>12,.2f}",
        f"  Avg Win / Loss    : {m.get('avg_win', 0):>8,.2f} / {m.get('avg_loss', 0):>8,.2f}",
        f"  Total Fees        : {m.get('total_fee', 0):>12,.2f}",
        f"  Total Funding     : {m.get('total_funding', 0):>12,.2f}",
        "-" * 64,
        f"  Leverage          : {lev}x",
        f"  Risk / Trade      : {risk:.2f} %",
        f"  Years             : {m.get('years', 0):>12,.2f}",
        f"  Liquidated        : {'YES ⚠️' if liquidated else 'No'}",
        "=" * 64,
    ]
    return "\n".join(lines)
