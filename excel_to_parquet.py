import pandas as pd
import argparse
import os

def convert_excel_to_parquet(excel_path, parquet_path=None):
    """
    将 Excel 文件转换为 Parquet 格式
    """
    if not os.path.exists(excel_path):
        print(f"错误: 找不到文件 - {excel_path}")
        return

    if parquet_path is None:
        base_name = os.path.splitext(excel_path)[0]
        parquet_path = f"{base_name}.parquet"

    print(f"正在读取 Excel 文件: {excel_path} ...")
    try:
        # 依赖于 openpyxl 或 calamine 等引擎，如果遇到特定格式问题可以指定 engine
        df = pd.read_excel(excel_path)
        print(f"成功读取 {len(df)} 行数据。正在转换为 Parquet 格式...")
        
        # 使用 pyarrow 引擎保存，通常速度更快且压缩更好
        df.to_parquet(parquet_path, engine='pyarrow', index=False)
        print(f"转换成功！已保存至: {parquet_path}")
        
    except Exception as e:
        print(f"转换过程中发生错误: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将 Excel 文件转换为 Parquet 格式的小工具")
    parser.add_argument("input", help="输入的 Excel 文件路径 (例如: data.xlsx)")
    parser.add_argument("-o", "--output", help="输出的 Parquet 文件路径 (可选，默认在同目录下生成同名 .parquet 文件)", default=None)
    
    args = parser.parse_args()
    convert_excel_to_parquet(args.input, args.output)
