import math

class RiskManager:
    def __init__(self, max_trade_amount=1000, is_trading_allowed=True,
                 max_consecutive_losses=3, daily_loss_limit_pct=0.05):
        """
        :param max_trade_amount:      单笔最大开仓张数（硬性上限）
        :param is_trading_allowed:    全局交易开关（外部可直接设为 False 熔断）
        :param max_consecutive_losses: 连续亏损 N 次后自动熔断
        :param daily_loss_limit_pct:  单日最大亏损比例（如 0.05 = 亏掉本金 5% 即停）
        """
        self.max_trade_amount = max_trade_amount
        self.is_trading_allowed = is_trading_allowed
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_loss_limit_pct = daily_loss_limit_pct

        # 内部状态
        self._consecutive_losses = 0       # 当前连续亏损次数
        self._daily_start_balance = None   # 当日起始余额（第一次喂入时记录）
        self._daily_loss_triggered = False # 当日亏损熔断是否已触发

    # ── 外部主动通知接口 ─────────────────────────────────────────────────────

    def notify_trade_result(self, pnl: float, current_balance: float):
        """
        每次平仓后主动调用，喂入本次净盈亏和当前余额。
        :param pnl:              本次净盈亏 (U)，亏损为负数
        :param current_balance:  平仓后账户可用余额
        """
        # 初始化当日起始余额
        if self._daily_start_balance is None:
            self._daily_start_balance = current_balance + abs(pnl) if pnl < 0 else current_balance

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0  # 盈利则重置连亏计数

        # 检查连亏熔断
        if self._consecutive_losses >= self.max_consecutive_losses:
            self.is_trading_allowed = False
            print(f"🚨 [风控熔断] 连续亏损 {self._consecutive_losses} 次，已自动停止交易！")

        # 检查当日亏损上限
        if self._daily_start_balance and self._daily_start_balance > 0:
            daily_loss = (self._daily_start_balance - current_balance) / self._daily_start_balance
            if daily_loss >= self.daily_loss_limit_pct:
                self.is_trading_allowed = False
                self._daily_loss_triggered = True
                print(f"🚨 [风控熔断] 当日亏损达 {daily_loss*100:.1f}%，超过上限 "
                      f"{self.daily_loss_limit_pct*100:.1f}%，已自动停止交易！")

    def reset_daily(self, new_balance: float = None):
        """每日开始时调用，重置日内状态（连亏不重置，跨日继续累计）"""
        self._daily_start_balance = new_balance  # None 表示等待第一笔交易时再初始化
        self._daily_loss_triggered = False
        if new_balance:
            print(f"📅 [风控] 日内状态已重置，起始余额: {new_balance:.2f} U")
        else:
            print("📅 [风控] 日内状态已重置（余额待确认）")

    def manual_resume(self):
        """人工确认后恢复交易（熔断后需手动调用）"""
        self.is_trading_allowed = True
        self._consecutive_losses = 0
        self._daily_loss_triggered = False
        print("✅ [风控] 已手动恢复交易。")

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def is_fused(self) -> bool:
        """当前是否处于熔断状态"""
        return not self.is_trading_allowed

    # ── 原有核心逻辑（不动）────────────────────────────────────────────────

    def check_order(self, symbol: str, side: str, amount: int) -> bool:
        """基础的拦截风控"""
        if not self.is_trading_allowed:
            print(f"❌ 风控拦截：系统当前禁止交易（连亏熔断或日亏熔断）。")
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

        max_loss_allowed = balance * risk_pct
        price_risk_per_contract = abs(entry_price - sl_price) * contract_size
        open_fee_per_contract = entry_price * contract_size * fee_rate
        close_fee_per_contract = sl_price * contract_size * fee_rate
        total_fee_per_contract = open_fee_per_contract + close_fee_per_contract
        total_risk_per_contract = price_risk_per_contract + total_fee_per_contract

        if total_risk_per_contract <= 0:
            return 0

        target_contracts = int(math.floor(max_loss_allowed / total_risk_per_contract))
        target_contracts = min(target_contracts, self.max_trade_amount)

        while target_contracts > 0:
            notional = target_contracts * contract_size * entry_price
            margin_required = notional / leverage
            fee_required = notional * fee_rate
            if (margin_required + fee_required) <= balance:
                break
            target_contracts -= 1

        return target_contracts
