/*
================================================================================
  PCAP 报文表初始化脚本 - 纯 DDL（建表 + 索引）
  用途：
    1. 全新部署时单独执行本脚本即可建表，不需要先部署存储过程
    2. 作为表结构的"权威文档"，方便 DBA 直接查看/修改
    3. 存储过程 sp_ensure_pcap_table 内部也实现了相同的建表逻辑（运行时自动维护）
       本脚本是"显式版"，存储过程是"运行时自动版"，两者结构保持同步

  执行方式：
    sqlcmd -S localhost -E -d pcap_db -i db_init.sql

  注意：
    - 若表已存在，本脚本不会重建（IF OBJECT_ID IS NULL 保护）
    - 若需重建表，先 DROP TABLE packets; 再执行本脚本
    - 修改表结构请同步更新本文件 + sp_ensure_pcap_table 中的 DDL
================================================================================
*/

USE pcap_db;
GO

SET NOCOUNT ON;
GO


/* ============================================================================
   1. 建表 - packets
      存储所有抓包文件的报文数据
      字段说明：
        - pcap_file: 源文件名，用于文件级查重（代码端判断）
        - packet_hash: 报文级唯一哈希，建有 IGNORE_DUP_KEY 唯一索引做逐包去重
        - 已移除 pcap_hash 字段（原文件级 MD5，改用文件名查重更简单）
============================================================================ */
IF OBJECT_ID('dbo.packets', 'U') IS NOT NULL
BEGIN
    PRINT N'表 dbo.packets 已存在，跳过建表。如需重建请先 DROP TABLE packets;';
END
ELSE
BEGIN
    CREATE TABLE dbo.packets (
        id          INT IDENTITY(1,1) PRIMARY KEY,           -- 自增主键
        pcap_file   VARCHAR(255)  NULL,                     -- 源抓包文件名（用于文件级查重）
        packet_hash VARCHAR(64)   NULL,                     -- 单条报文 MD5（报文级去重）
        timestamp   DATETIME2(6)  NULL,                     -- 报文时间戳（微秒精度）
        mac_src     VARCHAR(20)   NULL,                     -- 源 MAC
        mac_dst     VARCHAR(20)   NULL,                     -- 目的 MAC
        ip_src      VARCHAR(45)   NULL,                     -- 源 IP（IPv4/IPv6 通用长度）
        ip_dst      VARCHAR(45)   NULL,                     -- 目的 IP
        sport       INT           NULL,                     -- 源端口
        dport       INT           NULL,                     -- 目的端口
        flag        VARCHAR(30)   NULL,                     -- TCP 标志位（SYN ACK FIN RST PSH URG）
        seq         BIGINT        NULL,                     -- TCP 序列号
        ack         BIGINT        NULL,                     -- TCP 确认号
        len         INT           NULL,                     -- TCP 载荷长度
        summary     NVARCHAR(300) NULL,                     -- 报文摘要（人类可读）
        insert_time DATETIME2(3)  NULL,                     -- 入库时间
        post_url    NVARCHAR(2000) NULL,                    -- HTTP POST 请求路径
        client_time DATETIME2(3)  NULL,                     -- 客户端发送时间（从 JSON 载荷提取）
        payload_hash VARCHAR(32)  NULL,                     -- HTTP 载荷 MD5 指纹（用于链路跨段关联）
        direction   VARCHAR(10)   NULL,                     -- 流量方向（如 REQ, RES）
        frame_number BIGINT       NULL                      -- PCAP 报文序号
    );

    PRINT N'表 dbo.packets 创建成功';
END;
GO


/* ============================================================================
   2. 索引 - 非聚集索引
      IX_packets_pcap_file 用于文件级查重和按文件统计
============================================================================ */

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.packets') AND name = 'IX_packets_pcap_file')
BEGIN
    CREATE INDEX IX_packets_pcap_file ON dbo.packets (pcap_file);
    PRINT N'索引 IX_packets_pcap_file 创建成功';
END
ELSE
BEGIN
    PRINT N'索引 IX_packets_pcap_file 已存在，跳过';
END
GO

/* ============================================================================
   2.1 索引 - 业务查询与关联索引
============================================================================ */
IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.packets') AND name = 'IX_packets_payload_hash')
BEGIN
    CREATE INDEX IX_packets_payload_hash ON dbo.packets (payload_hash, timestamp);
    PRINT N'索引 IX_packets_payload_hash 创建成功';
END
GO

IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.packets') AND name = 'IX_packets_client_time')
BEGIN
    CREATE INDEX IX_packets_client_time ON dbo.packets (client_time, post_url) INCLUDE (ip_src, ip_dst, sport, dport);
    PRINT N'索引 IX_packets_client_time 创建成功';
END
GO


/* ============================================================================
   3. 报文级唯一索引 - packet_hash 去重
      开启 IGNORE_DUP_KEY，批量插入时遇到重复 hash 静默跳过（不报错）
      过滤索引（WHERE packet_hash IS NOT NULL）避免 NULL 值参与索引
      这是报文级去重机制：防止同一文件内重复报文、跨文件重复报文

      注意：此索引会让批量插入慢 10-20%（每行需检查唯一性），
      但保证了报文级去重，是业务必需的功能。
      IGNORE_DUP_KEY = ON 确保重复行静默跳过，不报错不中断。
============================================================================ */
IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.packets') AND name = 'UQ_packets_packet_hash')
BEGIN
    CREATE UNIQUE INDEX UQ_packets_packet_hash
    ON dbo.packets (packet_hash)
    WHERE packet_hash IS NOT NULL
    WITH (IGNORE_DUP_KEY = ON);
    PRINT N'唯一索引 UQ_packets_packet_hash 创建成功（IGNORE_DUP_KEY = ON，报文级去重）';
END
ELSE
BEGIN
    PRINT N'唯一索引 UQ_packets_packet_hash 已存在，跳过';
END
GO


/* ============================================================================
   4. 验证 - 列出表结构和索引
============================================================================ */
PRINT N'';
PRINT N'=== 表结构 ===';
SELECT
    c.name AS column_name,
    t.name AS type_name,
    c.max_length,
    c.is_nullable,
    c.is_identity
FROM sys.columns c
JOIN sys.types t ON c.user_type_id = t.user_type_id
WHERE c.object_id = OBJECT_ID('dbo.packets')
ORDER BY c.column_id;

PRINT N'';
PRINT N'=== 索引清单 ===';
SELECT
    i.name AS index_name,
    i.type_desc,
    i.is_unique,
    STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS columns
FROM sys.indexes i
LEFT JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
LEFT JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
WHERE i.object_id = OBJECT_ID('dbo.packets')
GROUP BY i.name, i.type_desc, i.is_unique
ORDER BY i.name;
GO


/*
================================================================================
  字段说明（运维参考）：

  id          自增主键，无业务含义，仅作行唯一标识
  pcap_file   源 .pcap/.pcapng 文件名（不含路径），用于按文件分组查询
              代码端按文件名做文件级查重，无 pcap_hash 字段
  packet_hash 单条报文 MD5（时间戳+MAC+IP+端口+flag+seq+ack+len 拼接计算）
              用于报文级去重：建有唯一索引 UQ_packets_packet_hash（IGNORE_DUP_KEY=ON），
              批量插入时重复报文自动静默跳过
  timestamp   报文抓取时间，DATETIME2(6) 支持微秒精度（原始 pcap 是纳秒，截断到微秒）
  mac_src/dst MAC 地址，格式 aa:bb:cc:dd:ee:ff
  ip_src/dst  IPv4 或 IPv6 地址，VARCHAR(45) 覆盖 IPv6 最长形式
  sport/dport 源/目的端口，INT（0-65535）
  flag        TCP 标志位字符串，如 "SYN ACK"、"FIN ACK"、"RST"
              空值表示非 TCP 报文（UDP/ICMP）
  seq/ack     TCP 序列号/确认号，BIGINT（TCP 序列号可能超过 INT 范围）
  len         TCP 载荷字节数，由 IP.len - IP头 - TCP头 计算得出
  summary     报文可读摘要，含时间/MAC/IP/端口/Info，NVARCHAR(300) 应对中文
  insert_time 入库时间戳，用于排查入库顺序问题
  post_url    HTTP POST 请求路径，业务事件类型
  client_time 客户端发送时间戳，DATETIME2(3) 精确到毫秒，从 200KB 的 JSON 载荷(sendTime) 中提取
  payload_hash 载荷 MD5 指纹，VARCHAR(32)，用于跨代理服务器的前后段链路关联
  direction   流量方向
  frame_number 报文在原 PCAP 文件中的偏移/序号，用于界面按需读取载荷详情

  索引说明：
  IX_packets_pcap_file       按文件名查询，覆盖导入/按文件统计场景
  IX_packets_payload_hash    按哈希和时间查询，用于 200 毫秒时间窗口的跨代理解析自关联
  IX_packets_client_time     按客户端时间和 URL 查询，支持 IP/端口 的 INCLUDE (业务侧快速排查)
  UQ_packets_packet_hash     报文级唯一索引，IGNORE_DUP_KEY=ON 静默去重
  （已移除 IX_packets_pcap_hash，因为不再有 pcap_hash 字段）
================================================================================
*/
