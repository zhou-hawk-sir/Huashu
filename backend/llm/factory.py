"""LLM 客户端工厂

根据配置自动选择 DeepSeek / Qwen3 / Mock 实现，支持多 provider 降级。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.config import get_setting, get_config
from utils.logger import get_logger
from .base import LLMClient, LLMResponse, Message
from .mock import MockClient
from .openai_compat import OpenAICompatClient

logger = get_logger("llm.factory")

# 预置 provider 配置
_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "api_key_env": "QWEN_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "mock": {
        "model": "mock",
    },
}


# ---------------------------------------------------------------------------
# 熔断器状态
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """熔断器：某 provider 连续失败 N 次后暂时跳过"""
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    failures: int = 0
    last_failure_ts: float = 0.0
    is_open: bool = False

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_ts = time.time()
        if self.failures >= self.failure_threshold:
            self.is_open = True

    def record_success(self) -> None:
        self.failures = 0
        self.is_open = False

    def is_available(self) -> bool:
        if not self.is_open:
            return True
        # 冷却期后尝试恢复
        if time.time() - self.last_failure_ts >= self.cooldown_seconds:
            self.is_open = False
            self.failures = 0
            return True
        return False


# ---------------------------------------------------------------------------
# 降级 LLM 客户端
# ---------------------------------------------------------------------------

class FallbackLLMClient(LLMClient):
    """多 provider 降级客户端（带熔断器）"""

    def __init__(
        self,
        providers: List[Dict[str, Any]],
        circuit_breaker: Optional[CircuitBreaker] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.providers = providers  # List[{"name": str, "client": LLMClient}]
        self.cb = circuit_breaker or CircuitBreaker()
        self._current_idx = 0

    def _get_next_available(self) -> Optional[LLMClient]:
        """获取下一个可用的 provider（跳过熔断中的）"""
        for i in range(len(self.providers)):
            idx = (self._current_idx + i) % len(self.providers)
            p = self.providers[idx]
            if self._is_available(p["name"]):
                return p["client"], p["name"], idx
        return None, None, -1

    def _is_available(self, name: str) -> bool:
        provider_cb = getattr(self, f"_cb_{name}", None)
        if provider_cb is None:
            return True
        return provider_cb.is_available()

    def _get_cb(self, name: str) -> CircuitBreaker:
        attr = f"_cb_{name}"
        cb = getattr(self, attr, None)
        if cb is None:
            cb = CircuitBreaker()
            setattr(self, attr, cb)
        return cb

    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """尝试 providers，按顺序降级"""
        errors: List[str] = []
        start_idx = self._current_idx

        for i in range(len(self.providers)):
            client, name, idx = self._get_next_available()
            if client is None:
                break

            cb = self._get_cb(name)
            if not cb.is_available():
                # 跳过熔断中的 provider
                self._current_idx = (self._current_idx + 1) % len(self.providers)
                continue

            try:
                resp = client.chat(messages, tools=tools, **kwargs)
                # 成功：重置熔断器
                cb.record_success()
                logger.debug(f"LLM call succeeded via {name}")
                return resp
            except Exception as e:
                err_msg = f"{name}: {e}"
                errors.append(err_msg)
                cb.record_failure()
                logger.warning(f"LLM provider {name} failed: {e}")
                self._current_idx = (self._current_idx + 1) % len(self.providers)
                continue

        # 所有 provider 都失败
        logger.error(f"All LLM providers failed: {'; '.join(errors)}")
        raise RuntimeError(f"All LLM providers failed: {'; '.join(errors[-3:])}")

    async def stream_chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ):
        """流式调用（第一个成功 provider）"""
        for i in range(len(self.providers)):
            client, name, idx = self._get_next_available()
            if client is None:
                break

            cb = self._get_cb(name)
            if not cb.is_available():
                self._current_idx = (self._current_idx + 1) % len(self.providers)
                continue

            try:
                async for chunk in client.stream_chat(messages, tools=tools, **kwargs):
                    yield chunk
                cb.record_success()
                return
            except Exception as e:
                cb.record_failure()
                logger.warning(f"stream_chat provider {name} failed: {e}")
                self._current_idx = (self._current_idx + 1) % len(self.providers)
                continue

        raise RuntimeError("All LLM providers failed in stream mode")


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def create_llm_client(provider: Optional[str] = None) -> LLMClient:
    """根据 settings.yaml 创建 LLM 客户端。

    支持单 provider 和多 provider 降级模式。
    降级顺序由 fallback_order 配置决定，默认：deepseek → qwen → openai
    """
    cfg = get_config()
    llm_cfg = get_setting("llm", {})
    name = (provider or llm_cfg.get("provider", "mock")).lower()

    if name == "mock":
        logger.info("Using MockClient (offline demo)")
        return MockClient(model="mock")

    # 多 provider 降级模式
    fallback_order = llm_cfg.get("fallback_order", ["deepseek", "qwen", "openai"])

    providers: List[Dict[str, Any]] = []
    for pname in fallback_order:
        preset = _PRESETS.get(pname)
        if preset is None:
            continue

        base_url = llm_cfg.get("fallback_base_urls", {}).get(pname) or preset["base_url"]
        model = llm_cfg.get("fallback_models", {}).get(pname) or preset["model"]
        api_key_env = preset["api_key_env"]
        api_key = cfg.get("llm_api_key") or os.environ.get(api_key_env, "")
        if not api_key:
            logger.debug(f"Skipping provider {pname}: no API key")
            continue

        try:
            client = OpenAICompatClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout=llm_cfg.get("timeout", 60),
                temperature=llm_cfg.get("temperature", 0.2),
                max_tokens=llm_cfg.get("max_tokens", 4096),
            )
            providers.append({"name": pname, "client": client})
            logger.info(f"Added LLM provider: {pname} ({model})")
        except Exception as e:
            logger.warning(f"Failed to init provider {pname}: {e}")

    if not providers:
        configured = (provider or llm_cfg.get("provider", "mock"))
        needed_env = (_PRESETS.get(str(configured).lower(), {}) or {}).get(
            "api_key_env", "DEEPSEEK_API_KEY"
        )
        logger.warning(
            "=" * 60 + "\n"
            f"⚠️  已配置 LLM provider='{configured}'，但未找到任何可用 API Key，"
            f"将回退到 MockClient（仅离线演示，Agent 不会真正调用大模型）。\n"
            f"    请设置环境变量 {needed_env}（部署见 deploy.sh 的 EnvironmentFile）。\n"
            + "=" * 60
        )
        return MockClient(model="mock")

    if len(providers) == 1:
        logger.info(f"Single LLM provider: {providers[0]['name']}")
        return providers[0]["client"]

    logger.info(
        f"Using FallbackLLMClient with {len(providers)} providers: "
        f"{[p['name'] for p in providers]}"
    )
    return FallbackLLMClient(providers=providers)