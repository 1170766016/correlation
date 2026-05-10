"""
独立测试 app.py 中的 LLM 连接逻辑。

用法:
  1. 直接改下面的 BASE_URL / MODEL / API_KEY 三个变量
  2. 或通过环境变量覆盖:
       set TEST_LLM_URL=https://api.deepseek.com/v1
       set TEST_LLM_MODEL=deepseek-chat
       set TEST_LLM_KEY=sk-xxxxxxxx
  3. 运行:
       .conda/python.exe scratch/test_llm_connection.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import normalize_llm_url, call_llm


# ============ 测试配置 (按需修改) ============
BASE_URL = os.getenv("TEST_LLM_URL", "https://api.deepseek.com/v1")
MODEL    = os.getenv("TEST_LLM_MODEL", "deepseek-v4-flash")
API_KEY  = os.getenv("TEST_LLM_KEY", "")
# ============================================


def test_url_normalization():
    print("=" * 60)
    print("1) normalize_llm_url 单元测试")
    print("=" * 60)
    cases = [
        ("http://x.x.x.x/v1",                    "http://x.x.x.x/v1/chat/completions"),
        ("http://x.x.x.x/v1/",                   "http://x.x.x.x/v1/chat/completions"),
        ("http://x.x.x.x/v1/chat/completions",   "http://x.x.x.x/v1/chat/completions"),
        ("http://x.x.x.x/v1/chat/completions/",  "http://x.x.x.x/v1/chat/completions"),
        ("https://api.deepseek.com/v1",          "https://api.deepseek.com/v1/chat/completions"),
    ]
    all_pass = True
    for inp, expected in cases:
        got = normalize_llm_url(inp)
        ok = got == expected
        all_pass &= ok
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {inp!r:55s} -> {got!r}")
        if not ok:
            print(f"         expected: {expected!r}")
    print(f"\n  结果: {'全部通过' if all_pass else '有失败用例'}\n")
    return all_pass


def test_real_call():
    print("=" * 60)
    print("2) 真实 LLM 调用测试")
    print("=" * 60)
    print(f"  Base URL : {BASE_URL}")
    print(f"  Model    : {MODEL}")
    print(f"  API Key  : {'(已设置, 长度=' + str(len(API_KEY)) + ')' if API_KEY else '(未设置)'}")
    print(f"  规范化后 : {normalize_llm_url(BASE_URL)}")
    print()

    if not API_KEY and 'deepseek' in BASE_URL:
        print("  警告: deepseek 需要 API Key。请设置 TEST_LLM_KEY 环境变量或改 API_KEY 常量。")
        print("  仍然发起一次请求以观察错误信息...\n")

    # 模拟一份极简的 lift 结果
    mock_results = [
        {
            "feature": "VCM_M4_Rotor_Magnet_Attach_MC_ID",
            "value": "MC_BAD_007",
            "lift": 3.85,
            "fail_count": 42,
            "fail_ratio": 28.0,
            "base_count": 60,
            "base_ratio": 7.3,
        },
        {
            "feature": "VCM_M8_Aging_Socket",
            "value": "S3",
            "lift": 2.10,
            "fail_count": 18,
            "fail_ratio": 12.0,
            "base_count": 95,
            "base_ratio": 5.7,
        },
    ]

    print("  发送请求中...\n")
    result = call_llm(
        api_key=API_KEY,
        top_results=mock_results,
        station_filter="METROLOGY",
        mode_filter="VA_DRIVER2_AFE_P_VA/CONNECT NG",
        api_url=BASE_URL,
        model_name=MODEL,
    )

    print("-" * 60)
    print("  响应内容:")
    print("-" * 60)
    print(result)
    print("-" * 60)

    # 简单判断是否成功 (失败时 call_llm 返回以 "⚠️" 开头的字符串)
    ok = not result.startswith("⚠️")
    print(f"\n  判定: {'调用成功' if ok else '调用失败'}\n")
    return ok


if __name__ == "__main__":
    u_ok = test_url_normalization()
    c_ok = test_real_call()
    print("=" * 60)
    print(f"总结: url 规范化 = {'OK' if u_ok else 'FAIL'}, LLM 调用 = {'OK' if c_ok else 'FAIL'}")
    print("=" * 60)
    sys.exit(0 if (u_ok and c_ok) else 1)
