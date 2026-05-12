"""
终检Fail产品全流程共性聚集度分析 Demo 应用
基于提升度(Lift)算法，从全流程离散工序因素中识别导致Fail的异常聚集特征
"""
import warnings
warnings.filterwarnings("ignore", message="coroutine 'expire_cache' was never awaited")
warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*tracemalloc.*")

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import numpy as np
import os
import json
import re
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

st.set_page_config(page_title="终检Fail共性聚集度分析", layout="wide")

DATA_PATH = os.path.join(os.path.dirname(__file__), "PRB_data.csv")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard F11.xlsx")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.5-27b-fp8")

SKIP_COLS = {'Build'}


def normalize_val(v):
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
        if s.lower() in ('nan', 'none', 'null', 'nat', ''):
            return None
        return s


@st.cache_data(show_spinner=False, max_entries=1)
def load_dashboard_dict():
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
            return col_names, desc_map
        except Exception:
            pass
    return None, {}


@st.cache_data(show_spinner="正在加载数据...", max_entries=2)
def load_data_from_path(file_path, usecols_tuple):
    """从文件路径加载CSV（缓存），usecols_tuple 为 tuple 或 None"""
    usecols = list(usecols_tuple) if usecols_tuple else None
    return pd.read_csv(file_path, usecols=usecols, low_memory=False)


def load_data(file_path=None, uploaded_file=None, dashboard_cols=None):
    """加载CSV数据，优先使用上传文件，其次使用本地默认文件"""
    if uploaded_file is not None:
        return pd.read_csv(uploaded_file, low_memory=False)
    if file_path and os.path.exists(file_path):
        if dashboard_cols:
            csv_cols = pd.read_csv(file_path, nrows=0).columns.tolist()
            use_cols = [c for c in dashboard_cols if c in csv_cols and c not in SKIP_COLS]
            use_cols_extra = [c for c in ['Date', 'Results', 'Failed_Station', 'Failure_Mode'] if c in csv_cols]
            for c in use_cols_extra:
                if c not in use_cols:
                    use_cols.append(c)
            return load_data_from_path(file_path, tuple(use_cols))
        return load_data_from_path(file_path, None)
    return None


def classify_columns(df):
    """
    将列分为时间列、ID列、离散特征列、元数据列
    """
    time_cols = []
    id_cols = []
    meta_cols = []
    discrete_cols = []

    time_pattern = re.compile(r'_Time$|_time$|_Staging_time$', re.IGNORECASE)
    id_pattern = re.compile(r'^SN$', re.IGNORECASE)

    for col in df.columns:
        if col in SKIP_COLS:
            continue
        if col in ['Results', 'Failed_Station', 'Failure_Mode', 'Date']:
            meta_cols.append(col)
        elif id_pattern.match(col):
            id_cols.append(col)
        elif time_pattern.search(col):
            if re.search(r'_Staging_time$', col, re.IGNORECASE):
                discrete_cols.append(col)
            else:
                time_cols.append(col)
        else:
            discrete_cols.append(col)

    return time_cols, id_cols, meta_cols, discrete_cols


def compute_lift(df_base, df_fail, feature_cols, lift_weight=0.3, min_count=3, max_unique_ratio=0.3):
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

    time_pattern = re.compile(r'_Start_Time$|_End_Time$|_datetime$', re.IGNORECASE)

    total_cols = len(feature_cols)
    fail_ratio_weight = 1.0 - lift_weight

    for idx, col in enumerate(feature_cols):
        if idx % 50 == 0:
            pct = min(idx / total_cols, 0.9)
            try:
                st.session_state['lift_progress'].progress(pct)
                st.session_state['lift_progress_text'].markdown(
                    f"计算提升度... ({idx}/{total_cols})"
                )
            except Exception:
                pass

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
            val = fail_series.dropna().iloc[0] if fail_series.dropna().shape[0] > 0 else None
            val_str = normalize_val(val) or '缺失值'
            fail_one_results.append({
                'feature': col,
                'value': val_str,
                'fail_count': int(n_fail),
                'fail_ratio': 1.0
            })
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
            if val_str is None or val_str == '缺失值':
                continue
            if len(val_str) > 200:
                val_str = val_str[:197] + '...'

            lift = p_fail_arr[i] / p_base_arr[i] if p_base_arr[i] > 0 else 0
            fail_ratio = p_fail_arr[i]
            composite_score = lift_weight * lift + fail_ratio_weight * (fail_ratio * 100)

            if lift > 1.0:
                lift_results.append({
                    'feature': col,
                    'value': val_str,
                    'fail_count': int(fail_cnts[i]),
                    'base_count': int(base_cnts[i]),
                    'p_fail': round(float(p_fail_arr[i]), 6),
                    'p_base': round(float(p_base_arr[i]), 6),
                    'lift': round(float(lift), 4),
                    'fail_ratio': round(float(fail_ratio), 4),
                    'composite_score': round(float(composite_score), 4)
                })

            ratio_results.append({
                'feature': col,
                'value': val_str,
                'fail_count': int(fail_cnts[i]),
                'base_count': int(base_cnts[i]),
                'p_fail': round(float(p_fail_arr[i]), 6),
                'p_base': round(float(p_base_arr[i]), 6),
                'fail_ratio': round(float(fail_ratio), 4),
                'composite_score': round(float(composite_score), 4)
            })

    lift_results.sort(key=lambda x: x['composite_score'], reverse=True)
    ratio_results.sort(key=lambda x: x['fail_ratio'], reverse=True)
    return lift_results, ratio_results, fail_one_results


def get_top10_by_feature(results, sort_key='composite_score'):
    """每个特征只保留指定指标最高的一个取值，按该指标降序取TOP 10"""
    best_per_feature = {}
    for r in results:
        feat = r['feature']
        if feat not in best_per_feature or r[sort_key] > best_per_feature[feat][sort_key]:
            best_per_feature[feat] = r
    sorted_best = sorted(best_per_feature.values(), key=lambda x: x[sort_key], reverse=True)
    return sorted_best[:10]





def list_available_models(api_key, api_base):
    """查询 API 可用模型列表"""
    import requests
    url = f"{api_base.rstrip('/')}/models"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15
        )
        if response.status_code == 200:
            data = response.json()
            models = [m.get('id', m) if isinstance(m, dict) else m
                      for m in data.get('data', [])]
            return models
        return []
    except Exception:
        return []


def call_llm(api_key, api_base, model, top10_data, desc_map, failed_station, failure_mode, fail_one_data=None, hour_top10_data=None):
    """
    调用LLM生成结构化分析报告
    仅发送TOP 10聚集度数据，不发送原始数据
    """
    import requests

    if not api_key or len(top10_data) == 0:
        return None

    top10_with_desc = []
    for item in top10_data:
        feat = item['特征列']
        desc = desc_map.get(feat, '无描述')
        top10_with_desc.append({
            '特征列': feat,
            '特征含义': desc,
            '聚集取值': item['聚集取值'],
            '综合评分': item['综合评分'],
            '提升度Lift': item['提升度Lift'],
            'Fail内占比': item['Fail内占比'],
            'Fail出现次数': item['Fail出现次数'],
            '基准出现次数': item['基准出现次数'],
            'Fail集中度': item['Fail集中度'],
            '基准占比': item['基准占比']
        })

    top10_text = json.dumps(top10_with_desc, ensure_ascii=False, indent=2)

    fail_one_text = ""
    if fail_one_data and len(fail_one_data) > 0:
        fail_one_with_desc = []
        for item in fail_one_data:
            feat = item['feature']
            desc = desc_map.get(feat, '无描述')
            fail_one_with_desc.append({
                '特征列': feat,
                '特征含义': desc,
                '聚集取值': item['value'],
                'Fail出现次数': item['fail_count']
            })
        fail_one_text = "\n\n#### ⚠️ Fail 内完全一致的特征（唯一值=1）\n以下特征在当前 Fail 样本中只有一个唯一取值（100%集中），建议重点关注：\n" + json.dumps(fail_one_with_desc, ensure_ascii=False, indent=2)

    hour_text = ""
    if hour_top10_data and len(hour_top10_data) > 0:
        hour_items = []
        for item in hour_top10_data:
            hour_items.append({
                '工序': item['feature'].replace('_Hour', ''),
                '聚集小时': f"{item['value']}时",
                'Lift': item['lift'],
                'Fail内占比': f"{item['fail_ratio']*100:.1f}%",
                'Fail次数': item['fail_count']
            })
        hour_text = "\n\n#### ⏰ 时间小时段异常聚集\n以下为按小时聚合的时间类制程因素中，Fail 高度集中的时段：\n" + json.dumps(hour_items, ensure_ascii=False, indent=2)

    prompt = f"""你是一名资深的3C制造质量专家。我们对产线终检Fail的产品进行了全流程（约200个工序因素）的共性聚集度分析。
"提升度(Lift)"表示该因素在Fail产品中出现的频率远超正常水平。Lift值越高，该因素在Fail产品中越异常聚集。
"Fail内占比"表示该取值在当前Fail样本中的占比，不考虑基准频率。Fail内占比越高，说明该取值在NG中越集中。
"综合评分"是Lift和Fail内占比的加权组合，用于平衡两者。

当前筛选条件：Failed_Station={failed_station if failed_station else '全部'}，Failure_Mode={failure_mode if failure_mode else '全部'}

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
（指出哪些工序在哪些小时段Fail高度集中，可能提示换班、设备调试等时段性异常，必须引用工序完整列名）

请使用中文输出，简洁专业，避免冗余叙述。提到特征时必须使用完整的特征列名称（如VCM_M1_FPC_UpCoil_Attach_Staging_time），不要简写。"""

    url = f"{api_base.rstrip('/')}/chat/completions"

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            err_detail = response.text[:500]
            return f"LLM调用失败 (HTTP {response.status_code}): {err_detail}"
    except requests.exceptions.Timeout:
        return "LLM调用超时（90秒），请检查网络或稍后重试。"
    except Exception as e:
        return f"LLM调用异常: {str(e)[:300]}"


def _get_cache_key(start_date, end_date, failed_station, lift_weight):
    """生成计算结果的缓存键"""
    return f"{start_date}_{end_date}_{failed_station}_{lift_weight}"


def extract_hour_features(df):
    """检测时间列，抽取小时(0-23)作为离散特征，返回(修改后df, 新增小时特征列名列表)
    只处理 _End_Time 和 _Start_Time 列（实际时间戳），不处理 _Staging_time（持续秒数）
    """
    hour_cols = []
    time_pattern = re.compile(r'_End_Time$|_Start_Time$|_datetime$', re.IGNORECASE)
    n = len(df)
    if n == 0:
        return df, hour_cols

    for col in list(df.columns):
        if col in SKIP_COLS or not time_pattern.search(col):
            continue
        try:
            s = df[col]
            if str(s.dtype) == "category":
                s = s.astype(object)
            parsed = pd.to_datetime(s, errors="coerce", cache=True)
            valid_ratio = parsed.notna().sum() / n
            if valid_ratio < 0.3:
                continue
            hour_col = f"{col}_Hour"
            df[hour_col] = parsed.dt.hour.astype("Int64")
            hour_cols.append(hour_col)
        except Exception:
            continue
    return df, hour_cols


def main():
    st.title("终检Fail产品全流程共性聚集度分析")
    st.markdown("基于**提升度 (Lift)** 算法，从全流程 ~500 个离散工序因素中识别导致终检 Fail 的异常聚集特征。")

    # ── 侧边栏：数据源 ──
    st.sidebar.header("数据源")
    uploaded_file = st.sidebar.file_uploader("上传CSV文件（可选）", type=["csv"])
    use_default = st.sidebar.checkbox("使用本地 PRB_data.csv", value=True)

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

    # 用 session_state 缓存原始数据，避免每次交互重新读取
    need_reload = False
    if 'df_raw' not in st.session_state:
        need_reload = True
    if uploaded_file is not None and st.session_state.get('uploaded_name') != uploaded_file.name:
        need_reload = True
    if uploaded_file is None and st.session_state.get('uploaded_name') is not None:
        need_reload = True

    if need_reload:
        if uploaded_file is not None:
            st.session_state['df_raw'] = load_data(uploaded_file=uploaded_file)
            st.session_state['uploaded_name'] = uploaded_file.name
        elif use_default:
            with st.spinner("首次加载数据..."):
                st.session_state['df_raw'] = load_data(file_path=DATA_PATH, dashboard_cols=dashboard_cols)
            st.session_state['uploaded_name'] = None
        else:
            st.sidebar.warning("请上传CSV文件或勾选使用本地数据")
            st.info("请先在左侧配置数据源。")
            return

    df_raw = st.session_state['df_raw']

    if df_raw is None:
        st.error("未能加载数据。请检查文件路径或上传有效文件。")
        return

    # ── 侧边栏：数据概览 ──
    with st.sidebar.expander("数据概览", expanded=False):
        st.write(f"总行数: {len(df_raw):,}")
        st.write(f"总列数: {len(df_raw.columns)}")

    # ── 校验必要列 ──
    required_cols = ['Results', 'Failed_Station', 'Failure_Mode']
    missing_required = [c for c in required_cols if c not in df_raw.columns]
    if missing_required:
        st.error(f"数据缺少必要列: {missing_required}。可用列: {list(df_raw.columns)[:30]}")
        return

    # ── 时间列确定 ──
    date_col = 'Date'
    if date_col not in df_raw.columns:
        candidates = [c for c in df_raw.columns if 'date' in c.lower()]
        if candidates:
            date_col = st.sidebar.selectbox("选择日期列", candidates)
        else:
            st.error("未找到 Date 列，无法按日期筛选。")
            return

    # ── 侧边栏：筛选条件 ──
    st.sidebar.header("筛选条件")

    # 缓存日期转换结果
    if 'date_col_converted' not in st.session_state or st.session_state.get('date_col_name') != date_col:
        df_raw[date_col] = pd.to_datetime(df_raw[date_col], errors='coerce')
        st.session_state['date_col_converted'] = True
        st.session_state['date_col_name'] = date_col

    date_min = df_raw[date_col].min().date()
    date_max = df_raw[date_col].max().date()

    date_range = st.sidebar.date_input(
        "日期范围",
        value=(date_min, date_max),
        min_value=date_min,
        max_value=date_max
    )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    elif hasattr(date_range, '__iter__') and not isinstance(date_range, str):
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
        help="Lift 在综合评分中的权重，Fail内占比自动占剩余权重"
    )
    fail_ratio_weight = 1.0 - lift_weight
    st.sidebar.caption(f"Fail内占比权重: {fail_ratio_weight:.2f}")

    # 日期筛选（缓存）
    date_mask = (df_raw[date_col].dt.date >= start_date) & (df_raw[date_col].dt.date <= end_date)
    df_date_filtered = df_raw[date_mask]

    if len(df_date_filtered) == 0:
        st.warning("所选日期范围内无数据，请调整日期范围。")
        return

    station_values = df_date_filtered['Failed_Station'].dropna().unique()
    station_options = ["全部"] + sorted(station_values.tolist())
    failed_station = st.sidebar.selectbox("Failed_Station", station_options, index=0)

    # ── 开始分析按钮 ──
    analyze_btn = st.sidebar.button("开始分析", type="primary", use_container_width=True)

    # 生成缓存键
    cache_key = _get_cache_key(start_date, end_date, failed_station, lift_weight)

    # 如果参数变化，清除旧缓存
    if st.session_state.get('last_cache_key') != cache_key:
        for k in ['lift_results', 'ratio_results', 'fail_one_results', 'top10',
                   'df_base', 'df_fail', 'n_base', 'n_fail', 'all_feature_cols',
                   'hour_lift_results', 'hour_top10', 'hour_ratio_results', 'hour_ratio_top10']:
            st.session_state.pop(k, None)
        st.session_state['last_cache_key'] = cache_key

    # 有缓存时直接显示结果，无需再次点击按钮
    has_cache = 'lift_results' in st.session_state

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
        lift_results = st.session_state['lift_results']
        ratio_results = st.session_state['ratio_results']
        fail_one_results = st.session_state['fail_one_results']
        top10 = st.session_state['top10']
        df_base = st.session_state['df_base']
        df_fail = st.session_state['df_fail']
        n_base = st.session_state['n_base']
        n_fail = st.session_state['n_fail']
        all_feature_cols = st.session_state['all_feature_cols']
        hour_lift_results = st.session_state.get('hour_lift_results', [])
        hour_top10 = st.session_state.get('hour_top10', [])
        hour_ratio_results = st.session_state.get('hour_ratio_results', [])
        hour_ratio_top10 = st.session_state.get('hour_ratio_top10', [])
    else:
        # 重新计算
        with st.spinner("正在准备数据..."):
            df_pass = df_date_filtered[df_date_filtered['Results'] == 'PASS']
            if failed_station != "全部":
                df_station_all = df_date_filtered[df_date_filtered['Failed_Station'] == failed_station]
            else:
                df_station_all = df_date_filtered
            df_base = pd.concat([df_pass, df_station_all]).drop_duplicates()
            df_fail = df_station_filtered = df_date_filtered[
                (df_date_filtered['Results'] == 'FAIL') if failed_station == "全部"
                else (df_date_filtered['Results'] == 'FAIL') & (df_date_filtered['Failed_Station'] == failed_station)
            ]

            n_base = len(df_base)
            n_fail = len(df_fail)

        if n_fail == 0:
            st.error("## 无Fail数据\n当前筛选条件下 Fail 产品数量为 0，请调整筛选条件。")
            return

        fail_rate = n_fail / n_base * 100

        # 概览指标卡
        col1, col2, col3 = st.columns(3)
        col1.metric("基准总数", f"{n_base:,}")
        col2.metric("Fail数量", f"{n_fail:,}")
        col3.metric("Fail率", f"{fail_rate:.2f}%")

        if dashboard_cols:
            all_feature_cols = [c for c in dashboard_cols if c in df_base.columns and c not in SKIP_COLS]
        else:
            time_cols, id_cols, meta_cols, discrete_cols = classify_columns(df_base)
            all_feature_cols = [c for c in discrete_cols if c in df_base.columns]

        filtered_cols = []
        for col in all_feature_cols:
            if df_base[col].nunique(dropna=True) >= 2:
                filtered_cols.append(col)
        all_feature_cols = filtered_cols

        st.info(f"**特征统计**：参与 Lift 计算的工序离散特征共 {len(all_feature_cols)} 列（已排除常量列）")
        st.info(f"**综合评分权重**：Lift={lift_weight:.0%} | Fail内占比={fail_ratio_weight:.0%}")

        lift_progress = st.progress(0)
        lift_progress_text = st.empty()
        lift_progress_text.markdown("计算提升度...")
        st.session_state['lift_progress'] = lift_progress
        st.session_state['lift_progress_text'] = lift_progress_text

        with st.spinner("正在计算全特征提升度（Lift）..."):
            lift_results, ratio_results, fail_one_results = compute_lift(
                df_base, df_fail, all_feature_cols, lift_weight=lift_weight, min_count=3
            )

        lift_progress_text.markdown("正在提取时间小时特征...")
        time_cols, _, _, _ = classify_columns(df_base)
        if time_cols:
            df_base, hour_cols = extract_hour_features(df_base)
            df_fail, _ = extract_hour_features(df_fail)
            if hour_cols:
                lift_progress_text.markdown("正在计算时间类提升度（小时）...")
                hour_results, hour_ratio_raw, _ = compute_lift(
                    df_base, df_fail, hour_cols, lift_weight=lift_weight, min_count=1
                )
                hour_lift_results = hour_results
                hour_ratio_results = hour_ratio_raw
            else:
                hour_lift_results = []
        else:
            hour_lift_results = []
        hour_top10 = get_top10_by_feature(hour_lift_results) if hour_lift_results else []
        hour_ratio_top10 = (get_top10_by_feature(hour_ratio_results, sort_key='fail_ratio')
                            if hour_ratio_results else [])

        lift_progress.progress(1.0)
        lift_progress_text.markdown("分析完成!")

        top10 = get_top10_by_feature(lift_results)

        # 缓存到 session_state
        st.session_state['lift_results'] = lift_results
        st.session_state['ratio_results'] = ratio_results
        st.session_state['fail_one_results'] = fail_one_results
        st.session_state['top10'] = top10
        st.session_state['df_base'] = df_base
        st.session_state['df_fail'] = df_fail
        st.session_state['n_base'] = n_base
        st.session_state['n_fail'] = n_fail
        st.session_state['all_feature_cols'] = all_feature_cols
        st.session_state['hour_lift_results'] = hour_lift_results
        st.session_state['hour_top10'] = hour_top10
        st.session_state['hour_ratio_results'] = hour_ratio_results
        st.session_state['hour_ratio_top10'] = hour_ratio_top10

    # 从缓存恢复后也需要显示指标卡
    if has_cache and not analyze_btn:
        fail_rate = n_fail / n_base * 100
        col1, col2, col3 = st.columns(3)
        col1.metric("基准总数", f"{n_base:,}")
        col2.metric("Fail数量", f"{n_fail:,}")
        col3.metric("Fail率", f"{fail_rate:.2f}%")

    st.success(f"共发现 **{len(lift_results)}** 条聚集特征（Lift > 1.0 且 Fail 出现 ≥ 3 次）")
    st.info(f"每个特征取综合评分最高代表 → **TOP {len(top10)}** 特征")

    # ═══════════════════════════════════════════════
    # 可视化图表区域 (Tabs)
    # ═══════════════════════════════════════════════

    st.markdown("---")
    st.subheader("可视化分析")

    if len(top10) == 0:
        st.warning("未发现显著的聚集特征（所有 Lift 值均 ≤ 1.0）。")
        return

    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 共性聚集度排行榜(Lift)",
        "📉 Fail内占比排行榜",
        "🔍 单因子 Pass/Fail 对比",
        "⏰ 时间小时聚集度(Hour)"
    ])

    # ── Tab 1: TOP 10 水平条形图 ──
    with tab1:
        df_chart = pd.DataFrame(top10)
        df_chart['label'] = df_chart.apply(
            lambda r: f"{r['feature'][:55]} → {r['value']}", axis=1
        )
        df_chart = df_chart.sort_values('lift', ascending=True)

        bar_fig = go.Figure()
        bar_fig.add_trace(go.Bar(
            y=df_chart['label'],
            x=df_chart['lift'],
            orientation='h',
            marker=dict(
                color=df_chart['lift'],
                colorscale='Reds',
                showscale=True,
                colorbar=dict(title='Lift')
            ),
            text=df_chart.apply(
                lambda r: f"Lift={r['lift']:.1f} | Fail#{r['fail_count']}",
                axis=1
            ),
            textposition='outside',
            textfont=dict(size=11),
            hovertemplate=(
                '<b>特征列</b>: %{customdata[0]}<br>'
                '<b>聚集取值</b>: %{customdata[1]}<br>'
                '<b>Lift</b>: %{x:.2f}<br>'
                '<b>Fail次数</b>: %{customdata[2]}<br>'
                '<b>基准次数</b>: %{customdata[3]}<br>'
                '<b>Fail集中度</b>: %{customdata[4]:.2%}<br>'
                '<b>基准占比</b>: %{customdata[5]:.2%}<br>'
                '<extra></extra>'
            ),
            customdata=df_chart[['feature', 'value', 'fail_count', 'base_count', 'p_fail', 'p_base']].values
        ))

        bar_fig.add_vline(
            x=1.0, line_dash="dash", line_color="gray",
            annotation_text="Lift=1.0 基准线", annotation_position="top"
        )

        bar_fig.update_layout(
            title='TOP 10 离散工序因素 Fail 聚集度（Lift）',
            xaxis_title='Lift（提升度 → 越高越异常）',
            yaxis=dict(title='', tickfont=dict(size=10)),
            height=650,
            margin=dict(l=20, r=120, t=50, b=20),
            showlegend=False
        )

        st.plotly_chart(bar_fig, use_container_width=True)

    # ── Tab 2: Fail 内占比排行榜 ──
    with tab2:
        ratio_filtered = [r for r in ratio_results if r['fail_ratio'] > 0.20]
        top10_ratio = get_top10_by_feature(ratio_filtered, sort_key='fail_ratio')

        if len(top10_ratio) == 0:
            st.warning("无 Fail 内占比数据可用于展示（需 fail_ratio > 20% 且非缺失值）。")
        else:
            df_ratio_top = pd.DataFrame(top10_ratio)
            df_ratio_top['label'] = df_ratio_top.apply(
                lambda r: f"{r['feature'][:55]} → {r['value']}", axis=1
            )
            df_ratio_top = df_ratio_top.sort_values('fail_ratio', ascending=True)

            ratio_fig = go.Figure()
            ratio_fig.add_trace(go.Bar(
                y=df_ratio_top['label'],
                x=df_ratio_top['fail_ratio'] * 100,
                orientation='h',
                marker=dict(
                    color=df_ratio_top['fail_ratio'],
                    colorscale='Blues',
                    showscale=True,
                    colorbar=dict(title='Fail占比')
                ),
                text=df_ratio_top.apply(
                    lambda r: f"{r['fail_ratio']*100:.1f}% | Fail#{r['fail_count']}",
                    axis=1
                ),
                textposition='outside',
                textfont=dict(size=11),
                hovertemplate=(
                    '<b>特征列</b>: %{customdata[0]}<br>'
                    '<b>聚集取值</b>: %{customdata[1]}<br>'
                    '<b>Fail内占比</b>: %{x:.2f}%<br>'
                    '<b>Fail次数</b>: %{customdata[2]}<br>'
                    '<b>基准次数</b>: %{customdata[3]}<br>'
                    '<extra></extra>'
                ),
                customdata=df_ratio_top[['feature', 'value', 'fail_count', 'base_count']].values
            ))

            ratio_fig.update_layout(
                title='TOP 10 特征取值在 Fail 样本内的占比（不考虑基准频率）',
                xaxis_title='Fail 内占比 (%) → 越高说明该取值在 NG 中越集中',
                yaxis=dict(title='', tickfont=dict(size=10)),
                height=650,
                margin=dict(l=20, r=120, t=50, b=20),
                showlegend=False
            )

            st.plotly_chart(ratio_fig, use_container_width=True)

            remaining = [r for r in ratio_filtered if r['feature'] not in {x['feature'] for x in top10_ratio}]
            if remaining:
                with st.expander(f"查看剩余 Fail 内占比 > 20% 的特征（共 {len(remaining)} 条）"):
                    df_rem = pd.DataFrame(remaining)
                    df_rem = df_rem.sort_values('fail_ratio', ascending=False)
                    st.dataframe(
                        df_rem[['feature', 'value', 'fail_count', 'fail_ratio']].rename(columns={
                            'feature': '特征列',
                            'value': '聚集取值',
                            'fail_count': 'Fail次数',
                            'fail_ratio': 'Fail内占比'
                        }),
                        use_container_width=True
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
                feat = top10[rank]['feature']
                val = top10[rank]['value']
                lift_val = top10[rank]['lift']
                fail_cnt = top10[rank]['fail_count']

                if feat not in df_base.columns:
                    continue

                st.markdown(f"**TOP{rank+1}**: `{feat}` (Lift={lift_val})")

                # 向量化：用 value_counts 一次性计算，避免逐值循环
                base_vc = df_base[feat].apply(normalize_val).value_counts()
                fail_vc = df_fail[feat].apply(normalize_val).value_counts()

                top_vals = base_vc.head(20).index.tolist()
                if val not in top_vals:
                    top_vals.insert(0, val)

                df_plot_data = []
                for v in top_vals:
                    b_cnt = int(base_vc.get(v, 0))
                    f_cnt = int(fail_vc.get(v, 0))
                    df_plot_data.append({
                        '取值': str(v)[:40],
                        'Pass': b_cnt - f_cnt,
                        'Fail': f_cnt
                    })

                df_plot = pd.DataFrame(df_plot_data)

                comp_fig = go.Figure()
                comp_fig.add_trace(go.Bar(
                    name='Pass',
                    x=df_plot['取值'],
                    y=df_plot['Pass'],
                    marker_color='#4ECDC4',
                    hovertemplate='Pass: %{y}<extra></extra>'
                ))
                comp_fig.add_trace(go.Bar(
                    name='Fail',
                    x=df_plot['取值'],
                    y=df_plot['Fail'],
                    marker_color='#FF6B6B',
                    hovertemplate='Fail: %{y}<extra></extra>'
                ))

                comp_fig.update_layout(
                    title=dict(
                        text=f'{feat[:60]}',
                        x=0.5,
                        xanchor='center',
                        y=0.98,
                        yanchor='top',
                        font=dict(size=13)
                    ),
                    xaxis_title='特征取值',
                    yaxis_title='样本数量',
                    barmode='group',
                    height=380,
                    margin=dict(l=20, r=20, t=60, b=80),
                    legend=dict(
                        orientation='h',
                        yanchor='bottom',
                        y=1.15,
                        x=0.5,
                        xanchor='center'
                    ),
                    xaxis=dict(tickfont=dict(size=9), tickangle=30)
                )

                st.plotly_chart(comp_fig, use_container_width=True)
                st.markdown("---")

    # ── Tab 4: 时间小时特征聚集度排行榜 ──
    with tab4:
        if not hour_lift_results:
            st.info("未发现时间类特征或时间数据不足，无法计算小时级聚集度。")
        else:
            st.caption("将时间类制程因素的时分秒按小时(0-23)聚合后计算 Lift / Fail内占比，识别 Fail 集中在哪些小时时段。")

            sub_tab_lift, sub_tab_ratio = st.tabs(["⏰ 小时 Lift 排行", "⏰ 小时 Fail 占比排行"])

            with sub_tab_lift:
                df_hour = pd.DataFrame(hour_top10)
                df_hour['label'] = df_hour.apply(
                    lambda r: f"{r['feature'].replace('_Hour','')[:45]} → {r['value']}时", axis=1
                )
                df_hour = df_hour.sort_values('lift', ascending=True)

                hour_fig = go.Figure()
                hour_fig.add_trace(go.Bar(
                    y=df_hour['label'],
                    x=df_hour['lift'],
                    orientation='h',
                    marker=dict(
                        color=df_hour['lift'],
                        colorscale='Tealgrn',
                        showscale=True,
                        colorbar=dict(title='Lift')
                    ),
                    text=df_hour.apply(
                        lambda r: f"Lift={r['lift']:.2f} | Fail#{r['fail_count']}",
                        axis=1
                    ),
                    textposition='outside',
                    textfont=dict(size=11),
                    hovertemplate=(
                        '<b>工序</b>: %{customdata[0]}<br>'
                        '<b>小时</b>: %{customdata[1]}时<br>'
                        '<b>Lift</b>: %{x:.2f}<br>'
                        '<b>Fail次数</b>: %{customdata[2]}<br>'
                        '<b>基准次数</b>: %{customdata[3]}<br>'
                        '<b>Fail集中度</b>: %{customdata[4]:.2%}<br>'
                        '<b>基准占比</b>: %{customdata[5]:.2%}<br>'
                        '<extra></extra>'
                    ),
                    customdata=df_hour[['feature', 'value', 'fail_count', 'base_count', 'p_fail', 'p_base']].values
                ))

                hour_fig.add_vline(
                    x=1.0, line_dash="dash", line_color="gray",
                    annotation_text="Lift=1.0 基准线", annotation_position="top"
                )

                hour_fig.update_layout(
                    title='时间类工序特征按小时聚集度 TOP',
                    xaxis_title='Lift（提升度 → 越高越异常）',
                    yaxis=dict(title='', tickfont=dict(size=10)),
                    height=650,
                    margin=dict(l=20, r=120, t=50, b=20),
                    showlegend=False
                )

                st.plotly_chart(hour_fig, use_container_width=True)

                with st.expander("查看完整时间小时特征数据"):
                    df_hour_display = df_hour.copy()
                    df_hour_display['工序'] = df_hour_display['feature'].str.replace('_Hour', '')
                    df_hour_display['小时'] = df_hour_display['value'].apply(lambda x: f"{x}时")
                    df_hour_display['Fail集中度'] = df_hour_display['p_fail'].apply(lambda x: f"{x*100:.2f}%")
                    df_hour_display['基准占比'] = df_hour_display['p_base'].apply(lambda x: f"{x*100:.2f}%")
                    st.dataframe(
                        df_hour_display[['工序', '小时', 'lift', 'fail_count', 'base_count', 'Fail集中度', '基准占比']].rename(columns={
                            'lift': 'Lift', 'fail_count': 'Fail次数', 'base_count': '基准次数'
                        }),
                        use_container_width=True
                    )

                st.markdown("""
                **说明**：将时间类制程因素按小时聚合（0-23时），计算每个小时段的 Lift 值。
                Lift > 1 表示 Fail 产品在该小时段的占比高于基准水平，可能提示该时段存在异常（如换班、设备调试等）。
                """)

            with sub_tab_ratio:
                ratio_filtered = [r for r in hour_ratio_results if r['fail_ratio'] > 0.05] if hour_ratio_results else []
                hour_ratio_top10_show = get_top10_by_feature(ratio_filtered, sort_key='fail_ratio') if ratio_filtered else []

                if not hour_ratio_top10_show:
                    st.warning("无时间类 Fail 内占比数据可用于展示。")
                else:
                    df_hour_ratio = pd.DataFrame(hour_ratio_top10_show)
                    df_hour_ratio['label'] = df_hour_ratio.apply(
                        lambda r: f"{r['feature'].replace('_Hour','')[:45]} → {r['value']}时", axis=1
                    )
                    df_hour_ratio = df_hour_ratio.sort_values('fail_ratio', ascending=True)

                    ratio_fig = go.Figure()
                    ratio_fig.add_trace(go.Bar(
                        y=df_hour_ratio['label'],
                        x=df_hour_ratio['fail_ratio'] * 100,
                        orientation='h',
                        marker=dict(
                            color=df_hour_ratio['fail_ratio'],
                            colorscale='Tealgrn',
                            showscale=True,
                            colorbar=dict(title='Fail占比')
                        ),
                        text=df_hour_ratio.apply(
                            lambda r: f"{r['fail_ratio']*100:.1f}% | Fail#{r['fail_count']}",
                            axis=1
                        ),
                        textposition='outside',
                        textfont=dict(size=11),
                        hovertemplate=(
                            '<b>工序</b>: %{customdata[0]}<br>'
                            '<b>小时</b>: %{customdata[1]}时<br>'
                            '<b>Fail内占比</b>: %{x:.2f}%<br>'
                            '<b>Fail次数</b>: %{customdata[2]}<br>'
                            '<b>基准次数</b>: %{customdata[3]}<br>'
                            '<extra></extra>'
                        ),
                        customdata=df_hour_ratio[['feature', 'value', 'fail_count', 'base_count']].values
                    ))

                    ratio_fig.update_layout(
                        title='时间类工序特征按小时 Fail 内占比 TOP',
                        xaxis_title='Fail 内占比 (%) → 越高说明该时段在 NG 中越集中',
                        yaxis=dict(title='', tickfont=dict(size=10)),
                        height=650,
                        margin=dict(l=20, r=120, t=50, b=20),
                        showlegend=False
                    )

                    st.plotly_chart(ratio_fig, use_container_width=True)

                    remaining = [r for r in ratio_filtered if r['feature'] not in {x['feature'] for x in hour_ratio_top10_show}]
                    if remaining:
                        with st.expander(f"查看其余小时 Fail 内占比 > 5% 的特征（共 {len(remaining)} 条）"):
                            df_rem = pd.DataFrame(remaining)
                            df_rem = df_rem.sort_values('fail_ratio', ascending=False)
                            df_rem['工序'] = df_rem['feature'].str.replace('_Hour', '')
                            df_rem['小时'] = df_rem['value'].apply(lambda x: f"{x}时")
                            st.dataframe(
                                df_rem[['工序', '小时', 'fail_count', 'fail_ratio']].rename(columns={
                                    'fail_count': 'Fail次数',
                                    'fail_ratio': 'Fail内占比'
                                }),
                                use_container_width=True
                            )

                    st.markdown("""
                    **说明**：仅统计该小时段在当前 Fail 样本中的占比，不考虑其在整体基准数据中的分布频率。
                    例如某工序在 14 时的 Fail 占全部 Fail 的 78.6%，说明该时段在 NG 样本中高度集中。
                    """)

    # ═══════════════════════════════════════════════
    # 详细数据表
    # ═══════════════════════════════════════════════

    st.markdown("---")
    st.subheader("TOP 10 详细数据")

    df_display = pd.DataFrame(top10)
    df_display.insert(0, '排名', range(1, len(df_display) + 1))
    df_display['Fail集中度'] = df_display['p_fail'].apply(lambda x: f"{x*100:.2f}%")
    df_display['基准占比'] = df_display['p_base'].apply(lambda x: f"{x*100:.2f}%")
    df_display['Fail内占比'] = df_display['fail_ratio'].apply(lambda x: f"{x*100:.1f}%")

    display_cols = ['排名', 'feature', 'value', 'composite_score', 'lift', 'fail_count', 'base_count', 'Fail集中度', '基准占比', 'Fail内占比']
    st.dataframe(
        df_display[display_cols].rename(columns={
            'feature': '特征列', 'value': '聚集取值', 'lift': 'Lift',
            'fail_count': 'Fail次数', 'base_count': '基准次数',
            'composite_score': f'综合评分(Lift{lift_weight:.0%}+Fail{fail_ratio_weight:.0%})'
        }),
        use_container_width=True
    )

    with st.expander("查看全部 Lift > 1.0 的聚集特征"):
        full_results = [r for r in lift_results if r['lift'] > 1.0]
        if full_results:
            df_full = pd.DataFrame(full_results)
            df_full['Fail集中度'] = df_full['p_fail'].apply(lambda x: f"{x*100:.2f}%")
            df_full['基准占比'] = df_full['p_base'].apply(lambda x: f"{x*100:.2f}%")
            df_full['Fail内占比'] = df_full['fail_ratio'].apply(lambda x: f"{x*100:.1f}%")
            display_cols_full = ['feature', 'value', 'composite_score', 'lift', 'fail_count', 'base_count', 'Fail集中度', '基准占比', 'Fail内占比']
            st.dataframe(
                df_full[display_cols_full].rename(columns={
                    'feature': '特征列', 'value': '聚集取值', 'lift': 'Lift',
                    'fail_count': 'Fail次数', 'base_count': '基准次数',
                    'composite_score': f'综合评分(Lift{lift_weight:.0%}+Fail{fail_ratio_weight:.0%})'
                }),
                use_container_width=True
            )
        else:
            st.write("无额外 Lift > 1.0 的特征")

    # Fail 内唯一值=1 的列单独展示
    if fail_one_results:
        st.markdown("---")
        st.subheader("⚠️ Fail 内唯一值=1 的特征（所有 Fail 样本取值完全一致）")
        df_fail_one = pd.DataFrame(fail_one_results)
        df_fail_one['Fail内占比'] = df_fail_one['fail_ratio'].apply(lambda x: f"{x*100:.1f}%")
        st.dataframe(
            df_fail_one[['feature', 'value', 'fail_count', 'Fail内占比']].rename(columns={
                'feature': '特征列', 'value': '聚集取值', 'fail_count': 'Fail次数'
            }),
            use_container_width=True
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

        top10_for_llm = [{
            '排位': i + 1,
            '特征列': item['feature'],
            '聚集取值': item['value'],
            '综合评分': item['composite_score'],
            '提升度Lift': item['lift'],
            'Fail内占比': f"{item['fail_ratio']*100:.1f}%",
            'Fail出现次数': item['fail_count'],
            '基准出现次数': item['base_count'],
            'Fail集中度': f"{item['p_fail']*100:.2f}%",
            '基准占比': f"{item['p_base']*100:.2f}%"
        } for i, item in enumerate(top10)]

        try:
            hour_top10_for_llm = [{
                'feature': item['feature'],
                'value': item['value'],
                'lift': item['lift'],
                'fail_ratio': item['fail_ratio'],
                'fail_count': item['fail_count']
            } for item in hour_top10] if hour_top10 else None

            llm_report = call_llm(LLM_API_KEY, LLM_API_BASE, LLM_MODEL, top10_for_llm, desc_map, failed_station, "全部", fail_one_data=fail_one_results, hour_top10_data=hour_top10_for_llm)
            if llm_report:
                llm_report_placeholder.empty()
                st.markdown(llm_report)
            else:
                llm_report_placeholder.warning("LLM 报告生成失败，请检查 API Key 和网络连接。")
        except Exception as e:
            llm_report_placeholder.error(f"LLM 调用异常: {str(e)[:300]}")
    else:
        llm_report_placeholder.info("未配置 LLM API Key，跳过报告生成。")


if __name__ == "__main__":
    main()