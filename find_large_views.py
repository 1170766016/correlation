import json
import os

log_path = r"C:\Users\J\.gemini\antigravity\brain\2f4b7540-d224-4393-8907-3aa95cd9ebc4\.system_generated\logs\transcript.jsonl"

with open(log_path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        try:
            data = json.loads(line)
            step_index = data.get("step_index")
            step_type = data.get("type")
            status = data.get("status")
            
            if step_type == "VIEW_FILE" and status == "DONE":
                content = data.get("content", "")
                if "run_ml_and_llm_async" in content:
                    print(f"步骤 {step_index}: VIEW_FILE 成功, content 长度: {len(content)}")
                    # 打印前 3 行和后 3 行
                    lines = content.splitlines()
                    print(f"  前3行: {lines[:3]}")
                    print(f"  后3行: {lines[-3:]}")
        except Exception as e:
            pass
