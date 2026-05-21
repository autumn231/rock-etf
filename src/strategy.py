"""
纯策略逻辑 —— 与 UI、数据源完全解耦。

generate_signal() 是唯一入口，输入市场指标，输出 SignalResult。
可在回测、测试、UI 中复用。
"""

from dataclasses import dataclass


@dataclass
class SignalResult:
    """策略决策结果"""

    signal_type: str       # "error" | "warning" | "success" | "info"  → Streamlit 颜色
    title: str             # 标题（带 emoji）
    instruction: str       # 人类可读的执行指令
    action: str            # "buy_stock" | "sell_stock" | "hold" | "intercept"
    target_stock_pct: float | None  # 目标股票仓位（%），None = 维持不变


# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------


@dataclass
class StrategyConfig:
    """策略阈值参数，所有值均为百分比（0-100）"""
    buy_extreme: float = 10.0    # PE ≤ 此值 → 极寒买入（目标 30% 仓位）
    buy_cheap: float = 30.0      # PE ≤ 此值 → 低估买入（目标 20% 仓位）
    sell_warn: float = 50.0      # PE > 此值 → 开始考虑卖出
    sell_panic: float = 70.0     # PE > 此值 → 清仓逃顶（目标 10% 仓位）
    pb_confirm_warn: float = 40.0  # PE>sell_warn + PB>此值 → 减仓
    pb_confirm_panic: float = 50.0 # PE>sell_panic + PB>此值 → 清仓


# ---------------------------------------------------------------------------
# 决策树
# ---------------------------------------------------------------------------


def generate_signal(
    pe_q: float,
    pb_q: float,
    above_ma60: bool,
    config: StrategyConfig | None = None,
) -> SignalResult:
    """根据 PE/PB 分位和均线状态生成交易信号。

    Parameters
    ----------
    pe_q : float
        PE 历史分位（0-100），5 年滚动窗口
    pb_q : float
        PB 历史分位（0-100），5 年滚动窗口
    above_ma60 : bool
        沪深 300 收盘价是否在 60 日均线上方
    config : StrategyConfig | None
        策略阈值参数，None 则使用默认值

    Returns
    -------
    SignalResult
    """
    if config is None:
        config = StrategyConfig()

    bx = config.buy_extreme
    bc = config.buy_cheap
    sw = config.sell_warn
    sp = config.sell_panic
    pw = config.pb_confirm_warn
    pp = config.pb_confirm_panic

    # 自动纠正反转：确保警戒线 ≤ 清仓线、买入阈值递增
    if bx > bc:
        bx, bc = bc, bx
    if sw > sp:
        sw, sp = sp, sw
    if pw > pp:
        pw, pp = pp, pw

    # ── 1. 左侧买入区 ──
    if pe_q <= bx:
        return SignalResult(
            signal_type="error",
            title="🚨 【极寒买入】",
            instruction=(
                f"PE 跌破 {bx:.0f}% 分位！\n\n"
                "👉 执行：若股票未满 30%，同比例卖出城投和国债，加仓股票至 30% 满仓。"
            ),
            action="buy_stock",
            target_stock_pct=30.0,
        )

    if pe_q <= bc:
        return SignalResult(
            signal_type="warning",
            title="⚠️ 【低估买入】",
            instruction=(
                f"PE 跌破 {bc:.0f}% 分位！\n\n"
                "👉 执行：若股票未达 20%，同比例卖出城投和国债，加仓股票至 20%。"
            ),
            action="buy_stock",
            target_stock_pct=20.0,
        )

    # ── 2. 右侧卖出与趋势跟随区 ──

    # 2a. PE > 清仓线 且跌破均线 → 清仓逃顶 or 拦截
    if pe_q > sp and not above_ma60:
        if pb_q > pp:
            return SignalResult(
                signal_type="error",
                title="🛑 【清仓逃顶】",
                instruction=(
                    f"真泡沫破裂！PB > {pp:.0f}% 确认高估。\n\n"
                    "👉 执行：若股票 > 10%，清仓至 10% 底仓，余钱买债（城投国债各半）。"
                ),
                action="sell_stock",
                target_stock_pct=10.0,
            )
        else:
            return SignalResult(
                signal_type="success",
                title="🛡️ 【系统拦截】",
                instruction=(
                    f"利润坍塌『假高估』陷阱！PE 虚高但 PB ≤ {pp:.0f}%。\n\n"
                    "👉 纪律：无视跌破均线，拒绝割肉，继续持仓。"
                ),
                action="intercept",
                target_stock_pct=None,
            )

    # 2b. 卖出警戒线 < PE ≤ 清仓线 且跌破均线 → 减仓防守 or 拦截
    if pe_q > sw and not above_ma60:
        if pb_q > pw:
            return SignalResult(
                signal_type="warning",
                title="📉 【减仓防守】",
                instruction=(
                    f"PE 进入高位且跌破均线，PB > {pw:.0f}% 确认高估。\n\n"
                    "👉 执行：若股票 > 20%，减仓至 20%，余钱买债。"
                ),
                action="sell_stock",
                target_stock_pct=20.0,
            )
        else:
            return SignalResult(
                signal_type="success",
                title="🛡️ 【系统拦截】",
                instruction=(
                    f"遭遇『假高估』陷阱！PE 虚高但 PB ≤ {pw:.0f}%。\n\n"
                    "👉 纪律：无视均线，继续持仓。"
                ),
                action="intercept",
                target_stock_pct=None,
            )

    # 2c. PE > 卖出警戒线 但趋势完好 → 享受泡沫
    if pe_q > sw and above_ma60:
        return SignalResult(
            signal_type="success",
            title="🔥 【享受泡沫】",
            instruction=(
                "估值偏高但趋势完好！沪深 300 仍站于 60 日均线之上。\n\n"
                "👉 纪律：死死拿住股票，让利润狂奔！"
            ),
            action="hold",
            target_stock_pct=None,
        )

    # ── 3. 常态持仓区（bc < PE ≤ sw） ──
    return SignalResult(
        signal_type="info",
        title="☕ 【静待时机】",
        instruction=(
            f"PE 在 {bc:.0f}%-{sw:.0f}% 之间，未触发任何阈值。\n\n"
            "👉 纪律：维持当前仓位，安心装死。"
        ),
        action="hold",
        target_stock_pct=None,
    )
