import math
import logging

logger = logging.getLogger("risk_manager")


class RiskManager:
    def __init__(self, max_trade_amount=1000, is_trading_allowed=True,
                 max_consecutive_losses=3, daily_loss_limit_pct=0.05):
        self.max_trade_amount       = max_trade_amount
        self.is_trading_allowed     = is_trading_allowed
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_loss_limit_pct   = daily_loss_limit_pct

        self._consecutive_losses    = 0
        self._daily_start_balance   = None
        self._daily_loss_triggered  = False

    def notify_trade_result(self, pnl: float, current_balance: float):
        """每次平仓后调用，传入净盈亏和当前余额。"""
        if self._daily_start_balance is None:
            self._daily_start_balance = (
                current_balance + abs(pnl) if pnl < 0 else current_balance
            )

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        if self._consecutive_losses >= self.max_consecutive_losses:
            self.is_trading_allowed = False
            logger.warning(
                "🚨 [风控熔断] 连续亏损 %d 次，已自动停止交易！",
                self._consecutive_losses,
            )

        if self._daily_start_balance and self._daily_start_balance > 0:
            daily_loss = (
                (self._daily_start_balance - current_balance) / self._daily_start_balance
            )
            if daily_loss >= self.daily_loss_limit_pct:
                self.is_trading_allowed   = False
                self._daily_loss_triggered = True
                logger.warning(
                    "🚨 [风控熔断] 当日亏损达 %.1f%%，超过上限 %.1f%%，已自动停止交易！",
                    daily_loss * 100,
                    self.daily_loss_limit_pct * 100,
                )

    def reset_daily(self, new_balance: float = None):
        """每日开始时调用，重置日内状态（连亏不重置，跨日继续累计）。"""
        self._daily_start_balance  = new_balance
        self._daily_loss_triggered = False
        if new_balance:
            logger.info("📅 [风控] 日内状态已重置，起始余额: %.2f U", new_balance)
        else:
            logger.info("📅 [风控] 日内状态已重置（余额待确认）")

    def set_daily_start_balance(self, balance: float):
        """
        弱点修复：Bot 启动时主动设置当日起始余额，
        而不是等第一笔亏损才初始化，避免基准偏低。
        """
        if self._daily_start_balance is None:
            self._daily_start_balance = balance
            logger.info("📅 [风控] 当日起始余额已初始化：%.2f U", balance)

    def manual_resume(self):
        """人工确认后恢复交易（熔断后手动调用）。"""
        self.is_trading_allowed    = True
        self._consecutive_losses   = 0
        self._daily_loss_triggered = False
        logger.info("✅ [风控] 已手动恢复交易。")

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def is_fused(self) -> bool:
        return not self.is_trading_allowed

    def check_order(self, symbol: str, side: str, amount: int) -> bool:
        if not self.is_trading_allowed:
            logger.warning("❌ 风控拦截：系统当前禁止交易（连亏熔断或日亏熔断）。")
            return False
        if amount > self.max_trade_amount:
            logger.warning(
                "❌ 风控拦截：单笔下单数量 (%d) 超过硬性上限 (%d)。",
                amount, self.max_trade_amount,
            )
            return False
        if amount <= 0:
            logger.warning("❌ 风控拦截：下单数量不能 ≤ 0。")
            return False
        return True

    def calculate_position_size(
        self, balance: float, entry_price: float, sl_price: float,
        risk_pct: float, contract_size: float, fee_rate: float, leverage: float
    ) -> int:
        """固定风险头寸计算 (Fixed Fractional Sizing)"""
        if entry_price <= 0 or sl_price <= 0 or balance <= 0 or entry_price == sl_price:
            return 0

        max_loss_allowed         = balance * risk_pct
        price_risk_per_contract  = abs(entry_price - sl_price) * contract_size
        open_fee_per_contract    = entry_price * contract_size * fee_rate
        close_fee_per_contract   = sl_price    * contract_size * fee_rate
        total_risk_per_contract  = (
            price_risk_per_contract + open_fee_per_contract + close_fee_per_contract
        )
        if total_risk_per_contract <= 0:
            return 0

        target_contracts = int(math.floor(max_loss_allowed / total_risk_per_contract))
        target_contracts = min(target_contracts, self.max_trade_amount)

        while target_contracts > 0:
            notional       = target_contracts * contract_size * entry_price
            margin_required = notional / leverage
            fee_required    = notional * fee_rate
            if (margin_required + fee_required) <= balance:
                break
            target_contracts -= 1

        return target_contracts
