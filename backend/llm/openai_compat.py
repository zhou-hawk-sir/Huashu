"""OpenAI 兼容 LLM 客户端

DeepSeek 与 Qwen3 都使用 OpenAI 兼容协议，因此可以用同一客户端。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI, OpenAI

from .base import LLMChunk, LLMClient, LLMResponse, Message, ToolCall


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容客户端，支持 DeepSeek/Qwen3/任何兼容接口。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, **kwargs)
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self._sync = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._async = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        params = self._build_params(messages, tools, kwargs)
        resp = self._sync.chat.completions.create(**params)

        choice = resp.choices[0]
        tool_calls: List[ToolCall] = []
        for tc in (choice.message.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage={
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
                "total_tokens": resp.usage.total_tokens if resp.usage else 0,
            },
            raw=resp,
        )

    async def stream_chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        params = self._build_params(messages, tools, kwargs)
        params["stream"] = True
        stream = await self._async.chat.completions.create(**params)

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            tool_calls: List[ToolCall] = []
            for dtc in (delta.tool_calls or []):
                args = {}
                if dtc.function and dtc.function.arguments:
                    try:
                        args = json.loads(dtc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                tool_calls.append(
                    ToolCall(
                        id=dtc.id or f"call_{id(dtc)}",
                        name=(dtc.function.name if dtc.function else "") or "",
                        arguments=args,
                    )
                )
            yield LLMChunk(
                delta_content=delta.content or "",
                delta_tool_calls=tool_calls,
                finish_reason=chunk.choices[0].finish_reason,
            )

    def _build_params(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": self.model,
            "messages": self.to_provider_messages(messages),
            "temperature": kwargs.get("temperature", 0.2),
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        if tools:
            params["tools"] = self.to_provider_tools(tools)
            params["tool_choice"] = kwargs.get("tool_choice", "auto")
        return params