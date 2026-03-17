"""
risk/risk_manager.py - 机构级风控管理器 V4.0

V4.0 升级：
  - Equity Curve Trading: 策略资金曲线跌破 EMA 时自动降仓
  - 回撤保护: 实时监控回撤，分级触发（警告/减仓/熔断）
  - Regime 感知: 高风险 regime 自动降仓（如刚从 BULL 切到 BEAR）
  - 动态风险预算: 根据连续盈亏动态调整 risk_pct
  - 每日最大交易次数限制
  - 持仓时间超限保护
"""
import math
import logging
from collections import deque

logger = logging.getLogger("risk_manager")


class RiskManager:
    # ── 默认参数常量 ──────────────────────────────────────────────────────
    DEFAULT_DRAWDOWN_WARNING_PCT:  float = 0.03   # 3% 回撤警告
    DEFAULT_DRAWDOWN_REDUCE_PCT:   float = 0.05   # 5% 回撤减仓
    DEFAULT_DRAWDOWN_HALT_PCT:     float = 0.08   # 8% 回撤暂停
    DEFAULT_EQUITY_EMA_PERIOD:     int   = 10     # Equity Curve EMA 周期
    DEFAULT_EQUITY_HISTORY_LEN:    int   = 50     # 保留最近 N 笔交易余额
    DEFAULT_EQUITY_MIN_SAMPLES:    int   = 5      # 至少 N 笔交易才启用 EMA 降权
    EQUITY_BELOW_EMA_SCALE:        float = 0.6    # 资金曲线低于均线时降至 60%
    DEFAULT_BASE_RISK_PCT:         float = 0.01   # 基础风险比例 1%
    DEFAULT_RECENT_PNLS_LEN:       int   = 20     # 最近 N 笔盈亏
    DEFAULT_MAX_DAILY_TRADES:      int   = 8      # 每日最多交易次数
    MIN_EFFECTIVE_RISK_PCT:        float = 0.001  # 最低有效风险比例 0.1%
    KELLY_MIN_MULT:                float = 0.5    # Kelly 最低仓位乘数
    KELLY_MAX_MULT:                float = 1.5    # Kelly 最高仓位乘数
    KELLY_MIN_SAMPLES:             int   = 3      # 最少 N 笔交易才启用动态调节
    LOSS_PENALTY_PER_LOSS:         float = 0.15   # 每次连续亏损的惩罚系数
    LOSS_PENALTY_MIN:              float = 0.4    # 连亏惩罚最低值
    LOSS_PENALTY_TRIGGER:          int   = 2      # 连亏 N 次开始惩罚

    def __init__(self, max_trade_amount=1000, is_trading_allowed=True,
                 max_consecutive_losses=3, daily_loss_limit_pct=0.05):
        self.max_trade_amount       = max_trade_amount
        self.is_trading_allowed     = is_trading_allowed
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_loss_limit_pct   = daily_loss_limit_pct

        self._consecutive_losses    = 0
        self._daily_start_balance   = None
        self._daily_loss_triggered  = False

        # ── V4.0: 回撤保护 ────────────────────────────────────────────────
        self._peak_balance:        float = 0.0
        self._drawdown_warning_pct: float = self.DEFAULT_DRAWDOWN_WARNING_PCT
        self._drawdown_reduce_pct:  float = self.DEFAULT_DRAWDOWN_REDUCE_PCT
        self._drawdown_halt_pct:    float = self.DEFAULT_DRAWDOWN_HALT_PCT
        self._drawdown_level:      str   = "normal"

        # ── V4.0: Equity Curve Trading ────────────────────────────────────
        self._equity_history: deque = deque(maxlen=self.DEFAULT_EQUITY_HISTORY_LEN)
        self._equity_ema_period:  int   = self.DEFAULT_EQUITY_EMA_PERIOD
        self._equity_ema:         float = 0.0
        self._equity_below_ema:   bool  = False

        # ── V4.0: Regime 感知仓位调节 ─────────────────────────────────────
        self._regime_risk_mult:   float = 1.0
        self._regime_name:        str   = ""

        # ── V4.0: 动态风险预算 ────────────────────────────────────────────
        self._base_risk_pct:      float = self.DEFAULT_BASE_RISK_PCT
        self._dynamic_risk_pct:   float = self.DEFAULT_BASE_RISK_PCT
        self._recent_pnls: deque = deque(maxlen=self.DEFAULT_RECENT_PNLS_LEN)

        # ── V4.0: 每日交易次数限制 ────────────────────────────────────────
        self._daily_trade_count:  int = 0
        self._max_daily_trades:   int = self.DEFAULT_MAX_DAILY_TRADES

    # ── V4.0: 回撤保护 ────────────────────────────────────────────────────

    def update_drawdown(self, current_balance: float):
        """每次余额变化时调用，检测回撤等级。"""
        if current_balance <= 0:
            return

        # 更新历史最高
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance

        if self._peak_balance <= 0:
            return

        drawdown = (self._peak_balance - current_balance) / self._peak_balance

        old_level = self._drawdown_level
        if drawdown >= self._drawdown_halt_pct:
            self._drawdown_level = "halted"
            if old_level != "halted":
                self.is_trading_allowed = False
                logger.warning(
                    f"🚨 [回撤熔断] 回撤 {drawdown*100:.1f}% ≥ {self._drawdown_halt_pct*100:.0f}%，"
                    f"已暂停交易！峰值={self._peak_balance:.2f}, 当前={current_balance:.2f}"
                )
        elif drawdown >= self._drawdown_reduce_pct:
            self._drawdown_level = "reduced"
            if old_level not in ("reduced", "halted"):
                logger.warning(
                    f"⚠️ [回撤减仓] 回撤 {drawdown*100:.1f}% ≥ {self._drawdown_reduce_pct*100:.0f}%，"
                    f"自动降低仓位至 50%"
                )
        elif drawdown >= self._drawdown_warning_pct:
            self._drawdown_level = "warning"
            if old_level == "normal":
                logger.info(
                    f"⚡ [回撤警告] 回撤 {drawdown*100:.1f}% ≥ {self._drawdown_warning_pct*100:.0f}%"
                )
        else:
            self._drawdown_level = "normal"

    @property
    def drawdown_scale(self) -> float:
        """返回回撤对应的仓位缩放系数 [0, 1]。"""
        if self._drawdown_level == "halted":
            return 0.0
        elif self._drawdown_level == "reduced":
            return 0.5
        elif self._drawdown_level == "warning":
            return 0.75
        return 1.0

    # ── V4.0: Equity Curve Trading ────────────────────────────────────────

    def update_equity_curve(self, balance: float):
        """每次平仓后更新资金曲线 EMA。"""
        self._equity_history.append(balance)
        if len(self._equity_history) < 3:
            self._equity_ema = balance
            return

        # 计算 EMA
        alpha = 2.0 / (self._equity_ema_period + 1)
        if self._equity_ema == 0:
            self._equity_ema = balance
        else:
            self._equity_ema = self._equity_ema * (1 - alpha) + balance * alpha

        self._equity_below_ema = balance < self._equity_ema
        if self._equity_below_ema:
            logger.info(
                f"📉 [Equity Curve] 余额 {balance:.2f} < EMA {self._equity_ema:.2f}，策略降权"
            )

    @property
    def equity_curve_scale(self) -> float:
        """Equity Curve Trading 仓位缩放系数。"""
        if self._equity_below_ema and len(self._equity_history) >= self.DEFAULT_EQUITY_MIN_SAMPLES:
            return self.EQUITY_BELOW_EMA_SCALE
        return 1.0

    # ── V4.0: Regime 感知仓位调节 ─────────────────────────────────────────

    def set_regime_context(self, regime: str, confidence: float = 1.0,
                           transition_action: str = None):
        """
        由 runner.py 在每轮循环中调用，更新 regime 上下文。
        高风险场景（如 BULL→BEAR 刚切换）自动降仓。
        """
        self._regime_name = regime

        if transition_action in ("close_long", "close_short"):
            # 刚发生方向性 regime 切换，风险最高
            self._regime_risk_mult = 0.5
        elif regime == "wait":
            self._regime_risk_mult = 0.0  # WAIT 不开仓
        elif confidence < 0.4:
            self._regime_risk_mult = 0.6
        elif confidence < 0.7:
            self._regime_risk_mult = 0.8
        else:
            self._regime_risk_mult = 1.0

    @property
    def regime_scale(self) -> float:
        return self._regime_risk_mult

    # ── V4.0: 动态风险预算 ────────────────────────────────────────────────

    def _update_dynamic_risk(self):
        """
        根据近期盈亏动态调整 risk_pct。
        连续盈利 → 适度加仓（Kelly 精神）
        连续亏损 → 大幅降仓（保护本金优先）
        """
        if len(self._recent_pnls) < self.KELLY_MIN_SAMPLES:
            self._dynamic_risk_pct = self._base_risk_pct
            return

        pnls = list(self._recent_pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls)

        # 简化 Kelly: risk_mult = max(0.5, min(1.5, 2*WR))
        # WR=0.3 → 0.6x, WR=0.5 → 1.0x, WR=0.7 → 1.4x
        kelly_mult = max(self.KELLY_MIN_MULT, min(self.KELLY_MAX_MULT, win_rate * 2.0))

        # 连续亏损额外惩罚
        if self._consecutive_losses >= self.LOSS_PENALTY_TRIGGER:
            loss_penalty = max(
                self.LOSS_PENALTY_MIN,
                1.0 - self._consecutive_losses * self.LOSS_PENALTY_PER_LOSS,
            )
            kelly_mult *= loss_penalty

        self._dynamic_risk_pct = self._base_risk_pct * kelly_mult

    def get_effective_risk_pct(self, base_risk_pct: float = None) -> float:
        """返回综合所有因素后的有效风险比例。"""
        if base_risk_pct:
            self._base_risk_pct = base_risk_pct
        self._update_dynamic_risk()

        effective = self._dynamic_risk_pct
        effective *= self.drawdown_scale
        effective *= self.equity_curve_scale
        effective *= self.regime_scale
        return max(self.MIN_EFFECTIVE_RISK_PCT, effective)

    # ── 原有方法保持兼容 ─────────────────────────────────────────────────────

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

        # V4.0: 更新盈亏历史
        self._recent_pnls.append(pnl)
        self._daily_trade_count += 1

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

        # V4.0: 更新回撤监控和资金曲线
        self.update_drawdown(current_balance)
        self.update_equity_curve(current_balance)

    def reset_daily(self, new_balance: float = None):
        """每日开始时调用，重置日内状态（连亏不重置，跨日继续累计）。"""
        self._daily_start_balance  = new_balance
        self._daily_loss_triggered = False
        self._daily_trade_count    = 0  # V4.0: 重置日内交易次数

        # V4.0: 更新峰值余额
        if new_balance and new_balance > self._peak_balance:
            self._peak_balance = new_balance

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
        # V4.0: 初始化峰值
        if balance > self._peak_balance:
            self._peak_balance = balance

    def manual_resume(self):
        """人工确认后恢复交易（熔断后手动调用）。"""
        self.is_trading_allowed    = True
        self._consecutive_losses   = 0
        self._daily_loss_triggered = False
        self._drawdown_level       = "normal"  # V4.0: 重置回撤等级
        logger.info("✅ [风控] 已手动恢复交易。")

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def is_fused(self) -> bool:
        return not self.is_trading_allowed

    # V4.0: 日内交易次数检查
    @property
    def daily_trades_exhausted(self) -> bool:
        return self._daily_trade_count >= self._max_daily_trades

    def check_order(self, symbol: str, side: str, amount: int,
                    notional_usdt: float = None) -> bool:
        """
        风控前置检查。V4.0 新增：回撤检查 + 日内交易次数检查。
        """
        if not self.is_trading_allowed:
            logger.warning("❌ 风控拦截：系统当前禁止交易（连亏熔断或日亏熔断）。")
            return False
        if self._drawdown_level == "halted":
            logger.warning("❌ 风控拦截：回撤超限，已暂停交易。")
            return False
        if self.daily_trades_exhausted:
            logger.warning(
                f"❌ 风控拦截：日内已交易 {self._daily_trade_count} 次，"
                f"达上限 {self._max_daily_trades} 次。"
            )
            return False
        if notional_usdt is not None and notional_usdt > self.max_trade_amount:
            logger.warning(
                "❌ 风控拦截：单笔金额 (%.2f USDT) 超过硬性上限 (%d USDT)。",
                notional_usdt, self.max_trade_amount,
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
        """固定风险头寸计算 (Fixed Fractional Sizing)，V4.0 集成动态风险因子。"""
        if entry_price <= 0 or sl_price <= 0 or balance <= 0 or entry_price == sl_price:
            return 0

        # V4.0: 使用动态风险比例
        effective_risk = self.get_effective_risk_pct(risk_pct)

        max_loss_allowed         = balance * effective_risk
        price_risk_per_contract  = abs(entry_price - sl_price) * contract_size
        open_fee_per_contract    = entry_price * contract_size * fee_rate
        close_fee_per_contract   = sl_price    * contract_size * fee_rate
        total_risk_per_contract  = (
            price_risk_per_contract + open_fee_per_contract + close_fee_per_contract
        )
        if total_risk_per_contract <= 0:
            return 0

        target_contracts = int(math.floor(max_loss_allowed / total_risk_per_contract))

        # Fix: max_trade_amount 是 USDT 金额上限，需换算为张数上限再比较
        notional_per_contract = entry_price * contract_size
        if notional_per_contract > 0:
            max_contracts_by_amount = int(math.floor(
                self.max_trade_amount / (notional_per_contract / leverage)
            ))
            target_contracts = min(target_contracts, max_contracts_by_amount)

        while target_contracts > 0:
            notional       = target_contracts * contract_size * entry_price
            margin_required = notional / leverage
            fee_required    = notional * fee_rate
            if (margin_required + fee_required) <= balance:
                break
            target_contracts -= 1

        return target_contracts

    # ── V4.0: 风控状态摘要（供前端展示）─────────────────────────────────────

    def get_status_summary(self) -> dict:
        """返回风控状态摘要，供 API/前端展示。"""
        return {
            "is_trading_allowed": self.is_trading_allowed,
            "consecutive_losses": self._consecutive_losses,
            "drawdown_level":     self._drawdown_level,
            "drawdown_scale":     self.drawdown_scale,
            "equity_curve_scale": self.equity_curve_scale,
            "regime_scale":       self.regime_scale,
            "dynamic_risk_pct":   round(self._dynamic_risk_pct * 100, 3),
            "daily_trade_count":  self._daily_trade_count,
            "max_daily_trades":   self._max_daily_trades,
            "peak_balance":       round(self._peak_balance, 2),
            "equity_below_ema":   self._equity_below_ema,
        }
