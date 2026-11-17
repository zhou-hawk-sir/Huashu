"""国际化（中英文消息）"""
from __future__ import annotations

_MESSAGES = {
    "zh": {
        "tool.deny.high_risk": "高危操作已被安全护栏拒绝：{reason}",
        "tool.confirm.required": "此操作属于中危级别，需要您确认后才能执行",
        "tool.not_found": "未找到工具：{name}",
        "permission.denied_root": "出于安全考虑，禁止以 root 身份直接执行命令",
        "injection.detected": "检测到疑似提示词注入，已拦截该输入",
        "tool.timeout": "工具执行超时",
        "tool.error": "工具执行失败：{error}",
        "confirm.prompt": "工具 {tool} 准备执行，是否允许？",
        "session.created": "会话已创建：{id}",
        "session.not_found": "会话不存在：{id}",
    },
    "en": {
        "tool.deny.high_risk": "High-risk operation denied by safety guard: {reason}",
        "tool.confirm.required": "Medium-risk operation requires your confirmation",
        "tool.not_found": "Tool not found: {name}",
        "permission.denied_root": "Direct root execution is not allowed for safety",
        "injection.detected": "Suspected prompt injection detected and blocked",
        "tool.timeout": "Tool execution timed out",
        "tool.error": "Tool execution failed: {error}",
        "confirm.prompt": "Tool {tool} is ready to run. Allow?",
        "session.created": "Session created: {id}",
        "session.not_found": "Session not found: {id}",
    },
}


def t(key: str, lang: str = "zh", **kwargs) -> str:
    """翻译 key；缺失时回退到英文，再缺失时返回 key 本身。"""
    msg = _MESSAGES.get(lang, {}).get(key)
    if msg is None:
        msg = _MESSAGES.get("en", {}).get(key, key)
    try:
        return msg.format(**kwargs)
    except (KeyError, IndexError):
        return msg