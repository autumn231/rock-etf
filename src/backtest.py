"""
回测引擎 —— 用历史数据验证磐石策略的绩效。

数据来源
-------
- 沪深 300 日线：已缓存 (csi300_daily.csv) 或实时抓取
- PE/PB 历史：已缓存 (pe_series.csv, pb_series.csv)
- 黄金：AKShare 实时抓取黄金 ETF (518880)
- 债券：AKShare 实时抓取国债指数 (sh000012)

回测逻辑
--------
- 逐日模拟，仅在有信号触发时才再平衡
- 半年期强制再平衡（6/30、12/31）
- 无需未来数据，每个决策点仅用当时已发生的数据
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from datetime import datetime, date

import akshare as ak

from .data import load_cached_history
from .strategy import generate_signal, StrategyConfig

# ------------------------------------------------------------------
# 结果模型
# ------------------------------------------------------------------


@dataclass
class BacktestSummary:
    """回测绩效汇总"""
    total_return: float          # 总收益率 %
    annual_return: float         # 年化收益率 %
    benchmark_return: float      # 基准总收益率 %
    benchmark_annual_return: float  # 基准年化 %
    max_drawdown: float          # 最大回撤 %
    benchmark_max_drawdown: float
    sharpe: float                # 夏普比率（无风险利率 2.5%）
    benchmark_sharpe: float
    win_rate: float              # 日胜率（跑赢基准的交易日占比）
    num_trades: int              # 交易次数
    final_value: float           # 最终净值
    benchmark_final: float       # 基准最终净值
    start_date: str
    end_date: str
    years: float                 # 回测年数


@dataclass
class BacktestSeries:
    """回测时序数据（供图表用）"""
    dates: list[str]
    portfolio_value: list[float]
    benchmark_value: list[float]
    stock_weight: list[float]
    signal_actions: list[str]


# ------------------------------------------------------------------
# 数据加载
# ------------------------------------------------------------------


def _align_datecol(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """统一日期列名为 'date' 并转为 datetime"""
    if df is None or df.empty:
        return df
    date_cols = ["date", "日期", "Date"]
    for c in date_cols:
        if c in df.columns:
            df = df.rename(columns={c: "date"})
            break
    df["date"] = pd.to_datetime(df["date"])
    return df


def _load_all_data() -> dict:
    """加载所有历史数据，返回 dict of DataFrame，键为 'csi300','pe','pb','gold','bond'"""
    data = {}

    # 1. CSI 300 日线（优先缓存，无则抓取）
    csi = load_cached_history("csi300")
    if csi is None:
        csi = ak.stock_zh_index_daily(symbol="sh000300")
    data["csi300"] = _align_datecol(csi, "csi300")

    # 2. PE / PB
    for key in ("pe", "pb"):
        df = load_cached_history(key)
        if df is None:
            fn = ak.stock_index_pe_lg if key == "pe" else ak.stock_index_pb_lg
            df = fn(symbol="沪深300")
        data[key] = _align_datecol(df, key)

    # 3. 黄金 ETF (518880)
    try:
        gold = ak.fund_etf_hist_em(
            symbol="518880", period="daily",
            start_date="20000101", end_date=datetime.now().strftime("%Y%m%d"),
            adjust=""
        )
        data["gold"] = _align_datecol(gold, "gold")
    except Exception:
        data["gold"] = None

    # 4. 国债指数 (sh000012) 作为债券代理
    try:
        bond = ak.stock_zh_index_daily(symbol="sh000012")
        data["bond"] = _align_datecol(bond, "bond")
    except Exception:
        data["bond"] = None

    return data


# ------------------------------------------------------------------
# 核心回测
# ------------------------------------------------------------------


def run_backtest(
    initial_capital: float = 1_000_000.0,
    force_refresh: bool = False,
    strategy_config: StrategyConfig | None = None,
) -> tuple[BacktestSummary, BacktestSeries]:
    """
    运行完整回测。

    Parameters
    ----------
    initial_capital : float
        初始资金
    force_refresh : bool
        是否强制重新抓取数据（默认用缓存）

    Returns
    -------
    (BacktestSummary, BacktestSeries)
    """
    data = _load_all_data()
    _validate_data(data)

    # === 合并对齐 ===
    merged = _merge_data(data)

    if merged.empty:
        raise ValueError("回测数据为空，请先刷新获取数据")

    # === 逐日模拟 ===
    return _simulate(merged, initial_capital, strategy_config)


# ------------------------------------------------------------------
# 内部实现
# ------------------------------------------------------------------


def _validate_data(data: dict) -> None:
    """检查关键数据是否存在"""
    for key in ("csi300", "pe", "pb"):
        if data.get(key) is None or data[key].empty:
            raise ValueError(f"缺少 {key} 数据，请先刷新缓存")


def _merge_data(data: dict) -> pd.DataFrame:
    """将所有数据按日期左合并到 CSI300 日线上"""
    base = data["csi300"][["date", "close"]].copy()
    base = base.rename(columns={"close": "csi300_close"})
    base = base.sort_values("date")

    # PE
    pe = data["pe"][["date", "滚动市盈率"]].copy()
    base = base.merge(pe, on="date", how="left")

    # PB
    pb = data["pb"][["date", "市净率"]].copy()
    base = base.merge(pb, on="date", how="left")

    # 黄金 ETF
    gold = data.get("gold")
    if gold is not None and not gold.empty:
        gold = gold[["date", "收盘"]].copy().rename(columns={"收盘": "gold_close"})
        base = base.merge(gold, on="date", how="left")

    # 国债指数
    bond = data.get("bond")
    if bond is not None and not bond.empty:
        bond = bond[["date", "close"]].copy().rename(columns={"close": "bond_close"})
        base = base.merge(bond, on="date", how="left")

    # 向前填充估值数据（PE/PB 可能不是每个交易日都有）
    base[["滚动市盈率", "市净率"]] = base[["滚动市盈率", "市净率"]].ffill()
    base = base.dropna(subset=["滚动市盈率", "市净率"])

    # 填充黄金和债券
    for col in ["gold_close", "bond_close"]:
        if col in base.columns:
            base[col] = base[col].ffill().bfill()

    return base.reset_index(drop=True)


def _simulate(
    df: pd.DataFrame,
    capital: float,
    strategy_config: StrategyConfig | None = None,
) -> tuple[BacktestSummary, BacktestSeries]:
    """逐日模拟策略"""

    n = len(df)
    dates = df["date"].tolist()
    csi_close = df["csi300_close"].values
    pe_vals = df["滚动市盈率"].values
    pb_vals = df["市净率"].values

    # 日收益率
    csi_ret = np.full(n, np.nan)
    csi_ret[1:] = csi_close[1:] / csi_close[:-1] - 1
    csi_ret[0] = 0

    has_gold = "gold_close" in df.columns and df["gold_close"].notna().any()
    has_bond = "bond_close" in df.columns and df["bond_close"].notna().any()

    if has_gold:
        gold_close = df["gold_close"].values
        gold_ret = np.full(n, np.nan)
        gold_ret[1:] = gold_close[1:] / gold_close[:-1] - 1
        gold_ret[0] = 0
    else:
        gold_ret = np.full(n, 0.0002)  # ~5% 年化假设

    if has_bond:
        bond_close = df["bond_close"].values
        bond_ret = np.full(n, np.nan)
        bond_ret[1:] = bond_close[1:] / bond_close[:-1] - 1
        bond_ret[0] = 0
    else:
        bond_ret = np.full(n, 0.00014)  # ~3.5% 年化假设

    # === 状态变量 ===
    PORTFOLIO = np.full(n, np.nan)
    BENCHMARK = np.full(n, np.nan)
    STOCK_W = np.full(n, np.nan)
    SIG_ACTIONS = [""] * n

    PORTFOLIO[0] = capital
    BENCHMARK[0] = capital

    w_stock = 0.10   # 初始默认保守
    w_gold = 0.10
    w_bond = 0.80

    # MA60 滚动
    ma60_vals = np.full(n, np.nan)
    for i in range(59, n):
        ma60_vals[i] = np.mean(csi_close[i - 59 : i + 1])

    # 交易计数
    trade_count = 0

    for i in range(1, n):
        # ----- 当日资产净值 -----
        port = PORTFOLIO[i - 1]
        bench = BENCHMARK[i - 1]

        # 资产日收益
        r_s = csi_ret[i] if not np.isnan(csi_ret[i]) else 0
        r_g = gold_ret[i] if not np.isnan(gold_ret[i]) else 0
        r_b = bond_ret[i] if not np.isnan(bond_ret[i]) else 0

        # 资产实际增值（再平衡前）
        stock_val = port * w_stock * (1 + r_s)
        gold_val = port * w_gold * (1 + r_g)
        bond_val = port * w_bond * (1 + r_b)
        port_new = stock_val + gold_val + bond_val

        bench_new = bench * (1 + r_s)

        PORTFOLIO[i] = port_new
        BENCHMARK[i] = bench_new

        # ----- 权重漂移 -----
        w_stock = stock_val / port_new if port_new > 0 else w_stock
        w_gold = gold_val / port_new if port_new > 0 else w_gold
        w_bond = bond_val / port_new if port_new > 0 else w_bond

        # ----- 策略信号 -----
        pe_q = _calc_percentile(pe_vals, i)
        pb_q = _calc_percentile(pb_vals, i)
        above_ma60 = csi_close[i] >= ma60_vals[i] if not np.isnan(ma60_vals[i]) else True

        signal = generate_signal(pe_q, pb_q, above_ma60, config=strategy_config)
        SIG_ACTIONS[i] = signal.action

        # ----- 再平衡 -----
        rebalance = False
        target = signal.target_stock_pct

        if target is not None and signal.action in ("buy_stock", "sell_stock"):
            # 仅在当前权重与目标偏差超过 2% 时才再平衡
            if abs(w_stock - target / 100.0) > 0.02:
                rebalance = True

        # 半年期强制再平衡
        dt = dates[i]
        if isinstance(dt, date | datetime):
            month, day = dt.month, dt.day
        else:
            month, day = dt.month, dt.day

        if (month == 6 and day == 30) or (month == 12 and day == 31):
            rebalance = True

        if rebalance:
            if target is not None:
                w_stock = target / 100.0
            # 即使无信号（hold 日），半年再平衡也重新对齐黄金和债券
            w_gold = 0.10
            w_bond = 1.0 - w_stock - w_gold
            trade_count += 1

        STOCK_W[i] = w_stock

    # === 绩效指标 ===
    summary = _compute_metrics(
        PORTFOLIO, BENCHMARK, csi_ret, dates, trade_count, capital
    )

    series = BacktestSeries(
        dates=[str(d.date()) if hasattr(d, "date") else str(d) for d in dates],
        portfolio_value=PORTFOLIO.tolist(),
        benchmark_value=BENCHMARK.tolist(),
        stock_weight=(STOCK_W * 100).tolist(),
        signal_actions=SIG_ACTIONS,
    )

    return summary, series


def _calc_percentile(values: np.ndarray, idx: int, window: int = 1250) -> float:
    """计算 values[idx] 在最近 window 个数据中的分位"""
    start = max(0, idx - window)
    chunk = values[start:idx]
    valid = chunk[~np.isnan(chunk)]
    if len(valid) < 60:
        return 50.0  # 数据不足时返回中位
    current = values[idx]
    return float((valid < current).mean() * 100)


def _compute_metrics(
    portfolio: np.ndarray,
    benchmark: np.ndarray,
    benchmark_returns: np.ndarray,
    dates: list,
    trade_count: int,
    capital: float,
) -> BacktestSummary:
    """计算绩效指标"""
    n = len(portfolio)
    total_ret = (portfolio[-1] / capital - 1) * 100
    bench_ret = (benchmark[-1] / capital - 1) * 100

    # 年数
    if isinstance(dates[0], datetime):
        years = (dates[-1] - dates[0]).days / 365.25
    else:
        start = pd.to_datetime(dates[0])
        end = pd.to_datetime(dates[-1])
        years = (end - start).days / 365.25
    years = max(years, 0.1)

    annual_ret = ((1 + total_ret / 100) ** (1 / years) - 1) * 100
    bench_annual = ((1 + bench_ret / 100) ** (1 / years) - 1) * 100

    # 最大回撤
    def max_dd(arr):
        peak = np.maximum.accumulate(arr)
        dd = (arr - peak) / peak * 100
        return float(np.min(dd))

    mdd = max_dd(portfolio)
    bench_mdd = max_dd(benchmark)

    # 夏普（日频，无风险 2.5%/年 ≈ 0.0097%/交易日）
    risk_free_daily = 0.025 / 252
    port_returns = portfolio[1:] / portfolio[:-1] - 1
    excess = port_returns - risk_free_daily
    sharpe = float(np.mean(excess) / np.std(excess) * np.sqrt(252)) if np.std(excess) > 0 else 0

    bench_excess = benchmark_returns[1:] - risk_free_daily
    bench_sharpe = float(np.mean(bench_excess) / np.std(bench_excess) * np.sqrt(252)) if np.std(bench_excess) > 0 else 0

    # 日胜率
    beats = (port_returns > benchmark_returns[1:]).mean() * 100

    return BacktestSummary(
        total_return=round(total_ret, 2),
        annual_return=round(annual_ret, 2),
        benchmark_return=round(bench_ret, 2),
        benchmark_annual_return=round(bench_annual, 2),
        max_drawdown=round(mdd, 2),
        benchmark_max_drawdown=round(bench_mdd, 2),
        sharpe=round(sharpe, 2),
        benchmark_sharpe=round(bench_sharpe, 2),
        win_rate=round(beats, 1),
        num_trades=trade_count,
        final_value=round(portfolio[-1], 2),
        benchmark_final=round(benchmark[-1], 2),
        start_date=str(dates[0].date()) if hasattr(dates[0], "date") else str(dates[0]),
        end_date=str(dates[-1].date()) if hasattr(dates[-1], "date") else str(dates[-1]),
        years=round(years, 1),
    )
