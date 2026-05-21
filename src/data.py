"""
数据获取与缓存层。

缓存策略：
- 每次成功抓取后，将原始日线/PE/PB 数据存为 CSV，计算结果存为 JSON
- 缓存 4 小时内有效
- 支持 force_refresh 跳过缓存
- CSI 300 日线有备用源（东方财富），PE/PB 失败时降级到过期缓存
- 每次信号生成后记录到操作日志 CSV
"""

import os
import json
import time
import csv
import concurrent.futures
from dataclasses import dataclass, asdict
from datetime import datetime

import pandas as pd
import akshare as ak


# ---------- 超时保护 ----------

_AKSHARE_TIMEOUT = 35  # 每个 AKShare 调用的最大等待秒数


def _ak_call(fn):
    """在独立线程中调用 AKShare 函数，防止卡死主进程。"""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn)
    try:
        return fut.result(timeout=_AKSHARE_TIMEOUT)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        pool.shutdown(wait=False)
        raise TimeoutError(f"AKShare 接口超过 {_AKSHARE_TIMEOUT}s 无响应")

# ---------- 路径与常量 ----------

CACHE_DIR = "data_cache"
CACHE_MAX_AGE = 3600 * 4          # 4 小时
SIGNAL_LOG_FILE = "signal_log.csv"
SIGNAL_LOG_MAX = 365              # 保留最近 365 条

CSI300_FILE = "csi300_daily.csv"
PE_FILE = "pe_series.csv"
PB_FILE = "pb_series.csv"
RESULT_FILE = "last_result.json"


# ---------- 路径工具 ----------


def _project_root() -> str:
    f = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(f))


def _cache_path(name: str) -> str:
    path = os.path.join(_project_root(), CACHE_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


# ---------- 缓存读写 ----------


def _is_fresh(path: str, max_age: int = CACHE_MAX_AGE) -> bool:
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < max_age


def _read_cached_json(name: str, max_age: int = CACHE_MAX_AGE) -> dict | None:
    path = _cache_path(name)
    if not _is_fresh(path, max_age):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_cached_json(name: str, data: dict) -> None:
    with open(_cache_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- 数据模型 ----------


@dataclass
class MarketData:
    """策略需要的全部市场数据"""
    close: float
    ma60: float
    above_ma60: bool
    pe_q: float
    pb_q: float
    fetch_time: str
    cached: bool = False
    source_note: str = ""   # 标明数据来源


# ---------- 公开 API ----------


def get_cache_mtime() -> datetime | None:
    """返回上次成功缓存的时刻，无缓存时返回 None"""
    path = _cache_path(RESULT_FILE)
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path))


# ------------------------------------------------------------------
# 信号日志
# ------------------------------------------------------------------


def log_signal(
    date_str: str,
    pe_q: float,
    pb_q: float,
    signal_type: str,
    action: str,
    target_pct: float | None,
    above_ma60: bool,
    close: float,
) -> None:
    """
    追加一条信号记录到 CSV。
    同一天多次调用只会保留最后一次（去重）。
    自动清理超出上限的旧记录。
    """
    path = _cache_path(SIGNAL_LOG_FILE)
    row = {
        "日期": date_str,
        "收盘": close,
        "PE分位": f"{pe_q:.1f}%",
        "PB分位": f"{pb_q:.1f}%",
        "均线上": "是" if above_ma60 else "否",
        "信号": signal_type,
        "动作": action,
        "目标股票%": f"{target_pct:.0f}%" if target_pct is not None else "维持",
    }

    fieldnames = list(row.keys())

    # 读已有记录，移除同日期旧条目
    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r.get("日期") != date_str:
                        existing.append(r)
        except Exception:
            existing = []

    existing.append(row)

    # 只保留最近 N 条
    keep = existing[-SIGNAL_LOG_MAX:]

    # 重写
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(keep)


def clear_signal_log() -> None:
    """清空所有信号日志"""
    path = _cache_path(SIGNAL_LOG_FILE)
    if os.path.exists(path):
        os.remove(path)


def get_signal_log(n: int = 30) -> list[dict]:
    """返回最近 n 条信号日志。"""
    path = _cache_path(SIGNAL_LOG_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        return rows[-n:]
    except Exception:
        return []


# ------------------------------------------------------------------
# 历史数据读取（供回测和走势图使用）
# ------------------------------------------------------------------


def load_cached_history(name: str) -> pd.DataFrame | None:
    """从缓存 CSV 加载历史数据。name: 'csi300' | 'pe' | 'pb'"""
    mapping = {"csi300": CSI300_FILE, "pe": PE_FILE, "pb": PB_FILE}
    fname = mapping.get(name)
    if not fname:
        return None
    path = _cache_path(fname)
    if not os.path.exists(path):
        return None
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return None


# ------------------------------------------------------------------
# 带备用源的数据抓取
# ------------------------------------------------------------------


# 沪深 300 日线列名映射（东方财富备用源返回中文列名 → 统一为英文）
_COLUMN_MAP_CSI300 = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}


def _normalize_csi300_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一沪深 300 日线列名：无论主源/备用源都产出英文列名"""
    rename_map = {k: v for k, v in _COLUMN_MAP_CSI300.items() if k in df.columns}
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _fetch_csi300() -> tuple[pd.DataFrame, str]:
    """沪深 300 日线：主源失败自动切到东方财富，统一列名为英文"""
    try:
        df = _ak_call(lambda: ak.stock_zh_index_daily(symbol="sh000300"))
        return _normalize_csi300_columns(df), "主源"
    except Exception as e1:
        try:
            df = _ak_call(lambda: ak.stock_zh_index_daily_em(symbol="sh000300"))
            return _normalize_csi300_columns(df), "备用(东方财富)"
        except Exception as e2:
            raise RuntimeError(
                f"日线主源({e1}) / 备用({e2}) 均失败"
            ) from e2


def _fetch_pe_pb() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """PE + PB 历史序列。各自单独 try，互相不影响。"""
    pe_df = pb_df = None
    errors = []

    try:
        pe_df = _ak_call(lambda: ak.stock_index_pe_lg(symbol="沪深300"))
    except Exception as e:
        errors.append(f"PE: {e}")

    try:
        pb_df = _ak_call(lambda: ak.stock_index_pb_lg(symbol="沪深300"))
    except Exception as e:
        errors.append(f"PB: {e}")

    if pe_df is None and pb_df is None:
        raise RuntimeError("PE/PB 均失败: " + "; ".join(errors))

    note = "主源"
    if pe_df is None or pb_df is None:
        note = f"部分降级({'; '.join(errors)})"

    return pe_df, pb_df, note


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------


def fetch_data(force_refresh: bool = False) -> tuple[MarketData | None, str]:
    """
    入口函数。逐源抓取，每源有独立备用/降级策略。

    Returns
    -------
    (MarketData | None, status_msg)
    """
    # --- 1. 缓存命中 ---
    if not force_refresh:
        cached = _read_cached_json(RESULT_FILE)
        if cached is not None:
            md = MarketData(**cached)
            md.cached = True
            return md, "✅ 使用缓存数据"

    # --- 2. 全新抓取（外层 try 兜底，防止任何漏网异常崩掉 app） ---
    try:
        errors = []
        df_k = df_pe = df_pb = None
        source_note_parts = []

        try:
            df_k, note = _fetch_csi300()
            source_note_parts.append(f"日线:{note}")
        except Exception as e:
            errors.append(str(e))

        try:
            pe_raw, pb_raw, note = _fetch_pe_pb()
            df_pe = pe_raw
            df_pb = pb_raw
            source_note_parts.append(f"估值:{note}")
        except Exception as e:
            errors.append(str(e))

        # --- 3. 部分失败时尝试用旧缓存补齐 ---
        stale_result = _read_cached_json(RESULT_FILE, max_age=9999999)

        if df_k is None:
            if stale_result is not None:
                close = stale_result["close"]
                ma60 = stale_result["ma60"]
                above_ma60 = stale_result["above_ma60"]
                source_note_parts.append("日线:过期缓存")
            else:
                return None, f"🚨 日线获取失败，且无缓存: {'; '.join(errors)}"
        else:
            close = float(df_k.iloc[-1]["close"])
            ma60 = float(df_k.iloc[-1].get("ma60", df_k["close"].rolling(60).mean().iloc[-1]))
            above_ma60 = close >= ma60

        # PE 计算（静默降级，不抛异常）
        pe_q = stale_result["pe_q"] if stale_result else 50.0
        if df_pe is not None:
            try:
                pe_series = pd.to_numeric(df_pe["滚动市盈率"], errors="coerce").dropna()
                pe_window = pe_series.tail(1250)
                pe_q = float((pe_window < pe_window.iloc[-1]).mean() * 100)
            except Exception:
                if stale_result is not None:
                    source_note_parts.append("PE:降级到旧值")

        # PB 计算（静默降级，不抛异常）
        pb_q = stale_result["pb_q"] if stale_result else 50.0
        if df_pb is not None:
            try:
                pb_series = pd.to_numeric(df_pb["市净率"], errors="coerce").dropna()
                pb_window = pb_series.tail(1250)
                pb_q = float((pb_window < pb_window.iloc[-1]).mean() * 100)
            except Exception:
                if stale_result is not None:
                    source_note_parts.append("PB:降级到旧值")

        # --- 4. 缓存原始数据 ---
        if df_k is not None:
            df_k.to_csv(_cache_path(CSI300_FILE), index=False, encoding="utf-8-sig")
        if df_pe is not None:
            df_pe.to_csv(_cache_path(PE_FILE), index=False, encoding="utf-8-sig")
        if df_pb is not None:
            df_pb.to_csv(_cache_path(PB_FILE), index=False, encoding="utf-8-sig")

        # --- 5. 组装结果 ---
        result = MarketData(
            close=close,
            ma60=ma60,
            above_ma60=above_ma60,
            pe_q=pe_q,
            pb_q=pb_q,
            fetch_time=datetime.now().isoformat(),
            source_note=" | ".join(source_note_parts),
        )
        _write_cached_json(RESULT_FILE, asdict(result))

        msg_parts = ["✅ 数据更新"]
        if errors:
            msg_parts.append(f"⚠️ 部分降级: {'; '.join(errors)}")
        return result, " | ".join(msg_parts)

    except Exception as e:
        # 外层兜底：任何没被内层 catch 住的异常 → 降级到过期缓存
        stale_path = _cache_path(RESULT_FILE)
        if os.path.exists(stale_path):
            with open(stale_path, "r", encoding="utf-8") as f:
                stale = json.load(f)
            md = MarketData(**stale)
            md.cached = True
            return md, f"⚠️ 系统异常 ({e})，使用历史缓存"
        return None, f"🚨 系统异常：{e}"
