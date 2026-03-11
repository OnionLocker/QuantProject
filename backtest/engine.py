"""
backtest/engine.py - 离线回测引擎 V3.5

V3.5 升级内容：
  1. AUTO 模式回测：内置 MarketRegimeSelector，动态切换策略（与实盘一致）
  2. Trailing Stop：盈利达 ATR * trigger 后激活追踪止损
  3. 时间止损：持仓超 N 根 K 线强制平仓
  4. 动态仓位：根据 regime 置信度和策略胜率缩放仓位
  5. 策略切换明细记录（供前端展示每次切换的时间和原因）

调用示例：
  result = run_backtest(
      strategy    = PriceActionV2(),
      symbol      = "ETH/USDT",
      timeframe   = "1h",
      start_date  = "2023-01-01",
      end_date    = "2024-01-01",
      initial_capital = 5000.0,
  )

  # AUTO 模式回测：
  result = run_backtest(
      strategy    = "AUTO",          # 传字符串 "AUTO" 启用 selector
      symbol      = "BTC/USDT",
      timeframe   = "1h",
  )
"""
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)
import pandas as pd
pd.set_option("future.no_silent_downcasting", True)
import numpy as np

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

# V3.5: Trailing Stop 默认参数（从 config.yaml risk_v25 读取）
_v25 = _cfg.get("risk_v25", {})
DEFAULT_TRAILING_ENABLE   = _v25.get("trailing_stop_enable", True)
DEFAULT_TRAILING_TRIGGER  = _v25.get("trailing_stop_trigger", 0.5)
DEFAULT_TRAILING_DISTANCE = _v25.get("trailing_stop_distance", 0.8)
DEFAULT_TIME_STOP_ENABLE  = _v25.get("time_stop_enable", True)
DEFAULT_TIME_STOP_BARS    = _v25.get("time_stop_bars", 24)
DEFAULT_DYNAMIC_POS       = _v25.get("dynamic_position_enable", True)

# 支持回测的品种白名单
SUPPORTED_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
    "LTC/USDT", "BCH/USDT", "MATIC/USDT", "UNI/USDT", "ATOM/USDT",
]

# 支持的周期
SUPPORTED_TIMEFRAMES = ["15m", "1h", "4h", "1d"]


# ── 回测平仓逻辑统一抽取（多单/空单复用）──────────────────────────────────────
def _close_position(
    close_price, close_reason, position_side, position_amount,
    entry_price, open_fee_paid, gross, balance, total_fees_paid,
    CONTRACT_SIZE, FEE_RATE, bt_rm, fuse_triggered_count,
    trades, equity_curve, ts, silent,
):
    """
    统一执行平仓结算逻辑，返回更新后的:
    (balance, total_fees_paid, position_amount, position_side, fuse_triggered_count)
    """
    notional      = position_amount * CONTRACT_SIZE * close_price
    close_fee     = notional * FEE_RATE
    balance      -= close_fee
    total_fees_paid += close_fee
    balance      += gross
    net_pnl       = gross - (open_fee_paid + close_fee)

    # 回测内熔断模拟
    bt_rm.notify_trade_result(net_pnl, balance)
    if bt_rm.is_fused:
        fuse_triggered_count += 1
        if not silent:
            tqdm.write(f"[{ts}] 🚨 回测熔断触发（第{fuse_triggered_count}次），跳过后续开仓直至手动恢复")
        bt_rm.manual_resume()

    # 补全交易记录
    if trades and trades[-1]["exit_ts"] is None:
        t = trades[-1]
        t["exit_ts"]     = str(ts)
        t["exit_price"]  = round(close_price, 4)
        t["exit_reason"] = close_reason
        t["pnl"]         = round(net_pnl, 2)
        t["result"]      = "win" if net_pnl > 0 else "loss"

    equity_curve.append({
        "date":    str(ts)[:10],
        "balance": round(balance, 2),
        "pnl":     round(net_pnl, 2),
        "result":  "win" if net_pnl > 0 else "loss",
    })

    if not silent:
        side_label = "多" if position_side == "long" else "空"
        emoji = "🎉" if net_pnl > 0 else "🩸"
        tqdm.write(f"[{ts}] {emoji} 平{side_label} {close_reason} "
                   f"@{close_price:.2f} 净盈亏:{net_pnl:+.2f}U 余额:{balance:.2f}U")

    return (balance, total_fees_paid, 0, None, fuse_triggered_count)


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
    # ── V3.5 新增参数 ───────────────────────────────────────────────────────
    trailing_stop: bool    = None,   # 追踪止损，None = 读 config.yaml
    time_stop:     bool    = None,   # 时间止损，None = 读 config.yaml
    dynamic_pos:   bool    = None,   # 动态仓位，None = 读 config.yaml
    silent: bool           = False,
    progress_cb            = None,   # 进度回调 callable(pct: int)，每10%调用一次
) -> dict:
    """
    V3.5 回测引擎。

    :param strategy:        策略实例，或字符串 "AUTO" 启用自动选择器，为 None 时从 config.yaml 创建
    :param symbol:          交易对，如 "ETH/USDT"
    :param timeframe:       K 线周期
    :param start_date:      开始日期 "YYYY-MM-DD"
    :param end_date:        结束日期 "YYYY-MM-DD"
    :param initial_capital: 初始资金（U）
    :param leverage:        杠杆倍数（默认读 config.yaml）
    :param risk_pct:        单笔风险占本金比例（默认读 config.yaml）
    :param fee_rate:        手续费率（默认读 config.yaml）
    :param slippage:        滑点（默认 0.0002）
    :param trailing_stop:   是否启用追踪止损（默认读 config.yaml risk_v25）
    :param time_stop:       是否启用时间止损（默认读 config.yaml risk_v25）
    :param dynamic_pos:     是否启用动态仓位（默认读 config.yaml risk_v25）
    :param silent:          True 时抑制终端输出
    :return:                包含统计指标 + equity_curve + 策略切换明细 的 dict
    """
    # ── AUTO 模式检测 ─────────────────────────────────────────────────────────
    use_auto = False
    if isinstance(strategy, str) and strategy.upper() == "AUTO":
        use_auto = True
        strategy = None   # 下面会通过 selector 动态获取

    # 参数默认值
    if strategy is None and not use_auto:
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
    SLIP_BASE     = float(slippage)  if slippage  is not None else 0.0002

    # V3.5 高级风控参数
    TRAILING_ENABLE   = trailing_stop if trailing_stop is not None else DEFAULT_TRAILING_ENABLE
    TRAILING_TRIGGER  = DEFAULT_TRAILING_TRIGGER
    TRAILING_DISTANCE = DEFAULT_TRAILING_DISTANCE
    TIME_STOP_ENABLE  = time_stop    if time_stop    is not None else DEFAULT_TIME_STOP_ENABLE
    TIME_STOP_BARS    = DEFAULT_TIME_STOP_BARS
    DYNAMIC_POS       = dynamic_pos  if dynamic_pos  is not None else DEFAULT_DYNAMIC_POS

    rm = RiskManager(max_trade_amount=DEFAULT_MAX_AMT)

    # ── AUTO 模式：初始化 Selector ─────────────────────────────────────────
    selector          = None
    current_strategy  = None   # AUTO 模式下当前活跃策略
    strategy_switches = []     # 策略切换明细 [{bar_idx, time, from, to, regime, confidence}]
    per_strategy_trades = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    if use_auto:
        from strategy.selector import MarketRegimeSelector
        selector = MarketRegimeSelector(_cfg)
        # 初始策略使用 PA_5S（与实盘一致）
        current_strategy = get_strategy("PA_5S")
        if not silent:
            print("🤖 AUTO 模式：MarketRegimeSelector 已启用")
    else:
        current_strategy = strategy

    # 下载历史数据
    df = fetch_history_range(symbol, timeframe, start_date, end_date)
    if df is None or len(df) == 0:
        return {"status": "error", "error": f"无法获取 {symbol} {timeframe} 历史数据，请稍后重试"}

    # ── 对整个 df 一次性预计算指标，避免逐K线重复 rolling ───────────────────
    if hasattr(current_strategy, "precompute"):
        df = current_strategy.precompute(df)

    # ── 预计算 ATR（用于自适应滑点 + Trailing Stop）──────────────────────────
    if "atr" not in df.columns:
        _tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"]  - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = _tr.rolling(14).mean()

    # 提取市场状态分布统计（ADAPTIVE 策略特有）
    regime_stats = {}
    if hasattr(current_strategy, "regime_stats"):
        regime_stats = current_strategy.regime_stats(df)

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

    # V3.5: Trailing Stop 状态
    trailing_active     = False
    trailing_best_price = 0.0

    # V3.5: 时间止损状态
    bars_in_position = 0

    # V3.5: 当前持仓使用的策略名（用于统计 per-strategy 绩效）
    _trade_strategy_name = ""

    total_trades    = winning_trades = losing_trades = 0
    max_balance     = initial_capital
    max_drawdown    = 0.0

    equity_curve = []   # [{date, balance}] 每笔交易后记录一个点

    # ── 回测内熔断模拟（与实盘风控保持一致）────────────────────────────────
    bt_rm = RiskManager(
        max_trade_amount       = DEFAULT_MAX_AMT,
        max_consecutive_losses = rm.max_consecutive_losses,
        daily_loss_limit_pct   = rm.daily_loss_limit_pct,
    )
    bt_rm.set_daily_start_balance(initial_capital)
    fuse_triggered_count = 0

    strategy_label = "AUTO" if use_auto else current_strategy.name
    if not silent:
        features = []
        if TRAILING_ENABLE:   features.append("追踪止损")
        if TIME_STOP_ENABLE:  features.append("时间止损")
        if DYNAMIC_POS:       features.append("动态仓位")
        feat_str = "、".join(features) if features else "无"
        print(f"\n🚀 V3.5 回测: {strategy_label} | {symbol} {timeframe} | "
              f"{start_date} → {end_date} | 资金: {initial_capital} U")
        print(f"   高级功能: {feat_str}")
        print("-" * 75)

    start_idx = getattr(current_strategy, "warmup_bars", 50)
    # AUTO 模式需要更多预热（selector 的 EMA/ADX 计算）
    if use_auto:
        start_idx = max(start_idx, 150)

    # K线数不足以覆盖预热期 → 直接返回"无交易"结果
    if len(df) <= start_idx:
        if not silent:
            print(f"⚠️ K线数({len(df)})不足预热期({start_idx})，本区间无法触发交易。")
        return {
            "status":            "done",
            "strategy":          strategy_label,
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
    last_pct_report = -1

    # AUTO 模式：上一次 selector 评估时间（模拟1分钟缓存间隔，回测中每 5 根K线评估一次）
    _selector_eval_interval = 5
    _last_selector_eval     = 0
    _last_regime_conf       = 0.5   # 上次 regime 置信度

    for i in _iter:
        # 进度回调
        if progress_cb and total_steps > 0:
            pct = int((i - start_idx) / total_steps * 100)
            pct = min(pct, 99)
            if pct // 10 != last_pct_report // 10:
                last_pct_report = pct
                progress_cb(pct)

        # 熔断检查
        if bt_rm.is_fused:
            pass   # 有持仓时继续监控平仓，但不开新仓

        candle           = df.iloc[i]
        ts               = candle.name
        c_open           = candle["open"]
        c_high           = candle["high"]
        c_low            = candle["low"]
        c_close          = candle["close"]
        _atr_now         = candle["atr"] if "atr" in candle.index else 0

        # ── V3.5 AUTO 模式：每隔 N 根K线评估市场状态，按需切换策略 ─────────
        if use_auto and selector is not None:
            if (i - _last_selector_eval) >= _selector_eval_interval:
                _last_selector_eval = i
                # 只传历史窗口给 selector（回测不能看到未来数据）
                hist_window = df.iloc[:i+1]
                # 回测中不调用新闻/链上数据（无实时数据），禁用相关权重
                selector.news_weight = 0
                selector.enable_market_extra = False
                selector._tech_cache_seconds = 0   # 禁用缓存，每次都重算
                regime_result = selector.evaluate(hist_window, symbol + ":USDT")

                new_name = regime_result.get("strategy_name", "")
                _last_regime_conf = regime_result.get("confidence", 0.5)

                # WAIT 观望 → 空仓时不开仓
                if regime_result.get("regime") == "wait" and position_amount == 0:
                    pass  # 后面的信号生成会跳过

                # 策略切换
                if new_name and new_name != current_strategy.name:
                    old_name = current_strategy.name
                    try:
                        s_params = _cfg.get("selector", {}).get("strategy_params", {})
                        params = s_params.get(new_name, {})
                        new_strat = get_strategy(new_name, **params)
                        # 新策略可能需要 precompute
                        if hasattr(new_strat, "precompute") and not hasattr(df, '_precomputed_' + new_name):
                            df = new_strat.precompute(df)
                            setattr(df, '_precomputed_' + new_name, True)
                        current_strategy = new_strat
                        strategy_switches.append({
                            "bar_idx":    i,
                            "time":       str(ts),
                            "from":       old_name,
                            "to":         new_name,
                            "regime":     regime_result.get("regime", ""),
                            "confidence": round(_last_regime_conf, 3),
                            "reason":     regime_result.get("reason", ""),
                        })
                        if not silent:
                            tqdm.write(
                                f"[{ts}] 🔄 策略切换: {old_name} → {new_name} "
                                f"(regime={regime_result['regime']}, conf={_last_regime_conf:.2f})"
                            )
                    except Exception as e:
                        if not silent:
                            tqdm.write(f"[{ts}] ⚠️ 策略切换失败: {new_name} → {e}")

        # 信号生成
        if hasattr(current_strategy, "signal_from_row"):
            signal = current_strategy.signal_from_row(df, i)
        else:
            SIGNAL_WINDOW = max(200, getattr(current_strategy, "swing_l", 8) * 6 + 50)
            win_start = max(0, i - SIGNAL_WINDOW)
            signal = current_strategy.generate_signal(df.iloc[win_start:i])

        action, reason = signal["action"], signal["reason"]

        # AUTO + WAIT 状态：空仓时跳过开仓信号
        if use_auto and selector is not None:
            if (getattr(selector, '_confirmed_regime', '') == 'wait'
                    and position_amount == 0 and action in ("BUY", "SELL")):
                action = "HOLD"
                reason = "WAIT 观望跳过"

        # 回撤追踪
        if balance > max_balance:
            max_balance = balance
        dd = (max_balance - balance) / max_balance * 100
        if dd > max_drawdown:
            max_drawdown = dd

        # ── 空仓开仓 ────────────────────────────────────────────────────────
        if position_amount == 0 and action in ("BUY", "SELL") and not bt_rm.is_fused:
            _slip      = _adaptive_slippage(_atr_now, c_open, SLIP_BASE) if slippage is None else SLIP_BASE
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

            # V3.5: 动态仓位 - 根据 regime 置信度缩放
            if DYNAMIC_POS and use_auto and _last_regime_conf < 0.7:
                conf_scale = max(0.4, _last_regime_conf / 0.7)
                contracts = max(1, int(contracts * conf_scale))
                if not silent:
                    tqdm.write(
                        f"[{ts}] 📊 动态仓位: conf={_last_regime_conf:.2f} "
                        f"scale={conf_scale:.2f} → {contracts}张"
                    )

            # V3.5: 策略绩效降权（AUTO 模式下，近期胜率低的策略减仓）
            if DYNAMIC_POS and use_auto:
                s_stats = per_strategy_trades.get(current_strategy.name, {})
                s_total = s_stats.get("wins", 0) + s_stats.get("losses", 0)
                if s_total >= 5:
                    s_wr = s_stats["wins"] / s_total
                    if s_wr < 0.35:
                        contracts = max(1, int(contracts * 0.6))

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

            # V3.5: 重置 trailing / 时间止损状态
            trailing_active     = False
            trailing_best_price = 0.0
            bars_in_position    = 0
            _trade_strategy_name = current_strategy.name

            # 记录开仓信息
            trades.append({
                "entry_ts":    str(ts),
                "entry_price": round(actual_entry, 4),
                "side":        position_side,
                "sl":          round(active_sl, 4),
                "tp":          round(active_tp, 4),
                "contracts":   contracts,
                "strategy":    current_strategy.name,   # V3.5: 记录使用的策略
                "exit_ts":     None,
                "exit_price":  None,
                "exit_reason": None,
                "pnl":         None,
                "result":      None,
            })

            if not silent:
                emoji = "🟢" if action == "BUY" else "🔴"
                strat_tag = f" [{current_strategy.name}]" if use_auto else ""
                tqdm.write(f"[{ts}] {emoji} 开{position_side}{strat_tag} "
                           f"| {contracts}张 @{actual_entry:.2f} SL:{active_sl:.2f}")

        # ── 持仓中：V3.5 高级风控 ───────────────────────────────────────────
        elif position_amount > 0:
            is_long = position_side == "long"
            bars_in_position += 1

            # ── V3.5: Trailing Stop ─────────────────────────────────────────
            if TRAILING_ENABLE and _atr_now > 0:
                if is_long:
                    profit_pts = c_close - entry_price
                else:
                    profit_pts = entry_price - c_close
                trigger_level = _atr_now * TRAILING_TRIGGER

                if profit_pts >= trigger_level:
                    if not trailing_active:
                        trailing_active = True
                        trailing_best_price = c_close
                        if not silent:
                            tqdm.write(
                                f"[{ts}] ✅ Trailing Stop 激活 "
                                f"(盈利={profit_pts:.2f} > 触发={trigger_level:.2f})"
                            )
                    # 更新最优价
                    if is_long and c_close > trailing_best_price:
                        trailing_best_price = c_close
                    elif not is_long and c_close < trailing_best_price:
                        trailing_best_price = c_close

                    # 计算新的追踪止损价
                    trail_dist = _atr_now * TRAILING_DISTANCE
                    if is_long:
                        new_trail_sl = trailing_best_price - trail_dist
                        if new_trail_sl > active_sl:
                            active_sl = new_trail_sl
                    else:
                        new_trail_sl = trailing_best_price + trail_dist
                        if new_trail_sl < active_sl or active_sl == 0:
                            active_sl = new_trail_sl

            # ── 平仓检测（多单）──────────────────────────────────────────────
            close_price, close_reason = 0.0, ""
            _close_slip = _adaptive_slippage(_atr_now, c_open, SLIP_BASE) if slippage is None else SLIP_BASE

            if is_long:
                sl_hit = c_low  <= active_sl
                tp_hit = c_high >= active_tp

                if sl_hit and tp_hit:
                    dist_sl = entry_price - active_sl
                    dist_tp = active_tp  - entry_price
                    if dist_tp <= dist_sl:
                        close_price = active_tp * (1 + _close_slip)
                        close_reason = "止盈" + (" (trailing)" if trailing_active else "")
                        winning_trades += 1
                    else:
                        close_price = active_sl * (1 - _close_slip)
                        close_reason = "追踪止损" if trailing_active else "止损"
                        losing_trades += 1
                elif sl_hit:
                    close_price = active_sl * (1 - _close_slip)
                    close_reason = "追踪止损" if trailing_active else "止损"
                    losing_trades += 1
                elif tp_hit:
                    close_price = active_tp * (1 + _close_slip)
                    close_reason = "止盈"
                    winning_trades += 1
                elif action == "SELL":
                    close_price = c_close * (1 - _close_slip)
                    close_reason = "反转平多"

            # ── 平仓检测（空单）──────────────────────────────────────────────
            else:
                sl_hit = c_high >= active_sl
                tp_hit = c_low  <= active_tp

                if sl_hit and tp_hit:
                    dist_sl = active_sl  - entry_price
                    dist_tp = entry_price - active_tp
                    if dist_tp <= dist_sl:
                        close_price = active_tp * (1 - _close_slip)
                        close_reason = "止盈" + (" (trailing)" if trailing_active else "")
                        winning_trades += 1
                    else:
                        close_price = active_sl * (1 + _close_slip)
                        close_reason = "追踪止损" if trailing_active else "止损"
                        losing_trades += 1
                elif sl_hit:
                    close_price = active_sl * (1 + _close_slip)
                    close_reason = "追踪止损" if trailing_active else "止损"
                    losing_trades += 1
                elif tp_hit:
                    close_price = active_tp * (1 - _close_slip)
                    close_reason = "止盈"
                    winning_trades += 1
                elif action == "BUY":
                    close_price = c_close * (1 + _close_slip)
                    close_reason = "反转平空"

            # ── V3.5: 时间止损 ──────────────────────────────────────────────
            if TIME_STOP_ENABLE and close_price == 0 and bars_in_position >= TIME_STOP_BARS:
                if is_long and c_close >= entry_price:
                    close_price = c_close * (1 - _close_slip)
                    close_reason = f"时间止损({bars_in_position}K线)"
                elif not is_long and c_close <= entry_price:
                    close_price = c_close * (1 + _close_slip)
                    close_reason = f"时间止损({bars_in_position}K线)"
                elif bars_in_position >= int(TIME_STOP_BARS * 1.5):
                    # 超时 1.5 倍，无论盈亏强制平仓
                    slip_dir = -1 if is_long else 1
                    close_price = c_close * (1 + slip_dir * _close_slip)
                    close_reason = f"强制时间止损({bars_in_position}K线)"

            # ── 执行平仓 ────────────────────────────────────────────────────
            if close_price > 0:
                if is_long:
                    gross = (close_price - entry_price) * position_amount * CONTRACT_SIZE
                else:
                    gross = (entry_price - close_price) * position_amount * CONTRACT_SIZE

                (balance, total_fees_paid, position_amount, position_side,
                 fuse_triggered_count) = _close_position(
                    close_price=close_price, close_reason=close_reason,
                    position_side="long" if is_long else "short",
                    position_amount=position_amount,
                    entry_price=entry_price, open_fee_paid=open_fee_paid,
                    gross=gross, balance=balance, total_fees_paid=total_fees_paid,
                    CONTRACT_SIZE=CONTRACT_SIZE, FEE_RATE=FEE_RATE,
                    bt_rm=bt_rm, fuse_triggered_count=fuse_triggered_count,
                    trades=trades, equity_curve=equity_curve, ts=ts, silent=silent,
                )

                # V3.5: 统计 per-strategy 绩效
                net_pnl = trades[-1].get("pnl", 0) if trades else 0
                if _trade_strategy_name:
                    ps = per_strategy_trades[_trade_strategy_name]
                    if net_pnl and net_pnl > 0:
                        ps["wins"] += 1
                    elif net_pnl and net_pnl < 0:
                        ps["losses"] += 1
                    ps["pnl"] += (net_pnl or 0)

                # 重置 trailing / 时间止损状态
                trailing_active     = False
                trailing_best_price = 0.0
                bars_in_position    = 0

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
        trade_times = set()
        for t in trades:
            if t.get("entry_ts"):
                trade_times.add(int(pd.Timestamp(t["entry_ts"]).timestamp()))
            if t.get("exit_ts"):
                trade_times.add(int(pd.Timestamp(t["exit_ts"]).timestamp()))
        # 策略切换点也保留
        for sw in strategy_switches:
            if sw.get("time"):
                try:
                    trade_times.add(int(pd.Timestamp(sw["time"]).timestamp()))
                except Exception:
                    pass

        step = len(candles_raw) // MAX_CANDLES
        sampled = []
        for idx, c in enumerate(candles_raw):
            if idx % step == 0 or c["time"] in trade_times:
                sampled.append(c)
        candles_raw = sampled

    # 回测完成后推送 100% 进度
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
        if use_auto:
            print(f"🔄 策略切换次数: {len(strategy_switches)}")
            for sn, stats in per_strategy_trades.items():
                s_total = stats["wins"] + stats["losses"]
                s_wr = stats["wins"] / s_total * 100 if s_total > 0 else 0
                print(f"   {sn}: {s_total}笔 胜率={s_wr:.1f}% 盈亏={stats['pnl']:+.2f}U")
        print(f"[参数] 品种={symbol} 周期={timeframe} 杠杆={LEVERAGE}x 风险={RISK_PCT*100}%/笔")

    # ── V3.5: per-strategy 统计汇总 ─────────────────────────────────────────
    per_strategy_summary = {}
    for sn, stats in per_strategy_trades.items():
        s_total = stats["wins"] + stats["losses"]
        per_strategy_summary[sn] = {
            "total_trades": s_total,
            "wins":         stats["wins"],
            "losses":       stats["losses"],
            "win_rate_pct": round(stats["wins"] / s_total * 100, 2) if s_total > 0 else 0,
            "total_pnl":    round(stats["pnl"], 2),
        }

    return {
        "status":                "done",
        "strategy":              strategy_label,
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
        # 执行参数快照
        "leverage":              LEVERAGE,
        "risk_pct":              RISK_PCT,
        "fee_rate":              FEE_RATE,
        "slippage":              SLIP_BASE,
        "adaptive_slippage":     slippage is None,
        # 市场状态分布（ADAPTIVE 策略特有）
        "regime_stats":          regime_stats,
        # 高级风险指标
        "sharpe_ratio":          sharpe_ratio,
        "sortino_ratio":         sortino_ratio,
        "calmar_ratio":          calmar_ratio,
        "profit_factor":         profit_factor,
        "avg_win_pct":           avg_win_pct,
        "avg_loss_pct":          avg_loss_pct,
        # ── V3.5 新增 ──────────────────────────────────────────────────────
        "is_auto_mode":          use_auto,
        "strategy_switches":     strategy_switches,      # 策略切换明细列表
        "per_strategy_stats":    per_strategy_summary,    # 各策略分别统计
        "features": {
            "trailing_stop":  TRAILING_ENABLE,
            "time_stop":      TIME_STOP_ENABLE,
            "dynamic_pos":    DYNAMIC_POS,
        },
    }


if __name__ == "__main__":
    result = run_backtest()
    print(f"\n资金曲线样本（前5笔）: {result.get('equity_curve', [])[:5]}")

    # V3.5: AUTO 模式测试
    # result_auto = run_backtest(strategy="AUTO", symbol="BTC/USDT", timeframe="1h")
    # print(f"AUTO 模式切换次数: {len(result_auto.get('strategy_switches', []))}")
