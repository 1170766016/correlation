# Excel 转 Parquet 工具使用指南

这是一个简单高效的 Python 命令行小工具 (`excel_to_parquet.py`)，专门用于将 `.xlsx` 或 `.xls` 格式的 Excel 数据文件转换为性能更优的 `.parquet` 数据格式。

## 为什么要转换为 Parquet 格式？

1. **极致的读取速度**：在 Pandas 或 Streamlit 应用中，加载 Parquet 格式通常比 Excel 甚至 CSV 快十倍以上。
2. **更小的文件体积**：Parquet 是一种列式存储格式，具有极高的压缩比，能大幅节省磁盘和内存空间。
3. **保留数据类型**：Parquet 能严格保留数据的格式类型（如时间戳、整数等），避免在读取时发生类型推断错误。

---

## 安装依赖

在运行该脚本前，请确保您的环境中已安装必要的 Python 依赖包。您可以在终端（或 VSCode 的 Terminal）中运行以下命令：

```bash
pip install pandas pyarrow openpyxl
```

*(注：如果您希望极致的 Excel 读取速度，还可以额外安装 `calamine` 库，脚本也能自动兼容：`pip install python-calamine`)*

---

## 如何使用？

请打开命令行（CMD、PowerShell 或 VSCode 终端），并确保路径处于 `excel_to_parquet.py` 所在的文件夹下。

### 1. 基础转换（最常用）

只需提供要转换的 Excel 文件路径即可。

```bash
python excel_to_parquet.py 您的数据.xlsx
```

**效果**：脚本会在 `您的数据.xlsx` 相同的目录下，自动生成一个名为 `您的数据.parquet` 的文件。

### 2. 指定自定义输出路径

如果您希望将转换后的文件保存到其他文件夹，或者重命名，可以使用 `-o` 或 `--output` 参数。

```bash
python excel_to_parquet.py data/原始数据.xlsx -o results/优化后数据.parquet
```

**效果**：原始的 Excel 数据将被转换并保存为 `results/优化后数据.parquet`。

### 3. 获取帮助

如果您忘记了命令参数，可以随时查看内置帮助：

```bash
python excel_to_parquet.py -h
```
*(这会打印出所有可用参数的说明)*

---

## 常见问题与注意事项

- **多工作表(Sheet)**：目前脚本默认只会读取 Excel 中的**第一个工作表**进行转换。
- **非表格数据**：Parquet 专注于纯数据存储，原 Excel 中的任何图表、字体颜色、合并单元格格式等**都不会**被保留。
- **文件占用**：转换时，请确保目标 Excel 文件没有被本地的 Office Excel 软件独占打开，否则可能会提示读取失败。
