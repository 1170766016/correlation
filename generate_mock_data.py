import pandas as pd
import numpy as np
import os
import random
from datetime import datetime, timedelta

def generate_mock_data(n_samples=5000):
    print("开始生成模拟数据...")
    random.seed(42)
    np.random.seed(42)
    
    # 基础配置
    base_date = datetime(2026, 5, 10, 8, 0, 0)
    dates = [base_date + timedelta(seconds=i * ((2026 - 2026)*3600 + 8 * 24 * 3600 / n_samples)) for i in range(n_samples)]
    
    # 初始化字段
    results = []
    failed_stations = []
    failure_modes = []
    
    # 极其高危的单因子：
    # 1. 胶水批次 VCM_Glue_lot_ID_1 为 LOT_2005 的产品高概率失败 (50% 失败率)
    # 2. 机台 VCM_M1_FPC_UpCoil_Attach_MC_ID 为 MC_BAD_007 的产品高概率失败 (60% 失败率)
    # 3. 滞留时间 VCM_M1_FPC_UpCoil_Attach_Staging_time 为 '180' (表示180秒延误) 的产品高概率失败 (55% 失败率)
    
    # 极其高危的多因子组合 (多维交叉)：
    # 当 VCM_Feature_1 == 'High' 且 VCM_Feature_2 == 'Fast' 时，产品有 80% 的概率失败！
    
    # 时间聚集：
    # 在 2026-05-18 06:00:00 到 12:00:00 这个 6 小时时间段内，Fail 占比极大（模拟突发性班次异常）
    
    # 1. 生成主要的工序特征列
    fpc_mc_ids = [f"MC_{i:03d}" for i in range(1, 6)] + ["MC_BAD_007"]
    glue_lots = [f"LOT_{i:04d}" for i in range(2001, 2005)] + ["LOT_2005"]
    staging_times = ["10", "20", "30", "180"]
    
    mc_probs = [0.2, 0.2, 0.2, 0.2, 0.18, 0.02]  # MC_BAD_007 正常情况下占比很低
    lot_probs = [0.23, 0.23, 0.23, 0.23, 0.08]   # LOT_2005 正常情况下占比很低
    staging_probs = [0.45, 0.45, 0.08, 0.02]     # '180' 延误情况正常下极少
    
    f1_vals = ["Low", "Normal", "High"]
    f1_probs = [0.15, 0.75, 0.10]
    
    f2_vals = ["Slow", "Medium", "Fast"]
    f2_probs = [0.15, 0.75, 0.10]
    
    rows = []
    
    for i in range(n_samples):
        dt = dates[i]
        sn = f"SN{i:06d}"
        
        # 正常随机生成特征
        mc_id = np.random.choice(fpc_mc_ids, p=mc_probs)
        glue_lot = np.random.choice(glue_lots, p=lot_probs)
        staging_time = np.random.choice(staging_times, p=staging_probs)
        
        f1 = np.random.choice(f1_vals, p=f1_probs)
        f2 = np.random.choice(f2_vals, p=f2_probs)
        
        # 判定是否属于时间高危区 (2026-05-18 06:00 ~ 12:00)
        is_time_high_risk = (dt.year == 2026 and dt.month == 5 and dt.day == 18 and 6 <= dt.hour < 12)
        
        # 计算失败概率
        fail_prob = 0.02  # 基础失败率 2%
        
        if mc_id == "MC_BAD_007":
            fail_prob = max(fail_prob, 0.60)
        if glue_lot == "LOT_2005":
            fail_prob = max(fail_prob, 0.50)
        if staging_time == "180":
            fail_prob = max(fail_prob, 0.55)
        if f1 == "High" and f2 == "Fast":
            fail_prob = max(fail_prob, 0.80)
        if is_time_high_risk:
            fail_prob = max(fail_prob, 0.35)  # 突发时段异常失败率 35%
            
        is_fail = random.random() < fail_prob
        
        # 如果是 FAIL，再次微调特征以增强信号
        if is_fail:
            results.append("FAIL")
            # 80% 的 FAIL 归于 METROLOGY，20% 归于 FUNCTION_TEST
            station = "METROLOGY" if random.random() < 0.8 else "FUNCTION_TEST"
            failed_stations.append(station)
            
            # 根据特征微调故障模式
            if mc_id == "MC_BAD_007" or glue_lot == "LOT_2005":
                failure_modes.append("VA_DRIVER_NG")
            elif f1 == "High" and f2 == "Fast":
                failure_modes.append("TILT_NG")
            elif is_time_high_risk:
                failure_modes.append("SCRATCH_NG")
            else:
                failure_modes.append(random.choice(["VA_DRIVER_NG", "TILT_NG", "SCRATCH_NG"]))
        else:
            results.append("PASS")
            failed_stations.append(np.nan)
            failure_modes.append(np.nan)
            
        # 生成时间字段 VCM_M1_FPC_UpCoil_Attach_End_Time
        # 相比主 Date 延迟 5-15 分钟
        delay = random.randint(5, 15)
        end_time_dt = dt + timedelta(minutes=delay)
        end_time_str = end_time_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        row = {
            "SN": sn,
            "Date": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "VCM_M1_FPC_UpCoil_Attach_MC_ID": mc_id,
            "VCM_Glue_lot_ID_1": glue_lot,
            "VCM_M1_FPC_UpCoil_Attach_Staging_time": staging_time,
            "VCM_M1_FPC_UpCoil_Attach_End_Time": end_str if 'end_str' in locals() else end_time_str,
            "VCM_Feature_1": f1,
            "VCM_Feature_2": f2,
        }
        
        # 添加一些无关的离散干扰特征以充实 50 个特征列的视觉效果
        for k in range(3, 45):
            row[f"VCM_Feature_{k}"] = f"VAL_{random.randint(1, 3)}"
            
        rows.append(row)
        
    df = pd.DataFrame(rows)
    df["Results"] = results
    df["Failed_Station"] = failed_stations
    df["Failure_Mode"] = failure_modes
    
    # 强制将 `VCM_M1_FPC_UpCoil_Attach_End_Time` 写进每一行
    df["VCM_M1_FPC_UpCoil_Attach_End_Time"] = [
        (dates[i] + timedelta(minutes=random.randint(5, 15))).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(n_samples)
    ]
    
    # 另加一个完全一致的特征（Fail 内唯一值 = 1）以演示 Tab 3/4 的独特逻辑
    # 比如在所有的 FAIL 中，VCM_M1_Constant_Feature 的值全部为 "Locked"
    # 而在 PASS 中，有 Locked，也有 Normal
    constant_feature_vals = []
    for r in results:
        if r == "FAIL":
            constant_feature_vals.append("Locked")
        else:
            constant_feature_vals.append(random.choice(["Locked", "Normal", "Standby"]))
    df["VCM_M1_Constant_Feature"] = constant_feature_vals

    return df

def generate_dashboard_dict(feature_columns):
    print("开始生成数据字典 dashboard F11.xlsx...")
    
    # 包含 4 列的映射字典
    desc_map = {
        "VCM_M1_FPC_UpCoil_Attach_MC_ID": "M1工序FPC上电极贴附设备机台号",
        "VCM_Glue_lot_ID_1": "底座贴附用点胶胶水批次号1",
        "VCM_M1_FPC_UpCoil_Attach_Staging_time": "M1工序FPC上电极贴附滞留时间(秒)",
        "VCM_M1_FPC_UpCoil_Attach_End_Time": "M1工序FPC上电极贴附完成时间戳",
        "VCM_M1_Constant_Feature": "M1工序常态锁定检测机制",
        "VCM_Feature_1": "VCM点胶气压状态等级",
        "VCM_Feature_2": "VCM点胶移动速度等级"
    }
    
    for k in range(3, 45):
        desc_map[f"VCM_Feature_{k}"] = f"VCM制程第{k}项辅助监控参数"
        
    rows = []
    for col in feature_columns:
        desc = desc_map.get(col, f"未配置描述的特征 {col}")
        rows.append([col, "", "", desc])
        
    df_dict = pd.DataFrame(rows, columns=["特征列名", "空列1", "空列2", "含义描述"])
    return df_dict

if __name__ == "__main__":
    # 生成数据
    df_data = generate_mock_data(5000)
    
    # 准备列名
    feature_cols = [c for c in df_data.columns if c not in ["SN", "Date", "Results", "Failed_Station", "Failure_Mode"]]
    
    # 写入 CSV
    csv_path = "PRB数据.csv"
    df_data.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"数据成功写入: {csv_path} (行数: {len(df_data)})")
    
    # 写入 Excel 字典
    dict_path = "dashboard F11.xlsx"
    df_dict = generate_dashboard_dict(feature_cols)
    df_dict.to_excel(dict_path, index=False)
    print(f"字典成功写入: {dict_path} (行数: {len(df_dict)})")
    print("模拟数据环境准备完成！")
