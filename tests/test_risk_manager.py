"""
tests/test_risk_manager.py - RiskManager 关键路径测试

覆盖：
  - 连亏熔断触发与恢复
  - 日亏熔断
  - check_order 金额校验
  - calculate_position_size 边界情况
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from risk.risk_manager import RiskManager


class TestConsecutiveLossFuse:
    """连续亏损熔断测试"""

    def test_fuse_after_max_losses(self):
        rm = RiskManager(max_consecutive_losses=3)
        rm.set_daily_start_balance(10000)

        rm.notify_trade_result(-10, 9990)
        assert rm.is_fused is False
        rm.notify_trade_result(-10, 9980)
        assert rm.is_fused is False
        rm.notify_trade_result(-10, 9970)
        assert rm.is_fused is True
        assert rm.consecutive_losses == 3

    def test_win_resets_counter(self):
        rm = RiskManager(max_consecutive_losses=3)
        rm.set_daily_start_balance(10000)

        rm.notify_trade_result(-10, 9990)
        rm.notify_trade_result(-10, 9980)
        # 盈利一次应重置计数
        rm.notify_trade_result(50, 10030)
        assert rm.consecutive_losses == 0
        assert rm.is_fused is False

    def test_manual_resume(self):
        rm = RiskManager(max_consecutive_losses=1)
        rm.set_daily_start_balance(10000)
        rm.notify_trade_result(-10, 9990)
        assert rm.is_fused is True

        rm.manual_resume()
        assert rm.is_fused is False
        assert rm.consecutive_losses == 0


class TestDailyLossFuse:
    """日亏熔断测试"""

    def test_daily_loss_triggers_fuse(self):
        rm = RiskManager(daily_loss_limit_pct=0.05, max_consecutive_losses=999)
        rm.set_daily_start_balance(10000)

        # 亏损 6%（超过 5% 阈值）
        rm.notify_trade_result(-600, 9400)
        assert rm.is_fused is True

    def test_daily_loss_under_limit(self):
        rm = RiskManager(daily_loss_limit_pct=0.05, max_consecutive_losses=999)
        rm.set_daily_start_balance(10000)

        # 亏损 3%（未超过）
        rm.notify_trade_result(-300, 9700)
        assert rm.is_fused is False

    def test_reset_daily(self):
        rm = RiskManager(daily_loss_limit_pct=0.05, max_consecutive_losses=999)
        rm.set_daily_start_balance(10000)
        rm.notify_trade_result(-600, 9400)
        assert rm.is_fused is True

        rm.reset_daily(9400)
        # reset_daily 不会自动恢复 is_trading_allowed，需要 manual_resume
        # 但会重置 daily_loss_triggered
        assert rm._daily_loss_triggered is False


class TestCheckOrder:
    """check_order 校验测试"""

    def test_rejects_when_fused(self):
        rm = RiskManager()
        rm.is_trading_allowed = False
        assert rm.check_order("BTC/USDT", "buy", 10) is False

    def test_rejects_zero_amount(self):
        rm = RiskManager()
        assert rm.check_order("BTC/USDT", "buy", 0) is False
        assert rm.check_order("BTC/USDT", "buy", -1) is False

    def test_rejects_over_max_trade_amount(self):
        rm = RiskManager(max_trade_amount=1000)
        # 传入名义金额 1500 USDT 超过上限
        assert rm.check_order("BTC/USDT", "buy", 50, notional_usdt=1500) is False

    def test_accepts_within_limit(self):
        rm = RiskManager(max_trade_amount=1000)
        assert rm.check_order("BTC/USDT", "buy", 50, notional_usdt=800) is True

    def test_accepts_without_notional(self):
        """不传 notional_usdt 时只检查基本条件"""
        rm = RiskManager(max_trade_amount=100)
        assert rm.check_order("BTC/USDT", "buy", 999) is True


class TestCalculatePositionSize:
    """仓位计算边界测试"""

    def test_basic_calculation(self):
        rm = RiskManager(max_trade_amount=5000)
        contracts = rm.calculate_position_size(
            balance=10000, entry_price=50000, sl_price=49000,
            risk_pct=0.01, contract_size=0.01, fee_rate=0.0005, leverage=3
        )
        assert contracts > 0
        assert isinstance(contracts, int)

    def test_zero_balance(self):
        rm = RiskManager()
        assert rm.calculate_position_size(
            balance=0, entry_price=50000, sl_price=49000,
            risk_pct=0.01, contract_size=0.01, fee_rate=0.0005, leverage=3
        ) == 0

    def test_same_entry_and_sl(self):
        rm = RiskManager()
        assert rm.calculate_position_size(
            balance=10000, entry_price=50000, sl_price=50000,
            risk_pct=0.01, contract_size=0.01, fee_rate=0.0005, leverage=3
        ) == 0

    def test_negative_price(self):
        rm = RiskManager()
        assert rm.calculate_position_size(
            balance=10000, entry_price=-1, sl_price=49000,
            risk_pct=0.01, contract_size=0.01, fee_rate=0.0005, leverage=3
        ) == 0

    def test_max_trade_amount_cap(self):
        """max_trade_amount 应当限制合约张数"""
        rm = RiskManager(max_trade_amount=5)
        contracts = rm.calculate_position_size(
            balance=1000000, entry_price=50000, sl_price=49000,
            risk_pct=0.5, contract_size=0.01, fee_rate=0.0005, leverage=10
        )
        assert contracts <= 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
