import pandas as pd
import os
import time
import argparse

def convert_excel_to_csv(input_path, output_path=None):
    """
    将 Excel 文件转换为 CSV 文件，以提高分析平台的加载速度。
    """
    if not os.path.exists(input_path):
        print(f"错误: 找不到文件 {input_path}")
        return

    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + ".csv"

    print(f"正在转换: {os.path.basename(input_path)} ...")
    start_time = time.time()
    
    try:
        # 读取 Excel (所有列)
        df = pd.read_excel(input_path, engine='openpyxl')
        # 保存为 CSV (使用 utf-8-sig 以便在 Excel 中正常打开中文)
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        
        duration = time.time() - start_time
        print(f"转换成功! 保存至: {output_path}")
        print(f"耗时: {duration:.2f} 秒")
        print(f"提示: CSV 文件大小约为 Excel 的 1/3，但加载速度快 10 倍以上。")
    except Exception as e:
        print(f"转换失败: {e}")

def batch_convert(directory):
    """
    批量转换目录下所有的 Excel 文件
    """
    files = [f for f in os.listdir(directory) if f.endswith(('.xlsx', '.xls'))]
    if not files:
        print("未发现需要转换的 Excel 文件。")
        return
    
    print(f"发现 {len(files)} 个文件，准备开始批量转换...")
    for f in files:
        convert_excel_to_csv(os.path.join(directory, f))
    print("\n所有任务已完成！")

if __name__ == "__main__":
    # 使用说明：
    # 1. 转换单个文件：python excel_to_csv_converter.py "你的数据.xlsx"
    # 2. 批量转换当前目录：python excel_to_csv_converter.py .
    
    parser = argparse.ArgumentParser(description="Excel 转 CSV 快速工具")
    parser.add_argument("path", help="Excel 文件路径 或 文件夹路径")
    args = parser.parse_args()

    if os.path.isdir(args.path):
        batch_convert(args.path)
    else:
        convert_excel_to_csv(args.path)
