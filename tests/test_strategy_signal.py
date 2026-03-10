"""
tests/test_strategy_signal.py - 策略信号输出格式验证

确保所有注册策略的 generate_signal() 返回标准 dict 格式。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import pandas as pd
import numpy as np
from strategy.registry import list_strategies, get_strategy

# 生成模拟 K 线数据
def _make_fake_df(bars: int = 300):
    np.random.seed(42)
    close = 50000 + np.cumsum(np.random.randn(bars) * 100)
    high  = close + np.abs(np.random.randn(bars) * 50)
    low   = close - np.abs(np.random.randn(bars) * 50)
    open_ = close + np.random.randn(bars) * 30
    vol   = np.random.randint(100, 10000, bars).astype(float)
    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": vol,
    })
    df.index = pd.date_range("2024-01-01", periods=bars, freq="1h")
    return df


REQUIRED_KEYS = {"action", "reason", "sl", "tp1"}
VALID_ACTIONS  = {"BUY", "SELL", "HOLD"}


class TestAllStrategiesOutputFormat:
    """验证所有注册策略的信号格式"""

    @pytest.fixture(scope="class")
    def fake_df(self):
        return _make_fake_df(300)

    def test_all_strategies_return_valid_signal(self, fake_df):
        strategies = list_strategies()
        assert len(strategies) > 0, "没有任何注册策略"

        for info in strategies:
            name = info["name"]
            try:
                strat = get_strategy(name)
            except Exception as e:
                pytest.fail(f"策略 {name} 实例化失败: {e}")

            signal = strat.generate_signal(fake_df)

            # 检查返回类型
            assert isinstance(signal, dict), (
                f"策略 {name} 返回了 {type(signal)}，应为 dict"
            )

            # 检查必需 key
            for key in REQUIRED_KEYS:
                assert key in signal, (
                    f"策略 {name} 缺少 key '{key}'，实际: {list(signal.keys())}"
                )

            # 检查 action 值
            assert signal["action"] in VALID_ACTIONS, (
                f"策略 {name} 返回了无效 action='{signal['action']}'"
            )

            # sl/tp1 应为数值
            assert isinstance(signal["sl"], (int, float)), (
                f"策略 {name} 的 sl={signal['sl']} 不是数值"
            )
            assert isinstance(signal["tp1"], (int, float)), (
                f"策略 {name} 的 tp1={signal['tp1']} 不是数值"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
