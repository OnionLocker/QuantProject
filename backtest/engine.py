"""
backtest/engine.py - 离线回测引擎（支持自定义品种 / 周期 / 日期范围 / 初始资金）

调用示例：
  result = run_backtest(
      strategy    = PriceActionV2(),
      symbol      = "ETH/USDT",
      timeframe   = "1h",
      start_date  = "2023-01-01",
      end_date    = "2024-01-01",
      initial_capital = 5000.0,
  )
"""
import sys
import os
from datetime import datetime, timedelta

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)
import pandas as pd
pd.set_option("future.no_silent_downcasting", True)

from tqdm import tqdm
from data.market_data import fetch_history_range
from strategy.registry import get_strategy
from risk.risk_manager import RiskManager
from utils.config_loader import get_config

# ── 从 config.yaml 读取默认参数 ──────────────────────────────────────────────
_cfg = get_config()
_bc  = _cfg.get("bot",      {})
_rc  = _cfg.get("risk",     {})
_sc  = _cfg.get("strategy", {})

DEFAULT_SYMBOL      = _bc.get("symbol", "BTC/USDT:USDT").split(":")[0]
DEFAULT_TIMEFRAME   = _bc.get("timeframe",  "1h")
DEFAULT_LEVERAGE    = _bc.get("leverage",    3)
DEFAULT_CONTRACT    = _bc.get("contract_size",    0.01)
DEFAULT_FEE_RATE    = _bc.get("taker_fee_rate",   0.0005)
DEFAULT_RISK_PCT    = _rc.get("risk_per_trade_pct", 0.01)
DEFAULT_MAX_AMT     = _rc.get("max_trade_amount",  5000)
SLIPPAGE            = 0.0002

# 支持回测的品种白名单
SUPPORTED_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
    "LTC/USDT", "BCH/USDT", "MATIC/USDT", "UNI/USDT", "ATOM/USDT",
]

# 支持的周期
SUPPORTED_TIMEFRAMES = ["15m", "1h", "4h", "1d"]


def run_backtest(
    strategy=None,
    symbol: str          = None,
    timeframe: str       = None,
    start_date: str      = None,
    end_date: str        = None,
    initial_capital: float = 5000.0,
    silent: bool         = False,
) -> dict:
    """
    :param strategy:        策略实例（为 None 时从 config.yaml 自动创建）
    :param symbol:          交易对，如 "ETH/USDT"（默认 config.yaml）
    :param timeframe:       K 线周期（默认 config.yaml）
    :param start_date:      开始日期 "YYYY-MM-DD"（默认 3 年前）
    :param end_date:        结束日期 "YYYY-MM-DD"（默认今天）
    :param initial_capital: 初始资金（U）
    :param silent:          True 时抑制终端输出
    :return:                包含统计指标 + equity_curve 的 dict
    """
    # 参数默认值
    if strategy is None:
        strategy = get_strategy(_sc.get("name", "PA_V2"), **_sc.get("params", {}))
    symbol    = (symbol    or DEFAULT_SYMBOL).strip()
    timeframe = (timeframe or DEFAULT_TIMEFRAME).strip()

    today = datetime.now().strftime("%Y-%m-%d")
    three_years_ago = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    start_date = start_date or three_years_ago
    end_date   = end_date   or today

    # 参数校验
    if symbol not in SUPPORTED_SYMBOLS:
        return {"status": "error", "error": f"不支持的品种: {symbol}"}
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"status": "error", "error": f"不支持的周期: {timeframe}"}
    try:
        s_dt = datetime.strptime(start_date, "%Y-%m-%d")
        e_dt = datetime.strptime(end_date,   "%Y-%m-%d")
        if e_dt <= s_dt:
            return {"status": "error", "error": "结束日期必须晚于开始日期"}
        if (e_dt - s_dt).days < 30:
            return {"status": "error", "error": "回测区间不能少于 30 天"}
    except ValueError as ve:
        return {"status": "error", "error": f"日期格式错误: {ve}"}

    # 确定合约规格（合约张数 = 1 个最小单位）
    CONTRACT_SIZE = DEFAULT_CONTRACT
    LEVERAGE      = DEFAULT_LEVERAGE
    FEE_RATE      = DEFAULT_FEE_RATE
    RISK_PCT      = DEFAULT_RISK_PCT

    rm = RiskManager(max_trade_amount=DEFAULT_MAX_AMT)

    # 下载历史数据
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) == 0:
        return {"status": "error", "error": f"无法获取 {symbol} {timeframe} 历史数据，请稍后重试"}

    # ── 回测主循环 ───────────────────────────────────────────────────────────
    balance         = initial_capital
    position_amount = 0
    position_side   = None
    entry_price     = 0.0
    open_fee_paid   = 0.0
    active_sl       = 0.0
    active_tp       = 0.0
    total_fees_paid = 0.0

    total_trades    = winning_trades = losing_trades = 0
    max_balance     = initial_capital
    max_drawdown    = 0.0

    equity_curve = []   # [{date, balance}] 每笔交易后记录一个点

    if not silent:
        print(f"\n🚀 回测: {strategy.name} | {symbol} {timeframe} | "
              f"{start_date} → {end_date} | 初始资金: {initial_capital} U")
        print("-" * 75)

    start_idx = max(51, getattr(strategy, "swing_l", 8) * 2 + 10)
    _iter = tqdm(range(start_idx, len(df)), desc="⏳ 回测", unit="K", ncols=90,
                 disable=silent)

    for i in _iter:
        historical_slice = df.iloc[:i]
        candle           = df.iloc[i]
        ts               = candle.name
        c_open           = candle["open"]
        c_high           = candle["high"]
        c_low            = candle["low"]
        c_close          = candle["close"]

        signal = strategy.generate_signal(historical_slice)
        action, reason = signal["action"], signal["reason"]

        # 回撤追踪
        if balance > max_balance:
            max_balance = balance
        dd = (max_balance - balance) / max_balance * 100
        if dd > max_drawdown:
            max_drawdown = dd

        # ── 空仓开仓 ────────────────────────────────────────────────────────
        if position_amount == 0 and action in ("BUY", "SELL"):
            actual_entry = (c_open * (1 + SLIPPAGE) if action == "BUY"
                            else c_open * (1 - SLIPPAGE))

            contracts = rm.calculate_position_size(
                balance       = balance,
                entry_price   = actual_entry,
                sl_price      = signal["sl"],
                risk_pct      = RISK_PCT,
                contract_size = CONTRACT_SIZE,
                fee_rate      = FEE_RATE,
                leverage      = LEVERAGE,
            )
            if contracts < 1:
                continue

            notional      = contracts * CONTRACT_SIZE * actual_entry
            open_fee      = notional * FEE_RATE
            balance      -= open_fee
            total_fees_paid += open_fee
            open_fee_paid  = open_fee

            position_amount = contracts
            entry_price     = actual_entry
            active_sl       = signal["sl"]
            active_tp       = signal["tp1"]
            position_side   = "long" if action == "BUY" else "short"
            total_trades   += 1

            if not silent:
                emoji = "🟢" if action == "BUY" else "🔴"
                tqdm.write(f"[{ts}] {emoji} 开{position_side} "
                           f"| {contracts}张 @{actual_entry:.2f} SL:{active_sl:.2f}")

        # ── 多单平仓检测 ─────────────────────────────────────────────────────
        elif position_amount > 0 and position_side == "long":
            close_price, close_reason = 0.0, ""

            if c_low <= active_sl:
                close_price, close_reason = active_sl * (1 - SLIPPAGE), "止损"
                losing_trades += 1
            elif c_high >= active_tp:
                close_price, close_reason = active_tp * (1 + SLIPPAGE), "止盈"
                winning_trades += 1
            elif action == "SELL":
                close_price, close_reason = c_close, "反转平多"

            if close_price > 0:
                notional      = position_amount * CONTRACT_SIZE * close_price
                close_fee     = notional * FEE_RATE
                balance      -= close_fee
                total_fees_paid += close_fee
                gross         = (close_price - entry_price) * position_amount * CONTRACT_SIZE
                balance      += gross
                net_pnl       = gross - (open_fee_paid + close_fee)

                equity_curve.append({
                    "date":    str(ts)[:10],
                    "balance": round(balance, 2),
                    "pnl":     round(net_pnl, 2),
                    "result":  "win" if net_pnl > 0 else "loss",
                })

                if not silent:
                    emoji = "🎉" if net_pnl > 0 else "🩸"
                    tqdm.write(f"[{ts}] {emoji} 平多 {close_reason} "
                               f"@{close_price:.2f} 净盈亏:{net_pnl:+.2f}U 余额:{balance:.2f}U")

                position_amount = 0
                position_side   = None

        # ── 空单平仓检测 ─────────────────────────────────────────────────────
        elif position_amount > 0 and position_side == "short":
            close_price, close_reason = 0.0, ""

            if c_high >= active_sl:
                close_price, close_reason = active_sl * (1 + SLIPPAGE), "止损"
                losing_trades += 1
            elif c_low <= active_tp:
                close_price, close_reason = active_tp * (1 - SLIPPAGE), "止盈"
                winning_trades += 1
            elif action == "BUY":
                close_price, close_reason = c_close, "反转平空"

            if close_price > 0:
                notional      = position_amount * CONTRACT_SIZE * close_price
                close_fee     = notional * FEE_RATE
                balance      -= close_fee
                total_fees_paid += close_fee
                gross         = (entry_price - close_price) * position_amount * CONTRACT_SIZE
                balance      += gross
                net_pnl       = gross - (open_fee_paid + close_fee)

                equity_curve.append({
                    "date":    str(ts)[:10],
                    "balance": round(balance, 2),
                    "pnl":     round(net_pnl, 2),
                    "result":  "win" if net_pnl > 0 else "loss",
                })

                if not silent:
                    emoji = "🎉" if net_pnl > 0 else "🩸"
                    tqdm.write(f"[{ts}] {emoji} 平空 {close_reason} "
                               f"@{close_price:.2f} 净盈亏:{net_pnl:+.2f}U 余额:{balance:.2f}U")

                position_amount = 0
                position_side   = None

    win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0.0
    roi      = (balance - initial_capital) / initial_capital * 100

    # 精简资金曲线：最多返回 200 个点（避免响应体过大）
    if len(equity_curve) > 200:
        step = len(equity_curve) // 200
        equity_curve = equity_curve[::step]

    if not silent:
        print("\n" + "=" * 75)
        print(f"💰 初始: {initial_capital:.2f} | 最终: {balance:.2f}")
        print(f"📈 ROI: {roi:+.2f}% | 最大回撤: {max_drawdown:.2f}%")
        print(f"📊 总交易: {total_trades} | 盈: {winning_trades} | 亏: {losing_trades} | 胜率: {win_rate:.2f}%")
        print(f"💸 总手续费: {total_fees_paid:.2f} U")
        print(f"[参数] 品种={symbol} 周期={timeframe} 杠杆={LEVERAGE}x 风险={RISK_PCT*100}%/笔")

    return {
        "status":            "done",
        "strategy":          strategy.name,
        "symbol":            symbol,
        "timeframe":         timeframe,
        "start_date":        start_date,
        "end_date":          end_date,
        "initial_capital":   initial_capital,
        "final_balance":     round(balance, 2),
        "roi_pct":           round(roi, 2),
        "max_drawdown_pct":  round(max_drawdown, 2),
        "win_rate_pct":      round(win_rate, 2),
        "total_trades":      total_trades,
        "winning_trades":    winning_trades,
        "losing_trades":     losing_trades,
        "total_fees_paid":   round(total_fees_paid, 2),
        "equity_curve":      equity_curve,
        "candle_count":      len(df),
    }


if __name__ == "__main__":
    result = run_backtest()
    print(f"\n资金曲线样本（前5笔）: {result.get('equity_curve', [])[:5]}")
