"""命令合法性校验器

提供：
- 主程序名提取（处理管道、分号、&&、||、反引号）
- 单个命令是否在知识库
- 危险参数检测
- 整体校验结果
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import get_logger
from .store import CmdStore, get_default_store

logger = get_logger("cmd_kb.validator")


# 危险模式（与 risk_rules.yaml 互补，独立维护以防知识库未收录）
_BUILTIN_BLACKLIST = {
    "rm",   # 删除：高危；但若带上具体参数会再做参数检查
    "mkfs", # 格式化：高危
    "dd",   # 字节级写：高危
    "shred",  # 安全删除
    "wipefs",
    "fdisk", "parted",
}


@dataclass
class ValidationIssue:
    """单条校验问题。"""
    severity: str   # error | warn
    kind: str       # unknown_cmd | dangerous_arg | builtin_risk
    message: str
    command: str
    arg: Optional[str] = None


@dataclass
class ValidationResult:
    """整体校验结果。"""
    valid: bool
    commands: List[str] = field(default_factory=list)   # 提取出的主程序名（去重保序）
    unknown_commands: List[str] = field(default_factory=list)
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def to_text(self) -> str:
        if not self.issues:
            return f"✓ 命令合法：{', '.join(self.commands)}"
        lines = []
        for i in self.issues:
            tag = "✗" if i.severity == "error" else "!"
            extra = f" 参数={i.arg}" if i.arg else ""
            lines.append(f"{tag} [{i.kind}] {i.message}（命令：{i.command}{extra}）")
        return "\n".join(lines)


class CmdValidator:
    """命令合法性校验器。"""

    def __init__(self, store: Optional[CmdStore] = None) -> None:
        self.store = store or get_default_store()

    def extract_program_names(self, cmdline: str) -> List[str]:
        """从复合命令中提取主程序名列表。

        处理：
        - 管道 `|`
        - 顺序执行 `;` / `&&` / `||`
        - 命令替换 `` ` ` `` / `$( )`
        - 重定向 `>` / `>>` / `<`
        - 前缀赋值 `VAR=val cmd`
        """
        if not cmdline:
            return []

        # 预处理：移除命令替换内部的命令（仅外层命令需要校验）
        cleaned = re.sub(r"`[^`]*`", "", cmdline)
        cleaned = re.sub(r"\$\([^)]*\)", "", cleaned)

        # 按 shell 分隔符切分
        pieces = re.split(r"[|;&]+", cleaned)
        names: List[str] = []
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            # 移除重定向部分
            piece = re.sub(r"[<>].*", "", piece)
            piece = piece.strip()
            if not piece:
                continue
            # 移除前导赋值
            piece = re.sub(r"^(\w+=\S+\s+)+", "", piece)
            # 移除 sudo / nohup 前缀
            piece = re.sub(r"^(sudo|nohup|time|env)\s+", "", piece)
            # 第一个 token 即主程序名
            try:
                tokens = shlex.split(piece, posix=True)
            except ValueError:
                tokens = piece.split()
            if not tokens:
                continue
            name = tokens[0]
            # 跳过 ./  /
            name = name.split("/")[-1]
            if name and name not in names:
                names.append(name)
        return names

    def validate(self, cmdline: str) -> ValidationResult:
        """校验一条完整命令行。"""
        names = self.extract_program_names(cmdline)
        result = ValidationResult(valid=True, commands=names)

        for name in names:
            entry = self.store.get(name)
            if entry is None:
                result.unknown_commands.append(name)
                result.issues.append(ValidationIssue(
                    severity="error",
                    kind="unknown_cmd",
                    message=f"命令 '{name}' 不在系统合法命令知识库中",
                    command=name,
                ))
                result.valid = False
                continue

            # 检查危险参数
            try:
                tokens = shlex.split(cmdline, posix=True)
            except ValueError:
                tokens = cmdline.split()

            for arg in tokens[1:]:
                if not arg.startswith("-"):
                    continue
                # 与 entry.dangerous_args 比对
                for danger in entry.dangerous_args:
                    if danger in arg or arg in danger:
                        result.issues.append(ValidationIssue(
                            severity="warn",
                            kind="dangerous_arg",
                            message=f"命令 '{name}' 使用了已知危险参数 '{arg}'（参考：{danger}）",
                            command=name,
                            arg=arg,
                        ))
                        break

            # 工具自身 risk
            if entry.risk == "high":
                result.issues.append(ValidationIssue(
                    severity="warn",
                    kind="high_risk_cmd",
                    message=f"命令 '{name}' 属于高危命令（{entry.category}），需二次确认",
                    command=name,
                ))

        return result