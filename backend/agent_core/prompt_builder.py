"""系统提示与工具 schema 构建

集成 Linux 命令知识库：在系统提示中嵌入"可用命令清单"，
LLM 只能从知识库中选取命令生成操作。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from llm.base import Message
from mcp.manifest import manifest_to_provider_format
from mcp.registry import get_default_registry
from utils.logger import get_logger

logger = get_logger("agent_core.prompt_builder")


_SYSTEM_PROMPT = """你是麒麟智能运维 Agent（OpsAgent），部署在麒麟高级服务器版 V11 (LoongArch) 上。
你的职责是协助系统管理员完成日常运维任务，包括进程管理、网络诊断、磁盘清理、日志查询、服务管理、文件操作等。

## 行为准则
1. **安全优先**：在执行任何可能影响系统的操作前，先评估风险等级。
2. **最小权限**：使用工具时遵守最小权限原则，不要越权。
3. **可解释**：每一步都要说明你在做什么、为什么这样做。
4. **谨慎写操作**：删除、修改配置、重启服务等操作需谨慎，必要时主动询问。
5. **拒绝危险**：遇到 rm -rf、chmod 777、停止关键服务等请求时，直接拒绝并解释原因。
6. **命令知识库约束**（重要）：
   - **只能调用下方"可用工具清单"中明确列出的工具**，禁止编造或假设存在其他工具。
   - 若用户的需求超出可用工具能力，直接说明"知识库中无对应工具"，不要虚构。
   - 所有工具**直接执行，无二次确认**（仅 shell.execute_elevated 由 sudoers NOPASSWD 决定）。
   - 系统已内置 Linux 命令知识库（见 LLM 输入上下文），如需构造 shell 命令，**仅使用知识库内合法命令**。

## 你可用的工具（MCP Plugin）
{{tool_list}}

## 路径选择规则（写工作目录内的文件不受拦截）

Agent 以**启动它的当前用户**身份执行工具（不再 nobody 中间层）。
默认你能写到任何你自己拥有的文件，包括 `/home/<you>/...`。

- **写文件**：直接写用户给出的绝对路径。如果你写 `/home/alice/RJB/x.txt` 而 alice 不是你，**由内核自然拒绝**——这时才需要走 `shell.execute_elevated`（需要用户二次确认）。
- **临时 / 缓存文件**：建议写到 `/tmp/<name>`（全局可写，无需 sudo）。
- **持久化文件**：用用户在请求中**显式给出的绝对路径**；不要凭空"猜"用户家目录。
- **遇到权限错误时**：检查是不是路径本身的问题（例如你要写 alice 的目录）。
  如确需写非自己拥有的目录，使用 `shell.execute_elevated`（已含二次确认弹窗），用户批准后会以 root 身份执行。
- **不要主动 `chmod 777 /home/...`**：那是不可逆的破坏性操作，必须拒绝。

## 启动流程
- 用户提问时先理解意图；
- 若需要执行工具，直接选择工具名称 + 参数；
- `shell.execute` 默认当前用户**直接执行**（无二次确认、不弹窗）；`shell.execute_elevated` 走 root（仍需 sudo NOPASSWD，否则报"需要密码"）；
- 必要时主动解释下一步打算做什么、风险如何。
- **禁止无目的探测系统目录**：不要主动 ls /、cd /home、pwd、uname 等，除非用户明确要求或任务需要；
- **文件操作优先**：若任务在项目目录，先用 file.read 确认目录结构，不要盲目探测；
- **一次调用解决**：能用一个工具完成的任务，不要拆成多个工具分步调用。

## 工具选择原则（重要）
- **优先用专用工具**：若任务匹配 `process.list` / `disk.find_large` / `file.read` 等语义工具，先用它们（结构化更好、有业务校验）。
- **次选 `shell.execute`**：对于专用工具未覆盖的命令（lsof / ss / netstat / ping / iostat / rm / chmod / mv / systemctl 等），直接 `shell.execute {cmd: "<完整命令>"}`，**直接执行，无需确认**。
- **删除文件**：优先 `file.remove`（带路径安全 + symlink + protected 检查）；如需 `rm -rf` 这类复杂命令直接用 `shell.execute {cmd: "rm -rf ..."}`。
- **不要使用 sudo / su / doas**：所有 shell 命令都以启动 Agent 的当前用户身份执行；只有涉及系统目录（/etc、/boot 等）才用 `shell.execute_elevated`（前提是 sudoers 已配 NOPASSWD）。

## 当前环境
- 操作系统：麒麟高级服务器 V11（LoongArch）
- 默认运行用户：**启动 Agent 的当前用户**（如 alice / opsagent / root，取决于谁起的服务）
- 提权工具：`shell.execute_elevated`（HIGH 风险，二次确认后 `sudo -n` 提权到 root）
- 公共可写目录：`/tmp`（1777）、`/var/tmp`（1777），无 sudo 即可写
- 沙箱：`safety.sandbox.enabled=false`（默认关闭，可按 settings.yaml 开启 bwrap）
"""


class PromptBuilder:
    """构建系统提示与工具 schema。"""

    def __init__(self, registry=None, cmd_searcher=None, max_cmd_kb_in_prompt: int = 30) -> None:
        self.registry = registry
        self.cmd_searcher = cmd_searcher
        self.max_cmd_kb_in_prompt = max_cmd_kb_in_prompt

    def build_system_message(
        self,
        ctx_metadata: Optional[Dict[str, Any]] = None,
        local_context: Optional[Dict[str, Any]] = None,
    ) -> Message:
        manifests = []
        registry = self.registry or get_default_registry()
        for t in registry.list_tools():
            manifests.append(t.to_manifest())

        tool_lines = []
        for m in manifests:
            tool_lines.append(
                f"- **{m['name']}**: {m['description']}"
            )
        tool_list = "\n".join(tool_lines) if tool_lines else "（暂无工具）"

        # ★ 修复：原 _SYSTEM_PROMPT.format(tool_list=...) 把用户提示词里的 {xxx}
        # 占位符（如本机可用命令段里的 `{cmd}` 这种小括号含 cmd 的反引号片段）
        # 也当 placeholder 解析，导致 KeyError('cmd')。
        # 改用纯字符串替换（仅替换单一锚点 {{tool_list}}）。
        content = _SYSTEM_PROMPT.replace("{{tool_list}}", tool_list)

        # 若注入了命令知识库检索器，把可用命令总数附在系统提示中
        if self.cmd_searcher is not None:
            store = self.cmd_searcher.store
            stats = store.stats()
            content += "\n\n## Linux 命令知识库状态\n"
            content += f"- 已收录命令数：{stats['total']}\n"
            content += f"- 分类数：{len(stats['by_category'])}\n"
            content += f"- 风险分布：low={stats['by_risk'].get('low', 0)}, "
            content += f"medium={stats['by_risk'].get('medium', 0)}, "
            content += f"high={stats['by_risk'].get('high', 0)}\n"
            content += (
                "\n知识库内命令样本（首批）：\n"
                + "\n".join(
                    f"- {e.name} [{e.risk}, {e.category}]: {e.desc}"
                    for e in store.all()[:self.max_cmd_kb_in_prompt]
                )
            )
            content += (
                "\n\n当用户问『怎么看 XX / 怎么 XX』等问题时，**主动使用命令知识库**检索匹配的命令，"
                "再调用对应的 MCP 工具（如 disk.usage 对应 df、process.list 对应 ps 等）。"
            )

        # 受信任工作目录（会话级目录信任）
        workspace = (ctx_metadata or {}).get("workspace")
        if workspace and isinstance(workspace, dict):
            ws_root = workspace.get("root")
            ws_raw = workspace.get("raw")
            if ws_root:
                content += (
                    "\n\n## 受信任工作目录（Trusted Workspace）\n"
                    f"- 当前会话的 workspace 根：\n  ```\n  {ws_root}\n  ```\n"
                    f"- 用户原始输入：{ws_raw}\n"
                    "- **规则**：落在该目录内的 `medium` 风险操作（如 file.write / shell.execute "
                    "包含该目录的写入）会被自动放行，无需向用户二次确认。\n"
                    "- **越界保护**：对 workspace 外的路径继续走二次确认；任何对 `/etc /boot /sys "
                    "/proc /usr /bin /sbin /var/lib /var/log /root/.ssh` 等保护路径的访问均会被硬拦截。\n"
                    "- 切换 workspace：通过调用方提供的 set_workspace 接口；旧值会被覆盖、system 消息也会重建。\n"
                )

        # Stage 1 Task 1.4：本机环境（local context）注入
        if local_context:
            content += _render_local_context(local_context)

        return Message(role="system", content=content)

    def build_tools_schema(self) -> List[Dict[str, Any]]:
        registry = self.registry or get_default_registry()
        manifests = [t.to_manifest() for t in registry.list_tools()]
        return manifest_to_provider_format(manifests)

    def augment_user_message_with_kb(self, user_msg: Message) -> Message:
        """对单条 user 消息做知识库检索增强：把相关命令塞到 user 消息末尾。"""
        if self.cmd_searcher is None or not user_msg.content:
            return user_msg

        results = self.cmd_searcher.search(user_msg.content, top_k=6)
        if not results:
            return user_msg

        kb_text = "\n\n[系统提示：以下命令与你的问题相关，仅从这些命令中选择]\n"
        kb_text += self.cmd_searcher.search_to_text(user_msg.content, top_k=6)
        augmented = Message(
            role=user_msg.role,
            content=user_msg.content + kb_text,
            name=user_msg.name,
            tool_call_id=user_msg.tool_call_id,
            tool_calls=user_msg.tool_calls,
        )
        logger.debug(f"augmented user message with {len(results)} KB hits")
        return augmented


# ============================================================
# Stage 1 Task 1.4: 本机环境段（local_context）
# ============================================================

def _render_local_context(ctx: Dict[str, Any]) -> str:
    """把 collect_local_context() 返回的 dict 渲染成 system prompt 的一个段。"""
    lines = ["\n\n## 本机环境（local environment）"]
    arch = ctx.get("arch") or "unknown"
    distro = ctx.get("distro") or "unknown"
    kernel = ctx.get("kernel") or "unknown"
    lines.append(f"- 架构 / arch: `{arch}`")
    lines.append(f"- 发行版 / distro: `{distro}`")
    lines.append(f"- 内核 / kernel: `{kernel}`")

    if ctx.get("kernel_version"):
        lines.append(f"- 内核版本 / kernel_version: `{ctx.get('kernel_version')}`")
    if ctx.get("system"):
        lines.append(f"- 系统 / system: `{ctx.get('system')}`")
    if ctx.get("cpu_model") or ctx.get("cpu_cores"):
        cpu_model = ctx.get("cpu_model") or "unknown"
        cpu_cores = ctx.get("cpu_cores", 0)
        lines.append(f"- CPU: `{cpu_model}` × {cpu_cores} 核")
    if ctx.get("memory_total_gb"):
        lines.append(f"- 内存总量: {ctx.get('memory_total_gb')} GB")
        if ctx.get("memory_available_gb") is not None:
            lines.append(f"- 可用内存: {ctx.get('memory_available_gb')} GB")
    if ctx.get("executable_count") is not None:
        lines.append(f"- PATH 可执行文件数: {ctx.get('executable_count')}")

    # ── 本机可用 shell 命令索引（启动时扫描 PATH）────
    # 让 LLM 知道本机有哪些命令可用，避免在没装某工具时去拼 `command not found`。
    exes = ctx.get("executables") or []
    if exes:
        # 按字典序排序后取最多 200 个名字 + 提示是否被截断
        # 不全列：完整 KB 在 cmd_store 里，shell.execute 校验时会查
        sample = sorted(exes)[:200]
        suffix = " ..." if ctx.get("executables_truncated") else ""
        lines.append(
            f"\n## 本机可用 shell 命令（共 {ctx.get('executable_count')} 个，"
            f"展示前 {len(sample)}）{suffix}\n"
            + ", ".join(f"`{x}`" for x in sample)
            + "\n\n> LLM 应当基于上面这份名单判断命令是否存在。"
            "任意 shell 命令都可走 `shell.execute` 工具（KB 校验会兜底）。"
            "**禁止使用 sudo / su / doas**——统一以启动 Agent 的当前用户身份执行；"
            "需要 root 的操作在 `shell.execute_elevated` 中单独处理。"
        )
    top_history = ctx.get("top_history") or []
    if top_history:
        preview = top_history[:5]
        lines.append("- 最近 shell 命令（top 5）: " + ", ".join(f"`{c}`" for c in preview))
    lines.append(
        "\n> 上述信息来自 `tools.shell_discovery.collect_local_context()`，"
        "LLM 应基于本机实际环境给出命令（如在 x86 上别假设 `dpkg -i` 不可用，"
        "在麒麟 loongarch 上别假设 `apt-get` 一定存在）。"
    )
    return "\n".join(lines)


def build_system_message(
    ctx_metadata: Optional[Dict[str, Any]] = None,
    local_context: Optional[Dict[str, Any]] = None,
) -> Message:
    """便捷入口：直接调用默认 PromptBuilder 构造 system 消息。

    与 PromptBuilder().build_system_message(...) 等价，便于无需显式实例化
    的场景（如脚本 / 测试 / FastAPI 启动时一次性注入本机上下文）。
    """
    return PromptBuilder().build_system_message(
        ctx_metadata=ctx_metadata, local_context=local_context
    )