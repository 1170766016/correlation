import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

import db
import engine

load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_BASE = os.getenv("LLM_API_BASE", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

app = FastAPI(title="AI 根因分析告警中枢", description="接收外部产线异常告警并自动触发根因诊断")

class AnomalyTriggerRequest(BaseModel):
    project: str
    failed_station: str
    hours_back: int = 24

def process_anomaly_background(req: AnomalyTriggerRequest):
    print(f"\n[{req.project}] 🚀 后台任务启动: 正在抓取过去 {req.hours_back} 小时 [{req.failed_station}] 的异常数据...")
    try:
        # 1. 抓取数据
        df_raw = db.fetch_recent_data(project=req.project, station=req.failed_station, hours=req.hours_back)
        
        # 2. 跑融合分析引擎
        print(f"[{req.project}] 🔄 数据就绪，启动统计学与 ML 融合分析引擎...")
        result = engine.run_fused_diagnosis(
            df_raw=df_raw,
            project=req.project,
            station=req.failed_station,
            api_key=LLM_API_KEY,
            api_base=LLM_API_BASE,
            model=LLM_MODEL
        )
        
        # 3. 输出并预留 Webhook 推送
        print("\n" + "="*50)
        print("🎯 [最终融合诊断报告]")
        print("="*50)
        print(result["report"])
        print("="*50)
        print("提示：此处可以将上面的报告通过企业微信/钉钉 Webhook 推送给产线质量群！")
        
    except Exception as e:
        print(f"❌ 后台任务执行失败: {str(e)}")


@app.post("/api/v1/trigger_diagnosis")
async def trigger_diagnosis(req: AnomalyTriggerRequest, background_tasks: BackgroundTasks):
    """
    外部系统（如 MES / 良率监控大屏）调用此接口触发诊断。
    接口会立刻返回 HTTP 200，后台排队拉取 Doris(MySQL) 数据跑 LightGBM。
    """
    if not req.project or not req.failed_station:
        raise HTTPException(status_code=400, detail="project 和 failed_station 不能为空")
        
    # 将高耗时的拉取、计算和调大模型操作放入后台异步执行
    background_tasks.add_task(process_anomaly_background, req)
    
    return {
        "status": "success",
        "message": f"已成功接收 {req.project} 告警，后台已开始分析 {req.failed_station} 工站。请等待群通知。",
        "task_id": "T" + os.urandom(4).hex()
    }

if __name__ == "__main__":
    import uvicorn
    # 为了方便测试直接运行
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
