import math

class RiskManager:
    def __init__(self, max_trade_amount=1000, is_trading_allowed=True):
        self.max_trade_amount = max_trade_amount
        self.is_trading_allowed = is_trading_allowed

    def check_order(self, symbol: str, side: str, amount: int) -> bool:
        """基础的拦截风控"""
        if not self.is_trading_allowed:
            print("❌ 风控拦截：系统当前禁止交易。")
            return False
        if amount > self.max_trade_amount:
            print(f"❌ 风控拦截：单笔下单数量 ({amount}) 超过全局硬性上限 ({self.max_trade_amount})。")
            return False
        if amount <= 0:
            print("❌ 风控拦截：下单数量不能小于等于 0。")
            return False
        return True

    def calculate_position_size(self, balance: float, entry_price: float, sl_price: float, 
                                risk_pct: float, contract_size: float, fee_rate: float, leverage: float) -> int:
        """
        核心资金管理：固定风险头寸计算 (Fixed Fractional Sizing)
        无论止损多宽多窄，保证这一单打损后，账户总资金刚好只亏掉 risk_pct
        """
        if entry_price <= 0 or sl_price <= 0 or balance <= 0 or entry_price == sl_price:
            return 0

        # 1. 本次允许的最大亏损金额 (U)
        max_loss_allowed = balance * risk_pct

        # 2. 如果开 1 张合约，触发止损时的纯价格亏损金额 (U)
        price_risk_per_contract = abs(entry_price - sl_price) * contract_size

        # 3. 1 张合约的双边手续费预估 (按 Taker 算开平仓最坏情况)
        open_fee_per_contract = entry_price * contract_size * fee_rate
        close_fee_per_contract = sl_price * contract_size * fee_rate
        total_fee_per_contract = open_fee_per_contract + close_fee_per_contract

        # 4. 单张合约的真实总风险 (加上了摩擦成本)
        total_risk_per_contract = price_risk_per_contract + total_fee_per_contract

        if total_risk_per_contract <= 0:
            return 0

        # 5. 反推可以开多少张 (向下取整，宁少勿多)
        target_contracts = int(math.floor(max_loss_allowed / total_risk_per_contract))
        
        # 兜底最大张数限制
        target_contracts = min(target_contracts, self.max_trade_amount)

        # 6. 杠杆可用资金校验：即使风险允许，账户里也要有足够的钱交保证金
        while target_contracts > 0:
            notional = target_contracts * contract_size * entry_price
            margin_required = notional / leverage
            fee_required = notional * fee_rate
            
            # 如果保证金+手续费足够，校验通过
            if (margin_required + fee_required) <= balance:
                break
            # 如果钱不够，减少一张继续试
            target_contracts -= 1

        return target_contracts