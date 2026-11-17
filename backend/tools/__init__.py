"""运维工具集 - 整合后版本（最终去重）。

设计原则：能用 shell 命令的不重复造轮子。
- shell.execute / shell.execute_elevated 是统一入口，受 KB + 风险分类器 + 二次确认守门。
- 只保留有**复杂解析**或**业务校验**的工具（避免 LLM 在 40+ 个薄壳里迷路）。

暴露给 LLM 的工具：
- shell.execute / shell.execute_elevated（统一入口，受 policy 守门）
- process.list / process.kill（ps 解析 / 信号安全校验）
- disk.find_large / disk.cleanup（业务校验）
- log.journal（journalctl 集成）
- file.read / file.write / file.remove（路径安全 + 审计）
- service.status / service.restart（二次确认）
- web_search

被合并进 shell.execute 的薄壳（LLM 直接用 shell 等价命令即可）：
- process.lsof       → lsof -nP [-iTCP ...] [-c cmd] [-u user]
- process.ports      → ss -tlnp / netstat -tlnp / lsof -iTCP -sTCP:LISTEN
- process.fd         → ls -la /proc/<pid>/fd
- network.list       → ss -tunlp || netstat -tunlp

剩余的旧工具类（lsof_tools.py / network_tools.py / disk_tools.py 中的薄壳）已
不在 all_tools() 中暴露；定义保留以备历史 import 兼容，可后续 Stage 删除。

被移除（不在 all_tools 暴露）：
- sys_tools：SystemInfoTool / MemoryInfoTool / LoadAvgTool / UlimitTool / DmesgTool
- system_tools：6 个 system.*Tool
- iotop_tools：IOStatTool / IOTopTool / DfTool / DuTool
- disk.usage：df -h 薄壳
- network.connectivity：ping 薄壳
- shell_discovery 四个 Tool（信息已并入 /api/system/local-context）
"""
from .base import Tool, ToolResult, RiskLevel
from . import process_tools
from . import disk_tools  # 仅保留 find_large / cleanup
from . import log_tools
from . import file_tools
from . import service_tools
from . import shell_tools
from .web_search import WebSearchTool
# 故意不导入 lsof_tools / network_tools —— 已并入 shell.execute
# 故意不导入 sys_tools / system_tools / iotop_tools —— 已被 shell.execute 取代


def all_tools():
    """返回实际暴露给 LLM 的工具实例（最终整合版）。"""
    return [
        # ---- 进程管理（语义化：ps 解析 + 安全信号）----
        process_tools.ProcessListTool(),
        process_tools.ProcessKillTool(),
        # ---- 磁盘（仅含业务校验）----
        disk_tools.DiskFindLargeTool(),
        disk_tools.DiskCleanupTool(),
        # ---- 日志（journalctl 集成）----
        log_tools.LogJournalTool(),
        # ---- 文件（路径安全 + 审计）----
        file_tools.FileReadTool(),
        file_tools.FileWriteTool(),
        file_tools.FileRemoveTool(),
        # ---- 服务（二次确认）----
        service_tools.ServiceStatusTool(),
        service_tools.ServiceRestartTool(),
        # ---- Shell 统一入口（默认当前用户；涉及受保护路径再走 elevated）----
        shell_tools.ShellExecuteTool(),
        shell_tools.ShellExecuteElevatedTool(),
        # ---- Web 搜索 ----
        WebSearchTool(),
    ]


__all__ = ["Tool", "ToolResult", "RiskLevel", "all_tools", "SHELL_EQUIVALENTS"]


# 旧薄壳工具的 shell 等价命令速查（便于运维定位）：
SHELL_EQUIVALENTS = {
    "process.lsof":    "lsof -nP [-iTCP] [-c cmd] [-u user]",
    "process.ports":   "ss -tlnp  ||  netstat -tlnp  ||  lsof -iTCP -sTCP:LISTEN",
    "process.fd":      "ls -la /proc/<pid>/fd",
    "network.list":    "ss -tunlp  ||  netstat -tunlp",
    "disk.usage":      "df -h",
    "network.connectivity": "ping -c 3 -W 2 <host>",
}
