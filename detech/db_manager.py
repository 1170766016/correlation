"""
db_manager.py - PCAP 入库数据库操作封装

将原本散落在 detech.py 中的数据库操作集中到本模块，便于维护和跟踪。
所有 DDL、查重、删除逻辑通过 SQL Server 存储过程实现（见 db_schema.sql），
批量插入仍用 executemany + INSERT VALUES 以保持 fast_executemany 性能。

依赖：
    - pyodbc
    - db_schema.sql 已在目标数据库执行过（创建 4 个存储过程）

使用示例：
    from db_manager import PcapDbManager
    mgr = PcapDbManager(cfg)
    mgr.connect()
    mgr.ensure_schema("packets")
    status = mgr.check_duplicate("packets", file_name)
    if status == 1:
        print(f"同名文件已存在，将覆盖: {file_name}")
        mgr.delete_by_file("packets", file_name)
    mgr.insert_batch("packets", rows)
    mgr.close()
"""

from __future__ import annotations

import datetime
import os
from typing import Any, Dict, Optional

try:
    import pyodbc
except ImportError:
    pyodbc = None  # type: ignore


class PcapDbManager:
    """PCAP 数据库操作管理器

    封装 4 个核心操作：
      1. ensure_schema       - 调用 sp_ensure_pcap_table 维护表结构
      2. check_duplicate     - 调用 sp_check_pcap_duplicate 查重
      3. delete_by_file      - 调用 sp_delete_pcap_by_file 覆盖旧数据
      4. insert_batch        - 批量插入（保留 executemany 直 INSERT，性能最优）

    批量插入未走存储过程的原因：
      pyodbc fast_executemany=True 时，INSERT VALUES 是一次 RPC 发送整批参数，
      而存储过程 + executemany 会变成每行 1 次 RPC，N 次网络往返，性能下降 5-10 倍。
      业务逻辑（DDL/查重/删除）放存储过程，性能敏感的批量插入保留 Python 直发。
    """

    # SQL Server 单语句参数硬上限
    PARAM_LIMIT = 2100
    # INSERT 语句的列数（与表结构一一对应，修改时务必同步）
    # 新增 5 个字段：post_url, client_time, payload_hash, direction, frame_number，列数增加到 20
    NUM_COLUMNS = 20
    # 默认批量大小（依赖 fast_executemany）
    DEFAULT_BATCH_SIZE = 5000
    # 索引碎片率阈值：超过此值建议重建
    INDEX_REBUILD_THRESHOLD = 30.0
    # 索引重组阈值：5%-30% 之间建议重组
    INDEX_REORG_THRESHOLD = 5.0

    def __init__(self, cfg: Dict[str, Any]) -> None:
        """初始化数据库连接配置

        cfg 字段：
            server, port, database, username, password, driver, table_name
        """
        if pyodbc is None:
            raise ImportError(
                "未找到 pyodbc 模块，请安装：pip install pyodbc"
            )
        self.cfg = cfg
        self.conn: Optional[pyodbc.Connection] = None
        self.cursor: Optional[pyodbc.Cursor] = None
        self._schema_checked = False  # 同连接内表结构是否已验证

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """建立数据库连接，autocommit=False 以启用显式事务"""
        server = self.cfg.get("server", "localhost")
        port = self.cfg.get("port", "1433")
        database = self.cfg.get("database", "pcap_db")
        username = self.cfg.get("username", "")
        password = self.cfg.get("password", "")
        driver = self.cfg.get("driver", "ODBC Driver 17 for SQL Server")

        if not server or not database:
            raise ValueError("数据库服务器或数据库名称未配置")

        if username:
            conn_str = (
                f"DRIVER={{{driver}}};SERVER={server},{port};"
                f"DATABASE={database};UID={username};PWD={password}"
            )
        else:
            conn_str = (
                f"DRIVER={{{driver}}};SERVER={server},{port};"
                f"DATABASE={database};Trusted_Connection=yes"
            )

        # 直接以 autocommit=False 建立连接，确保事务边界正确
        self.conn = pyodbc.connect(conn_str, autocommit=False)
        self.cursor = self.conn.cursor()
        # fast_executemany 提速关键：批量 INSERT 按行 RPC，单次网络往返
        try:
            self.cursor.fast_executemany = True
        except Exception:
            pass

    def close(self) -> None:
        """关闭连接，忽略关闭过程中的异常"""
        if self.cursor:
            try:
                self.cursor.close()
            except Exception:
                pass
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.cursor = None

    def commit(self) -> None:
        if self.conn:
            self.conn.commit()

    def rollback(self) -> None:
        if self.conn:
            try:
                self.conn.rollback()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 业务操作（走存储过程）
    # ------------------------------------------------------------------

    def ensure_schema(self, table_name: str = "packets") -> None:
        """调用 sp_ensure_pcap_table 维护表结构（建表/补列/索引）

        同连接内只执行一次，后续调用直接返回（_schema_checked 标志）
        存储过程未部署时回退到内联 SQL（最小建表 + 索引）
        """
        if self._schema_checked:
            return
        if not self.cursor or not self.conn:
            raise RuntimeError("未建立数据库连接")

        try:
            sql = "{CALL sp_ensure_pcap_table(?)}"
            self.cursor.execute(sql, table_name)
            # 存储过程内部用了多个动态 SQL，可能产生多个结果集，全部消费掉
            while self.cursor.nextset():
                pass
            self.conn.commit()
            self._schema_checked = True
        except Exception:
            # 清理未消费结果集
            try:
                while self.cursor.nextset():
                    pass
            except Exception:
                pass
            # 兜底：内联最小 DDL（建表 + 索引，不做 ALTER 兼容）
            # 完整 DDL 见 db_init.sql，建议用户预先部署
            self.cursor.execute(f"""
            IF OBJECT_ID('dbo.{table_name}', 'U') IS NULL
            CREATE TABLE [{table_name}] (
                id INT IDENTITY(1,1) PRIMARY KEY,
                pcap_file VARCHAR(255), packet_hash VARCHAR(64),
                timestamp DATETIME2(6), mac_src VARCHAR(20), mac_dst VARCHAR(20),
                ip_src VARCHAR(45), ip_dst VARCHAR(45), sport INT, dport INT,
                flag VARCHAR(30), seq BIGINT, ack BIGINT, len INT,
                summary NVARCHAR(300), insert_time DATETIME2(3),
                post_url NVARCHAR(2000), client_time DATETIME2(3),
                payload_hash VARCHAR(32), direction VARCHAR(10), frame_number BIGINT
            )
            """)
            self.cursor.execute(f"""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.{table_name}') AND name = 'IX_{table_name}_pcap_file')
                CREATE INDEX [IX_{table_name}_pcap_file] ON [{table_name}] (pcap_file)
            """)
            # 报文级唯一索引（IGNORE_DUP_KEY = ON，批量插入时静默去重）
            self.cursor.execute(f"""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.{table_name}') AND name = 'UQ_{table_name}_packet_hash')
                CREATE UNIQUE INDEX [UQ_{table_name}_packet_hash] ON [{table_name}] (packet_hash)
                WHERE packet_hash IS NOT NULL
                WITH (IGNORE_DUP_KEY = ON)
            """)
            self.conn.commit()
            self._schema_checked = True

    def check_duplicate(
        self,
        table_name: str,
        pcap_file: str,
    ) -> int:
        """调用 sp_check_pcap_duplicate 查重（仅按文件名）

        返回 status:
            0  未重复，可入库
            1  同名文件已存在，应覆盖（调用方需先 delete_by_file）

        实现说明：
            用户决定文件级去重仅按文件名判断，不再用 pcap_hash。
            简化逻辑：只查 pcap_file 是否已存在。
            存储过程未部署时回退到内联 SQL。
        """
        if not self.cursor:
            raise RuntimeError("未建立数据库连接")

        try:
            sql = "{CALL sp_check_pcap_duplicate(?, ?)}"
            self.cursor.execute(sql, table_name, pcap_file)
            row = self.cursor.fetchone()
            if row is None:
                return 0
            return int(row[0])
        except Exception:
            try:
                while self.cursor.nextset():
                    pass
            except Exception:
                pass
            return self._check_duplicate_fallback(table_name, pcap_file)

    def _check_duplicate_fallback(
        self,
        table_name: str,
        pcap_file: str,
    ) -> int:
        """查重的兼容实现：按文件名查重（存储过程未部署时启用）"""
        if not self.cursor:
            raise RuntimeError("未建立数据库连接")

        self.cursor.execute(
            f"SELECT TOP 1 1 FROM [{table_name}] WHERE pcap_file = ?",
            (pcap_file,),
        )
        row = self.cursor.fetchone()
        return 1 if row else 0

    def delete_by_file(self, table_name: str, pcap_file: str) -> int:
        """调用 sp_delete_pcap_by_file 删除指定文件的所有记录

        返回删除行数
        存储过程未部署时回退到内联 SQL
        """
        if not self.cursor:
            raise RuntimeError("未建立数据库连接")
        try:
            sql = "{CALL sp_delete_pcap_by_file(?, ?)}"
            self.cursor.execute(sql, table_name, pcap_file)
            row = self.cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            # 清理未消费结果集，避免污染后续查询
            try:
                while self.cursor.nextset():
                    pass
            except Exception:
                pass
            # 兜底：内联 DELETE，rowcount 取删除行数
            self.cursor.execute(
                f"DELETE FROM [{table_name}] WHERE pcap_file = ?",
                (pcap_file,),
            )
            return self.cursor.rowcount

    # ------------------------------------------------------------------
    # 业务查询逻辑
    # ------------------------------------------------------------------

    def analyze_cross_proxy_links(self, table_name: str) -> list[tuple]:
        """跨代理链路关联分析
        
        基于 payload_hash 自关联，时间窗口 <= 200 毫秒。
        由于我们提取了 HTTP POST 且打上了 Hash 指纹，可无视 IP 转换精确配对。
        返回 (client_ip, client_request_time, web_server_ip, proxy_forward_time, proxy_latency, post_url)
        """
        if not self.cursor:
            raise RuntimeError("未建立数据库连接")
            
        sql = f"""
        SELECT 
            inbound.ip_src AS client_ip,
            inbound.timestamp AS client_request_time,
            outbound.ip_dst AS web_server_ip,
            outbound.timestamp AS proxy_forward_time,
            DATEDIFF(millisecond, inbound.timestamp, outbound.timestamp) AS proxy_latency,
            inbound.post_url
        FROM [{table_name}] inbound
        INNER JOIN [{table_name}] outbound 
            ON inbound.payload_hash = outbound.payload_hash
            AND inbound.payload_hash IS NOT NULL
            AND outbound.timestamp >= inbound.timestamp
            AND outbound.timestamp <= DATEADD(millisecond, 200, inbound.timestamp)
        WHERE 
            -- 这里可以通过 IP 段来精确区分 inbound 和 outbound，为了演示泛用性，利用时间先后
            inbound.id != outbound.id
        ORDER BY inbound.timestamp
        """
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall()
            return [tuple(r) for r in rows]
        except Exception as e:
            # 捕获异常，避免崩溃
            raise RuntimeError(f"链路关联查询失败: {str(e)}")

    # ------------------------------------------------------------------
    # 批量插入（性能敏感，不走存储过程）
    # ------------------------------------------------------------------

    # 分段提交：每 N 批 commit 一次，降低锁持有时间
    COMMIT_EVERY_N_BATCHES = 5  # 每 5 批 commit 一次（25000 行 @ 5000/批）

    def insert_batch(
        self,
        table_name: str,
        rows: list[tuple],
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_cb=None,
        cancel_check=None,
        commit_every_n_batches: int = COMMIT_EVERY_N_BATCHES,
    ) -> int:
        """批量插入报文行（分段提交，降低锁持有时间）

        参数：
            table_name              - 目标表名
            rows                    - 行数据列表，每行 16 列元组
            batch_size              - 每批大小，默认 5000
            progress_cb             - 进度回调 fn(inserted, total) -> None
            cancel_check            - 取消检查 fn() -> bool，返回 True 则中断
            commit_every_n_batches  - 每 N 批 commit 一次，默认 5（25000 行）
                                      设为 0 或负数则不自动 commit（调用方负责）

        返回：
            成功插入的行数。取消时返回已提交的行数（已 commit 的部分保留，
            当前未提交段回滚）。

        异常处理：
            - 若 fast_executemany 不可用导致 2100 参数超限，自动切到 131 行/批重试
            - 异常时回滚当前未提交段，已 commit 的段不受影响
            - 异常向上抛出，调用方可据此决定后续处理

        事务边界：
            本方法在每 N 批后自动 commit。最后不满一段的数据在方法返回前
            也会 commit。调用方通常不需要再额外 commit。
        """
        if not self.cursor or not self.conn:
            raise RuntimeError("未建立数据库连接")
        if not rows:
            return 0

        sql = (
            f"INSERT INTO [{table_name}] "
            f"(pcap_file, packet_hash, timestamp, mac_src, mac_dst, "
            f" ip_src, ip_dst, sport, dport, flag, seq, ack, len, summary, insert_time, "
            f" post_url, client_time, payload_hash, direction, frame_number) "
            f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )

        # fast_executemany 不可用时的安全批量大小
        safe_batch = self.PARAM_LIMIT // self.NUM_COLUMNS  # = 131
        effective_batch = batch_size
        do_segment_commit = commit_every_n_batches > 0

        total = len(rows)
        inserted = 0
        committed = 0  # 已 commit 的行数
        batch_idx = 0
        segment_batch_count = 0  # 当前段内已执行的批次数
        batch_start = 0

        while batch_start < total:
            # 取消检查：回滚当前未提交段，保留已 commit 的数据
            if cancel_check and cancel_check():
                self.rollback()
                return committed

            batch_end = min(batch_start + effective_batch, total)
            chunk = rows[batch_start:batch_end]

            try:
                self.cursor.executemany(sql, chunk)
            except Exception as exec_err:
                err_str = str(exec_err)
                # 2100 参数超限兜底：切到安全批量重试本批
                if (
                    ("8003" in err_str or "exceed" in err_str.lower()
                     or "too many" in err_str.lower())
                    and effective_batch > safe_batch
                ):
                    effective_batch = safe_batch
                    batch_end = min(batch_start + effective_batch, total)
                    chunk = rows[batch_start:batch_end]
                    self.cursor.executemany(sql, chunk)
                else:
                    raise

            inserted += len(chunk)
            batch_idx += 1
            segment_batch_count += 1
            batch_start = batch_end

            # 分段提交：每 N 批 commit 一次，释放锁
            if do_segment_commit and segment_batch_count >= commit_every_n_batches:
                self.conn.commit()
                committed = inserted
                segment_batch_count = 0

            # 进度回调
            if progress_cb and (batch_idx % 3 == 0 or batch_start >= total):
                progress_cb(inserted, total)

        # 提交最后不满一段的数据
        if do_segment_commit and inserted > committed:
            self.conn.commit()
            committed = inserted

        return inserted

    # ------------------------------------------------------------------
    # 静态工具方法
    # ------------------------------------------------------------------

    @classmethod
    def is_schema_deployed(cls, conn) -> bool:
        """检测目标数据库是否已部署 db_schema.sql 中的存储过程

        供调用方在启动时做前置检查。
        注意：传入的 conn 应该是独立的或当前 cursor 已无未消费结果集的连接，
        本方法会创建临时 cursor 查询，不影响主 cursor 状态。
        """
        cur = None
        try:
            # 用独立 cursor 查询，避免污染主 cursor 的结果集状态
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM sys.objects WHERE name = 'sp_ensure_pcap_table' AND type = 'P'"
            )
            row = cur.fetchone()
            return bool(row and row[0] > 0)
        except Exception:
            return False
        finally:
            # 确保异常路径也释放 cursor，避免连接游标泄漏
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 索引维护（碎片检测 + 重建/重组）
    # ------------------------------------------------------------------

    def get_index_fragmentation(self, table_name: str = "packets") -> list[dict]:
        """查询指定表所有索引的碎片情况

        返回每个索引的字典列表：
            [{name, fragmentation_pct, page_count, size_mb, recommend}, ...]
        recommend 取值：'rebuild' / 'reorganize' / 'ok'

        实现：直接调用 sys.dm_db_index_physical_stats DMV
        比 EXEC sp_ensure_pcap_table 跑动态 SQL 更稳，无 SP 依赖
        """
        if not self.cursor:
            raise RuntimeError("未建立数据库连接")

        # SAMPLED 模式：抽样扫描，对大表友好（< 1000 页时自动用 DETAILED）
        self.cursor.execute("""
            SELECT
                i.name AS index_name,
                ips.avg_fragmentation_in_percent,
                ips.page_count,
                ips.page_count * 8 / 1024.0 AS size_mb
            FROM sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID(?), NULL, NULL, 'SAMPLED') ips
            JOIN sys.indexes i ON ips.object_id = i.object_id AND ips.index_id = i.index_id
            WHERE ips.index_id > 0  -- 排除堆(0)，只看真正的索引
              AND i.name IS NOT NULL
            ORDER BY ips.avg_fragmentation_in_percent DESC
        """, (f"dbo.{table_name}",))

        result = []
        for row in self.cursor.fetchall():
            name = row[0]
            frag = float(row[1]) if row[1] is not None else 0.0
            pages = int(row[2]) if row[2] is not None else 0
            size_mb = float(row[3]) if row[3] is not None else 0.0

            if frag >= self.INDEX_REBUILD_THRESHOLD:
                recommend = "rebuild"
            elif frag >= self.INDEX_REORG_THRESHOLD:
                recommend = "reorganize"
            else:
                recommend = "ok"

            result.append({
                "name": name,
                "fragmentation_pct": round(frag, 2),
                "page_count": pages,
                "size_mb": round(size_mb, 2),
                "recommend": recommend,
            })

        # 清理结果集
        try:
            while self.cursor.nextset():
                pass
        except Exception:
            pass

        return result

    def rebuild_index(self, table_name: str, index_name: str, online: bool = True) -> None:
        """重建指定索引（重量级操作，会消耗 CPU/IO，可能锁表）

        参数：
            online=True  使用 ONLINE 选项，重建期间表可读写（需 Enterprise 版）
            online=False 锁表重建，速度快但阻塞写入（Standard 版或大表推荐）

        注意：重建期间会占用大量 tempdb 空间（约索引大小的 1.5 倍）
        """
        if not self.cursor or not self.conn:
            raise RuntimeError("未建立数据库连接")

        # QUOTENAME 防注入
        tbl = f"[{table_name}]"  # 简化版，table_name 来自配置非用户输入
        idx = f"[{index_name}]"

        if online:
            sql = f"ALTER INDEX {idx} ON {tbl} REBUILD WITH (ONLINE = ON, SORT_IN_TEMPDB = ON)"
        else:
            sql = f"ALTER INDEX {idx} ON {tbl} REBUILD WITH (SORT_IN_TEMPDB = ON)"

        self.cursor.execute(sql)
        self.conn.commit()

    def reorganize_index(self, table_name: str, index_name: str) -> None:
        """重组指定索引（轻量级，不锁表，适合碎片率 5%-30%）

        重组是逻辑排序，不重建 B+ 树，资源占用低
        """
        if not self.cursor or not self.conn:
            raise RuntimeError("未建立数据库连接")

        tbl = f"[{table_name}]"
        idx = f"[{index_name}]"
        sql = f"ALTER INDEX {idx} ON {tbl} REORGANIZE"

        self.cursor.execute(sql)
        self.conn.commit()

    def needs_maintenance(self, table_name: str = "packets") -> tuple[bool, list[dict]]:
        """快速检查是否需要维护

        返回 (need_maintain, frag_info)
        need_maintain=True 表示至少一个索引需要重组或重建
        """
        frag_info = self.get_index_fragmentation(table_name)
        need = any(item["recommend"] in ("rebuild", "reorganize") for item in frag_info)
        return need, frag_info

