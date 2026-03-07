"""
strategy/registry.py - 策略注册表

新增策略步骤：
  1. 在 _REGISTRY 中追加 "NAME": ClassName
  2. 重启服务即生效，前端策略下拉框自动出现
"""
from strategy.base import BaseStrategy
from strategy.pa_setups import PriceActionSetups

_REGISTRY: dict[str, type[BaseStrategy]] = {
    "PA_5S": PriceActionSetups,
}


def register(name: str, cls: type[BaseStrategy]):
    _REGISTRY[name] = cls


def get_strategy(name: str, **params) -> BaseStrategy:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"策略 '{name}' 未注册。已注册列表: {list(_REGISTRY.keys())}")
    return cls(**params)


def list_strategies() -> list[dict]:
    """返回所有已注册策略的名称、类名和可调参数元数据。"""
    return [
        {
            "name":   k,
            "class":  v.__name__,
            "params": getattr(v, "PARAMS", []),
        }
        for k, v in _REGISTRY.items()
    ]


def get_strategy_params(name: str) -> list[dict]:
    cls = _REGISTRY.get(name)
    if cls is None:
        return []
    return getattr(cls, "PARAMS", [])
