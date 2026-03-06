import sys
import os
from tqdm import tqdm
import warnings

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from data.market_data import download_history_to_csv
from strategy.price_action_v2 import PriceActionV2
# ⚠️ 引入同一套风控模块
from risk.risk_manager import RiskManager

warnings.simplefilter(action='ignore', category=FutureWarning)
import pandas as pd
pd.set_option('future.no_silent_downcasting', True)

my_strategy = PriceActionV2(swing_l=8) 
my_risk_manager = RiskManager(max_trade_amount=5000) # 回测可以稍微放宽张数上限

SYMBOL = 'BTC/USDT'  
TIMEFRAME = '1h'
INITIAL_CAPITAL = 5000.0      
# ⚠️ 统一使用固定风险参数
RISK_PER_TRADE_PCT = 0.01     
LEVERAGE = 3                  
CONTRACT_SIZE = 0.01          
TAKER_FEE_RATE = 0.0005       
SLIPPAGE = 0.0002             

def run_backtest(strategy):
    df = download_history_to_csv(SYMBOL, TIMEFRAME, years=3)
    if df is None or len(df) == 0: return

    balance = INITIAL_CAPITAL     
    position_amount = 0            
    position_side = None           
    entry_price = 0.0             
    margin_used = 0.0              
    total_fees_paid = 0.0
    active_sl = 0.0               
    active_tp = 0.0               

    total_trades = winning_trades = losing_trades = 0
    max_balance = INITIAL_CAPITAL
    max_drawdown = 0.0 

    print(f"\n🚀 开始 V2 回测: 【{strategy.name}】 | 风险模型: 固额 {RISK_PER_TRADE_PCT*100}%")
    print("-" * 75)
    
    start_idx = max(51, strategy.swing_l * 2 + 10)
    
    for i in tqdm(range(start_idx, len(df)), desc="⏳ 回测计算中", unit="根K线", ncols=90):
        historical_slice = df.iloc[:i]
        current_candle = df.iloc[i]
        current_time = current_candle.name
        current_open, current_high, current_low, current_close = current_candle['open'], current_candle['high'], current_candle['low'], current_candle['close']
        
        signal = strategy.generate_signal(historical_slice)
        action, reason = signal["action"], signal["reason"]

        if balance > max_balance: max_balance = balance
        current_drawdown = (max_balance - balance) / max_balance * 100
        if current_drawdown > max_drawdown: max_drawdown = current_drawdown

        if position_amount == 0:
            if action in ["BUY", "SELL"]:
                actual_entry = current_open * (1 + SLIPPAGE) if action == "BUY" else current_open * (1 - SLIPPAGE)
                
                # ⚠️ 核心变更：调用引擎测算张数
                dynamic_trade_amount = my_risk_manager.calculate_position_size(
                    balance=balance,
                    entry_price=actual_entry,
                    sl_price=signal["sl"],
                    risk_pct=RISK_PER_TRADE_PCT,
                    contract_size=CONTRACT_SIZE,
                    fee_rate=TAKER_FEE_RATE,
                    leverage=LEVERAGE
                )

                if dynamic_trade_amount >= 1:
                    notional_value = dynamic_trade_amount * CONTRACT_SIZE * actual_entry
                    open_fee = notional_value * TAKER_FEE_RATE
                    balance -= open_fee
                    total_fees_paid += open_fee
                    
                    position_amount = dynamic_trade_amount
                    entry_price = actual_entry
                    margin_used = notional_value / LEVERAGE
                    active_sl = signal["sl"]    
                    active_tp = signal["tp1"]   
                    total_trades += 1
                    
                    position_side = 'long' if action == "BUY" else 'short'
                    emoji = "🟢" if action == "BUY" else "🔴"
                    est_loss = balance * RISK_PER_TRADE_PCT
                    tqdm.write(f"[{current_time}] {emoji} 开{position_side[:1]} | 张数: {position_amount} | 均价: {entry_price:.2f} | 止损: {active_sl:.2f} | 预定风险: {est_loss:.1f}U")

        elif position_amount > 0 and position_side == 'long':
            sell_price, sell_reason = 0, ""
            if current_low <= active_sl: sell_price, sell_reason, losing_trades = active_sl, "🔴 触发结构止损", losing_trades + 1
            elif current_high >= active_tp: sell_price, sell_reason, winning_trades = active_tp, "🎉 触发止盈", winning_trades + 1
            elif action == "SELL": sell_price, sell_reason = current_close, "🟡 反转平多"

            if sell_price > 0:
                close_notional = position_amount * CONTRACT_SIZE * sell_price
                close_fee = close_notional * TAKER_FEE_RATE
                balance -= close_fee; total_fees_paid += close_fee
                gross_profit = (sell_price - entry_price) * position_amount * CONTRACT_SIZE
                balance += gross_profit
                net_profit = gross_profit - (close_fee + margin_used * LEVERAGE * TAKER_FEE_RATE) 
                tqdm.write(f"[{current_time}] {sell_reason} | 平仓价: {sell_price:.2f} | 净盈亏: {net_profit:+.2f}U | 余额: {balance:.2f}U")
                position_amount = 0; position_side = None

        elif position_amount > 0 and position_side == 'short':
            sell_price, sell_reason = 0, ""
            if current_high >= active_sl: sell_price, sell_reason, losing_trades = active_sl, "🔴 触发结构止损", losing_trades + 1
            elif current_low <= active_tp: sell_price, sell_reason, winning_trades = active_tp, "🎉 触发止盈", winning_trades + 1
            elif action == "BUY": sell_price, sell_reason = current_close, "🟡 反转平空"

            if sell_price > 0:
                close_notional = position_amount * CONTRACT_SIZE * sell_price
                close_fee = close_notional * TAKER_FEE_RATE
                balance -= close_fee; total_fees_paid += close_fee
                gross_profit = (entry_price - sell_price) * position_amount * CONTRACT_SIZE
                balance += gross_profit
                net_profit = gross_profit - (close_fee + margin_used * LEVERAGE * TAKER_FEE_RATE)
                tqdm.write(f"[{current_time}] {sell_reason} | 平仓价: {sell_price:.2f} | 净盈亏: {net_profit:+.2f}U | 余额: {balance:.2f}U")
                position_amount = 0; position_side = None

    print("\n" + "=" * 75)
    print(f"💰 初始本金: {INITIAL_CAPITAL:.2f} | 最终金额: {balance:.2f}")
    print(f"📈 净收益率 (ROI): {((balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100):+.2f}%")
    print(f"⚠️ 最大回撤: {max_drawdown:.2f}% | 胜率: {(winning_trades / total_trades * 100) if total_trades > 0 else 0:.2f}%")

if __name__ == "__main__":
    run_backtest(my_strategy)