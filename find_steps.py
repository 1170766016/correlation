import json
import os

log_path = r"C:\Users\J\.gemini\antigravity\brain\2f4b7540-d224-4393-8907-3aa95cd9ebc4\.system_generated\logs\transcript.jsonl"

print(f"检查日志文件是否存在: {os.path.exists(log_path)}")
if not os.path.exists(log_path):
    print("找不到日志文件!")
    exit(1)

with open(log_path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        try:
            data = json.loads(line)
            step_index = data.get("step_index")
            step_type = data.get("type")
            status = data.get("status")
            
            # 搜索 tool_calls
            tool_calls = data.get("tool_calls", [])
            has_app = False
            for tc in tool_calls:
                args = str(tc.get("args", {}))
                if "app.py" in args:
                    has_app = True
            
            content = data.get("content", "")
            if "app.py" in content:
                has_app = True
                
            if has_app:
                print(f"行 {i} / 步骤 {step_index}: type={step_type}, status={status}, content_len={len(content)}, tool_calls={len(tool_calls)}")
        except Exception as e:
            print(f"解析错误行 {i}: {e}")
