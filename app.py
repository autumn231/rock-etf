import streamlit as st
import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.data import (
    fetch_data,
    get_cache_mtime,
    log_signal,
    get_signal_log,
    clear_signal_log,
    load_cached_history,
)
from src.strategy import generate_signal, StrategyConfig
from src.portfolio import Holdings, calculate_trades
from src.backtest import run_backtest

# ============================================================
# 页面全局设置
# ============================================================
st.set_page_config(page_title="磐石ETF双核轮动量化系统", page_icon="📈", layout="centered")

# ── 自定义 CSS ──
st.markdown("""
<style>
    /* 指标数字微调 */
    [data-testid="stMetricValue"] { font-size: 1.6rem !important; font-weight: 700 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; font-weight: 500 !important; color: #64748b !important; }
    /* 分割线更淡 */
    hr { margin: 0.8rem 0 !important; border-color: #f1f5f9 !important; }
    /* 表格圆角 */
    [data-testid="stTable"] table { border-radius: 8px; overflow: hidden; }
    /* 侧边栏更紧凑 */
    .css-1544g2n { padding-top: 1rem; }
</style>
""", unsafe_allow_html=True)

now = datetime.datetime.now()
today_str = now.strftime("%Y-%m-%d")

# ============================================================
# 侧边栏
# ============================================================
with st.sidebar:
    st.header("📋 当前持仓")
    with st.form("holdings_form"):
        stock_val = st.number_input("沪深300 ETF 市值（元）", min_value=0.0, value=0.0, step=10000.0, format="%.2f")
        gold_val = st.number_input("黄金市值（元）", min_value=0.0, value=0.0, step=10000.0, format="%.2f")
        chengduo_val = st.number_input("城投债市值（元）", min_value=0.0, value=0.0, step=10000.0, format="%.2f")
        govt_val = st.number_input("国债市值（元）", min_value=0.0, value=0.0, step=10000.0, format="%.2f")
        st.form_submit_button("💾 更新持仓", use_container_width=True)

    st.divider()

    # ---- 历史信号 ----
    with st.expander("📜 历史信号记录"):
        logs = get_signal_log(30)
        if logs:
            for entry in reversed(logs):
                st.markdown(
                    f"**{entry['日期']}**  {entry['信号']}  "
                    f"PE{entry['PE分位']} PB{entry['PB分位']}  "
                    f"目标{entry['目标股票%']}"
                )
            if st.button("🗑️ 清空记录", use_container_width=True):
                clear_signal_log()
                st.rerun()
        else:
            st.caption("尚无记录，下次刷新后将自动记录")

    st.divider()

    # ---- 新资金入场（首日建仓） ----
    st.markdown("➕ **新资金入场**")
    new_capital = st.number_input(
        "准备投入总额（元）",
        min_value=0.0,
        value=0.0,
        step=10000.0,
        format="%.2f",
        key="new_capital",
        label_visibility="collapsed",
    )

    st.divider()

    # ---- 缓存状态 ----
    st.caption("🔄 数据缓存")
    cache_mtime = get_cache_mtime()
    if cache_mtime:
        st.caption(f"上次更新：{cache_mtime.strftime('%m-%d  %H:%M')}")
    else:
        st.caption("尚未缓存，首次加载将自动抓取")

    if st.button("🔄 强制刷新数据", type="secondary", use_container_width=True):
        st.session_state["force_refresh"] = True
        st.rerun()

    st.divider()

    # ---- 策略阈值（日常） ----
    with st.expander("⚙️ 策略阈值"):
        _D_KEYS = ["d_pe_be", "d_pe_bc", "d_pe_sw", "d_pe_sp", "d_pb_pw", "d_pb_pp"]
        _D_PRESETS = {
            "默认 (10/30/50/70/40/50)": (10, 30, 50, 70, 40, 50),
            "保守 (15/40/40/60/30/40)": (15, 40, 40, 60, 30, 40),
            "激进 (5/20/60/80/50/60)": (5, 20, 60, 80, 50, 60),
            "关闭PB拦截 (10/30/50/70/0/0)": (10, 30, 50, 70, 0, 0),
        }

        # 恢复默认（在 widget 创建之前）
        if st.session_state.pop("_d_restore", False):
            dk = list(_D_PRESETS.keys())[0]
            st.session_state["_d_preset"] = dk
            for k, v in zip(_D_KEYS, _D_PRESETS[dk]):
                st.session_state[k] = v

        def _apply_daily_preset():
            vals = _D_PRESETS.get(st.session_state["_d_preset"])
            if vals:
                for k, v in zip(_D_KEYS, vals):
                    st.session_state[k] = v

        st.selectbox("预设", list(_D_PRESETS.keys()), key="_d_preset",
                     on_change=_apply_daily_preset, label_visibility="collapsed")

        c1, c2 = st.columns(2)
        with c1:
            st.slider("极寒买入 PE≤%", 1, 30, 10, key="d_pe_be")
            st.slider("低估买入 PE≤%", 5, 50, 30, key="d_pe_bc")
            st.slider("卖出警戒 PE>%", 30, 80, 50, key="d_pe_sw")
        with c2:
            st.slider("清仓逃顶 PE>%", 50, 95, 70, key="d_pe_sp")
            st.slider("减仓需 PB>%", 0, 80, 40, key="d_pb_pw")
            st.slider("清仓需 PB>%", 0, 80, 50, key="d_pb_pp")

        if st.button("↩ 恢复默认", use_container_width=True):
            st.session_state["_d_restore"] = True
            st.rerun()

# ============================================================
# 主面板
# ============================================================
st.title("📊 磐石ETF双核轮动量化系统")
st.caption(f"当前交易日复盘：{today_str}（基于近5年滚动数据）")

# ---- 半年期强制再平衡雷达 ----
is_rebalance_window = (now.month == 6 and now.day >= 25) or (now.month == 12 and now.day >= 25)
if is_rebalance_window:
    st.error(
        "⏳ **【系统最高指令】半年期强制再平衡窗口已开启！**\n\n"
        "临近 6月末/12月末，请在最近的交易日收盘前，强行执行资产剪枝纪律：\n"
        "1. 检查股票仓位是否与今日指令对齐，偏离则强制买卖对齐。\n"
        "2. 检查剩余资金，强制恢复 "
        "**[黄金10%] + [城投债:国债 = 1:1]** 的极限防守阵型。"
    )

st.divider()

# ============================================================
# 核心数据获取
# ============================================================
force_refresh = st.session_state.pop("force_refresh", False)

with st.spinner("正在从云端抓取近5年原始数据并动态计算，请稍候..."):
    data, status_msg = fetch_data(force_refresh=force_refresh)

if data is None:
    st.error(f"🚨 系统异常：{status_msg}")
    st.stop()

# ---- 四指标展示 ----
col1, col2, col3, col4 = st.columns(4)
col1.metric(label="沪深300收盘", value=f"{data.close:.2f}")
col2.metric(
    label="60日均线",
    value=f"{data.ma60:.2f}",
    delta="线上(多头)" if data.above_ma60 else "线下(空头)",
    delta_color="normal" if data.above_ma60 else "inverse",
)
col3.metric(label="PE 历史分位(5Y)", value=f"{data.pe_q:.1f}%")
col4.metric(label="PB 历史分位(5Y)", value=f"{data.pb_q:.1f}%")

if data.cached:
    st.caption(f"ℹ️ {status_msg}")
else:
    st.caption(f"ℹ️ 来源: {data.source_note}")

# ============================================================
# PE/PB 历史走势图
# ============================================================
with st.expander("📈 估值走势图（近5年）", expanded=False):
    pe_df = load_cached_history("pe")
    pb_df = load_cached_history("pb")

    if pe_df is not None and pb_df is not None and len(pe_df) > 250:
        try:
            cutoff = pd.Timestamp.now() - pd.DateOffset(years=5)
            pe_df["日期"] = pd.to_datetime(pe_df["日期"])
            pb_df["日期"] = pd.to_datetime(pb_df["日期"])
            pe_5y = pe_df[pe_df["日期"] >= cutoff].copy()
            pb_5y = pb_df[pb_df["日期"] >= cutoff].copy()

            pe_val = pe_5y["滚动市盈率"]
            pb_val = pb_5y["市净率"]

            # 分位阈值
            p10, p30, p70, p90 = pe_val.quantile([0.10, 0.30, 0.70, 0.90])
            current_pe = pe_val.iloc[-1]
            current_pb = pb_val.iloc[-1]

            # 判断当前区域
            if current_pe <= p10:
                zone_label, zone_color = "极寒区", "#ef4444"
            elif current_pe <= p30:
                zone_label, zone_color = "低估区", "#f59e0b"
            elif current_pe <= p70:
                zone_label, zone_color = "常态区", "#22c55e"
            elif current_pe <= p90:
                zone_label, zone_color = "偏高区", "#f97316"
            else:
                zone_label, zone_color = "泡沫区", "#ef4444"

            fig = make_subplots(specs=[[{"secondary_y": True}]])

            # PE 面积填充（底部到曲线）
            fig.add_trace(go.Scatter(
                x=pe_5y["日期"], y=pe_val,
                fill="tozeroy",
                fillcolor="rgba(37, 99, 235, 0.08)",
                line=dict(color="#2563eb", width=2),
                name="PE(TTM)",
                hovertemplate="PE: %{y:.2f}<extra></extra>",
            ), secondary_y=False)

            # PB 曲线（右轴）
            fig.add_trace(go.Scatter(
                x=pb_5y["日期"], y=pb_val,
                line=dict(color="#f97316", width=1.5, dash="dot"),
                name="PB",
                yaxis="y2",
                hovertemplate="PB: %{y:.3f}<extra></extra>",
            ), secondary_y=True)

            # 极淡的估值带（add_hrect — 横跨全图）
            bands = [
                (0, p10, "rgba(239, 68, 68, 0.04)"),
                (p10, p30, "rgba(245, 158, 11, 0.04)"),
                (p70, p90, "rgba(249, 115, 22, 0.04)"),
                (p90, pe_val.max() * 1.05, "rgba(239, 68, 68, 0.06)"),
            ]
            for y0, y1, color in bands:
                fig.add_hrect(y0=y0, y1=y1, fillcolor=color,
                              line_width=0, layer="below")

            # 极细阈值虚线（无标注）
            for level, color in [(p10, "#ef4444"), (p30, "#f59e0b"),
                                 (p70, "#f97316"), (p90, "#ef4444")]:
                fig.add_hline(y=level, line=dict(
                    color=color, width=1, dash="dash"
                ), secondary_y=False)

            # 当前值醒目标记
            fig.add_trace(go.Scatter(
                x=[pe_5y["日期"].iloc[-1]], y=[current_pe],
                mode="markers",
                marker=dict(size=10, color=zone_color,
                            line=dict(width=2, color="#ffffff")),
                name=f"当前 {current_pe:.2f}",
                showlegend=False,
            ), secondary_y=False)

            fig.update_layout(
                height=380,
                margin=dict(l=40, r=20, t=10, b=30),
                legend=dict(orientation="h", y=1.08, x=0),
                hovermode="x unified",
                hoverlabel=dict(bgcolor="white", font_size=13),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(showgrid=False),
                yaxis=dict(showgrid=True, gridcolor="#f1f5f9",
                           title="PE(TTM)", griddash="dot"),
                yaxis2=dict(showgrid=False, title="PB",
                            overlaying="y", side="right"),
            )

            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

            # ── 图下方估值区域卡片 ──
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.markdown(
                f"<div style='background:{zone_color}20; border-radius:8px; padding:8px; text-align:center'>"
                f"<div style='font-size:11px;color:#64748b'>当前区域</div>"
                f"<div style='font-size:18px;font-weight:700;color:{zone_color}'>{zone_label}</div>"
                f"<div style='font-size:13px;color:#1e293b'>PE {current_pe:.2f}</div>"
                f"</div>", unsafe_allow_html=True
            )
            cols = [c2, c3, c4, c5]
            for col, (label, val, color) in zip(cols, [
                ("P10 极寒", p10, "#ef4444"),
                ("P30 低估", p30, "#f59e0b"),
                ("P70 偏高", p70, "#f97316"),
                ("P90 泡沫", p90, "#ef4444"),
            ]):
                col.markdown(
                    f"<div style='background:{color}15; border-radius:8px; padding:8px; text-align:center'>"
                    f"<div style='font-size:11px;color:#64748b'>{label}</div>"
                    f"<div style='font-size:16px;font-weight:600;color:{color}'>{val:.1f}</div>"
                    f"</div>", unsafe_allow_html=True
                )

        except Exception as e:
            st.caption(f"图表生成中遇到问题: {e}")
    else:
        st.caption("缓存数据不足，请先刷新获取数据")

st.divider()

# ============================================================
# 策略大脑判定
# ============================================================
daily_config = StrategyConfig(
    buy_extreme=st.session_state.get("d_pe_be", 10),
    buy_cheap=st.session_state.get("d_pe_bc", 30),
    sell_warn=st.session_state.get("d_pe_sw", 50),
    sell_panic=st.session_state.get("d_pe_sp", 70),
    pb_confirm_warn=st.session_state.get("d_pb_pw", 40),
    pb_confirm_panic=st.session_state.get("d_pb_pp", 50),
)

st.subheader("💡 今日执行指令")
signal = generate_signal(
    pe_q=data.pe_q, pb_q=data.pb_q, above_ma60=data.above_ma60,
    config=daily_config,
)

if signal.signal_type == "error":
    st.error(f"{signal.title}\n\n{signal.instruction}")
elif signal.signal_type == "warning":
    st.warning(f"{signal.title}\n\n{signal.instruction}")
elif signal.signal_type == "success":
    st.success(f"{signal.title}\n\n{signal.instruction}")
else:
    st.info(f"{signal.title}\n\n{signal.instruction}")

# ---- 记录本次信号到日志 ----
log_signal(
    date_str=today_str,
    pe_q=data.pe_q,
    pb_q=data.pb_q,
    signal_type=signal.title,
    action=signal.action,
    target_pct=signal.target_stock_pct,
    above_ma60=data.above_ma60,
    close=data.close,
)

# ============================================================
# 新资金建仓指令
# ============================================================
if new_capital > 0:
    pe = data.pe_q
    bc = daily_config.buy_cheap
    bx = daily_config.buy_extreme
    if pe > bc:
        stock_pct, bond_pct, gold_pct = 10, 80, 10
    elif pe >= bx:
        stock_pct, bond_pct, gold_pct = 20, 70, 10
    else:
        stock_pct, bond_pct, gold_pct = 30, 60, 10

    st.divider()
    st.subheader("💰 新资金建仓方案")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("总投入", f"¥{new_capital:,.2f}")
    c2.metric("股票", f"¥{new_capital * stock_pct / 100:,.0f}")
    c3.metric("城投债", f"¥{new_capital * bond_pct / 200:,.0f}")
    c4.metric("国债", f"¥{new_capital * bond_pct / 200:,.0f}")
    c5.metric("黄金", f"¥{new_capital * gold_pct / 100:,.0f}")

    st.info(
        f"📌 当前 PE 处于 {pe:.1f}% 分位 → {stock_pct}%股 + {bond_pct}%债 + {gold_pct}%金\n\n"
        f"城投债与国债各占债券的一半（各 {bond_pct/2:.0f}%）"
    )

# ============================================================
# 调仓指令
# ============================================================
holdings = Holdings(
    stock_value=stock_val,
    gold_value=gold_val,
    chengduo_bond_value=chengduo_val,
    govt_bond_value=govt_val,
)

if holdings.total_value > 0:
    trades = calculate_trades(holdings, signal)

    st.divider()
    st.subheader("📝 调仓指令")

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("总市值", f"¥{trades.total_value:,.2f}")
    col_b.metric("当前股票占比", f"{trades.current_stock_pct:.1f}%")
    if trades.target_stock_pct is not None:
        col_c.metric("目标股票占比", f"{trades.target_stock_pct:.0f}%")
    else:
        col_c.metric("目标股票占比", "维持不变")

    rows = []
    for label, delta in [
        ("沪深300 ETF", trades.stock_delta),
        ("黄金", trades.gold_delta),
        ("城投债", trades.chengduo_bond_delta),
        ("国债", trades.govt_bond_delta),
    ]:
        if abs(delta) < 1.0:
            rows.append({"资产": label, "操作": "✅ 不动", "金额": "—"})
        elif delta > 0:
            rows.append({"资产": label, "操作": "🟢 买入", "金额": f"+¥{delta:,.2f}"})
        else:
            rows.append({"资产": label, "操作": "🔴 卖出", "金额": f"¥{delta:,.2f}"})

    st.table(rows)

    if trades.target_stock_pct is not None:
        tp = trades.target_stock_pct
        bond_rest = (100 - tp - 10) / 2
        st.info(
            f"💡 调仓后资产比例："
            f"股票 {tp:.0f}%  +  黄金 10%  +  "
            f"城投债 {bond_rest:.0f}%  +  国债 {bond_rest:.0f}%"
        )
else:
    st.caption("💡 在左侧边栏输入当前持仓市值，即可查看具体调仓金额。")

# ============================================================
# 回测绩效
# ============================================================
st.divider()
with st.expander("📊 回测绩效（可调阈值）", expanded=False):

    # ── 阈值预设 ──
    _SLIDER_KEYS = ["s_pe_be", "s_pe_bc", "s_pe_sw", "s_pe_sp", "s_pb_pw", "s_pb_pp"]

    _PRESETS = {
        "默认 (10/30/50/70/40/50)": (10, 30, 50, 70, 40, 50),
        "保守 (15/40/40/60/30/40)": (15, 40, 40, 60, 30, 40),
        "激进 (5/20/60/80/50/60)": (5, 20, 60, 80, 50, 60),
        "关闭PB拦截 (10/30/50/70/0/0)": (10, 30, 50, 70, 0, 0),
    }

    # 恢复默认（在 widget 创建之前）
    if st.session_state.pop("_bt_restore", False):
        dk = list(_PRESETS.keys())[0]
        st.session_state["_preset_sel"] = dk
        for k, v in zip(_SLIDER_KEYS, _PRESETS[dk]):
            st.session_state[k] = v

    def _apply_preset():
        vals = _PRESETS.get(st.session_state["_preset_sel"])
        if vals:
            for k, v in zip(_SLIDER_KEYS, vals):
                st.session_state[k] = v

    sel = st.selectbox("🎯 阈值预设", list(_PRESETS.keys()),
                       key="_preset_sel", on_change=_apply_preset)

    # 当前预设说明
    st.caption(f"当前：{sel}")

    # 滑条
    c_left, c_right = st.columns(2)
    with c_left:
        pe_be = st.slider("极寒买入 PE≤%", 1, 30, 10, key="s_pe_be")
        pe_bc = st.slider("低估买入 PE≤%", 5, 50, 30, key="s_pe_bc")
        pe_sw = st.slider("卖出警戒 PE>%", 30, 80, 50, key="s_pe_sw")
    with c_right:
        pe_sp = st.slider("清仓逃顶 PE>%", 50, 95, 70, key="s_pe_sp")
        pb_pw = st.slider("减仓需 PB>%", 0, 80, 40, key="s_pb_pw")
        pb_pp = st.slider("清仓需 PB>%", 0, 80, 50, key="s_pb_pp")

    rcol1, rcol2, rcol3 = st.columns([1, 1, 3])
    with rcol1:
        if st.button("↩ 恢复默认", use_container_width=True):
            st.session_state["_bt_restore"] = True
            st.rerun()

    bt_config = StrategyConfig(
        buy_extreme=pe_be, buy_cheap=pe_bc,
        sell_warn=pe_sw, sell_panic=pe_sp,
        pb_confirm_warn=pb_pw, pb_confirm_panic=pb_pp,
    )

    if st.button("▶ 运行回测", use_container_width=True, type="primary"):
        with st.spinner("回测进行中（需加载黄金/债券历史数据，约 30 秒）..."):
            try:
                summary, series = run_backtest(
                    force_refresh=False, strategy_config=bt_config
                )
                st.session_state["bt_summary"] = summary
                st.session_state["bt_series"] = series
            except Exception as e:
                st.error(f"回测失败: {e}")

    if "bt_summary" in st.session_state:
        s = st.session_state["bt_summary"]
        se = st.session_state["bt_series"]

        col_a, col_b = st.columns([2, 1])
        with col_a:
            st.markdown(f"**回测区间**：{s.start_date} ~ {s.end_date}（{s.years} 年，{s.num_trades} 次交易）")

        # 绩效表格
        perf = [
            {"指标": "总收益率", "策略": f"{s.total_return:+.1f}%", "买入持有": f"{s.benchmark_return:+.1f}%"},
            {"指标": "年化收益率", "策略": f"{s.annual_return:+.1f}%", "买入持有": f"{s.benchmark_annual_return:+.1f}%"},
            {"指标": "最大回撤", "策略": f"{s.max_drawdown:.1f}%", "买入持有": f"{s.benchmark_max_drawdown:.1f}%"},
            {"指标": "夏普比率", "策略": f"{s.sharpe:.2f}", "买入持有": f"{s.benchmark_sharpe:.2f}"},
            {"指标": "日胜率", "策略": f"{s.win_rate:.1f}%", "买入持有": "—"},
            {"指标": "最终净值(¥1M起)", "策略": f"¥{s.final_value:,.0f}", "买入持有": f"¥{s.benchmark_final:,.0f}"},
        ]
        st.table(perf)

        # 净值曲线图
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=se.dates, y=se.portfolio_value,
            name="磐石策略", line=dict(color="#1f77b4"),
        ))
        fig.add_trace(go.Scatter(
            x=se.dates, y=se.benchmark_value,
            name="沪深300买入持有", line=dict(color="#ff7f0e", dash="dot"),
        ))
        fig.update_layout(
            height=400,
            margin=dict(l=20, r=20, t=10, b=20),
            legend=dict(orientation="h", y=1.08),
            hovermode="x unified",
            yaxis_title="净值 (¥)",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "⚠️ 回测假设：债券用国债指数代理（城投债与国债各半的配置简化为全国债），"
            "黄金用 518880 ETF。不考虑交易成本和税费。"
        )
    else:
        st.caption("点击「运行回测」加载历史数据并模拟策略。首次运行约需 30-60 秒。")

# ============================================================
# 入场指南
# ============================================================
with st.expander("📖 查看系统行动纲领与入场指南"):
    # 从当前配置读阈值，确保与实际一致
    _bx = daily_config.buy_extreme
    _bc = daily_config.buy_cheap
    st.markdown(f"""
    **【新资金首日入场指南（基于当前阈值）】**
    * 若 PE > {_bc:.0f}%：10%股 + 80%债 + 10%金
    * 若 {_bx:.0f}% < PE ≤ {_bc:.0f}%：20%股 + 70%债 + 10%金
    * 若 PE ≤ {_bx:.0f}%：30%股 + 60%债 + 10%金

    **【纪律铁律】**
    * 1. 黄金永远锁定总资产 10%。
    * 2. 债券部分永远保持 城投债 : 国债 = 1 : 1。
    * 3. 每逢 6月底/12月底，强制通过人工买卖对齐上述比例。
    """)
