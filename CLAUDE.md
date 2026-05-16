# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Streamlit web app for analyzing common failure patterns in manufacturing final inspection ("终检Fail产品全流程共性聚集度分析"). Uses **Lift analysis** (提升度) to identify which equipment/stations/cavities/lots are over-represented among failed products compared to the overall population.

## Commands

```bash
# Run the Streamlit app (using the bundled conda environment)
.conda/Scripts/streamlit.exe run app.py

# Or if streamlit is on PATH
streamlit run app.py

# Convert Excel to CSV (recommended for large datasets)
.conda/python.exe excel_to_csv_converter.py data.xlsx
.conda/python.exe excel_to_csv_converter.py .          # batch convert all .xlsx in dir

# Install dependencies
pip install -r requirements.txt

# Run core logic tests (no Streamlit server needed)
python scratch/test_app_logic.py
```

## Architecture

`app.py` is the entire application (~655 lines). The data flow is:

1. **Data loading** — User uploads CSV/Excel/Parquet or uses local `PRB数据.csv`. Required columns: `Results`, `Date`, `Failed_Station`, `Failure_Mode`. Columns are classified via `classify_columns()` into time, ID, meta, and discrete feature groups.

2. **Filtering** — Data is filtered by date range, `Failed_Station`, and `Failure_Mode`. The "base pool" (`df_base`) contains all rows (Pass + Fail) matching filters; `df_fail` is the Fail-only subset.

3. **Time feature extraction** (`extract_time_features`) — Datetime columns are parsed and replaced with a date feature (`_日期`, `YYYY-MM-DD`). Aggregating to the day level is intentional: hour/shift-level grouping merged records from different days into the same bucket, which diluted Fail concentration and produced misleading Lift values.

4. **Lift calculation** (`compute_lift`) — For each feature column, computes `Lift = P(fail|value) / P(base|value)`. Returns top N results where Lift > 1.0 and fail count ≥ `min_fail_count`. Columns with >50% unique values (like serial numbers) are excluded.

5. **LLM reporting** (`call_llm`) — Sends top results to a company LLM (OpenAI-compatible API) for structured root-cause analysis in Chinese. API URL and key are configured via env vars or sidebar input.

6. **Visualization** — Plotly horizontal bar chart of Lift values + a detailed data table.

## Key Constants

- **`SKIP_COLS`** — Metadata columns excluded from analysis (SNs, timestamps, Results, etc.)
- **`FOCUS_COLS`** — ~200 whitelisted manufacturing process columns (MC_IDs, cavity IDs, vendor/lot info, etc.)
- **`REQUIRED_COLS`** — `['Results', 'Date', 'Failed_Station', 'Failure_Mode']` — must exist in uploaded data
- **`LLM_API_URL`** / **`LLM_MODEL`** — Configured via `COMPANY_LLM_URL` and `COMPANY_MODEL_NAME` env vars; the API key is set via `COMPANY_LLM_KEY` env var or sidebar input

## Dependencies

`streamlit`, `pandas`, `plotly`, `requests`, `openpyxl` (see `requirements.txt`). Python 3.10+.
