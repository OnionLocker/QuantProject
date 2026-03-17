"""
tests/conftest.py - 全局 pytest 夹具

提供：
  - 项目根目录 sys.path 注入
  - 通用 K 线生成工具
  - 临时 SQLite 数据库
  - FastAPI TestClient
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Any, Dict

import numpy as np
import pandas as pd
import pytest

# ── 项目路径注入 ──────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ══════════════════════════════════════════════════════════════════════════════
# K 线生成工具
# ══════════════════════════════════════════════════════════════════════════════

def make_ohlcv(
    bars: int = 300,
    base_price: float = 50000.0,
    trend: float = 0.0,
    volatility: float = 100.0,
    seed: int = 42,
) -> pd.DataFrame:
    """
    生成模拟 OHLCV 数据。

    Args:
        bars: K 线数量
        base_price: 起始价格
        trend: 每根 K 线的均值漂移（正=上涨趋势，负=下跌趋势）
        volatility: 价格波动标准差
        seed: 随机种子
    """
    rng = np.random.RandomState(seed)
    close = base_price + np.cumsum(rng.randn(bars) * volatility + trend)
    # 确保价格为正
    close = np.maximum(close, base_price * 0.5)
    high = close + np.abs(rng.randn(bars) * volatility * 0.5)
    low = close - np.abs(rng.randn(bars) * volatility * 0.5)
    open_ = close + rng.randn(bars) * volatility * 0.3
    volume = rng.randint(100, 10000, bars).astype(float)

    df = pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
    df.index = pd.date_range("2024-01-01", periods=bars, freq="1h")
    return df


def make_bullish_df(bars: int = 300) -> pd.DataFrame:
    """生成强烈上涨趋势的 K 线"""
    return make_ohlcv(bars=bars, trend=50.0, volatility=80.0, seed=10)


def make_bearish_df(bars: int = 300) -> pd.DataFrame:
    """生成强烈下跌趋势的 K 线"""
    return make_ohlcv(bars=bars, trend=-50.0, volatility=80.0, seed=20)


def make_ranging_df(bars: int = 300) -> pd.DataFrame:
    """生成震荡行情的 K 线（零趋势，低波动）"""
    return make_ohlcv(bars=bars, trend=0.0, volatility=30.0, seed=30)


def make_pin_bar_long(base: float = 50000.0) -> pd.DataFrame:
    """
    构造一组包含 Pin Bar 探底反转（做多信号 S1L）的 K 线。
    倒数第 2 根为 Pin Bar：长下影线、小实体、在 EMA 上方。
    """
    bars = 100
    # 先构造上涨趋势（使 EMA 向上、价格在 EMA 上方）
    rng = np.random.RandomState(100)
    prices = base + np.cumsum(np.ones(bars) * 20 + rng.randn(bars) * 10)
    prices = np.maximum(prices, base * 0.8)

    opens = prices.copy()
    closes = prices.copy()
    highs = prices + np.abs(rng.randn(bars) * 20)
    lows = prices - np.abs(rng.randn(bars) * 20)
    volume = np.full(bars, 5000.0)

    # 构造信号 K 线（倒数第 2 根，index -2）
    j = bars - 2
    # Pin Bar：长下影线 > 66% 总长，小实体 < 33%，上影线很短
    pin_low = prices[j] - 500  # 长下影线
    pin_high = prices[j] + 20  # 很短的上影线
    pin_open = prices[j] - 5
    pin_close = prices[j] + 10  # 收在高位

    opens[j] = pin_open
    closes[j] = pin_close
    highs[j] = pin_high
    lows[j] = pin_low

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volume,
    })
    df.index = pd.date_range("2024-01-01", periods=bars, freq="1h")
    return df


def make_engulfing_bullish(base: float = 50000.0) -> pd.DataFrame:
    """
    构造一组包含看涨吞没形态（做多信号 S4L）的 K 线。
    """
    bars = 100
    rng = np.random.RandomState(200)
    prices = base + np.cumsum(np.ones(bars) * 15 + rng.randn(bars) * 10)
    prices = np.maximum(prices, base * 0.8)

    opens = prices.copy()
    closes = prices.copy()
    highs = prices + np.abs(rng.randn(bars) * 15)
    lows = prices - np.abs(rng.randn(bars) * 15)
    volume = np.full(bars, 5000.0)

    # 构造看涨吞没：j-1 为阴线，j 为阳线且完全吞没前一根
    j = bars - 2
    m = j - 1

    # 前一根阴线（母线）
    opens[m] = prices[m] + 100
    closes[m] = prices[m] - 100
    highs[m] = opens[m] + 10
    lows[m] = closes[m] - 10

    # 当前阳线（子线，完全吞没母线）：实体很大（动量过滤）
    opens[j] = closes[m] - 10  # 开盘低于母线收盘
    closes[j] = opens[m] + 50  # 收盘高于母线开盘
    highs[j] = closes[j] + 10
    lows[j] = opens[j] - 10

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volume,
    })
    df.index = pd.date_range("2024-01-01", periods=bars, freq="1h")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 数据库夹具
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """
    创建临时 SQLite 数据库，并将 db_handler 指向该临时文件。
    测试结束后自动清理。
    """
    db_path = str(tmp_path / "test_trading.db")
    # 注入到 db_handler 的 DB_PATH
    import execution.db_handler as dbh
    monkeypatch.setattr(dbh, "DB_PATH", db_path)
    # 清除线程缓存连接
    if hasattr(dbh._thread_local, 'conn'):
        dbh._thread_local.conn = None
    # 初始化表结构
    dbh.init_db()
    yield db_path
    # 清理
    dbh.close_thread_conn()


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI TestClient 夹具
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def app_client(tmp_db):
    """
    提供一个已初始化数据库的 FastAPI TestClient。
    """
    from fastapi.testclient import TestClient
    from api.server import app
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth_headers(app_client) -> Dict[str, str]:
    """
    注册一个测试用户并返回带 JWT 的认证头。
    """
    resp = app_client.post("/api/auth/register", json={
        "username": "testuser",
        "password": "testpass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
