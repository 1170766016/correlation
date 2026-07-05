# 跨代理 HTTP 流量关联分析工具 (Cross-Proxy Traffic Correlation Tool)

## 项目简介
本项目是一个专门用于工业/产线网络环境下，解决**跨代理服务器（负载均衡）**流量溯源与分析的工具。
在拥有过万客户端并发请求后端 MES 系统的架构中，传统的基于 TCP 四元组的追踪方式会因为代理服务器的 NAT（IP 和端口转换）而失效。本项目通过创新的**“载荷哈希指纹匹配”**和**“动静分离存储”**架构，实现了无视 NAT 转换的精准链路串联。

## 核心特性
* **无视 NAT 转换的链路精准匹配**：对 HTTP POST 请求的主体（Body）计算 MD5 指纹，配合 200ms 极短时间窗口，精确找出“前端请求”和“后端请求”的因果关系，从而定位代理丢包和网络延迟。
* **动静分离应对海量抓包**：面对每日高达 200GB+ 的 PCAP 抓包文件，程序拒绝将数百 KB 的 `filedata` JSON 直接存入数据库。相反，仅将提取出的业务特征（如 `post_url`, `client_time`, `payload_hash`）作为元数据入库，将数据库容量消耗降低了 99% 以上。
* **高精度时间解析**：原生支持从海量 JSON 载荷中提取带有 2 位小数的 `sendTime` 客户端日志时间，并精准转化为 SQL Server 的 `DATETIME2(3)`（毫秒级）。
* **UI 可视化操作**：提供了便捷的 Tkinter 图形界面，支持一键载入抓包文件、应用 BPF 过滤、批量极速入库以及链路自关联报表展示。

## 目录结构
```text
correlation/
│
├── detech/
│   ├── detech.py               # 核心主程序：UI 界面、PCAP 解析、MD5 指纹计算
│   ├── db_manager.py           # 数据库操作引擎：负责高并发批量插入 (fast_executemany) 及关联查询
│   ├── db_schema.sql           # SQL 核心脚本：包含建表及智能升级的存储过程 (推荐使用)
│   ├── db_init.sql             # SQL 建表参考：纯 DDL 语句，供 DBA 审查使用的静态版本
│   └── db_config.json          # 数据库连接配置文件 (自动生成)
│
├── cross_proxy_requirements.md # 架构设计与详细业务需求文档
└── README.md                   # 本说明文件
```

## 环境依赖
* **Python**: Python 3.8 或以上版本。
* **抓包工具**: 服务器需安装 [Wireshark](https://www.wireshark.org/) / `tshark`（用于抓包及后续的高性能解析升级）。
* **数据库**: Microsoft SQL Server (2016 或以上版本支持 `DATETIME2` 和 JSON 索引特性最佳)。
* **Python 依赖包**:
  ```bash
  pip install scapy pyodbc
  ```
  *(注：根据 Windows 系统，您可能需要安装对应版本的 Microsoft ODBC Driver for SQL Server)*

## 快速开始

1. **数据库初始化**
   - 打开 SQL Server Management Studio (SSMS)。
   - 连接到目标数据库实例，执行 `detech/db_schema.sql` 脚本。该脚本会自动创建 `packets` 表、必要的联合索引以及自动升级存储过程。

2. **配置数据库连接**
   - 运行程序前，您可以直接在项目目录下创建 `db_config.json`，或在运行程序后通过界面上的【数据库设置】按钮进行可视化配置。

3. **运行主程序**
   - 进入 `detech` 目录，执行：
     ```bash
     python detech.py
     ```
   - 在弹出的窗口中，选择您抓取的 `.pcap` 文件。
   - 输入过滤规则（或保留默认），点击【过滤】。
   - 待过滤完毕后，点击【保存到数据库】，程序会自动解析 `sendTime` 并计算 MD5 指纹入库。
   - 入库完成后，点击【分析跨代理链路】，即可在界面下方看到经过配对的完整前后段网络链路和代理延迟。

## 架构与技术文档
关于系统应对 200GB+ 级抓包文件的完整设计思考、以及对于“前端/后端”视角的请求定义，请参考：
👉 [cross_proxy_requirements.md](./cross_proxy_requirements.md)
