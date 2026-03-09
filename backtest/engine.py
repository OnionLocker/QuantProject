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


# ── 自适应滑点：根据 ATR 占价格比例动态调整 ───────────────────────────────────
def _adaptive_slippage(atr: float, price: float,
                       base: float = 0.0002,
                       scale: float = 0.15) -> float:
    """
    滑点 = max(base, atr/price * scale)
    - 低波动期：接近 base (0.02%)
    - 高波动期：随 ATR 线性放大，最高 0.5%
    示例：ATR/price=0.5% → 滑点≈0.075%；ATR/price=2% → 0.3%
    """
    if price <= 0 or atr <= 0:
        return base
    dynamic = (atr / price) * scale
    return min(max(dynamic, base), 0.005)   # 最高 0.5%


def run_backtest(
    strategy=None,
    symbol: str            = None,
    timeframe: str         = None,
    start_date: str        = None,
    end_date: str          = None,
    initial_capital: float = 5000.0,
    # ── 执行层参数（用户可在回测界面调整）────────────────────────────────────
    leverage:    float     = None,   # 杠杆倍数，None = 读 config.yaml
    risk_pct:    float     = None,   # 单笔风险占本金比例，None = 读 config.yaml
    fee_rate:    float     = None,   # 手续费率，None = 读 config.yaml
    slippage:    float     = None,   # 滑点假设，None = 使用默认值 0.0002
    silent: bool           = False,
    progress_cb            = None,   # 进度回调 callable(pct: int)，每10%调用一次
) -> dict:
    """
    :param strategy:        策略实例（为 None 时从 config.yaml 自动创建）
    :param symbol:          交易对，如 "ETH/USDT"
    :param timeframe:       K 线周期
    :param start_date:      开始日期 "YYYY-MM-DD"
    :param end_date:        结束日期 "YYYY-MM-DD"
    :param initial_capital: 初始资金（U）
    :param leverage:        杠杆倍数（默认读 config.yaml）
    :param risk_pct:        单笔风险占本金比例（默认读 config.yaml）
    :param fee_rate:        手续费率（默认读 config.yaml）
    :param slippage:        滑点（默认 0.0002）
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
        if e_dt < s_dt:
            return {"status": "error", "error": "结束日期不能早于开始日期"}
    except ValueError as ve:
        return {"status": "error", "error": f"日期格式错误: {ve}"}

    # 执行层参数：用户传入则覆盖默认值
    CONTRACT_SIZE = DEFAULT_CONTRACT
    LEVERAGE      = float(leverage)  if leverage  is not None else DEFAULT_LEVERAGE
    FEE_RATE      = float(fee_rate)  if fee_rate  is not None else DEFAULT_FEE_RATE
    RISK_PCT      = float(risk_pct)  if risk_pct  is not None else DEFAULT_RISK_PCT
    SLIPPAGE      = float(slippage)  if slippage  is not None else 0.0002

    rm = RiskManager(max_trade_amount=DEFAULT_MAX_AMT)

    # 下载历史数据
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) == 0:
        return {"status": "error", "error": f"无法获取 {symbol} {timeframe} 历史数据，请稍后重试"}

    # ── 对整个 df 一次性预计算指标，避免逐K线重复 rolling ───────────────────
    if hasattr(strategy, "precompute"):
        df = strategy.precompute(df)

    # ── 预计算 ATR（用于自适应滑点，不依赖策略是否计算过）────────────────────
    if "atr" not in df.columns:
        _tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"]  - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = _tr.rolling(14).mean()

    # 提取市场状态分布统计（ADAPTIVE 策略特有）
    regime_stats = {}
    if hasattr(strategy, "regime_stats"):
        regime_stats = strategy.regime_stats(df)

    # ── 回测主循环 ───────────────────────────────────────────────────────────
    balance         = initial_capital
    position_amount = 0
    position_side   = None
    trades          = []          # 详细交易列表，用于前端 K 线图标注
    _open_trade_idx = None        # 当前持仓的开仓 K 线 index（用于关联平仓）
    entry_price     = 0.0
    open_fee_paid   = 0.0
    active_sl       = 0.0
    active_tp       = 0.0
    total_fees_paid = 0.0

    total_trades    = winning_trades = losing_trades = 0
    max_balance     = initial_capital
    max_drawdown    = 0.0

    equity_curve = []   # [{date, balance}] 每笔交易后记录一个点

    # ── 回测内熔断模拟（与实盘风控保持一致）────────────────────────────────
    # 使用独立实例，避免污染全局 rm
    bt_rm = RiskManager(
        max_trade_amount       = DEFAULT_MAX_AMT,
        max_consecutive_losses = rm.max_consecutive_losses,
        daily_loss_limit_pct   = rm.daily_loss_limit_pct,
    )
    bt_rm.set_daily_start_balance(initial_capital)
    fuse_triggered_count = 0   # 统计熔断触发次数

    if not silent:
        print(f"\n🚀 回测: {strategy.name} | {symbol} {timeframe} | "
              f"{start_date} → {end_date} | 初始资金: {initial_capital} U")
        print("-" * 75)

    start_idx = getattr(strategy, "warmup_bars", 50)

    # K线数不足以覆盖预热期 → 直接返回"无交易"结果，不报错
    if len(df) <= start_idx:
        if not silent:
            print(f"⚠️ K线数({len(df)})不足预热期({start_idx})，本区间无法触发交易。")
        return {
            "status":            "done",
            "strategy":          strategy.name,
            "symbol":            symbol,
            "timeframe":         timeframe,
            "start_date":        start_date,
            "end_date":          end_date,
            "initial_capital":   initial_capital,
            "final_balance":     round(initial_capital, 2),
            "roi_pct":           0.0,
            "max_drawdown_pct":  0.0,
            "win_rate_pct":      0.0,
            "total_trades":      0,
            "winning_trades":    0,
            "losing_trades":     0,
            "total_fees_paid":   0.0,
            "equity_curve":      [],
            "candle_count":      len(df),
            "note":              f"K线数({len(df)})不足策略预热期({start_idx}根)，区间内无交易触发。",
        }

    _iter = tqdm(range(start_idx, len(df)), desc="⏳ 回测", unit="K", ncols=90,
                 disable=silent)

    total_steps     = len(df) - start_idx
    last_pct_report = -1   # 进度上报去重

    for i in _iter:
        # 进度回调：每完成 10% 上报一次
        if progress_cb and total_steps > 0:
            pct = int((i - start_idx) / total_steps * 100)
            pct = min(pct, 99)   # 100% 留给引擎返回结果时
            if pct // 10 != last_pct_report // 10:
                last_pct_report = pct
                progress_cb(pct)
        # ── 熔断检查：与实盘逻辑保持一致，熔断时跳过开仓 ─────────────────
        if bt_rm.is_fused:
            # 有持仓时继续监控平仓，但不开新仓
            pass

        candle           = df.iloc[i]
        ts               = candle.name
        c_open           = candle["open"]
        c_high           = candle["high"]
        c_low            = candle["low"]
        c_close          = candle["close"]

        # 优先使用预计算行信号，否则降级为滑窗切片
        if hasattr(strategy, "signal_from_row"):
            signal = strategy.signal_from_row(df, i)
        else:
            SIGNAL_WINDOW = max(200, getattr(strategy, "swing_l", 8) * 6 + 50)
            win_start = max(0, i - SIGNAL_WINDOW)
            signal = strategy.generate_signal(df.iloc[win_start:i])

        action, reason = signal["action"], signal["reason"]

        # 回撤追踪
        if balance > max_balance:
            max_balance = balance
        dd = (max_balance - balance) / max_balance * 100
        if dd > max_drawdown:
            max_drawdown = dd

        # ── 空仓开仓 ────────────────────────────────────────────────────────
        if position_amount == 0 and action in ("BUY", "SELL") and not bt_rm.is_fused:
            _atr_val   = candle.get("atr", 0) if hasattr(candle, "get") else (candle["atr"] if "atr" in candle.index else 0)
            _slip      = _adaptive_slippage(_atr_val, c_open, SLIPPAGE) if slippage is None else SLIPPAGE
            actual_entry = (c_open * (1 + _slip) if action == "BUY"
                            else c_open * (1 - _slip))

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
            _open_trade_idx = i

            # 记录开仓信息（后续平仓时补全）
            trades.append({
                "entry_ts":    str(ts),
                "entry_price": round(actual_entry, 4),
                "side":        position_side,
                "sl":          round(active_sl, 4),
                "tp":          round(active_tp, 4),
                "contracts":   contracts,
                # 平仓后补全：
                "exit_ts":     None,
                "exit_price":  None,
                "exit_reason": None,
                "pnl":         None,
                "result":      None,
            })

            if not silent:
                emoji = "🟢" if action == "BUY" else "🔴"
                tqdm.write(f"[{ts}] {emoji} 开{position_side} "
                           f"| {contracts}张 @{actual_entry:.2f} SL:{active_sl:.2f}")

        # ── 多单平仓检测 ─────────────────────────────────────────────────────
        elif position_amount > 0 and position_side == "long":
            close_price, close_reason = 0.0, ""
            _atr_c = candle["atr"] if "atr" in candle.index else 0
            _close_slip = _adaptive_slippage(_atr_c, c_open, SLIPPAGE) if slippage is None else SLIPPAGE

            sl_hit = c_low  <= active_sl
            tp_hit = c_high >= active_tp

            if sl_hit and tp_hit:
                # 同一根K线同时触碰SL和TP：按谁离入场价更近来判断先触发
                # 多单：SL在下方，TP在上方；谁距入场价更近谁先触发
                dist_sl = entry_price - active_sl
                dist_tp = active_tp  - entry_price
                if dist_tp <= dist_sl:
                    close_price, close_reason = active_tp * (1 + _close_slip), "止盈"
                    winning_trades += 1
                else:
                    close_price, close_reason = active_sl * (1 - _close_slip), "止损"
                    losing_trades += 1
            elif sl_hit:
                close_price, close_reason = active_sl * (1 - _close_slip), "止损"
                losing_trades += 1
            elif tp_hit:
                close_price, close_reason = active_tp * (1 + _close_slip), "止盈"
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

                # ── 回测内熔断模拟：通知风控，触发时记录并跳过后续开仓 ──────
                bt_rm.notify_trade_result(net_pnl, balance)
                if bt_rm.is_fused:
                    fuse_triggered_count += 1
                    if not silent:
                        tqdm.write(f"[{ts}] 🚨 回测熔断触发（第{fuse_triggered_count}次），跳过后续开仓直至手动恢复")
                    # 回测中自动恢复熔断（模拟日内结束后恢复）
                    bt_rm.manual_resume()

                # 补全多单交易记录
                if trades and trades[-1]["exit_ts"] is None:
                    t = trades[-1]
                    t["exit_ts"]    = str(ts)
                    t["exit_price"] = round(close_price, 4)
                    t["exit_reason"]= close_reason
                    t["pnl"]        = round(net_pnl, 2)
                    t["result"]     = "win" if net_pnl > 0 else "loss"

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
            _atr_c = candle["atr"] if "atr" in candle.index else 0
            _close_slip = _adaptive_slippage(_atr_c, c_open, SLIPPAGE) if slippage is None else SLIPPAGE

            sl_hit = c_high >= active_sl
            tp_hit = c_low  <= active_tp

            if sl_hit and tp_hit:
                # 同一根K线同时触碰SL和TP：按谁离入场价更近来判断先触发
                # 空单：SL在上方，TP在下方；谁距入场价更近谁先触发
                dist_sl = active_sl  - entry_price
                dist_tp = entry_price - active_tp
                if dist_tp <= dist_sl:
                    close_price, close_reason = active_tp * (1 - _close_slip), "止盈"
                    winning_trades += 1
                else:
                    close_price, close_reason = active_sl * (1 + _close_slip), "止损"
                    losing_trades += 1
            elif sl_hit:
                close_price, close_reason = active_sl * (1 + _close_slip), "止损"
                losing_trades += 1
            elif tp_hit:
                close_price, close_reason = active_tp * (1 - _close_slip), "止盈"
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

                # ── 回测内熔断模拟：通知风控，触发时记录并跳过后续开仓 ──────
                bt_rm.notify_trade_result(net_pnl, balance)
                if bt_rm.is_fused:
                    fuse_triggered_count += 1
                    if not silent:
                        tqdm.write(f"[{ts}] 🚨 回测熔断触发（第{fuse_triggered_count}次），跳过后续开仓直至手动恢复")
                    bt_rm.manual_resume()

                # 补全空单交易记录
                if trades and trades[-1]["exit_ts"] is None:
                    t = trades[-1]
                    t["exit_ts"]    = str(ts)
                    t["exit_price"] = round(close_price, 4)
                    t["exit_reason"]= close_reason
                    t["pnl"]        = round(net_pnl, 2)
                    t["result"]     = "win" if net_pnl > 0 else "loss"

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

    # ── 高级统计指标 ─────────────────────────────────────────────────────────
    import math as _math

    # 每笔交易收益率序列
    trade_returns = [t["pnl"] / initial_capital for t in trades if t.get("pnl") is not None]

    # 夏普比率（年化，假设无风险利率 0，按交易笔数年化）
    sharpe_ratio = 0.0
    if len(trade_returns) >= 2:
        mean_r = sum(trade_returns) / len(trade_returns)
        std_r  = (_math.sqrt(sum((r - mean_r) ** 2 for r in trade_returns) / (len(trade_returns) - 1))
                  if len(trade_returns) > 1 else 0)
        if std_r > 0:
            # 年化因子：假设每年约 252 个交易日，每日平均交易次数
            days_total = max(1, (datetime.strptime(end_date, "%Y-%m-%d") -
                                 datetime.strptime(start_date, "%Y-%m-%d")).days)
            trades_per_year = len(trade_returns) / days_total * 365
            sharpe_ratio = round((mean_r / std_r) * _math.sqrt(trades_per_year), 3)

    # Sortino 比率（只用下行波动率）
    sortino_ratio = 0.0
    if len(trade_returns) >= 2:
        mean_r    = sum(trade_returns) / len(trade_returns)
        neg_rets  = [r for r in trade_returns if r < 0]
        if neg_rets:
            downside_std = _math.sqrt(sum(r ** 2 for r in neg_rets) / len(neg_rets))
            if downside_std > 0:
                days_total = max(1, (datetime.strptime(end_date, "%Y-%m-%d") -
                                     datetime.strptime(start_date, "%Y-%m-%d")).days)
                trades_per_year = len(trade_returns) / days_total * 365
                sortino_ratio = round((mean_r / downside_std) * _math.sqrt(trades_per_year), 3)

    # Calmar 比率（年化收益 / 最大回撤）
    calmar_ratio = 0.0
    if max_drawdown > 0:
        days_total     = max(1, (datetime.strptime(end_date, "%Y-%m-%d") -
                                 datetime.strptime(start_date, "%Y-%m-%d")).days)
        annualized_roi = roi / 100 * (365 / days_total)
        calmar_ratio   = round(annualized_roi / (max_drawdown / 100), 3)

    # 盈亏比（平均盈利 / 平均亏损）
    wins  = [t["pnl"] for t in trades if t.get("pnl") is not None and t["pnl"] > 0]
    loses = [t["pnl"] for t in trades if t.get("pnl") is not None and t["pnl"] < 0]
    profit_factor = 0.0
    avg_win_pct   = 0.0
    avg_loss_pct  = 0.0
    if wins and loses:
        avg_win  = sum(wins)  / len(wins)
        avg_loss = abs(sum(loses) / len(loses))
        profit_factor = round(avg_win / avg_loss, 3) if avg_loss > 0 else 0.0
        avg_win_pct   = round(avg_win  / initial_capital * 100, 3)
        avg_loss_pct  = round(avg_loss / initial_capital * 100, 3)

    # 精简资金曲线：最多返回 200 个点（避免响应体过大）
    if len(equity_curve) > 200:
        step = len(equity_curve) // 200
        equity_curve = equity_curve[::step]

    # ── 构建 K 线数据供前端图表使用 ──────────────────────────────────────────
    # 仅保留回测区间内的 K 线，并精简到最多 2000 根（避免响应体过大）
    candles_raw = []
    for idx_ts, row in df.iterrows():
        candles_raw.append({
            "time":  int(pd.Timestamp(idx_ts).timestamp()),
            "open":  round(float(row["open"]),  4),
            "high":  round(float(row["high"]),  4),
            "low":   round(float(row["low"]),   4),
            "close": round(float(row["close"]), 4),
        })
    MAX_CANDLES = 2000
    if len(candles_raw) > MAX_CANDLES:
        step = len(candles_raw) // MAX_CANDLES
        candles_raw = candles_raw[::step]

    # 修复：回测完成后推送 100% 进度
    if progress_cb:
        progress_cb(100)

    if not silent:
        print("\n" + "=" * 75)
        print(f"💰 初始: {initial_capital:.2f} | 最终: {balance:.2f}")
        print(f"📈 ROI: {roi:+.2f}% | 最大回撤: {max_drawdown:.2f}%")
        print(f"📊 总交易: {total_trades} | 盈: {winning_trades} | 亏: {losing_trades} | 胜率: {win_rate:.2f}%")
        print(f"💸 总手续费: {total_fees_paid:.2f} U")
        if fuse_triggered_count > 0:
            print(f"🚨 回测熔断触发次数: {fuse_triggered_count}")
        print(f"[参数] 品种={symbol} 周期={timeframe} 杠杆={LEVERAGE}x 风险={RISK_PCT*100}%/笔")

    return {
        "status":                "done",
        "strategy":              strategy.name,
        "symbol":                symbol,
        "timeframe":             timeframe,
        "start_date":            start_date,
        "end_date":              end_date,
        "initial_capital":       initial_capital,
        "final_balance":         round(balance, 2),
        "roi_pct":               round(roi, 2),
        "max_drawdown_pct":      round(max_drawdown, 2),
        "win_rate_pct":          round(win_rate, 2),
        "total_trades":          total_trades,
        "winning_trades":        winning_trades,
        "losing_trades":         losing_trades,
        "total_fees_paid":       round(total_fees_paid, 2),
        "equity_curve":          equity_curve,
        "candle_count":          len(df),
        "fuse_triggered_count":  fuse_triggered_count,
        # K 线 + 交易列表（供前端 K 线图标注）
        "candles":               candles_raw,
        "trades":                trades,
        # 执行参数快照（便于结果区展示）
        "leverage":              LEVERAGE,
        "risk_pct":              RISK_PCT,
        "fee_rate":              FEE_RATE,
        "slippage":              SLIPPAGE,
        "adaptive_slippage":     slippage is None,  # True=自适应，False=固定
        # 市场状态分布（ADAPTIVE 策略特有，其他策略为空 {}）
        "regime_stats":          regime_stats,
        # 高级风险指标
        "sharpe_ratio":          sharpe_ratio,
        "sortino_ratio":         sortino_ratio,
        "calmar_ratio":          calmar_ratio,
        "profit_factor":         profit_factor,
        "avg_win_pct":           avg_win_pct,
        "avg_loss_pct":          avg_loss_pct,
    }


if __name__ == "__main__":
    result = run_backtest()
    print(f"\n资金曲线样本（前5笔）: {result.get('equity_curve', [])[:5]}")
