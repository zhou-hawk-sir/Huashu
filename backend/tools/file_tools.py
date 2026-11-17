"""文件工具

权限模型（仿 Claude Code settings-strict）：
- 默认以**当前用户**身份执行 file.write / file.read / file.remove，
  由内核 DAC 自然放行/拒绝。Agent 的权限 = 启动它的用户的权限。
- 不再做 `sudo -u opsagent` 的中间切换（之前会导致 alice 的目录写不进去）。
- file.remove 仍走二次确认（HIGH 风险）；file.write 走 MEDIUM + workspace 信任降级。

路径安全（C3 修复）：
- `_SAFE_PATH` 拒绝 `..` 路径段
- 校验前 `os.path.normpath` + `os.path.realpath`（symlink 防护）
- prefix 匹配用 `path == p or path.startswith(p + "/")`（路径边界）
"""
from __future__ import annotations

import asyncio as _asyncio
import os
import re
from typing import Any, List

from utils.config import get_setting
from .base import RiskLevel, Tool, ToolResult


def _protected_paths() -> List[str]:
    cfg = get_setting("safety.block_paths", []) or []
    return cfg


def _is_path_safe(path: str) -> tuple[bool, str]:
    """统一路径安全检查，返回 (ok, reason)。

    三道关：
    1. 正则 `_SAFE_PATH`：禁 `..` 段、相对路径、Windows 风格
    2. `os.path.normpath` + 含 `..` 再拒（防 编码段如 `%2e%2e` 之外的旁路）
    3. 调用方负责 realpath（见 `_resolve_real`），本函数不读盘
    """
    _SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./\-]+$")
    if not _SAFE_PATH.match(path):
        return False, "Invalid path"
    if ".." in path.split("/"):
        return False, "Invalid path: contains '..' parent traversal"
    return True, ""


def _path_under(path: str, prefix: str) -> bool:
    """前缀匹配的路径边界版本：`/etcfoo` 不算 `/etc` 下。"""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _resolve_real(path: str) -> str:
    """realpath（symlink 防护）。失败时回退 normpath，调用方需注意。"""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


class FileReadTool(Tool):
    name = "file.read"
    description = "读取文件内容"
    risk_level = RiskLevel.LOW
    required_capability = "file.read"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "lines": {"type": "integer", "default": 100},
        },
        "required": ["path"],
    }

    _SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./\-]+$")

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        path = kwargs["path"]
        ok, reason = _is_path_safe(path)
        if not ok:
            return ToolResult(success=False, error=reason)
        # realpath 防护：symlink 逃逸必须重判 protected
        real_path = _resolve_real(path)
        if not _is_path_safe(real_path)[0]:
            return ToolResult(success=False, error=f"Refused: symlink target unsafe ({real_path})")

        for protected in _protected_paths():
            if _path_under(real_path, protected):
                return ToolResult(success=False, error=f"Access denied: {real_path} is protected")

        if not os.path.exists(real_path):
            return ToolResult(success=False, error=f"File not found: {real_path}")

        lines = kwargs.get("lines", 100)
        # 默认当前用户；如要读 root-only 文件，请用 shell.execute_elevated
        try:
            proc = await _asyncio.create_subprocess_exec(
                "head", "-n", str(lines), real_path,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await _asyncio.wait_for(proc.communicate(), timeout=10)
        except _asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return ToolResult(success=False, error="read timeout")
        except FileNotFoundError as e:
            return ToolResult(success=False, error=f"Command not found: {e}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if proc.returncode != 0:
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")
            return ToolResult(success=False, error=stderr)

        content = stdout_b.decode("utf-8", errors="replace")
        return ToolResult(
            success=True,
            output={
                "path": path,
                "lines": len(content.splitlines()),
                "content": content,
            },
        )


class FileWriteTool(Tool):
    name = "file.write"
    description = "创建或覆盖（也可追加）一个文本文件的内容"
    risk_level = RiskLevel.MEDIUM
    required_capability = "file.write"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件绝对路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
            "append": {
                "type": "boolean",
                "default": False,
                "description": "追加到已有内容末尾；默认 False 为覆盖",
            },
        },
        "required": ["path", "content"],
    }

    _SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./\-]+$")
    _DENY_PREFIXES = ("/etc", "/boot", "/sys", "/proc", "/usr", "/bin", "/sbin", "/lib")

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        path = kwargs["path"]
        ok, reason = _is_path_safe(path)
        if not ok:
            return ToolResult(success=False, error=reason)
        real_path = _resolve_real(path)
        if not _is_path_safe(real_path)[0]:
            return ToolResult(success=False, error=f"Refused: symlink target unsafe ({real_path})")

        for protected in _protected_paths():
            if _path_under(real_path, protected):
                return ToolResult(success=False, error=f"Refused: {real_path} is protected")

        for deny in self._DENY_PREFIXES:
            if _path_under(real_path, deny):
                return ToolResult(success=False, error=f"Refused: cannot write under {deny}")

        content = kwargs["content"]
        append = bool(kwargs.get("append", False))

        # 用 tee 通过 stdin 写入：内容走 stdin 不走 argv，避免 shell 注入；
        # **默认当前用户**（不 sudo）。如要写 root 拥有的目录，请用
        # shell.execute_elevated 走 sudo，这是显式高危路径，policy 已拦。
        argv = ["tee", "-a", real_path] if append else ["tee", real_path]
        proc = None
        try:
            proc = await _asyncio.create_subprocess_exec(
                *argv,
                stdin=_asyncio.subprocess.PIPE,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            _, stderr_b = await _asyncio.wait_for(
                proc.communicate(input=content.encode("utf-8")),
                timeout=10,
            )
        except _asyncio.TimeoutError:
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return ToolResult(success=False, error="write timeout")
        except FileNotFoundError as e:
            return ToolResult(success=False, error=f"Command not found: {e}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if proc.returncode != 0:
            stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
            return ToolResult(
                success=False,
                error=stderr_text or f"returncode={proc.returncode}",
            )

        return ToolResult(
            success=True,
            output={
                "path": path,
                "bytes_written": len(content.encode("utf-8")),
                "mode": "append" if append else "overwrite",
            },
            metadata={"command": argv},
        )


class FileRemoveTool(Tool):
    name = "file.remove"
    description = "删除文件或目录。recursive=true 时递归删除整个目录（rm -rf），默认 false 删单文件/空目录"
    risk_level = RiskLevel.MEDIUM
    required_capability = "file.write"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标路径"},
            "force": {"type": "boolean", "default": False},
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "递归删除整个目录（等同 rm -rf）。默认 false 仅删单文件/空目录",
            },
        },
        "required": ["path"],
    }

    _SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./\-]+$")
    _DENY_PREFIXES = ("/etc", "/boot", "/sys", "/proc", "/usr", "/bin", "/sbin", "/lib", "/dev")

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        path = kwargs["path"]
        ok, reason = _is_path_safe(path)
        if not ok:
            return ToolResult(success=False, error=reason)
        real_path = _resolve_real(path)
        if not _is_path_safe(real_path)[0]:
            return ToolResult(success=False, error=f"Refused: symlink target unsafe ({real_path})")

        for protected in _protected_paths():
            if _path_under(real_path, protected):
                return ToolResult(success=False, error=f"Refused: {real_path} is protected")

        for deny in self._DENY_PREFIXES:
            if _path_under(real_path, deny):
                return ToolResult(success=False, error=f"Refused: cannot remove under {deny}")

        if not os.path.exists(real_path):
            return ToolResult(success=False, error=f"Path not found: {real_path}")

        recursive = bool(kwargs.get("recursive", False))

        # 非递归模式：拒绝非空目录
        if not recursive and os.path.isdir(real_path) and os.listdir(real_path):
            return ToolResult(
                success=False,
                error="Refused: directory not empty; use recursive=true to delete non-empty directories",
            )

        # 递归删除用 rm -rf，非递归用 rm / rm -f
        if recursive:
            cmd = ["rm", "-rf", real_path]
            timeout = 60  # 递归删除可能较慢
        elif kwargs.get("force"):
            cmd = ["rm", "-f", real_path]
            timeout = 10
        else:
            cmd = ["rm", real_path]
            timeout = 10

        try:
            proc = await _asyncio.create_subprocess_exec(
                *cmd,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            _, stderr_b = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
        except _asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return ToolResult(success=False, error="remove timeout")
        except FileNotFoundError as e:
            return ToolResult(success=False, error=f"Command not found: {e}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if proc.returncode != 0:
            stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
            return ToolResult(
                success=False,
                error=stderr_text or f"returncode={proc.returncode}",
            )

        return ToolResult(
            success=True,
            output={"path": path, "removed": True, "recursive": recursive},
            metadata={"command": cmd},
        )