"""
持仓计算 —— 根据当前持仓和策略信号，算出具体调仓金额。

用户输入四大类资产的当前市值，系统输出每类资产该买/卖多少。
"""

from dataclasses import dataclass
from .strategy import SignalResult


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Holdings:
    """用户当前持仓（市值，元）"""
    stock_value: float = 0.0
    gold_value: float = 0.0
    chengduo_bond_value: float = 0.0
    govt_bond_value: float = 0.0

    @property
    def total_value(self) -> float:
        return self.stock_value + self.gold_value + self.chengduo_bond_value + self.govt_bond_value

    @property
    def stock_pct(self) -> float:
        t = self.total_value
        return 0.0 if t == 0 else self.stock_value / t * 100


@dataclass
class TradeInstruction:
    """调仓指令 —— 各资产应买卖金额（正=买入，负=卖出）"""
    stock_delta: float
    gold_delta: float
    chengduo_bond_delta: float
    govt_bond_delta: float
    total_value: float
    current_stock_pct: float
    target_stock_pct: float | None  # None = 维持不变
    has_action: bool = False        # True 表示需要实际调仓


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------


def calculate_trades(holdings: Holdings, signal: SignalResult) -> TradeInstruction:
    """根据持仓和信号计算调仓金额。

    Parameters
    ----------
    holdings : Holdings
        用户当前各资产市值
    signal : SignalResult
        策略信号

    Returns
    -------
    TradeInstruction
        每个资产应买卖的金额
    """
    total = holdings.total_value
    if total == 0:
        return TradeInstruction(
            stock_delta=0.0,
            gold_delta=0.0,
            chengduo_bond_delta=0.0,
            govt_bond_delta=0.0,
            total_value=0.0,
            current_stock_pct=0.0,
            target_stock_pct=signal.target_stock_pct,
            has_action=False,
        )

    current_stock_pct = holdings.stock_pct

    # 无目标仓位 → hold / intercept → 不动
    if signal.target_stock_pct is None:
        return TradeInstruction(
            stock_delta=0.0,
            gold_delta=0.0,
            chengduo_bond_delta=0.0,
            govt_bond_delta=0.0,
            total_value=total,
            current_stock_pct=round(current_stock_pct, 1),
            target_stock_pct=None,
            has_action=False,
        )

    target_pct = signal.target_stock_pct

    # 目标值
    target_stock_value = total * target_pct / 100.0
    target_gold_value = total * 0.10  # 黄金永远 10%
    remaining = total - target_stock_value - target_gold_value
    target_chengduo = remaining / 2.0
    target_govt = remaining / 2.0

    # 差值（正=买入，负=卖出）
    sd = round(target_stock_value - holdings.stock_value, 2)
    gd = round(target_gold_value - holdings.gold_value, 2)
    cd = round(target_chengduo - holdings.chengduo_bond_value, 2)
    gbd = round(target_govt - holdings.govt_bond_value, 2)

    # 判断是否真的有操作（金额 > 1 元算有动作）
    has = any(abs(d) > 1.0 for d in (sd, gd, cd, gbd))

    return TradeInstruction(
        stock_delta=sd,
        gold_delta=gd,
        chengduo_bond_delta=cd,
        govt_bond_delta=gbd,
        total_value=total,
        current_stock_pct=round(current_stock_pct, 1),
        target_stock_pct=target_pct,
        has_action=has,
    )
