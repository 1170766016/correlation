import os
import pandas as pd
from sqlalchemy import create_engine
import pymysql
import random
import numpy as np
from datetime import datetime, timedelta

# 从环境变量读取数据库配置
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "password")
DB_NAME = os.getenv("DB_NAME", "factory_db")
DB_PORT = os.getenv("DB_PORT", "3306")

def get_engine():
    db_url = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(db_url)

def clean_db_data(df: pd.DataFrame) -> pd.DataFrame:
    """预处理数据格式（与前端 Dashboard 逻辑一致）"""
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    
    for col in df.columns:
        if col.endswith("_Time") or col.endswith("_time"):
            if not col.lower().endswith("staging_time"):
                df[col] = pd.to_datetime(df[col], errors="coerce")
    return df

def fetch_recent_data(project: str, station: str, hours: int = 24) -> pd.DataFrame:
    """
    直连 MySQL 拉取过去 hours 小时的指定项目和工站数据。
    """
    query = f"""
        SELECT * FROM eol_data
        WHERE Project = '{project}'
          AND Date >= NOW() - INTERVAL {hours} HOUR
    """
    
    try:
        engine = get_engine()
        df = pd.read_sql(query, engine)
        if len(df) == 0:
            raise ValueError("查询结果为空")
    except Exception as e:
        print(f"⚠️ MySQL 连接失败或无数据 ({e})。将使用本地 Mock 数据进行演示...")
        df = generate_mock_data_for_api(project, station, n=1000)
        
    return clean_db_data(df)

def generate_mock_data_for_api(project: str, station: str, n: int = 500) -> pd.DataFrame:
    """如果连不上数据库，自动降级生成的演示数据"""
    random.seed(42)
    np.random.seed(42)
    stations = ["METROLOGY", "FUNCTION_TEST", "FGAVI"]
    machines = [f"MC_{i:03d}" for i in range(1, 16)]
    lots = [f"LOT_{i:04d}" for i in range(2001, 2010)]
    
    rows = []
    base_date = datetime.now() - timedelta(hours=24)
    for i in range(n):
        dt = base_date + timedelta(minutes=i * (24 * 60 / n))
        
        is_fail = random.random() < 0.10
        is_target = is_fail and random.random() < 0.6
        
        if is_target:
            st = station if station else "METROLOGY"
            fm = "VA_DRIVER_NG"
            mc = "MC_BAD_007"
            lot = "LOT_2005"
        elif is_fail:
            st = random.choice(stations)
            fm = "OTHER_NG"
            mc = random.choice(machines)
            lot = random.choice(lots)
        else:
            st = ""
            fm = ""
            mc = random.choice(machines)
            lot = random.choice(lots)
            
        rows.append({
            "sn": f"SN{i:06d}",
            "Date": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "Results": "FAIL" if is_fail else "PASS",
            "Failed_Station": st,
            "Failure_Mode": fm,
            "Project": project,
            "VCM_M1_FPC_UpCoil_Attach_MC_ID": mc,
            "VCM_Glue_lot_ID_1": lot,
            "VCM_M1_FPC_UpCoil_Attach_End_Time": (dt + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        })
    return pd.DataFrame(rows)
