"""把 tools.yaml 与运行时 all_tools() 对齐（A1 修复）

背景：`config/tools.yaml` 与 `tools/__init__.py::all_tools()` 曾是两套 13 个工具，
名单只有前 10 个重合。后果是 `shell.execute` 在配置里没有声明，
策略引擎读不到它的风险等级，风险判定完全依赖命令正则单点。

做法：以 `all_tools()` 为唯一事实源，重新生成 tools.yaml。
工具改了以后跑一次即可，不要手改 yaml。

运行：
    cd backend && python scripts/sync_tools_yaml.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from tools import all_tools  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "config" / "tools.yaml"

HEADER = """# MCP 工具清单（Claude 风格 Schema）
#
# 本文件由 scripts/sync_tools_yaml.py 从 tools/__init__.py::all_tools() 生成，
# 请勿手改。工具增删后重新生成：
#     cd backend && python scripts/sync_tools_yaml.py
#
# 注意：shell.execute / shell.execute_elevated 的 risk_level 声明为 low，
# 实际风险由 safety/risk_classifier 按命令内容动态分级（高危命令直接拒绝）。
"""

# 需要额外说明风险来源的工具
RISK_NOTES: Dict[str, str] = {
    "shell.execute": "按命令内容动态分级：高危拒绝、中危二次确认、低危放行",
    "shell.execute_elevated": "提权入口，受 permissions.yaml 与 risk_classifier 双重约束",
    "web_search": "出网访问，默认中危需确认",
}


def build() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tool in all_tools():
        item = tool.to_manifest()
        note = RISK_NOTES.get(item.get("name", ""))
        if note:
            item["risk_note"] = note
        out.append(item)
    return out


def main() -> int:
    tools = build()
    text = HEADER + "\ntools:\n" + yaml.safe_dump(
        {"tools": tools}, allow_unicode=True, sort_keys=False
    ).split("tools:\n", 1)[1]
    if "--check" in sys.argv:
        current = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
        same = [t["name"] for t in yaml.safe_load(current).get("tools", [])] == [
            t["name"] for t in tools
        ]
        print(f"tools.yaml 与 all_tools() 名称一致：{same}（{len(tools)} 个）")
        return 0 if same else 1

    CONFIG.write_text(text, encoding="utf-8")
    print(f"已写入 {CONFIG}，共 {len(tools)} 个工具：")
    for t in tools:
        print(f"  - {t['name']}  risk={t['risk_level']}  cap={t.get('required_capability', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
