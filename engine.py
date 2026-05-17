import pandas as pd
import numpy as np
import math
from typing import List, Dict, Any, Optional
import requests
import json
import itertools
import lightgbm as lgb
import re

def normalize_val(v: Any) -> Optional[str]:
    if pd.isna(v):
        return None
    if isinstance(v, float):
        if math.isinf(v):
            return None
        if v.is_integer():
            return str(int(v))
        return str(v)
    return str(v).strip()

def compute_lift_engine(df_base: pd.DataFrame, df_fail: pd.DataFrame, feature_cols: List[str], lift_weight: float = 0.3, min_count: int = 3):
    """
    后台计算 Lift 和 Fail 占比的引擎代码（剥离 UI）
    """
    lift_results = []
    ratio_results = []
    
    n_base = len(df_base)
    n_fail = len(df_fail)
    if n_base == 0 or n_fail == 0:
        return [], []

    fail_ratio_weight = 1.0 - lift_weight
    max_unique = max(1000, int(n_base * 0.3))

    for col in feature_cols:
        if col not in df_base.columns or col not in df_fail.columns:
            continue
            
        base_series = df_base[col]
        fail_series = df_fail[col]
        
        if base_series.nunique(dropna=True) > max_unique:
            continue

        base_vc = base_series.value_counts(dropna=False)
        fail_vc = fail_series.value_counts(dropna=False)

        all_vals = list(set(base_vc.keys()).union(set(fail_vc.keys())))
        
        for val in all_vals:
            f_cnt = fail_vc.get(val, 0)
            if f_cnt < min_count:
                continue
                
            b_cnt = base_vc.get(val, 0)
            if b_cnt == 0:
                continue
                
            val_str = normalize_val(val)
            if val_str is None or val_str == "缺失值":
                continue
                
            p_fail = f_cnt / n_fail
            p_base = b_cnt / n_base
            lift = p_fail / p_base if p_base > 0 else 0
            
            fail_ratio = p_fail
            composite_score = lift_weight * lift + fail_ratio_weight * (fail_ratio * 100)
            
            if lift > 1.0:
                lift_results.append({
                    "feature": col,
                    "value": val_str,
                    "fail_count": int(f_cnt),
                    "base_count": int(b_cnt),
                    "lift": float(lift),
                    "fail_ratio": float(fail_ratio),
                    "composite_score": float(composite_score),
                })

    lift_results.sort(key=lambda x: x["composite_score"], reverse=True)
    return lift_results

def get_top10_by_feature(results: List[Dict[str, Any]], sort_key: str = "composite_score") -> List[Dict[str, Any]]:
    best_per_feature = {}
    for r in results:
        feat = r["feature"]
        if feat not in best_per_feature or r[sort_key] > best_per_feature[feat][sort_key]:
            best_per_feature[feat] = r
    sorted_best = sorted(best_per_feature.values(), key=lambda x: x[sort_key], reverse=True)
    return sorted_best[:10]

def run_ml_diagnosis_engine(df_base: pd.DataFrame, df_fail: pd.DataFrame, all_feature_cols: List[str]):
    """
    后台运行 LightGBM 诊断和多因子交叉挖掘引擎
    """
    if len(df_base) < 10 or len(df_fail) < 10:
        return pd.DataFrame(), pd.DataFrame()
        
    df_ml = pd.concat([df_base, df_fail], ignore_index=True)
    y = df_ml["Results"].map({"PASS": 0, "FAIL": 1})
    
    features_to_use = []
    for c in all_feature_cols:
        if c not in df_ml.columns: continue
        if c.lower() == "date": continue
        if re.search(r"_time$", c, re.IGNORECASE) and not re.search(r"_staging_time$", c, re.IGNORECASE):
            continue
        features_to_use.append(c)
        
    if not features_to_use:
        return pd.DataFrame(), pd.DataFrame()
        
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
    df_imp = pd.DataFrame({'特征列': X.columns, '信息增益 (Gain)': imp_vals})
    df_imp = df_imp[df_imp['信息增益 (Gain)'] > 0]
    df_imp = df_imp.sort_values(by='信息增益 (Gain)', ascending=False)
    
    combo_results = []
    top_features = df_imp.head(8)['特征列'].tolist()
    if top_features:
        X_str = X[top_features].astype(str)
        X_fail_str = X_str[y == 1]
        X_base_str = X_str[y == 0]
        
        min_fail_count = max(5, int(len(X_fail_str) * 0.01))
        min_fail_count = min(min_fail_count, 20)
        
        for k in [2, 3, 4]:
            if len(top_features) < k: break
            for combo_cols in itertools.combinations(top_features, k):
                combo_cols = list(combo_cols)
                fail_vc = X_fail_str.groupby(combo_cols).size()
                fail_vc = fail_vc[fail_vc >= min_fail_count]
                if fail_vc.empty: continue
                
                base_vc = X_base_str.groupby(combo_cols).size()
                for val_tuple, f_cnt in fail_vc.items():
                    if k == 1: val_tuple = (val_tuple,)
                    b_cnt = base_vc.get(val_tuple, 0)
                    total_cnt = f_cnt + b_cnt
                    ratio = f_cnt / total_cnt
                    
                    if ratio >= 0.2:
                        rule_parts = [f"[{c}]='{v}'" for c, v in zip(combo_cols, val_tuple)]
                        combo_results.append({
                            "高危组合条件": " 且 ".join(rule_parts),
                            "Fail概率": ratio,
                            "Fail次数": f_cnt
                        })
                        
    df_combo = pd.DataFrame(combo_results)
    if not df_combo.empty:
        df_combo = df_combo.sort_values(by=["Fail概率", "Fail次数"], ascending=[False, False])
        
    return df_imp, df_combo

def call_fused_llm(api_key: str, api_base: str, model: str, station: str, 
                   top10_lift: List[Dict], df_imp: pd.DataFrame, df_combo: pd.DataFrame) -> str:
    """
    融合统计学 Lift 和机器学习 LightGBM 结果的终极诊断模型
    """
    if not api_key:
        return "LLM API Key 未配置"
        
    lift_text = json.dumps([
        {"特征": r["feature"], "聚集值": r["value"], "Lift": r["lift"], "Fail次数": r["fail_count"]} 
        for r in top10_lift
    ], ensure_ascii=False)
    
    imp_text = df_imp.head(5).to_json(orient='records', force_ascii=False) if not df_imp.empty else "无显著特征"
    combo_text = df_combo.head(5).to_json(orient='records', force_ascii=False) if not df_combo.empty else "无组合"

    prompt = f"""你是一名资深的3C制造质量总监。目前产线【{station}】工站触发了良率告警，系统刚刚跑完了过去24小时全量数据的融合分析。

【1. 统计学聚集度分析 (Lift)】
Lift表示在Fail中出现频率超出大盘正常的倍数，越高越异常：
{lift_text}

【2. 机器学习核心病灶诊断 (Gain)】
Gain是树模型找出的起决定性拆分作用的单因子重灾区：
{imp_text}

【3. 高危多因子交叉组合表】
这是真实引发报废的深层条件：
{combo_text}

请你融合这两套系统的数据，向现场排查工程师发出“行动指令”，直接说重点：
#### 🚨 核心案发工站在哪
(综合 Lift 和 Gain 的共同指向，指出最严重的机台或物料批次，不用解释算法)
#### 💥 致命触发条件是什么
(根据交叉组合，用大白话描述什么条件同时满足时报废最严重)
#### 🛠️ 第一优先级排查建议
(具体到让谁去查哪个机台的哪个参数，越落地越好)
"""

    url = f"{api_base.rstrip('/')}/chat/completions"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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
            return f"LLM失败: {response.text[:100]}"
    except Exception as e:
        return f"LLM异常: {str(e)[:100]}"

def run_fused_diagnosis(df_raw: pd.DataFrame, project: str, station: str, api_key: str, api_base: str, model: str):
    """主控流程"""
    df_base = df_raw[df_raw["Results"] == "PASS"].copy()
    df_fail = df_raw[df_raw["Results"] == "FAIL"].copy()
    if station:
        df_fail = df_fail[df_fail["Failed_Station"] == station]
        
    # 获取所有的特征列（这里偷懒直接拿所有除基础外的列，真实场景应像app.py读取配置）
    all_feature_cols = [c for c in df_raw.columns if c not in ["sn", "Serial_No", "Date", "Results", "Failed_Station", "Failure_Mode", "Project", "Build", "Config"]]
    
    print(f"✅ 数据切分完毕: PASS={len(df_base)}, FAIL={len(df_fail)}")
    
    lift_results = compute_lift_engine(df_base, df_fail, all_feature_cols)
    top10_lift = get_top10_by_feature(lift_results)
    print("✅ Lift 计算完成")
    
    df_imp, df_combo = run_ml_diagnosis_engine(df_base, df_fail, all_feature_cols)
    print("✅ LightGBM 计算完成")
    
    report = call_fused_llm(api_key, api_base, model, station, top10_lift, df_imp, df_combo)
    print("✅ LLM 报告生成完成")
    
    return {
        "status": "success",
        "top10_lift": top10_lift,
        "lgb_importance": df_imp.head(5).to_dict(orient="records") if not df_imp.empty else [],
        "lgb_combinations": df_combo.head(5).to_dict(orient="records") if not df_combo.empty else [],
        "report": report
    }
