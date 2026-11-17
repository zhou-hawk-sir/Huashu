"""审计脱敏器（H2）

规则：
- 字段名匹配 `_PASSWORD_FIELDS` → 值替换为 "***"
- 字段名匹配 `_TOKEN_FIELDS` → 值替换为 "***"
- content / cmd / text 中出现的 `key=value` / `password=...` / `token=...` 模式 → 值替换
- 超过 `_CMD_TRUNCATE_LEN` 的 cmd 截断（保留前 200 + 后 50）
"""
from __future__ import annotations

import re
from typing import Any, Dict

_PASSWORD_FIELDS = frozenset({
    "password", "passwd", "pwd", "secret", "private_key",
    "client_secret", "db_password", "mysql_pwd", "postgres_pwd",
})
_TOKEN_FIELDS = frozenset({
    "token", "api_key", "apikey", "access_token", "refresh_token",
    "auth_token", "authorization", "session_id", "cookie",
    "secret_key", "private_key",
})
_CMD_TRUNCATE_LEN = 4000  # 超过则截断到 200+…+50

# 值里出现的 key=value 形式（命令、文件内容、错误消息）
_INPLACE_SECRET = re.compile(
    r"(password|passwd|pwd|token|api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|secret[_-]?key|client[_-]?secret)\s*[:=]\s*([^\s,;\"'`]*)",
    re.IGNORECASE,
)


def _redact_str_value(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    kl = key.lower()
    if kl in _PASSWORD_FIELDS or kl in _TOKEN_FIELDS:
        return "***REDACTED***"
    return value


def _redact_str_content(s: str) -> str:
    """在字符串内部匹配 password=... token=... 模式。"""
    return _INPLACE_SECRET.sub(r"\1=***REDACTED***", s)


def redact(obj: Any) -> Any:
    """递归脱敏 dict / list / str。

    不可变类型（int/float/bool/None）原样返回。
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return _redact_str_content(obj)
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in (_PASSWORD_FIELDS | _TOKEN_FIELDS):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact(x) for x in obj)
    return obj


def redact_command(cmd: Any) -> Any:
    """专门给 shell 命令用的：超长截断 + 内部 secret 替换。"""
    if not isinstance(cmd, str):
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        else:
            return cmd
    if len(cmd) > _CMD_TRUNCATE_LEN:
        cmd = cmd[:200] + f"...[truncated {len(cmd)-250} chars]..." + cmd[-50:]
    return _redact_str_content(cmd)
