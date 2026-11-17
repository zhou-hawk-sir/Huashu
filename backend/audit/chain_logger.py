"""思维链日志（JSONL 格式）"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.config import get_setting
from utils.logger import get_logger
from utils.redactor import redact
from .tracer import TraceStep

logger = get_logger("audit.chain")


class ChainLogger:
    """将 TraceStep 写入按日期分割的 JSONL 文件。"""

    def __init__(self, log_dir: Optional[str] = None) -> None:
        if log_dir is None:
            log_dir = get_setting("audit.chain_log_dir", "./logs/chain")
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _today_path(self) -> Path:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.log_dir / f"{today}.jsonl"

    def _scrub(self, step: TraceStep) -> TraceStep:
        """H2: 双保险——即便上游忘了脱敏，落地前再过一遍 redact。"""
        try:
            return replace(
                step,
                input=redact(step.input) if step.input else step.input,
                output=redact(step.output) if step.output else step.output,
            )
        except Exception:
            return step

    async def write(self, step: TraceStep) -> None:
        async with self._lock:
            path = self._today_path()
            with path.open("a", encoding="utf-8") as f:
                f.write(self._scrub(step).to_jsonl() + "\n")
                f.flush()

    def sync_write(self, step: TraceStep) -> None:
        path = self._today_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(self._scrub(step).to_jsonl() + "\n")
            f.flush()