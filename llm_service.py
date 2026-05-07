import os
import json
import requests

# ===================== CONFIGURATION =====================
# 这里配置你公司自建模型的 API 信息
# 建议通过环境变量管理敏感信息，或者在此手动修改
LLM_API_URL = os.getenv("COMPANY_LLM_URL", "http://your-company-api-endpoint/v1/chat/completions")
LLM_API_KEY = os.getenv("COMPANY_LLM_KEY", "your-api-key-here")
MODEL_NAME = os.getenv("COMPANY_MODEL_NAME", "company-model-v1")

class LLMSummaryService:
    @staticmethod
    def generate_prompt(data):
        """将统计分析结果转化为结构化的 Prompt"""
        summary = data.get('summary', {})
        corr_top = data.get('correlation_top', [])
        hw_attr = data.get('hardware_attribution', [])
        boxplot = data.get('boxplot_data', [])
        
        # 1. 概览信息
        prompt = f"### 分析任务：制程异常归因分析\n"
        prompt += f"**数据概览**：总样本 {summary.get('total')}, Fail数 {summary.get('fail')}, 不良率 {summary.get('fail_rate')}%\n\n"
        
        # 2. 相关性排名
        prompt += "### 1. 统计相关性排名 (Top N)：\n"
        for i, item in enumerate(corr_top[:5]):
            prompt += f"- {item['feature']}: 相关性分数 {item['score']} ({item['type']})\n"
        
        # 3. 硬件归因证据
        if hw_attr:
            prompt += "\n### 2. 硬件单元异常证据：\n"
            for item in hw_attr[:3]:
                prompt += f"- 单元: {item['unit']}, NG数: {item['ng_count']}, 该单元不良率: {item['ng_rate']}%\n"
        
        # 4. 参数偏移证据
        if boxplot:
            prompt += "\n### 3. 参数偏移证据 (Boxplot)：\n"
            for item in boxplot[:2]:
                p = item['pass']
                f = item['fail']
                prompt += f"- 特征: {item['feature']}, Pass中位数: {p[2]}, Fail中位数: {f[2]} (偏移量: {round(abs(f[2]-p[2]), 4)})\n"
        
        prompt += "\n---\n"
        prompt += "### 任务要求：\n"
        prompt += "作为一名资深制程诊断工程师，请根据上述统计证据，输出一份结构化的排查指引：\n"
        prompt += "1. **结论归纳**：用业务语言一句话总结最可能的异常点（过滤弱相关，直指核心）。\n"
        prompt += "2. **证据列举**：分条列出支撑上述结论的硬件证据和参数证据。\n"
        prompt += "3. **处置建议**：给出具体、可执行的排查或维修建议。\n"
        prompt += "请使用专业、简洁的中文，输出格式参考 Markdown。"
        
        return prompt

    @classmethod
    def get_summary(cls, analysis_data):
        """调用公司自建模型接口"""
        if not analysis_data:
            return "无数据可供分析。"

        prompt = cls.generate_prompt(analysis_data)
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}"
        }
        
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": "你是一个专业的制造行业数据分析专家，擅长从统计数据中找出根因并给出处置建议。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 1000
        }
        
        try:
            # 如果是标准的 OpenAI 兼容接口，可以直接用 requests
            response = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            res_json = response.json()
            return res_json['choices'][0]['message']['content']
        except Exception as e:
            return f"LLM 总结生成失败：{str(e)}\n\n(提示：请检查 llm_service.py 中的 API 地址和 Key 配置)"

# 如果作为脚本运行，可进行简单测试
if __name__ == "__main__":
    # 模拟一份数据
    test_data = {
        'summary': {'total': 10000, 'fail': 200, 'fail_rate': 2.0},
        'correlation_top': [{'feature': 'Rotor_Glue_Time', 'score': 0.85, 'type': 'numerical'}],
        'hardware_attribution': [{'unit': 'Rotor_Glue #2 Nozzle', 'ng_count': 150, 'ng_rate': 75.0}],
        'boxplot_data': [{'feature': 'Glue_Amount', 'pass': [10,12,15,18,20], 'fail': [5,6,7,8,9]}]
    }
    print(LLMSummaryService.get_summary(test_data))
