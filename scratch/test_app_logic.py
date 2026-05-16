
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np
from app import extract_time_features, compute_lift


def make_mock_df(n=200):
    """内联生成模拟数据，替代已删除的 app.generate_mock_data"""
    np.random.seed(42)
    base_date = pd.Timestamp("2026-04-01")
    rows = []
    for i in range(n):
        dt = base_date + pd.Timedelta(days=np.random.randint(0, 29))
        is_fail = np.random.random() < 0.30
        rows.append({
            "Date": dt.strftime("%Y-%m-%d"),
            "Results": "FAIL" if is_fail else "PASS",
            "Failed_Station": "METROLOGY" if is_fail else "",
            "Failure_Mode": "VA_DRIVER2_AFE_P_VA/CONNECT NG" if is_fail and np.random.random() < 0.5 else "",
            "VCM_M1_FPC_UpCoil_Attach_End_Time": (dt + pd.Timedelta(hours=np.random.randint(0, 23))).strftime("%Y-%m-%d %H:%M:%S"),
            "VCM_M2_Stator_Plasma_End_Time": (dt + pd.Timedelta(hours=np.random.randint(0, 23))).strftime("%Y-%m-%d %H:%M:%S"),
            "VCM_M3_BCA_Bending_End_Time": (dt + pd.Timedelta(hours=np.random.randint(0, 23))).strftime("%Y-%m-%d %H:%M:%S"),
            "VCM_M8_Aging_MC_ID": f"MC_{np.random.randint(1, 16):03d}",
            "VCM_M1_FPC_UpCoil_Attach_MC_ID": f"MC_{np.random.randint(1, 16):03d}",
            "Config": np.random.choice(["Config_A", "Config_B"]),
        })
    return pd.DataFrame(rows)


def test_logic():
    print("生成模拟数据...")
    df = make_mock_df(n=200)
    print(f"数据量: {len(df)}")

    df_base = df.copy()
    df_fail = df_base[df_base['Results'] == 'FAIL'].copy()

    print("提取时间特征...")
    df_base, new_cols = extract_time_features(df_base)
    df_fail = df_base.loc[df_fail.index]

    print(f"新增特征列: {new_cols[:5]}...")

    all_feature_cols = []
    for col in df_base.columns:
        if col in ['Results', 'Date', 'Failed_Station', 'Failure_Mode']:
            continue
        if col.endswith("_6H"):
            continue
        nuniq = df_base[col].nunique()
        if nuniq < 2:
            continue
        all_feature_cols.append(col)

    print(f"参与分析的特征列数: {len(all_feature_cols)}")

    print("计算 Lift...")
    lift_res, ratio_res, _ = compute_lift(df_base, df_fail, all_feature_cols, min_count=2)

    print("\nTop 5 Lift 结果:")
    for i, res in enumerate(lift_res[:5]):
        print(f"{i+1}. 特征: {res['feature']}, 取值: {res['value']}, Lift: {res['lift']:.4f}, Fail数: {res['fail_count']}")

    if len(lift_res) > 0:
        print("\n✅ 逻辑测试通过!")
    else:
        print("\n⚠️ 未找到 Lift 结果，请检查逻辑或增加模拟数据。")


def test_compute_lift_fixture():
    """
    手写小 fixture, 验证 compute_lift 的 3 个核心行为:
      1) Lift 数学正确
      2) min_count 短路过滤
      3) nan/None/'' 过滤
    """
    df_base = pd.DataFrame({
        'machine': ['A']*6 + ['B']*4,
        'noise':   [f'v{i}' for i in range(10)],
        'blank':   [None, '', 'nan', 'NaT', None, '', 'nan', 'NaT', None, 'nan'],
    })
    df_fail = df_base.iloc[[6, 7, 8, 0]].copy()

    results = compute_lift(
        df_base, df_fail,
        feature_cols=['machine', 'noise', 'blank'],
        min_count=2,
    )
    lift_res, _, _ = results
    feat_to_row = {r['feature']: r for r in lift_res}

    # --- 1) Lift 数学: B 取值 fail=3/4=0.75, base=4/10=0.4, lift=1.875
    assert 'machine' in feat_to_row, f"machine 应入选, got {list(feat_to_row)}"
    m = feat_to_row['machine']
    assert m['value'] == 'B', f"machine 最高 Lift 应为 B, got {m['value']}"
    assert m['fail_count'] == 3
    assert m['base_count'] == 4
    assert abs(m['lift'] - 1.875) < 1e-6, f"Lift 计算错误: {m['lift']}"

    # --- 2) min_count 过滤: noise 每值只出现 1 次 fail < 2, 整列短路
    assert 'noise' not in feat_to_row, "noise 不应入选 (每值 fail < min_count)"

    # --- 3) nan/None/'' 过滤
    assert 'blank' not in feat_to_row, "blank 列所有值都是 nan-like, 应被过滤"

    # --- 4) 返回结构按 lift 降序
    assert all(lift_res[i]['lift'] >= lift_res[i+1]['lift'] for i in range(len(lift_res)-1))

    print("✅ compute_lift fixture 测试通过")


if __name__ == "__main__":
    test_compute_lift_fixture()
    test_logic()
