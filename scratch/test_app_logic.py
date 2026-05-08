
import pandas as pd
import numpy as np
from app import extract_time_features, compute_lift, generate_mock_data

def test_logic():
    print("生成模拟数据...")
    df = generate_mock_data(n=200)
    print(f"数据量: {len(df)}")
    
    # 模拟筛选
    df_base = df.copy()
    df_fail = df_base[df_base['Results'] == 'FAIL'].copy()
    
    print("提取时间特征...")
    df_base, new_cols, time_cols = extract_time_features(df_base)
    df_fail = df_base.loc[df_fail.index]
    
    print(f"新增特征列: {new_cols[:5]}...")
    print(f"时间列: {time_cols[:5]}...")
    
    # 确定特征列
    all_feature_cols = []
    for col in df_base.columns:
        if col in ['Results', 'Date', 'Failed_Station', 'Failure_Mode', 'sn', 'Serial_No']:
            continue
        if col in time_cols:
            continue
        nuniq = df_base[col].nunique()
        if nuniq < 2:
            continue
        all_feature_cols.append(col)
    
    print(f"参与分析的特征列数: {len(all_feature_cols)}")
    
    print("计算 Lift...")
    results = compute_lift(df_base, df_fail, all_feature_cols, min_fail_count=2, top_n=10)
    
    print("\nTop 5 Lift 结果:")
    for i, res in enumerate(results[:5]):
        print(f"{i+1}. 特征: {res['feature']}, 取值: {res['value']}, Lift: {res['lift']}, Fail数: {res['fail_count']}")

    if len(results) > 0:
        print("\n✅ 逻辑测试通过!")
    else:
        print("\n⚠️ 未找到 Lift 结果，请检查逻辑或增加模拟数据。")

if __name__ == "__main__":
    test_logic()
