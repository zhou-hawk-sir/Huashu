"""pytest 公共配置：把 backend 加入 sys.path

测试在 backend 目录下运行，模块以 `memory.*`、`utils.*` 方式导入。
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
