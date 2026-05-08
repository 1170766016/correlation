import os, gc, random, json
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta

# ===================== PAGE CONFIG =====================
st.set_page_config(
    page_title="终检Fail产品全流程共性聚集度分析",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ===================== CONSTANTS =====================
SKIP_COLS = {
    'sn', 'Serial_No', 'Serial_No_18',
    'transdatetime', 'insertdatetime',
    'Rev', 'Site', 'Results',
    'Failed_Station', 'Failure_Mode'
}

# LLM 配置
LLM_API_URL = os.getenv("COMPANY_LLM_URL", "http://your-company-api-endpoint/v1/chat/completions")
LLM_MODEL = os.getenv("COMPANY_MODEL_NAME", "company-model-v1")

# ===================== FOCUS COLUMNS (WHITE LIST) =====================
# 关注的约 200 个列名
FOCUS_COLS = [
    'Project', 'Build', 'Config', 'Aging_Mode', 'Agingfacedown_Mode', 'ME_Mode', 'Serial_No',
    'VCM_M1_FPC_Load_End_Time', 'VCM_M1_FPC_UpCoil_Attach_MC_ID', 'VCM_M1_FPC_UpCoil_Attach_Start_Time',
    'VCM_M1_FPC_UpCoil_Attach_End_Time', 'VCM_M1_FPC_UpCoil_Attach_Staging_time',
    'VCM_M1_FPC_DownCoil_Attach_Start_Time', 'VCM_M1_FPC_DownCoil_Attach_End_Time',
    'VCM_M1_FPC_DownCoil_Attach_Staging_time', 'VCM_M1_FPC_Coil_Baking_MC_ID',
    'VCM_M1_FPC_Coil_Baking_Start_Time', 'VCM_M1_FPC_Coil_Baking_End_Time',
    'VCM_M1_FPC_Coil_Baking_Staging_time', 'VCM_M1_Jet_Soldering_MC_ID',
    'VCM_M1_Jet_Soldering_MC_Head_ID', 'VCM_M1_Jet_Soldering_End_Time',
    'VCM_M2_Stator_Load_End_Time', 'VCM_M2_Stator_SpinClean_MC_ID', 'VCM_M2_Stator_SpinClean_End_Time',
    'VCM_M2_Stator_Plasma_MC_ID', 'VCM_M2_Stator_Plasma_Start_Time', 'VCM_M2_Stator_Plasma_End_Time',
    'VCM_M2_Stator_Plasma_Staging_time', 'VCM_M2_Stator_FPC_Attach_MC_ID',
    'VCM_M2_Stator_FPC_Attach_MC_Head_ID', 'VCM_M2_Stator_FPC_Attach_Start_Time',
    'VCM_M2_Stator_FPC_Attach_End_Time', 'VCM_M2_Stator_FPC_Attach_Staging_time',
    'VCM_M2_Stator_Soma_Attach_MC_ID', 'VCM_M2_Stator_Soma_Attach_End_Time',
    'VCM_M2_StatorSubAssy_Baking_MC_ID', 'VCM_M2_StatorSubAssy_Baking_Start_Time',
    'VCM_M2_StatorSubAssy_Baking_End_Time', 'VCM_M2_StatorSubAssy_Baking_Staging_time',
    'VCM_M3_BCA_Plasma_MC_ID', 'VCM_M3_BCA_Plasma_Start_Time', 'VCM_M3_BCA_Plasma_End_Time',
    'VCM_M3_BCA_Plasma_Staging_time', 'VCM_M3_BCA_Bending_MC_ID', 'VCM_M3_BCA_Bending_MC_Head_ID',
    'VCM_M3_BCA_Bending_Socket', 'VCM_M3_BCA_Bending_Start_Time', 'VCM_M3_BCA_Bending_End_Time',
    'VCM_M3_BCA_Bending_Staging_time', 'VCM_M3_BCA_Baking_MC_ID', 'VCM_M3_BCA_Baking_Start_Time',
    'VCM_M3_BCA_Baking_End_Time', 'VCM_M3_BCA_Baking_Staging_time', 'VCM_M3_SpinClean_MC_ID',
    'VCM_M3_SpinClean_End_Time', 'VCM_M3_StatorAssy_Baking_MC_ID', 'VCM_M3_StatorAssy_Baking_Start_Time',
    'VCM_M3_StatorAssy_Baking_End_Time', 'VCM_M3_StatorAssy_Baking_Staging_time',
    'VCM_M4_Rotor_Load_End_Time', 'VCM_M4_Rotor_SpinClean_MC_ID', 'VCM_M4_Rotor_SpinClean_End_Time',
    'VCM_M4_Rotor_Plasma_MC_ID', 'VCM_M4_Rotor_Plasma_Start_Time', 'VCM_M4_Rotor_Plasma_End_Time',
    'VCM_M4_Rotor_Plasma_Staging_time', 'VCM_M4_Rotor_Magnet_Dispensing_MC_ID',
    'VCM_M4_Rotor_Magnet_Dispensing_MC_Head_ID', 'VCM_M4_Rotor_Magnet_Dispensing_Start_Time',
    'VCM_M4_Rotor_Magnet_Dispensing_End_Time', 'VCM_M4_Rotor_Magnet_Attach_MC_ID',
    'VCM_M4_Rotor_Magnet_Attach_MC_Head_ID', 'VCM_M4_Rotor_Magnet_Attach_Start_Time',
    'VCM_M4_Rotor_Magnet_Attach_End_Time', 'VCM_M4_Rotor_Magnet_Attach_Staging_time',
    'VCM_M4_Rotor_Magnet_Baking_MC_ID', 'VCM_M4_Rotor_Magnet_Baking_Start_Time',
    'VCM_M4_Rotor_Magnet_Baking_End_Time', 'VCM_M4_Rotor_Magnet_Baking_Staging_time',
    'VCM_M4_RotorAssy_SpinClean_MC_ID', 'VCM_M4_RotorAssy_SpinClean_End_Time',
    'VCM_M4_Rotor_Grease_Dispensing_MC_ID', 'VCM_M4_Rotor_Grease_Dispensing_MC_Head_ID',
    'VCM_M4_Rotor_Grease_Dispensing_End_Time', 'VCM_M4_RotorAssy_Baking_MC_ID',
    'VCM_M4_RotorAssy_Baking_Start_Time', 'VCM_M4_RotorAssy_Baking_End_Time',
    'VCM_M4_RotorAssy_Baking_Staging_time', 'VCM_M5_ShiledCan_Load_End_Time',
    'VCM_M5_ShiledCan_2DBC_MC_ID', 'VCM_M5_ShiledCan_2DBC_Socket', 'VCM_M5_ShiledCan_2DBC_End_Time',
    'VCM_M5_ShiledCan_SpinClean_MC_ID', 'VCM_M5_ShiledCan_SpinClean_MGZ_ID',
    'VCM_M5_ShiledCan_SpinClean_End_Time', 'VCM_M5_ShieldCan_Shim_Attach_MC_ID',
    'VCM_M5_ShieldCan_Shim_Attach_MC_Head_ID', 'VCM_M5_ShieldCan_Shim_Attach_End_Time',
    'VCM_M5_ShieldCan_Shim_AutoClave_MC_ID', 'VCM_M5_ShieldCan_Shim_AutoClave_Start_Time',
    'VCM_M5_ShieldCan_Shim_AutoClave_End_Time', 'VCM_M5_ShieldCan_Shim_AutoClave_Staging_time',
    'VCM_M6_StatorAssy_Load_End_Time', 'VCM_M6_StatorAssy_Plasma_MC_ID',
    'VCM_M6_StatorAssy_Plasma_Start_Time', 'VCM_M6_StatorAssy_Plasma_End_Time',
    'VCM_M6_StatorAssy_Plasma_Staging_time', 'VCM_M6_StatorAssy_Grease_MC_ID',
    'VCM_M6_StatorAssy_Grease_MC_Head_ID', 'VCM_M6_StatorAssy_Grease_End_Time',
    'VCM_M6_Ball_Rotor_Assy_MC_ID', 'VCM_M6_Ball_Rotor_Assy_End_Time', 'VCM_M6_Blade_Assembly_MC_ID',
    'VCM_M6_Blade_Assembly_MC_Head_ID', 'VCM_M6_Blade_Assembly_End_Time',
    'VCM_M6_ShieldCan_Dispensing_MC_ID', 'VCM_M6_ShieldCan_Dispensing_Head_ID',
    'VCM_M6_ShieldCan_Dispensing_End_Time', 'VCM_M6_ShieldCan_Assy_MC_ID',
    'VCM_M6_ShieldCan_Assy_End_Time', 'VCM_M6_ShieldCan_Baking_MC_ID',
    'VCM_M6_ShieldCan_Baking_Start_Time', 'VCM_M6_ShieldCan_Baking_End_Time',
    'VCM_M6_ShieldCan_Baking_Staging_time', 'VCM_XRay_MC_ID', 'VCM_XRay_End_Time',
    'VCM_M7_AgGlue1_MC_ID', 'VCM_M7_AgGlue1_MC_Head_ID', 'VCM_M7_AgGlue1_End_Time',
    'VCM_M7_AgGlue2_MC_ID', 'VCM_M7_AgGlue2_MC_Head_ID', 'VCM_M7_AgGlue2_End_Time',
    'VCM_M7_AgGlue_Baking_MC_ID', 'VCM_M7_AgGlue_Baking_Start_Time', 'VCM_M7_AgGlue_Baking_End_Time',
    'VCM_M7_AgGlue_Baking_Staging_time', 'VCM_M8_Aging_MC_ID', 'VCM_M8_Aging_Nozzle',
    'VCM_M8_Aging_Socket', 'VCM_M8_Aging_End_Time', 'VCM_M8_Aging_Facedown_MC_ID',
    'VCM_M8_Aging_Facedown_Nozzle', 'VCM_M8_Aging_Facedown_Socket', 'VCM_M8_Aging_Facedown_End_Time',
    'VCM_M8_Blow suck_MC_ID', 'VCM_M8_Blow suck_End_Time', 'VCM_Function_Test1_MC_ID',
    'VCM_Function_Test1_Socket', 'VCM_Function_Test1_End_Time', 'VCM_M9_Cover_Attach_MC_ID',
    'VCM_M9_Cover_Attach_MC_Head_ID', 'VCM_M9_Cover_Attach_End_Time', 'VCM_M9_Cover_AutoClave_MC_ID',
    'VCM_M9_Cover_AutoClave_Start_Time', 'VCM_M9_Cover_AutoClave_End_Time',
    'VCM_M9_Cover_AutoClave_Staging_time', 'VCM_M10_Function_test_MC_ID',
    'VCM_M10_Function_test_Socket', 'VCM_M10_Function_test_End_Time', 'VCM_FPC_Part_SN',
    'VCM_FPC_Vendor', 'VCM_Coil1_Part_SN', 'VCM_Coil2_Part_SN', 'VCM_Stator_Part_SN',
    'VCM_Stator_Cavity_ID', 'VCM_Soma_Part_SN', 'VCM_Rotor_Part_SN', 'VCM_Rotor_Cavity_ID',
    'VCM_Balance Magnet_Vendor', 'VCM_Balance Magnet_lot_ID_1', 'VCM_Driver Magnet_Vendor',
    'VCM_Driver Magnet_lot_ID_1', 'VCM_Shield Can_Part_SN', 'VCM_Shield Can_Cavity_ID',
    'VCM_Shim_lot_ID_1', 'VCM_Ball_lot_ID_1', 'VCM_Blade1_Part_SN', 'VCM_Blade2_Part_SN',
    'VCM_Blade3_Part_SN', 'VCM_Blade4_Part_SN', 'VCM_Blade5_Part_SN', 'VCM_Blade6_Part_SN',
    'VCM_Cosmetic Cover_Part_SN', 'VCM_CoilAttach_to_FPC_Glue_lot_ID_1',
    'VCM_FPC_to_Stator_Glue_lot_ID_1', 'VCM_Bending_Glue_lot_ID_1', 'VCM_Bending_Glue_lot_ID_2',
    'VCM_MagnetAttach_Glue_lot_ID_1', 'VCM_RotorAssy_Grease_lot_ID_1',
    'VCM_StatorAssy_Grease_lot_ID_1', 'VCM_ShiledCan_Attach_Glue_lot_ID_1',
    'VCM_Ag_Glue_lot_ID_1', 'VCM_Ag_Glue_lot_ID_2', 'VCM_Solder ball_lot_ID_1'
]
REQUIRED_COLS = ['Results', 'Date', 'Failed_Station', 'Failure_Mode']
LOAD_COLS = list(set(FOCUS_COLS + REQUIRED_COLS))


# ===================== HELPERS =====================
def is_time_column(col_name):
    """判断列名是否为时间类型"""
    suffixes = ('_End_Time', '_Start_Time', '_Time', '_datetime')
    exact = ('Date', 'Out_Time')
    if col_name in exact:
        return True
    return any(col_name.endswith(s) for s in suffixes)


def hour_to_shift(h):
    """小时 → 班次"""
    if 0 <= h < 8:
        return '夜班(00-08)'
    elif 8 <= h < 16:
        return '早班(08-16)'
    else:
        return '中班(16-24)'


# ===================== TIME FEATURE EXTRACTION =====================
def extract_time_features(df):
    """
    检测时间列，提取 小时 和 班次 作为新的离散特征。
    返回 (处理后的df, 新增特征列名列表, 原始时间列名列表)
    """
    new_cols = []
    time_cols = []
    for col in list(df.columns):
        if col in SKIP_COLS:
            continue
        if not is_time_column(col):
            continue
        try:
            parsed = pd.to_datetime(df[col], errors='coerce')
            valid_ratio = parsed.notna().sum() / max(len(df), 1)
            if valid_ratio < 0.3:
                continue
            time_cols.append(col)
            hour_col = f"{col}_小时"
            shift_col = f"{col}_班次"
            df[hour_col] = parsed.dt.hour.apply(
                lambda h: f"{int(h):02d}时" if pd.notna(h) else None)
            df[shift_col] = parsed.dt.hour.apply(
                lambda h: hour_to_shift(int(h)) if pd.notna(h) else None)
            new_cols.extend([hour_col, shift_col])
        except Exception:
            continue
    return df, new_cols, time_cols


# ===================== LIFT CALCULATION =====================
def compute_lift(df_base, df_fail, feature_cols, min_fail_count=3, top_n=20):
    """
    纯离散共性聚集度分析（Lift 提升度）
    - df_base: 基准池（含 Pass + Fail）
    - df_fail: 目标Fail样本池
    - feature_cols: 参与分析的特征列
    - min_fail_count: Fail中出现次数最低门槛
    - top_n: 返回 TOP N
    返回: list of dict, 按 Lift 降序
    """
    n_base = len(df_base)
    n_fail = len(df_fail)
    if n_base == 0 or n_fail == 0:
        return []

    results = []
    for col in feature_cols:
        if col in SKIP_COLS:
            continue
        try:
            # Fail池中各取值计数
            fail_vc = df_fail[col].astype(str).value_counts()
            # 基准池中各取值计数
            base_vc = df_base[col].astype(str).value_counts()

            best = None
            for val, fail_cnt in fail_vc.items():
                if val in ('nan', 'None', '', 'NaT'):
                    continue
                if fail_cnt < min_fail_count:
                    continue
                base_cnt = base_vc.get(val, 0)
                if base_cnt == 0:
                    continue
                p_fail = fail_cnt / n_fail
                p_base = base_cnt / n_base
                lift = round(p_fail / p_base, 3)
                if best is None or lift > best['lift']:
                    best = {
                        'feature': col,
                        'value': str(val),
                        'lift': lift,
                        'fail_count': int(fail_cnt),
                        'fail_ratio': round(p_fail * 100, 1),
                        'base_count': int(base_cnt),
                        'base_ratio': round(p_base * 100, 1),
                    }
            if best is not None and best['lift'] > 1.0:
                results.append(best)
        except Exception:
            continue

    results.sort(key=lambda x: x['lift'], reverse=True)
    return results[:top_n]


# ===================== LLM =====================
def call_llm(api_key, top_results, station_filter, mode_filter):
    """调用 LLM 生成结构化报告"""
    data_text = json.dumps(top_results[:15], ensure_ascii=False, indent=2)
    prompt = f"""### 分析任务：终检Fail产品共性聚集度归因

**当前筛选条件**：
- Failed_Station: {station_filter}
- Failure_Mode: {mode_filter}

**TOP 聚集度数据（Lift 提升度排名）**：
{data_text}

说明：Lift > 1 表示该取值在Fail产品中的占比高于基准（含Pass+Fail），Lift越高表示Fail产品越集中于该取值。

### 请作为质量专家输出：
1. **核心不良聚集点**：哪些机台/载具/Socket/模穴等因素出现异常聚集？直指核心。
2. **时间维度异常**：是否集中在某个时段或班次？
3. **具体排查建议**：给出可执行的排查或维修建议。

请使用专业简洁的中文，Markdown 格式输出。"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是3C制造行业的资深质量分析专家，擅长从统计聚集度数据中定位根因并给出处置建议。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 1500
    }
    try:
        resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"⚠️ LLM 调用失败: {str(e)}\n\n请检查 API 地址和 Key 配置。"


# ===================== MOCK DATA =====================
def generate_mock_data(n=500):
    """生成模拟数据，植入异常规律用于 Demo 验证"""
    random.seed(42)
    np.random.seed(42)

    stations = ['METROLOGY', 'FUNCTION_TEST', 'FGAVI']
    modes_normal = [
        'VA_STICTION_CLOSE2OPEN_MA',
        'VA_GAINTRIM_VCMSTROKE_DIAMETER_UM',
        'VA_CLRAMP_CLOSELINEAERREGION_FW_DIAMETER',
        'VA_STICTION_OPEN2CLOSE_MA'
    ]
    TARGET_MODE = 'VA_DRIVER2_AFE_P_VA/CONNECT NG'
    machines = [f'MC_{i:03d}' for i in range(1, 16)]
    BAD_MC = 'MC_BAD_007'
    carriers = [f'Carrier_{chr(65 + i)}' for i in range(12)]
    sockets = [f'S{i}' for i in range(1, 9)]
    nozzles = [f'N{i}' for i in range(1, 7)]
    cavities = ['Cav_1', 'Cav_2', 'Cav_3', 'Cav_4']
    vendors = ['Vendor_A', 'Vendor_B', 'Vendor_C']
    lots = [f'LOT_{i:04d}' for i in range(2001, 2021)]
    base_date = datetime(2026, 4, 1)

    # 工序前缀
    proc_prefixes = [
        'VCM_M1_FPC_UpCoil_Attach', 'VCM_M1_FPC_Coil_Baking',
        'VCM_M1_Jet_Soldering', 'VCM_M2_Stator_Plasma',
        'VCM_M2_Stator_FPC_Attach', 'VCM_M2_StatorSubAssy_Baking',
        'VCM_M3_BCA_Plasma', 'VCM_M3_BCA_Bending',
        'VCM_M3_BCA_Baking', 'VCM_M3_StatorAssy_Baking',
        'VCM_M4_Rotor_Plasma', 'VCM_M4_Rotor_Magnet_Dispensing',
        'VCM_M4_Rotor_Magnet_Attach', 'VCM_M4_Rotor_Magnet_Baking',
        'VCM_M4_RotorAssy_Baking', 'VCM_M5_ShieldCan_Shim_Attach',
        'VCM_M5_ShieldCan_Shim_AutoClave', 'VCM_M6_StatorAssy_Plasma',
        'VCM_M6_StatorAssy_Grease', 'VCM_M6_Ball_Rotor_Assy',
        'VCM_M6_Blade_Assembly', 'VCM_M6_ShieldCan_Assy',
        'VCM_M6_ShieldCan_Baking', 'VCM_XRay',
        'VCM_M7_AgGlue1', 'VCM_M7_AgGlue2', 'VCM_M7_AgGlue_Baking',
        'VCM_M8_Aging', 'VCM_Function_Test1',
        'VCM_M9_Cover_Attach', 'VCM_M9_Cover_AutoClave',
        'VCM_M10_Function_test',
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
            station = 'METROLOGY'
            fm = TARGET_MODE
            if random.random() < 0.9:
                dt = base_date + timedelta(days=day_off, hours=random.randint(2, 3), minutes=minute)
        elif is_fail:
            station = random.choice(stations)
            fm = random.choice(modes_normal)
        else:
            station = ''
            fm = ''

        row = {
            'sn': f'SN{i:06d}',
            'Serial_No': f'SER-{i:06d}',
            'Date': dt.strftime('%Y-%m-%d'),
            'Results': 'FAIL' if is_fail else 'PASS',
            'Failed_Station': station,
            'Failure_Mode': fm,
            'Project': 'VA3199',
            'Build': random.choice(['PRB', 'MP']),
            'Config': random.choice(['Config_A', 'Config_B']),
        }

        # MC_ID + End_Time per process
        for pfx in proc_prefixes:
            mc = BAD_MC if is_target and random.random() < 0.9 else random.choice(machines)
            row[f'{pfx}_MC_ID'] = mc
            t = dt + timedelta(minutes=random.randint(0, 300))
            row[f'{pfx}_End_Time'] = t.strftime('%Y-%m-%d %H:%M:%S')

        # Socket / Nozzle
        for sc in ['VCM_M8_Aging_Socket', 'VCM_M3_BCA_Bending_Socket',
                    'VCM_Function_Test1_Socket', 'VCM_M10_Function_test_Socket']:
            row[sc] = random.choice(sockets)
        for nz in ['VCM_M8_Aging_Nozzle']:
            row[nz] = random.choice(nozzles)

        # Cavity / Vendor / Lot
        row['VCM_Stator_Cavity_ID'] = random.choice(cavities)
        row['VCM_Rotor_Cavity_ID'] = random.choice(cavities)
        row['VCM_FPC_Vendor'] = random.choice(vendors)
        row['VCM_Bending_Glue_lot_ID_1'] = random.choice(lots)
        row['VCM_MagnetAttach_Glue_lot_ID_1'] = random.choice(lots)
        row['VCM_Ag_Glue_lot_ID_1'] = random.choice(lots)

        rows.append(row)

    return pd.DataFrame(rows)


# ===================== CUSTOM CSS =====================
def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', 'Microsoft YaHei', sans-serif; }
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 12px; padding: 20px; color: white; text-align: center;
    }
    .metric-card.teal { background: linear-gradient(135deg, #009688 0%, #00796b 100%); }
    .metric-card.orange { background: linear-gradient(135deg, #e65100 0%, #bf360c 100%); }
    .metric-card.red { background: linear-gradient(135deg, #c62828 0%, #b71c1c 100%); }
    .metric-value { font-size: 32px; font-weight: 700; margin-bottom: 4px; }
    .metric-label { font-size: 13px; opacity: 0.9; }
    .llm-box {
        background: #1a2a3a; color: #e0e0e0; border-radius: 12px;
        padding: 24px; margin: 16px 0;
        box-shadow: 0 8px 24px rgba(0,0,0,0.15);
    }
    .llm-box h1,.llm-box h2,.llm-box h3 { color: #4dd0e1; }
    .llm-box b, .llm-box strong { color: #fff; }
    .section-header {
        font-size: 20px; font-weight: 700; color: #333;
        border-left: 4px solid #009688; padding-left: 12px; margin: 24px 0 16px;
    }
    </style>
    """, unsafe_allow_html=True)


# ===================== MAIN APP =====================
def main():
    inject_css()

    # ---- Header ----
    st.markdown("# 🔍 终检Fail产品全流程共性聚集度分析")
    st.markdown("*基于 Lift 提升度的纯离散共性聚集度分析平台 — 找出NG产品的共性因素*")
    st.divider()

    # ---- Sidebar ----
    with st.sidebar:
        st.header("⚙️ 控制面板")

        st.subheader("📁 数据输入")
        uploaded = st.file_uploader("上传 CSV / Excel 文件", type=['csv', 'xlsx', 'xls'])
        use_mock = st.checkbox("使用 Demo 模拟数据", value=False)

        st.subheader("🔑 LLM 配置")
        api_key = st.text_input("API Key", type="password",
                                value=os.getenv("COMPANY_LLM_KEY", ""))

        st.subheader("🔧 筛选条件")
        date_range = st.date_input("时间范围", value=[], help="选择起止日期")

        # Placeholder for dynamic dropdowns
        station_options = ['全部']
        mode_options = ['全部']
        df_raw = None

        # Load data to populate dropdowns
        if use_mock:
            df_raw = generate_mock_data()
        elif uploaded:
            try:
                # 检查文件中存在哪些需要的列，防止 usecols 报错
                header = []
                if uploaded.name.endswith('.csv'):
                    header = pd.read_csv(uploaded, nrows=0).columns.tolist()
                else:
                    header = pd.read_excel(uploaded, nrows=0, engine='openpyxl').columns.tolist()
                
                valid_cols = [c for c in LOAD_COLS if c in header]
                
                if uploaded.name.endswith('.csv'):
                    df_raw = pd.read_csv(uploaded, usecols=valid_cols)
                else:
                    df_raw = pd.read_excel(uploaded, engine='openpyxl', usecols=valid_cols)
                
                # 内存优化：类型压缩
                for col in df_raw.select_dtypes(include=['object']).columns:
                    if col not in REQUIRED_COLS and df_raw[col].nunique() < len(df_raw) * 0.2:
                        df_raw[col] = df_raw[col].astype('category')
                        
                st.success(f"✅ 成功加载 {len(df_raw)} 行数据，已开启内存优化 (usecols + category)")
            except Exception as e:
                st.error(f"文件读取失败: {e}")

        if df_raw is not None and 'Failed_Station' in df_raw.columns:
            vals = df_raw['Failed_Station'].dropna()
            vals = vals[vals.astype(str).str.strip() != '']
            station_options = ['全部'] + sorted(vals.unique().tolist())

        station_sel = st.selectbox("Failed Station", station_options, index=0)

        # Failure Mode 联动: 根据时间+工站筛选后取 TOP 10
        if df_raw is not None and 'Failure_Mode' in df_raw.columns:
            temp = df_raw.copy()
            if station_sel != '全部':
                temp = temp[temp['Failed_Station'] == station_sel]
            if 'Date' in temp.columns and len(date_range) == 2:
                dt_col = pd.to_datetime(temp['Date'], errors='coerce')
                temp = temp[(dt_col >= pd.Timestamp(date_range[0])) &
                            (dt_col <= pd.Timestamp(date_range[1]))]
            fm_vals = temp['Failure_Mode'].dropna()
            fm_vals = fm_vals[fm_vals.astype(str).str.strip() != '']
            if len(fm_vals) > 0:
                top_modes = fm_vals.value_counts().head(10).index.tolist()
                mode_options = ['全部'] + top_modes

        mode_sel = st.selectbox("Failure Mode (TOP 10)", mode_options, index=0)

        min_count = st.slider("最低Fail出现次数", 2, 20, 3)
        top_n = st.slider("TOP N", 5, 30, 20)

        btn = st.button("🚀 开始分析", type="primary", use_container_width=True)

    # ---- Main Area ----
    if not btn:
        st.info("👈 请在左侧上传数据或勾选 Demo 数据，配置筛选条件后点击 **开始分析**。")
        return

    if df_raw is None:
        st.error("请先上传数据文件或勾选 Demo 模拟数据。")
        return

    # ---- Normalize Results ----
    if 'Results' not in df_raw.columns:
        for c in df_raw.columns:
            if df_raw[c].nunique() == 2:
                vals = set(df_raw[c].dropna().astype(str).str.lower().unique())
                if vals & {'fail', 'ng', 'failed'}:
                    df_raw = df_raw.rename(columns={c: 'Results'})
                    break
    if 'Results' not in df_raw.columns:
        st.error("❌ 未找到 Results 列，无法判断 Pass/Fail。")
        return

    df_raw['Results'] = df_raw['Results'].astype(str).apply(
        lambda x: 'FAIL' if x.strip().lower() in ('fail', 'ng', 'failed') else 'PASS')

    # ---- Apply Filters → df_base ----
    df_base = df_raw.copy()
    if 'Date' in df_base.columns and len(date_range) == 2:
        dt_col = pd.to_datetime(df_base['Date'], errors='coerce')
        df_base = df_base[(dt_col >= pd.Timestamp(date_range[0])) &
                          (dt_col <= pd.Timestamp(date_range[1]))]

    if station_sel != '全部' and 'Failed_Station' in df_base.columns:
        # 基准池: 对应工站的 Pass (Failed_Station为空) + 该工站的 Fail
        mask_fail = (df_base['Results'] == 'FAIL') & (df_base['Failed_Station'] == station_sel)
        mask_pass = df_base['Results'] == 'PASS'
        df_base = df_base[mask_fail | mask_pass]

    # df_fail: 从 df_base 中筛出 Fail
    df_fail = df_base[df_base['Results'] == 'FAIL'].copy()

    if station_sel != '全部' and 'Failed_Station' in df_fail.columns:
        df_fail = df_fail[df_fail['Failed_Station'] == station_sel]

    if mode_sel != '全部' and 'Failure_Mode' in df_fail.columns:
        df_fail = df_fail[df_fail['Failure_Mode'] == mode_sel]

    if len(df_fail) == 0:
        st.warning("⚠️ 筛选后无 Fail 数据，请调整筛选条件。")
        return

    # ---- Time Feature Extraction ----
    with st.spinner("正在提取时间特征（小时/班次）..."):
        df_base, new_time_cols, orig_time_cols = extract_time_features(df_base)
        df_fail = df_base.loc[df_fail.index]

    # ---- Determine Feature Columns ----
    all_feature_cols = []
    for col in df_base.columns:
        if col in SKIP_COLS:
            continue
        if col in orig_time_cols:
            continue  # 原始时间列不参与，用提取后的小时/班次代替
        if col == 'Date':
            continue
        # 跳过唯一值极多的列(如 Part_SN)
        nuniq = df_base[col].nunique()
        if nuniq < 2:
            continue
        if nuniq > len(df_base) * 0.5:
            continue  # 唯一值太多，视为ID列跳过
        all_feature_cols.append(col)

    # ---- Compute Lift ----
    with st.spinner("正在计算 Lift 提升度..."):
        lift_results = compute_lift(df_base, df_fail, all_feature_cols,
                                    min_fail_count=min_count, top_n=top_n)

    if not lift_results:
        st.warning("未发现 Lift > 1 的聚集因素。请调整筛选条件或降低最低出现次数。")
        return

    # ---- Metrics ----
    st.markdown('<div class="section-header">📊 数据概览</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    n_base = len(df_base)
    n_fail = len(df_fail)
    fail_rate = round(n_fail / n_base * 100, 2) if n_base > 0 else 0
    with c1:
        st.markdown(f"""<div class="metric-card teal">
            <div class="metric-value">{n_base:,}</div>
            <div class="metric-label">基准池总数 (Pass+Fail)</div></div>""",
                    unsafe_allow_html=True)
    with c2:
        st.markdown(f"""<div class="metric-card orange">
            <div class="metric-value">{n_fail:,}</div>
            <div class="metric-label">Fail 数量</div></div>""",
                    unsafe_allow_html=True)
    with c3:
        st.markdown(f"""<div class="metric-card red">
            <div class="metric-value">{fail_rate}%</div>
            <div class="metric-label">Fail 率</div></div>""",
                    unsafe_allow_html=True)

    # ---- LLM Report ----
    st.markdown('<div class="section-header">🤖 LLM 结构化报告</div>', unsafe_allow_html=True)
    if api_key and api_key.strip():
        with st.spinner("正在调用 LLM 生成报告..."):
            llm_text = call_llm(api_key, lift_results, station_sel, mode_sel)
        st.markdown(f'<div class="llm-box">{llm_text}</div>', unsafe_allow_html=True)
    else:
        st.info("💡 未配置 LLM API Key，跳过智能总结。请在侧边栏输入 Key 以启用。")

    # ---- TOP N Lift Chart ----
    st.markdown(f'<div class="section-header">📈 TOP {len(lift_results)} 共性聚集度 (Lift 提升度)</div>',
                unsafe_allow_html=True)

    # Prepare chart data (reverse for horizontal bar - top at top)
    chart_data = list(reversed(lift_results))
    y_labels = []
    annotations = []
    for item in chart_data:
        # 简化特征名
        feat = item['feature'].replace('VCM_', '').replace('_', ' ')
        y_labels.append(feat)
        annotations.append(f"→ {item['value']}  (Fail {item['fail_count']}个, {item['fail_ratio']}%)")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=y_labels,
        x=[item['lift'] for item in chart_data],
        orientation='h',
        marker=dict(
            color=[item['lift'] for item in chart_data],
            colorscale=[[0, '#4dd0e1'], [0.5, '#ff9800'], [1, '#e53935']],
            line=dict(width=0),
        ),
        text=annotations,
        textposition='outside',
        textfont=dict(size=11, color='#333'),
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Lift: %{x:.2f}<br>"
            "%{text}<extra></extra>"
        ),
    ))
    fig.add_vline(x=1.0, line_dash="dash", line_color="#999",
                  annotation_text="Lift=1 (无聚集)", annotation_position="top")
    fig.update_layout(
        height=max(400, len(chart_data) * 35 + 100),
        margin=dict(l=20, r=250, t=30, b=30),
        xaxis_title="Lift 提升度",
        yaxis=dict(tickfont=dict(size=11)),
        plot_bgcolor='#fafafa',
        paper_bgcolor='white',
    )
    st.plotly_chart(fig, use_container_width=True)

    # ---- Detail Table ----
    st.markdown('<div class="section-header">📋 完整聚集度数据表</div>', unsafe_allow_html=True)
    df_table = pd.DataFrame(lift_results)
    df_table.index = range(1, len(df_table) + 1)
    df_table.index.name = '排名'
    df_table.columns = ['特征列', '聚集取值', 'Lift', 'Fail数', 'Fail占比%', '基准数', '基准占比%']
    st.dataframe(df_table, use_container_width=True, height=min(600, len(df_table) * 40 + 60))

    gc.collect()


if __name__ == '__main__':
    main()
