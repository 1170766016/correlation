"""验证 split_date_signals: 拆分后的两部分数据"""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

df = app.generate_mock_data(n=2000)
df['Results'] = df['Results'].astype(str).str.strip().str.lower().map(
    lambda x: 'FAIL' if x in ('fail', 'ng', 'failed') else 'PASS'
).astype('category')

df, new_tc, orig_tc = app.extract_time_features(df)
skip = set(orig_tc) | {'Date', '_Date_dt'}
feats = [c for c in df.columns
         if c not in app.SKIP_COLS and c not in skip
         and df[c].nunique(dropna=True) >= 2
         and df[c].nunique(dropna=True) <= len(df) * 0.5]

df_fail = df[df['Results'] == 'FAIL']

# compute_lift 取全量
all_lift = app.compute_lift(df, df_fail, feats, min_fail_count=3, top_n=9999)
lift_results, date_summary = app.split_date_signals(all_lift)

print("=" * 60)
print(f"split_date_signals 结果")
print("=" * 60)
print(f"  可操作特征 (lift_results): {len(lift_results)} 条")
for d in lift_results[:5]:
    print(f"    {d['feature']}: {d['value']}  lift={d['lift']}")
if len(lift_results) > 5:
    print(f"    ... 还有 {len(lift_results)-5} 条")

print()
print(f"  异常日期摘要 (date_summary): {len(date_summary)} 条")
for ds in date_summary[:5]:
    lift_str = (f"Lift={ds['lift_max']}" if ds['lift_min'] == ds['lift_max']
                else f"Lift {ds['lift_min']}~{ds['lift_max']}")
    print(f"    {ds['date']}: {ds['n_procs']}个工序, {lift_str}, "
          f"Fail {ds['fail_count']}个 ({ds['fail_ratio']}%)")
    print(f"      工序: {', '.join(ds['procs'][:5])}{'...' if ds['n_procs']>5 else ''}")

print()
print("验证: lift_results 中是否有日期特征混入?",
      any('日期' in r['feature'] for r in lift_results))
print("验证: date_summary 结构完整?",
      all(k in ds for k in ('date','n_procs','procs','lift_max','lift_min','fail_count')
          for ds in date_summary) if date_summary else 'N/A (无日期信号)')
