"""通用 shell 执行工具

让 LLM 可以执行任意 shell 命令（如 mkdir / tar / netstat 等）。
运行时强制两条护栏：
1. KB 校验：命令名必须在 CmdStore 中（不接受内置 blacklist 之外未登记的命令）
2. RiskClassifier：对完整命令行再做一次三档分级（policy_engine 一致）

权限模型（仿 Claude Code settings-strict）：
- `shell.execute`：**默认当前用户**（不再 nobody 降权）。
  谁启动 Agent 谁执行；写自己 `/home/alice/x` 直接成功。
- `shell.execute_elevated`：默认 HIGH 风险，需用户二次确认；
  允许 `sudo -n` 提权 root（在 policy.yaml 的 allow_root_for_tools 白名单内）。
- 两者都不再做"权限熔断→推荐 /tmp"——既然当前用户写得进去，就直接成功。
"""
from __future__ import annotations

import re
import shlex
import time
from typing import Any, Dict, Optional

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


def _classify(cmdline: str) -> Dict[str, Any]:
    """调用 policy_engine 的 risk_classifier 给单条命令分级。

    Returns dict {level, allow, reason}。
    """
    from safety.risk_classifier import RiskClassifier
    risk = RiskClassifier().classify_command(cmdline)
    return {
        "level": risk.level.value,
        "allow": risk.allow,
        "reason": risk.reason,
        "require_confirmation": risk.require_confirmation,
    }


class ShellExecuteTool(Tool):
    """通用 shell 命令执行器：默认当前用户，唯一拦截就是 sudo / su / doas。

    设计原则（用户已要求精简）：
    - 工具本身不再有 risk_level 等级
    - 不做二次确认（不弹 confirm_request）
    - 唯一拦截：命令含 sudo / su / doas / pkexec 一律拒绝
    - 命令名不在 KB 时直接拒绝（避免 LLM 编造工具）
    - 其它一律以启动 Agent 的当前用户身份直接执行（仿 Claude Code "never sudo"）
    """

    name = "shell.execute"
    description = (
        "执行任意 shell 命令（如 mkdir / rm / cp / chmod / lsof / ss / cat / grep ...）。"
        "**默认以启动 Agent 的当前用户身份执行**，无需二次确认、无 sudo。"
        "**唯一禁用**：命令里包含 sudo / su / doas / pkexec 时直接拒绝（用 shell.execute_elevated）。"
        "命令名必须在 KB 中（避免 LLM 编造）。"
        "返回 {returncode, stdout, stderr, duration_ms}。"
    )
    # 保留 LOW 仅为 schema 兼容；不再用于 confirm / 阻塞逻辑
    risk_level = RiskLevel.LOW
    required_capability = "shell.execute"
    input_schema = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "完整命令行（字符串）"},
            "cwd": {"type": "string", "description": "可选：执行工作目录"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30，最大 600"},
        },
        "required": ["cmd"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        cmdline: str = kwargs.get("cmd", "").strip()
        cwd: Optional[str] = kwargs.get("cwd")
        timeout: int = min(int(kwargs.get("timeout", 30)), 600)

        if not cmdline:
            return ToolResult(success=False, error="cmd 不能为空")

        if "\x00" in cmdline:
            return ToolResult(success=False, error="cmd 含非法字符")

        try:
            argv = shlex.split(cmdline)
        except ValueError as e:
            return ToolResult(success=False, error=f"shell 引号解析失败：{e}")
        if not argv:
            return ToolResult(success=False, error="cmd 解析后为空")

        # ★ 唯一拦截：禁止 sudo / su / doas / pkexec（仿 Claude Code "never sudo"） ★
        if argv and argv[0] in ("sudo", "su", "doas", "pkexec"):
            return ToolResult(
                success=False,
                error=(
                    "已拦截：命令以 sudo / su / doas / pkexec 开头。"
                    "所有命令都以启动 Agent 的当前用户身份直接执行（无需提权）。"
                    "如确需 root，请显式调用 `shell.execute_elevated` 工具。"
                ),
            )
        # 防御：禁止脚本里嵌套 sudo（`bash -c "sudo ..."` / `sh -c "su -c ..."`）
        _SUDO_INNER = re.compile(r"\b(sudo|su|doas|pkexec)\b")
        if argv and argv[0] in ("bash", "sh", "zsh", "dash", "ksh", "ash", "csh", "tcsh", "fish") \
                and _SUDO_INNER.search(cmdline):
            return ToolResult(
                success=False,
                error=(
                    "已拦截：嵌套脚本中检测到 sudo/su/doas/pkexec。"
                    "如确需 root，请显式调用 `shell.execute_elevated` 工具。"
                ),
            )

        # KB 校验（命令名不在 cmd_store 中只警告、不阻断 —— 用户已要求"不要抗拒"）
        from cmd_kb.validator import CmdValidator
        try:
            kb_result = CmdValidator().validate(cmdline)
        except Exception as e:
            # KB 校验失败也不再阻断（防御性兜底）
            kb_result = None
            unknown_warnings: List[str] = [f"KB 校验失败: {e}"]

        # 已精简：不再因 kb_result.valid=False 拒绝执行
        # 即使命令名不在 store / 有 dangerous_args，亦只警告（透传到 metadata）

        # 实际执行：默认当前用户（user=None）
        # ★ 沙箱模式：sandbox.enabled=true 时，shell.execute 在 bwrap 命名空间中执行
        #   （PID/IPC/UTS/Net 隔离 + /etc /boot /sys /proc 只读），
        #   即使 Agent 被注入执行 rm -rf /，也只能删沙箱内的 tmpfs，宿主机不受影响。
        # ★ 非沙箱模式：传字符串走 bash -c，支持管道 / 重定向 / 命令替换等所有合法 shell 语法
        start = time.time()
        try:
            from utils.config import get_setting
            sandbox_enabled = get_setting("sandbox.enabled", False)
            if sandbox_enabled:
                from execution.sandbox_linux import create_sandbox
                profile_name = get_setting("sandbox.profile", "shell_execute")
                sandbox = create_sandbox(profile_name)
                result = await sandbox.run(
                    ["bash", "-c", cmdline], timeout=timeout, cwd=cwd
                )
            else:
                result = await run_cmd(cmdline, user=None, timeout=timeout, cwd=cwd)
        except PermissionError as e:
            return ToolResult(success=False, error=str(e))
        except FileNotFoundError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=f"执行异常：{e}")

        kb_meta: Dict[str, Any] = {}
        if kb_result is not None:
            kb_meta["commands"] = kb_result.commands
            if kb_result.unknown_commands:
                kb_meta["unknown"] = kb_result.unknown_commands
            if kb_result.issues:
                kb_meta["kb_warnings"] = [
                    f"[{i.severity}] {i.message}" for i in kb_result.issues
                ]
        else:
            kb_meta["kb_error"] = (
                "; ".join(unknown_warnings) if 'unknown_warnings' in dir() else ""
            )

        # ★ 不再因 returncode != 0 标失败 —— 命令确实执行了，只是退出码非 0
        # （find 无匹配 / grep 无匹配 / test -f 不存在 等都属于"正常返回非 0"）。
        # 把 stderr 也放进 output，前端能看到完整输出。
        err_text = (result.stderr or "").strip()
        return ToolResult(
            success=True,
            output={
                "returncode": result.returncode,
                "stdout": result.stdout[:8000],
                "stderr": err_text[:2000],
                "duration_ms": int((time.time() - start) * 1000),
            },
            metadata=kb_meta,
            error=err_text if result.returncode != 0 else None,
        )


def _classify_for_test(cmdline: str) -> Dict[str, Any]:
    """测试入口：仅跑分类，不执行。"""
    return _classify(cmdline)


class ShellExecuteElevatedTool(Tool):
    """提权 shell 执行工具 —— 显式 root 路径（仿 Claude Code elevated Bash）。

    设计（精简版 —— 不再二次确认）：
    - 通过 `sudo -n` 提权到 root，前提是启动 Agent 的用户在 sudoers NOPASSWD 白名单内。
    - **唯一拦截**：与 shell.execute 一致，禁止 `sudo / su / doas` 嵌套（避免无穷递归提权）
    - 命令名在 KB 中才放行
    - 不再走 confirmation / risk_classifier 二级门禁
    - 真要执行会被 sudo 拒绝时返回明确错误（含修复 sudoers 的步骤提示）
    """

    name = "shell.execute_elevated"
    description = (
        "以 root 身份（通过 sudo -n）执行 shell 命令。"
        "要求启动 Agent 的用户在 /etc/sudoers.d/opsagent 配置 NOPASSWD。"
        "用于运维需要改系统配置 / 写 root 拥有目录的场景。"
        "返回 {returncode, stdout, stderr, duration_ms, elevated: True}。"
    )
    risk_level = RiskLevel.LOW  # 不再走 confirm；仅作 schema 兼容
    required_capability = "shell.execute.elevated"
    input_schema = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "完整命令行（字符串）"},
            "cwd": {"type": "string", "description": "可选：执行工作目录"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30，最大 600"},
        },
        "required": ["cmd"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        cmdline: str = kwargs.get("cmd", "").strip()
        cwd: Optional[str] = kwargs.get("cwd")
        timeout: int = min(int(kwargs.get("timeout", 30)), 600)

        if not cmdline:
            return ToolResult(success=False, error="cmd 不能为空")
        if "\x00" in cmdline:
            return ToolResult(success=False, error="cmd 含非法字符")

        try:
            argv = shlex.split(cmdline)
        except ValueError as e:
            return ToolResult(success=False, error=f"shell 引号解析失败：{e}")
        if not argv:
            return ToolResult(success=False, error="cmd 解析后为空")

        # ★ 禁止 sudo 嵌套（避免 sudo sudo） ★
        if argv and argv[0] in ("sudo", "su", "doas", "pkexec"):
            return ToolResult(
                success=False,
                error=(
                    "已拦截：elevated 工具内部不能再用 sudo / su / doas（避免无穷提权）。"
                    "本工具会自己 sudo -n 提权到 root，请直接写命令本体（如 `cat /etc/passwd`）。"
                ),
            )
        _SUDO_INNER = re.compile(r"\b(sudo|su|doas|pkexec)\b")
        if argv and argv[0] in ("bash", "sh", "zsh", "dash", "ksh") and _SUDO_INNER.search(cmdline):
            return ToolResult(
                success=False, error="elevated 工具嵌套脚本中检测到 sudo/su/doas，已拒绝",
            )

        # KB 校验
        from cmd_kb.validator import CmdValidator
        try:
            kb_result = CmdValidator().validate(cmdline)
        except Exception as e:
            return ToolResult(success=False, error=f"KB 校验失败: {e}")
        if not kb_result.valid:
            issues = "\n".join(f"  - {i.message}" for i in kb_result.issues)
            return ToolResult(
                success=False,
                error="命令未通过知识库校验：\n" + issues,
                metadata={"commands": kb_result.commands,
                          "unknown": kb_result.unknown_commands},
            )

        # 真正提权到 root：sudo -n <cmd>
        start = time.time()
        try:
            result = await run_cmd(cmdline, user="root", timeout=timeout, cwd=cwd)
        except PermissionError as e:
            return ToolResult(
                success=False,
                error=(
                    f"提权失败：{e}。可能原因：\n"
                    f"  1) 启动 Agent 的当前用户未配置 NOPASSWD sudoers；\n"
                    f"  2) 当前用户不在 sudoers 白名单。\n"
                    f"请在 /etc/sudoers.d/opsagent 增加（按需替换 username）：\n"
                    f"  thunder ALL=(ALL) NOPASSWD: ALL"
                ),
            )
        except FileNotFoundError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=f"执行异常：{e}")

        return ToolResult(
            success=result.returncode == 0,
            output={
                "returncode": result.returncode,
                "stdout": result.stdout[:8000],
                "stderr": result.stderr[:2000],
                "duration_ms": int((time.time() - start) * 1000),
                "commands": kb_result.commands,
                "elevated": True,
                "elevated_user": "root",
            },
            error=None if result.returncode == 0 else (result.stderr or f"returncode={result.returncode}"),
        )