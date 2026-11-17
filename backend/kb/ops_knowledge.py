"""运维知识库：故障场景 RAG

参照 OpenClaw Memory 模块 + Claude Code 推理链，
实现结构化运维知识库，支持 BM25 语义检索和诊断推理。

知识库预置 20+ 故障场景，涵盖：
- 磁盘空间问题
- CPU 负载异常
- 内存泄漏
- 网络故障
- 服务宕机
- 配置文件漂移
- 端口占用
等典型运维场景
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger("kb.ops_knowledge")


# ---------------------------------------------------------------------------
# 预置故障场景知识库（20+ 场景）
# ---------------------------------------------------------------------------
_OPS_SCENARIOS: List[Dict[str, Any]] = [
    # ---- 磁盘类 ----
    {
        "id": "disk_full",
        "scenario": "磁盘空间满",
        "severity": "critical",
        "symptoms": [
            "df -h 显示 100%",
            "no space left on device",
            "磁盘使用率 100%",
            "写入失败",
            "tar: write error",
        ],
        "keywords": ["disk", "space", "full", "df", "磁盘", "满", "空间"],
        "root_cause": "日志文件或临时文件堆积，个别大文件占用空间",
        "diagnosis_steps": [
            "df -h -- 查看各分区使用率",
            "find /var/log -type f -size +100M -- 查看大日志文件",
            "du -sh /var/log/* -- 按目录大小排序",
            "df -i -- 检查 inode 是否耗尽",
        ],
        "solutions": [
            "清理 /var/log 下的过期日志文件（find -mtime +30 -delete）",
            "清理 /tmp 下的临时文件",
            "清理 /var/tmp 下的过期文件",
            "建议设置 logrotate 日志轮转",
        ],
        "prevention": "配置 logrotate，设置合理的保留周期和文件大小",
        "tools_used": ["disk.usage", "disk.find_large", "shell.execute"],
        "category": "disk",
    },
    {
        "id": "disk_inode_full",
        "scenario": "inode 耗尽",
        "severity": "critical",
        "symptoms": [
            "df -i 显示 100%",
            "touch: cannot create file",
            "mkdir: No space left on device (但 df -h 有空间)",
        ],
        "keywords": ["inode", "iused", "file", "count", "耗尽"],
        "root_cause": "小文件数量过多（大量空文件或极小文件）",
        "diagnosis_steps": [
            "df -i -- 查看各分区 inode 使用率",
            "find / -xdev -type f | wc -l -- 统计文件总数",
            "find / -xdev -type d | wc -l -- 统计目录总数",
        ],
        "solutions": [
            "查找并删除大量小文件",
            "find / -xdev -type f -size 0 -delete -- 删除空文件",
            "清理 /tmp 下的小文件",
        ],
        "prevention": "定期巡检 inode 使用率，设置小文件告警阈值",
        "tools_used": ["shell.execute"],
        "category": "disk",
    },
    {
        "id": "disk_io_high",
        "scenario": "磁盘 I/O 繁忙",
        "severity": "high",
        "symptoms": [
            "iostat %util 高",
            "iotop 显示某个进程大量读写",
            "磁盘响应缓慢",
            "load average 升高",
        ],
        "keywords": ["io", "iotop", "iostat", "disk", "busy", "磁盘", "IO"],
        "root_cause": "某个进程大量读写磁盘，可能是日志写入、数据库swap或IO密集型任务",
        "diagnosis_steps": [
            "iostat -x 2 5 -- 查看各设备 I/O 状态",
            "iotop -- 查看进程级 I/O 占用",
            "ps aux -- sort by io -- 查看高 IO 进程",
            "lsof 查看打开大量文件的进程",
        ],
        "solutions": [
            "确认高 IO 进程的合法性",
            "如果是业务进程，考虑错峰运行或限速",
            "如果是日志进程，配置日志轮转",
            "考虑将日志或数据迁移到独写磁盘",
        ],
        "prevention": "配置 I/O 监控告警，关注 %util > 80% 的持续情况",
        "tools_used": ["disk.iotop", "process.lsof", "shell.execute"],
        "category": "disk",
    },
    # ---- CPU 类 ----
    {
        "id": "cpu_100",
        "scenario": "CPU 使用率 100%",
        "severity": "critical",
        "symptoms": [
            "top load average 100%",
            "CPU sy(system) 高",
            "CPU us(user) 高",
            "服务器响应缓慢",
            "load average 异常高",
        ],
        "keywords": ["cpu", "load", "100%", "load average", "top", "CPU", "负载"],
        "root_cause": "僵尸进程、无效死循环、异常计算任务或DDoS流量",
        "diagnosis_steps": [
            "top -H -- 查看各 CPU 核心使用率和进程",
            "ps aux -- sort by cpu -- head -20 -- 查看 CPU 占用前20",
            "ps -eo pid,ppid,stat,comm -- 查看进程状态（Z=僵尸）",
            "mpstat 2 5 -- 查看 CPU 各核心忙闲比",
        ],
        "solutions": [
            "如果是 zombie 进程，找到父进程并重启或 kill 父进程",
            "如果是死循环，kill -9 <pid> 终止",
            "如果是合法高负载，考虑扩容或优化算法",
            "如果是外部攻击，封禁来源 IP",
        ],
        "prevention": "配置 CPU 使用率告警（阈值 80%），定期巡检异常进程",
        "tools_used": ["process.list", "shell.execute"],
        "category": "cpu",
    },
    {
        "id": "cpu_iowait_high",
        "scenario": "CPU iowait 高",
        "severity": "high",
        "symptoms": [
            "top CPU wa% 高",
            "iowait 高但 CPU 使用率不高",
            "系统响应慢",
        ],
        "keywords": ["iowait", "io", "wait", "磁盘", "等待"],
        "root_cause": "进程等待磁盘 I/O 完成，可能是大量换页、swap 或磁盘瓶颈",
        "diagnosis_steps": [
            "free -h -- 查看内存和 swap 使用",
            "vmstat 2 5 -- 查看 si/so（换入换出）",
            "iostat -x 2 5 -- 查看磁盘 I/O",
            "ps aux -- sort by io -- 查看高 I/O 进程",
        ],
        "solutions": [
            "如果是 swap 过多，增加物理内存或优化内存使用",
            "如果是磁盘瓶颈，考虑升级磁盘或优化 IO 模式",
            "如果是换页频繁，优化应用内存分配模式",
        ],
        "prevention": "监控 swap 使用率，内存不足时及时扩容",
        "tools_used": ["system.memory", "shell.execute"],
        "category": "cpu",
    },
    # ---- 内存类 ----
    {
        "id": "memory_leak",
        "scenario": "内存泄漏",
        "severity": "critical",
        "symptoms": [
            "free -h 显示内存持续下降",
            "available 持续减少",
            "swap 使用率逐渐升高",
            "OOM killer 触发",
        ],
        "keywords": ["memory", "mem", "leak", "OOM", "内存", "泄漏"],
        "root_cause": "应用程序内存未释放，导致可用内存逐渐耗尽",
        "diagnosis_steps": [
            "free -h -- 查看内存总量/使用/可用",
            "ps aux -- sort by rss -- 查看进程内存占用排名",
            "pmap -x <pid> -- 查看进程详细内存映射",
            "dmesg | grep -i 'killed process' -- 查看 OOM Kill 记录",
        ],
        "solutions": [
            "重启泄漏进程（kill -9 <pid>）",
            "如果是 Java 进程，触发 heap dump 分析",
            "升级内存或增加 swap",
            "代码层面修复内存泄漏",
        ],
        "prevention": "配置内存使用率告警（阈值 85%），设置 OOM 告警",
        "tools_used": ["system.memory", "process.list", "shell.execute"],
        "category": "memory",
    },
    {
        "id": "swap_full",
        "scenario": "Swap 空间耗尽",
        "severity": "high",
        "symptoms": [
            "free -h swap 显示 used = total",
            "系统变慢",
            "OOM killer 触发",
            "dmesg 有 OOM 记录",
        ],
        "keywords": ["swap", "oom", "memory", "reclaim", "交换", "内存"],
        "root_cause": "物理内存不足，swap 也被耗尽，导致 OOM",
        "diagnosis_steps": [
            "free -h -- 查看 swap 使用情况",
            "ps aux -- sort by rss -- head -10 -- 查看内存大户",
            "dmesg -T | grep -i oom -- 查看 OOM 记录",
            "vmstat 2 5 -- 查看 si/so 换页速率",
        ],
        "solutions": [
            "紧急：临时增加 swap（dd + mkswap + swapon）",
            "找到内存大户进程，评估是否正常",
            "考虑物理扩容或业务降级",
        ],
        "prevention": "监控 swap 使用率，物理内存使用率 > 80% 时告警",
        "tools_used": ["system.memory", "process.list"],
        "category": "memory",
    },
    # ---- 网络类 ----
    {
        "id": "network_unreachable",
        "scenario": "网络不可达",
        "severity": "critical",
        "symptoms": [
            "ping 不通",
            "telnet 端口失败",
            "curl 超时",
            "网络连接失败",
        ],
        "keywords": ["network", "ping", "unreachable", "timeout", "网络", "不通"],
        "root_cause": "网络链路故障、DNS 解析失败、防火墙阻断或服务端口未监听",
        "diagnosis_steps": [
            "ping -c 3 <gateway> -- 测试网关连通性",
            "ping -c 3 8.8.8.8 -- 测试外网连通性",
            "nslookup <host> -- 测试 DNS 解析",
            "netstat -tunlp -- 查看本机监听端口",
            "ss -tunlp -- 替代 netstat 查看端口",
        ],
        "solutions": [
            "如果是网关故障，检查网络配置和交换机",
            "如果是 DNS 故障，修改 /etc/resolv.conf 或切换 DNS 服务器",
            "如果是防火墙阻断，iptables -L -n 检查规则",
            "如果是服务未启动，systemctl start <service>",
        ],
        "prevention": "配置网络可达性监控（每分钟 ping 健康检查）",
        "tools_used": ["network.connectivity", "network.list", "shell.execute"],
        "category": "network",
    },
    {
        "id": "port_conflict",
        "scenario": "端口被占用",
        "severity": "medium",
        "symptoms": [
            "bind: Address already in use",
            "服务启动失败",
            "端口 8080 已被占用",
        ],
        "keywords": ["port", "bind", "address", "in use", "端口", "占用"],
        "root_cause": "目标端口已被其他进程占用，导致新服务无法绑定",
        "diagnosis_steps": [
            "lsof -i:<port> -- 查看占用端口的进程",
            "ss -tunlp | grep <port> -- 查看端口占用详情",
            "netstat -tunlp | grep <port> -- 替代方案",
        ],
        "solutions": [
            "确认占用端口的进程是否需要",
            "如果不需要，kill -9 <pid> 终止",
            "如果需要，更换服务端口（如 8080→8081）",
        ],
        "prevention": "服务启动脚本先检查端口是否可用，更换端口配置",
        "tools_used": ["process.lsof", "network.list"],
        "category": "network",
    },
    {
        "id": "firewall_block",
        "scenario": "防火墙阻断连接",
        "severity": "high",
        "symptoms": [
            "telnet 端口失败（被拒绝）",
            "服务本机正常，远程无法访问",
            "iptables REJECT/DROP",
        ],
        "keywords": ["firewall", "iptables", "block", "reject", "防火墙", "阻断"],
        "root_cause": "防火墙规则阻断，常见于 iptables/firewalld/ufw 规则变更",
        "diagnosis_steps": [
            "iptables -L -n -- 查看默认策略和规则",
            "firewalld: firewall-cmd --list-all",
            "ufw status -- if using ufw",
            "ss -tunlp -- 确认端口确实在监听",
        ],
        "solutions": [
            "如果是测试环境，iptables -F 清空规则",
            "如果是生产环境，添加对应端口放行规则",
            "iptables -A INPUT -p tcp --dport <port> -j ACCEPT",
        ],
        "prevention": "变更防火墙规则前先备份，配置变更管理流程",
        "tools_used": ["shell.execute"],
        "category": "network",
    },
    # ---- 服务类 ----
    {
        "id": "service_down",
        "scenario": "服务宕机",
        "severity": "critical",
        "symptoms": [
            "systemctl status <service> 显示 failed/inactive",
            "服务进程不存在",
            "curl localhost:<port> 连接失败",
            "服务自动退出",
        ],
        "keywords": ["service", "down", "failed", "inactive", "宕机", "服务"],
        "root_cause": "进程崩溃、配置错误、依赖服务未启动、资源耗尽或被 OOM Kill",
        "diagnosis_steps": [
            "systemctl status <service> -- 查看服务详细状态",
            "journalctl -u <service> -n 50 -- 查看最近日志",
            "systemctl list-dependencies <service> -- 查看依赖链",
            "dmesg -T | grep <service> -- 查看系统日志中的痕迹",
        ],
        "solutions": [
            "如果是配置错误，修正配置后 systemctl restart",
            "如果是资源不足（端口/内存），解决资源问题后重启",
            "如果是崩溃，查看 core dump 或告警联系人",
            "systemctl restart <service> -- 尝试重启服务",
        ],
        "prevention": "配置服务监控（每分钟检查 active 状态），异常时自动告警",
        "tools_used": ["service.status", "log.journal", "shell.execute"],
        "category": "service",
    },
    {
        "id": "service_restart_loop",
        "scenario": "服务反复重启",
        "severity": "critical",
        "symptoms": [
            "systemctl status 显示 start-limit-hit",
            "服务不断 restart/stop",
            "start request repeat too quickly",
        ],
        "keywords": ["restart", "loop", "start-limit", "反复", "重启"],
        "root_cause": "配置错误或依赖不满足导致服务启动后立即失败，systemd 进入反复重启限制",
        "diagnosis_steps": [
            "systemctl status <service> -- 查看状态和重启计数",
            "journalctl -u <service> -- 查看完整启动日志",
            "cat /run/systemd/generator.late/<service>.service -- 查看生成的服务文件",
        ],
        "solutions": [
            "先 journalctl --vacuum-time=1s 清理重启限制",
            "修复导致启动失败的根因（配置/依赖/权限）",
            "再次 systemctl restart <service>",
        ],
        "prevention": "服务启动前做依赖检查，健康检查通过后再真正启动",
        "tools_used": ["service.status", "log.journal"],
        "category": "service",
    },
    # ---- 进程类 ----
    {
        "id": "zombie_process",
        "scenario": "僵尸进程堆积",
        "severity": "high",
        "symptoms": [
            "ps aux 显示进程状态为 Z",
            "top 显示 zombie 数量 > 0",
            "d entry in ps output",
        ],
        "keywords": ["zombie", "z", "defunct", "僵死", "僵尸"],
        "root_cause": "父进程未调用 wait() 回收子进程资源，常见于编程错误或父进程阻塞",
        "diagnosis_steps": [
            "ps aux | grep Z -- 查看所有僵尸进程",
            "ps -eo pid,ppid,stat,comm -- 查看进程父子关系",
            "top -b -n 1 | grep zombie -- 快速统计",
        ],
        "solutions": [
            "找到僵尸进程的父进程 PID",
            "kill -CHLD <parent_pid> -- 尝试通知父进程回收",
            "如果父进程无响应，kill -9 <parent_pid> 终止父进程",
            "重启相关服务",
        ],
        "prevention": "代码层面确保所有子进程被正确回收，设置僵尸进程数量告警",
        "tools_used": ["process.list", "shell.execute"],
        "category": "process",
    },
    {
        "id": "too_many_processes",
        "scenario": "进程数过多（pid 耗尽）",
        "severity": "high",
        "symptoms": [
            "fork: retry: No such device",
            "cannot create new process",
            "ps 报错 too many open files",
        ],
        "keywords": ["process", "pid", "max", "limit", "进程", "耗尽"],
        "root_cause": "进程数达到系统上限（/proc/sys/kernel/pid_max），新进程无法创建",
        "diagnosis_steps": [
            "ps aux | wc -l -- 统计总进程数",
            "cat /proc/sys/kernel/pid_max -- 查看 pid 上限",
            "ps -eo pid -- 查看已分配的 pid 列表",
            "top -b -n 1 | head -15 -- 查看系统概览",
        ],
        "solutions": [
            "找出异常创建进程的父进程",
            "kill 不必要的进程释放 pid",
            "如果是正常满载，调高 pid_max: echo 4194304 > /proc/sys/kernel/pid_max",
        ],
        "prevention": "监控进程数，设置 /proc/sys/kernel/pid_max 调高",
        "tools_used": ["process.list", "shell.execute"],
        "category": "process",
    },
    # ---- 配置类 ----
    {
        "id": "config_drift",
        "scenario": "配置文件漂移",
        "severity": "medium",
        "symptoms": [
            "服务行为与预期不符",
            "diff /etc/config 发现变更",
            "某次变更后服务异常",
        ],
        "keywords": ["config", "diff", "drift", "change", "配置", "漂移"],
        "root_cause": "手动修改或自动化脚本修改了配置文件，与基准版本产生差异",
        "diagnosis_steps": [
            "diff -u <backup> <current> -- 对比备份和当前配置",
            "stat <config> -- 查看配置文件的修改时间",
            "git diff -- if config is version controlled",
            "journalctl -u <service> -- 查看配置相关的日志",
        ],
        "solutions": [
            "如果是有害变更，用备份或 git 回滚",
            "如果是必要的变更，更新基线配置并记录变更",
            "使用 Ansible/Chef 等配置管理工具管理配置文件",
        ],
        "prevention": "配置文件纳入版本控制，变更走审批流程，配置变更自动告警",
        "tools_used": ["shell.execute", "file.read"],
        "category": "config",
    },
    # ---- 日志类 ----
    {
        "id": "logstorm",
        "scenario": "日志风暴",
        "severity": "high",
        "symptoms": [
            "日志文件在几秒内涨到 GB 级",
            "磁盘空间快速下降",
            "日志文件异常大",
            "/var/log 目录占满",
        ],
        "keywords": ["log", "storm", "大量", "日志", "增长"],
        "root_cause": "应用程序异常大量写日志（循环打印ERROR、调试日志未关闭、外部告警风暴）",
        "diagnosis_steps": [
            "tail -f /var/log/<service>.log -- 查看最新日志内容",
            "find /var/log -type f -mmin -5 | xargs ls -lh -- 查看最近被修改的日志",
            "lsof | grep log -- 查看所有写日志的进程",
        ],
        "solutions": [
            "紧急：停止写日志的进程或重定向日志输出",
            "清空或轮转当前超大日志文件 > /var/log/service.log",
            "找到异常日志来源，修复应用程序",
            "配置 logrotate 限制日志大小",
        ],
        "prevention": "日志级别合理配置，生产环境禁用 DEBUG，设置日志大小监控",
        "tools_used": ["log.journal", "shell.execute"],
        "category": "log",
    },
    {
        "id": "disk_large_files",
        "scenario": "大日志文件占用空间",
        "severity": "medium",
        "symptoms": [
            "df -h 显示 /var 分区快满",
            "ls -lh /var/log/*.log 显示文件过大",
            "日志文件超过 10GB",
        ],
        "keywords": ["log", "large", "huge", "日志", "大文件"],
        "root_cause": "日志文件长期未轮转，单个文件体积过大",
        "diagnosis_steps": [
            "find /var/log -type f -size +1G -- 查找超大日志文件",
            "ls -lhS /var/log/*.log | head -- 按大小排序",
            "du -sh /var/log/* -- 各目录空间占用",
        ],
        "solutions": [
            "配置 logrotate 强制轮转",
            "手动压缩或归档历史日志",
            "删除确认已归档的日志文件",
        ],
        "prevention": "配置 logrotate，按天或按大小轮转，保留 30 天",
        "tools_used": ["disk.find_large", "disk.cleanup"],
        "category": "log",
    },
    # ---- 安全类 ----
    {
        "id": " brute_force",
        "scenario": "暴力破解",
        "severity": "high",
        "symptoms": [
            "/var/log/auth.log 大量失败登录",
            "lastb 显示大量错误登录",
            "SSH 登录尝试次数异常",
            "FAIL bootstrap",
        ],
        "keywords": ["ssh", "brute", "login", "fail", "auth", "暴力", "破解"],
        "root_cause": "外部攻击者通过字典或暴力尝试 SSH 登录",
        "diagnosis_steps": [
            "lastb -- 查看失败登录记录",
            "grep 'Failed password' /var/log/auth.log | tail -50",
            "awk '{print $11}' auth.log | sort | uniq -c | sort -rn | head",
            "iptables -L -n | grep <ip> -- 查看已封禁 IP",
        ],
        "solutions": [
            "使用 fail2ban 自动封禁：fail2ban-client set sshd banip <ip>",
            "手动 iptables 封禁：iptables -A INPUT -s <ip> -j DROP",
            "修改 SSH 端口或禁用密码登录（仅用密钥）",
            "配置 allow list 仅允许白名单 IP 登录",
        ],
        "prevention": "禁用密码登录使用密钥登录，安装 fail2ban，配置登录告警",
        "tools_used": ["log.journal", "shell.execute"],
        "category": "security",
    },
    # ---- 系统类 ----
    {
        "id": "load_high",
        "scenario": "系统负载过高",
        "severity": "high",
        "symptoms": [
            "load average >> CPU 核心数",
            "top 显示 load average 红色",
            "系统响应极慢",
            "SSH 登录卡顿",
        ],
        "keywords": ["load", "loadavg", "high", "负载", "高"],
        "root_cause": "CPU 密集型进程、IO 等待、进程队列堆积或硬件资源不足",
        "diagnosis_steps": [
            "uptime -- 查看 load average",
            "top -b -n 1 -- 查看 CPU/IO 进程",
            "vmstat 2 5 -- 查看 CPU 和 IO 状态",
            "ps aux -- sort by cpu -- head -20",
        ],
        "solutions": [
            "根据诊断结果定位根因（CPU/IO/进程数）",
            "优化或终止高负载进程",
            "扩容 CPU/内存/磁盘",
        ],
        "prevention": "配置 load average 告警，定期巡检",
        "tools_used": ["process.list", "system.memory", "shell.execute"],
        "category": "system",
    },
    {
        "id": "file_descriptor_exhausted",
        "scenario": "文件描述符耗尽",
        "severity": "high",
        "symptoms": [
            "too many open files",
            "cannot open file",
            "socket: too many open files",
        ],
        "keywords": ["fd", "file", "descriptor", "ulimit", "文件描述符"],
        "root_cause": "进程打开的文件/socket 数量达到 ulimit 上限",
        "diagnosis_steps": [
            "lsof -p <pid> | wc -l -- 查看进程打开的 fd 数量",
            "cat /proc/sys/fs/file-max -- 查看系统全局 fd 上限",
            "ulimit -n -- 查看当前用户的 fd 上限",
        ],
        "solutions": [
            "临时调高 ulimit -n 65535",
            "永久修改 /etc/security/limits.conf",
            "找到打开大量 fd 的进程，确认是否泄漏",
        ],
        "prevention": "在 limits.conf 中为服务用户设置足够的 nofile，监控报警",
        "tools_used": ["process.lsof", "shell.execute"],
        "category": "system",
    },
]


# ---------------------------------------------------------------------------
# BM25 检索器（轻量实现，无需 sklearn）
# ---------------------------------------------------------------------------

class BM25Indexer:
    """轻量级 BM25 索引器（无需外部依赖）"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.documents: Dict[str, str] = {}
        self.doc_tokens: Dict[str, List[str]] = {}
        self.avgdl: float = 0.0
        self.doc_count: int = 0

    def add_document(self, doc_id: str, text: str) -> None:
        self.documents[doc_id] = text
        self.doc_tokens[doc_id] = self._tokenize(text)

    def build(self) -> None:
        self.doc_count = len(self.documents)
        total_len = sum(len(t) for t in self.doc_tokens.values())
        self.avgdl = total_len / self.doc_count if self.doc_count else 1.0

    def _tokenize(self, text: str) -> List[str]:
        tokens = []
        # 英文按词
        for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
            if len(word) > 1:
                tokens.append(word)
        # 中文按bigram
        chars = re.findall(r"[一-鿿]", text)
        for i in range(len(chars) - 1):
            tokens.append(chars[i] + chars[i + 1])
        return tokens

    def score(self, query: str, doc_ids: List[str]) -> List[tuple]:
        """BM25 评分"""
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []

        # 统计词频
        doc_tf: Dict[str, Counter] = {}
        idf: Dict[str, float] = {}
        all_tokens: set = set()
        for did in doc_ids:
            tokens = self.doc_tokens.get(did, [])
            doc_tf[did] = Counter(tokens)
            all_tokens.update(tokens)

        for term in all_tokens:
            doc_with_term = sum(1 for did in doc_ids if term in doc_tf[did])
            if doc_with_term > 0:
                idf[term] = math.log((self.doc_count - doc_with_term + 0.5) / (doc_with_term + 0.5) + 1)
            else:
                idf[term] = 0.0

        scores: Dict[str, float] = {}
        for did in doc_ids:
            score = 0.0
            tf = doc_tf.get(did, Counter())
            dl = len(self.doc_tokens.get(did, []))
            for q_term in q_tokens:
                if q_term not in idf:
                    continue
                term_tf = tf.get(q_term, 0)
                idf_val = idf[q_term]
                numerator = term_tf * (self.k1 + 1)
                denominator = term_tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += idf_val * numerator / (denominator + 0.001)
            scores[did] = score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# 诊断报告数据类
# ---------------------------------------------------------------------------

@dataclass
class DiagnosisReport:
    """诊断报告"""
    scenario_id: str
    scenario_name: str
    severity: str
    confidence: float  # 0.0 ~ 1.0
    root_cause: str
    diagnosis_steps: List[str]
    solutions: List[str]
    prevention: str
    matched_symptoms: List[str]
    tools_used: List[str]

    def to_text(self) -> str:
        lines = [
            f"## 诊断报告：{self.scenario_name} [严重度: {self.severity}]",
            f"**置信度：** {self.confidence:.0%}",
            f"**根因：** {self.root_cause}",
            "",
            "**匹配症状：**",
        ]
        for s in self.matched_symptoms:
            lines.append(f"  - {s}")
        lines.append("")
        lines.append("**诊断步骤：**")
        for step in self.diagnosis_steps:
            lines.append(f"  - {step}")
        lines.append("")
        lines.append("**解决方案：**")
        for sol in self.solutions:
            lines.append(f"  - {sol}")
        lines.append("")
        lines.append(f"**预防措施：** {self.prevention}")
        lines.append(f"**推荐工具：** {', '.join(self.tools_used)}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "root_cause": self.root_cause,
            "diagnosis_steps": self.diagnosis_steps,
            "solutions": self.solutions,
            "prevention": self.prevention,
            "matched_symptoms": self.matched_symptoms,
            "tools_used": self.tools_used,
        }


# ---------------------------------------------------------------------------
# 运维知识库主类
# ---------------------------------------------------------------------------

class OpsKnowledgeBase:
    """运维知识库：预置故障场景 + BM25 检索"""

    def __init__(self, kb_path: Optional[str] = None) -> None:
        if kb_path is None:
            kb_path = str(Path(__file__).parent / "ops_scenarios.json")
        self.kb_path = Path(kb_path)
        self.scenarios: List[Dict[str, Any]] = []
        self.index = BM25Indexer()
        self._load()

    def _load(self) -> None:
        """加载知识库：优先从 JSON 文件，fallback 到内置场景"""
        if self.kb_path.exists():
            try:
                raw = json.loads(self.kb_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self.scenarios = raw
                else:
                    self.scenarios = raw.get("scenarios", [])
                logger.info(f"loaded {len(self.scenarios)} scenarios from {self.kb_path}")
            except Exception as e:
                logger.warning(f"failed to load {self.kb_path}: {e}, using built-in scenarios")
                self.scenarios = _OPS_SCENARIOS
        else:
            self.scenarios = _OPS_SCENARIOS
            logger.info(f"using built-in {len(self.scenarios)} scenarios")

        self._build_index()

    def _build_index(self) -> None:
        """构建 BM25 索引"""
        for s in self.scenarios:
            doc_text = " ".join([
                s.get("scenario", ""),
                " ".join(s.get("symptoms", [])),
                " ".join(s.get("keywords", [])),
                s.get("root_cause", ""),
            ])
            self.index.add_document(s["id"], doc_text)
        self.index.build()

    def retrieve(self, query: str, top_k: int = 3, min_score: float = 0.1) -> List[Dict[str, Any]]:
        """BM25 检索最相关场景"""
        if not query:
            return []
        doc_ids = [s["id"] for s in self.scenarios]
        scores = dict(self.index.score(query, doc_ids))
        results = []
        for s in self.scenarios:
            sid = s["id"]
            score = scores.get(sid, 0.0)
            if score >= min_score:
                results.append((s, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return [{"scenario": s, "score": round(sc, 3)} for s, sc in results[:top_k]]

    def match_by_symptoms(self, symptoms: List[str], top_k: int = 3) -> List[Dict[str, Any]]:
        """根据症状文本匹配场景"""
        query = " ".join(symptoms)
        return self.retrieve(query, top_k=top_k)

    def diagnose(self, symptoms: List[str]) -> List[DiagnosisReport]:
        """根因诊断：症状 → 匹配场景 → 生成报告"""
        matches = self.match_by_symptoms(symptoms, top_k=3)
        reports = []
        for m in matches:
            s = m["scenario"]
            matched_sxs = []
            for sx in s.get("symptoms", []):
                sx_lower = sx.lower()
                for user_sx in symptoms:
                    if any(kw in sx_lower for kw in [user_sx.lower(), user_sx]):
                        matched_sxs.append(sx)
                        break
            if not matched_sxs:
                matched_sxs = symptoms[:2]
            reports.append(DiagnosisReport(
                scenario_id=s["id"],
                scenario_name=s["scenario"],
                severity=s.get("severity", "medium"),
                confidence=m.get("score", 0.0) / 10.0,
                root_cause=s.get("root_cause", "未知"),
                diagnosis_steps=s.get("diagnosis_steps", []),
                solutions=s.get("solutions", []),
                prevention=s.get("prevention", ""),
                matched_symptoms=matched_sxs,
                tools_used=s.get("tools_used", []),
            ))
        return reports

    def scenario_count(self) -> int:
        return len(self.scenarios)

    def get_all_categories(self) -> List[str]:
        cats = set()
        for s in self.scenarios:
            cats.add(s.get("category", "other"))
        return sorted(cats)


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_default_kb: Optional[OpsKnowledgeBase] = None


def get_default_kb() -> OpsKnowledgeBase:
    global _default_kb
    if _default_kb is None:
        _default_kb = OpsKnowledgeBase()
    return _default_kb
