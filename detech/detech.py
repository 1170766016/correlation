import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from scapy.all import wrpcap, sniff
from scapy.utils import PcapReader
from scapy.plist import PacketList
from scapy.layers.inet import IP, TCP, UDP, ICMP
from scapy.layers.inet6 import IPv6
from scapy.layers.l2 import Ether
try:
    from scapy.layers.http import HTTP as HTTP_LAYER
    from scapy.packet import bind_layers
    _HTTP_AVAILABLE = True
    # 扩展 HTTP 解码端口绑定：默认 Scapy 仅绑定 80，此处补上常见明文 HTTP 端口。
    # 注意：443 是 HTTPS（TLS 加密），不在此绑定，避免干扰 TLS 流量分析。
    for _p in (80, 8080, 8000, 8008, 8888):
        try:
            bind_layers(TCP, HTTP_LAYER, dport=_p)
            bind_layers(TCP, HTTP_LAYER, sport=_p)
        except Exception:
            pass
except ImportError:
    HTTP_LAYER = None
    _HTTP_AVAILABLE = False
import re
import os
import json
try:
    import orjson
except ImportError:
    orjson = None
import datetime
import sqlite3
try:
    import pyodbc
except ImportError:
    pyodbc = None
import binascii
import hashlib
import urllib.request
import urllib.error
import pathlib
import threading
import time
from collections import OrderedDict, Counter
from enum import Enum
from typing import Optional, Dict, Any

# 数据库操作统一委托给 db_manager 模块（存储过程化，便于维护跟踪）
# 兼容两种运行方式：作为包内模块导入 / 作为脚本直接运行
try:
    from .db_manager import PcapDbManager  # 包内导入（from detech import ...）
except ImportError:
    from db_manager import PcapDbManager   # 直接运行 python detech.py


class WinPcapFilter:
    # ---- 常量定义 ----
    UI_UPDATE_INTERVAL = 100        # 每N条报文刷新一次 UI
    DB_BATCH_SIZE = 5000            # 数据库批量插入大小（配合 fast_executemany，越大往返越少）
    DB_PROGRESS_EVERY = 3           # 每完成 N 个批次才刷新一次进度条，降低主线程负担
    DB_PARAM_LIMIT = 2100           # SQL Server 单语句参数硬上限
    DB_NUM_COLUMNS = 15             # INSERT 语句的列数（占位符个数，已移除 pcap_hash）
    RAW_HEX_MAX_BYTES = 2048        # raw_hex 最大存储字节数
    DISPLAY_PAGE_SIZE = 2000        # 显示区最大报文数量（避免 ScrolledText 过慢）
    FILTER_MAX_PACKETS = 500000     # do_filter 最大保留报文数量（防止内存溢出）

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Wireshark抓包过滤工具")
        self.root.geometry("850x680")
        self.pcap_paths: list = []
        self.all_pkts: list = []       # 原始所有报文
        self.all_pkts_count: int = 0   # 原始报文总数
        self.filtered_pkts: list = []  # 过滤后报文
        self._busy: bool = False       # 防止重复提交
        self._db_schema_checked = False  # 同进程内表结构是否已验证过，避免每次入库重复跑 DDL 检查
        self._cancel_event = threading.Event()  # 任务取消标志，后台线程定期检查
        self.init_ui()

    def init_ui(self) -> None:
        # 文件选择区
        file_frame = ttk.LabelFrame(self.root, text="选择抓包文件")
        file_frame.pack(fill="x", padx=10, pady=5)
        self.path_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.path_var, width=60).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(file_frame, text="浏览", command=self.choose_file).grid(row=0, column=1, padx=5)

        # 操作按钮区
        btn_frame = ttk.LabelFrame(self.root, text="操作")
        btn_frame.pack(fill="x", padx=10, pady=5)
        for col in range(4):
            btn_frame.columnconfigure(col, weight=1)
        ttk.Button(btn_frame, text="解析全量入库", command=self.save_to_db).grid(row=0, column=0, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="分析跨代理链路", command=self.analyze_db_links).grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="索引维护", command=self.maintain_indexes).grid(row=0, column=2, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="数据库设置", command=self.open_db_settings).grid(row=0, column=3, sticky="ew", padx=5, pady=4)

        # 进度条 + 取消按钮
        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(progress_frame, text="取消", command=self._cancel_task, state="disabled")
        self.cancel_btn.pack(side="right", padx=(5, 0))

        # 结果显示区
        result_frame = ttk.LabelFrame(self.root, text="操作日志与结果")
        result_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.show_text = scrolledtext.ScrolledText(result_frame, font=("Consolas", 10))
        self.show_text.pack(fill="both", expand=True)

    # ---- 工具方法 ----

    def _set_busy(self, busy: bool) -> None:
        """设置忙碌状态，防止重复提交；同步切换取消按钮可用性"""
        self._busy = busy
        try:
            self.cancel_btn.config(state="normal" if busy else "disabled")
        except tk.TclError:
            pass
        if busy:
            self._cancel_event.clear()  # 新任务开始前清空中断标志

    def _cancel_task(self) -> None:
        """用户点击取消按钮：设置中断标志，后台线程下次检查点会退出"""
        self._cancel_event.set()
        self.show_text.insert("end", "\n⚠ 已请求取消，正在等待当前批次处理完毕...\n")
        self.show_text.see("end")
        self.cancel_btn.config(state="disabled")  # 防止重复点击

    def _update_progress(self, value: float) -> None:
        """更新进度条（线程安全，通过 root.after 调用）"""
        self.progress_var.set(value)






    @staticmethod
    def _match_http_header(pkt, header_name: str, target_value: str) -> bool:
        """匹配 HTTP 报文头字段值（大小写不敏感），支持 http.connection == "close" 这类显示过滤。
        依赖 scapy.layers.http 解码（导入该模块即自动绑定 TCP 80<->HTTP）。
        只读取头部前 8KB，避免大文件上传场景下把整个 HTTP body 拉进内存。"""
        if not _HTTP_AVAILABLE or HTTP_LAYER is None:
            return False
        if HTTP_LAYER not in pkt:
            return False
        try:
            # 只取前 8KB：HTTP 头部远小于此，body 可能几十 MB，全读会撑爆内存
            raw = bytes(pkt[HTTP_LAYER])[:8192]
            # 头部与 body 以 \r\n\r\n 分隔；若前 8KB 没找到分隔符，说明头部异常长或非标准
            header_block = raw.split(b'\r\n\r\n', 1)[0]
            text = header_block.decode('latin-1', errors='replace')
            lines = text.split('\r\n')
            # 第一行是请求行/状态行，从第二行起为头部
            for line in lines[1:]:
                if ':' in line:
                    name, _, value = line.partition(':')
                    if name.strip().lower() == header_name.lower():
                        return value.strip().lower() == target_value.strip().lower()
        except Exception:
            pass
        return False

    def choose_file(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        paths = filedialog.askopenfilenames(
            filetypes=[("抓包文件", "*.pcap *.pcapng"), ("所有文件", "*.*")]
        )
        if paths:
            self.pcap_paths = list(paths)
            display_path = "; ".join(self.pcap_paths)
            self.path_var.set(display_path)
            self.show_text.delete(1.0, "end")
            self.show_text.insert("end", f"已选择 {len(self.pcap_paths)} 个文件：\n")
            for p in self.pcap_paths:
                self.show_text.insert("end", f"  - {p}\n")
            self.show_text.insert("end", "\n就绪，请点击“解析全量入库”以批量处理。\n")






    def maintain_indexes(self) -> None:
        """索引维护：检查碎片率，按需重组/重建"""
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if pyodbc is None:
            messagebox.showerror("错误", "未找到 pyodbc 模块")
            return

        cfg = self._load_db_config()
        table_name = cfg.get("table_name", "packets")
        if not cfg.get("server") or not cfg.get("database"):
            messagebox.showerror("错误", "请先配置数据库")
            self.open_db_settings()
            return

        self._set_busy(True)
        self._update_progress(0)
        self.show_text.insert("end", "\n正在检查索引碎片情况...\n")

        def _maintain_task():
            mgr: Optional[PcapDbManager] = None
            try:
                mgr = PcapDbManager(cfg)
                mgr.connect()

                # 1. 查询碎片
                frag_info = mgr.get_index_fragmentation(table_name)

                if not frag_info:
                    self.root.after(0, lambda: (
                        self.show_text.insert("end", "未找到索引（表可能未建索引）\n"),
                        messagebox.showinfo("索引维护", "未找到索引")
                    ))
                    return

                # 2. 显示碎片报告
                report_lines = ["【索引碎片报告】", "-" * 60]
                for idx in frag_info:
                    action_map = {"rebuild": "→ 建议重建", "reorganize": "→ 建议重组", "ok": "✓ 正常"}
                    report_lines.append(
                        f"{idx['name']:<35} 碎片率: {idx['fragmentation_pct']:>6.2f}%  "
                        f"页数: {idx['page_count']:>8}  大小: {idx['size_mb']:>8.2f}MB  {action_map[idx['recommend']]}"
                    )
                report_lines.append("-" * 60)
                report = "\n".join(report_lines)
                self.root.after(0, lambda r=report: self.show_text.insert("end", r + "\n"))

                # 3. 找出需要维护的索引
                to_rebuild = [f for f in frag_info if f["recommend"] == "rebuild"]
                to_reorganize = [f for f in frag_info if f["recommend"] == "reorganize"]

                if not to_rebuild and not to_reorganize:
                    self.root.after(0, lambda: messagebox.showinfo("索引维护", "所有索引状态良好，无需维护"))
                    return

                # 4. 询问用户是否执行维护
                action_desc = []
                if to_rebuild:
                    action_desc.append(f"重建 {len(to_rebuild)} 个索引（{', '.join(i['name'] for i in to_rebuild)}）")
                if to_reorganize:
                    action_desc.append(f"重组 {len(to_reorganize)} 个索引（{', '.join(i['name'] for i in to_reorganize)}）")
                confirm_msg = (
                    f"检测到以下索引需要维护：\n\n"
                    + "\n".join(action_desc) +
                    f"\n\n是否立即执行？\n"
                    f"  - 重组：轻量级，不锁表，可继续入库\n"
                    f"  - 重建：重量级，可能锁表，建议空闲时执行\n"
                    f"  - ONLINE 重建（如支持）不锁表但占用 tempdb"
                )

                # 用线程安全的 messagebox.askyesno
                import queue
                result_queue = queue.Queue()
                self.root.after(0, lambda: result_queue.put(messagebox.askyesno("确认维护", confirm_msg)))
                user_confirm = result_queue.get(timeout=30)  # 等 30 秒

                if not user_confirm:
                    self.root.after(0, lambda: self.show_text.insert("end", "用户取消维护\n"))
                    return

                # 5. 执行维护
                total_actions = len(to_rebuild) + len(to_reorganize)
                done = 0

                # 先做重组（快）
                for idx in to_reorganize:
                    self.root.after(0, lambda n=idx['name']: self.show_text.insert(
                        "end", f"正在重组索引 {n}...\n"
                    ))
                    mgr.reorganize_index(table_name, idx["name"])
                    done += 1
                    self.root.after(0, lambda p=(done/total_actions*100): self._update_progress(p))

                # 再做重建（慢，尝试 ONLINE 模式）
                for idx in to_rebuild:
                    self.root.after(0, lambda n=idx['name']: self.show_text.insert(
                        "end", f"正在重建索引 {n}...\n"
                    ))
                    try:
                        mgr.rebuild_index(table_name, idx["name"], online=True)
                    except Exception:
                        # ONLINE 不支持（Standard 版）回退到离线重建
                        self.root.after(0, lambda n=idx['name']: self.show_text.insert(
                            "end", f"  ONLINE 重建不可用，改用离线重建 {n}...\n"
                        ))
                        mgr.rebuild_index(table_name, idx["name"], online=False)
                    done += 1
                    self.root.after(0, lambda p=(done/total_actions*100): self._update_progress(p))

                self.root.after(0, lambda: (
                    self.show_text.insert("end", f"索引维护完成，共处理 {total_actions} 个索引\n"),
                    messagebox.showinfo("完成", f"索引维护完成，共处理 {total_actions} 个索引")
                ))

            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", f"索引维护失败：{m}"))
            finally:
                if mgr:
                    mgr.close()
                self.root.after(0, lambda: (
                    self._set_busy(False),
                    self._update_progress(100)
                ))

        threading.Thread(target=_maintain_task, daemon=True).start()


    def analyze_db_links(self) -> None:
        """从数据库分析跨代理链路并展示"""
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if pyodbc is None:
            messagebox.showerror("错误", "未找到 pyodbc 模块")
            return

        cfg = self._load_db_config()
        if not cfg.get("server") or not cfg.get("database"):
            messagebox.showerror("错误", "请先配置数据库")
            self.open_db_settings()
            return
        
        table_name = cfg.get("table_name", "packets")
        self._set_busy(True)
        self.show_text.insert("end", "\n正在从数据库进行跨代理链路自关联查询（基于 payload_hash）...\n")
        
        def _task():
            mgr = None
            try:
                mgr = PcapDbManager(cfg)
                mgr.connect()
                links = mgr.analyze_cross_proxy_links(table_name)
                
                report = [f"✓ 查询完成，共匹配到 {len(links)} 条完整链路。"]
                if links:
                    report.append(f"{'客户端 IP':<18} | {'发起请求时间':<24} | {'代理转发时间':<24} | {'分发服务器 IP':<18} | {'代理耗时(ms)':<10} | URL")
                    report.append("-" * 120)
                    for r in links:
                        cip, ctime, sip, stime, latency, url = r
                        ctime_str = ctime.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if ctime else ""
                        stime_str = stime.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if stime else ""
                        report.append(f"{str(cip):<18} | {ctime_str:<24} | {stime_str:<24} | {str(sip):<18} | {str(latency):<10} | {str(url)}")
                
                report_str = "\n".join(report) + "\n"
                self.root.after(0, lambda: (
                    self.show_text.insert("end", report_str),
                    self.show_text.see("end")
                ))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror("错误", f"查询失败: {err}"))
            finally:
                if mgr:
                    mgr.close()
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=_task, daemon=True).start()

    # ---- 数据库 相关 ----

    @property
    def _db_config_path(self) -> str:
        return os.path.join(os.path.dirname(__file__), "db_config.json")

    def _load_db_config(self) -> Dict[str, Any]:
        """加载数据库配置，环境变量优先覆盖文件值"""
        path = self._db_config_path
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        else:
            cfg = {}

        # 默认配置值
        default_cfg = {
            "server": "localhost",
            "port": "1433",
            "database": "pcap_db",
            "username": "",
            "password": "",
            "driver": "ODBC Driver 17 for SQL Server",
            "table_name": "packets"
        }
        
        # 合并默认值
        for k, v in default_cfg.items():
            if k not in cfg:
                cfg[k] = v

        # 环境变量优先覆盖
        env_server = os.environ.get("COMPANY_DB_SERVER", "")
        env_port = os.environ.get("COMPANY_DB_PORT", "")
        env_database = os.environ.get("COMPANY_DB_NAME", "")
        env_username = os.environ.get("COMPANY_DB_USER", "")
        env_password = os.environ.get("COMPANY_DB_PASS", "")
        env_driver = os.environ.get("COMPANY_DB_DRIVER", "")
        
        if env_server:
            cfg["server"] = env_server
        if env_port:
            cfg["port"] = env_port
        if env_database:
            cfg["database"] = env_database
        if env_username:
            cfg["username"] = env_username
        if env_password:
            cfg["password"] = env_password
        if env_driver:
            cfg["driver"] = env_driver

        return cfg

    def _save_db_config(self, config: Dict[str, Any]) -> None:
        """保存数据库配置到文件。若密码来自环境变量则不重复写入文件，降低明文泄露风险。"""
        path = self._db_config_path
        save_config = dict(config)

        # 如果环境变量中已有密码且与当前值相同，则不写入文件
        env_pass = os.environ.get("COMPANY_DB_PASS", "")
        if env_pass and save_config.get("password") == env_pass:
            save_config["password"] = ""

        with open(path, "w", encoding="utf-8") as f:
            json.dump(save_config, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def open_db_settings(self) -> None:
        cfg = self._load_db_config()
        env_pass_present = bool(os.environ.get("COMPANY_DB_PASS", ""))

        win = tk.Toplevel(self.root)
        win.title("数据库设置 (SQL Server)")
        win.geometry("580x360")
        win.resizable(False, False)
        win.grab_set()  # 启用模态，锁定父窗口焦点

        # Grid布局配置
        win.columnconfigure(1, weight=1)

        # Server
        ttk.Label(win, text="服务器 (Server):").grid(row=0, column=0, sticky="w", padx=15, pady=(15,5))
        server_var = tk.StringVar(value=cfg.get("server", ""))
        ttk.Entry(win, textvariable=server_var, width=40).grid(row=0, column=1, sticky="w", padx=15, pady=(15,5))

        # Port
        ttk.Label(win, text="端口 (Port):").grid(row=1, column=0, sticky="w", padx=15, pady=5)
        port_var = tk.StringVar(value=cfg.get("port", "1433"))
        ttk.Entry(win, textvariable=port_var, width=40).grid(row=1, column=1, sticky="w", padx=15, pady=5)

        # Database
        ttk.Label(win, text="数据库 (Database):").grid(row=2, column=0, sticky="w", padx=15, pady=5)
        db_var = tk.StringVar(value=cfg.get("database", "pcap_db"))
        ttk.Entry(win, textvariable=db_var, width=40).grid(row=2, column=1, sticky="w", padx=15, pady=5)

        # Table
        ttk.Label(win, text="表名 (Table):").grid(row=3, column=0, sticky="w", padx=15, pady=5)
        table_var = tk.StringVar(value=cfg.get("table_name", "packets"))
        ttk.Entry(win, textvariable=table_var, width=40).grid(row=3, column=1, sticky="w", padx=15, pady=5)

        # Username
        ttk.Label(win, text="用户名 (Username):").grid(row=4, column=0, sticky="w", padx=15, pady=5)
        user_var = tk.StringVar(value=cfg.get("username", ""))
        ttk.Entry(win, textvariable=user_var, width=40).grid(row=4, column=1, sticky="w", padx=15, pady=5)

        # Password
        ttk.Label(win, text="密码 (Password):").grid(row=5, column=0, sticky="w", padx=15, pady=5)
        pass_var = tk.StringVar(value=cfg.get("password", ""))
        pass_entry = ttk.Entry(win, textvariable=pass_var, width=40, show="*")
        pass_entry.grid(row=5, column=1, sticky="w", padx=15, pady=5)
        if env_pass_present:
            ttk.Label(win, text="(已由环境变量注入)").grid(row=5, column=2, sticky="w", padx=5)

        # Driver
        ttk.Label(win, text="驱动 (Driver):").grid(row=6, column=0, sticky="w", padx=15, pady=5)
        driver_var = tk.StringVar(value=cfg.get("driver", "ODBC Driver 17 for SQL Server"))
        ttk.Entry(win, textvariable=driver_var, width=40).grid(row=6, column=1, sticky="w", padx=15, pady=5)

        def save_and_close():
            new_cfg = {
                "server": server_var.get().strip(),
                "port": port_var.get().strip(),
                "database": db_var.get().strip(),
                "table_name": table_var.get().strip(),
                "username": user_var.get().strip(),
                "password": pass_var.get().strip(),
                "driver": driver_var.get().strip()
            }
            self._save_db_config(new_cfg)
            win.destroy()
            messagebox.showinfo("提示", "数据库配置已保存！")

        def test_conn():
            test_cfg = {
                "server": server_var.get().strip(),
                "port": port_var.get().strip(),
                "database": db_var.get().strip(),
                "username": user_var.get().strip(),
                "password": pass_var.get().strip(),
                "driver": driver_var.get().strip()
            }
            if pyodbc is None:
                messagebox.showerror("错误", "未找到 pyodbc 模块", parent=win)
                return
            try:
                mgr = PcapDbManager(test_cfg)
                mgr.connect()
                mgr.close()
                messagebox.showinfo("成功", "测试连接成功！\n数据库可正常访问。", parent=win)
            except Exception as e:
                messagebox.showerror("失败", f"测试连接失败：\n{str(e)}", parent=win)

        btn_frame = ttk.Frame(win)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=(20, 10))
        ttk.Button(btn_frame, text="测试连通性", command=test_conn, width=15).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="保存", command=save_and_close, width=15).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="取消", command=win.destroy, width=15).pack(side="left", padx=10)
    def save_to_db(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if not hasattr(self, 'pcap_paths') or not self.pcap_paths:
            messagebox.showwarning("提示", "请先选择抓包文件")
            return

        if pyodbc is None:
            messagebox.showerror("驱动错误", "未找到 pyodbc 模块。")
            return

        import shutil
        tshark_path = shutil.which("tshark")
        if not tshark_path:
            alt_path = r"C:\Program Files\Wireshark\tshark.exe"
            if os.path.exists(alt_path):
                tshark_path = alt_path
            else:
                messagebox.showerror("错误", "未找到 tshark.exe！\n请安装 Wireshark 并将其添加到系统 PATH 中。")
                return

        cfg = self._load_db_config()
        server = cfg.get("server", "localhost")
        database = cfg.get("database", "pcap_db")
        table_name = cfg.get("table_name", "packets")

        if not server or not database:
            messagebox.showerror("错误", "请先配置正确的数据库服务器与数据库名称！")
            self.open_db_settings()
            return

        self._set_busy(True)
        self._update_progress(0)
        self.show_text.insert("end", f"\n正在使用 tshark 流式解析并保存数据到 SQL Server...\n")
        self.show_text.see("end")

        def _save_db_task():
            mgr = None
            try:
                mgr = PcapDbManager(cfg)
                mgr.connect()
                try:
                    schema_ok = mgr.is_schema_deployed(mgr.conn)
                except Exception:
                    schema_ok = True
                if not schema_ok:
                    self.root.after(0, lambda: self.show_text.insert("end", "⚠ 警告：未检测到存储过程...\n\n"))
                
                mgr.ensure_schema(table_name)
                
                inserted_total = 0
                total_files = len(self.pcap_paths)
                
                import re
                RE_SENDTIME1 = re.compile(r'"sendTime"\s*:\s*([\d\.]+)')
                RE_SENDTIME2 = re.compile(r'sendTime[=%\w]+([\d\.]+)')
                
                for file_idx, current_path in enumerate(self.pcap_paths, 1):
                    if self._cancel_event.is_set():
                        break
                        
                    if not os.path.exists(current_path):
                        self.root.after(0, lambda p=current_path: self.show_text.insert("end", f"⚠ 文件不存在跳过: {p}\n"))
                        continue
                        
                    self.root.after(0, lambda i=file_idx, t=total_files, p=current_path: (
                        self.show_text.insert("end", f"\n[{i}/{t}] 正在处理: {p}\n"),
                        self.show_text.see("end")
                    ))
                    
                    file_name = os.path.basename(current_path)
                    status = mgr.check_duplicate(table_name, file_name)
                    if status == 1:
                        self.root.after(0, lambda: self.show_text.insert("end", f"  检测到同名文件已入库，正在覆盖旧数据...\n"))
                        deleted = mgr.delete_by_file(table_name, file_name)
                        mgr.commit()
                        self.root.after(0, lambda d=deleted: self.show_text.insert("end", f"  已删除旧数据 {d} 条\n"))

                    import subprocess
                    cmd = [
                        tshark_path, "-r", current_path, "-T", "ek",
                        "-e", "frame.time_epoch", "-e", "frame.number",
                        "-e", "eth.src", "-e", "eth.dst",
                        "-e", "ip.src", "-e", "ip.dst",
                        "-e", "ipv6.src", "-e", "ipv6.dst",
                        "-e", "tcp.srcport", "-e", "tcp.dstport",
                        "-e", "udp.srcport", "-e", "udp.dstport",
                        "-e", "tcp.flags.str", "-e", "tcp.seq", "-e", "tcp.ack", "-e", "tcp.len",
                        "-e", "http.request.uri", "-e", "http.file_data"
                    ]
                    
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                    
                    batch_rows = []
                    batch_insert_time = datetime.datetime.now()
                    
                    def flush_batch():
                        nonlocal inserted_total
                        if not batch_rows: return
                        def cancel_check(): return self._cancel_event.is_set()
                        inserted = mgr.insert_batch(
                            table_name=table_name, rows=batch_rows,
                            batch_size=self.DB_BATCH_SIZE, progress_cb=None, cancel_check=cancel_check
                        )
                        inserted_total += inserted
                        batch_rows.clear()
                        def _update_ui(t):
                            self.show_text.insert("end", f"  已入库 {t} 条...\n")
                            try:
                                if float(self.show_text.index("end-1c")) > 2000.0:
                                    self.show_text.delete("1.0", "end-1500l")
                            except Exception:
                                pass
                            self.show_text.see("end")
                        self.root.after(0, _update_ui, inserted_total)
                    
                    for line in process.stdout:
                        if self._cancel_event.is_set():
                            process.terminate()
                            break
                        if not line.strip(): continue
                        try:
                            pkt_data = orjson.loads(line) if orjson else json.loads(line)
                        except Exception:
                            continue
                        if "index" in pkt_data: continue
                        layers = pkt_data.get("layers", {})
                        if not layers: continue
                        
                        def get_first(d, k, default=None):
                            val = d.get(k)
                            if val and isinstance(val, list) and len(val) > 0: return val[0]
                            return default
                            
                        ts_epoch = get_first(layers, "frame_time_epoch")
                        try: packet_time = datetime.datetime.fromtimestamp(float(ts_epoch)) if ts_epoch else None
                        except: packet_time = None
                        frame_num = int(get_first(layers, "frame_number", 0))
                        mac_src = get_first(layers, "eth_src")
                        mac_dst = get_first(layers, "eth_dst")
                        ip_src = get_first(layers, "ip_src") or get_first(layers, "ipv6_src")
                        ip_dst = get_first(layers, "ip_dst") or get_first(layers, "ipv6_dst")
                        sport = get_first(layers, "tcp_srcport") or get_first(layers, "udp_srcport")
                        dport = get_first(layers, "tcp_dstport") or get_first(layers, "udp_dstport")
                        flag_val = get_first(layers, "tcp_flags_str")
                        seq_val = get_first(layers, "tcp_seq")
                        ack_val = get_first(layers, "tcp_ack")
                        len_val = get_first(layers, "tcp_len")
                        
                        if seq_val is not None: seq_val = int(seq_val)
                        if ack_val is not None: ack_val = int(ack_val)
                        if len_val is not None: len_val = int(len_val)
                        
                        post_url = get_first(layers, "http_request_uri")
                        file_data_hex = get_first(layers, "http_file_data")
                        
                        payload_hash = None
                        client_time = None
                        if file_data_hex:
                            try:
                                file_data_str = str(file_data_hex)
                                payload_hash = hashlib.md5(file_data_str.encode('utf-8')).hexdigest()
                                if "sendTime" in file_data_str:
                                    m = RE_SENDTIME1.search(file_data_str)
                                    if not m: m = RE_SENDTIME2.search(file_data_str)
                                    if m: client_time = datetime.datetime.fromtimestamp(float(m.group(1)))
                            except Exception:
                                pass
                                
                        summary_val = f"Len={len_val}"
                        pkt_ts_str = f"{float(ts_epoch):.6f}" if ts_epoch else ""
                        hash_src = f"{pkt_ts_str}|{mac_src or ''}|{mac_dst or ''}|{ip_src or ''}|{ip_dst or ''}|{sport or ''}|{dport or ''}|{flag_val or ''}|{seq_val or ''}|{ack_val or ''}|{len_val or ''}".encode('utf-8')
                        packet_hash = hashlib.md5(hash_src).hexdigest()
                        
                        row = (file_name, packet_hash, packet_time, mac_src, mac_dst, ip_src, ip_dst, sport, dport, flag_val, seq_val, ack_val, len_val, summary_val, batch_insert_time, post_url, client_time, payload_hash, "UNKNOWN", frame_num)
                        batch_rows.append(row)
                        if len(batch_rows) >= self.DB_BATCH_SIZE:
                            flush_batch()
                            
                    flush_batch()
                
                if self._cancel_event.is_set():
                    self.root.after(0, lambda: self.show_text.insert("end", f"⚠ 已取消入库。\n"))
                    return
                
                self.root.after(0, lambda: self.show_text.insert("end", f"\n✅ 已成功保存 {inserted_total} 条报文至 SQL Server\n"))
                self.root.after(0, lambda: self.show_text.see("end"))
                
            except Exception as e:
                if mgr: mgr.rollback()
                self.root.after(0, lambda m=str(e): messagebox.showerror("错误", f"保存数据库失败：{m}"))
            finally:
                if mgr: mgr.close()
                self.root.after(0, lambda: (self._set_busy(False), self._update_progress(100)))

        threading.Thread(target=_save_db_task, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = WinPcapFilter(root)
    root.mainloop()
