"""
strategy/regime_detector.py - 技术面市场状态检测器

判断逻辑（三重确认）：
  1. ADX > threshold        → 有方向性趋势
  2. EMA快线斜率方向         → 价格趋势方向
  3. 收盘价 vs EMA慢线位置   → 价格所处结构位置

市场状态：
  'bull'    - 牛市：ADX高 + EMA向上 + 收盘在EMA慢线之上
  'bear'    - 熊市：ADX高 + EMA向下 + 收盘在EMA慢线之下
  'ranging' - 震荡：ADX低，方向不明

确认机制：
  连续 confirm_bars 根K线处于同一状态才切换，避免频繁误判
"""
import numpy as np
import pandas as pd


# ── 状态标签常量 ───────────────────────────────────────────────────────────────
BULL    = 'bull'
RANGING = 'ranging'
BEAR    = 'bear'


def calc_adx(H: np.ndarray, L: np.ndarray, C: np.ndarray,
             period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    计算 ADX / +DI / -DI（Wilder 平滑法，与 TradingView 一致）。
    返回 (adx, plus_di, minus_di) 三个 numpy 数组。
    """
    n     = len(C)
    alpha = 1.0 / period

    # True Range
    prev_c    = np.empty(n);  prev_c[0] = C[0];  prev_c[1:] = C[:-1]
    tr        = np.maximum.reduce([H - L, np.abs(H - prev_c), np.abs(L - prev_c)])

    # Directional Movement
    up_move   = np.empty(n);  up_move[0]   = 0.0;  up_move[1:]   = np.diff(H)
    down_move = np.empty(n);  down_move[0] = 0.0;  down_move[1:] = -np.diff(L)
    plus_dm   = np.where((up_move > down_move) & (up_move > 0),   up_move,   0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder 平滑（1/period 衰减）
    def _wilder(arr):
        out    = np.empty(n)
        out[0] = arr[0]
        for i in range(1, n):
            out[i] = out[i - 1] * (1 - alpha) + arr[i] * alpha
        return out

    atr_s     = _wilder(tr)
    safe_atr  = np.where(atr_s > 1e-10, atr_s, 1e-10)
    plus_di   = 100.0 * _wilder(plus_dm)  / safe_atr
    minus_di  = 100.0 * _wilder(minus_dm) / safe_atr

    di_sum   = plus_di + minus_di
    safe_sum = np.where(di_sum > 1e-10, di_sum, 1e-10)
    dx       = 100.0 * np.abs(plus_di - minus_di) / safe_sum
    adx      = _wilder(dx)

    return adx, plus_di, minus_di


def calc_ema(arr: np.ndarray, span: int) -> np.ndarray:
    """Pandas-compatible EWM（adjust=False）以 numpy 实现。"""
    alpha  = 2.0 / (span + 1)
    out    = np.empty(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = out[i - 1] * (1 - alpha) + arr[i] * alpha
    return out


def confirm_regime(raw: np.ndarray, n: int) -> np.ndarray:
    """
    确认期过滤：连续 n 根K线都处于同一状态，才真正切换。
    避免短暂噪音引起的频繁换仓。
    """
    if len(raw) == 0:
        return raw.copy()

    result  = raw.copy()
    current = raw[0]
    pending = raw[0]
    streak  = 1

    for i in range(1, len(raw)):
        if raw[i] == pending:
            streak += 1
        else:
            pending = raw[i]
            streak  = 1
        if streak >= n:
            current = pending
        result[i] = current

    return result


class RegimeDetector:
    """
    技术面市场状态检测器。

    参数：
        adx_period    : ADX 计算周期（默认 14）
        ema_fast      : 快速 EMA 周期，用于计算斜率（默认 20）
        ema_slow      : 慢速 EMA 周期，用于判断价格结构位置（默认 50）
        slope_window  : 计算 EMA 斜率的回看根数（默认 5）
        adx_threshold : ADX 判定为"有趋势"的阈值（默认 25）
        confirm_bars  : 状态切换所需的连续确认K线数（默认 5）
    """

    def __init__(
        self,
        adx_period:    int   = 14,
        ema_fast:      int   = 20,
        ema_slow:      int   = 50,
        slope_window:  int   = 5,
        adx_threshold: float = 25.0,
        confirm_bars:  int   = 5,
    ):
        self.adx_period    = adx_period
        self.ema_fast      = ema_fast
        self.ema_slow      = ema_slow
        self.slope_window  = slope_window
        self.adx_threshold = adx_threshold
        self.confirm_bars  = confirm_bars
        # 预热K线数（ADX + 慢线 + 确认期）
        self.warmup_bars   = ema_slow + adx_period + confirm_bars + 5

    def compute(self, df: pd.DataFrame) -> np.ndarray:
        """
        对整个 DataFrame 计算每根K线的市场状态。

        返回 numpy 字符串数组，长度 = len(df)，
        值为 'bull' | 'ranging' | 'bear'。
        """
        H = df['high'].values
        L = df['low'].values
        C = df['close'].values
        n = len(C)

        adx_arr, _, _ = calc_adx(H, L, C, self.adx_period)
        ema_fast_arr  = calc_ema(C, self.ema_fast)
        ema_slow_arr  = calc_ema(C, self.ema_slow)

        # EMA 斜率：快线在 slope_window 根内的变化量
        slope = np.zeros(n)
        for i in range(self.slope_window, n):
            slope[i] = ema_fast_arr[i] - ema_fast_arr[i - self.slope_window]

        # 逐根判断原始状态
        raw = np.full(n, RANGING, dtype=object)
        for i in range(n):
            if np.isnan(adx_arr[i]):
                continue
            trending  = adx_arr[i] > self.adx_threshold
            bull_dir  = slope[i] > 0 and C[i] > ema_slow_arr[i]
            bear_dir  = slope[i] < 0 and C[i] < ema_slow_arr[i]

            if trending and bull_dir:
                raw[i] = BULL
            elif trending and bear_dir:
                raw[i] = BEAR
            # else: RANGING (default)

        # 应用确认期过滤
        return confirm_regime(raw, self.confirm_bars)

    def compute_series(self, df: pd.DataFrame) -> pd.Series:
        """同 compute()，返回 pd.Series，索引与 df 一致（便于调试）。"""
        return pd.Series(self.compute(df), index=df.index, name='regime')
