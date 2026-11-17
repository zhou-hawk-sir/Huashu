"""工具 manifest 构造：从 tools.yaml 或代码动态生成"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# LLM provider（OpenAI / DeepSeek / Anthropic 兼容）要求 tool.function.name
# 仅匹配 ^[a-zA-Z0-9_-]+$。但内部工具名采用 "category.action" 的点分风格
# （disk.usage / process.list 等），更符合运维表达。
# 因此在 → LLM 时把 '.' 替换为 '_'，← LLM 时反向还原。
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def to_provider_name(internal_name: str) -> str:
    """内部工具名 (disk.usage) -> 发送给 LLM 的工具名 (disk_usage)。"""
    return _NAME_RE.sub("_", internal_name)


def from_provider_name(provider_name: str) -> str:
    """LLM 返回的工具名 (disk_usage) -> 内部工具名 (disk.usage)。

    由于我们的内部命名只用 '.' 做分隔符、无其他非法字符，
    'disk_usage' 安全地还原为 'disk.usage'。若还原后的名字不在注册表里，
    调用方会回退到原名查找（双保险）。
    """
    return provider_name.replace("_", ".")


def build_manifest_from_tools(tools: List[Any]) -> List[Dict[str, Any]]:
    """从工具实例列表构造 manifest。"""
    return [t.to_manifest() for t in tools]


def load_tools_from_yaml(path: str | Path) -> List[Dict[str, Any]]:
    """从 YAML 文件加载 manifest 列表。"""
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("tools", [])


def manifest_to_provider_format(manifests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将内部 manifest 转为 OpenAI 兼容的 tools 格式。

    OpenAI 格式：
    {
      "type": "function",
      "function": {
        "name": "...",
        "description": "...",
        "parameters": {...}  # JSON Schema
      }
    }

    同时维护 internal <-> provider 的双向映射，便于 dispatcher 把
    LLM 返回的 provider 风格名字翻回内部名。
    """
    out: List[Dict[str, Any]] = []
    for m in manifests:
        internal = m["name"]
        provider = to_provider_name(internal)
        if provider != internal:
            # 通过 description 注入映射，LLM 看到的是工具文档，
            # 不影响 schema 本身；映射仍由 dispatcher 处理。
            desc = m["description"]
            if "(" not in desc and "internal:" not in desc:
                # 不强行改 description；让 mapping 由 dispatcher 解析
                pass
        out.append({
            "type": "function",
            "function": {
                "name": provider,
                "description": m["description"],
                "parameters": m.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def resolve_tool_name(name: str, registry_provider_names: Optional[List[str]] = None) -> str:
    """把 LLM 返回的名字翻译成内部工具名。

    Args:
        name: LLM 返回的 function.name（已替换 . 为 _）
        registry_provider_names: 注册表中所有 provider 风格名字列表，
            若提供则只在能匹配时做反向替换，否则直接 replace(_→.)

    Returns:
        内部工具名（例如 'disk.usage'）。如果还原后不在注册表里，
        返回原 name（让上层报错 'tool not found'）。
    """
    if not name:
        return name
    # 如果 name 已经包含内部风格（点号），直接返回
    if "." in name:
        return name
    candidate = from_provider_name(name)
    if registry_provider_names is None:
        return candidate
    # 仅当 candidate 也在注册表的 provider 名字里（转回去）时采用
    if candidate.replace(".", "_") in registry_provider_names or candidate in registry_provider_names:
        return candidate
    return name