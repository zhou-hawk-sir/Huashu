"""Mock LLM 客户端

用于离线演示与单元测试。基于关键词规则选择工具调用。
- 看到"进程/process/ps" → 调 process.list
- 看到"磁盘/df/空间" → 调 disk.usage
- 看到"日志/journal" → 调 log.journal
- 看到"清理/删除" → 调 disk.cleanup (中危)
- 看到"重启服务" → 调 service.restart (中危)
- 看到"kill" → 调 process.kill

第二次见到 tool 角色消息时产出最终文本。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator, Dict, List, Optional

from .base import LLMChunk, LLMClient, LLMResponse, Message, ToolCall


class MockClient(LLMClient):
    """基于规则的 mock LLM。"""

    RULES = [
        (re.compile(r"(进程|process|\bps\b)", re.I), "process.list", {}),
        (re.compile(r"(网络|netstat|连接|connect)", re.I), "network.list", {}),
        (re.compile(r"(磁盘|df|du|空间|space)", re.I), "disk.usage", {}),
        (
            re.compile(r"(大文件|find.*large|大.*文件)", re.I),
            "disk.find_large",
            {"min_size_mb": 500},
        ),
        (re.compile(r"(日志|journal|log)", re.I), "log.journal", {"lines": 50}),
        (
            re.compile(r"(重启|restart).*?([\w-]+)", re.I),
            "service.restart",
            None,
        ),
        (
            re.compile(r"(清理|cleanup).*?([/\w]+)", re.I),
            "disk.cleanup",
            None,
        ),
        (re.compile(r"(kill|杀).*?(\d+)", re.I), "process.kill", None),
    ]

    def __init__(self, model: str = "mock", **kwargs: Any) -> None:
        super().__init__(model=model, **kwargs)
        self._call_counter = 0

    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self._call_counter += 1

        # 检查是否有 tool 角色消息 → 视为"已看到工具结果"，产出最终回复
        has_tool_result = any(m.role == "tool" for m in messages)
        if has_tool_result:
            tool_msg = next(m for m in reversed(messages) if m.role == "tool")
            return LLMResponse(
                content=f"[Mock] 收到您的运维请求，已完成操作。工具返回：{tool_msg.content[:300]}",
                finish_reason="stop",
                usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            )

        # 取最后一条 user 消息
        user_msg = next((m for m in reversed(messages) if m.role == "user"), None)
        text = user_msg.content if user_msg else ""

        # 匹配工具
        tool_name, tool_args = self._match(text)

        if tool_name:
            tool_call = ToolCall(
                id=f"call_mock_{self._call_counter}",
                name=tool_name,
                arguments=tool_args or {},
            )
            return LLMResponse(
                content="",
                tool_calls=[tool_call],
                finish_reason="tool_calls",
                usage={
                    "prompt_tokens": len(text),
                    "completion_tokens": 10,
                    "total_tokens": len(text) + 10,
                },
            )

        # 无工具调用 + 无工具结果 → 文本
        return LLMResponse(
            content=f"[Mock] 收到您的请求：{text}。当前为离线演示模式，已配置 mock LLM，未执行任何工具调用。",
            finish_reason="stop",
            usage={
                "prompt_tokens": len(text),
                "completion_tokens": 30,
                "total_tokens": len(text) + 30,
            },
        )

    async def stream_chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        resp = self.chat(messages, tools, **kwargs)
        if resp.has_tool_calls:
            for tc in resp.tool_calls:
                yield LLMChunk(delta_tool_calls=[tc], finish_reason="tool_calls")
        else:
            content = resp.content
            chunk_size = 8
            for i in range(0, len(content), chunk_size):
                yield LLMChunk(delta_content=content[i:i + chunk_size])
                await asyncio.sleep(0.01)
            yield LLMChunk(finish_reason="stop")

    def _match(self, text: str):
        for pattern, name, default_args in self.RULES:
            m = pattern.search(text)
            if not m:
                continue
            if default_args is not None:
                return name, default_args
            groups = m.groups()
            if name == "service.restart" and len(groups) >= 2:
                return name, {"name": groups[1]}
            if name == "disk.cleanup" and len(groups) >= 2:
                return name, {"path": groups[1]}
            if name == "process.kill" and len(groups) >= 2:
                return name, {"pid": int(groups[1])}
            return name, {}
        return None, None