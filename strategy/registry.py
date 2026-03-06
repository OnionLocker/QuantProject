"""
strategy/registry.py - 策略注册表

用法：
  1. 在此文件 _REGISTRY 中注册你的策略类
  2. main.py / engine.py 通过 get_strategy(name, **params) 拿到策略实例
  3. 切换策略只需改 config.yaml，无需动 main.py
"""
from strategy.base import BaseStrategy
from strategy.price_action_v2 import PriceActionV2

# ── 注册表：name -> 类 ───────────────────────────────────────────────────────
_REGISTRY: dict[str, type[BaseStrategy]] = {
    "PA_V2": PriceActionV2,
    # 未来在这里追加，例如：
    # "RSI_MACD": RsiMacdStrategy,
    # "BB_SQUEEZE": BollingerSqueezeStrategy,
}


def register(name: str, cls: type[BaseStrategy]):
    """动态注册新策略（在策略文件末尾调用）"""
    _REGISTRY[name] = cls


def get_strategy(name: str, **params) -> BaseStrategy:
    """
    按名称实例化策略。
    :param name:   注册名，如 "PA_V2"
    :param params: 策略超参数，透传给 __init__
    :raises KeyError: 策略名不存在时
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"策略 '{name}' 未注册。已注册列表: {list(_REGISTRY.keys())}")
    return cls(**params)


def list_strategies() -> list[dict]:
    """返回所有已注册策略的名称和类名"""
    return [{"name": k, "class": v.__name__} for k, v in _REGISTRY.items()]
