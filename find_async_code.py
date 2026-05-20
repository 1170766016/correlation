import json
import os

log_path = r"C:\Users\J\.gemini\antigravity\brain\2f4b7540-d224-4393-8907-3aa95cd9ebc4\.system_generated\logs\transcript.jsonl"

with open(log_path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        try:
            data = json.loads(line)
            step_index = data.get("step_index")
            tool_calls = data.get("tool_calls", [])
            for tc in tool_calls:
                args = tc.get("args", {})
                args_str = str(args)
                if "模拟数据" in args_str:
                    print(f"步骤 {step_index}: method={tc.get('method')}")
                    if "ReplacementContent" in args:
                        print(f"  ReplacementContent: {args['ReplacementContent'][:300]}")
                    if "CodeContent" in args:
                        print(f"  CodeContent: {args['CodeContent'][:300]}")
        except Exception as e:
            pass
