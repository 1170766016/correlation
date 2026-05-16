"""验证时间特征提取和 Lift 计算"""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import app

np.random.seed(42)
base_date = pd.Timestamp("2026-04-01")
rows = []
for i in range(2000):
    dt = base_date + pd.Timedelta(days=np.random.randint(0, 29))
    is_fail = np.random.random() < 0.30
    rows.append({
        "Date": dt.strftime("%Y-%m-%d"),
        "Results": "FAIL" if is_fail else "PASS",
        "Failed_Station": "METROLOGY" if is_fail else "",
        "Failure_Mode": "VA_DRIVER2_AFE_P_VA/CONNECT NG" if is_fail and np.random.random() < 0.5 else "",
        "VCM_M1_FPC_UpCoil_Attach_End_Time": (dt + pd.Timedelta(hours=np.random.randint(0, 23))).strftime("%Y-%m-%d %H:%M:%S"),
        "VCM_M8_Aging_MC_ID": f"MC_{np.random.randint(1, 16):03d}",
        "VCM_M1_FPC_UpCoil_Attach_MC_ID": f"MC_{np.random.randint(1, 16):03d}",
        "Config": np.random.choice(["Config_A", "Config_B"]),
    })
df = pd.DataFrame(rows)

df, new_cols = app.extract_time_features(df)
skip = set() | {'Date'}
feats = [c for c in df.columns
         if c not in app.SKIP_COLS and c not in skip
         and df[c].nunique(dropna=True) >= 2]

df_fail = df[df['Results'] == 'FAIL']
all_lift = app.compute_lift(df, df_fail, feats, min_count=3)

print("=" * 60)
print(f"Lift 计算结果: {len(all_lift)} 条")
for d in all_lift[:5]:
    print(f"  {d['feature']}: {d['value']}  lift={d['lift']:.4f}")
print()
print(f"新增时间特征列: {len(new_cols)}")
print("Done")
