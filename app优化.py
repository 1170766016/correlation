"""
终检Fail产品全流程共性聚集度分析 - 交互效应优化版
主要解决：单因素分析无法识别“因素组合”导致的失效（Interaction Effect）
"""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import os
import re
import json
import logging
from datetime import datetime, timedelta
from scipy.stats import fisher_exact, chi2_contingency
from itertools import combinations
from sklearn.tree import DecisionTreeClassifier, _tree
from sklearn.preprocessing import LabelEncoder

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 核心引擎代码 (继承并优化自 app.py) ---

def normalize_val(v):
    if pd.isna(v): return None
    try:
        f = float(v)
        if f.is_integer(): return str(int(f))
        return str(f)
    except (ValueError, TypeError):
        s = str(v).strip()
        if s.lower() in ('nan', 'none', 'null', 'nat', ''): return None
        return s

def get_normalized_vc(series):
    vc = series.value_counts(dropna=False)
    norm_dict = {}
    for k, v in vc.items():
        nk = normalize_val(k)
        if nk is not None:
            norm_dict[nk] = norm_dict.get(nk, 0) + v
    return pd.Series(norm_dict) if norm_dict else pd.Series(dtype=int)

def _apply_bh_fdr(candidates, alpha=0.05):
    if not candidates: return []
    n = len(candidates)
    sorted_cands = sorted(candidates, key=lambda x: x['p_value'])
    adjusted = [0.0] * n
    adjusted[-1] = min(sorted_cands[-1]['p_value'], 1.0)
    for i in range(n - 2, -1, -1):
        raw_adj = sorted_cands[i]['p_value'] * n / (i + 1)
        adjusted[i] = min(raw_adj, adjusted[i + 1], 1.0)
    significant = []
    for i, r in enumerate(sorted_cands):
        r['p_value_adjusted'] = round(adjusted[i], 6)
        if adjusted[i] <= alpha:
            significant.append(r)
    return significant

def compute_lift(df_base, df_fail, feature_cols, lift_weight=0.3, min_count=3):
    n_base, n_fail = len(df_base), len(df_fail)
    n_pass = n_base - n_fail
    if n_fail == 0 or n_base == 0: return [], [], []

    lift_candidates = []
    ratio_results = []
    fail_one_candidates = []

    for col in feature_cols:
        if col not in df_fail.columns: continue
        
        # 使用归一化计数，确保 1.0 和 1 等格式被正确合并
        base_counts = get_normalized_vc(df_base[col])
        fail_counts = get_normalized_vc(df_fail[col])
        
        if fail_counts.empty: continue

        all_vals = fail_counts.index
        base_aligned = base_counts.reindex(all_vals, fill_value=0)

        p_fail_arr = fail_counts.values / n_fail
        p_base_arr = base_aligned.values / n_base

        for i, val in enumerate(all_vals):
            a = int(fail_counts.values[i])
            if a < min_count: continue
            
            val_str = val # 已经是归一化后的字符串
            
            lift = p_fail_arr[i] / p_base_arr[i] if p_base_arr[i] > 0 else 0
            if lift <= 1.0: continue

            # 显著性检验
            c = int(base_aligned.values[i] - a)
            b, d = n_fail - a, n_pass - max(0, c)
            _, p_val = fisher_exact([[a, b], [max(0, c), max(0, d)]], alternative='greater')

            result_item = {
                'feature': col, 'value': val_str, 'fail_count': a, 'base_count': int(base_aligned.values[i]),
                'p_fail': p_fail_arr[i], 'p_base': p_base_arr[i], 'lift': lift, 'p_value': p_val,
                'fail_ratio': p_fail_arr[i], 'composite_score': lift_weight * lift + (1-lift_weight) * (p_fail_arr[i]*100)
            }
            lift_candidates.append(result_item)
            if a >= 1: ratio_results.append(result_item)
        
        if df_fail[col].nunique() == 1:
            val_f1 = normalize_val(df_fail[col].iloc[0])
            if val_f1:
                fail_one_candidates.append({'feature': col, 'value': val_f1, 'fail_count': n_fail, 'fail_ratio': 1.0})

    lift_results = _apply_bh_fdr(lift_candidates)
    sig_features = {r['feature'] for r in lift_results}
    fail_one_results = [r for r in fail_one_candidates if r['feature'] not in sig_features]
    
    return lift_results, ratio_results, fail_one_results

# --- 重点：N因素深度归因 (决策树路径提取) ---

def compute_decision_tree_rules(df, feature_cols, target_col='Results', max_depth=3, min_samples_leaf=5):
    """
    使用决策树自动发现导致 Fail 的 N 因素组合路径
    返回：风险规则列表 [{path, fail_rate, count, lift}]
    """
    if df.empty or len(df[df[target_col] == 'FAIL']) < 5:
        return []

    # 1. 数据预处理
    df_tree = df[feature_cols + [target_col]].copy()
    
    # 将目标转为数值
    df_tree['is_fail'] = (df_tree[target_col] == 'FAIL').astype(int)
    global_fail_rate = df_tree['is_fail'].mean()
    if global_fail_rate == 0: return []

    # 对特征进行 Label Encoding
    le_dict = {}
    X = pd.DataFrame()
    for col in feature_cols:
        le = LabelEncoder()
        # 归一化处理：统一 1.0 和 1 等格式
        vals = df_tree[col].apply(normalize_val).fillna("缺失值").astype(str)
        X[col] = le.fit_transform(vals)
        le_dict[col] = le

    # 2. 训练决策树
    clf = DecisionTreeClassifier(max_depth=max_depth, min_samples_leaf=min_samples_leaf, random_state=42)
    clf.fit(X, df_tree['is_fail'])

    # 3. 提取路径规则
    tree_ = clf.tree_
    feature_name = [
        feature_cols[i] if i != _tree.TREE_UNDEFINED else "undefined!"
        for i in tree_.feature
    ]

    rules = []

    def recurse(node, depth, current_path):
        if tree_.feature[node] != _tree.TREE_UNDEFINED:
            name = feature_name[node]
            threshold = tree_.threshold[node]
            # 决策树处理数值，LabelEncoder 转后的值是整数
            # 左分支: 编码值 <= threshold
            # 右分支: 编码值 > threshold
            
            # 由于是离散特征，我们遍历编码值来还原语义
            le = le_dict[name]
            all_classes = le.classes_
            
            # 左路: <= threshold 的所有类别
            left_classes = [all_classes[i] for i in range(len(all_classes)) if i <= threshold]
            # 右路: > threshold 的所有类别
            right_classes = [all_classes[i] for i in range(len(all_classes)) if i > threshold]

            if len(left_classes) < len(right_classes):
                left_desc = f"{name} ∈ {left_classes}"
            else:
                left_desc = f"{name} ∉ {right_classes}"
                
            if len(right_classes) < len(left_classes):
                right_desc = f"{name} ∈ {right_classes}"
            else:
                right_desc = f"{name} ∉ {left_classes}"

            recurse(tree_.children_left[node], depth + 1, current_path + [left_desc])
            recurse(tree_.children_right[node], depth + 1, current_path + [right_desc])
        else:
            # 叶子节点
            samples = tree_.n_node_samples[node]
            values = tree_.value[node][0] # [[pass_count, fail_count]]
            n_fail = values[1]
            fail_rate = n_fail / samples
            lift = fail_rate / global_fail_rate if global_fail_rate > 0 else 0
            
            if lift > 1.2 and n_fail >= 3:
                rules.append({
                    'path': " AND ".join(current_path),
                    'fail_rate': round(fail_rate, 4),
                    'lift': round(lift, 2),
                    'count': int(samples),
                    'fail_count': int(n_fail)
                })

    recurse(0, 1, [])
    rules.sort(key=lambda x: x['lift'], reverse=True)
    return rules

# --- 重点：交互效应分析逻辑 ---

def compute_interaction_effects(df_base, df_fail, lift_results, top_n=8, min_count=3):
    """
    分析前 N 个显著特征的两两组合效应
    识别：Combination_Lift > max(Single_Lift_1, Single_Lift_2) * 1.2
    """
    n_base, n_fail = len(df_base), len(df_fail)
    if n_fail == 0 or n_base == 0: return []

    # 1. 获取前 N 个最显著的特征取值
    best_per_feat = {}
    for r in lift_results:
        f = r['feature']
        if f not in best_per_feat or r['composite_score'] > best_per_feat[f]['composite_score']:
            best_per_feat[f] = r
    
    top_items = sorted(best_per_feat.values(), key=lambda x: x['composite_score'], reverse=True)[:top_n]
    if len(top_items) < 2: return []

    interaction_results = []
    
    # 2. 两两组合测试
    for item1, item2 in combinations(top_items, 2):
        f1, v1 = item1['feature'], item1['value']
        f2, v2 = item2['feature'], item2['value']
        
        # 尝试匹配原始值（处理 normalize 之前的差异）
        # 这里为了简化，直接在过滤后的数据上做与运算
        mask_base = (df_base[f1].astype(str).str.strip() == str(v1)) & (df_base[f2].astype(str).str.strip() == str(v2))
        mask_fail = (df_fail[f1].astype(str).str.strip() == str(v1)) & (df_fail[f2].astype(str).str.strip() == str(v2))
        
        a = mask_fail.sum()
        if a < min_count: continue
        
        total_in_base = mask_base.sum()
        if total_in_base == 0: continue
        
        p_fail_comb = a / n_fail
        p_base_comb = total_in_base / n_base
        lift_comb = p_fail_comb / p_base_comb if p_base_comb > 0 else 0
        
        # 定义“协同效应”：组合提升度显著高于单因素
        max_single_lift = max(item1['lift'], item2['lift'])
        synergy = lift_comb / max_single_lift if max_single_lift > 0 else 0
        
        if lift_comb > 1.0:
            interaction_results.append({
                'f1': f1, 'v1': v1,
                'f2': f2, 'v2': v2,
                'lift1': item1['lift'], 'lift2': item2['lift'],
                'lift_comb': round(lift_comb, 4),
                'synergy': round(synergy, 2),
                'fail_count': int(a),
                'base_count': int(total_in_base),
                'label': f"{f1}={v1} \n+ {f2}={v2}"
            })
            
    interaction_results.sort(key=lambda x: x['synergy'], reverse=True)
    return interaction_results

# --- UI 辅助函数 ---

def plot_interaction_heatmap(interactions):
    if not interactions: return None
    
    df = pd.DataFrame(interactions)
    # 提取唯一的特征描述
    nodes = list(set(df['f1'].tolist() + df['f2'].tolist()))
    
    # 建立矩阵
    matrix = pd.DataFrame(index=nodes, columns=nodes, dtype=float)
    for _, r in df.iterrows():
        matrix.loc[r['f1'], r['f2']] = r['lift_comb']
        matrix.loc[r['f2'], r['r1']] = r['lift_comb'] # 对称

    fig = px.imshow(
        matrix, 
        labels=dict(x="因素 A", y="因素 B", color="组合 Lift"),
        color_continuous_scale='Reds',
        title="因素组合交互效应热力图 (组合 Lift)"
    )
    fig.update_layout(height=600)
    return fig

# --- Main App ---

def main():
    st.set_page_config(page_title="归因分析优化版 - 交互效应", layout="wide")
    st.title("🚀 终检Fail归因分析 - 交互效应增强版")
    st.markdown("本版本在基础 Lift 分析之上，增加了 **“因素组合” (Interaction)** 识别功能，能够发现两个因素叠加导致的高风险点。")

    # 1. 侧边栏模拟数据生成 (简化流程用于演示)
    st.sidebar.header("数据配置")
    n_samples = st.sidebar.slider("模拟样本量", 500, 5000, 1000)
    
    @st.cache_data
    def get_data(n):
        # 构造带有交互效应的模拟数据
        np.random.seed(42)
        data = pd.DataFrame({
            'SN': [f'SN{i:05d}' for i in range(n)],
            'Machine_A': np.random.choice(['A1', 'A2', 'A3'], n),
            'Glue_Batch': np.random.choice(['G_v1', 'G_v2', 'G_v3'], n),
            'Operator': np.random.choice(['Op_01', 'Op_02', 'Op_03'], n),
            'Results': 'PASS',
            'Failed_Station': '', 'Failure_Mode': '', 'Date': '2026-05-15'
        })
        
        # 注入交互效应：Machine_A='A2' 且 Glue_Batch='G_v3' 时，Fail 概率暴增
        # 单独 A2 概率 5%, 单独 G_v3 概率 5%, 组合后概率 80%
        for i in range(n):
            prob = 0.02 # 基础背景噪声
            if data.loc[i, 'Machine_A'] == 'A2' and data.loc[i, 'Glue_Batch'] == 'G_v3':
                prob = 0.85
            elif data.loc[i, 'Machine_A'] == 'A2':
                prob = 0.08
            elif data.loc[i, 'Glue_Batch'] == 'G_v3':
                prob = 0.08
            
            if np.random.random() < prob:
                data.loc[i, 'Results'] = 'FAIL'
                data.loc[i, 'Failed_Station'] = 'TEST_01'
                data.loc[i, 'Failure_Mode'] = 'DEFECT_X'
        return data

    df_raw = get_data(n_samples)
    
    st.sidebar.info(f"当前数据包含 {len(df_raw)} 条记录，Fail 率: {(df_raw['Results']=='FAIL').mean():.1%}")

    # 2. 执行分析
    if st.sidebar.button("开始深度分析", type="primary"):
        df_base = df_raw
        df_fail = df_raw[df_raw['Results'] == 'FAIL']
        
        features = ['Machine_A', 'Glue_Batch', 'Operator']
        
        with st.spinner("计算单因素 Lift..."):
            lift_results, _, _ = compute_lift(df_base, df_fail, features)
        
        with st.spinner("挖掘组合交互效应..."):
            interactions = compute_interaction_effects(df_base, df_fail, lift_results, top_n=10)

        # 3. 结果展示
        tab1, tab2, tab3 = st.tabs(["📊 单因素排行", "🧪 双因素交互", "🌳 N因素深度归因"])

        with tab1:
            st.subheader("单因素 Lift 排行 (BH-FDR 校正后)")
            if lift_results:
                df_l = pd.DataFrame(lift_results).sort_values('lift', ascending=False)
                st.dataframe(df_l[['feature', 'value', 'lift', 'fail_count', 'p_value_adjusted']], use_container_width=True)
            else:
                st.warning("未发现显著单因素。")

        with tab2:
            st.subheader("双因素协同失效 (Interaction)")
            if interactions:
                df_i = pd.DataFrame(interactions)
                fig_i = px.scatter(
                    df_i, x="lift1", y="lift2", size="lift_comb", color="synergy",
                    hover_name="label", text="label",
                    labels={"lift1": "因素1 单独 Lift", "lift2": "因素2 单独 Lift", "synergy": "协同倍数"},
                    title="交互效应分布图 (球体大小=组合Lift)",
                    color_continuous_scale="Reds"
                )
                st.plotly_chart(fig_i, use_container_width=True)
                st.table(df_i[['f1', 'v1', 'f2', 'v2', 'lift1', 'lift2', 'lift_comb', 'synergy']].head(10))
            else:
                st.info("未发现明显的双因素交互效应。")

        with tab3:
            st.subheader("决策树自动发现：多因素组合路径")
            st.markdown("""
            **原理**：利用决策树（CART）自动寻找不良率最高的“群体特征路径”。  
            它可以自动识别 **3因素、4因素** 甚至更复杂的组合条件，并计算该条件下的群体 Lift。
            """)
            
            with st.spinner("正在训练深度归因树..."):
                # 选取 Top 20 显著特征参与训练，防止噪声干扰
                top_features = list(set([r['feature'] for r in lift_results[:20]]))
                rules = compute_decision_tree_rules(df_raw, top_features)
            
            if rules:
                for i, rule in enumerate(rules[:8]):
                    with st.container():
                        c1, c2 = st.columns([3, 1])
                        with c1:
                            st.write(f"**路径 {i+1}**: `{rule['path']}`")
                        with c2:
                            st.metric("组合 Lift", f"{rule['lift']}x", delta=f"不良率 {rule['fail_rate']:.1%}")
                        st.divider()
                
                df_rules = pd.DataFrame(rules)
                fig_tree = px.bar(
                    df_rules.head(15), x="lift", y="path", orientation='h',
                    color="fail_rate", text="fail_count",
                    labels={"lift": "群体提升度 (Lift)", "path": "组合条件路径"},
                    title="高风险组合路径排行 (按 Lift 降序)",
                    color_continuous_scale="OrRd"
                )
                fig_tree.update_layout(yaxis={'categoryorder':'total ascending'}, height=500)
                st.plotly_chart(fig_tree, use_container_width=True)
            else:
                st.info("数据量不足或未发现显著的多因素组合路径。")

    else:
        st.info("点击左侧「开始深度分析」查看归因结论。")

if __name__ == "__main__":
    main()
