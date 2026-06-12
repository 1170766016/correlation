import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from scapy.all import rdpcap, wrpcap, sniff
from scapy.plist import PacketList
from scapy.layers.inet import IP, TCP, UDP, ICMP
from scapy.layers.inet6 import IPv6
from scapy.layers.l2 import Ether
import os
import json
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


class ConnStatus(Enum):
    """TCP 连接状态枚举，替代散落的魔法字符串"""
    CLOSED = "CLOSED"
    CONN_REFUSED = "CONN_REFUSED"
    NO_RESPONSE = "NO_RESPONSE"
    HANDSHAKE_FAILED = "HANDSHAKE_FAILED"
    RST_AFTER_HANDSHAKE = "RST_AFTER_HANDSHAKE"
    ESTABLISHED_RST = "ESTABLISHED_RST"
    ESTABLISHED_ONGOING = "ESTABLISHED_ONGOING"
    PRE_ESTAB_RST = "PRE_ESTAB_RST"
    PRE_ESTAB_CLOSED = "PRE_ESTAB_CLOSED"
    PRE_ESTAB_ONGOING = "PRE_ESTAB_ONGOING"


# 非异常状态集合（用于判定连接是否正常）
_NORMAL_STATUSES = frozenset({
    ConnStatus.CLOSED,
    ConnStatus.ESTABLISHED_ONGOING,
    ConnStatus.PRE_ESTAB_CLOSED,
    ConnStatus.PRE_ESTAB_ONGOING,
    ConnStatus.ESTABLISHED_RST,
})


class WinPcapFilter:
    # ---- 常量定义 ----
    TCP_SESSION_TIMEOUT = 60.0      # 秒，超过此间隔判定为新会话
    RST_MIN_PACKETS = 4             # RST 异常判定的最小包数
    LLM_MAX_SAMPLES = 40            # 发送给 LLM 的最大样本数
    LLM_SAMPLE_PER_TYPE = 4         # 每种错误类型的最大样本数
    LLM_API_TIMEOUT = 300           # API 调用超时（秒）
    LLM_MAX_TOKENS = 2048
    LLM_MAX_RETRIES = 3             # API 调用最大重试次数
    LLM_RETRY_BACKOFF = 1.0         # 重试初始退避时间（秒）
    LLM_RETRYABLE_CODES = {429, 500, 502, 503}  # 可重试的 HTTP 状态码
    UI_UPDATE_INTERVAL = 100        # 每N条报文刷新一次 UI
    DB_BATCH_SIZE = 1000            # 数据库批量插入大小
    RAW_HEX_MAX_BYTES = 2048        # raw_hex 最大存储字节数
    DISPLAY_PAGE_SIZE = 2000        # 显示区最大报文数量（避免 ScrolledText 过慢）

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Wireshark抓包过滤工具")
        self.root.geometry("850x680")
        self.pcap_path: str = ""
        self.all_pkts: list = []       # 原始所有报文
        self.filtered_pkts: list = []  # 过滤后报文
        self._busy: bool = False       # 防止重复提交
        self.init_ui()

    def init_ui(self) -> None:
        # 文件选择区
        file_frame = ttk.LabelFrame(self.root, text="选择抓包文件")
        file_frame.pack(fill="x", padx=10, pady=5)
        self.path_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.path_var, width=60).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(file_frame, text="浏览", command=self.choose_file).grid(row=0, column=1, padx=5)

        # 过滤规则区
        filter_frame = ttk.LabelFrame(self.root, text="过滤规则（Wireshark语法）")
        filter_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(filter_frame, text="预设规则：").grid(row=0, column=0)
        self.rule = tk.StringVar(value="tcp port 80 or 443")
        rules = [
            "tcp port 80 or 443",
            "icmp",
            "tcp",
            "udp",
            "ip host 192.168.1.100",
            "udp port 53"
        ]
        ttk.Combobox(filter_frame, textvariable=self.rule, values=rules, width=45).grid(row=0, column=1, padx=5)
        ttk.Button(filter_frame, text="开始过滤", command=self.do_filter).grid(row=1, column=0, columnspan=2, pady=5)

        # 进度条
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(self.root, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 5))

        # 结果显示区
        result_frame = ttk.LabelFrame(self.root, text="过滤结果")
        result_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.show_text = scrolledtext.ScrolledText(result_frame, font=("Consolas", 10))
        self.show_text.pack(fill="both", expand=True)

        # 导出按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=5)
        ttk.Button(btn_frame, text="导出文本", command=self.export_txt).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="保存抓包", command=self.save_pcap).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="保存到数据库", command=self.save_to_db).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="分析异常连接", command=self.analyze_connections).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="LLM分析错误", command=self.llm_analyze_errors).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="LLM设置", command=self.open_llm_settings).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="数据库设置", command=self.open_db_settings).pack(side="left", padx=5)

    # ---- 工具方法 ----

    def _set_busy(self, busy: bool) -> None:
        """设置忙碌状态，防止重复提交"""
        self._busy = busy

    def _update_progress(self, value: float) -> None:
        """更新进度条（线程安全，通过 root.after 调用）"""
        self.progress_var.set(value)

    @staticmethod
    def _packet_info(pkt) -> str:
        parts: list[str] = []
        tcp_layer = pkt.getlayer(TCP)
        udp_layer = pkt.getlayer(UDP)
        icmp_layer = pkt.getlayer(ICMP)
        ip_layer = pkt.getlayer(IP)
        ipv6_layer = pkt.getlayer(IPv6)

        if tcp_layer:
            t = tcp_layer
            flags: list[str] = []
            if t.flags & 0x02: flags.append("SYN")
            if t.flags & 0x10: flags.append("ACK")
            if t.flags & 0x01: flags.append("FIN")
            if t.flags & 0x04: flags.append("RST")
            if t.flags & 0x08: flags.append("PSH")
            if t.flags & 0x20: flags.append("URG")
            flags_str = " ".join(flags) if flags else str(t.flags)
            payload_len = 0
            if ip_layer:
                ip_len = ip_layer.len
                ip_hdr = ip_layer.ihl * 4
                tcp_hdr = t.dataofs * 4
                payload_len = ip_len - ip_hdr - tcp_hdr
            elif ipv6_layer:
                # 直接使用 TCP payload 长度，避免 IPv6 扩展头干扰
                payload_len = len(bytes(t.payload)) if t.payload else 0
            parts.append(f"[{flags_str}] Seq={t.seq} Ack={t.ack} Len={payload_len}")
        elif udp_layer:
            u = udp_layer
            parts.append(f"Len={u.len}")
        elif icmp_layer:
            icmp = icmp_layer
            parts.append(f"Type={icmp.type} Code={icmp.code}")

        if ip_layer:
            sport = f":{tcp_layer.sport}" if tcp_layer else f":{udp_layer.sport}" if udp_layer else ""
            dport = f":{tcp_layer.dport}" if tcp_layer else f":{udp_layer.dport}" if udp_layer else ""
            parts.append(f"{ip_layer.src}{sport} > {ip_layer.dst}{dport}")
        elif ipv6_layer:
            sport = f":{tcp_layer.sport}" if tcp_layer else f":{udp_layer.sport}" if udp_layer else ""
            dport = f":{tcp_layer.dport}" if tcp_layer else f":{udp_layer.dport}" if udp_layer else ""
            parts.append(f"{ipv6_layer.src}{sport} > {ipv6_layer.dst}{dport}")
        return " ".join(parts)

    def _format_packet_display(self, i: int, pkt) -> str:
        """将单个包格式化为显示文本（统一 do_filter 和 export_txt 的显示逻辑）"""
        lines: list[str] = [f"第 {i} 条"]
        ts = datetime.datetime.fromtimestamp(float(pkt.time)).strftime(
            "%Y-%m-%d %H:%M:%S.%f") if hasattr(pkt, 'time') else "N/A"
        lines.append(f"时间：{ts}")

        ether_layer = pkt.getlayer(Ether)
        ip_layer = pkt.getlayer(IP)
        ipv6_layer = pkt.getlayer(IPv6)
        tcp_layer = pkt.getlayer(TCP)
        udp_layer = pkt.getlayer(UDP)

        if ether_layer:
            lines.append(f"MAC：{ether_layer.src} → {ether_layer.dst}")
        if ip_layer:
            lines.append(f"IP：{ip_layer.src} → {ip_layer.dst}")
        elif ipv6_layer:
            lines.append(f"IPv6：{ipv6_layer.src} → {ipv6_layer.dst}")
        if tcp_layer:
            lines.append(f"TCP 端口：{tcp_layer.sport} → {tcp_layer.dport}")
        if udp_layer:
            lines.append(f"UDP 端口：{udp_layer.sport} → {udp_layer.dport}")
        lines.append(f"Info：{self._packet_info(pkt)}")
        return "\n".join(lines) + "\n\n"

    def _match_filter(self, pkt, rule: str) -> bool:
        """简单的BPF过滤规则匹配"""
        # 规范化多空格，将连续的多空格压缩为单空格，极大提升语法兼容性
        rule = " ".join(rule.strip().lower().split())

        # 纯协议过滤
        if rule == "tcp":
            return TCP in pkt
        if rule == "udp":
            return UDP in pkt
        if rule == "icmp":
            return ICMP in pkt

        # tcp port X or Y
        if rule.startswith("tcp port"):
            ports = [int(w) for w in rule.replace("or", " ").split() if w.isdigit()]
            if TCP in pkt:
                return pkt[TCP].sport in ports or pkt[TCP].dport in ports
            return False

        # udp port X
        if rule.startswith("udp port"):
            ports = [int(w) for w in rule.replace("or", " ").split() if w.isdigit()]
            if UDP in pkt:
                return pkt[UDP].sport in ports or pkt[UDP].dport in ports
            return False

        # ip host X.X.X.X
        if rule.startswith("ip host"):
            host = rule.split()[-1]
            if IP in pkt:
                return pkt[IP].src == host or pkt[IP].dst == host
            return False

        # 未知规则，不进行匹配
        return False

    def choose_file(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        path = filedialog.askopenfilename(
            filetypes=[("抓包文件", "*.pcap *.pcapng"), ("所有文件", "*.*")]
        )
        if path:
            self.pcap_path = path
            self.path_var.set(path)
            self.show_text.delete(1.0, "end")
            self.show_text.insert("end", f"已选择：{path}\n正在加载并解析文件，请稍候...\n")
            self._set_busy(True)
            self._update_progress(0)

            def _load_task():
                try:
                    pkts = rdpcap(self.pcap_path)
                    self.root.after(0, lambda p=pkts: (
                        self.show_text.insert("end", f"文件加载成功，共 {len(p)} 条报文\n"),
                        messagebox.showinfo("成功", f"文件加载成功，共 {len(p)} 条报文")
                    ))
                    self.all_pkts = pkts
                except Exception as e:
                    err_msg = str(e)
                    self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", f"加载文件失败：{m}"))
                    self.all_pkts = []
                finally:
                    self.root.after(0, lambda: (
                        self._set_busy(False),
                        self._update_progress(100)
                    ))

            threading.Thread(target=_load_task, daemon=True).start()

    def do_filter(self) -> None:
        if not self.pcap_path or not self.all_pkts:
            messagebox.showerror("错误", "请先选择并加载抓包文件！")
            return
        if self._busy:
            messagebox.showwarning("提示", "上一个任务正在执行中，请稍候")
            return

        rule = self.rule.get().strip()
        self.show_text.delete(1.0, "end")
        self.show_text.insert("end", f"正在过滤：{rule}\n")
        self._set_busy(True)
        self._update_progress(0)

        def _filter_task():
            try:
                # 优先尝试使用 sniff 进行标准的 BPF 过滤，若失败则回退到手写的 _match_filter 进行兼容
                if not rule:
                    filtered = list(self.all_pkts)
                else:
                    try:
                        filtered = list(sniff(offline=self.pcap_path, filter=rule))
                    except Exception as bpf_err:
                        # BPF 降级时提供更清晰的提示，告知用户哪些规则可能不被支持
                        msg = (f"⚠ 标准 BPF 过滤失败，已降级为简单内置过滤器。\n"
                               f"  内置过滤器仅支持: tcp, udp, icmp, tcp port X, udp port X, ip host X\n"
                               f"  BPF 错误: {str(bpf_err)}\n\n")
                        self.root.after(0, lambda m=msg: self.show_text.insert("end", m))
                        filtered = [p for p in self.all_pkts if self._match_filter(p, rule)]

                self.filtered_pkts = PacketList(filtered, name="filtered") if not isinstance(filtered, PacketList) else filtered

                if not self.filtered_pkts:
                    self.root.after(0, lambda: self.show_text.insert("end", "未匹配到报文\n"))
                    return

                total = len(self.filtered_pkts)
                header = f"找到 {total} 条报文\n" + "-" * 80 + "\n\n"
                self.root.after(0, lambda h=header: self.show_text.insert("end", h))

                # 如果报文数超过显示上限，提示并截断显示
                display_count = min(total, self.DISPLAY_PAGE_SIZE)
                if total > self.DISPLAY_PAGE_SIZE:
                    truncate_msg = (f"⚠ 报文数量过多（{total} 条），仅显示前 {self.DISPLAY_PAGE_SIZE} 条。"
                                    f"完整数据请使用「导出文本」功能。\n\n")
                    self.root.after(0, lambda m=truncate_msg: self.show_text.insert("end", m))

                # 分批向 UI 插入文本，避免一次性操作导致卡顿
                batch_lines: list[str] = []
                for i, pkt in enumerate(self.filtered_pkts[:display_count], 1):
                    batch_lines.append(self._format_packet_display(i, pkt))
                    if i % self.UI_UPDATE_INTERVAL == 0:
                        text_block = "".join(batch_lines)
                        progress = i / display_count * 100
                        self.root.after(0, lambda t=text_block, p=progress: (
                            self.show_text.insert("end", t),
                            self._update_progress(p),
                        ))
                        batch_lines = []

                # 插入剩余
                if batch_lines:
                    text_block = "".join(batch_lines)
                    self.root.after(0, lambda t=text_block: self.show_text.insert("end", t))

                self.root.after(0, lambda: self._update_progress(100))

            except Exception as e:
                err_msg = f"过滤失败：{str(e)}"
                self.root.after(0, lambda m=err_msg: messagebox.showerror("过滤失败", m))
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=_filter_task, daemon=True).start()

    def export_txt(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if not self.filtered_pkts:
            messagebox.showwarning("提示", "无数据可导出")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("文本文件", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"过滤时间：{datetime.datetime.now()}\n")
                f.write(f"规则：{self.rule.get()}\n")
                f.write(f"总数：{len(self.filtered_pkts)}\n")
                f.write("-"*80 + "\n\n")
                for i, pkt in enumerate(self.filtered_pkts, 1):
                    f.write(self._format_packet_display(i, pkt))
            messagebox.showinfo("成功", "已导出文本")

    def save_pcap(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if not self.filtered_pkts:
            messagebox.showwarning("提示", "无数据可保存")
            return
        path = filedialog.asksaveasfilename(defaultextension=".pcap")
        if path:
            self._set_busy(True)
            self._update_progress(0)
            self.show_text.insert("end", f"\n正在保存抓包文件到 {path}...\n")

            def _save_pcap_task():
                try:
                    wrpcap(path, self.filtered_pkts)
                    self.root.after(0, lambda: (
                        self.show_text.insert("end", f"抓包文件已成功保存到：{path}\n"),
                        messagebox.showinfo("成功", f"已保存：{path}")
                    ))
                except Exception as e:
                    err_msg = str(e)
                    self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", f"保存抓包失败：{m}"))
                finally:
                    self.root.after(0, lambda: (
                        self._set_busy(False),
                        self._update_progress(100)
                    ))

            threading.Thread(target=_save_pcap_task, daemon=True).start()

    @staticmethod
    def _packet_to_row(pcap_file: str, pkt, pcap_hash: Optional[str] = None) -> tuple:
        ts = float(pkt.time) if hasattr(pkt, 'time') else None
        packet_time = datetime.datetime.fromtimestamp(ts) if ts is not None else None
        file_name = os.path.basename(pcap_file) if pcap_file else None
        
        # 使用 getlayer 缓存层对象，极大提升属性检索效率
        ether_layer = pkt.getlayer(Ether)
        mac_src = ether_layer.src if ether_layer else None
        mac_dst = ether_layer.dst if ether_layer else None
        
        ip_layer = pkt.getlayer(IP)
        ipv6_layer = pkt.getlayer(IPv6)
        ip_src = ip_layer.src if ip_layer else ipv6_layer.src if ipv6_layer else None
        ip_dst = ip_layer.dst if ip_layer else ipv6_layer.dst if ipv6_layer else None
        
        tcp_layer = pkt.getlayer(TCP)
        udp_layer = pkt.getlayer(UDP)
        
        if tcp_layer:
            sport, dport = tcp_layer.sport, tcp_layer.dport
        elif udp_layer:
            sport, dport = udp_layer.sport, udp_layer.dport
        else:
            sport, dport = None, None

        # 提取 TCP 报文的特有字段 (flag, seq, ack, len)
        flag_val = None
        seq_val = None
        ack_val = None
        len_val = None

        if tcp_layer:
            t = tcp_layer
            flags = []
            if t.flags & 0x02: flags.append("SYN")
            if t.flags & 0x10: flags.append("ACK")
            if t.flags & 0x01: flags.append("FIN")
            if t.flags & 0x04: flags.append("RST")
            if t.flags & 0x08: flags.append("PSH")
            if t.flags & 0x20: flags.append("URG")
            flag_val = " ".join(flags) if flags else str(t.flags)
            
            seq_val = t.seq
            ack_val = t.ack
            
            # 计算载荷长度
            payload_len = 0
            if ip_layer:
                ip_len = ip_layer.len if ip_layer.len is not None else len(bytes(ip_layer))
                ip_hdr = ip_layer.ihl * 4
                tcp_hdr = t.dataofs * 4 if t.dataofs is not None else 20
                payload_len = max(0, ip_len - ip_hdr - tcp_hdr)
            elif ipv6_layer:
                payload_len = len(bytes(t.payload)) if t.payload else 0
            len_val = payload_len

        # 构造定制的 summary 字段格式
        summary_lines = []
        ts_str = packet_time.strftime("%Y-%m-%d %H:%M:%S.%f") if packet_time else "N/A"
        summary_lines.append(f"时间：{ts_str}")
        if ether_layer:
            summary_lines.append(f"MAC：{ether_layer.src} → {ether_layer.dst}")
        if ip_layer:
            summary_lines.append(f"IP：{ip_layer.src} → {ip_layer.dst}")
        elif ipv6_layer:
            summary_lines.append(f"IPv6：{ipv6_layer.src} → {ipv6_layer.dst}")
        if tcp_layer:
            summary_lines.append(f"TCP 端口：{tcp_layer.sport} → {tcp_layer.dport}")
        elif udp_layer:
            summary_lines.append(f"UDP 端口：{udp_layer.sport} → {udp_layer.dport}")
        summary_lines.append(f"Info：{WinPcapFilter._packet_info(pkt)}")
        summary_val = "\n".join(summary_lines)

        insert_time = datetime.datetime.now()

        # 计算单条报文的唯一哈希值
        h_pkt = hashlib.md5()
        pkt_ts_str = f"{ts:.6f}" if ts is not None else ""
        h_pkt.update(pkt_ts_str.encode('utf-8'))
        h_pkt.update(str(mac_src or '').encode('utf-8'))
        h_pkt.update(str(mac_dst or '').encode('utf-8'))
        h_pkt.update(str(ip_src or '').encode('utf-8'))
        h_pkt.update(str(ip_dst or '').encode('utf-8'))
        h_pkt.update(str(sport or '').encode('utf-8'))
        h_pkt.update(str(dport or '').encode('utf-8'))
        h_pkt.update(str(flag_val or '').encode('utf-8'))
        h_pkt.update(str(seq_val or '').encode('utf-8'))
        h_pkt.update(str(ack_val or '').encode('utf-8'))
        h_pkt.update(str(len_val or '').encode('utf-8'))
        try:
            h_pkt.update(bytes(pkt))
        except Exception:
            pass
        packet_hash = h_pkt.hexdigest()

        return (
            file_name,
            pcap_hash,
            packet_hash,
            packet_time,
            mac_src, mac_dst,
            ip_src, ip_dst,
            sport, dport,
            flag_val,
            seq_val,
            ack_val,
            len_val,
            summary_val,
            insert_time
        )

    def save_to_db(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if not self.filtered_pkts:
            messagebox.showwarning("提示", "无数据可保存，请先过滤")
            return

        if pyodbc is None:
            messagebox.showerror(
                "驱动错误", 
                "未找到 pyodbc 模块。请在您的 Python 环境中安装 pyodbc 以连接 SQL Server：\n"
                "  pip install pyodbc"
            )
            return

        cfg = self._load_db_config()
        server = cfg.get("server", "localhost")
        port = cfg.get("port", "1433")
        database = cfg.get("database", "pcap_db")
        username = cfg.get("username", "")
        password = cfg.get("password", "")
        driver = cfg.get("driver", "ODBC Driver 17 for SQL Server")
        table_name = cfg.get("table_name", "packets")

        if not server or not database:
            messagebox.showerror("错误", "请先配置正确的数据库服务器与数据库名称！")
            self.open_db_settings()
            return

        self._set_busy(True)
        self._update_progress(0)
        self.show_text.insert("end", f"\n正在保存数据到 SQL Server 数据库 {server} ({database})...\n")

        def _save_db_task():
            conn = None
            try:
                # 计算当前 PCAP 文件的 MD5 哈希指纹
                pcap_hash = ""
                if os.path.exists(self.pcap_path):
                    md5 = hashlib.md5()
                    try:
                        with open(self.pcap_path, "rb") as f:
                            for chunk in iter(lambda: f.read(4096), b""):
                                md5.update(chunk)
                        pcap_hash = md5.hexdigest()
                    except Exception:
                        pass

                # 构造 SQL Server 连接字符串
                if username:
                    # 使用 SQL Server 身份验证
                    conn_str = f"DRIVER={{{driver}}};SERVER={server},{port};DATABASE={database};UID={username};PWD={password}"
                else:
                    # 使用 Windows 身份验证
                    conn_str = f"DRIVER={{{driver}}};SERVER={server},{port};DATABASE={database};Trusted_Connection=yes"

                conn = pyodbc.connect(conn_str)
                cursor = conn.cursor()
                
                # 开启显式事务以获得最佳性能并保证 ACID 原则
                conn.autocommit = False

                # 创建表的 SQL 语句，增加 pcap_hash 和 packet_hash 字段，使用方括号防止保留字冲突
                create_table_sql = f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{table_name}' AND xtype='U')
                CREATE TABLE [{table_name}] (
                    id          INT IDENTITY(1,1) PRIMARY KEY,
                    pcap_file   VARCHAR(255),
                    pcap_hash   VARCHAR(64),
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
                    insert_time DATETIME2(3)
                )
                """
                cursor.execute(create_table_sql)
                conn.commit()

                # 如果表已经存在，则自动检查并添加缺少的列，确保平滑升级
                alter_table_sql = f"""
                IF EXISTS (SELECT * FROM sysobjects WHERE name='{table_name}' AND xtype='U')
                BEGIN
                    -- 如果 timestamp 字段还是 FLOAT 类型，则将其转换为 DATETIME2(6)
                    IF EXISTS (SELECT * FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id WHERE c.object_id = OBJECT_ID('{table_name}') AND c.name = 'timestamp' AND t.name != 'datetime2')
                        ALTER TABLE [{table_name}] ALTER COLUMN timestamp DATETIME2(6);
                        
                    -- 添加缺少的列，采用紧凑的长度设计以节省空间
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'pcap_hash')
                        ALTER TABLE [{table_name}] ADD pcap_hash VARCHAR(64);
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'packet_hash')
                        ALTER TABLE [{table_name}] ADD packet_hash VARCHAR(64);
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'flag')
                        ALTER TABLE [{table_name}] ADD flag VARCHAR(30);
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'seq')
                        ALTER TABLE [{table_name}] ADD seq BIGINT;
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'ack')
                        ALTER TABLE [{table_name}] ADD ack BIGINT;
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'len')
                        ALTER TABLE [{table_name}] ADD len INT;
                    IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('{table_name}') AND name = 'insert_time')
                        ALTER TABLE [{table_name}] ADD insert_time DATETIME2(3);
                END
                """
                cursor.execute(alter_table_sql)
                conn.commit()

                # 建立非聚集索引以确保在大数据量下的文件名、哈希寻靶性能（Index Seek）
                index_pcap_file_sql = f"""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('{table_name}') AND name = 'IX_{table_name}_pcap_file')
                    CREATE INDEX [IX_{table_name}_pcap_file] ON [{table_name}] (pcap_file);
                """
                cursor.execute(index_pcap_file_sql)

                index_pcap_hash_sql = f"""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('{table_name}') AND name = 'IX_{table_name}_pcap_hash')
                    CREATE INDEX [IX_{table_name}_pcap_hash] ON [{table_name}] (pcap_hash);
                """
                cursor.execute(index_pcap_hash_sql)

                # 建立过滤空值的唯一索引，开启 IGNORE_DUP_KEY 确保批量插入时静默过滤重复行
                unique_packet_hash_sql = f"""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('{table_name}') AND name = 'UQ_{table_name}_packet_hash')
                    CREATE UNIQUE INDEX [UQ_{table_name}_packet_hash] 
                    ON [{table_name}] (packet_hash) 
                    WHERE packet_hash IS NOT NULL 
                    WITH (IGNORE_DUP_KEY = ON);
                """
                cursor.execute(unique_packet_hash_sql)
                conn.commit()

                file_name = os.path.basename(self.pcap_path)

                # 1. 检查是否存在相同内容指纹的文件已保存在数据库（支持防改名重复上传）
                if pcap_hash:
                    cursor.execute(f"SELECT DISTINCT pcap_file FROM [{table_name}] WHERE pcap_hash = ?", (pcap_hash,))
                    existing_file_row = cursor.fetchone()
                    if existing_file_row:
                        existing_filename = existing_file_row[0]
                        self.root.after(0, lambda: (
                            self.show_text.insert("end", f"检测到数据库中已存在相同内容的抓包文件（原文件名：{existing_filename}），已自动跳过导入。\n"),
                            messagebox.showinfo("提示", f"抓包文件内容已存在于数据库中（原文件名：{existing_filename}），无需重复保存。")
                        ))
                        return

                # 2. 检查文件名是否相同
                cursor.execute(f"SELECT DISTINCT pcap_hash FROM [{table_name}] WHERE pcap_file = ?", (file_name,))
                existing_rows = cursor.fetchall()
                if existing_rows:
                    has_same_hash = any(r[0] == pcap_hash for r in existing_rows)
                    if has_same_hash:
                        self.root.after(0, lambda: (
                            self.show_text.insert("end", f"检测到数据库中已存在同名且内容相同的抓包文件 {file_name}，已自动跳过导入。\n"),
                            messagebox.showinfo("提示", f"同名且内容相同的抓包文件 {file_name} 已存在，已自动跳过。")
                        ))
                        return
                    else:
                        # 覆盖旧数据（删除当前同名文件的记录）
                        self.root.after(0, lambda: self.show_text.insert("end", f"检测到同名但内容不同的抓包文件 {file_name}，正在覆盖数据库中的旧数据...\n"))
                        cursor.execute(f"DELETE FROM [{table_name}] WHERE pcap_file = ?", (file_name,))

                sql = f"""
                    INSERT INTO [{table_name}]
                        (pcap_file, pcap_hash, packet_hash, timestamp, mac_src, mac_dst,
                         ip_src, ip_dst, sport, dport, flag,
                         seq, ack, len, summary, insert_time)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """

                # 分批插入，避免一次性构建所有 row 占用过多内存并突破 pyodbc 单次参数上限
                total = len(self.filtered_pkts)
                inserted = 0
                for batch_start in range(0, total, self.DB_BATCH_SIZE):
                    batch_end = min(batch_start + self.DB_BATCH_SIZE, total)
                    rows = []
                    for j in range(batch_start, batch_end):
                        rows.append(self._packet_to_row(
                            self.pcap_path, self.filtered_pkts[j], pcap_hash
                        ))
                    cursor.executemany(sql, rows)
                    inserted += len(rows)

                    # 动态更新进度条
                    if total > 0:
                        progress = (inserted / total) * 100
                        self.root.after(0, lambda p=progress: self._update_progress(p))

                conn.commit()  # 提交所有事务
                self.root.after(0, lambda i=inserted: (
                    self.show_text.insert("end", f"已成功保存 {i} 条报文至 SQL Server 表 [{table_name}]\n"),
                    messagebox.showinfo("成功", f"已成功保存 {i} 条报文到 SQL Server")
                ))
            except Exception as e:
                if conn:
                    try:
                        conn.rollback()  # 发生任何异常，全部事务强制回滚
                    except Exception:
                        pass
                err_msg = str(e)
                self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", f"保存数据库失败：{m}"))
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                self.root.after(0, lambda: (
                    self._set_busy(False),
                    self._update_progress(100)
                ))

        threading.Thread(target=_save_db_task, daemon=True).start()

    def analyze_connections(self) -> None:
        if not self.all_pkts:
            messagebox.showerror("错误", "请先选择并加载抓包文件！")
            return
        if self._busy:
            messagebox.showwarning("提示", "上一个任务正在执行中，请稍候")
            return

        self._set_busy(True)
        self._update_progress(0)
        base = str(pathlib.Path(self.pcap_path).with_suffix(""))
        normal_path = base + "_正常.txt"
        error_path = base + "_异常.txt"
        self.show_text.insert("end", f"\n正在分析TCP连接（后台运行中）...\n")
        self.root.update()

        def _analyze_task():
            try:
                def get_flags(pkt) -> str:
                    if TCP in pkt:
                        return pkt[TCP].sprintf("%TCP.flags%")
                    return ""

                connections: OrderedDict = OrderedDict()
                active_instances: Dict[tuple, Dict] = {}
                total_pkts_count = len(self.all_pkts)

                for i, pkt in enumerate(self.all_pkts):
                    # 动态更新进度条
                    if total_pkts_count > 0 and i % 1000 == 0:
                        progress = (i / total_pkts_count) * 100
                        self.root.after(0, lambda p=progress: self._update_progress(p))

                    # 兼容 IPv4 和 IPv6 上的 TCP 数据包
                    if not (TCP in pkt and (IP in pkt or IPv6 in pkt)):
                        continue

                    if IP in pkt:
                        src_ip, dst_ip = pkt[IP].src, pkt[IP].dst
                    else:
                        src_ip, dst_ip = pkt[IPv6].src, pkt[IPv6].dst

                    sport, dport = pkt[TCP].sport, pkt[TCP].dport
                    pkt_time = float(pkt.time) if hasattr(pkt, 'time') else 0.0
                    flags = get_flags(pkt)

                    # 将源和目的排序以对齐双向流量为同一个四元组
                    endpoint_a, endpoint_b = sorted([(src_ip, sport), (dst_ip, dport)])
                    four_tuple = (endpoint_a[0], endpoint_a[1], endpoint_b[0], endpoint_b[1])

                    # 切分 TCP 生命周期的实例
                    if four_tuple not in active_instances:
                        active_instances[four_tuple] = {"instance_id": 0, "last_time": pkt_time}
                    else:
                        inst = active_instances[four_tuple]
                        # 如果检测到新的 SYN 包（不含 ACK 才是纯发起连接的 SYN）或者时间差过大
                        # 则判定为新起会话实例
                        is_new_syn = ("S" in flags and "A" not in flags)
                        time_gap = pkt_time - inst["last_time"]
                        # 仅在正向时间差超过阈值时切分会话
                        # 负时间差（乱序/多接口合并抓包）不触发切分，避免误判
                        if is_new_syn or (time_gap > 0 and time_gap > self.TCP_SESSION_TIMEOUT):
                            inst["instance_id"] += 1
                            inst["last_time"] = pkt_time  # 彻底重置为新会话起点时间
                        else:
                            # 跟踪最新时间戳，确保时间差计算基于已见到的最大值
                            inst["last_time"] = max(pkt_time, inst["last_time"])

                    instance_id = active_instances[four_tuple]["instance_id"]
                    key = (four_tuple[0], four_tuple[1], four_tuple[2], four_tuple[3], instance_id)

                    if key not in connections:
                        connections[key] = []
                    direction = f"{src_ip}:{sport}->{dst_ip}:{dport}"
                    connections[key].append((i, direction, flags, pkt_time))

                normal_lines: list[str] = []
                error_lines: list[str] = []
                total_pkts = sum(len(v) for v in connections.values())
                header = [
                    f"分析时间：{datetime.datetime.now()}",
                    f"抓包文件：{self.pcap_path}",
                    f"总报文数：{total_pkts}",
                ]

                incorrect: list[tuple] = []
                for key, pkts_list in connections.items():
                    src_ip, src_port, dst_ip, dst_port, inst_id = key
                    flags_list = [f[2] for f in pkts_list]
                    flags_seq = " ".join(flags_list)
                    short_name = f"{src_ip}:{src_port}<->{dst_ip}:{dst_port}"

                    # 提取状态特征
                    has_synack = any("S" in f and "A" in f for f in flags_list)
                    has_rst = any("R" in f for f in flags_list)
                    has_fin = any("F" in f for f in flags_list)

                    # 判断首个包是否包含 SYN 且非 ACK (纯发起 SYN)
                    is_syn_start = ("S" in flags_list[0] and "A" not in flags_list[0]) if flags_list else False

                    if not is_syn_start:
                        # 捕获前已建立的连接，容忍其存在
                        if has_rst:
                            status = ConnStatus.PRE_ESTAB_RST
                        elif has_fin:
                            status = ConnStatus.PRE_ESTAB_CLOSED
                        else:
                            status = ConnStatus.PRE_ESTAB_ONGOING
                    else:
                        # 从完整三次握手 SYN 开始的连接
                        if not has_synack:
                            if has_rst:
                                status = ConnStatus.CONN_REFUSED
                            else:
                                status = ConnStatus.NO_RESPONSE
                        else:
                            # 找到了 SYN-ACK，检查后续是否有 ACK 确认 (包括普通的 A 或者带数据的 PA 等)
                            sa_idx = -1
                            for idx, flg in enumerate(flags_list):
                                if "S" in flg and "A" in flg:
                                    sa_idx = idx
                                    break

                            has_ack_after_sa = False
                            if sa_idx != -1:
                                has_ack_after_sa = any("A" in f for f in flags_list[sa_idx + 1:])

                            if not has_ack_after_sa:
                                status = ConnStatus.HANDSHAKE_FAILED
                            else:
                                # 握手已成功建立
                                if has_rst:
                                    # 刚建立就立刻断开 (例如包数很少) 判定为异常
                                    if len(flags_list) <= self.RST_MIN_PACKETS:
                                        status = ConnStatus.RST_AFTER_HANDSHAKE
                                    else:
                                        status = ConnStatus.ESTABLISHED_RST
                                elif has_fin:
                                    status = ConnStatus.CLOSED
                                else:
                                    status = ConnStatus.ESTABLISHED_ONGOING

                    # 判定哪些是异常连接（使用枚举集合比较，避免字符串拼写错误）
                    is_error = status not in _NORMAL_STATUSES
                    if is_error:
                        incorrect.append((short_name, flags_seq, status.value, len(pkts_list)))

                    conn_lines = [
                        f"{short_name:^50} | {flags_seq[:70]:^55} | {status.value:^20}",
                        f"  Instance: {inst_id} | Packets: {len(pkts_list)}",
                    ]
                    for p in pkts_list:
                        idx, direction, fl, t = p
                        ts = datetime.datetime.fromtimestamp(float(t)).strftime("%H:%M:%S.%f")
                        conn_lines.append(f"    [{idx:>4}] {ts} {direction} [{fl}]")
                    conn_lines.append("")

                    target = error_lines if is_error else normal_lines
                    target.extend(conn_lines)

                # 写正常连接文件
                with open(normal_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(header + [""] + normal_lines))

                # 写异常连接文件
                error_header = header + [
                    f"总连接数：{len(connections)}",
                    f"正常连接数：{len(connections) - len(incorrect)}",
                    f"异常连接数：{len(incorrect)}",
                    "",
                ]
                with open(error_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(error_header + error_lines))

                result_msg = (f"分析完成，共 {len(connections)} 条连接，"
                              f"异常 {len(incorrect)} 条\n"
                              f"正常→{normal_path}\n"
                              f"异常→{error_path}\n")
                info_msg = (f"分析完成\n正常连接：{len(connections) - len(incorrect)} 条→{normal_path}\n"
                            f"异常连接：{len(incorrect)} 条→{error_path}")
                self.root.after(0, lambda r=result_msg, i=info_msg: (
                    self.show_text.insert("end", r),
                    messagebox.showinfo("成功", i),
                ))

            except Exception as e:
                err_msg = f"分析失败：{str(e)}"
                self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", m))
            finally:
                self.root.after(0, lambda: (
                    self._set_busy(False),
                    self._update_progress(100)
                ))

        threading.Thread(target=_analyze_task, daemon=True).start()

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
        ttk.Entry(win, textvariable=port_var, width=15).grid(row=1, column=1, sticky="w", padx=15, pady=5)

        # Database
        ttk.Label(win, text="数据库名 (Database):").grid(row=2, column=0, sticky="w", padx=15, pady=5)
        database_var = tk.StringVar(value=cfg.get("database", ""))
        ttk.Entry(win, textvariable=database_var, width=40).grid(row=2, column=1, sticky="w", padx=15, pady=5)

        # Username
        ttk.Label(win, text="用户名 (Username):").grid(row=3, column=0, sticky="w", padx=15, pady=5)
        username_var = tk.StringVar(value=cfg.get("username", ""))
        ttk.Entry(win, textvariable=username_var, width=40).grid(row=3, column=1, sticky="w", padx=15, pady=5)
        ttk.Label(win, text="（留空表示使用 Windows 身份验证）", foreground="gray").grid(row=3, column=2, sticky="w", padx=(0,15))

        # Password
        ttk.Label(win, text="密码 (Password):").grid(row=4, column=0, sticky="w", padx=15, pady=5)
        password_var = tk.StringVar(value=cfg.get("password", ""))
        ttk.Entry(win, textvariable=password_var, width=40, show="*").grid(row=4, column=1, sticky="w", padx=15, pady=5)
        if env_pass_present:
            ttk.Label(win, text="（已从环境变量读取）", foreground="green").grid(row=4, column=2, sticky="w", padx=(0,15))

        # Driver
        ttk.Label(win, text="ODBC 驱动 (Driver):").grid(row=5, column=0, sticky="w", padx=15, pady=5)
        driver_var = tk.StringVar(value=cfg.get("driver", "ODBC Driver 17 for SQL Server"))
        drivers = [
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 18 for SQL Server",
            "SQL Server",
            "ODBC Driver 13 for SQL Server"
        ]
        driver_combo = ttk.Combobox(win, textvariable=driver_var, values=drivers, width=37)
        driver_combo.grid(row=5, column=1, sticky="w", padx=15, pady=5)

        # Table Name
        ttk.Label(win, text="表名 (Table):").grid(row=6, column=0, sticky="w", padx=15, pady=5)
        table_var = tk.StringVar(value=cfg.get("table_name", "packets"))
        ttk.Entry(win, textvariable=table_var, width=40).grid(row=6, column=1, sticky="w", padx=15, pady=5)

        def save():
            new_cfg: Dict[str, Any] = {
                "server": server_var.get().strip(),
                "port": port_var.get().strip(),
                "database": database_var.get().strip(),
                "username": username_var.get().strip(),
                "password": password_var.get().strip(),
                "driver": driver_var.get().strip(),
                "table_name": table_var.get().strip(),
            }
            self._save_db_config(new_cfg)
            messagebox.showinfo("成功", "数据库设置已保存")
            win.destroy()

        ttk.Button(win, text="保存", command=save).grid(row=7, column=1, sticky="w", padx=15, pady=15)

    # ---- LLM 相关 ----

    @property
    def _llm_config_path(self) -> str:
        return os.path.join(os.path.dirname(__file__), "llm_config.json")

    def _load_llm_config(self) -> Dict[str, Any]:
        """加载 LLM 配置，环境变量优先覆盖文件值（与主项目 app.py 的 COMPANY_LLM_* 保持一致）"""
        path = self._llm_config_path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-3.5-turbo"}

        # 环境变量优先覆盖文件中的值
        env_key = os.environ.get("COMPANY_LLM_KEY", "")
        env_url = os.environ.get("COMPANY_LLM_URL", "")
        env_model = os.environ.get("COMPANY_MODEL_NAME", "")
        if env_key:
            cfg["api_key"] = env_key
        if env_url:
            cfg["base_url"] = env_url
        if env_model:
            cfg["model"] = env_model

        return cfg

    def _save_llm_config(self, config: Dict[str, Any]) -> None:
        """保存 LLM 配置到文件。若 API Key 来自环境变量则不重复写入文件，降低明文泄露风险。"""
        path = self._llm_config_path
        save_config = dict(config)

        # 如果环境变量中已有 API Key 且与当前值相同，则不写入文件
        env_key = os.environ.get("COMPANY_LLM_KEY", "")
        if env_key and save_config.get("api_key") == env_key:
            save_config["api_key"] = ""

        with open(path, "w", encoding="utf-8") as f:
            json.dump(save_config, f, ensure_ascii=False, indent=2)
        # 尝试限制配置文件权限（Windows 上效果有限但聊胜于无）
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def open_llm_settings(self) -> None:
        cfg = self._load_llm_config()
        env_key_present = bool(os.environ.get("COMPANY_LLM_KEY", ""))

        win = tk.Toplevel(self.root)
        win.title("LLM 设置")
        win.geometry("560x320")
        win.resizable(False, False)
        win.grab_set()  # 启用模态，锁定父窗口焦点

        ttk.Label(win, text="Base URL:").grid(row=0, column=0, sticky="w", padx=10, pady=(15,5))
        url_var = tk.StringVar(value=cfg.get("base_url", ""))
        ttk.Entry(win, textvariable=url_var, width=55).grid(row=0, column=1, padx=10, pady=(15,5))

        ttk.Label(win, text="API Key:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        key_var = tk.StringVar(value=cfg.get("api_key", ""))
        ttk.Entry(win, textvariable=key_var, width=55, show="*").grid(row=1, column=1, padx=10, pady=5)
        if env_key_present:
            ttk.Label(win, text="（已从环境变量 COMPANY_LLM_KEY 读取）",
                       foreground="green").grid(row=1, column=2, sticky="w", padx=5)

        ttk.Label(win, text="Model ID:").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        model_var = tk.StringVar(value=cfg.get("model", ""))
        ttk.Entry(win, textvariable=model_var, width=55).grid(row=2, column=1, padx=10, pady=5)

        # enable_thinking 配置（高级选项，用于特定 LLM 服务）
        thinking_var = tk.BooleanVar(value=bool(cfg.get("enable_thinking", False)))
        ttk.Checkbutton(
            win, text="启用 Thinking 模式（仅部分 LLM 服务支持）",
            variable=thinking_var
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=5)

        def save():
            new_cfg: Dict[str, Any] = {
                "base_url": url_var.get().strip(),
                "api_key": key_var.get().strip(),
                "model": model_var.get().strip(),
            }
            if thinking_var.get():
                new_cfg["enable_thinking"] = True
            # 不保存 enable_thinking=False 以保持配置文件简洁
            self._save_llm_config(new_cfg)
            messagebox.showinfo("成功", "LLM 设置已保存")
            win.destroy()

        ttk.Button(win, text="保存", command=save).grid(row=4, column=1, sticky="e", padx=10, pady=15)

    def _call_llm(self, prompt: str) -> str:
        """调用 LLM API，对可重试的 HTTP 错误（429/500/502/503）和网络错误做指数退避重试"""
        cfg = self._load_llm_config()
        base_url = cfg.get("base_url", "").rstrip("/")
        api_key = cfg.get("api_key", "")
        model = cfg.get("model", "")

        if not api_key:
            raise ValueError("请先在 LLM 设置中配置 API Key，或设置环境变量 COMPANY_LLM_KEY")

        url = f"{base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": self.LLM_MAX_TOKENS,
        }
        # chat_template_kwargs 为特定 LLM 服务参数，仅在配置中显式启用时发送
        if cfg.get("enable_thinking"):
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        data = json.dumps(payload).encode("utf-8")

        last_error: Optional[Exception] = None
        for attempt in range(self.LLM_MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("Authorization", f"Bearer {api_key}")

                # 注意：不要绕过 SSL 证书验证（禁止使用 ssl._create_unverified_context）
                with urllib.request.urlopen(req, timeout=self.LLM_API_TIMEOUT) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    return result["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"API 请求失败 (HTTP {e.code}): {err_body[:200]}")
                if e.code not in self.LLM_RETRYABLE_CODES:
                    raise last_error
                # 可重试错误码，等待后重试
            except urllib.error.URLError as e:
                last_error = RuntimeError(f"网络错误: {e.reason}")
                # 网络错误也可以重试
            except Exception as e:
                raise RuntimeError(f"未知错误: {str(e)}")

            # 指数退避等待
            if attempt < self.LLM_MAX_RETRIES:
                wait = self.LLM_RETRY_BACKOFF * (2 ** attempt)
                time.sleep(wait)

        # 重试耗尽
        raise last_error or RuntimeError("LLM API 调用失败（已达最大重试次数）")

    def _show_llm_result(self, reply: str, error_path: str, error_count: int) -> None:
        """在主线程中显示 LLM 分析结果"""
        self.show_text.delete("1.0", "end")
        analysis = (
            "=" * 80 + "\n"
            "LLM 错误连接分析报告\n"
            "=" * 80 + "\n"
            f"分析时间：{datetime.datetime.now()}\n"
            f"异常文件：{error_path}\n"
            f"错误连接总数：{error_count}\n\n"
            + reply
        )
        self.show_text.insert("end", analysis + "\n\n")
        messagebox.showinfo("完成", f"LLM 分析完成，共 {error_count} 条错误连接")

    def llm_analyze_errors(self) -> None:
        if self._busy:
            messagebox.showwarning("提示", "当前有任务正在执行，请稍候")
            return
        if not self.pcap_path:
            messagebox.showerror("错误", "请先选择并加载抓包文件！")
            return

        error_path = str(pathlib.Path(self.pcap_path).with_suffix("")) + "_异常.txt"
        if not os.path.exists(error_path):
            messagebox.showerror("错误", f"找不到异常文件，请先执行分析异常连接：\n{error_path}")
            return

        self.show_text.insert("end", f"\n正在分析错误特征...\n")
        self.root.update()
        self._set_busy(True)

        try:
            errors: list[dict] = []
            current: Optional[dict] = None
            
            # 使用 for line in f 流式按行读取解析，内存消耗降低为常数级 O(1)
            with open(error_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "<->" in line and " | " in line:
                        if current:
                            errors.append(current)
                        parts = line.split(" | ")
                        conn = parts[0].strip()
                        flags = parts[1].strip() if len(parts) > 1 else ""
                        status = parts[2].strip() if len(parts) > 2 else ""
                        current = {"conn": conn, "status": status, "flags": flags}
                    elif current and "Packets:" in line:
                        try:
                            current["packet_count"] = int(line.split(":")[-1].strip())
                        except (ValueError, IndexError):
                            pass  # 非数字格式，跳过
            if current:
                errors.append(current)

            if not errors:
                self.show_text.insert("end", "未发现错误连接\n")
                self._set_busy(False)
                return

            status_cnt = Counter(e["status"] for e in errors)
            ip_cnt: Counter = Counter()
            port_cnt: Counter = Counter()
            flag_cnt: Counter = Counter()
            for e in errors:
                for side in e["conn"].split("<->"):
                    parts = side.strip().rsplit(":", 1)
                    if len(parts) == 2:
                        ip_cnt[parts[0]] += 1
                        try:
                            port_cnt[int(parts[1])] += 1
                        except (ValueError, IndexError):
                            port_cnt[parts[1]] += 1
                if e["flags"]:
                    flag_cnt[e["flags"]] += 1

            def fmt_counter(c: Counter, total: int, n: int = 10) -> str:
                lst = c.most_common(n)
                return "\n".join(f"  {k}: {v}次 ({v/total*100:.1f}%)" for k, v in lst) if lst else "  无"

            # 优化后的样本提取：单次线性循环
            samples: list[dict] = []
            type_counts: Dict[str, int] = {}
            for e in sorted(errors, key=lambda x: x["status"]):
                st = e["status"]
                type_counts[st] = type_counts.get(st, 0) + 1
                if type_counts[st] <= self.LLM_SAMPLE_PER_TYPE:
                    samples.append(e)
                    if len(samples) >= self.LLM_MAX_SAMPLES:
                        break
            sample_lines = [f"  连接: {e['conn']}  标志: {e['flags'][:60]}  状态: {e['status']}" for e in samples]

            prompt = f"""你是一个网络专家，分析以下 TCP 连接异常数据，找出错误连接的共同特征和原因。

【统计概览】
错误连接总数：{len(errors)}

【错误类型分布】
{fmt_counter(status_cnt, len(errors))}

【涉及最多的 IP（Top 10）】
{fmt_counter(ip_cnt, len(errors))}

【涉及最多的端口（Top 10）】
{fmt_counter(port_cnt, len(errors))}

【常见标志序列（Top 10）】
{fmt_counter(flag_cnt, len(errors))}

【样本连接（每种错误类型取 {self.LLM_SAMPLE_PER_TYPE} 条，共 {len(samples)} 条）】：
{chr(10).join(sample_lines) if sample_lines else "  无"}

请基于以上统计和样本数据，分析：
1. 主要错误类型及占比
2. 涉及最多的 IP 和端口，是否有明显聚集
3. 可能的原因推断（如：某台设备异常、扫描攻击、防火墙拦截、NAT 问题等）
4. 建议的排查方向

请给出专业、全面的分析报告。"""

            self.show_text.delete("1.0", "end")
            self.show_text.insert("end", "正在调用 LLM 分析（后台运行中，请稍候）...\n")
            self.root.update()

            # 在 try 块内提前捕获需要的值，避免 lambda 闭包引用问题
            error_count = len(errors)

            def _llm_task():
                try:
                    reply = self._call_llm(prompt)
                    # 使用默认参数绑定当前值，避免 Python 3 except 块退出后变量被清除
                    self.root.after(0, lambda r=reply: self._show_llm_result(r, error_path, error_count))
                except Exception as ex:
                    # 关键修复：先将异常信息转为字符串，再通过默认参数传入 lambda
                    # Python 3 的 except 子句退出后会删除 ex 变量（PEP 3110），
                    # 如果 lambda 直接引用 ex，延迟执行时会触发 NameError
                    err_msg = f"LLM 分析失败：{str(ex)}"
                    self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", m))
                finally:
                    self.root.after(0, lambda: self._set_busy(False))

            threading.Thread(target=_llm_task, daemon=True).start()

        except Exception as e:
            messagebox.showerror("错误", f"分析失败：{str(e)}")
            self._set_busy(False)


if __name__ == "__main__":
    root = tk.Tk()
    app = WinPcapFilter(root)
    root.mainloop()