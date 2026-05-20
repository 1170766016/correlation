"""
终检Fail产品全流程共性聚集度分析 Demo 应用
基于提升度(Lift)算法，从全流程离散工序因素中识别导致Fail的异常聚集特征

================================================================================
【修改记录 / Changelog】
* 2026-05-17: 引入 LightGBM 模型进行 AI 根因诊断 (Tab 5)，通过计算特征信息增益自动挖掘多因子与非线性根因组合。
* 2026-05-16: 重构 Tab4 时间分析模块：引入 6小时(6H) 分桶聚集逻辑，并针对 Top 10 异常区段绘制了半小时粒度(30min)的 Fail 数量变化趋势线图；同步更新了大模型诊断的数据输入结构。
* 2026-05-16: 修改 Lift 默认权重为 0.3；时间特征分析(Tab4)中排除对 _Start_Time 列的分析。
* 2026-05-16: 全面添加 Type Hints 类型提示；引入 logging 记录系统日志；修复吞没异常的错误机制；支持 st.secrets 增加秘钥读取安全性。
* 2026-05-15: 优化统计过滤方法，引入卡方检验/Fisher精确检验；支持失效模式(Failure Mode)多选
* 2026-05-14: 性能优化，将逐行操作替换为向量化操作；优化UI显示修复NaN计算问题
* 2026-05-13: 增加对 Excel 文件的支持 (使用 calamine/openpyxl 引擎)
* 2026-05-12: 移除 Sankey 图表及相关依赖逻辑
================================================================================
"""

import warnings

warnings.filterwarnings("ignore", message="coroutine 'expire_cache' was never awaited")
warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*tracemalloc.*")

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import os
import json
import re
import logging
from typing import List, Dict, Tuple, Any, Optional, Union
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 配置系统日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

st.set_page_config(page_title="终检Fail共性聚集度分析", layout="wide")

DATA_PATH = os.path.join(os.path.dirname(__file__), "PRB数据.csv")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard F11.xlsx")


# 优先从 st.secrets 获取，回退到 os.getenv
def get_secret(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


LLM_API_KEY = get_secret("LLM_API_KEY", "")
LLM_API_BASE = get_secret("LLM_API_BASE", "")
LLM_MODEL = get_secret("LLM_MODEL", "")

SKIP_COLS = {"Build"}


def generate_mock_data(n_rows: int = 10000) -> pd.DataFrame:
    """生成模拟制造数据用于效果展示"""
    import numpy as np
    np.random.seed(42)

    n_fail = int(n_rows * 0.08)
    n_pass = n_rows - n_fail

    dates = pd.date_range("2025-01-01", "2025-03-31", freq="h")
    stations = ["SMT-01", "SMT-02", "AOI-01", "FCT-01", "ICT-01", "ASSY-01", "ASSY-02"]
    modes = ["开路", "短路", "焊接不良", "功能异常", "外观不良", "尺寸超差"]

    shuffled_results = ["PASS"] * n_pass + ["FAIL"] * n_fail
    idx = np.arange(n_rows)
    np.random.shuffle(idx)
    shuffled_results = [shuffled_results[i] for i in idx]
    data = {"Results": shuffled_results}

    data["Date"] = [dates[i % len(dates)].strftime("%Y-%m-%d %H:%M:%S") for i in range(n_rows)]

    failed_station_list = [""] * n_rows
    failure_mode_list = [""] * n_rows
    for i, r in enumerate(shuffled_results):
        if r == "FAIL":
            si = sum(1 for j in range(i) if shuffled_results[j] == "FAIL")
            failed_station_list[i] = stations[si % len(stations)]
            failure_mode_list[i] = modes[si % len(modes)]
    data["Failed_Station"] = failed_station_list
    data["Failure_Mode"] = failure_mode_list

    data["MC_ID"] = [f"MC{np.random.randint(1, 21):03d}" for _ in range(n_rows)]
    data["Cavity"] = [f"Cav_{np.random.choice(['A','B','C','D'])}" for _ in range(n_rows)]
    data["Vendor_Lot"] = [f"LOT{np.random.randint(1000, 9999)}" for _ in range(n_rows)]
    data["Operator"] = [f"OP{np.random.randint(1, 51):03d}" for _ in range(n_rows)]
    data["Line_ID"] = [f"L{np.random.randint(1, 9)}" for _ in range(n_rows)]
    data["Fixture_ID"] = [f"FIX{np.random.randint(100, 500)}" for _ in range(n_rows)]
    data["Program_Ver"] = [f"V{np.random.randint(1, 6)}.{np.random.randint(0, 10)}" for _ in range(n_rows)]
    data["Supplier"] = [np.random.choice(["SUP_A", "SUP_B", "SUP_C", "SUP_D"], p=[0.4, 0.3, 0.2, 0.1]) for _ in range(n_rows)]
    data["Shift"] = [np.random.choice(["白班", "夜班"]) for _ in range(n_rows)]
    data["Temperature"] = [round(np.random.uniform(20, 30), 1) for _ in range(n_rows)]
    data["Humidity"] = [round(np.random.uniform(40, 70), 1) for _ in range(n_rows)]
    data["PCB_Batch"] = [f"PCB{np.random.randint(2025, 2026)}-{np.random.randint(1, 50):03d}" for _ in range(n_rows)]
    data["Tray_ID"] = [f"TRAY{np.random.randint(1, 201):03d}" for _ in range(n_rows)]

    # ── 添加时间类列（模拟各工序的结束时间），用于 Tab4 时间聚集分析 ──
    base_times = pd.to_datetime(data["Date"])
    # 模拟 SMT 工序结束时间（在 Date 基础上加上 0~2 小时随机偏移）
    smt_offset = pd.to_timedelta(np.random.uniform(0, 7200, n_rows), unit="s")
    data["SMT_End_Time"] = (base_times + smt_offset).strftime("%Y-%m-%d %H:%M:%S").tolist()
    # 模拟 AOI 工序结束时间（SMT 之后 0.5~1.5 小时）
    aoi_offset = smt_offset + pd.to_timedelta(np.random.uniform(1800, 5400, n_rows), unit="s")
    data["AOI_End_Time"] = (base_times + aoi_offset).strftime("%Y-%m-%d %H:%M:%S").tolist()
    # 模拟 FCT 工序结束时间（AOI 之后 1~3 小时）
    fct_offset = aoi_offset + pd.to_timedelta(np.random.uniform(3600, 10800, n_rows), unit="s")
    data["FCT_End_Time"] = (base_times + fct_offset).strftime("%Y-%m-%d %H:%M:%S").tolist()

    # 注入时间聚集信号：让 FAIL 样本的 FCT_End_Time 集中在特定几天
    fail_indices = [i for i, r in enumerate(shuffled_results) if r == "FAIL"]
    # 50% 的 FAIL 集中在 2025-01-15 ~ 2025-01-17 这3天
    n_time_signal = int(len(fail_indices) * 0.50)
    signal_days = pd.date_range("2025-01-15", "2025-01-17", freq="D")
    for i in range(n_time_signal):
        fi = fail_indices[i]
        day = signal_days[i % len(signal_days)]
        hour = np.random.randint(8, 22)
        minute = np.random.randint(0, 60)
        data["FCT_End_Time"][fi] = f"{day.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}:00"

    # 模拟工序滞留时间（秒数，非时间戳，不应被时间分析处理）
    data["SMT_Staging_time"] = [round(np.random.uniform(60, 600), 1) for _ in range(n_rows)]
    data["AOI_Staging_time"] = [round(np.random.uniform(30, 300), 1) for _ in range(n_rows)]

    df = pd.DataFrame(data)

    # 注入信号: 让某些取值在 FAIL 中更集中
    fail_df = df[df["Results"] == "FAIL"]
    pass_df = df[df["Results"] == "PASS"]

    signal_col_val = [
        ("MC_ID", "MC005", 0.35), ("MC_ID", "MC012", 0.25),
        ("Cavity", "Cav_B", 0.40), ("Cavity", "Cav_D", 0.20),
        ("Vendor_Lot", "LOT5678", 0.30), ("Vendor_Lot", "LOT1234", 0.20),
        ("Line_ID", "L3", 0.30), ("Line_ID", "L7", 0.25),
        ("Fixture_ID", "FIX250", 0.25), ("Fixture_ID", "FIX180", 0.20),
        ("Operator", "OP015", 0.20),
        ("Supplier", "SUP_B", 0.30),
    ]

    for col, val, target_ratio in signal_col_val:
        n_available = len(fail_df)
        n_target = int(n_available * target_ratio)
        n_existing = fail_df[col].value_counts().get(val, 0)
        n_need = max(0, n_target - n_existing)
        if n_need <= 0:
            continue
        non_target = fail_df.index[fail_df[col] != val].tolist()
        n_assign = min(n_need, len(non_target))
        if n_assign > 0:
            assign_idx = np.random.choice(non_target, n_assign, replace=False)
            df.loc[assign_idx, col] = val

    pass_affected = pass_df.index[:]
    n_remove = int(len(pass_affected) * 0.005)
    if n_remove > 0:
        remove_idx = np.random.choice(pass_affected, n_remove, replace=False)
        df.loc[remove_idx, "MC_ID"] = "MC005"
        df.loc[remove_idx, "Cavity"] = "Cav_B"

    return df


def normalize_val(v: Any) -> Optional[str]:
    """统一数值格式：1.0 → 1，避免整型/浮点型分裂"""
    if pd.isna(v):
        return None
    try:
        f = float(v)
        if f.is_integer():
            return str(int(f))
        return str(f)
    except (ValueError, TypeError):
        s = str(v).strip()
        if s.lower() in ("nan", "none", "null", "nat", ""):
            return None
        return s


@st.cache_data(show_spinner=False, max_entries=1)
def load_dashboard_dict() -> Tuple[Optional[List[str]], Dict[str, str]]:
    """从 dashboard F11.xlsx 读取列名和含义映射"""
    if os.path.exists(DASHBOARD_PATH):
        try:
            df_dict = pd.read_excel(DASHBOARD_PATH)
            col_names = df_dict.iloc[:, 0].dropna().tolist()
            desc_map = {}
            for _, row in df_dict.iterrows():
                col = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else None
                desc = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else None
                if col and desc:
                    desc_map[col] = desc
            logger.info(f"成功加载 dashboard 字典，包含 {len(col_names)} 个列名")
            return col_names, desc_map
        except Exception as e:
            logger.error(f"读取 {DASHBOARD_PATH} 时发生异常: {str(e)}", exc_info=True)
    else:
        logger.warning(f"未能找到 Dashboard 字典文件: {DASHBOARD_PATH}")
    return None, {}


@st.cache_data(show_spinner="正在加载数据...", max_entries=2)
def load_data_from_path(
    file_path: str, usecols_tuple: Optional[Tuple[str, ...]], is_excel: bool = False
) -> pd.DataFrame:
    """从文件路径加载数据（缓存），支持 CSV, Excel, Parquet"""
    usecols = list(usecols_tuple) if usecols_tuple else None
    ext = file_path.lower()
    logger.info(f"开始加载数据文件: {file_path}")

    try:
        if is_excel or ext.endswith((".xlsx", ".xls")):
            picker = (lambda c: c in usecols) if usecols else None
            try:
                df = pd.read_excel(file_path, engine="calamine", usecols=picker)
            except (ImportError, ValueError):
                df = pd.read_excel(file_path, engine="openpyxl", usecols=picker)
        elif ext.endswith(".parquet"):
            df = pd.read_parquet(file_path, columns=usecols)
        else:
            df = pd.read_csv(file_path, usecols=usecols, low_memory=False)
        logger.info(f"成功加载文件 {file_path}，形状: {df.shape}")
        return df
    except Exception as e:
        logger.error(f"加载数据文件 {file_path} 失败: {str(e)}", exc_info=True)
        st.error(f"读取文件失败: {str(e)}")
        raise


def load_data(
    file_path: Optional[str] = None,
    uploaded_file: Any = None,
    dashboard_cols: Optional[List[str]] = None,
) -> Optional[pd.DataFrame]:
    """加载数据，优先使用上传文件，其次使用本地默认文件，支持 Parquet"""
    if uploaded_file is not None:
        name = uploaded_file.name.lower()
        logger.info(f"开始处理上传的文件: {name}")
        try:
            if name.endswith((".xlsx", ".xls")):
                try:
                    df = pd.read_excel(uploaded_file, engine="calamine")
                except (ImportError, ValueError):
                    df = pd.read_excel(uploaded_file, engine="openpyxl")
            elif name.endswith(".parquet"):
                df = pd.read_parquet(uploaded_file)
            else:
                df = pd.read_csv(uploaded_file, low_memory=False)
            logger.info(f"成功加载上传文件 {name}，形状: {df.shape}")
            return df
        except Exception as e:
            logger.error(f"读取上传文件 {name} 失败: {str(e)}", exc_info=True)
            st.error(f"处理上传文件异常: {str(e)}")
            return None

    if file_path and os.path.exists(file_path):
        ext = file_path.lower()
        is_excel = ext.endswith((".xlsx", ".xls"))
        is_parquet = ext.endswith(".parquet")

        if dashboard_cols:
            try:
                if is_excel:
                    try:
                        file_cols = pd.read_excel(
                            file_path, engine="calamine", nrows=1
                        ).columns.tolist()
                    except (ImportError, ValueError):
                        file_cols = pd.read_excel(
                            file_path, engine="openpyxl", nrows=1
                        ).columns.tolist()
                elif is_parquet:
                    # 快速读取 Parquet 列名
                    try:
                        import pyarrow.parquet as pq

                        file_cols = pq.read_table(
                            file_path, stop_at_metadata=True
                        ).column_names
                    except Exception:
                        file_cols = pd.read_parquet(file_path).columns.tolist()
                else:
                    file_cols = pd.read_csv(file_path, nrows=0).columns.tolist()
            except Exception as e:
                logger.error(
                    f"尝试读取文件 {file_path} 的列名失败: {str(e)}", exc_info=True
                )
                return None

            use_cols = [
                c for c in dashboard_cols if c in file_cols and c not in SKIP_COLS
            ]
            use_cols_extra = [
                c
                for c in ["Date", "Results", "Failed_Station", "Failure_Mode"]
                if c in file_cols
            ]
            for c in use_cols_extra:
                if c not in use_cols:
                    use_cols.append(c)
            return load_data_from_path(file_path, tuple(use_cols), is_excel=is_excel)
        return load_data_from_path(file_path, None, is_excel=is_excel)
    return None


def classify_columns(
    df: pd.DataFrame,
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """
    将列分为时间列、ID列、离散特征列、元数据列
    """
    time_cols = []
    id_cols = []
    meta_cols = []
    discrete_cols = []

    time_pattern = re.compile(r"_Time$|_time$|_Staging_time$", re.IGNORECASE)
    id_pattern = re.compile(r"^SN$", re.IGNORECASE)

    for col in df.columns:
        if col in SKIP_COLS:
            continue
        if col in ["Results", "Failed_Station", "Failure_Mode", "Date"]:
            meta_cols.append(col)
        elif id_pattern.match(col):
            id_cols.append(col)
        elif time_pattern.search(col):
            if re.search(r"_Staging_time$", col, re.IGNORECASE):
                discrete_cols.append(col)
            else:
                time_cols.append(col)
        else:
            discrete_cols.append(col)

    return time_cols, id_cols, meta_cols, discrete_cols


def compute_lift(
    df_base: pd.DataFrame,
    df_fail: pd.DataFrame,
    feature_cols: List[str],
    lift_weight: float = 0.3,
    min_count: int = 3,
    max_unique_ratio: float = 0.3,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    计算每个离散特征取值的提升度(Lift) — 向量化版本
    跳过唯一值过多的列、时间戳列、常量列
    返回：(lift_results, ratio_results, fail_one_results)
    """
    n_base = len(df_base)
    n_fail = len(df_fail)

    if n_fail == 0 or n_base == 0:
        return [], [], []

    max_unique = max(1000, int(n_base * max_unique_ratio))
    lift_results = []
    ratio_results = []
    fail_one_results = []

    time_pattern = re.compile(r"_Start_Time$|_End_Time$|_datetime$", re.IGNORECASE)

    total_cols = len(feature_cols)
    fail_ratio_weight = 1.0 - lift_weight

    logger.info(
        f"开始计算 Lift，基准样本量: {n_base}, Fail 样本量: {n_fail}, 待计算特征数: {total_cols}"
    )

    for idx, col in enumerate(feature_cols):
        if idx % 50 == 0:
            pct = min(idx / total_cols, 0.9)
            try:
                st.session_state["lift_progress"].progress(pct)
                st.session_state["lift_progress_text"].markdown(
                    f"计算提升度... ({idx}/{total_cols})"
                )
            except Exception as e:
                logger.debug(f"更新进度条失败: {str(e)}")

        if col not in df_fail.columns:
            continue

        if time_pattern.search(col):
            continue

        base_series = df_base[col]
        fail_series = df_fail[col]

        base_nunique = base_series.nunique(dropna=True)
        fail_nunique = fail_series.nunique(dropna=True)
        if base_nunique > max_unique:
            continue
        if base_nunique <= 1:
            continue

        if fail_nunique == 1:
            val = (
                fail_series.dropna().iloc[0]
                if fail_series.dropna().shape[0] > 0
                else None
            )
            val_str = normalize_val(val) or "缺失值"
            fail_one_results.append(
                {
                    "feature": col,
                    "value": val_str,
                    "fail_count": int(n_fail),
                    "fail_ratio": 1.0,
                }
            )
            continue

        # 向量化：一次性计算所有取值的 lift
        base_counts = base_series.value_counts(dropna=False)
        fail_counts = fail_series.value_counts(dropna=False)

        # 对齐索引
        all_vals = fail_counts.index
        base_aligned = base_counts.reindex(all_vals, fill_value=0)

        # 向量化计算
        fail_cnts = fail_counts.values
        base_cnts = base_aligned.values
        p_fail_arr = fail_cnts / n_fail
        p_base_arr = base_cnts / n_base

        # 掩码：满足最小计数、基准>0、非缺失值
        mask = (fail_cnts >= min_count) & (base_cnts > 0)

        for i, val in enumerate(all_vals):
            if not mask[i]:
                continue

            val_str = normalize_val(val)
            if val_str is None or val_str == "缺失值":
                continue
            if len(val_str) > 200:
                val_str = val_str[:197] + "..."

            lift = p_fail_arr[i] / p_base_arr[i] if p_base_arr[i] > 0 else 0
            fail_ratio = p_fail_arr[i]
            composite_score = lift_weight * lift + fail_ratio_weight * (
                fail_ratio * 100
            )

            if lift > 1.0:
                lift_results.append(
                    {
                        "feature": col,
                        "value": val_str,
                        "fail_count": int(fail_cnts[i]),
                        "base_count": int(base_cnts[i]),
                        "p_fail": round(float(p_fail_arr[i]), 6),
                        "p_base": round(float(p_base_arr[i]), 6),
                        "lift": round(float(lift), 4),
                        "fail_ratio": round(float(fail_ratio), 4),
                        "composite_score": round(float(composite_score), 4),
                    }
                )

            ratio_results.append(
                {
                    "feature": col,
                    "value": val_str,
                    "fail_count": int(fail_cnts[i]),
                    "base_count": int(base_cnts[i]),
                    "p_fail": round(float(p_fail_arr[i]), 6),
                    "p_base": round(float(p_base_arr[i]), 6),
                    "fail_ratio": round(float(fail_ratio), 4),
                    "composite_score": round(float(composite_score), 4),
                }
            )

    lift_results.sort(key=lambda x: x["composite_score"], reverse=True)
    ratio_results.sort(key=lambda x: x["fail_ratio"], reverse=True)
    return lift_results, ratio_results, fail_one_results


def is_col_empty(series):
    """判断一列是否完全为空（包括真正NaN、空字符串、英文nan/none、中文'缺失值'等占位符）"""
    s = series.dropna()
    if s.empty:
        return True
    s_str = s.astype(str).str.strip().str.lower()
    s_valid = s_str[~s_str.isin(["", "nan", "none", "缺失值", "null"])]
    return s_valid.empty


def get_top10_by_feature(
    results, sort_key="composite_score"
):
    """每个特征只保留指定指标最高的一个取值，按该指标降序取TOP 10"""
    best_per_feature = {}
    for r in results:
        feat = r["feature"]
        if (
            feat not in best_per_feature
            or r[sort_key] > best_per_feature[feat][sort_key]
        ):
            best_per_feature[feat] = r
    sorted_best = sorted(
        best_per_feature.values(), key=lambda x: x[sort_key], reverse=True
    )
    return sorted_best[:10]


def call_llm(
    api_key: str,
    api_base: str,
    model: str,
    top10_data: List[Dict[str, Any]],
    desc_map: Dict[str, str],
    failed_station: Optional[str],
    failure_mode: Optional[str],
    fail_one_data: Optional[List[Dict[str, Any]]] = None,
    hour_top10_data: Optional[List[Dict[str, Any]]] = None,
    lgb_imp_data: Optional[pd.DataFrame] = None,
    lgb_combo_data: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """
    调用LLM生成合并了统计分析与机器学习诊断的全面质量报告
    """
    import requests
    import json

    if not api_key or len(top10_data) == 0:
        return None

    top10_with_desc = []
    for item in top10_data:
        feat = item["特征列"]
        desc = desc_map.get(feat, "无描述")
        top10_with_desc.append(
            {
                "特征列": feat,
                "特征含义": desc,
                "聚集取值": item["聚集取值"],
                "综合评分": item["综合评分"],
                "提升度Lift": item["提升度Lift"],
                "Fail内占比": item["Fail内占比"],
                "Fail出现次数": item["Fail出现次数"],
                "基准出现次数": item["基准出现次数"],
                "Fail集中度": item["Fail集中度"],
                "基准占比": item["基准占比"],
            }
        )

    top10_text = json.dumps(top10_with_desc, ensure_ascii=False, indent=2)

    fail_one_text = ""
    if fail_one_data and len(fail_one_data) > 0:
        fail_one_with_desc = []
        for item in fail_one_data:
            feat = item["feature"]
            desc = desc_map.get(feat, "无描述")
            fail_one_with_desc.append(
                {
                    "特征列": feat,
                    "特征含义": desc,
                    "聚集取值": item["value"],
                    "Fail出现次数": item["fail_count"],
                }
            )
        fail_one_text = (
            "\n\n#### ⚠️ Fail 内完全一致的特征（唯一值=1）\n以下特征在当前 Fail 样本中只有一个唯一取值（100%集中），建议重点关注：\n"
            + json.dumps(fail_one_with_desc, ensure_ascii=False, indent=2)
        )

    hour_text = ""
    if hour_top10_data and len(hour_top10_data) > 0:
        hour_items = []
        for item in hour_top10_data:
            hour_items.append(
                {
                    "工序": item.get("original_feature", item["feature"].replace("_Day", "")),
                    "聚集日期": item["value"],
                    "Fail内占比": f"{item['fail_ratio'] * 100:.1f}%",
                    "Fail次数": item["fail_count"],
                }
            )
        hour_text = (
            "\n\n#### ⏰ 时间日期异常聚集 (按天聚合)\n以下为按天聚合的时间类制程因素中，Fail 高度集中的日期：\n"
            + json.dumps(hour_items, ensure_ascii=False, indent=2)
        )

    # 💡 融入 LightGBM 的 AI 诊断特征重要性与高危组合数据
    lgb_imp_text = "未进行 AI 特征重要性分析"
    if lgb_imp_data is not None and not lgb_imp_data.empty:
        lgb_imp_with_desc = []
        for _, row in lgb_imp_data.head(10).iterrows():
            feat = row["特征列"]
            desc = desc_map.get(feat, "无描述")
            lgb_imp_with_desc.append({
                "特征列": feat,
                "特征含义": desc,
                "信息增益 (Gain)": float(row["信息增益 (Gain)"])
            })
        lgb_imp_text = json.dumps(lgb_imp_with_desc, ensure_ascii=False, indent=2)

    lgb_combo_text = "未发现显著的交叉致死因子组合"
    if lgb_combo_data:
        lgb_combo_text = json.dumps(lgb_combo_data[:10], ensure_ascii=False, indent=2)

    prompt = f"""[角色定位]
你是一名卓越的 3C 智能制造与质量工程专家。当前任务是针对产线终检 Fail 样本进行全流程多维特征的共性聚集度分析与机器学习决策树特征重要性诊断。

[当前筛选上下文]
- Failed_Station (故障工位): {failed_station if failed_station else "ALL"}
- Failure_Mode (失效模式): {failure_mode if failure_mode else "ALL"}

[输入数据源]

【数据源 A：传统统计共性聚集度分析（Lift 提升度）】
以下是当前故障工位/失效模式下，故障占比与提升度最高的 Top 10 显性聚集因子数据：
{top10_text}
{fail_one_text}
{hour_text}

【数据源 B：LightGBM 决策树机器学习根因诊断】
以下是分类模型输出的 Top 10 关键根因特征（基于树分裂信息增益 Gain 排序，客观反映特征对 Pass/Fail 状态的强区分贡献度）：
{lgb_imp_text}

以下是模型挖掘出的高危多因子交叉组合规则（这些特征的多维复合交集在数据回测中表现出极高的致死率）：
{lgb_combo_text}

[分析准则与输出范式]
本报告面向数字化制造决策层。撰写过程必须遵循「高信息密度、强数据互证、严密因果逻辑、客观精炼」的工业诊断标准。请对上述统计聚集与机器学习模型计算结果进行深度的交叉验证与逻辑推导，直接输出强确定性的根因定性结论与数据关联说明。
- 严禁包含任何文学性修饰、抒情性前言或行业通识性铺垫。
- 本报告聚焦于“根因定位”，严禁输出任何发散性的改善步骤、现场行动建议或车间验证建议。
- 生成的 Markdown 文档中，各部分的标题层级必须使用 #### 或 #####，且报告结构必须严格包含以下三大专业板块：

#### 🎯 异常数据聚集与特征关联分析
（基于数据源 A，提炼单因子共性聚集特征与时间天级聚合异常趋势，指出故障样本高度集中的设备 ID、来料批次等制程设定，必须引用完整特征列名与具体量化数据）

#### 🤖 AI 根因模型与高危复合多因子分析
（基于数据源 B，交叉解读决策树特征重要性排行榜与多因子高危交叉组合规则，剖析哪些工艺因子在何种联合条件下发生异常交集，并指出其背后的数据致死概率）

#### 🔎 核心 NG 故障根因定性结论
（综合上述统计共性与机器学习决策树的计算共识，进行高可信度的根因归纳，用 1~2 句话直接阐明本次大批量产品发生 NG 失效的根本动力学源头与物理/工艺失准点，直奔靶心）

[语言约束]
请使用简体中文输出。分析应极其严谨专业，直陈其事。在提到任何特征字段时，必须使用数据源中的完整原始特征列名称，严禁缩写或更改。"""

    url = f"{api_base.rstrip('/')}/chat/completions"

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            err_detail = response.text[:500]
            return f"LLM调用失败 (HTTP {response.status_code}): {err_detail}"
    except requests.exceptions.Timeout:
        return "LLM调用超时（90秒），请检查网络或稍后重试。"
    except Exception as e:
        return f"LLM调用异常: {str(e)[:300]}"


def _get_cache_key(
    start_date: Any, end_date: Any, failed_station: str, lift_weight: float, time_resolution: str
) -> str:
    """生成计算结果的缓存键"""
    return f"{start_date}_{end_date}_{failed_station}_{lift_weight}_{time_resolution}"


def extract_time_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """检测时间列，抽取天(Day)作为离散特征，返回(修改后df, 新增列名列表)
    只处理 _End_Time 列（实际时间戳），不处理 _Staging_time（持续秒数）和 _Start_Time
    """
    time_day_cols: List[str] = []
    time_pattern = re.compile(r"_End_Time$|_datetime$", re.IGNORECASE)
    n = len(df)
    if n == 0:
        return df, time_day_cols

    for col in list(df.columns):
        if col in SKIP_COLS or not time_pattern.search(col):
            continue
        try:
            s = df[col]
            if s.dropna().empty:
                continue

            # 强制转换为 datetime 对象
            parsed = pd.to_datetime(s, errors="coerce")
            valid_ratio = parsed.notna().sum() / n
            if valid_ratio < 0.3:
                continue

            # 保留原时间列为 datetime 格式，方便后续趋势图计算
            df[col] = parsed

            # 生成天区间起点
            col_day = f"{col}_Day"
            df[col_day] = parsed.dt.normalize()
            time_day_cols.append(col_day)

        except Exception as e:
            logger.warning(f"处理时间列 {col} 时发生异常，跳过该列: {str(e)}")
            continue
    return df, time_day_cols


def list_available_models(api_key: str, api_base: str) -> List[str]:
    """调用 OpenAI 兼容 API 的 /models 端点，返回可用模型 ID 列表"""
    import requests

    if not api_key or not api_base:
        return []
    url = f"{api_base.rstrip('/')}/models"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json().get("data", [])
            return sorted([m["id"] for m in data if "id" in m])
        else:
            logger.warning(f"获取模型列表失败 (HTTP {response.status_code}): {response.text[:200]}")
            return []
    except Exception as e:
        logger.warning(f"获取模型列表异常: {str(e)[:200]}")
        return []


def main():
    st.title("终检Fail产品全流程共性聚集度分析")
    st.markdown(
        "基于**提升度 (Lift)** 算法，从全流程 ~500 个离散工序因素中识别导致终检 Fail 的异常聚集特征。"
    )

    # ── 侧边栏：数据源 ──
    st.sidebar.header("数据源")
    data_source = st.sidebar.radio(
        "选择数据源",
        options=["模拟数据（展示用）", "本地 PRB数据.csv", "上传外部数据"],
        index=0,
        help="选择本地 CSV 数据或上传自定义数据文件进行分析。"
    )

    uploaded_file = None
    use_mock = data_source == "模拟数据（展示用）"
    if data_source == "上传外部数据":
        uploaded_file = st.sidebar.file_uploader(
            "上传数据文件", type=["csv", "xlsx", "xls", "parquet"]
        )

    if LLM_API_KEY and LLM_API_KEY != "sk-your-api-key-here":
        available_models = list_available_models(LLM_API_KEY, LLM_API_BASE)
        if available_models:
            if LLM_MODEL in available_models:
                st.sidebar.success(f"LLM: {LLM_MODEL}")
            else:
                st.sidebar.error(f"模型 {LLM_MODEL} 无权访问")
                with st.sidebar.expander("可用模型列表", expanded=True):
                    for m in available_models:
                        st.code(m)
        else:
            st.sidebar.warning(f"LLM: {LLM_MODEL} (无法获取模型列表)")

    # ── 数据源选择变化时清理缓存 ──
    if st.session_state.get("last_data_source") != data_source:
        st.session_state.pop("df_raw", None)
        st.session_state.pop("uploaded_name", None)
        st.session_state["last_data_source"] = data_source
        # 清理分析缓存
        for k in [
            "lift_results", "ratio_results", "fail_one_results", "top10",
            "df_base", "df_fail", "n_base", "n_fail", "all_feature_cols",
            "hour_lift_results", "hour_top10", "hour_ratio_results", "hour_ratio_top10"
        ]:
            st.session_state.pop(k, None)

    # ── 数据加载 ──
    df_raw = None
    desc_map = {}
    dashboard_cols = None

    if use_mock:
        if "df_raw" not in st.session_state or st.session_state.get("last_data_source") != data_source:
            with st.spinner("正在生成模拟数据..."):
                st.session_state["df_raw"] = generate_mock_data(10000)
        st.sidebar.success("使用模拟数据（展示效果用）")
        st.sidebar.caption(f"模拟行数: {len(st.session_state['df_raw']):,}")
        df_raw = st.session_state["df_raw"]

    elif data_source == "本地 PRB数据.csv":
        dashboard_cols, desc_map = load_dashboard_dict()
        if dashboard_cols:
            st.sidebar.caption(f"Dashboard 特征列: {len(dashboard_cols)} 列")
        if "df_raw" not in st.session_state:
            with st.spinner("正在加载本地数据..."):
                st.session_state["df_raw"] = load_data(
                    file_path=DATA_PATH, dashboard_cols=dashboard_cols
                )
        df_raw = st.session_state["df_raw"]
        
    elif data_source == "上传外部数据":
        dashboard_cols, desc_map = load_dashboard_dict()
        if dashboard_cols:
            st.sidebar.caption(f"Dashboard 特征列: {len(dashboard_cols)} 列")
        if uploaded_file is not None:
            if "df_raw" not in st.session_state or st.session_state.get("uploaded_name") != uploaded_file.name:
                with st.spinner("正在加载上传文件..."):
                    st.session_state["df_raw"] = load_data(uploaded_file=uploaded_file)
                    st.session_state["uploaded_name"] = uploaded_file.name
            df_raw = st.session_state["df_raw"]

    if df_raw is None:
        if use_mock:
            st.error("模拟数据生成失败。")
        elif data_source == "本地 PRB数据.csv":
            file_exists = os.path.exists(DATA_PATH)
            dashboard_exists = os.path.exists(DASHBOARD_PATH)
            st.error(
                f"未能加载本地数据。\n- PRB数据.csv 存在: {file_exists}\n- dashboard F11.xlsx 存在: {dashboard_exists}"
            )
            st.info("请确保本地数据文件放置在正确路径。")
        elif data_source == "上传外部数据":
            st.info("请在左侧上传并加载外部数据文件（支持 CSV, Excel, Parquet）。")
        return

    # ── 侧边栏：数据概览 ──
    with st.sidebar.expander("数据概览", expanded=False):
        st.write(f"总行数: {len(df_raw):,}")
        st.write(f"总列数: {len(df_raw.columns)}")

    # ── 校验必要列 ──
    required_cols = ["Results", "Failed_Station", "Failure_Mode"]
    missing_required = [c for c in required_cols if c not in df_raw.columns]
    if missing_required:
        st.error(
            f"数据缺少必要列: {missing_required}。可用列: {list(df_raw.columns)[:30]}"
        )
        return

    # ── 时间列确定 ──
    date_col = "Date"
    if date_col not in df_raw.columns:
        candidates = [c for c in df_raw.columns if "date" in c.lower()]
        if candidates:
            date_col = st.sidebar.selectbox("选择日期列", candidates)
        else:
            st.error("未找到 Date 列，无法按日期筛选。")
            return

    # ── 侧边栏：筛选条件 ──
    st.sidebar.header("筛选条件")

    # 智能检测并进行日期转换，确保无论何时重载均能正确识别
    if not pd.api.types.is_datetime64_any_dtype(df_raw[date_col]):
        df_raw[date_col] = pd.to_datetime(df_raw[date_col], errors="coerce")

    if df_raw[date_col].isna().all():
        st.error(f"日期列 `{date_col}` 所有值转换后均为空，请检查日期格式。")
        return
    date_min = df_raw[date_col].min().date()
    date_max = df_raw[date_col].max().date()

    date_range = st.sidebar.date_input(
        "日期范围", value=(date_min, date_max), min_value=date_min, max_value=date_max
    )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    elif hasattr(date_range, "__iter__") and not isinstance(date_range, str):
        dr_list = list(date_range)
        start_date, end_date = dr_list[0], dr_list[-1]
    else:
        start_date, end_date = date_min, date_max

    st.sidebar.caption(f"数据日期范围: {date_min} → {date_max}")

    # ── 分析配置 ──
    st.sidebar.header("分析配置")
    min_unique_values = 2

    st.sidebar.markdown("**综合评分权重**")
    lift_weight = st.sidebar.slider(
        "Lift 权重",
        min_value=0.0,
        max_value=1.0,
        value=1.0,
        step=0.05,
        help="Lift 在综合评分中的权重，Fail内占比自动占剩余权重",
    )
    fail_ratio_weight = 1.0 - lift_weight
    st.sidebar.caption(f"Fail内占比权重: {fail_ratio_weight:.2f}")

    st.sidebar.markdown("**时间分辨率**")
    time_resolution = st.sidebar.select_slider(
        "时间趋势轴分辨率",
        options=["半小时", "1小时", "2小时"],
        value="半小时",
        help="设置 Tab 4 时间趋势图中横轴的时间聚合分辨率（每天 24 小时内的聚合跨度）",
    )

    # 日期筛选（缓存）
    date_mask = (df_raw[date_col].dt.date >= start_date) & (
        df_raw[date_col].dt.date <= end_date
    )
    df_date_filtered = df_raw[date_mask]

    if len(df_date_filtered) == 0:
        st.warning("所选日期范围内无数据，请调整日期范围。")
        return

    station_values = [x for x in df_date_filtered["Failed_Station"].dropna().unique() if str(x).strip() != ""]
    station_options = ["全部"] + sorted([str(x) for x in station_values])
    failed_station = st.sidebar.selectbox("Failed_Station", station_options, index=0)

    # ── 开始分析按钮 ──
    analyze_btn = st.sidebar.button(
        "开始分析", type="primary", use_container_width=True
    )

    # 生成缓存键
    cache_key = _get_cache_key(start_date, end_date, failed_station, lift_weight, time_resolution)

    # 如果参数变化，清除旧缓存
    if st.session_state.get("last_cache_key") != cache_key:
        for k in [
            "lift_results",
            "ratio_results",
            "fail_one_results",
            "top10",
            "df_base",
            "df_fail",
            "n_base",
            "n_fail",
            "all_feature_cols",
            "hour_lift_results",
            "hour_top10",
            "hour_ratio_results",
            "hour_ratio_top10",
            "df_imp",
            "combo_results",
            "comprehensive_llm_report",
        ]:
            st.session_state.pop(k, None)
        st.session_state["last_cache_key"] = cache_key

    # 有缓存时直接显示结果，无需再次点击按钮
    has_cache = "lift_results" in st.session_state

    if not analyze_btn and not has_cache:
        st.info("请在左侧配置筛选条件后，点击「**开始分析**」按钮。")
        with st.expander("数据预览（前 50 行）"):
            st.dataframe(df_raw.head(50), use_container_width=True)
        return

    # ═══════════════════════════════════════════════
    # 核心分析流程
    # ═══════════════════════════════════════════════

    if has_cache and not analyze_btn:
        # 使用缓存结果
        lift_results = st.session_state["lift_results"]
        ratio_results = st.session_state["ratio_results"]
        fail_one_results = st.session_state["fail_one_results"]
        top10 = st.session_state["top10"]
        df_base = st.session_state["df_base"]
        df_fail = st.session_state["df_fail"]
        n_base = st.session_state["n_base"]
        n_fail = st.session_state["n_fail"]
        all_feature_cols = st.session_state["all_feature_cols"]
        hour_lift_results = st.session_state.get("hour_lift_results", [])
        hour_top10 = st.session_state.get("hour_top10", [])
        hour_ratio_results = st.session_state.get("hour_ratio_results", [])
        hour_ratio_top10 = st.session_state.get("hour_ratio_top10", [])
        df_imp = st.session_state.get("df_imp", pd.DataFrame())
        combo_results = st.session_state.get("combo_results", [])
    else:
        # 重新计算
        with st.spinner("正在准备数据..."):
            if failed_station != "全部":
                df_base = df_date_filtered[
                    (df_date_filtered["Results"] == "PASS")
                    | (
                        (df_date_filtered["Results"] == "FAIL")
                        & (df_date_filtered["Failed_Station"] == failed_station)
                    )
                ].copy()
                df_fail = df_date_filtered[
                    (df_date_filtered["Results"] == "FAIL")
                    & (df_date_filtered["Failed_Station"] == failed_station)
                ].copy()
            else:
                df_base = df_date_filtered.copy()
                df_fail = df_date_filtered[df_date_filtered["Results"] == "FAIL"].copy()

            n_base = len(df_base)
            n_fail = len(df_fail)

        if n_fail == 0:
            st.error(
                "## 无Fail数据\n当前筛选条件下 Fail 产品数量为 0，请调整筛选条件。"
            )
            return

        fail_rate = n_fail / n_base * 100

        # 概览指标卡
        col1, col2, col3 = st.columns(3)
        col1.metric("基准总数", f"{n_base:,}")
        col2.metric("Fail数量", f"{n_fail:,}")
        col3.metric("Fail率", f"{fail_rate:.2f}%")

        if dashboard_cols:
            all_feature_cols = [
                c for c in dashboard_cols if c in df_base.columns and c not in SKIP_COLS
            ]
        else:
            time_cols, id_cols, meta_cols, discrete_cols = classify_columns(df_base)
            all_feature_cols = [c for c in discrete_cols if c in df_base.columns]

        filtered_cols = []
        for col in all_feature_cols:
            if col in df_base.columns and col in df_fail.columns:
                if not is_col_empty(df_base[col]) and not is_col_empty(df_fail[col]):
                    if df_base[col].nunique(dropna=True) >= 2:
                        filtered_cols.append(col)
        all_feature_cols = filtered_cols

        st.info(
            f"**特征统计**：参与 Lift 计算的工序离散特征共 {len(all_feature_cols)} 列（已排除常量列）"
        )
        st.info(
            f"**综合评分权重**：Lift={lift_weight:.0%} | Fail内占比={fail_ratio_weight:.0%}"
        )

        lift_progress = st.progress(0)
        lift_progress_text = st.empty()
        lift_progress_text.markdown("计算提升度...")
        st.session_state["lift_progress"] = lift_progress
        st.session_state["lift_progress_text"] = lift_progress_text

        with st.spinner("正在计算全特征提升度（Lift）..."):
            lift_results, ratio_results, fail_one_results = compute_lift(
                df_base, df_fail, all_feature_cols, lift_weight=lift_weight, min_count=3
            )

        lift_progress_text.markdown("正在提取时间按天聚合特征(仅针对Fail数据)...")
        time_cols, _, _, _ = classify_columns(df_fail)
        dayhour_ratio_results = []

        if time_cols:
            df_fail, dayhour_cols = extract_time_features(df_fail)
            if dayhour_cols:
                lift_progress_text.markdown("正在计算时间类Fail占比（按天）...")
                for col_day in dayhour_cols:
                    original_col = col_day.replace("_Day", "")
                    fail_series = df_fail[col_day].dropna()
                    if len(fail_series) == 0:
                        continue
                    fail_counts = fail_series.value_counts()
                    for val_dt, count in fail_counts.items():
                        val_str = val_dt.strftime('%Y-%m-%d')
                        
                        fail_ratio = count / n_fail
                        dayhour_ratio_results.append(
                            {
                                "feature": col_day,
                                "original_feature": original_col,
                                "value": val_str,
                                "value_dt": val_dt,
                                "fail_count": int(count),
                                "base_count": "-",
                                "p_fail": float(fail_ratio),
                                "p_base": 0.0,
                                "fail_ratio": float(fail_ratio),
                                "lift": "-",
                                "composite_score": float(fail_ratio),
                            }
                        )

        dayhour_lift_results = []
        dayhour_ratio_top10 = (
            get_top10_by_feature(dayhour_ratio_results, sort_key="fail_ratio")
            if dayhour_ratio_results
            else []
        )
        dayhour_top10 = dayhour_ratio_top10


        lift_progress.progress(1.0)
        lift_progress_text.markdown("分析完成!")

        top10 = get_top10_by_feature(
            [r for r in lift_results if r["fail_ratio"] > 0.10]
        )

        # 缓存到 session_state
        st.session_state["lift_results"] = lift_results
        st.session_state["ratio_results"] = ratio_results
        st.session_state["fail_one_results"] = fail_one_results
        st.session_state["top10"] = top10
        st.session_state["df_base"] = df_base
        st.session_state["df_fail"] = df_fail
        st.session_state["n_base"] = n_base
        st.session_state["n_fail"] = n_fail
        st.session_state["all_feature_cols"] = all_feature_cols
        st.session_state["hour_lift_results"] = dayhour_lift_results
        st.session_state["hour_top10"] = dayhour_top10
        st.session_state["hour_ratio_results"] = dayhour_ratio_results
        st.session_state["hour_ratio_top10"] = dayhour_ratio_top10


        hour_ratio_results = dayhour_ratio_results
        hour_top10 = dayhour_top10

    # ── 同步进行 ML 计算与大模型根因诊断（消除多线程与轮询，确保 UI 骨架绝对一致，杜绝分身与嵌套问题） ──
    if "df_imp" not in st.session_state:
        df_imp_res = pd.DataFrame()
        combo_res = []
        
        if len(df_fail) >= 10 and len(df_base) >= 10:
            with st.spinner("🤖 正在进行 AI 决策树分类与高危组合特征挖掘..."):
                try:
                    import lightgbm as lgb
                    df_base_ml = df_base.copy()
                    df_fail_ml = df_fail.copy()
                    df_ml = pd.concat([df_base_ml, df_fail_ml], ignore_index=True)
                    
                    y = df_ml["Results"].map({"PASS": 0, "FAIL": 1})
                    features_to_use = []
                    import re
                    for c in all_feature_cols:
                        c_lower = c.lower()
                        if c_lower == "date" or (re.search(r"_time$", c, re.IGNORECASE) and not re.search(r"_staging_time$", c, re.IGNORECASE)):
                            continue
                        if c_lower == "sn" or c_lower.endswith("_sn") or c_lower.startswith("sn_") or "serial" in c_lower or "barcode" in c_lower:
                            continue
                        if df_ml[c].nunique(dropna=True) > 0.9 * len(df_ml):
                            continue
                        features_to_use.append(c)
                        
                    X = df_ml[features_to_use].copy()
                    for col in X.columns:
                        X[col] = X[col].fillna("缺失值").astype(str).astype('category')
                        
                    clf = lgb.LGBMClassifier(
                        objective='binary',
                        class_weight='balanced',
                        n_estimators=100,
                        importance_type='gain',
                        n_jobs=-1,
                        random_state=42,
                        verbose=-1
                    )
                    clf.fit(X, y)
                    imp_vals = clf.booster_.feature_importance(importance_type='gain')
                    df_imp_res = pd.DataFrame({
                        '特征列': X.columns,
                        '信息增益 (Gain)': imp_vals
                    })
                    df_imp_res = df_imp_res[df_imp_res['信息增益 (Gain)'] > 0]
                    df_imp_res = df_imp_res.sort_values(by='信息增益 (Gain)', ascending=False)
                    
                    if not df_imp_res.empty:
                        top_features = df_imp_res.head(8)['特征列'].tolist()
                        import itertools
                        X_str = X[top_features].astype(str)
                        X_fail_str = X_str[y == 1]
                        X_base_str = X_str[y == 0]
                        min_fail_count = max(5, int(len(X_fail_str) * 0.01))
                        if min_fail_count > 20:
                            min_fail_count = 20
                            
                        for k in [2, 3, 4]:
                            if len(top_features) < k:
                                break
                            for combo_cols in itertools.combinations(top_features, k):
                                combo_cols = list(combo_cols)
                                fail_vc = X_fail_str.groupby(combo_cols).size()
                                fail_vc = fail_vc[fail_vc >= min_fail_count]
                                if fail_vc.empty:
                                    continue
                                base_vc = X_base_str.groupby(combo_cols).size()
                                
                                for val_tuple, f_cnt in fail_vc.items():
                                    b_cnt = base_vc.get(val_tuple, 0)
                                    total_cnt = f_cnt + b_cnt
                                    ratio = f_cnt / total_cnt
                                    if ratio >= 0.2:
                                        rule_parts = []
                                        for col, val in zip(combo_cols, val_tuple):
                                            col_name = col
                                            val_display = "缺失值" if val == "nan" else val
                                            rule_parts.append(f"{col_name}='{val_display}'")
                                        rule_text = " 且 ".join(rule_parts)
                                        combo_res.append({
                                            "维度": f"{k}维",
                                            "高危组合条件": rule_text,
                                            "Fail概率": float(ratio),
                                            "Fail次数": int(f_cnt),
                                            "基准次数": int(b_cnt)
                                        })
                except Exception as e:
                    logger.warning(f"LightGBM 自动分类训练失败: {str(e)}")
                    df_imp_res = pd.DataFrame()
                    combo_res = []

        st.session_state["df_imp"] = df_imp_res
        st.session_state["combo_results"] = combo_res
        
        # 同步进行 LLM 调用
        llm_report = "系统未配置有效 LLM_API_KEY，已跳过大模型质量诊断报告生成。"
        if LLM_API_KEY and LLM_API_KEY != "sk-your-api-key-here" and len(top10) > 0:
            with st.spinner("🔮 正在调用大模型进行智能诊断并生成排查行动指南..."):
                try:
                    top10_for_llm = [
                        {
                            "排位": i + 1,
                            "特征列": item["feature"],
                            "聚集取值": item["value"],
                            "综合评分": item["composite_score"],
                            "提升度Lift": item["lift"],
                            "Fail内占比": f"{item['fail_ratio'] * 100:.1f}%",
                            "Fail出现次数": item["fail_count"],
                            "基准出现次数": item["base_count"],
                            "Fail集中度": f"{item['p_fail'] * 100:.2f}%",
                            "基准占比": f"{item['p_base'] * 100:.2f}%",
                        }
                        for i, item in enumerate(top10)
                    ]

                    hour_top10_for_llm = (
                        [
                            {
                                "feature": item["feature"],
                                "value": item["value"],
                                "lift": item["lift"],
                                "fail_ratio": item["fail_ratio"],
                                "fail_count": item["fail_count"],
                            }
                            for item in hour_top10
                        ]
                        if hour_top10
                        else None
                    )

                    llm_report = call_llm(
                        LLM_API_KEY,
                        LLM_API_BASE,
                        LLM_MODEL,
                        top10_for_llm,
                        desc_map,
                        failed_station,
                        "全部",
                        fail_one_data=fail_one_results,
                        hour_top10_data=hour_top10_for_llm,
                        lgb_imp_data=df_imp_res,
                        lgb_combo_data=combo_res,
                    )
                    if not llm_report:
                        llm_report = "生成大模型解读报告失败。"
                except Exception as e:
                    llm_report = f"LLM 调用异常: {str(e)[:300]}"
        
        st.session_state["comprehensive_llm_report"] = llm_report

    # 统一显示指标卡，保证 DOM 结构一致避免页面刷新错位
    fail_rate = n_fail / n_base * 100 if n_base > 0 else 0
    col1, col2, col3 = st.columns(3)
    col1.metric("基准总数", f"{n_base:,}")
    col2.metric("Fail数量", f"{n_fail:,}")
    col3.metric("Fail率", f"{fail_rate:.2f}%")

    st.success(
        f"共发现 **{len(lift_results)}** 条聚集特征（Lift > 1.0 且 Fail 出现 ≥ 3 次）"
    )
    st.info(f"每个特征取综合评分最高代表 → **TOP {len(top10)}** 特征")

    # ═══════════════════════════════════════════════
    # 可视化图表区域 (Tabs)
    # ═══════════════════════════════════════════════

    st.markdown("---")
    st.subheader("可视化分析")

    if n_fail < 10:
        st.warning(
            f"Fail 样本量仅 {n_fail}，Lift 分析无统计意义，直接展示 Fail 内分布："
        )
        small_sample_results = []
        for col in all_feature_cols:
            if col not in df_fail.columns:
                continue
            vc = df_fail[col].value_counts(dropna=False)
            for val, count in vc.items():
                val_str = normalize_val(val)
                if val_str is None:
                    continue
                small_sample_results.append(
                    {
                        "feature": col,
                        "value": val_str,
                        "fail_count": int(count),
                        "fail_ratio": round(count / n_fail, 4),
                    }
                )
        small_sample_results.sort(key=lambda x: x["fail_count"], reverse=True)
        if small_sample_results:
            top_n = small_sample_results[:50]
            df_ss = pd.DataFrame(top_n)
            df_ss["Fail内占比"] = df_ss["fail_ratio"].apply(lambda x: f"{x * 100:.1f}%")
            df_chart = df_ss.copy()
            df_chart["label"] = df_chart.apply(
                lambda r: f"{r['feature'][:50]} → {r['value']}", axis=1
            )
            df_chart = df_chart.sort_values("fail_count", ascending=True)
            fig = go.Figure()
            fig.add_trace(
                go.Bar(
                    y=df_chart["label"],
                    x=df_chart["fail_count"],
                    orientation="h",
                    marker=dict(
                        color=df_chart["fail_ratio"],
                        colorscale="Blues",
                        showscale=True,
                        colorbar=dict(title="Fail占比"),
                    ),
                    text=df_chart["Fail内占比"],
                    textposition="outside",
                    hovertemplate="<b>特征</b>: %{customdata[0]}<br><b>取值</b>: %{customdata[1]}<br><b>Fail次数</b>: %{x}<br><b>Fail内占比</b>: %{customdata[2]}<extra></extra>",
                    customdata=df_chart[["feature", "value", "Fail内占比"]].values,
                )
            )
            fig.update_layout(
                title=f"Fail 样本分布（共 {n_fail} 条）",
                xaxis_title="Fail出现次数",
                yaxis=dict(title="", tickfont=dict(size=9), automargin=True),
                height=max(400, len(top_n) * 18),
                margin=dict(r=120, t=50, b=20),
            )
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(
                df_ss[["feature", "value", "fail_count", "Fail内占比"]].rename(
                    columns={
                        "feature": "特征列",
                        "value": "聚集取值",
                        "fail_count": "Fail次数",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
        return

    if len(top10) == 0:
        st.warning("未发现显著的聚集特征（所有 Lift 值均 ≤ 1.0）。")
        return

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "📈 共性聚集度排行榜(Lift)",
            "📉 Fail内占比排行榜",
            "🔍 单因子 Pass/Fail 对比",
            "⏰ 时间小时聚集度(Hour)",
            "🤖 AI 根因诊断 (LightGBM)",
        ]
    )

    # ── Tab 1: TOP 10 水平条形图 ──
    with tab1:
        df_chart = pd.DataFrame(top10)
        df_chart["label"] = df_chart.apply(
            lambda r: f"{r['feature'][:55]} → {r['value']}", axis=1
        )
        df_chart = df_chart.sort_values("lift", ascending=True)

        bar_fig = go.Figure()
        bar_fig.add_trace(
            go.Bar(
                y=df_chart["label"],
                x=df_chart["lift"],
                orientation="h",
                marker=dict(
                    color=df_chart["lift"],
                    colorscale="Reds",
                    showscale=True,
                    colorbar=dict(title="Lift"),
                ),
                text=df_chart.apply(
                    lambda r: f"Lift={r['lift']:.1f} | Fail#{r['fail_count']}", axis=1
                ),
                textposition="outside",
                textfont=dict(size=11),
                hovertemplate=(
                    "<b>特征列</b>: %{customdata[0]}<br>"
                    "<b>聚集取值</b>: %{customdata[1]}<br>"
                    "<b>Lift</b>: %{x:.2f}<br>"
                    "<b>Fail次数</b>: %{customdata[2]}<br>"
                    "<b>基准次数</b>: %{customdata[3]}<br>"
                    "<b>Fail集中度</b>: %{customdata[4]:.2%}<br>"
                    "<b>基准占比</b>: %{customdata[5]:.2%}<br>"
                    "<extra></extra>"
                ),
                customdata=df_chart[
                    ["feature", "value", "fail_count", "base_count", "p_fail", "p_base"]
                ].values,
            )
        )

        bar_fig.add_vline(
            x=1.0,
            line_dash="dash",
            line_color="gray",
            annotation_text="Lift=1.0 基准线",
            annotation_position="top",
        )

        bar_fig.update_layout(
            title="TOP 10 离散工序因素 Fail 聚集度（Lift）",
            xaxis_title="Lift（提升度 → 越高越异常）",
            yaxis=dict(title="", tickfont=dict(size=10), automargin=True),
            height=650,
            margin=dict(r=120, t=50, b=20),
            showlegend=False,
        )

        st.plotly_chart(bar_fig, use_container_width=True)

    # ── Tab 2: Fail 内占比排行榜 ──
    with tab2:
        ratio_filtered = [r for r in ratio_results if r["fail_ratio"] > 0.20]
        top10_ratio = get_top10_by_feature(ratio_filtered, sort_key="fail_ratio")

        if len(top10_ratio) == 0:
            st.warning(
                "无 Fail 内占比数据可用于展示（需 fail_ratio > 20% 且非缺失值）。"
            )
        else:
            df_ratio_top = pd.DataFrame(top10_ratio)
            df_ratio_top["label"] = df_ratio_top.apply(
                lambda r: f"{r['feature'][:55]} → {r['value']}", axis=1
            )
            df_ratio_top = df_ratio_top.sort_values("fail_ratio", ascending=True)

            ratio_fig = go.Figure()
            ratio_fig.add_trace(
                go.Bar(
                    y=df_ratio_top["label"],
                    x=df_ratio_top["fail_ratio"] * 100,
                    orientation="h",
                    marker=dict(
                        color=df_ratio_top["fail_ratio"],
                        colorscale="Blues",
                        showscale=True,
                        colorbar=dict(title="Fail占比"),
                    ),
                    text=df_ratio_top.apply(
                        lambda r: (
                            f"{r['fail_ratio'] * 100:.1f}% | Fail#{r['fail_count']}"
                        ),
                        axis=1,
                    ),
                    textposition="outside",
                    textfont=dict(size=11),
                    hovertemplate=(
                        "<b>特征列</b>: %{customdata[0]}<br>"
                        "<b>聚集取值</b>: %{customdata[1]}<br>"
                        "<b>Fail内占比</b>: %{x:.2f}%<br>"
                        "<b>Fail次数</b>: %{customdata[2]}<br>"
                        "<b>基准次数</b>: %{customdata[3]}<br>"
                        "<extra></extra>"
                    ),
                    customdata=df_ratio_top[
                        ["feature", "value", "fail_count", "base_count"]
                    ].values,
                )
            )

            ratio_fig.update_layout(
                title="TOP 10 特征取值在 Fail 样本内的占比（不考虑基准频率）",
                xaxis_title="Fail 内占比 (%) → 越高说明该取值在 NG 中越集中",
                yaxis=dict(title="", tickfont=dict(size=10), automargin=True),
                height=650,
                margin=dict(r=120, t=50, b=20),
                showlegend=False,
            )

            st.plotly_chart(ratio_fig, use_container_width=True)

            remaining = [
                r
                for r in ratio_filtered
                if r["feature"] not in {x["feature"] for x in top10_ratio}
            ]
            if remaining:
                with st.expander(
                    f"查看剩余 Fail 内占比 > 20% 的特征（共 {len(remaining)} 条）"
                ):
                    df_rem = pd.DataFrame(remaining)
                    df_rem = df_rem.sort_values("fail_ratio", ascending=False)
                    st.dataframe(
                        df_rem[["feature", "value", "fail_count", "fail_ratio"]].rename(
                            columns={
                                "feature": "特征列",
                                "value": "聚集取值",
                                "fail_count": "Fail次数",
                                "fail_ratio": "Fail内占比",
                            }
                        ),
                        use_container_width=True,
                    )

            st.markdown("""
            **说明**：此图仅统计该取值在当前 Fail 样本中的占比，不考虑其在整体基准数据中的分布频率。
            例如某特征取值占 Fail 的 78.6%，说明该取值在 NG 样本中高度聚集，即使其 Lift 不高也值得排查。
            """)

    # ── Tab 3: 单因子 Pass/Fail 对比直方图 ──
    with tab3:
        n_show = min(5, len(top10))
        if n_show == 0:
            st.warning("无 TOP 特征可用于对比分析。")
        else:
            for rank in range(n_show):
                feat = top10[rank]["feature"]
                val = top10[rank]["value"]
                lift_val = top10[rank]["lift"]
                fail_cnt = top10[rank]["fail_count"]

                if feat not in df_base.columns:
                    continue

                st.markdown(f"**TOP{rank + 1}**: `{feat}` (Lift={lift_val})")

                # 优化：先聚类计数，再对少数唯一值进行 normalize，避免对全量数据逐行 apply
                def get_normalized_vc(series):
                    vc = series.value_counts(dropna=False)
                    norm_dict = {}
                    for k, v in vc.items():
                        nk = normalize_val(k)
                        if nk is not None:
                            norm_dict[nk] = norm_dict.get(nk, 0) + v
                    return pd.Series(norm_dict) if norm_dict else pd.Series(dtype=int)

                base_vc = get_normalized_vc(df_base[feat])
                fail_vc = get_normalized_vc(df_fail[feat])

                top_vals = base_vc.sort_values(ascending=False).head(20).index.tolist()
                if val not in top_vals:
                    top_vals.insert(0, val)

                # 单独计算 PASS 样本的分布，避免 base - fail 出现负值
                pass_vc = get_normalized_vc(
                    df_base[df_base["Results"] == "PASS"][feat]
                ) if feat in df_base.columns else pd.Series(dtype=int)

                df_plot_data = []
                for v in top_vals:
                    p_cnt = int(pass_vc.get(v, 0))
                    f_cnt = int(fail_vc.get(v, 0))
                    df_plot_data.append(
                        {"取值": str(v)[:40], "Pass": p_cnt, "Fail": f_cnt}
                    )

                df_plot = pd.DataFrame(df_plot_data)

                comp_fig = go.Figure()
                comp_fig.add_trace(
                    go.Bar(
                        name="Pass",
                        x=df_plot["取值"],
                        y=df_plot["Pass"],
                        marker_color="#4ECDC4",
                        hovertemplate="Pass: %{y}<extra></extra>",
                    )
                )
                comp_fig.add_trace(
                    go.Bar(
                        name="Fail",
                        x=df_plot["取值"],
                        y=df_plot["Fail"],
                        marker_color="#FF6B6B",
                        hovertemplate="Fail: %{y}<extra></extra>",
                    )
                )

                comp_fig.update_layout(
                    title=dict(
                        text=f"{feat[:60]}",
                        x=0.5,
                        xanchor="center",
                        y=0.98,
                        yanchor="top",
                        font=dict(size=13),
                    ),
                    xaxis_title="特征取值",
                    yaxis_title="样本数量",
                    barmode="group",
                    height=380,
                    margin=dict(l=20, r=20, t=60, b=80),
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=1.15,
                        x=0.5,
                        xanchor="center",
                    ),
                    xaxis=dict(tickfont=dict(size=9), tickangle=30),
                )

                st.plotly_chart(comp_fig, use_container_width=True)
                st.markdown("---")

    # ── Tab 4: 时间日期 Fail 占比排行 ──
    with tab4:
        if not hour_ratio_results:
            st.info("未发现时间类特征或时间数据不足，无法计算聚集度。")
        else:
            st.caption(
                "将时间类制程因素按天聚合，提取 Fail 占比最高的日期，并展示该日期内部每半小时/1小时/2小时 Fail 数量的变化趋势。"
            )

            ratio_filtered = (
                [r for r in hour_ratio_results if r["fail_ratio"] > 0.05]
                if hour_ratio_results
                else []
            )
            top10_all = sorted(
                ratio_filtered, key=lambda x: x["fail_ratio"], reverse=True
            )[:10]

            if not top10_all:
                st.warning("无时间类 Fail 内占比数据可用于展示。")
            else:
                # 根据侧边栏的分辨率滑动组件动态调整聚合周期
                # "半小时", "1小时", "2小时"
                if time_resolution == "半小时":
                    freq = "30min"
                    periods = 48
                    lbl_fmt = "%H:%M"
                    x_title = "每半小时 (30 min)"
                elif time_resolution == "1小时":
                    freq = "1h"
                    periods = 24
                    lbl_fmt = "%H:00"
                    x_title = "每小时 (1 h)"
                else:  # 2小时
                    freq = "2h"
                    periods = 12
                    lbl_fmt = "%H:00"
                    x_title = "每两小时 (2 h)"

                for rank, item in enumerate(top10_all):
                    feat = item["original_feature"]
                    val_str = item["value"]
                    val_dt = item["value_dt"]
                    fail_count = item["fail_count"]
                    fail_ratio = item["fail_ratio"]

                    st.markdown(
                        f"**TOP {rank + 1}**: `{feat}` · **{val_str}** "
                        f"(Fail次数: **{fail_count}**, 占比: **{fail_ratio * 100:.1f}%**)"
                    )

                    # 提取该整天的数据
                    mask = df_fail[feat].dt.normalize() == val_dt
                    df_subset = df_fail[mask]

                    if len(df_subset) > 0:
                        time_range = pd.date_range(start=val_dt, periods=periods, freq=freq)
                        counts_bucket = df_subset[feat].dt.floor(freq).value_counts()

                        df_trend = pd.DataFrame({"time": time_range})
                        df_trend["fail_count"] = df_trend["time"].map(counts_bucket).fillna(0)
                        df_trend["time_str"] = df_trend["time"].dt.strftime(lbl_fmt)

                        fig = go.Figure()
                        fig.add_trace(
                            go.Scatter(
                                x=df_trend["time_str"],
                                y=df_trend["fail_count"],
                                mode="lines+markers",
                                line=dict(color="#FF6B6B", width=3),
                                marker=dict(size=8, color="#FF6B6B"),
                                hovertemplate="时间: %{x}<br>Fail数量: %{y}<extra></extra>",
                            )
                        )

                        fig.update_layout(
                            title=dict(
                                text=f"{feat} {val_str} Fail 趋势变化 ({time_resolution}分辨率)",
                                font=dict(size=14),
                            ),
                            xaxis_title=x_title,
                            yaxis_title="Fail 数量",
                            height=300,
                            margin=dict(l=20, r=20, t=40, b=20),
                            yaxis=dict(rangemode="tozero"),
                        )
                        st.plotly_chart(fig, use_container_width=True)
                        st.markdown("---")

                st.markdown(
                    f"""
                **说明**：排行为全流程所有时间列在各天中 Fail 内占比最高的 Top 10，展开展示内部以 **{time_resolution}** 粒度的故障变化趋势。
                """
                )

    # ── Tab 5: AI 根因诊断 (LightGBM) ──
    with tab5:
        st.markdown("### 🤖 基于 LightGBM 的 AI 根因特征诊断")
        st.info("💡 **说明**：此模块会自动将当前筛选条件下的全部离散工序特征丢入 LightGBM 树模型进行二分类训练（Pass vs Fail）。模型输出的**特征重要性（Feature Importance）**能精准捕捉导致不良的复合交叉原因。本功能基于你提供的 Dashboard 特征集。")
        df_imp = st.session_state.get("df_imp", None)
        combo_results = st.session_state.get("combo_results", [])

        if st.session_state.get("ml_running", False):
            st.info("🤖 AI 决策树分类与高危组合特征挖掘正在后台全自动计算中，前台图表已就绪，请稍候...")
        elif df_imp is None or df_imp.empty:
            st.warning("⚠️ 样本量过小（Pass 或 Fail 数量不足 10 条）或模型未生成，无法显示 AI 特征重要性分析结果。")
        else:
            top_n_imp = df_imp.head(20)
            
            # 画图
            fig_imp = go.Figure()
            fig_imp.add_trace(
                go.Bar(
                    y=top_n_imp["特征列"],
                    x=top_n_imp["信息增益 (Gain)"],
                    orientation="h",
                    marker=dict(
                        color=top_n_imp["信息增益 (Gain)"],
                        colorscale="Viridis",
                        showscale=True,
                        colorbar=dict(title="Gain"),
                    ),
                    text=top_n_imp["信息增益 (Gain)"].apply(lambda x: f"{x:.1f}"),
                    textposition="outside",
                    textfont=dict(size=11),
                )
            )
            fig_imp.update_layout(
                title="TOP 20 导致 Fail 的最关键特征 (LightGBM 树分裂信息增益)",
                xaxis_title="信息增益 (Gain) → 越高代表对 Pass/Fail 的区分能力越强",
                yaxis=dict(title="", tickfont=dict(size=10), automargin=True, autorange="reversed"),
                height=max(500, len(top_n_imp) * 25),
                margin=dict(l=10, r=120, t=50, b=20),
                showlegend=False,
            )
            st.plotly_chart(fig_imp, use_container_width=True)
            
            st.markdown("---")
            # 详细数据展开
            with st.expander(f"查看具有信息增益 of 完整特征排行榜（共 {len(df_imp)} 项）", expanded=False):
                # 合并一下特征含义
                _, desc_map_dict = load_dashboard_dict()
                if not desc_map_dict:
                    desc_map_dict = desc_map
                if desc_map_dict:
                    df_imp['特征含义'] = df_imp['特征列'].map(desc_map_dict).fillna("无描述")
                    cols_order = ['特征列', '特征含义', '信息增益 (Gain)']
                else:
                    cols_order = ['特征列', '信息增益 (Gain)']
                    
                st.dataframe(
                    df_imp[cols_order],
                    use_container_width=True,
                    hide_index=True
                )
                
            st.markdown("---")
            st.subheader("🔗 核心高危组合特征挖掘 (多因子交叉)")
            st.info("自动提取 Top 8 核心特征进行 2~4 维度的排列组合，并回溯真实数据，精准算出真实的致命组合条件。")
            
            if combo_results:
                df_combo = pd.DataFrame(combo_results)
                # 按致死率和数量双重降序
                df_combo = df_combo.sort_values(by=["Fail概率", "Fail次数"], ascending=[False, False])
                df_combo["Fail概率"] = df_combo["Fail概率"].apply(lambda x: f"{x * 100:.1f}%")
                
                st.success(f"扫描完毕！共挖掘出 **{len(df_combo)}** 条真实有效的高危组合规则（过滤条件：致死率≥20%）。")
                st.dataframe(
                    df_combo,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.warning("未能挖掘出满足最低转化率要求的多因子组合条件（致死率要求≥20%）。")

    # ═══════════════════════════════════════════════
    # 详细数据表
    # ═══════════════════════════════════════════════

    st.markdown("---")
    st.subheader("TOP 10 详细数据")

    # 按 composite_score 降序排序，与 Tab1 图表排序一致
    top10_sorted = sorted(top10, key=lambda x: x["composite_score"], reverse=True)
    df_display = pd.DataFrame(top10_sorted)
    df_display.insert(0, "排名", range(1, len(df_display) + 1))
    df_display["Fail集中度"] = df_display["p_fail"].apply(lambda x: f"{x * 100:.2f}%")
    df_display["基准占比"] = df_display["p_base"].apply(lambda x: f"{x * 100:.2f}%")
    df_display["Fail内占比"] = df_display["fail_ratio"].apply(
        lambda x: f"{x * 100:.1f}%"
    )

    display_cols = [
        "排名",
        "feature",
        "value",
        "composite_score",
        "lift",
        "fail_count",
        "base_count",
        "Fail集中度",
        "基准占比",
        "Fail内占比",
    ]
    st.dataframe(
        df_display[display_cols].rename(
            columns={
                "feature": "特征列",
                "value": "聚集取值",
                "lift": "Lift",
                "fail_count": "Fail次数",
                "base_count": "基准次数",
                "composite_score": f"综合评分(Lift{lift_weight:.0%}+Fail{fail_ratio_weight:.0%})",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("查看全部 Lift > 1.0 的聚集特征"):
        full_results = [
            r for r in lift_results if r["lift"] > 1.0 and r["fail_ratio"] > 0.10
        ]
        if full_results:
            df_full = pd.DataFrame(full_results).sort_values(
                "composite_score", ascending=False
            )
            df_full["Fail集中度"] = df_full["p_fail"].apply(lambda x: f"{x * 100:.2f}%")
            df_full["基准占比"] = df_full["p_base"].apply(lambda x: f"{x * 100:.2f}%")
            df_full["Fail内占比"] = df_full["fail_ratio"].apply(
                lambda x: f"{x * 100:.1f}%"
            )
            display_cols_full = [
                "feature",
                "value",
                "composite_score",
                "lift",
                "fail_count",
                "base_count",
                "Fail集中度",
                "基准占比",
                "Fail内占比",
            ]
            st.dataframe(
                df_full[display_cols_full].rename(
                    columns={
                        "feature": "特征列",
                        "value": "聚集取值",
                        "lift": "Lift",
                        "fail_count": "Fail次数",
                        "base_count": "基准次数",
                        "composite_score": f"综合评分(Lift{lift_weight:.0%}+Fail{fail_ratio_weight:.0%})",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("无额外 Lift > 1.0 且 Fail内占比 > 10% 的特征")

    # Fail 内唯一值=1 的列单独展示
    if fail_one_results:
        st.markdown("---")
        st.subheader("⚠️ Fail 内唯一值=1 的特征（所有 Fail 样本取值完全一致）")
        df_fail_one = pd.DataFrame(fail_one_results)
        df_fail_one["Fail内占比"] = df_fail_one["fail_ratio"].apply(
            lambda x: f"{x * 100:.1f}%"
        )
        st.dataframe(
            df_fail_one[["feature", "value", "fail_count", "Fail内占比"]].rename(
                columns={
                    "feature": "特征列",
                    "value": "聚集取值",
                    "fail_count": "Fail次数",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.markdown("""
        **说明**：以上特征在当前 Fail 样本中只有一个唯一取值（100%集中），
        说明该特征在 NG 样本中高度一致。虽然不参与 Lift 计算，但可作为排查线索。
        """)

    st.markdown("---")
    st.subheader("🤖 LLM 智能诊断与排查行动指南")
    
    if "comprehensive_llm_report" in st.session_state:
        st.markdown(st.session_state["comprehensive_llm_report"])
    else:
        st.info("💡 报告生成就绪，在点击「开始分析」后会自动生成。")


if __name__ == "__main__":
    main()