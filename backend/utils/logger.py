"""日志工具

统一日志格式：时间 | 级别 | 模块 | 消息
支持控制台输出与可选文件输出。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _setup_root() -> None:
    global _initialized
    if _initialized:
        return

    root = logging.getLogger("opsagent")
    root.setLevel(logging.INFO)
    root.propagate = False

    # 控制台 Handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console)

    _initialized = True


def get_logger(name: str, log_file: Optional[Path] = None) -> logging.Logger:
    """获取 logger，可选挂载文件 handler。"""
    _setup_root()
    logger = logging.getLogger(f"opsagent.{name}")

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(fh)

    return logger