class BaseStrategy:
    def __init__(self, name="基础策略"):
        self.name = name

    def generate_signal(self, df):
        """
        所有的具体策略都必须实现这个功能。
        必须返回一个字典格式：
        {
            "action": "BUY" | "SELL" | "HOLD",
            "entry": float,      # 建议入场价
            "sl": float,         # 止损价
            "tp1": float,        # 第一止盈价
            "tp2": float,        # 第二止盈价
            "risk_r": float,     # 每1R的风险金额 (entry - sl 的绝对值)
            "reason": str,       # 触发理由
            "meta": dict         # 附加信息(供调试用)
        }
        """
        raise NotImplementedError("你必须在具体的策略里写明判断规则！")