import pandas as pd


class BaseStrategy:
    """
    所有策略的基类。子类必须实现 generate_signal()，
    可选实现 precompute() + signal_from_row() 以获得回测高性能路径。

    PARAMS 类变量：声明策略可调参数的元数据，供前端动态渲染表单。
    格式：
    [
        {
            "key":     "param_name",    # 与 __init__ 参数名一致
            "label":   "显示名称",
            "type":    "int" | "float", # 数据类型
            "default": <默认值>,
            "min":     <最小值>,
            "max":     <最大值>,
            "step":    <步长>,
            "tip":     "参数说明（可选）",
        },
        ...
    ]
    """

    # 子类覆盖此变量以声明可调参数
    PARAMS: list[dict] = []

    def __init__(self, name: str = "基础策略"):
        self.name = name
        # 引擎预热所需的最少 K 线数（子类可覆盖）
        self.warmup_bars: int = 50

    def generate_signal(self, df: pd.DataFrame) -> dict:
        """
        【必须实现】实盘 / 兼容接口：传入历史切片，返回当前信号。

        返回格式：
        {
            "action":  "BUY" | "SELL" | "HOLD",
            "entry":   float,
            "sl":      float,
            "tp1":     float,
            "tp2":     float,
            "risk_r":  float,
            "reason":  str,
            "meta":    dict,
        }
        """
        raise NotImplementedError("必须在具体策略里实现 generate_signal()")

    # ── 可选高性能接口（回测加速用）─────────────────────────────────────────────

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        【可选】对整个回测 df 一次性向量化计算所有指标和信号列。
        不实现时引擎跳过，走兼容路径。
        """
        return df

    def signal_from_row(self, df: pd.DataFrame, i: int) -> dict:
        """
        【可选】从预计算好的 df 中读取第 i 行的信号（O(1)）。
        不实现时引擎自动调用 generate_signal()。
        """
        raise NotImplementedError
