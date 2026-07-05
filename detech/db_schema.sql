/*
================================================================================
  PCAP 入库相关存储过程 - SQL Server
  用途：将数据库业务逻辑集中到 SQL Server 侧，便于维护、跟踪、性能调优
  执行方式：在 pcap_db 数据库下执行一次即可（重复执行会自动跳过已存在的对象）
================================================================================
  对象清单：
    1. sp_ensure_pcap_table        确保表结构存在并维护索引（幂等）
    2. sp_check_pcap_duplicate     检查抓包文件是否已入库（按哈希/文件名）
    3. sp_delete_pcap_by_file      按文件名删除旧数据（覆盖导入场景）
    4. sp_get_pcap_stats           查询表内统计（行数/文件数）
================================================================================
*/

USE pcap_db;
GO

SET NOCOUNT ON;
GO


/* ============================================================================
   1. sp_ensure_pcap_table
      确保表结构存在，自动补齐缺失列，维护索引
      幂等执行：可重复调用，无副作用
   参数：@table_name NVARCHAR(128) - 表名，默认 'packets'
============================================================================ */
IF OBJECT_ID('dbo.sp_ensure_pcap_table', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_ensure_pcap_table;
GO

CREATE PROCEDURE dbo.sp_ensure_pcap_table
    @table_name NVARCHAR(128) = 'packets'
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql NVARCHAR(MAX);

    -- 动态拼接 DDL（表名不能参数化，必须用 QUOTENAME 防注入）
    DECLARE @tbl NVARCHAR(258) = QUOTENAME(@table_name);

    -- 1.1 若表不存在则创建
    -- 注意：OBJECT_ID 第一参数的 dbo.xxx 部分仍需 QUOTENAME，否则表名含特殊字符会注入
    -- 已移除 pcap_hash 字段：文件级查重改用 pcap_file 文件名判断，不再需要文件 MD5
    SET @sql = N'
    IF OBJECT_ID(''dbo.' + @tbl + N''', ''U'') IS NULL
    CREATE TABLE ' + @tbl + N' (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        pcap_file   VARCHAR(255),
        packet_hash VARCHAR(64),
        timestamp   DATETIME2(6),
        mac_src     VARCHAR(20),
        mac_dst     VARCHAR(20),
        ip_src      VARCHAR(45),
        ip_dst      VARCHAR(45),
        sport       INT,
        dport       INT,
        flag        VARCHAR(30),
        seq         BIGINT,
        ack         BIGINT,
        len         INT,
        summary     NVARCHAR(300),
        insert_time DATETIME2(3),
        post_url    NVARCHAR(2000),
        client_time DATETIME2(3),
        payload_hash VARCHAR(32),
        direction   VARCHAR(10),
        frame_number BIGINT
    );';
    EXEC sp_executesql @sql;

    -- 1.2 timestamp 若为旧 FLOAT 类型则转换
    SET @sql = N'
    IF EXISTS (SELECT * FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id
               WHERE c.object_id = OBJECT_ID(''dbo.' + @tbl + N''')
                 AND c.name = ''timestamp'' AND t.name != ''datetime2'')
        ALTER TABLE ' + @tbl + N' ALTER COLUMN timestamp DATETIME2(6);';
    EXEC sp_executesql @sql;

    -- 1.3 补齐缺失列（每列独立 IF NOT EXISTS）
    -- 注意：DDL 不能在单个批处理中合并多个 ALTER，逐条执行更稳
    -- 已从补齐列表中移除 pcap_hash（新表结构无此字段）
    DECLARE @col_defs TABLE (col_name NVARCHAR(64), col_def NVARCHAR(200));
    INSERT INTO @col_defs VALUES
        (N'packet_hash', N'VARCHAR(64)'),
        (N'flag',        N'VARCHAR(30)'),
        (N'seq',         N'BIGINT'),
        (N'ack',         N'BIGINT'),
        (N'len',         N'INT'),
        (N'insert_time', N'DATETIME2(3)'),
        (N'post_url',    N'NVARCHAR(2000)'),
        (N'client_time', N'DATETIME2(3)'),
        (N'payload_hash',N'VARCHAR(32)'),
        (N'direction',   N'VARCHAR(10)'),
        (N'frame_number',N'BIGINT');

    DECLARE @col_name NVARCHAR(64), @col_def NVARCHAR(200);
    DECLARE col_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT col_name, col_def FROM @col_defs;
    OPEN col_cur;
    FETCH NEXT FROM col_cur INTO @col_name, @col_def;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        -- @col_name 来自内部表变量（白名单），无需 QUOTENAME；@tbl 已 QUOTENAME 防注入
        SET @sql = N'
        IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID(''dbo.' + @tbl + N''') AND name = ''' + @col_name + N''')
            ALTER TABLE ' + @tbl + N' ADD ' + @col_name + N' ' + @col_def + N';';
        EXEC sp_executesql @sql;
        FETCH NEXT FROM col_cur INTO @col_name, @col_def;
    END
    CLOSE col_cur;
    DEALLOCATE col_cur;

    -- 1.4 维护非聚集索引（建若不存在）
    -- P2-7：索引名统一用变量 + QUOTENAME 防注入（含 IF NOT EXISTS 条件中）
    DECLARE @idx_file NVARCHAR(258) = QUOTENAME('IX_' + @table_name + N'_pcap_file');

    SET @sql = N'
    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID(''dbo.' + @tbl + N''') AND name = ''' + REPLACE('IX_' + @table_name + N'_pcap_file', '''', '''''') + N''')
        CREATE INDEX ' + @idx_file + N' ON ' + @tbl + N' (pcap_file);';
    EXEC sp_executesql @sql;

    -- 已移除 IX_*_pcap_hash 索引（pcap_hash 字段不再存在）

    -- 1.5 维护报文级唯一索引（IGNORE_DUP_KEY = ON，批量插入时静默去重）
    -- 这是报文级去重机制：同一文件内重复报文、跨文件重复报文自动跳过
    -- 过滤索引（WHERE packet_hash IS NOT NULL）避免 NULL 值参与
    DECLARE @uq_name NVARCHAR(258) = QUOTENAME('UQ_' + @table_name + N'_packet_hash');
    SET @sql = N'
    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID(''dbo.' + @tbl + N''') AND name = ''UQ_' + @table_name + N'_packet_hash'')
        CREATE UNIQUE INDEX ' + @uq_name + N' ON ' + @tbl + N' (packet_hash)
        WHERE packet_hash IS NOT NULL
        WITH (IGNORE_DUP_KEY = ON);';
    EXEC sp_executesql @sql;

    -- 1.6 维护业务相关的组合索引
    DECLARE @idx_payload_hash NVARCHAR(258) = QUOTENAME('IX_' + @table_name + N'_payload_hash');
    SET @sql = N'
    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID(''dbo.' + @tbl + N''') AND name = ''' + REPLACE('IX_' + @table_name + N'_payload_hash', '''', '''''') + N''')
        CREATE INDEX ' + @idx_payload_hash + N' ON ' + @tbl + N' (payload_hash, timestamp);';
    EXEC sp_executesql @sql;

    DECLARE @idx_client_time NVARCHAR(258) = QUOTENAME('IX_' + @table_name + N'_client_time');
    SET @sql = N'
    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID(''dbo.' + @tbl + N''') AND name = ''' + REPLACE('IX_' + @table_name + N'_client_time', '''', '''''') + N''')
        CREATE INDEX ' + @idx_client_time + N' ON ' + @tbl + N' (client_time, post_url) INCLUDE (ip_src, ip_dst, sport, dport);';
    EXEC sp_executesql @sql;
END;
GO


/* ============================================================================
   2. sp_check_pcap_duplicate
      检查抓包文件是否已入库（仅按文件名查重）
      用结果集返回（避免 pyodbc OUTPUT 参数兼容性问题）：
        status = 0  未重复，可入库
        status = 1  同名文件已存在，应覆盖（调用方需先 sp_delete_pcap_by_file）
   参数：
      @pcap_file  VARCHAR(255) - 当前文件名
   说明：
      已移除 pcap_hash 参数和文件内容 MD5 查重逻辑。
      文件级查重简化为仅按文件名判断。
============================================================================ */
IF OBJECT_ID('dbo.sp_check_pcap_duplicate', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_check_pcap_duplicate;
GO

CREATE PROCEDURE dbo.sp_check_pcap_duplicate
    @table_name     NVARCHAR(128) = 'packets',
    @pcap_file      VARCHAR(255)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @tbl NVARCHAR(258) = QUOTENAME(@table_name);
    DECLARE @sql NVARCHAR(MAX);

    -- 仅按文件名查重：存在同名记录 → status=1（应覆盖）
    SET @sql = N'
    SELECT CASE WHEN EXISTS(
        SELECT 1 FROM ' + @tbl + N' WHERE pcap_file = @pf
    ) THEN 1 ELSE 0 END AS status;';

    EXEC sp_executesql @sql, N'@pf VARCHAR(255)', @pcap_file;
END;
GO


/* ============================================================================
   3. sp_delete_pcap_by_file
      按文件名删除旧数据（覆盖导入场景调用）
   参数：@pcap_file VARCHAR(255)
   返回：@@ROWCOUNT（通过 SELECT 返回删除行数）
============================================================================ */
IF OBJECT_ID('dbo.sp_delete_pcap_by_file', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_delete_pcap_by_file;
GO

CREATE PROCEDURE dbo.sp_delete_pcap_by_file
    @table_name NVARCHAR(128) = 'packets',
    @pcap_file  VARCHAR(255)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @tbl NVARCHAR(258) = QUOTENAME(@table_name);
    DECLARE @deleted INT = 0;

    -- P1-3：直接 DELETE，通过 sp_executesql 的 OUTPUT 参数传出 @@ROWCOUNT
    -- 去掉 OUTPUT 子句和 @tmp 表变量，减少 tempdb 压力和内存占用
    DECLARE @sql NVARCHAR(MAX) = N'
    DELETE FROM ' + @tbl + N'
    WHERE pcap_file = @pf;
    SET @d = @@ROWCOUNT;';

    EXEC sp_executesql @sql,
        N'@pf VARCHAR(255), @d INT OUTPUT',
        @pcap_file, @deleted OUTPUT;

    SELECT @deleted AS deleted_count;
END;
GO


/* ============================================================================
   4. sp_get_pcap_stats
      查询表统计信息（行数、文件数、最近入库时间）
      供运维监控使用
============================================================================ */
IF OBJECT_ID('dbo.sp_get_pcap_stats', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_get_pcap_stats;
GO

CREATE PROCEDURE dbo.sp_get_pcap_stats
    @table_name NVARCHAR(128) = 'packets'
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @tbl NVARCHAR(258) = QUOTENAME(@table_name);
    DECLARE @sql NVARCHAR(MAX) = N'
    SELECT
        COUNT(*) AS total_rows,
        COUNT(DISTINCT pcap_file) AS file_count,
        MAX(insert_time) AS latest_insert_time
    FROM ' + @tbl + N' WITH (NOLOCK);';
    EXEC sp_executesql @sql;
END;
GO


/* ============================================================================
   部署完成确认
============================================================================ */
SELECT name, type_desc
FROM sys.objects
WHERE name IN ('sp_ensure_pcap_table', 'sp_check_pcap_duplicate',
               'sp_delete_pcap_by_file', 'sp_get_pcap_stats')
  AND type = 'P'
ORDER BY name;
GO

/*
================================================================================
  部署说明：
  1. 用 SSMS 或 sqlcmd 连接到 pcap_db 数据库执行本脚本
     sqlcmd -S localhost -E -d pcap_db -i db_schema.sql
  2. 脚本幂等，可重复执行（已存在的存储过程会先 DROP 再 CREATE）
  3. 部署完成后，最后一行查询应返回 4 条记录
  4. detech.py 调用 db_manager.py 时会自动检测并提示存储过程是否已部署
================================================================================
*/
