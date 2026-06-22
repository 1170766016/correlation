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


class ConnStatus(Enum):
    """TCP 连接状态枚举，包含中文详细描述，并支持正常/异常状态分类"""
    # ==== 正常/非异常状态 (Normal Statuses) ====
    CLOSED = ("CLOSED", "正常关闭：连接正常建立并以 FIN 挥手关闭")
    ESTABLISHED_ONGOING = ("ESTABLISHED_ONGOING", "正常持续：连接正常建立，直到抓包结束仍在传输")
    PRE_ESTAB_CLOSED = ("PRE_ESTAB_CLOSED", "捕获前已建连-正常关闭：抓包前连接已建立，后续以 FIN 正常关闭")
    PRE_ESTAB_ONGOING = ("PRE_ESTAB_ONGOING", "捕获前已建连-正常持续：抓包前连接已建立，整个抓包期间持续进行")
    ESTABLISHED_RST = ("ESTABLISHED_RST", "正常重置：连接正常建立并传输较多数据后，以 RST 释放")

    # ==== 异常状态 (Abnormal Statuses) ====
    CONN_REFUSED = ("CONN_REFUSED", "连接被拒：客户端发起 SYN 后直接被对端 RST 拒绝")
    NO_RESPONSE = ("NO_RESPONSE", "连接无响应：客户端发起 SYN 后未收到对端任何报文（可能超时或防火墙丢包）")
    HANDSHAKE_FAILED = ("HANDSHAKE_FAILED", "握手失败：SYN 与 SYN-ACK 之后未收到最终的 ACK 确认")
    RST_AFTER_HANDSHAKE = ("RST_AFTER_HANDSHAKE", "建连后即被重置：三次握手刚完成即被 RST 强行中断")
    PRE_ESTAB_RST = ("PRE_ESTAB_RST", "捕获前已建连-异常重置：抓包前已建立，但在中途被 RST 异常中断")
    DATA_TRANSFER_TIMEOUT = ("DATA_TRANSFER_TIMEOUT", "传输超时：数据发送后无响应，发生严重重传")
    HALF_CLOSED_HANG = ("HALF_CLOSED_HANG", "半挥手卡死：一端发送 FIN 后，对端长期无 FIN 回应")
    SYN_RETRANSMISSION = ("SYN_RETRANSMISSION", "握手重传：TCP握手期间发生SYN或SYN-ACK重传（网络质量差或丢包）")
    ZERO_WINDOW = ("ZERO_WINDOW", "零窗口：连接期间出现TCP零窗口（对端缓存满，传输挂起）")

    def __new__(cls, code: str, desc: str):
        obj = object.__new__(cls)
        obj._value_ = code
        obj.desc = desc
        return obj

    @property
    def is_normal(self) -> bool:
        """判定该状态是否属于正常连接状态"""
        return self in {
            ConnStatus.CLOSED,
            ConnStatus.ESTABLISHED_ONGOING,
            ConnStatus.PRE_ESTAB_CLOSED,
            ConnStatus.PRE_ESTAB_ONGOING,
            ConnStatus.ESTABLISHED_RST
        }


class WinPcapFilter:
    # ---- 常量定义 ----
    TCP_SESSION_TIMEOUT = 60.0      # 秒，超过此间隔判定为新会话
    RST_MIN_PACKETS = 4             # RST 异常判定的最小包数
    HALF_CLOSED_TIMEOUT = 10.0      # 秒，FIN后对端不发送FIN的卡死判定时间
    MAX_RETRANS_THRESHOLD = 3       # 次，判定为传输超时的同一数据包最大重复发送次数
    LLM_MAX_SAMPLES = 40            # 发送给 LLM 的最大样本数
    LLM_SAMPLE_PER_TYPE = 4         # 每种错误类型的最大样本数
    LLM_API_TIMEOUT = 300           # API 调用超时（秒）
    LLM_MAX_TOKENS = 2048
    LLM_MAX_RETRIES = 3             # API 调用最大重试次数
    LLM_RETRY_BACKOFF = 1.0         # 重试初始退避时间（秒）
    LLM_RETRYABLE_CODES = {429, 500, 502, 503}  # 可重试的 HTTP 状态码
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
        self.pcap_path: str = ""
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

        # 过滤规则区
        filter_frame = ttk.LabelFrame(self.root, text="过滤规则（Wireshark语法）")
        filter_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(filter_frame, text="预设规则：").grid(row=0, column=0)
        self.rule = tk.StringVar(value='http.connection == "close"')
        rules = [
            'http.connection == "close"',
            "tcp port 80 or 443",
            "icmp",
            "tcp",
            "udp",
            "ip host 192.168.1.100",
            "udp port 53"
        ]
        ttk.Combobox(filter_frame, textvariable=self.rule, values=rules, width=45).grid(row=0, column=1, padx=5)
        ttk.Button(filter_frame, text="开始过滤", command=self.do_filter).grid(row=1, column=0, columnspan=2, pady=5)

        # 操作按钮区（置于过滤规则下方，避免服务器窗口被结果区挤压导致按钮不可见）
        btn_frame = ttk.LabelFrame(self.root, text="操作")
        btn_frame.pack(fill="x", padx=10, pady=5)
        for col in range(4):
            btn_frame.columnconfigure(col, weight=1)
        ttk.Button(btn_frame, text="导出文本", command=self.export_txt).grid(row=0, column=0, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="保存抓包", command=self.save_pcap).grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="保存到数据库", command=self.save_to_db).grid(row=0, column=2, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="分析异常连接", command=self.analyze_connections).grid(row=0, column=3, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="LLM分析错误", command=self.llm_analyze_errors).grid(row=1, column=0, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="LLM设置", command=self.open_llm_settings).grid(row=1, column=1, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="索引维护", command=self.maintain_indexes).grid(row=1, column=2, sticky="ew", padx=5, pady=4)
        ttk.Button(btn_frame, text="数据库设置", command=self.open_db_settings).grid(row=1, column=3, sticky="ew", padx=5, pady=4)

        # 进度条 + 取消按钮（任务执行时点击可中断后台遍历）
        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(progress_frame, text="取消", command=self._cancel_task, state="disabled")
        self.cancel_btn.pack(side="right", padx=(5, 0))

        # 结果显示区（置于底部并 expand，窗口高度不足时压缩本区而非按钮）
        result_frame = ttk.LabelFrame(self.root, text="过滤结果")
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

    def _determine_tcp_status(self, pkts_list: list[tuple]) -> ConnStatus:
        """根据 TCP 会话的所有报文特征序列判定连接状态"""
        if not pkts_list:
            return ConnStatus.PRE_ESTAB_ONGOING

        flags_list = [p[2] for p in pkts_list]
        
        # 判断首个包是否包含 SYN 且非 ACK (纯发起 SYN)
        is_syn_start = ("S" in flags_list[0] and "A" not in flags_list[0])

        has_synack = any("S" in f and "A" in f for f in flags_list)
        has_rst = any("R" in f for f in flags_list)
        has_fin = any("F" in f for f in flags_list)

        # 1. 基础状态判定
        status = None
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

        # 2. 深度异常状态检测（仅针对已建立或捕获前建立且未被 RST 强行断开的连接）
        if status in {ConnStatus.CLOSED, ConnStatus.ESTABLISHED_ONGOING, 
                      ConnStatus.PRE_ESTAB_CLOSED, ConnStatus.PRE_ESTAB_ONGOING}:
            
            # 检测 A. 半关闭挂起 (HALF_CLOSED_HANG)
            fin_indices = [i for i, p in enumerate(pkts_list) if "F" in p[2]]
            if fin_indices:
                first_fin_idx = fin_indices[0]
                first_fin_dir = pkts_list[first_fin_idx][1]
                # 检查首个 FIN 之后，是否有相反方向的 FIN
                has_reverse_fin = False
                for idx in fin_indices:
                    if pkts_list[idx][1] != first_fin_dir:
                        has_reverse_fin = True
                        break
                if not has_reverse_fin:
                    time_span_after_fin = pkts_list[-1][3] - pkts_list[first_fin_idx][3]
                    if time_span_after_fin >= self.HALF_CLOSED_TIMEOUT:
                        return ConnStatus.HALF_CLOSED_HANG

            # 检测 B. 数据传输超时 / 严重重传 (DATA_TRANSFER_TIMEOUT)
            directions = set(p[1] for p in pkts_list)
            for direct in directions:
                # 提取该方向的所有数据包
                dir_pkts = [p for p in pkts_list if p[1] == direct]
                # 过滤出含有数据载荷且非握手挥手的包：排除 SYN (S) 和 FIN (F)
                data_packets = [p for p in dir_pkts if p[6] > 0 and "S" not in p[2] and "F" not in p[2]]
                if not data_packets:
                    continue
                
                # 统计各 Seq 的出现次数
                seq_counts = Counter(p[4] for p in data_packets)
                # 找出重传次数 >= MAX_RETRANS_THRESHOLD 的 Seq
                retransmitted_seqs = [seq for seq, count in seq_counts.items() if count >= self.MAX_RETRANS_THRESHOLD]
                
                for r_seq in retransmitted_seqs:
                    first_tx_idx = -1
                    r_len = 0
                    for idx, p in enumerate(pkts_list):
                        if p[1] == direct and p[4] == r_seq and p[6] > 0:
                            first_tx_idx = idx
                            r_len = p[6]
                            break
                    
                    if first_tx_idx != -1:
                        # 检查在此之后是否有相反方向的包来合理 ACK 确认该数据
                        ack_confirmed = False
                        for p in pkts_list[first_tx_idx + 1:]:
                            if p[1] != direct: # 相反方向
                                # 正常的 ACK 序号应当大于等于发送包的 Seq + Payload_Len
                                if p[5] >= r_seq + r_len:
                                    ack_confirmed = True
                                    break
                        if not ack_confirmed:
                            return ConnStatus.DATA_TRANSFER_TIMEOUT

            # 检测 C. 零窗口 (ZERO_WINDOW)
            zero_win_pkts = [p for p in pkts_list if "A" in p[2] and "R" not in p[2] and "S" not in p[2] and len(p) > 7 and p[7] == 0]
            if zero_win_pkts:
                return ConnStatus.ZERO_WINDOW

            # 检测 D. SYN / SYN-ACK 重传 (SYN_RETRANSMISSION)
            syn_pkts = [p for p in pkts_list if "S" in p[2] and "A" not in p[2]]
            syn_ack_pkts = [p for p in pkts_list if "S" in p[2] and "A" in p[2]]
            syn_seqs = Counter(p[4] for p in syn_pkts)
            syn_ack_seqs = Counter(p[4] for p in syn_ack_pkts)
            if any(count >= 2 for count in syn_seqs.values()) or any(count >= 2 for count in syn_ack_seqs.values()):
                return ConnStatus.SYN_RETRANSMISSION

        return status


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

        # http.connection == "close" 等 Wireshark 显示过滤（非 BPF，由内置解析器支持）
        if rule.startswith("http.connection"):
            m = re.match(r'http\.connection\s*==\s*"([^"]*)"', rule)
            if not m:
                return False
            return self._match_http_header(pkt, "connection", m.group(1))

        # 未知规则，不进行匹配
        return False

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
        path = filedialog.askopenfilename(
            filetypes=[("抓包文件", "*.pcap *.pcapng"), ("所有文件", "*.*")]
        )
        if path:
            self.pcap_path = path
            self.path_var.set(path)
            self.show_text.delete(1.0, "end")
            self.show_text.insert("end", f"已选择：{path}\n")
            self._set_busy(True)
            self._update_progress(0)
            # 切到不确定模式：加载阶段不知道总包数，进度条来回滚动表示在干活
            try:
                self.progress_bar.config(mode="indeterminate")
                self.progress_bar.start(15)  # 每 15ms 推进一步
            except tk.TclError:
                pass

            # 记录开始时间，用于显示加载耗时
            load_start_time = time.time()

            def _load_task():
                try:
                    # 第一阶段：流式扫描计数，实时显示已扫描包数（避免用户以为卡住）
                    count = 0
                    last_report = 0
                    with PcapReader(self.pcap_path) as reader:
                        for i, _ in enumerate(reader):
                            count += 1
                            # 中断检查点
                            if i % 10000 == 0 and self._cancel_event.is_set():
                                self.root.after(0, lambda: self.show_text.insert("end", "⚠ 已取消文件加载\n"))
                                return
                            # 每 50000 包或每 1 秒报告一次进度（取较大间隔，避免 UI 抖动）
                            if count - last_report >= 50000:
                                last_report = count
                                elapsed = time.time() - load_start_time
                                rate = count / elapsed if elapsed > 0 else 0
                                msg = f"  正在扫描报文... 已读取 {count:,} 条 ({rate:,.0f} 包/秒, 已用 {elapsed:.1f}s)\n"
                                self.root.after(0, lambda m=msg: (
                                    self.show_text.delete("end-2l", "end"),  # 删除上一行进度
                                    self.show_text.insert("end", m),
                                    self.show_text.see("end")
                                ))

                    if self._cancel_event.is_set():
                        return

                    elapsed = time.time() - load_start_time
                    self.all_pkts_count = count
                    self.all_pkts = []

                    # 停止不确定模式进度条
                    self.root.after(0, lambda: (
                        self.progress_bar.stop(),
                        self.progress_bar.config(mode="determinate"),
                        self._update_progress(100),
                        self.show_text.delete("end-3l", "end"),  # 清掉进度行
                        self.show_text.insert("end",
                            f"✓ 文件加载完成，共 {count:,} 条报文（耗时 {elapsed:.1f}s）\n"
                        ),
                        self.show_text.see("end")
                    ))

                    # 第二阶段：自动触发过滤分析，让用户立即看到结果而不是干等
                    self.root.after(0, lambda: self.show_text.insert(
                        "end", "→ 自动开始过滤分析...\n"
                    ))
                    # 用 after 延迟一下，让 UI 先刷新
                    self.root.after(100, lambda: self.do_filter(auto_triggered=True))

                except Exception as e:
                    err_msg = str(e)
                    self.root.after(0, lambda m=err_msg: (
                        self.progress_bar.stop(),
                        self.progress_bar.config(mode="determinate"),
                        messagebox.showerror("错误", f"加载文件失败：{m}")
                    ))
                    self.all_pkts_count = 0
                    self.all_pkts = []
                finally:
                    self.root.after(0, lambda: (
                        self._set_busy(False),
                    ))

            threading.Thread(target=_load_task, daemon=True).start()

    def do_filter(self, auto_triggered: bool = False) -> None:
        if not self.pcap_path or not hasattr(self, "all_pkts_count") or self.all_pkts_count == 0:
            messagebox.showerror("错误", "请先选择并加载抓包文件！")
            return
        if self._busy:
            # 自动触发的过滤不弹窗警告（避免加载完紧接着自动过滤时被 busy 拦截）
            if not auto_triggered:
                messagebox.showwarning("提示", "上一个任务正在执行中，请稍候")
            return

        rule = self.rule.get().strip()
        if not auto_triggered:
            self.show_text.delete(1.0, "end")
        self.show_text.insert("end", f"正在过滤：{rule or '（无规则，加载全部）'}\n")
        self.show_text.see("end")
        self._set_busy(True)
        self._update_progress(0)

        filter_start_time = time.time()

        def _filter_task():
            try:
                # sniff 是黑盒，无法实时报告进度，先用不确定模式让进度条滚动
                self.root.after(0, lambda: (
                    self.progress_bar.config(mode="indeterminate"),
                    self.progress_bar.start(15)
                ))
                # 优先尝试使用 sniff 进行标准的 BPF 过滤，若失败则回退到手写的 _match_filter 进行兼容
                if not rule:
                    if self.all_pkts_count > self.FILTER_MAX_PACKETS:
                        msg = f"⚠ 抓包文件过大（共 {self.all_pkts_count} 条报文），无过滤规则时仅加载前 {self.FILTER_MAX_PACKETS} 条报文以防止内存溢出。\n\n"
                        self.root.after(0, lambda m=msg: self.show_text.insert("end", m))
                        with PcapReader(self.pcap_path) as reader:
                            filtered = []
                            for i in range(self.FILTER_MAX_PACKETS):
                                # 中断检查点：每 5000 包查一次取消标志
                                if i % 5000 == 0 and self._cancel_event.is_set():
                                    self.root.after(0, lambda: self.show_text.insert("end", "⚠ 已取消过滤任务\n"))
                                    return
                                try:
                                    filtered.append(next(reader))
                                except StopIteration:
                                    break
                    else:
                        with PcapReader(self.pcap_path) as reader:
                            filtered = []
                            for i, p in enumerate(reader):
                                if i % 5000 == 0 and self._cancel_event.is_set():
                                    self.root.after(0, lambda: self.show_text.insert("end", "⚠ 已取消过滤任务\n"))
                                    return
                                filtered.append(p)
                else:
                    try:
                        filtered = list(sniff(offline=self.pcap_path, filter=rule, count=self.FILTER_MAX_PACKETS))
                    except Exception as bpf_err:
                        # BPF 降级时提供更清晰的提示，告知用户哪些规则可能不被支持
                        msg = (f"⚠ 标准 BPF 过滤失败，已降级为简单内置过滤器。\n"
                               f"  内置过滤器仅支持: tcp, udp, icmp, tcp port X, udp port X, ip host X, http.connection\n"
                               f"  BPF 错误: {str(bpf_err)}\n\n")
                        self.root.after(0, lambda m=msg: self.show_text.insert("end", m))
                        filtered = []
                        with PcapReader(self.pcap_path) as reader:
                            for i, p in enumerate(reader):
                                # 降级路径单线程遍历全包，必须支持中断，否则大文件 UI 假死
                                if i % 5000 == 0 and self._cancel_event.is_set():
                                    self.root.after(0, lambda: self.show_text.insert("end", "⚠ 已取消过滤任务\n"))
                                    return
                                if self._match_filter(p, rule):
                                    filtered.append(p)
                                    if len(filtered) >= self.FILTER_MAX_PACKETS:
                                        break

                self.filtered_pkts = PacketList(filtered, name="filtered") if not isinstance(filtered, PacketList) else filtered

                if len(self.filtered_pkts) >= self.FILTER_MAX_PACKETS:
                    trunc_msg = (f"⚠ 匹配报文数量已达上限（{self.FILTER_MAX_PACKETS} 条），"
                                 f"后续匹配的报文已被截断以防止内存溢出。\n"
                                 f"完整数据请缩小过滤范围或分批处理。\n\n")
                    self.root.after(0, lambda m=trunc_msg: self.show_text.insert("end", m))

                if not self.filtered_pkts:
                    self.root.after(0, lambda: self.show_text.insert("end", "未匹配到报文\n"))
                    return

                total = len(self.filtered_pkts)
                filter_elapsed = time.time() - filter_start_time
                header = (f"✓ 过滤完成，找到 {total:,} 条报文（耗时 {filter_elapsed:.1f}s）\n"
                          + "-" * 80 + "\n\n")
                self.root.after(0, lambda h=header: (
                    self.show_text.insert("end", h),
                    self.show_text.see("end")
                ))

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

                self.root.after(0, lambda: (
                    self.progress_bar.stop(),
                    self.progress_bar.config(mode="determinate"),
                    self._update_progress(100)
                ))

            except Exception as e:
                err_msg = f"过滤失败：{str(e)}"
                self.root.after(0, lambda m=err_msg: (
                    self.progress_bar.stop(),
                    self.progress_bar.config(mode="determinate"),
                    messagebox.showerror("过滤失败", m)
                ))
            finally:
                self.root.after(0, lambda: (
                    self._set_busy(False),
                ))

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
    def _packet_to_row(pcap_file: str, pkt, insert_time=None) -> tuple:
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
        # 同时内联构造 info 摘要，复用同一份层对象，避免再调用 _packet_info 造成重复 getlayer
        flag_val = None
        seq_val = None
        ack_val = None
        len_val = None
        info_parts: list[str] = []

        if tcp_layer:
            t = tcp_layer
            f_val = t.flags
            flags = []
            if f_val & 0x02: flags.append("SYN")
            if f_val & 0x10: flags.append("ACK")
            if f_val & 0x01: flags.append("FIN")
            if f_val & 0x04: flags.append("RST")
            if f_val & 0x08: flags.append("PSH")
            if f_val & 0x20: flags.append("URG")
            flag_val = " ".join(flags) if flags else str(f_val)

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

            info_parts.append(f"[{flag_val}] Seq={seq_val} Ack={ack_val} Len={payload_len}")
        elif udp_layer:
            u = udp_layer
            info_parts.append(f"Len={u.len}")

        # ICMP 层（保持与原 _packet_info 输出一致）
        if not info_parts:
            icmp_layer = pkt.getlayer(ICMP)
            if icmp_layer:
                info_parts.append(f"Type={icmp_layer.type} Code={icmp_layer.code}")

        # info 地址部分（复用已提取的层对象，不再二次 getlayer）
        port_s = f":{sport}" if (tcp_layer or udp_layer) else ""
        port_d = f":{dport}" if (tcp_layer or udp_layer) else ""
        if ip_layer:
            info_parts.append(f"{ip_src}{port_s} > {ip_dst}{port_d}")
        elif ipv6_layer:
            info_parts.append(f"{ip_src}{port_s} > {ip_dst}{port_d}")
        info_val = " ".join(info_parts)

        # 构造 summary（复用已提取字段，移除对 _packet_info 的二次调用，省去约 5 次 getlayer）
        ts_str = packet_time.strftime("%Y-%m-%d %H:%M:%S.%f") if packet_time else "N/A"
        summary_lines = [f"时间：{ts_str}"]
        if ether_layer:
            summary_lines.append(f"MAC：{mac_src} → {mac_dst}")
        if ip_layer:
            summary_lines.append(f"IP：{ip_src} → {ip_dst}")
        elif ipv6_layer:
            summary_lines.append(f"IPv6：{ip_src} → {ip_dst}")
        if tcp_layer:
            summary_lines.append(f"TCP 端口：{sport} → {dport}")
        elif udp_layer:
            summary_lines.append(f"UDP 端口：{sport} → {dport}")
        summary_lines.append(f"Info：{info_val}")
        summary_val = "\n".join(summary_lines)

        if insert_time is None:
            insert_time = datetime.datetime.now()

        # 计算单条报文唯一哈希：用于区分唯一报文
        # 优化点：仅基于结构化字段单次拼接一次 update，比原始版本（多次 update + bytes(pkt)）快很多
        # 字段选择：时间戳+MAC+IP+端口+flag+seq+ack+len 组合已足够唯一标识一条报文
        pkt_ts_str = f"{ts:.6f}" if ts is not None else ""
        hash_src = "|".join([
            pkt_ts_str,
            str(mac_src or ''), str(mac_dst or ''),
            str(ip_src or ''), str(ip_dst or ''),
            str(sport or ''), str(dport or ''),
            str(flag_val or ''), str(seq_val or ''),
            str(ack_val or ''), str(len_val or '')
        ]).encode('utf-8')
        packet_hash = hashlib.md5(hash_src).hexdigest()

        return (
            file_name,
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
        database = cfg.get("database", "pcap_db")
        table_name = cfg.get("table_name", "packets")

        if not server or not database:
            messagebox.showerror("错误", "请先配置正确的数据库服务器与数据库名称！")
            self.open_db_settings()
            return

        self._set_busy(True)
        self._update_progress(0)
        self.show_text.insert(
            "end",
            f"\n正在保存数据到 SQL Server 数据库 {server} ({database})...\n"
        )

        def _save_db_task():
            mgr: Optional[PcapDbManager] = None
            try:
                # 1. 创建数据库管理器并连接
                mgr = PcapDbManager(cfg)
                mgr.connect()

                # 2. 部署存储过程检查（首次使用时提示，不影响后续流程）
                #    注意：is_schema_deployed 用独立 cursor，不影响 mgr.cursor
                try:
                    schema_ok = mgr.is_schema_deployed(mgr.conn)
                except Exception:
                    schema_ok = True  # 检测失败时不阻塞主流程，让 ensure_schema 自己处理
                if not schema_ok:
                    self.root.after(0, lambda: (
                        self.show_text.insert(
                            "end",
                            "⚠ 警告：未检测到存储过程，建议在数据库执行 db_schema.sql 部署：\n"
                            "  sqlcmd -S localhost -E -d pcap_db -i db_schema.sql\n"
                            "  当前将使用兜底内联 SQL 模式（功能正常但失去集中维护优势）\n\n"
                        ),
                    ))

                # 3. 确保表结构（调用 sp_ensure_pcap_table，同连接内只执行一次）
                mgr.ensure_schema(table_name)

                # 4. 文件级查重：仅按文件名判断是否已入库
                file_name = os.path.basename(self.pcap_path)
                status = mgr.check_duplicate(table_name, file_name)

                if status == 1:
                    # 同名文件已存在 → 覆盖（先删旧数据，释放锁后再插入）
                    self.root.after(0, lambda: self.show_text.insert(
                        "end",
                        f"检测到同名文件 {file_name} 已入库，正在覆盖旧数据...\n"
                    ))
                    deleted = mgr.delete_by_file(table_name, file_name)
                    mgr.commit()  # 立即提交删除，释放锁
                    self.root.after(0, lambda d=deleted: self.show_text.insert(
                        "end", f"已删除旧数据 {d} 条（已提交）\n"
                    ))

                # 5. 批量插入（仍用 executemany + INSERT VALUES，性能最优）
                #    insert_batch 内置分段提交（每 5 批 commit），无需调用方再 commit
                batch_insert_time = datetime.datetime.now()
                total = len(self.filtered_pkts)

                # 一次构造所有行，直接传入 insert_time 避免二次遍历
                all_rows = [
                    self._packet_to_row(self.pcap_path, pkt, insert_time=batch_insert_time)
                    for pkt in self.filtered_pkts
                ]

                def progress_cb(inserted: int, total: int) -> None:
                    if total > 0:
                        progress = (inserted / total) * 100
                        self.root.after(0, lambda p=progress: self._update_progress(p))

                def cancel_check() -> bool:
                    return self._cancel_event.is_set()

                inserted = mgr.insert_batch(
                    table_name=table_name,
                    rows=all_rows,
                    batch_size=self.DB_BATCH_SIZE,
                    progress_cb=progress_cb,
                    cancel_check=cancel_check,
                )

                if self._cancel_event.is_set():
                    # 用户取消：insert_batch 已回滚未提交段，已 commit 的段保留
                    self.root.after(0, lambda i=inserted: (
                        self.show_text.insert("end", f"⚠ 已取消入库，已提交 {i} 条（未提交部分已回滚）\n"),
                        messagebox.showinfo("已取消", f"入库任务已取消，已提交 {i} 条数据")
                    ))
                    return

                # insert_batch 内部已分段 commit，这里无需再 commit
                self.root.after(0, lambda i=inserted: (
                    self.show_text.insert("end", f"已成功保存 {i} 条报文至 SQL Server 表 [{table_name}]\n"),
                    messagebox.showinfo("成功", f"已成功保存 {i} 条报文到 SQL Server")
                ))

                # 入库完成后异步检查索引碎片，结果直接显示到主面板（不弹窗，避免每次入库打扰）
                try:
                    need_maintain, frag_info = mgr.needs_maintenance(table_name)
                    if need_maintain:
                        bad_indexes = [
                            f for f in frag_info if f["recommend"] in ("rebuild", "reorganize")
                        ]
                        report_lines = ["⚠ 检测到索引碎片过高，建议维护："]
                        for idx in bad_indexes:
                            action = "重建" if idx["recommend"] == "rebuild" else "重组"
                            report_lines.append(
                                f"  {idx['name']}: 碎片率 {idx['fragmentation_pct']}% "
                                f"({idx['page_count']} 页 / {idx['size_mb']} MB) → 建议{action}"
                            )
                        report_lines.append("  点击「索引维护」按钮可执行维护操作")
                        report = "\n".join(report_lines)
                        self.root.after(0, lambda r=report: (
                            self.show_text.insert("end", "\n" + r + "\n"),
                            self.show_text.see("end")
                        ))
                    else:
                        # 状态良好时也简短提示一下，让用户知道检查过
                        self.root.after(0, lambda: (
                            self.show_text.insert("end", "（索引碎片检查通过，状态良好）\n"),
                            self.show_text.see("end")
                        ))
                except Exception as frag_err:
                    # 碎片检查失败不影响主流程，仅记录日志
                    self.root.after(0, lambda e=str(frag_err): self.show_text.insert(
                        "end", f"（索引碎片检查跳过：{e}）\n"
                    ))
            except Exception as e:
                if mgr:
                    mgr.rollback()
                err_msg = str(e)
                self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", f"保存数据库失败：{m}"))
            finally:
                if mgr:
                    mgr.close()
                self.root.after(0, lambda: (
                    self._set_busy(False),
                    self._update_progress(100)
                ))

        threading.Thread(target=_save_db_task, daemon=True).start()

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

    def analyze_connections(self) -> None:
        if not self.pcap_path or not hasattr(self, "all_pkts_count") or self.all_pkts_count == 0:
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
        self.show_text.insert("end", f"\n开始分析 TCP 连接状态...\n")
        self.show_text.see("end")
        analyze_start_time = time.time()

        def _analyze_task():
            try:
                def get_flags(pkt) -> str:
                    if TCP in pkt:
                        return pkt[TCP].sprintf("%TCP.flags%")
                    return ""

                connections: OrderedDict = OrderedDict()
                active_instances: Dict[tuple, Dict] = {}
                total_pkts_count = self.all_pkts_count
                analyze_stage_start = time.time()

                with PcapReader(self.pcap_path) as reader:
                    for i, pkt in enumerate(reader):
                        # 动态更新进度条 (0% - 80%) + 实时状态文本
                        if total_pkts_count > 0 and i % 10000 == 0:
                            progress = (i / total_pkts_count) * 80
                            elapsed = time.time() - analyze_stage_start
                            rate = i / elapsed if elapsed > 0 else 0
                            pct = (i / total_pkts_count * 100) if total_pkts_count > 0 else 0
                            # 每 50000 包输出一次进度，避免刷屏
                            if i % 50000 == 0 and i > 0:
                                status = f"  扫描进度: {pct:.1f}% ({i:,}/{total_pkts_count:,}) - {rate:,.0f} 包/秒\n"
                                self.root.after(0, lambda s=status: (
                                    self.show_text.delete("end-2l", "end"),
                                    self.show_text.insert("end", s),
                                    self.show_text.see("end"),
                                    self._update_progress(progress)
                                ))
                            else:
                                self.root.after(0, lambda p=progress: self._update_progress(p))

                        # 中断检查点：每 5000 包查一次取消标志
                        if i % 5000 == 0 and self._cancel_event.is_set():
                            self.root.after(0, lambda: self.show_text.insert("end", "⚠ 已取消连接分析任务\n"))
                            return

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
                            # 如果检测到新的 SYN 包（不含 ACK 才是纯发起连接 of SYN）或者时间差过大
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
                        
                        # 提取深度异常检测所需的序列特征
                        seq = pkt[TCP].seq
                        ack = pkt[TCP].ack
                        
                        # 动态计算 TCP Payload 载荷长度
                        payload_len = 0
                        if IP in pkt:
                            ip_len = pkt[IP].len if pkt[IP].len is not None else len(bytes(pkt[IP]))
                            ip_hdr = pkt[IP].ihl * 4
                            tcp_hdr = pkt[TCP].dataofs * 4 if pkt[TCP].dataofs is not None else 20
                            payload_len = max(0, ip_len - ip_hdr - tcp_hdr)
                        elif IPv6 in pkt:
                            payload_len = len(bytes(pkt[TCP].payload)) if pkt[TCP].payload else 0
                            
                        # 提取 TCP 窗口大小
                        window = pkt[TCP].window

                        connections[key].append((i, direction, flags, pkt_time, seq, ack, payload_len, window))

                normal_lines: list[str] = []
                total_pkts = sum(len(v) for v in connections.values())
                header = [
                    f"分析时间：{datetime.datetime.now()}",
                    f"抓包文件：{self.pcap_path}",
                    f"总报文数：{total_pkts}",
                    f"注：连接列表格式为 [发起方 IP:端口]<->[接收方 IP:端口]",
                ]

                incorrect: list[tuple] = []
                
                # 异常 IP 统计
                ip_abnormal_counts = Counter()
                ip_init_counts = Counter()
                ip_target_counts = Counter()
                ip_status_counts = {}

                # 按状态分组存储异常连接块
                status_groups = {}

                total_conns = len(connections)
                # 第二阶段开始：状态判定
                self.root.after(0, lambda: (
                    self.show_text.delete("end-2l", "end"),
                    self.show_text.insert("end",
                        f"✓ 扫描完成，共 {total_conns:,} 条 TCP 连接，正在判定状态...\n"
                    ),
                    self.show_text.see("end")
                ))
                for conn_idx, (key, pkts_list) in enumerate(connections.items()):
                    # 动态更新进度条 (80% - 100%)
                    if total_conns > 0 and conn_idx % 100 == 0:
                        progress = 80 + (conn_idx / total_conns) * 20
                        self.root.after(0, lambda p=progress: self._update_progress(p))

                    src_ip, src_port, dst_ip, dst_port, inst_id = key
                    flags_list = [f[2] for f in pkts_list]
                    flags_seq = " ".join(flags_list)

                    # 根据首包确定主动发起方
                    first_pkt = pkts_list[0]
                    first_pkt_dir = first_pkt[1] # "src_ip:sport->dst_ip:dport"
                    init_side, target_side = first_pkt_dir.split("->")
                    init_ip = init_side.rsplit(":", 1)[0]
                    target_ip = target_side.rsplit(":", 1)[0]

                    short_name = f"{init_side}<->{target_side}"

                    # 调用新提取的辅助判定函数确定状态
                    status = self._determine_tcp_status(pkts_list)
                    is_error = not status.is_normal

                    if is_error:
                        incorrect.append((short_name, flags_seq, status.value, len(pkts_list)))
                        
                        # 统计 IP 数据
                        ip_abnormal_counts[init_ip] += 1
                        ip_abnormal_counts[target_ip] += 1
                        ip_init_counts[init_ip] += 1
                        ip_target_counts[target_ip] += 1
                        
                        if init_ip not in ip_status_counts:
                            ip_status_counts[init_ip] = Counter()
                        ip_status_counts[init_ip][status.value] += 1
                        if target_ip not in ip_status_counts:
                            ip_status_counts[target_ip] = Counter()
                        ip_status_counts[target_ip][status.value] += 1

                    conn_lines = [
                        f"{short_name:^50} | {flags_seq[:70]:^55} | {status.value:^20}",
                        f"  Instance: {inst_id} | Packets: {len(pkts_list)}",
                    ]
                    for p in pkts_list:
                        idx, direction, fl, t = p[:4]
                        ts = datetime.datetime.fromtimestamp(float(t)).strftime("%H:%M:%S.%f")
                        conn_lines.append(f"    [{idx:>4}] {ts} {direction} [{fl}]")
                    conn_lines.append("")

                    if is_error:
                        if status not in status_groups:
                            status_groups[status] = []
                        status_groups[status].append(conn_lines)
                    else:
                        normal_lines.extend(conn_lines)

                # 构建异常 IP Top 10 排行榜
                ip_summary_lines = []
                ip_summary_lines.append("【异常 IP Top 10 排行榜】")
                ip_summary_lines.append("注：此处统计包含该 IP 作为发起方或接收方在异常连接中出现的总次数")
                ip_summary_lines.append("-" * 80)
                
                top_ips = ip_abnormal_counts.most_common(10)
                if not top_ips:
                    ip_summary_lines.append("  暂无异常 IP")
                else:
                    for idx, (ip, count) in enumerate(top_ips, 1):
                        init_cnt = ip_init_counts[ip]
                        target_cnt = ip_target_counts[ip]
                        status_dist = ip_status_counts[ip]
                        status_dist_str = ", ".join(f"{k}: {v}次" for k, v in status_dist.most_common())
                        ip_summary_lines.append(
                            f"  No.{idx}  {ip:<40} | 异常总数: {count:>3} 次 | 发起: {init_cnt:>3} 次, 接收: {target_cnt:>3} 次\n"
                            f"        状态分布: {status_dist_str}"
                        )
                ip_summary_lines.append("-" * 80)
                ip_summary_lines.append("")

                # 将异常连接按状态分组排列
                error_lines = []
                error_lines.append("================================================================================")
                error_lines.append("【异常连接按状态类型分组详情】")
                error_lines.append("================================================================================")
                error_lines.append("")

                for status in sorted(status_groups.keys(), key=lambda s: s.name):
                    conns = status_groups[status]
                    status_desc = status.desc if hasattr(status, "desc") else ""
                    error_lines.append(f"### [{status.value}] {status_desc} (共 {len(conns)} 条连接)")
                    error_lines.append("-" * 80)
                    for conn_lines_block in conns:
                        error_lines.extend(conn_lines_block)
                    error_lines.append("")

                # 写正常连接文件
                with open(normal_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(header + [""] + normal_lines))

                # 写异常连接文件
                error_header = header + [
                    f"总连接数：{len(connections)}",
                    f"正常连接数：{len(connections) - len(incorrect)}",
                    f"异常连接数：{len(incorrect)}",
                    "",
                ] + ip_summary_lines
                with open(error_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(error_header + error_lines))

                result_msg = (f"✓ 分析完成，共 {len(connections):,} 条连接，"
                              f"异常 {len(incorrect):,} 条（总耗时 {time.time()-analyze_start_time:.1f}s）\n"
                              f"  正常 → {normal_path}\n"
                              f"  异常 → {error_path}\n")
                info_msg = (f"分析完成\n正常连接：{len(connections) - len(incorrect)} 条→{normal_path}\n"
                            f"异常连接：{len(incorrect)} 条→{error_path}")
                self.root.after(0, lambda r=result_msg, i=info_msg: (
                    self.show_text.insert("end", "\n" + r),
                    self.show_text.see("end"),
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