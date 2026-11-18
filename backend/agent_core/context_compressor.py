"""语义上下文压缩器

上下文接近上限时触发语义压缩：
当对话上下文接近 token 上限时，调用 LLM 生成摘要，
用摘要替换历史消息，保持语义完整性。
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from llm.base import LLMClient, Message
from utils.logger import get_logger

logger = get_logger("agent_core.context_compressor")


class ContextCompressor:
    """
    语义上下文压缩器。

    触发时机：estimate_tokens(context) >= max_tokens * compression_threshold
    压缩方式：保留 system prompt + 最近 N 条消息 + LLM 生成的摘要
    """

    def __init__(
        self,
        llm: LLMClient,
        max_tokens: int = 8000,
        compression_threshold: float = 0.85,
        retain_recent: int = 6,
    ) -> None:
        self.llm = llm
        self.max_tokens = max_tokens
        self.compression_threshold = compression_threshold
        self.retain_recent = retain_recent

    def should_compress(self, messages: List[Message]) -> bool:
        """判断是否需要压缩"""
        total = self._estimate_tokens(messages)
        threshold = int(self.max_tokens * self.compression_threshold)
        return total >= threshold

    def compress(
        self,
        messages: List[Message],
        memory_rewriter: Optional[Callable[[], Optional[str]]] = None,
    ) -> List[Message]:
        """
        压缩消息列表。

        策略：
        1. 保留 system prompt（始终保留）
        2. 保留最近 N 条消息（保留最新上下文）
        3. 将中间的历史消息压缩为一段摘要
        4. 记忆注入块换成精简版（memory_rewriter 提供），而不是原样占着窗口
        """
        # 分离 system 与非 system 消息。pinned 消息同样必须保留：
        # 记忆注入块承担「历史参考」作用，压缩时要与 system prompt 同等对待。
        system_msgs = [m for m in messages if m.role == "system" or m.pinned]
        non_system = [m for m in messages if not (m.role == "system" or m.pinned)]

        if len(non_system) <= self.retain_recent:
            # 没有可压缩的历史，但如果记忆块还在，仍然给它一次瘦身机会
            return self._slim_memory(system_msgs, memory_rewriter) + non_system

        # 保留最近消息
        recent = non_system[-self.retain_recent:]
        to_summarize = non_system[:-self.retain_recent:]

        # 生成摘要
        summary_text = self._summarize(to_summarize)

        # 构建压缩后的消息列表
        result: List[Message] = []
        result.extend(self._slim_memory(system_msgs, memory_rewriter))
        result.append(Message(
            role="system",
            content=f"[对话历史摘要]\n以下是对之前对话的摘要，供参考：\n{summary_text}",
        ))
        result.extend(recent)

        new_total = self._estimate_tokens(result)
        logger.info(
            f"Context compressed: {len(messages)} msgs → {len(result)} msgs "
            f"(tokens: {self._estimate_tokens(messages)} → {new_total})"
        )
        return result

    @staticmethod
    def _slim_memory(
        system_msgs: List[Message],
        memory_rewriter: Optional[Callable[[], Optional[str]]],
    ) -> List[Message]:
        """把记忆注入块换成精简版，其余 system 消息原样返回。

        记忆块是 pinned 的，压缩不会丢它；但它带完整方案正文，长任务里会持续
        占用窗口。这里只保留「编号 + 一句话结论」，模型仍能按编号引用，人也
        能凭编号回管理页核对原文。
        """
        out = list(system_msgs)
        if memory_rewriter is None:
            return out
        for i, m in enumerate(out):
            if m.role == "system" and m.name == "memory":
                try:
                    slim = memory_rewriter()
                except Exception as exc:  # 记忆层任何异常都不影响压缩
                    logger.debug(f"memory rewriter failed: {exc}")
                    slim = ""
                if slim and len(slim) < len(m.content or ""):
                    out[i] = Message(
                        role="system",
                        content=slim,
                        name="memory",
                        pinned=True,
                    )
                break
        return out

    def _summarize(self, messages: List[Message]) -> str:
        """调用 LLM 生成摘要"""
        history_lines = []
        for m in messages:
            role_tag = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(m.role, m.role)
            content = m.content[:300] if m.content else ""
            if m.tool_calls:
                tc_names = ", ".join(tc.name for tc in m.tool_calls)
                content = f"[调用工具: {tc_names}] {content}"
            history_lines.append(f"[{role_tag}]: {content}")

        history_text = "\n".join(history_lines[-20:])  # 最多 20 条

        prompt = (
            f"请用简洁的摘要（200字以内）概括以下对话历史，"
            f"保留关键决策、已执行的工具调用及其结果、用户的关键需求。\n\n"
            f"{history_text}"
        )

        try:
            resp = self.llm.chat([Message(role="user", content=prompt)])
            return resp.content.strip()[:500]
        except Exception as e:
            logger.warning(f"LLM summary failed: {e}")
            return f"[{len(messages)} 条历史消息已压缩]"

    def _estimate_tokens(self, messages: List[Message]) -> int:
        """
        粗略估算 token 数。

        规则：中文按字符数/2估算，英文按字符数/4估算。
        """
        total = 0
        for m in messages:
            content = m.content or ""
            # 简单估算：中文 chars / 2 + 英文 chars / 4
            chinese = sum(1 for c in content if "一" <= c <= "鿿")
            english = len(content) - chinese
            total += chinese // 2 + english // 4

            if m.tool_calls:
                for tc in m.tool_calls:
                    total += len(tc.name) // 4
                    for v in tc.arguments.values():
                        total += len(str(v)) // 4
        return total
