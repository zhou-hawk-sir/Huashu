"""运行时命令采集

启动时自动执行：
- compgen -c        列出所有可用命令（内置+外部）
- man -f / whatis   获取命令简介
- type -t           区分 builtin / external

把本机独有命令追加到 store。
"""
from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional, Tuple

from utils.config import get_setting
from utils.logger import get_logger
from .store import CmdEntry, CmdStore, get_default_store

logger = get_logger("cmd_kb.collector")


class RuntimeCollector:
    """运行时采集器。"""

    def __init__(self, store: Optional[CmdStore] = None) -> None:
        self.store = store or get_default_store()
        self.timeout = int(get_setting("cmd_kb.collect_timeout", 30))

    async def collect_async(self) -> int:
        """异步采集。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.collect)

    def collect(self) -> int:
        """同步采集入口。返回新增命令数。"""
        existing = set(self.store.all_names())
        names = self._collect_command_names()
        if not names:
            logger.warning("runtime collect: no commands found (not a Linux env?)")
            return 0

        new_entries: List[CmdEntry] = []
        for name in names:
            if name in existing:
                continue
            entry = CmdEntry(
                name=name,
                category="运行时采集",
                desc="",
                risk="medium",
                type="external",
                source="runtime",
            )
            new_entries.append(entry)

        if not new_entries:
            logger.info("runtime collect: no new commands")
            return 0

        added = self.store.upsert_many(new_entries, persist=True)
        logger.info(f"runtime collect: added {added} new commands")
        return added

    def _collect_command_names(self) -> List[str]:
        """执行 compgen -c 收集所有可用命令名。"""
        names: set[str] = set()
        # 方式 1: compgen（bash 内置）
        try:
            r = self._run_sync(["bash", "-c", "compgen -c"])
            for line in r.splitlines():
                line = line.strip()
                if line and self._is_valid_cmd_name(line):
                    names.add(line)
        except Exception as e:
            logger.debug(f"compgen failed: {e}")

        # 方式 2: ls /usr/bin /bin /sbin /usr/sbin
        for d in ("/usr/bin", "/bin", "/sbin", "/usr/sbin", "/usr/local/bin"):
            try:
                r = self._run_sync(["ls", d])
                for line in r.splitlines():
                    name = line.strip()
                    if name and self._is_valid_cmd_name(name):
                        names.add(name)
            except Exception:
                continue

        # 方式 3: which 命令查找 PATH 内命令
        try:
            r = self._run_sync(["bash", "-c", "compgen -c | xargs -I{} which {} 2>/dev/null | sort -u"])
            for line in r.splitlines():
                line = line.strip()
                if line and "/" in line:
                    name = line.split("/")[-1]
                    if self._is_valid_cmd_name(name):
                        names.add(name)
        except Exception:
            pass

        return sorted(names)

    def _run_sync(self, cmd: List[str]) -> str:
        import subprocess
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
            return r.stdout
        except Exception as e:
            logger.debug(f"run {cmd[0]} failed: {e}")
            return ""

    @staticmethod
    def _is_valid_cmd_name(name: str) -> bool:
        return bool(re.match(r"^[A-Za-z0-9_.+-]+$", name)) and len(name) <= 64

    def collect_with_descriptions(self, limit: int = 200) -> int:
        """对未知命令尝试用 whatis/man 获取描述（限速避免卡死）。"""
        candidates = [
            e.name for e in self.store.all()
            if not e.desc and e.source == "runtime"
        ][:limit]

        n = 0
        for name in candidates:
            desc = self._fetch_description(name)
            if desc:
                entry = self.store.get(name)
                if entry:
                    entry.desc = desc
                    self.store.upsert(entry, persist=True)
                    n += 1
        return n

    def _fetch_description(self, name: str) -> str:
        """尝试多种方式获取描述。"""
        # whatis
        r = self._run_sync(["whatis", name])
        if r and "nothing appropriate" not in r.lower():
            first = r.splitlines()[0].strip()
            # 去掉名字前缀
            parts = first.split(" - ", 1)
            return parts[1] if len(parts) == 2 else first
        return ""

    async def collect_full(self) -> Tuple[int, int]:
        """完整流程：先采集命令名，再尝试补描述。"""
        n1 = await self.collect_async()
        # 描述补全放后台，避免阻塞启动
        n2 = self.collect_with_descriptions(limit=200)
        return n1, n2