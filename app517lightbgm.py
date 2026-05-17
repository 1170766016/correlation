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
import random
import numpy as np
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


# ===================== MOCK DATA =====================
def generate_mock_data(n: int = 500) -> pd.DataFrame:
    """生成模拟数据，植入异常规律用于 Demo 验证"""
    random.seed(42)
    np.random.seed(42)

    stations = ["METROLOGY", "FUNCTION_TEST", "FGAVI"]
    modes_normal = [
        "VA_STICTION_CLOSE2OPEN_MA",
        "VA_GAINTRIM_VCMSTROKE_DIAMETER_UM",
        "VA_CLRAMP_CLOSELINEAERREGION_FW_DIAMETER",
        "VA_STICTION_OPEN2CLOSE_MA",
    ]
    TARGET_MODE = "VA_DRIVER2_AFE_P_VA/CONNECT NG"
    machines = [f"MC_{i:03d}" for i in range(1, 16)]
    BAD_MC = "MC_BAD_007"
    sockets = [f"S{i}" for i in range(1, 9)]
    nozzles = [f"N{i}" for i in range(1, 7)]
    cavities = ["Cav_1", "Cav_2", "Cav_3", "Cav_4"]
    vendors = ["Vendor_A", "Vendor_B", "Vendor_C"]
    lots = [f"LOT_{i:04d}" for i in range(2001, 2021)]
    base_date = datetime(2026, 4, 1)

    proc_prefixes = [
        "VCM_M1_FPC_UpCoil_Attach", "VCM_M1_FPC_Coil_Baking", "VCM_M1_Jet_Soldering",
        "VCM_M2_Stator_Plasma", "VCM_M2_Stator_FPC_Attach", "VCM_M2_StatorSubAssy_Baking",
        "VCM_M3_BCA_Plasma", "VCM_M3_BCA_Bending", "VCM_M3_BCA_Baking", "VCM_M3_StatorAssy_Baking",
        "VCM_M4_Rotor_Plasma", "VCM_M4_Rotor_Magnet_Dispensing", "VCM_M4_Rotor_Magnet_Attach",
        "VCM_M4_Rotor_Magnet_Baking", "VCM_M4_RotorAssy_Baking",
        "VCM_M5_ShieldCan_Shim_Attach", "VCM_M5_ShieldCan_Shim_AutoClave",
        "VCM_M6_StatorAssy_Plasma", "VCM_M6_StatorAssy_Grease", "VCM_M6_Ball_Rotor_Assy",
        "VCM_M6_Blade_Assembly", "VCM_M6_ShieldCan_Assy", "VCM_M6_ShieldCan_Baking",
        "VCM_XRay", "VCM_M7_AgGlue1", "VCM_M7_AgGlue2", "VCM_M7_AgGlue_Baking",
        "VCM_M8_Aging", "VCM_M8_Blow_suck", "VCM_Function_Test1",
        "VCM_M9_Cover_Attach", "VCM_M9_Cover_AutoClave", "VCM_M10_Function_test",
    ]

    rows = []
    for i in range(n):
        day_off = random.randint(0, 29)
        hour = random.randint(0, 23)
        minute = random.randint(0, 59)
        dt = base_date + timedelta(days=day_off, hours=hour, minutes=minute)

        is_fail = random.random() < 0.30
        is_target = is_fail and random.random() < 0.45

        if is_target:
            station = "METROLOGY"
            fm = TARGET_MODE
            if random.random() < 0.9:
                dt = base_date + timedelta(days=5, hours=random.randint(2, 3), minutes=minute)
        elif is_fail:
            station = random.choice(stations)
            fm = random.choice(modes_normal)
        else:
            station = ""
            fm = ""

        row = {
            "sn": f"SN{i:06d}", "Serial_No": f"SER-{i:06d}",
            "Date": dt.strftime("%Y-%m-%d"),
            "Results": "FAIL" if is_fail else "PASS",
            "Failed_Station": station, "Failure_Mode": fm,
            "Project": "VA3199", "Build": random.choice(["PRB", "MP"]),
            "Config": random.choice(["Config_A", "Config_B"]),
        }

        for pfx in proc_prefixes:
            mc = BAD_MC if is_target and random.random() < 0.9 else random.choice(machines)
            row[f"{pfx}_MC_ID"] = mc
            offset = random.randint(0, 30) if is_target else random.randint(0, 300)
            row[f"{pfx}_End_Time"] = (dt + timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M:%S")

        for sc in ["VCM_M8_Aging_Socket", "VCM_M3_BCA_Bending_Socket", "VCM_Function_Test1_Socket", "VCM_M10_Function_test_Socket"]:
            row[sc] = random.choice(sockets)
        row["VCM_M8_Aging_Nozzle"] = random.choice(nozzles)
        row["VCM_Stator_Cavity_ID"] = random.choice(cavities)
        row["VCM_Rotor_Cavity_ID"] = random.choice(cavities)
        row["VCM_FPC_Vendor"] = random.choice(vendors)
        row["VCM_Bending_Glue_lot_ID_1"] = random.choice(lots)
        row["VCM_MagnetAttach_Glue_lot_ID_1"] = random.choice(lots)
        row["VCM_Ag_Glue_lot_ID_1"] = random.choice(lots)
        rows.append(row)

    return pd.DataFrame(rows)


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


def get_top10_by_feature(
    results: List[Dict[str, Any]], sort_key: str = "composite_score"
) -> List[Dict[str, Any]]:
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


def list_available_models(api_key: str, api_base: str) -> List[str]:
    """查询 API 可用模型列表"""
    import requests

    url = f"{api_base.rstrip('/')}/models"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=15
        )
        if response.status_code == 200:
            data = response.json()
            models = [
                m.get("id", m) if isinstance(m, dict) else m
                for m in data.get("data", [])
            ]
            return models
        return []
    except Exception as e:
        logger.error(f"获取模型列表失败: {str(e)}")
        return []


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
) -> Optional[str]:
    """
    调用LLM生成结构化分析报告
    仅发送TOP 10聚集度数据，不发送原始数据
    """
    import requests

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
                    "工序": item.get("original_feature", item["feature"].replace("_6H", "")),
                    "聚集时间段": item["value"],
                    "Fail内占比": f"{item['fail_ratio'] * 100:.1f}%",
                    "Fail次数": item["fail_count"],
                }
            )
        hour_text = (
            "\n\n#### ⏰ 时间段异常聚集 (6小时区间)\n以下为按6小时聚合的时间类制程因素中，Fail 高度集中的时段：\n"
            + json.dumps(hour_items, ensure_ascii=False, indent=2)
        )

    prompt = f"""你是一名资深的3C制造质量专家。我们对产线终检Fail的产品进行了全流程（约200个工序因素）的共性聚集度分析。
"提升度(Lift)"表示该因素在Fail产品中出现的频率远超正常水平。Lift值越高，该因素在Fail产品中越异常聚集。
"Fail内占比"表示该取值在当前Fail样本中的占比，不考虑基准频率。Fail内占比越高，说明该取值在NG中越集中。
"综合评分"是Lift和Fail内占比的加权组合，用于平衡两者。

当前筛选条件：Failed_Station={failed_station if failed_station else "全部"}，Failure_Mode={failure_mode if failure_mode else "全部"}

以下是聚集度最高的TOP 10因素（含特征列含义）：
{top10_text}
{fail_one_text}
{hour_text}

请根据上述数据，输出纯文本的结构化排查报告，格式要求：
1. 使用 Markdown 格式，但标题层级使用 #### 或 #####（不要用过大的标题）
2. 每个部分用短段落或 bullet points 呈现
3. 层次清晰，重点突出

#### 核心不良聚集点
（用精炼的语言指出Fail产品最集中在哪些具体的设备ID、来料批次等因素，必须引用特征列的完整名称）

#### 异常工艺参数特征
（指出哪些工序的时间或设备设定值在Fail产品中相对于PASS产品表现出明显的整体偏移，必须引用特征列的完整名称）

#### 异常时段分析
（指出哪些工序在哪些 6 小时区间段 Fail 高度集中，可能提示异常班次或时段性异常，必须引用工序完整列名）

请使用中文输出，简洁专业，避免冗余叙述。提到特征时必须使用完整的特征列名称（如VCM_M1_FPC_UpCoil_Attach_Staging_time），不要简写。"""

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


def call_lgb_llm(
    api_key: str,
    api_base: str,
    model: str,
    df_imp: pd.DataFrame,
    df_combo: Optional[pd.DataFrame]
) -> Optional[str]:
    """
    调用 LLM 对 LightGBM 的分析结果进行大白话解读
    """
    import requests

    if not api_key:
        return None
        
    imp_text = df_imp.head(10).to_json(orient='records', force_ascii=False) if not df_imp.empty else "无显著特征"
    combo_text = df_combo.head(10).to_json(orient='records', force_ascii=False) if (df_combo is not None and not df_combo.empty) else "无高危组合"
    
    prompt = f"""你是一名资深的3C制造质量专家。我们刚利用 LightGBM 机器学习模型对产线的 40 万条数据（包含数百个工序参数）进行了深度诊断。

以下是模型提炼出的两组最致命的发现：

【1. 单一特征信息增益 (Gain) 排名 Top 10】
Gain 越高，说明该工序设备对判定产品是否 Fail 起到了决定性的拆分作用。这是“案发重灾区”。
{imp_text}

【2. 高危多因子交叉组合规则】
这是模型从真实数据中挖掘出的致命组合。当这几个特定条件同时满足时，良率会极度崩盘（Fail概率极高）。
{combo_text}

请根据上述数据，用大白话向车间的生产主管汇报，输出纯文本的结构化指导报告，格式要求：
1. 使用 Markdown 格式，但标题层级使用 #### 或 #####。
2. 语言要极其通俗、精炼，**不要解释什么是 Gain 或模型原理**，直接说结论。
3. 层次清晰：

#### 🚨 核心案发地在哪
（基于信息增益榜，指出产线目前的病灶集中在哪些特定的机台或工序段）

#### 💥 致命触发条件是什么
（基于交叉组合表，指出当哪几个特定条件同时满足时会导致大规模报废，列出 1~2 条最危险的）

请使用中文输出，专业且接地气。
"""

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
            return f"LLM解读失败 (HTTP {response.status_code})"
    except Exception as e:
        return f"LLM调用异常: {str(e)[:300]}"


def _get_cache_key(
    start_date: Any, end_date: Any, failed_station: str, lift_weight: float
) -> str:
    """生成计算结果的缓存键"""
    return f"{start_date}_{end_date}_{failed_station}_{lift_weight}"


def extract_time_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """检测时间列，抽取6小时(6H)作为离散特征，返回(修改后df, 新增列名列表)
    只处理 _End_Time 列（实际时间戳），不处理 _Staging_time（持续秒数）和 _Start_Time
    """
    time_6h_cols: List[str] = []
    time_pattern = re.compile(r"_End_Time$|_datetime$", re.IGNORECASE)
    n = len(df)
    if n == 0:
        return df, time_6h_cols

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

            # 保留原时间列为 datetime 格式，方便后续半小时趋势图计算
            df[col] = parsed

            # 生成 6 小时区间起点
            col_6h = f"{col}_6H"
            df[col_6h] = parsed.dt.floor("6h")
            time_6h_cols.append(col_6h)

        except Exception as e:
            logger.warning(f"处理时间列 {col} 时发生异常，跳过该列: {str(e)}")
            continue
    return df, time_6h_cols


def main():
    st.title("终检Fail产品全流程共性聚集度分析")
    st.markdown(
        "基于**提升度 (Lift)** 算法，从全流程 ~500 个离散工序因素中识别导致终检 Fail 的异常聚集特征。"
    )

    # ── 侧边栏：数据源 ──
    st.sidebar.header("数据源")
    uploaded_file = st.sidebar.file_uploader(
        "上传数据文件（可选）", type=["csv", "xlsx", "xls", "parquet"]
    )
    use_mock = st.sidebar.checkbox(
        "使用 Demo 模拟数据", value=False, disabled=uploaded_file is not None
    )
    use_default = st.sidebar.checkbox(
        "使用本地 PRB数据.csv", value=True, disabled=use_mock
    )

    if use_mock and "df_raw" in st.session_state:
        if st.session_state.get("uploaded_name") != "__mock__":
            st.session_state.pop("df_raw", None)
            st.rerun()

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

    # ── 数据加载（缓存到 session_state） ──
    dashboard_cols, desc_map = load_dashboard_dict()

    if dashboard_cols:
        st.sidebar.caption(f"Dashboard 特征列: {len(dashboard_cols)} 列")

    need_reload = False
    if "df_raw" not in st.session_state:
        need_reload = True
    elif (
        uploaded_file is not None
        and st.session_state.get("uploaded_name") != uploaded_file.name
    ):
        need_reload = True
    elif uploaded_file is None and st.session_state.get("uploaded_name") is not None:
        need_reload = True
    elif (
        use_mock
        and st.session_state.get("uploaded_name") != "__mock__"
        and not need_reload
    ):
        need_reload = True

    if need_reload:
        with st.spinner("正在加载数据..."):
            if use_mock:
                st.session_state["df_raw"] = generate_mock_data()
                st.session_state["uploaded_name"] = "__mock__"
            elif uploaded_file is not None:
                st.session_state["df_raw"] = load_data(uploaded_file=uploaded_file)
                st.session_state["uploaded_name"] = uploaded_file.name
            elif use_default:
                st.session_state["df_raw"] = load_data(
                    file_path=DATA_PATH, dashboard_cols=dashboard_cols
                )
                st.session_state["uploaded_name"] = None
            else:
                st.sidebar.warning("请上传数据文件或勾选使用本地数据")
                st.info("请先在左侧配置数据源。")
                return
        st.session_state.pop("date_col_converted", None)

    df_raw = st.session_state["df_raw"]

    if df_raw is None:
        file_exists = os.path.exists(DATA_PATH)
        dashboard_exists = os.path.exists(DASHBOARD_PATH)
        st.error(
            f"未能加载数据。\n- PRB数据.csv 存在: {file_exists}\n- dashboard F11.xlsx 存在: {dashboard_exists}"
        )
        st.info("请检查文件路径或上传有效文件。")
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

    # 缓存日期转换结果
    if (
        "date_col_converted" not in st.session_state
        or st.session_state.get("date_col_name") != date_col
    ):
        df_raw[date_col] = pd.to_datetime(df_raw[date_col], errors="coerce")
        st.session_state["date_col_converted"] = True
        st.session_state["date_col_name"] = date_col

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
        value=0.3,
        step=0.05,
        help="Lift 在综合评分中的权重，Fail内占比自动占剩余权重",
    )
    fail_ratio_weight = 1.0 - lift_weight
    st.sidebar.caption(f"Fail内占比权重: {fail_ratio_weight:.2f}")

    # 日期筛选（缓存）
    date_mask = (df_raw[date_col].dt.date >= start_date) & (
        df_raw[date_col].dt.date <= end_date
    )
    df_date_filtered = df_raw[date_mask]

    if len(df_date_filtered) == 0:
        st.warning("所选日期范围内无数据，请调整日期范围。")
        return

    station_values = df_date_filtered["Failed_Station"].dropna().unique()
    station_options = ["全部"] + sorted(station_values.tolist())
    failed_station = st.sidebar.selectbox("Failed_Station", station_options, index=0)

    # ── 开始分析按钮 ──
    analyze_btn = st.sidebar.button(
        "开始分析", type="primary", use_container_width=True
    )

    # 生成缓存键
    cache_key = _get_cache_key(start_date, end_date, failed_station, lift_weight)

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
                df_base = df_date_filtered[df_date_filtered["Results"] == "PASS"].copy()
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

        lift_progress_text.markdown("正在提取时间6小时区间特征(仅针对Fail数据)...")
        time_cols, _, _, _ = classify_columns(df_fail)
        dayhour_ratio_results = []

        if time_cols:
            df_fail, dayhour_cols = extract_time_features(df_fail)
            if dayhour_cols:
                lift_progress_text.markdown("正在计算时间类Fail占比（6小时）...")
                for col_6h in dayhour_cols:
                    original_col = col_6h.replace("_6H", "")
                    fail_series = df_fail[col_6h].dropna()
                    if len(fail_series) == 0:
                        continue
                    fail_counts = fail_series.value_counts()
                    for val_dt, count in fail_counts.items():
                        end_dt = val_dt + pd.Timedelta(hours=6)
                        val_str = f"{val_dt.strftime('%m-%d %H:00')}~{end_dt.strftime('%H:00')}"
                        
                        fail_ratio = count / n_fail
                        dayhour_ratio_results.append(
                            {
                                "feature": col_6h,
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

    # 从缓存恢复后也需要显示指标卡
    if has_cache and not analyze_btn:
        fail_rate = n_fail / n_base * 100
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

                df_plot_data = []
                for v in top_vals:
                    b_cnt = int(base_vc.get(v, 0))
                    f_cnt = int(fail_vc.get(v, 0))
                    df_plot_data.append(
                        {"取值": str(v)[:40], "Pass": b_cnt - f_cnt, "Fail": f_cnt}
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
                "将时间类制程因素按 6 小时区间聚合，提取 Fail 占比最高的时段，并展示该时段内部每半小时 Fail 数量的变化趋势。"
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

                    # 提取该 6 小时区间的数据
                    mask = df_fail[feat].dt.floor("6h") == val_dt
                    df_subset = df_fail[mask]

                    if len(df_subset) > 0:
                        # 按半小时聚合
                        time_range = pd.date_range(start=val_dt, periods=12, freq="30min")
                        counts_30min = df_subset[feat].dt.floor("30min").value_counts()

                        df_trend = pd.DataFrame({"time": time_range})
                        df_trend["fail_count"] = df_trend["time"].map(counts_30min).fillna(0)
                        df_trend["time_str"] = df_trend["time"].dt.strftime("%H:%M")

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
                                text=f"{feat} Fail趋势变化",
                                font=dict(size=14),
                            ),
                            xaxis_title="每半小时 (30 min)",
                            yaxis_title="Fail 数量",
                            height=300,
                            margin=dict(l=20, r=20, t=40, b=20),
                            yaxis=dict(rangemode="tozero"),
                        )
                        st.plotly_chart(fig, use_container_width=True)
                        st.markdown("---")

                st.markdown(
                    """
                **说明**：排行为全流程所有时间列在各 6 小时区间中 Fail 内占比最高的 Top 10，展开展示内部半小时粒度趋势。
                """
                )

    # ── Tab 5: AI 根因诊断 (LightGBM) ──
    with tab5:
        st.markdown("### 🤖 基于 LightGBM 的 AI 根因特征诊断")
        st.info("💡 **说明**：此模块会自动将当前筛选条件下的全部离散工序特征丢入 LightGBM 树模型进行二分类训练（Pass vs Fail）。模型输出的**特征重要性（Feature Importance）**能精准捕捉导致不良的复合交叉原因。本功能基于你提供的 Dashboard 特征集。")
        
        @st.fragment
        def render_ml_diagnosis():
            if st.button("🚀 运行 AI 根因诊断 (约需10~30秒)", key="run_ml_diagnosis", type="primary"):
                try:
                    import lightgbm as lgb
                except ImportError:
                    st.error("⚠️ 当前环境未安装 LightGBM 库。\n\n请在运行此应用的终端或命令行中执行：\n\n`pip install lightgbm`\n\n安装完成后无需重启应用，直接再次点击本按钮即可。")
                else:
                    with st.spinner("正在训练 LightGBM 模型并提取特征重要性..."):
                        df_base_ml = st.session_state["df_base"].copy()
                        df_fail_ml = st.session_state["df_fail"].copy()
                        
                        df_ml = pd.concat([df_base_ml, df_fail_ml], ignore_index=True)
                        
                        if len(df_fail_ml) < 10 or len(df_base_ml) < 10:
                            st.warning("⚠️ 样本量过小（Pass 或 Fail 数量不足 10 条），无法训练具备统计意义的模型。请放宽日期或工序筛选条件。")
                        else:
                            # 构造标签
                            y = df_ml["Results"].map({"PASS": 0, "FAIL": 1})
                            
                            # 特征列为仪表盘配置关注的离散特征
                            raw_features = st.session_state["all_feature_cols"]
                            
                            # 自动剔除精确到秒的绝对时间戳列（防噪音分类爆炸）
                            features_to_use = []
                            import re
                            for c in raw_features:
                                if c.lower() == "date":
                                    continue
                                # 如果以 _time 结尾且不是 _staging_time（滞留时间时长可以作为特征），则剔除
                                if re.search(r"_time$", c, re.IGNORECASE) and not re.search(r"_staging_time$", c, re.IGNORECASE):
                                    continue
                                features_to_use.append(c)
                                
                            X = df_ml[features_to_use].copy()
                            # 统一填充缺失值，并转换为 string -> category 以供 LightGBM 使用
                            for col in X.columns:
                                X[col] = X[col].fillna("缺失值").astype(str).astype('category')
                                
                            # 训练模型，class_weight='balanced' 处理 Pass 极多 Fail 极少的不均衡
                            clf = lgb.LGBMClassifier(
                                objective='binary',
                                class_weight='balanced',
                                n_estimators=100,
                                importance_type='gain', # 使用信息增益
                                n_jobs=-1,
                                random_state=42,
                                verbose=-1
                            )
                            
                            try:
                                clf.fit(X, y)
                                
                                # 提取特征重要性
                                imp_vals = clf.booster_.feature_importance(importance_type='gain')
                                df_imp = pd.DataFrame({
                                    '特征列': X.columns,
                                    '信息增益 (Gain)': imp_vals
                                })
                                
                                # 过滤掉完全没用的特征
                                df_imp = df_imp[df_imp['信息增益 (Gain)'] > 0]
                                df_imp = df_imp.sort_values(by='信息增益 (Gain)', ascending=False)
                                
                                if df_imp.empty:
                                    st.warning("模型未能找到任何具有显著信息增益区分度的特征。")
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
                                    with st.expander(f"查看具有信息增益的完整特征排行榜（共 {len(df_imp)} 项）", expanded=False):
                                        # 合并一下特征含义
                                        _, desc_map = load_dashboard_dict()
                                        if desc_map:
                                            df_imp['特征含义'] = df_imp['特征列'].map(desc_map).fillna("无描述")
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
                                    
                                    with st.spinner("正在进行多因子交叉扫描与真实概率回测..."):
                                        top_features = df_imp.head(8)['特征列'].tolist()
                                        
                                        import itertools
                                        
                                        # 使用 string 类型防止 groupby 因为 category 的笛卡尔积耗尽内存
                                        X_str = X[top_features].astype(str)
                                        X_fail_str = X_str[y == 1]
                                        X_base_str = X_str[y == 0]
                                        
                                        combo_results = []
                                        # 动态计算最低发生次数，最少 5 次，最多 20 次，防止极端巧合
                                        min_fail_count = max(5, int(len(X_fail_str) * 0.01))
                                        if min_fail_count > 20:
                                            min_fail_count = 20
                                            
                                        _, desc_map = load_dashboard_dict()
                                        
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
                                                    if k == 1:
                                                        val_tuple = (val_tuple,)
                                                        
                                                    b_cnt = base_vc.get(val_tuple, 0)
                                                    total_cnt = f_cnt + b_cnt
                                                    ratio = f_cnt / total_cnt
                                                    
                                                    # 过滤转化率低于 20% 的噪音组合
                                                    if ratio >= 0.2:
                                                        rule_parts = []
                                                        for col, val in zip(combo_cols, val_tuple):
                                                            col_name = desc_map.get(col, col) if desc_map else col
                                                            # 防止把 np.nan 转成的 'nan' 字符串显示出来，做个美化
                                                            val_display = "缺失值" if val == "nan" else val
                                                            rule_parts.append(f"[{col_name}]='{val_display}'")
                                                        rule_text = " 且 ".join(rule_parts)
                                                        
                                                        combo_results.append({
                                                            "维度": f"{k}维",
                                                            "高危组合条件": rule_text,
                                                            "Fail概率": ratio,
                                                            "Fail次数": f_cnt,
                                                            "基准次数": b_cnt
                                                        })
                                                        
                                        if combo_results:
                                            df_combo = pd.DataFrame(combo_results)
                                            # 按致死率和数量双重降序
                                            df_combo = df_combo.sort_values(by=["Fail概率", "Fail次数"], ascending=[False, False])
                                            df_combo["Fail概率"] = df_combo["Fail概率"].apply(lambda x: f"{x * 100:.1f}%")
                                            
                                            st.success(f"扫描完毕！共挖掘出 **{len(df_combo)}** 条真实有效的高危组合规则（过滤条件：致死率≥20% 且 Fail样本数≥{min_fail_count}）。")
                                            st.dataframe(
                                                df_combo,
                                                use_container_width=True,
                                                hide_index=True
                                            )
                                        else:
                                            st.warning(f"未能挖掘出满足最低转化率要求的多因子组合条件（Fail样本数要求≥{min_fail_count}，致死率要求≥20%）。")
                                            
                                        st.markdown("---")
                                        st.subheader("🤖 大模型智能根因解读")
                                        if LLM_API_KEY and LLM_API_KEY != "sk-your-api-key-here":
                                            with st.spinner("大模型正在深度思考分析报告，请稍候..."):
                                                lgb_report = call_lgb_llm(
                                                    LLM_API_KEY, 
                                                    LLM_API_BASE, 
                                                    LLM_MODEL, 
                                                    df_imp, 
                                                    df_combo if combo_results else None
                                                )
                                                if lgb_report:
                                                    st.markdown(lgb_report)
                                                else:
                                                    st.warning("生成大模型解读报告失败。")
                                        else:
                                            st.info("💡 如果在系统中配置了 LLM_API_KEY，此处将自动输出大模型生成的大白话行动指南报告。")
                                            
                            except Exception as e:
                                st.error(f"训练模型或组合挖掘时发生异常: {str(e)}")
    
        render_ml_diagnosis()

    # ═══════════════════════════════════════════════
    # 详细数据表
    # ═══════════════════════════════════════════════

    st.markdown("---")
    st.subheader("TOP 10 详细数据")

    df_display = pd.DataFrame(top10)
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

    # ═══════════════════════════════════════════════
    # LLM 报告（放在最后）
    # ═══════════════════════════════════════════════

    st.markdown("---")
    st.subheader("🤖 LLM 质量分析报告")

    llm_report_placeholder = st.empty()

    if LLM_API_KEY and LLM_API_KEY != "sk-your-api-key-here" and len(top10) > 0:
        llm_report_placeholder.info("正在调用 LLM 生成质量分析报告，请稍候...")

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

        try:
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
            )
            if llm_report:
                llm_report_placeholder.empty()
                st.markdown(llm_report)
            else:
                llm_report_placeholder.warning(
                    "LLM 报告生成失败，请检查 API Key 和网络连接。"
                )
        except Exception as e:
            llm_report_placeholder.error(f"LLM 调用异常: {str(e)[:300]}")
    else:
        llm_report_placeholder.info("未配置 LLM API Key，跳过报告生成。")


if __name__ == "__main__":
    main()
