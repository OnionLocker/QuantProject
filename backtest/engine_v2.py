"""
backtest/engine_v2.py — 极简向量化回测引擎（V11 重构版）

设计原则：
  - 一个策略对象在回测/实盘共用，策略只暴露 generate_signal(df) -> dict
  - 向量化到能向量化的都向量化（指标计算），循环只在"事件级"必须的地方（开/平仓/资金更新）
  - 诚实建模：taker 费 + 自适应滑点 + funding 8h 扣 + 强平
  - 200-300 行以内；任何"V7-V10 补丁式"的代码都不能出现

使用：
    from backtest.engine_v2 import run_backtest, BacktestConfig
    from bot.strategy import DonchianStrategy
    result = run_backtest(DonchianStrategy(), df, BacktestConfig(leverage=3))

返回 dict：
    {
      "trades": [ {entry_time, exit_time, side, entry, exit, pnl, fee, funding, ...} ],
      "equity_curve": pd.Series (index=timestamp),
      "metrics": dict (from metrics.compute_all),
      "config": BacktestConfig 的字段镜像,
      "liquidated": bool
    }
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Callable, Optional
import math

import numpy as np
import pandas as pd


# ─────────────────────────── 配置 ───────────────────────────

@dataclass
class BacktestConfig:
    initial_capital:    float = 10_000.0
    leverage:           int   = 3
    risk_per_trade_pct: float = 0.005     # 每笔风险 0.5% 账户
    taker_fee_rate:     float = 0.0005    # 单边 taker 费率（进场 + 出场各扣一次）
    slippage_base:      float = 0.0002    # 基础滑点（无波动时）
    slippage_max:       float = 0.003     # 滑点上限
    slippage_atr_mult:  float = 0.5       # 滑点随 ATR/price 放大的系数
    funding_rate_8h:    float = 0.0       # 默认 0：未提供真实历史时不假设成本（真实 BTC funding 长期均值近 0，正负抵消）
    maint_margin_rate:  float = 0.005     # OKX BTC 默认维持保证金率 0.5%
    allow_short:        bool  = True      # 允许做空
    bar_seconds:        int   = 14400     # 4h


# ─────────────────────────── 数据结构 ───────────────────────────

@dataclass
class Trade:
    side:        str    # 'long' | 'short'
    entry_time:  pd.Timestamp
    entry_price: float
    sl_price:    float
    amount:      float  # 合约张数（以 BTC 计，下同）
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float] = None
    exit_reason: str = ""           # 'exit_signal' | 'sl' | 'liquidation' | 'end_of_data'
    fee:         float = 0.0
    funding:     float = 0.0
    pnl:         float = 0.0        # 净盈亏（已扣 fee/funding）


# ─────────────────────────── 工具 ───────────────────────────

def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """经典 ATR（Wilder's），用 EMA(alpha=1/period) 近似。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder ATR: 首个取简单均值，之后 RMA
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return atr


def _slippage_rate(atr: float, price: float, cfg: BacktestConfig) -> float:
    if price <= 0 or not np.isfinite(atr):
        return cfg.slippage_base
    rate = cfg.slippage_base + cfg.slippage_atr_mult * (atr / price)
    return float(min(rate, cfg.slippage_max))


def _liquidation_price(entry: float, side: str, leverage: int, mmr: float) -> float:
    """
    近似清算价（简化版，不含手续费缓冲、累积盈亏）：
      多头：Pliq = entry * (1 - 1/lev + mmr)
      空头：Pliq = entry * (1 + 1/lev - mmr)
    对 3x，多头约 -33.3%+0.5%=-32.8% 触发。
    """
    if side == "long":
        return entry * (1 - 1.0 / leverage + mmr)
    return entry * (1 + 1.0 / leverage - mmr)


# ─────────────────────────── 主循环 ───────────────────────────

def run_backtest(
    strategy: Any,
    df: pd.DataFrame,
    cfg: BacktestConfig | None = None,
    funding_series: pd.Series | None = None,
    progress_cb: Optional[Callable[[float], None]] = None,
) -> dict:
    """
    strategy: 需实现 generate_signal(df_window: pd.DataFrame) -> dict
              返回：{"action": "BUY"|"SELL"|"HOLD"|"EXIT", "sl": float, "reason": str, ...}
              - BUY  开多
              - SELL 开空
              - EXIT 平当前仓（若无仓则忽略）
              - HOLD 什么都不做
              策略自己负责出场（如 Donchian 反向突破触发 EXIT）；回测层负责硬止损和强平。

    df: DataFrame，索引 timestamp，列 open/high/low/close/volume。必须按时间升序。
    funding_series: 可选，索引和 df 对齐（或用 reindex 兜底），值为单次 8h 资金费率（正表示多付空收）。
                    若为 None，使用 cfg.funding_rate_8h 常数近似。
    """
    if cfg is None:
        cfg = BacktestConfig()
    if df is None or df.empty:
        raise ValueError("df 为空")
    if not df.index.is_monotonic_increasing:
        raise ValueError("df 必须按 timestamp 升序")

    # 预先算 ATR，供滑点 / 止损 / 止损距离用
    warmup = getattr(strategy, "warmup_bars", 100)
    atr_period = getattr(strategy, "atr_period", 14)
    atr = _compute_atr(df, atr_period)

    # 资金费率每 8h 扣一次（UTC 00:00 / 08:00 / 16:00），4h 周期下 = 每 2 根 K 线
    funding_hours = {0, 8, 16}

    capital = float(cfg.initial_capital)
    equity  = capital                 # 实时权益（含浮盈）
    position: Optional[Trade] = None  # 当前持仓（最多 1 仓）
    trades: list[Trade] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = []
    liquidated = False

    n = len(df)
    progress_step = max(1, n // 20)

    # 暴露给策略的"当前窗口"切片（策略内部可读前 W 根）
    # 为了避免每轮 copy，这里传 df.iloc[:i+1] 的 view —— pandas 会返回 view，足够快
    for i in range(n):
        if progress_cb is not None and i % progress_step == 0:
            progress_cb(i / n)

        bar = df.iloc[i]
        ts  = df.index[i]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        cur_atr = float(atr.iat[i]) if i >= 1 else float("nan")

        # 热身期：不交易
        if i < warmup:
            equity_curve.append((ts, equity))
            continue

        # ── 1) 若有仓位，先在本根 K 线内结算止损 / 强平（按 bar 内最坏价）
        if position is not None:
            # 计算强平价（基于 entry 和杠杆，不随时间变；仓位的浮盈不纳入保守处理）
            liq = _liquidation_price(position.entry_price, position.side, cfg.leverage, cfg.maint_margin_rate)
            if position.side == "long":
                # 先判是否触发强平（最低价 <= liq）
                if l <= liq:
                    position = _close(position, liq, ts, "liquidation", cur_atr, cfg, capital_ref=None)
                    capital += position.pnl
                    trades.append(position)
                    position = None
                    liquidated = True
                    break  # 账户爆仓，终止
                # 再判硬止损
                elif l <= position.sl_price:
                    position = _close(position, position.sl_price, ts, "sl", cur_atr, cfg, capital_ref=None)
                    capital += position.pnl
                    trades.append(position)
                    position = None
            else:  # short
                if h >= liq:
                    position = _close(position, liq, ts, "liquidation", cur_atr, cfg, capital_ref=None)
                    capital += position.pnl
                    trades.append(position)
                    position = None
                    liquidated = True
                    break
                elif h >= position.sl_price:
                    position = _close(position, position.sl_price, ts, "sl", cur_atr, cfg, capital_ref=None)
                    capital += position.pnl
                    trades.append(position)
                    position = None

        # ── 2) 资金费结算（持仓穿越 00/08/16 UTC 时扣一次）
        if position is not None and ts.hour in funding_hours and ts.minute == 0:
            f_rate = (funding_series.get(ts, cfg.funding_rate_8h)
                      if funding_series is not None else cfg.funding_rate_8h)
            # 多头付/收 funding；正费率 = 多付空收
            notional = position.amount * c
            f_pay = notional * (f_rate if position.side == "long" else -f_rate)
            capital -= f_pay
            position.funding += f_pay

        # ── 3) 问策略要信号（传 i+1 个 bar，相当于"本根收盘后决策"）
        signal = strategy.generate_signal(df.iloc[: i + 1])
        action = (signal or {}).get("action", "HOLD")

        # ── 4) 处理 EXIT（策略自己平仓）
        if action == "EXIT" and position is not None:
            # 用本根收盘价 + 滑点平
            position = _close(position, c, ts, "exit_signal", cur_atr, cfg, capital_ref=None)
            capital += position.pnl
            trades.append(position)
            position = None

        # ── 5) 处理 BUY/SELL（仅在空仓时开新仓）
        if position is None and action in ("BUY", "SELL"):
            side = "long" if action == "BUY" else "short"
            if side == "short" and not cfg.allow_short:
                pass
            else:
                sl = float(signal.get("sl", 0.0))
                if sl <= 0 or not np.isfinite(sl):
                    # 策略未给 sl，用 2×ATR 兜底
                    sl = c - 2 * cur_atr if side == "long" else c + 2 * cur_atr
                risk_per_unit = abs(c - sl)
                if risk_per_unit <= 0:
                    pass
                else:
                    # 仓位张数 = 单笔风险金额 / 每张风险
                    risk_amount = capital * cfg.risk_per_trade_pct
                    amount = risk_amount / risk_per_unit
                    # 保证金约束：amount * price / leverage <= capital
                    max_amount = capital * cfg.leverage / c
                    amount = min(amount, max_amount)
                    if amount > 1e-8:
                        # 入场：扣 taker fee + 滑点（不利方向）
                        slip = _slippage_rate(cur_atr, c, cfg)
                        fill_px = c * (1 + slip) if side == "long" else c * (1 - slip)
                        fee = fill_px * amount * cfg.taker_fee_rate
                        capital -= fee
                        position = Trade(
                            side=side,
                            entry_time=ts,
                            entry_price=fill_px,
                            sl_price=sl,
                            amount=amount,
                            fee=fee,
                        )

        # ── 6) 实时权益（含浮盈）
        if position is not None:
            if position.side == "long":
                unreal = (c - position.entry_price) * position.amount
            else:
                unreal = (position.entry_price - c) * position.amount
            equity = capital + unreal
        else:
            equity = capital

        equity_curve.append((ts, equity))

    # 数据结束时还持仓 → 按最后收盘平
    if position is not None and not liquidated:
        last_c = float(df["close"].iat[-1])
        last_ts = df.index[-1]
        last_atr = float(atr.iat[-1])
        position = _close(position, last_c, last_ts, "end_of_data", last_atr, cfg, capital_ref=None)
        capital += position.pnl
        trades.append(position)
        position = None
        equity_curve[-1] = (last_ts, capital)

    # 转 pandas
    ec_df = pd.Series(
        [e for _, e in equity_curve],
        index=pd.DatetimeIndex([t for t, _ in equity_curve]),
        name="equity",
    )

    # 产出 metrics（延迟导入，避免循环依赖）
    from backtest.metrics import compute_all
    metrics = compute_all(ec_df, trades, cfg.initial_capital, cfg.bar_seconds)

    return {
        "trades": [_trade_to_dict(t) for t in trades],
        "equity_curve": ec_df,
        "metrics": metrics,
        "config": asdict(cfg),
        "liquidated": liquidated,
    }


# ─────────────────────────── helper：平仓 ───────────────────────────

def _close(
    pos: Trade,
    fill_price: float,
    ts: pd.Timestamp,
    reason: str,
    cur_atr: float,
    cfg: BacktestConfig,
    capital_ref,  # 预留（目前不用）
) -> Trade:
    """平仓结算：扣 taker 费 + 滑点，计算净 pnl。"""
    # 滑点：不利方向
    slip = _slippage_rate(cur_atr, fill_price, cfg)
    if pos.side == "long":
        exit_px = fill_price * (1 - slip)
        gross = (exit_px - pos.entry_price) * pos.amount
    else:
        exit_px = fill_price * (1 + slip)
        gross = (pos.entry_price - exit_px) * pos.amount

    exit_fee = exit_px * pos.amount * cfg.taker_fee_rate

    pos.exit_time   = ts
    pos.exit_price  = exit_px
    pos.exit_reason = reason
    pos.fee        += exit_fee
    pos.pnl         = gross - exit_fee - 0  # entry fee 已在开仓时从 capital 扣了，这里不重复
    # 但 funding 在持仓期间已从 capital 实时扣过，这里也不重复
    # pos.funding 记录总额仅作审计
    return pos


def _trade_to_dict(t: Trade) -> dict:
    d = asdict(t)
    # Timestamp → iso
    d["entry_time"] = t.entry_time.isoformat() if t.entry_time is not None else None
    d["exit_time"]  = t.exit_time.isoformat()  if t.exit_time  is not None else None
    return d
