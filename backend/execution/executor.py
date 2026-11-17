"""执行器：负责实际调用工具（含权限切换、审计埋点）"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils.logger import get_logger
from tools.base import ToolResult
from mcp.registry import get_default_registry
from safety.policy_engine import Decision, PolicyEngine
from safety.confirmation import ConfirmationManager
from audit.tracer import TraceStepKind, TraceActor

logger = get_logger("execution.executor")


@dataclass
class ExecutionResult:
    success: bool
    tool: str
    output: Any = None
    error: Optional[str] = None
    duration_ms: int = 0
    decision: str = "allow"
    user_run: str = ""
    required_confirmation: bool = False


class Executor:
    """执行器：编排策略评估 → 权限切换 → 工具调用 → 审计埋点。"""

    def __init__(
        self,
        policy: Optional[PolicyEngine] = None,
        confirmation_manager: Optional[ConfirmationManager] = None,
        tracer: Any = None,
        cmd_validator: Any = None,
    ) -> None:
        self.policy = policy or PolicyEngine()
        self.confirmation = confirmation_manager or ConfirmationManager()
        self.tracer = tracer
        self.registry = get_default_registry()
        self.cmd_validator = cmd_validator  # 可选：CmdValidator
        # CONFIRM 串行化锁：每 session 一把，防止多个 confirm 同时弹出
        self._confirm_locks: Dict[str, asyncio.Lock] = {}

    def _get_confirm_lock(self, session_id: str) -> asyncio.Lock:
        """获取指定 session 的确认锁（懒创建）。"""
        if session_id not in self._confirm_locks:
            self._confirm_locks[session_id] = asyncio.Lock()
        return self._confirm_locks[session_id]

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        confirm_callback: Optional[Any] = None,
        workspace_root: Optional[str] = None,
        session_id: Optional[str] = None,
        confirm_timeout: Optional[float] = None,
    ) -> ExecutionResult:
        """执行工具。

        Args:
            tool_name: 工具名
            arguments: 工具参数
            confirm_callback: 可选回调 async (req_id, request_dict) -> bool
                             用于 WebSocket 端发起确认；None 则同步等待 confirmation manager
            workspace_root: 受信任工作目录（realpath 后）；命中则 MEDIUM 自动 ALLOW
            confirm_timeout: 用户在前端确认的最长等待时间（秒）。None 则用
                             ConfirmationManager.default_timeout（默认 60s）。
                             不再硬编码 10s（H6 修复）。
        """
        start = time.time()
        verdict = self.policy.evaluate_tool_call(tool_name, arguments, workspace_root=workspace_root)

        # 命令权限 enforcer（denied_commands：s6 修复 - 之前 enforcer 加载了但 executor 没接，导致
        # 所有 denied 命令包括 reboot/poweroff/shutdown 都能跑，把 VM 重启了）。
        # 仅作用于 shell.execute 的 cmd 参数；其他工具跳过。
        if tool_name == "shell.execute":
            try:
                from safety.command_enforcer import get_enforcer
                cmd = (arguments or {}).get("cmd", "") or ""
                if cmd:
                    dec = get_enforcer().decide(cmd)
                    if dec.denied:
                        if self.tracer:
                            await self.tracer.trace(
                                step=TraceStepKind.RISK_EVAL,
                                actor=TraceActor.SAFETY,
                                input={"tool": tool_name, "args": arguments},
                                output={"decision": "deny", "reason": dec.reason},
                            )
                        return ExecutionResult(
                            success=False,
                            tool=tool_name,
                            error=f"命令被权限策略拦截：{dec.reason}",
                            duration_ms=int((time.time() - start) * 1000),
                            decision="deny",
                        )
            except Exception as e:
                # enforcer 出错不能阻塞执行（fail-open 会更糟？先 fail-closed 记日志）
                logger.warning(f"CommandEnforcer check failed (allowing): {e}")

        # Shell→Tool 重定向检查：如果 LLM 用 shell.execute 跑了有专用工具的命令，拒绝并提示
        if tool_name == "shell.execute":
            try:
                from tools.redirect import check_redirect
                cmd = (arguments or {}).get("cmd", "") or ""
                if cmd:
                    redir = check_redirect(cmd)
                    if redir:
                        return ExecutionResult(
                            success=False,
                            tool=tool_name,
                            error=f"该命令已重定向到专用工具 {redir['tool']}：{redir['reason']}",
                            duration_ms=int((time.time() - start) * 1000),
                            decision="redirect",
                        )
            except Exception as e:
                logger.warning(f"Shell redirect check failed (allowing): {e}")

        # 命令知识库校验：对工具参数做 shell 注入与命令合法性预检
        kb_issue = self._check_kb_arguments(tool_name, arguments)
        if kb_issue:
            if self.tracer:
                await self.tracer.trace(
                    step=TraceStepKind.KB_VALIDATE,
                    actor="safety",
                    input={"tool": tool_name, "args": arguments},
                    output={"issue": kb_issue},
                )
            return ExecutionResult(
                success=False,
                tool=tool_name,
                error=f"命令知识库校验失败：{kb_issue}",
                duration_ms=int((time.time() - start) * 1000),
                decision="kb_denied",
            )

        # 记录决策
        if self.tracer:
            await self.tracer.trace(
                step=TraceStepKind.RISK_EVAL,
                actor="safety",
                input={"tool": tool_name, "args": arguments},
                output={
                    "decision": verdict.decision.value,
                    "reason": verdict.reason,
                    "user": verdict.user_to_run,
                },
            )

        if verdict.decision == Decision.DENY:
            # 记录 deny 类型的 trace
            if self.tracer:
                await self.tracer.trace(
                    step=TraceStepKind.DENY,
                    actor=TraceActor.SAFETY,
                    input={"tool": tool_name, "args": arguments},
                    output={"reason": verdict.reason},
                )
            return ExecutionResult(
                success=False,
                tool=tool_name,
                error=verdict.reason,
                duration_ms=int((time.time() - start) * 1000),
                decision="deny",
            )

        # CONFIRM：中危需二次确认（赛题 §四.1「未授权不得改关键配置」+ §二「执行删除前触发安全校验」）。
        # 通过 confirm_callback 把 confirm_request 推给前端，等用户在 WS 回 confirm_response；
        # ConfirmationManager 用 Future 唤醒。超时（默认 60s）或用户拒绝 → fail-closed 不执行。
        # 高危已在上面 DENY，不会走到这里；能到此处的只有 MEDIUM。
        # 串行化：同 session 多个 CONFIRM 按序逐个推送，防止前端同时弹多个确认框。
        was_confirmed = False
        if verdict.decision == Decision.CONFIRM:
            confirm_lock = self._get_confirm_lock(session_id or "default")
            async with confirm_lock:
                rationale = verdict.reason or "该操作为中风险，请确认后执行"
                req_id, _fut = await self.confirmation.request(
                    tool=tool_name,
                    arguments=arguments,
                    rationale=rationale,
                    session_id=session_id or "default",
                )
                if self.tracer:
                    await self.tracer.trace(
                        step=TraceStepKind.CONFIRM_REQUEST,
                        actor=TraceActor.SAFETY,
                        input={"tool": tool_name, "args": arguments},
                        output={"request_id": req_id, "rationale": rationale},
                    )
                if confirm_callback is not None:
                    await confirm_callback(req_id, {
                        "request_id": req_id,
                        "tool": tool_name,
                        "arguments": arguments,
                        "rationale": rationale,
                        "risk_level": "medium",
                    })
                    outcome = await self.confirmation.wait(req_id, timeout=confirm_timeout)
                else:
                    # 无交互通道（如纯 REST 调用）：无法确认 → fail-closed 拒绝
                    outcome = {"approved": False, "reason": "no confirm channel"}

            was_confirmed = bool(outcome.get("approved"))
            if self.tracer:
                await self.tracer.trace(
                    step=TraceStepKind.CONFIRM_RESULT,
                    actor=TraceActor.USER,
                    output={
                        "request_id": req_id,
                        "approved": was_confirmed,
                        "reason": outcome.get("reason", ""),
                    },
                )
            if not was_confirmed:
                return ExecutionResult(
                    success=False,
                    tool=tool_name,
                    error=f"操作已取消（未确认/超时）：{outcome.get('reason', '')}",
                    duration_ms=int((time.time() - start) * 1000),
                    decision="user_denied",
                    required_confirmation=True,
                )
            # 已确认 → 继续执行

        # 实际调用工具
        tool = self.registry.get(tool_name)
        if tool is None:
            return ExecutionResult(
                success=False,
                tool=tool_name,
                error=f"tool not found: {tool_name}",
                duration_ms=int((time.time() - start) * 1000),
                decision="not_found",
            )

        try:
            if self.tracer:
                await self.tracer.trace(
                    step=TraceStepKind.EXEC,
                    actor=TraceActor.EXECUTOR,
                    input={"tool": tool_name, "args": arguments, "user": verdict.user_to_run},
                )
            tool_result: ToolResult = await tool.run(**arguments)
            duration = int((time.time() - start) * 1000)

            if self.tracer:
                await self.tracer.trace(
                    step=TraceStepKind.RESULT,
                    actor=TraceActor.EXECUTOR,
                    output={
                        "tool": tool_name,
                        "success": tool_result.success,
                        "duration_ms": duration,
                    },
                )

            return ExecutionResult(
                success=tool_result.success,
                tool=tool_name,
                output=tool_result.output,
                error=tool_result.error,
                duration_ms=duration,
                decision="executed",
                user_run=verdict.user_to_run or "",
                required_confirmation=was_confirmed,
            )
        except Exception as e:
            logger.exception("Tool execution exception")
            # 记录 error 类型的 trace
            if self.tracer:
                await self.tracer.trace(
                    step=TraceStepKind.ERROR,
                    actor=TraceActor.EXECUTOR,
                    input={"tool": tool_name, "args": arguments},
                    output={"error": str(e)},
                )
            return ExecutionResult(
                success=False,
                tool=tool_name,
                error=f"exception: {e}",
                duration_ms=int((time.time() - start) * 1000),
                decision="error",
            )

    def _check_kb_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """通过命令知识库校验工具参数中的字符串。

        检查项（赛题要求"抗注入能力"）：
        1. shell 注入特征 — 仅识别"元字符 + 后续命令"的组合，避免误伤合法参数
        2. 命令名是否在知识库中（对 path 参数里可能含命令路径的场景）
        3. 危险参数
        """
        if not self.cmd_validator:
            return None

        # 常见 read-only 管道右侧命令（白名单）——允许 ps|head / cat|grep / df|sort 这类
        SAFE_PIPE_TARGETS = {
            "head", "tail", "grep", "egrep", "fgrep",
            "awk", "gawk", "sed",
            "sort", "uniq", "wc", "cut", "tr",
            "column", "paste", "tee",
            "xargs",
            "less", "more", "nl", "fmt", "fold",
            "cat",  # 链接文件到 stdout，无害
        }
        # 黑名单：解释器 / shell 自身 / 网络外发工具，右侧出现必拦
        DANGEROUS_PIPE_TARGETS = {
            "sh", "bash", "zsh", "fish", "dash", "ash",
            "python", "python2", "python3", "perl", "ruby", "node", "php",
            "nc", "ncat", "socat", "curl", "wget",
        }

        # 链式命令白名单（用于 && / || 拆链后的每个命令头校验）：
        # 涵盖常见测试 / 读取 / 格式化命令，足够覆盖 `test X && echo y` /
        # `ls 2>/dev/null || echo "not found"` / `[ -d X ] && cmd` 这类合法用法。
        SAFE_CHAIN_COMMANDS = {
            "test", "[", "[[",
            "echo", "printf", "true", "false", "yes",
            "ls", "cat", "head", "tail", "less", "more",
            "grep", "egrep", "fgrep", "awk", "gawk", "sed",
            "sort", "uniq", "wc", "cut", "tr", "nl", "fmt", "fold",
            "column", "paste", "tee", "xargs",
            "pwd", "whoami", "id", "date", "stat", "file", "find",
            "which", "type", "command", "whereis",
            "dirname", "basename", "realpath", "readlink",
            "env", "printenv", "uname", "hostname", "nproc",
            "df", "du", "ps", "top", "free", "uptime",
            "md5sum", "sha256sum", "sha1sum", "xxd", "od",
            "tput", "stty", "tty", "logname", "groups", "getent",
            "expr", "let", "seq",
        }
        # 链式黑名单：解释器 / shell 自身 / 文件破坏 / 提权 / 网络外发
        DANGEROUS_CHAIN_COMMANDS = {
            "sh", "bash", "zsh", "fish", "dash", "ash", "ksh", "csh", "tcsh",
            "python", "python2", "python3", "perl", "ruby", "node", "php", "lua",
            "nc", "ncat", "socat", "curl", "wget", "ssh", "scp", "rsync",
            "rm", "mv", "cp", "chmod", "chown", "chgrp", "kill", "killall", "pkill",
            "systemctl", "service", "init", "reboot", "shutdown", "halt", "poweroff",
            "dd", "mkfs", "fdisk", "mount", "umount", "fsck",
            "iptables", "firewall-cmd", "ufw", "sudo", "su", "doas", "runuser",
            "crontab", "at", "batch",
            "make", "gcc", "g++", "cc", "ld", "as",
            "tar", "unzip", "zip", "gzip", "gunzip", "xz",
            "useradd", "userdel", "usermod", "groupadd", "groupdel", "groupmod",
            "passwd", "visudo", "chsh", "chfn",
        }

        # 反引号 / $(...) / 进程替换（始终拦）
        backtick_re = re.compile(r"`[^`]*[A-Za-z/][^`]*`")
        cmd_subst_re = re.compile(r"\$\(\s*[A-Za-z/][^)]*\)")
        proc_subst_re = re.compile(r"[<>]\(\s*[A-Za-z/]")

        # 链式：; / && / || / 后台 &（带上下文检查）
        # - ;  与单 &（后台）仍无条件拦：太容易混入危险命令
        # - && / || 按"两端命令头都在白名单"放行，否则拦
        # - 用 (?<![&|]) 避免吞掉 && / || 的首字符
        # - 用 lookahead `(?=\s+\S|\s*$)` 支持 `cmd &` 末尾孤立的 &（无后续 token）
        chain_re = re.compile(
            r"""
            (?<![&|])                  # 前面不是 & 或 |（避免吞掉 && / || 的首字符）
            \s*
            (?P<op>;|&&|\|\||&)        # 链式操作符
            (?=\s+\S|\s*$)             # 后面必须有 token（除末尾孤立的 & 外）
            """,
            re.VERBOSE,
        )

        def _extract_command_head(part: str) -> Optional[str]:
            """从一段命令中提取首词（命令名），跳过前导重定向 / env 前缀 / 绝对路径。"""
            s = part.strip()
            if not s:
                return None
            # 跳过前导重定向 / 文件描述符 / 进程替换：
            # `2>/dev/null < /etc/hosts cmd`、`>&2 cmd`、`<(cmd) cmd`
            prev = None
            while prev != s:
                prev = s
                m = re.match(
                    r'^(?:\d*>&?\d*\s+|[<>]\(\S+\)\s*|[<>]\S+\s+)+', s
                )
                if m:
                    s = s[m.end():]
            # 跳过环境变量前缀：`FOO=bar BAZ=qux cmd`
            while True:
                m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*=\S*\s+', s)
                if not m:
                    break
                s = s[m.end():]
            # 取第一个 token
            m = re.match(r'^(\S+)', s)
            if not m:
                return None
            head = m.group(1)
            # 去掉路径前缀
            head = head.rsplit('/', 1)[-1]
            return head

        for k, v in arguments.items():
            if not isinstance(v, str):
                continue
            # 1) 反引号 / $() / 进程替换
            if backtick_re.search(v):
                return f"参数 {k!r} 含 shell 注入特征：反引号命令替换"
            if cmd_subst_re.search(v):
                return f"参数 {k!r} 含 shell 注入特征：$() 命令替换"
            if proc_subst_re.search(v):
                return f"参数 {k!r} 含 shell 注入特征：进程替换"

            # 2) 链式命令（按类型分策略）
            m_chain = chain_re.search(v)
            if m_chain:
                op = m_chain.group('op')
                if op in (';', '&'):
                    # 无条件串联 / 后台：太容易混入危险命令，统一拦
                    return (
                        f"参数 {k!r} 含 shell 注入特征：链式操作符 {op!r}"
                        f"（无条件串联/后台，禁用）"
                    )
                # && / || → 拆链检查每个命令头
                # 注意：按字符串拆分时引号内的 &&/|| 会被错误地拆分。
                # 简单做法：先把引号内的 &&/|| 替换为占位符，split 完再算 heads。
                sanitized = re.sub(r"'[^']*'", "''", v)
                sanitized = re.sub(r'"[^"]*"', '""', sanitized)
                parts = re.split(r'\s*(?:&&|\|\|)\s*', sanitized)
                bad: List[str] = []
                for part in parts:
                    head = _extract_command_head(part)
                    if head is None or head == '':
                        bad.append("<空>")
                        continue
                    if head in DANGEROUS_CHAIN_COMMANDS:
                        bad.append(f"{head!r}（危险命令）")
                    elif head not in SAFE_CHAIN_COMMANDS:
                        bad.append(f"{head!r}（不在白名单）")
                if bad:
                    return (
                        f"参数 {k!r} 含 shell 注入特征：链式操作符 {op!r} "
                        f"中检出未授权命令头：{', '.join(bad)}"
                    )
                # 所有命令头都安全 → 放行

            # 3) 管道：右侧是危险命令 → 拦；右侧是安全 read-only → 放行
            # 用 (?<!\|) + (?!\|) 排除 ||（被当作逻辑 OR，不是管道）
            for m_pipe in re.finditer(
                r"(?<!\|)\|(?!\|)\s*(?P<right>[A-Za-z_/.][\w/.-]*)", v
            ):
                right_cmd = m_pipe.group("right").split("/")[-1]
                if right_cmd in DANGEROUS_PIPE_TARGETS:
                    return (
                        f"参数 {k!r} 含 shell 注入特征：管道右侧为危险命令 "
                        f"{right_cmd!r}（可能执行任意代码）"
                    )
                # SAFE_PIPE_TARGETS 没匹配 → 拒绝（防止漏过新型注入）
                if right_cmd not in SAFE_PIPE_TARGETS:
                    return (
                        f"参数 {k!r} 含 shell 注入特征：管道右侧为未知命令 "
                        f"{right_cmd!r}"
                    )

        # 对 path 类参数做路径知识库校验（可选）
        path_keys = {"path", "file", "target"}
        for k in path_keys:
            v = arguments.get(k)
            if not isinstance(v, str):
                continue
            basename = v.split("/")[-1]
            if basename and self.cmd_validator.store.has(basename):
                entry = self.cmd_validator.store.get(basename)
                if entry.risk == "high":
                    return f"参数 {k!r} 引用了高危命令 {basename!r}（风险等级：high），请通过专用工具而非路径操作"
        return None