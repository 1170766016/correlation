
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


def test_compute_lift_fixture():
    """
    手写小 fixture, 验证 compute_lift 的 3 个核心行为:
      1) Lift 数学正确
      2) min_fail_count 短路过滤
      3) _NAN_STRS (nan/None/''/'NaT') 过滤
    """
    # 基准池 10 行: machine 列 A:6, B:4; noise 列各不相同; blank 列全为 NaN-like
    df_base = pd.DataFrame({
        'machine': ['A']*6 + ['B']*4,
        'noise':   [f'v{i}' for i in range(10)],
        'blank':   [None, '', 'nan', 'NaT', None, '', 'nan', 'NaT', None, 'nan'],
    })
    # Fail 池 4 行 (是 base 的子集): B 出现 3 次, A 出现 1 次
    df_fail = df_base.iloc[[6, 7, 8, 0]].copy()  # 3×B + 1×A

    results = compute_lift(
        df_base, df_fail,
        feature_cols=['machine', 'noise', 'blank'],
        min_fail_count=2, top_n=10,
    )
    feat_to_row = {r['feature']: r for r in results}

    # --- 1) Lift 数学: B 取值 fail=3/4=0.75, base=4/10=0.4, lift=1.875
    assert 'machine' in feat_to_row, f"machine 应入选, got {list(feat_to_row)}"
    m = feat_to_row['machine']
    assert m['value'] == 'B', f"machine 最高 Lift 应为 B, got {m['value']}"
    assert m['fail_count'] == 3
    assert m['base_count'] == 4
    assert abs(m['lift'] - 1.875) < 1e-6, f"Lift 计算错误: {m['lift']}"

    # --- 2) min_fail_count 过滤: noise 每值只出现 1 次 fail < 2, 整列短路
    assert 'noise' not in feat_to_row, "noise 不应入选 (每值 fail < min_fail_count)"

    # --- 3) _NAN_STRS 过滤: blank 列所有取值都在过滤集 → 无结果
    assert 'blank' not in feat_to_row, "blank 列所有值都是 nan-like, 应被过滤"

    # --- 4) 返回结构按 lift 降序
    assert all(results[i]['lift'] >= results[i+1]['lift'] for i in range(len(results)-1))

    print("✅ compute_lift fixture 测试通过")


if __name__ == "__main__":
    test_compute_lift_fixture()
    test_logic()
