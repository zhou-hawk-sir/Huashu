"""环境自检（T02）：在虚拟机里第一件事就跑它

七项检查，全部只读或自清理，不改配置、不写业务数据：
    1  Python 版本 ≥ 3.11
    2  运行依赖可导入（不联网安装，只验证已装好）
    3  应用工厂可构建（create_app 不抛异常）
    4  /api/health 返回 200（用 TestClient，不占端口）
    5  bwrap 是否安装
    6  bwrap 是否可执行（容器/WSL 里常被 user namespace 卡住）
    7  记忆层闭环：建表 → 写入 → 检索 → 删除 → 残留为 0

用法：
    cd backend && python scripts/preflight.py
    cd backend && python scripts/preflight.py --verbose
退出码：0 全通过 / 1 有硬性项失败
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_rows: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    _rows.append((name, status, detail))
    icon = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[status]
    print(f"{icon} {name}" + (f" — {detail}" if detail else ""))


def check_python() -> str:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 11):
        record("Python 版本", OK, f"{ver} ({platform.machine()})")
        return OK
    record("Python 版本", FAIL, f"{ver}，要求 ≥ 3.11")
    return FAIL


def check_deps() -> str:
    missing = []
    for mod in ("fastapi", "uvicorn", "pydantic", "yaml", "openai", "httpx", "dotenv"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        record("运行依赖", FAIL, f"缺失 {', '.join(missing)}；先 pip install -r requirements.txt")
        return FAIL
    record("运行依赖", OK, "fastapi/uvicorn/pydantic/yaml/openai/httpx/dotenv 均已安装")
    return OK


def check_app() -> str:
    try:
        from api.app import create_app

        started = time.perf_counter()
        app = create_app()
        took = (time.perf_counter() - started) * 1000
    except Exception as e:
        record("应用工厂", FAIL, f"create_app 失败：{e}")
        return FAIL
    record("应用工厂", OK, f"{took:.0f} ms，memory={'on' if getattr(app.state, 'memory', None) else 'off'}")
    return OK


def check_health() -> str:
    try:
        from fastapi.testclient import TestClient
        from api.app import create_app

        client = TestClient(create_app())
        r = client.get("/api/health")
        if r.status_code == 200 and r.json().get("status") == "ok":
            record("健康检查", OK, "GET /api/health → 200")
            return OK
        record("健康检查", FAIL, f"状态码 {r.status_code}")
        return FAIL
    except Exception as e:
        record("健康检查", FAIL, f"{e}")
        return FAIL


def check_bwrap_installed() -> str:
    path = shutil.which("bwrap")
    if path:
        record("bwrap 安装", OK, path)
        return OK
    record("bwrap 安装", WARN, "未安装；麒麟执行 apt/dnf install bubblewrap")
    return WARN


def check_bwrap_works() -> str:
    if not shutil.which("bwrap"):
        record("bwrap 可用", WARN, "跳过（未安装）")
        return WARN
    try:
        r = subprocess.run(
            ["bwrap", "--dev-bind", "/", "/", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        record("bwrap 可用", FAIL, f"执行异常：{e}")
        return FAIL
    if r.returncode == 0:
        record("bwrap 可用", OK, "--dev-bind 冒烟通过")
        return OK
    msg = (r.stderr or "").strip().splitlines()
    detail = msg[-1] if msg else f"returncode={r.returncode}"
    record("bwrap 可用", WARN, f"沙箱不可用（容器/WSL 常见，属环境限制）：{detail}")
    return WARN


def check_memory() -> str:
    """记忆层闭环。用临时库，跑完即删，不碰 data/memory.db。"""
    try:
        from memory.schema import connect, table_names
        from memory.service import MemoryConfig, MemoryService
        from memory.writer import WriteRequest

        tmp = Path(tempfile.mkdtemp(prefix="preflight_mem_")) / "memory.db"
        conn = connect(tmp)
        svc = MemoryService(config=MemoryConfig(db_path=str(tmp)), conn=conn, db_path=str(tmp))

        tables = table_names(conn)
        need = {
            "memories",
            "memory_env",
            "memory_versions",
            "memory_conflicts",
            "memory_events",
            "memory_tombstones",
        }
        missing = need - set(tables)
        if missing:
            record("记忆层闭环", FAIL, f"缺表 {', '.join(sorted(missing))}")
            return FAIL

        wr = svc.write(
            WriteRequest(
                kind="episodic",
                title="自检样例：端口被占用导致服务起不来",
                symptom="ollama 起不来，11434 端口被占用",
                solution="ss -lntp 找占用进程后 kill，再重启服务",
            )
        )
        if wr is None or not wr.memory_id:
            record("记忆层闭环", FAIL, "写入失败")
            return FAIL

        started = time.perf_counter()
        res = svc.retrieve("ollama 起不来 11434 端口", session_id="preflight")
        took = (time.perf_counter() - started) * 1000
        if wr.memory_id not in res.ids:
            record("记忆层闭环", FAIL, "写入成功但检索召回失败")
            return FAIL

        guard = svc.guard("service.restart", {"name": "ollama"}, session_id="preflight")
        report = svc.delete(wr.memory_id, scope="all")
        conn.close()
        shutil.rmtree(tmp.parent, ignore_errors=True)

        if not report.get("clean"):
            record("记忆层闭环", FAIL, f"删除后仍有残留：{report.get('residual')}")
            return FAIL

        record(
            "记忆层闭环",
            OK,
            f"6 表就绪，写入→召回 {took:.1f} ms→断言(allow={guard.allowed})→删除无残留",
        )
        return OK
    except Exception as e:
        record("记忆层闭环", FAIL, f"{e}")
        return FAIL


def check_pkg_manager() -> None:
    """国产化环境提示：麒麟用 rpm/dnf，Debian 系用 dpkg/apt。"""
    pm = [n for n in ("dnf", "yum", "apt", "zypper") if shutil.which(n)]
    record("包管理器", OK if pm else WARN, ", ".join(pm) or "未识别")


def main() -> int:
    parser = argparse.ArgumentParser(description="华枢智维 V5.4 环境自检")
    parser.add_argument("--verbose", action="store_true")
    parser.parse_args()

    print("=" * 68)
    print("华枢智维 V5.4 环境自检")
    print(f"系统：{platform.platform()}  架构：{platform.machine()}")
    print("=" * 68)

    results = [
        check_python(),
        check_deps(),
        check_app(),
        check_health(),
        check_bwrap_installed(),
        check_bwrap_works(),
        check_memory(),
    ]
    check_pkg_manager()

    fails = [r for r in results if r == FAIL]
    warns = [r for r in results if r == WARN]
    print("-" * 68)
    print(f"通过 {len(results) - len(fails) - len(warns)} / 硬性失败 {len(fails)} / 告警 {len(warns)}")
    if fails:
        print("结论：存在硬性失败项，先修再跑集成。")
        return 1
    print("结论：可以进入集成验证。bwrap 告警不影响记忆层与安全链单测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
