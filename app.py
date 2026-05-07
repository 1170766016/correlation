import os, json, warnings, gc
import numpy as np
import pandas as pd
from flask import Flask, render_template, request, jsonify
from scipy.stats import chi2_contingency, pointbiserialr
from llm_service import LLMSummaryService

warnings.filterwarnings('ignore')

app = Flask(__name__)
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB


# ===================== MEMORY OPTIMIZATION =====================
def optimize_dtypes(df):
    """将分类列转为 category 类型，大幅降低内存占用"""
    for col in df.columns:
        try:
            if df[col].dtype == 'object':
                # 如果唯一值数量远小于总行数，转为 category 类型可节省 80%+ 内存
                if df[col].nunique() < df[col].count() * 0.5:
                    df[col] = df[col].astype('category')
            elif df[col].dtype == 'float64':
                df[col] = df[col].astype('float32')
            elif df[col].dtype == 'int64':
                if df[col].isna().any():
                    continue
                col_min, col_max = df[col].min(), df[col].max()
                if col_min >= np.iinfo(np.int32).min and col_max <= np.iinfo(np.int32).max:
                    df[col] = df[col].astype('int32')
        except Exception:
            continue
    return df


# ===================== ANALYSIS ENGINE =====================
def cramers_v(ct):
    chi2 = chi2_contingency(ct)[0]
    n_total = ct.sum().sum()
    min_dim = min(ct.shape) - 1
    if min_dim == 0 or n_total == 0:
        return 0.0
    return float(np.sqrt(chi2 / (n_total * min_dim)))

def analyze(df, top_n=10):
    skip = {'Serial_No','Date','Failed_Station','Failure_Mode','Results','Project','sn',
            'transdatetime','insertdatetime','Rev','Site','Serial_No_18'}
    feature_cols = [c for c in df.columns if c not in skip]
    y = (df['Results'] == 'Fail').astype(np.int8)
    total = len(df)
    fail_n = int(y.sum())
    pass_n = total - fail_n

    # 1. Correlation scores
    scores = []
    half_total = total * 0.5
    for col in feature_cols:
        series = df[col]
        na_count = series.isna().sum()
        if na_count > half_total:
            continue
        try:
            nunique = series.nunique()
            if nunique < 2:
                continue
            if series.dtype.kind in ('f', 'i') and nunique > 15:
                # 连续变量：点双列相关
                mask = series.notna()
                if mask.sum() < 20:
                    continue
                corr, pval = pointbiserialr(y[mask], series[mask])
                scores.append({'feature': col, 'score': round(abs(float(corr)), 4),
                               'type': 'numerical', 'method': 'Point-biserial'})
            else:
                # 分类变量：Cramér's V
                col_data = series.astype(str).fillna('MISSING')
                ct = pd.crosstab(col_data, y)
                if ct.shape[0] < 2 or ct.shape[1] < 2:
                    continue
                v = cramers_v(ct)
                scores.append({'feature': col, 'score': round(v, 4),
                               'type': 'categorical', 'method': "Cramér's V"})
        except:
            continue

    scores.sort(key=lambda x: x['score'], reverse=True)
    top_scores = scores[:top_n]

    # 2. Pass/NG comparison for top categorical features
    pass_ng = []
    for item in top_scores[:5]:
        col = item['feature']
        if item['type'] == 'categorical':
            ct = pd.crosstab(df[col], df['Results'])
            categories = ct.index.tolist()[:8]
            pass_vals = [int(ct.loc[c, 'Pass']) if 'Pass' in ct.columns and c in ct.index else 0 for c in categories]
            fail_vals = [int(ct.loc[c, 'Fail']) if 'Fail' in ct.columns and c in ct.index else 0 for c in categories]
            pass_ng.append({'feature': col, 'categories': [str(c) for c in categories],
                           'pass_values': pass_vals, 'fail_values': fail_vals})

    # 3. Hardware attribution (Nozzle/Socket/MC NG counts)
    hw_keywords = ('Nozzle', 'Socket', 'MC_ID', 'Head_ID')
    hw_cols = [c for c in feature_cols if any(k in c for k in hw_keywords)]
    hw_attr = []
    fail_mask = df['Results'] == 'Fail'
    fail_count = fail_mask.sum()
    if fail_count > 0:
        for col in hw_cols:
            vc_fail = df.loc[fail_mask, col].value_counts().head(5)
            vc_total = df[col].value_counts()
            for val, cnt in vc_fail.items():
                t = int(vc_total.get(val, cnt))
                hw_attr.append({'unit': f"{col}={val}", 'column': col, 'value': str(val),
                               'ng_count': int(cnt), 'total': t,
                               'ng_rate': round(cnt / t * 100, 1) if t > 0 else 0})
    hw_attr.sort(key=lambda x: x['ng_rate'], reverse=True)
    hw_attr = hw_attr[:10]

    # 4. Boxplot data for top numerical features
    num_features = [s for s in top_scores if s['type'] == 'numerical'][:5]
    boxplot = []
    pass_mask = ~fail_mask
    def bstat(s):
        if len(s) == 0:
            return [0,0,0,0,0]
        return [round(float(s.min()),2), round(float(s.quantile(0.25)),2),
                round(float(s.median()),2), round(float(s.quantile(0.75)),2),
                round(float(s.max()),2)]
    for item in num_features:
        col = item['feature']
        pass_data = df.loc[pass_mask, col].dropna()
        fail_data = df.loc[fail_mask, col].dropna()
        boxplot.append({'feature': col, 'pass': bstat(pass_data), 'fail': bstat(fail_data)})

    # 5. Failed station distribution
    if 'Failed_Station' in df.columns:
        station_dist = df.loc[fail_mask, 'Failed_Station'].value_counts().to_dict()
        station_dist = {str(k): int(v) for k, v in station_dist.items() if k and str(k).strip()}
    else:
        station_dist = {}

    # 6. Failure mode distribution
    if 'Failure_Mode' in df.columns:
        mode_dist = df.loc[fail_mask, 'Failure_Mode'].value_counts().to_dict()
        mode_dist = {str(k): int(v) for k, v in mode_dist.items() if k and str(k).strip()}
    else:
        mode_dist = {}

    # 7. NG trend by date (不做 df.copy，直接用临时 Series)
    if 'Date' in df.columns:
        date_series = pd.to_datetime(df['Date'], errors='coerce').dt.date
        temp = pd.DataFrame({'_date': date_series, '_fail': fail_mask})
        daily = temp.groupby('_date').agg(
            total=('_fail', 'count'),
            fail=('_fail', 'sum')
        ).reset_index()
        daily['rate'] = (daily['fail'] / daily['total'] * 100).round(1)
        ng_trend = {
            'dates': [str(d) for d in daily['_date']],
            'totals': daily['total'].tolist(),
            'fails': [int(x) for x in daily['fail']],
            'rates': daily['rate'].tolist()
        }
    else:
        ng_trend = {'dates':[],'totals':[],'fails':[],'rates':[]}

    # 8. Date range
    date_range = {'min': '', 'max': ''}
    if 'Date' in df.columns:
        dts = pd.to_datetime(df['Date'], errors='coerce').dropna()
        if len(dts) > 0:
            date_range = {'min': str(dts.min().date()), 'max': str(dts.max().date())}

    return {
        'summary': {'total': total, 'pass': pass_n, 'fail': fail_n,
                    'fail_rate': round(fail_n/total*100, 2) if total else 0},
        'correlation_top': top_scores,
        'pass_ng_comparison': pass_ng,
        'hardware_attribution': hw_attr,
        'boxplot_data': boxplot,
        'station_dist': station_dist,
        'mode_dist': mode_dist,
        'ng_trend': ng_trend,
        'date_range': date_range,
        'all_scores': scores,
    }


# ===================== ROUTES =====================
CACHED_DF = None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/analyze', methods=['POST'])
def upload_analyze():
    global CACHED_DF
    f = request.files.get('datafile')
    if not f:
        return jsonify({'error': '请上传数据文件'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower()
    path = os.path.join(app.config['UPLOAD_FOLDER'], 'data.' + ext)
    f.save(path)

    # ★ 关键优化：先读筛选表，再用 usecols 只加载需要的列
    # 这样 10万行×500列 的 Excel 只会加载筛选后的列，内存和速度大幅提升
    filter_cols = None
    ff = request.files.get('filterfile')
    if ff:
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], 'filter.' + ff.filename.rsplit('.', 1)[-1].lower())
        ff.save(fpath)
        if fpath.endswith('.csv'):
            fdf = pd.read_csv(fpath, header=None, usecols=[0])
        else:
            fdf = pd.read_excel(fpath, header=None, usecols=[0])
        filter_cols = fdf.iloc[:, 0].dropna().astype(str).tolist()
        # 加上系统必需列
        filter_cols += ['Results', 'Date', 'Failed_Station', 'Failure_Mode', 'Serial_No']
        filter_cols = list(set(filter_cols))  # 去重
        del fdf
        gc.collect()

    # 读取主数据文件
    print(f"[INFO] 正在加载数据文件: {path}")
    if ext == 'csv':
        if filter_cols:
            # CSV 先读表头确定有效列名
            header = pd.read_csv(path, nrows=0).columns.tolist()
            valid_cols = [c for c in filter_cols if c in header]
            df = pd.read_csv(path, usecols=valid_cols)
        else:
            df = pd.read_csv(path)
    else:
        if filter_cols:
            # Excel 先读表头确定有效列名
            header = pd.read_excel(path, nrows=0, engine='openpyxl').columns.tolist()
            valid_cols = [c for c in filter_cols if c in header]
            df = pd.read_excel(path, usecols=valid_cols, engine='openpyxl')
        else:
            df = pd.read_excel(path, engine='openpyxl')

    print(f"[INFO] 数据加载完成: {len(df)} 行 × {len(df.columns)} 列, 内存: {df.memory_usage(deep=True).sum() / 1024 / 1024:.1f} MB")

    # 内存优化：类型降级
    df = optimize_dtypes(df)
    print(f"[INFO] 类型优化后内存: {df.memory_usage(deep=True).sum() / 1024 / 1024:.1f} MB")
    gc.collect()

    # Detect Results column
    if 'Results' not in df.columns:
        for c in df.columns:
            if df[c].nunique() == 2:
                vals = set(df[c].dropna().unique())
                if vals & {'Fail','FAIL','fail','NG','ng','Ng'}:
                    df = df.rename(columns={c: 'Results'})
                    break

    if 'Results' not in df.columns:
        return jsonify({'error': '未找到Results列'}), 400

    # Normalize Results
    df['Results'] = df['Results'].astype(str).apply(
        lambda x: 'Fail' if x.lower() in ['fail','ng','failed'] else 'Pass')

    top_n = int(request.form.get('top_n', 10))
    CACHED_DF = df
    print(f"[INFO] 开始分析...")
    result = analyze(df, top_n)
    print(f"[INFO] 分析完成")
    return jsonify(result)

@app.route('/api/filter_by_date', methods=['POST'])
def filter_by_date():
    global CACHED_DF
    if CACHED_DF is None:
        return jsonify({'error': '请先加载数据'}), 400
    body = request.get_json()
    start = body.get('start')
    end = body.get('end')
    top_n = body.get('top_n', 10)

    df = CACHED_DF
    if 'Date' in df.columns and start and end:
        dt_col = pd.to_datetime(df['Date'], errors='coerce')
        mask = (dt_col >= start) & (dt_col <= end + ' 23:59:59')
        df = df[mask]
    if len(df) == 0:
        return jsonify({'error': '筛选后无数据'}), 400
    result = analyze(df, top_n)
    return jsonify(result)

@app.route('/api/get_llm_summary', methods=['POST'])
def get_llm_summary():
    global CACHED_DF
    if CACHED_DF is None:
        return jsonify({'error': '请先加载数据并分析'}), 400
    
    analysis_result = request.get_json()
    summary_text = LLMSummaryService.get_summary(analysis_result)
    return jsonify({'summary': summary_text})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8080)
