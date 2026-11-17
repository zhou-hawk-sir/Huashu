"""记忆检索链路自检脚本

用途：不启动服务、不连数据库文件，直接在内存库上跑一遍
「写入 → 建索引 → 召回 → 四因子打分 → 降权 → 排序」，
输出每条候选的得分构成，用于开发期确认链路正常。

运行：
    cd backend && python scripts/demo_memory.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.fingerprint import match  # noqa: E402
from memory.schema import connect  # noqa: E402
from memory.scorer import BM25Index, Scorer, normalize_sim  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from memory.types import Candidate, EnvFingerprint, MemoryRecord  # noqa: E402

CUR = EnvFingerprint(
    os_id="kylin", os_version="v11", os_sp="sp3",
    kernel="4.19.90-25.10.v2101", arch="x86_64",
    deps={"nginx": "1.24", "python3": "3.11"},
    captured_at="2026-11-17T03:00:00+00:00",
)


def seed(store: MemoryStore) -> None:
    rows = [
        (
            MemoryRecord(
                id="", kind="episodic",
                title="重启 ollama 服务恢复端口",
                symptom="ollama 起不来，11434 端口无响应",
                diagnosis="服务进程异常退出",
                solution="systemctl restart ollama",
            ),
            EnvFingerprint("kylin", "v11", "sp2", "4.19.90-24.6", "x86_64",
                           {"nginx": "1.20", "python3": "3.9"}, "2026-05-01T00:00:00+00:00", "pass"),
            6, 0,
        ),
        (
            MemoryRecord(
                id="", kind="episodic",
                title="定位端口占用进程后重启",
                symptom="ollama 起不来，11434 端口被旧进程占用",
                diagnosis="旧进程未退出，端口未释放",
                solution="ss -lntp 查占用进程，确认后 kill，再 systemctl restart ollama",
            ),
            EnvFingerprint("kylin", "v11", "sp3", "4.19.90-25.10", "x86_64",
                           {"nginx": "1.24", "python3": "3.11"}, "2026-11-10T00:00:00+00:00", "pass"),
            5, 0,
        ),
        (
            MemoryRecord(
                id="", kind="semantic",
                title="11434 是 ollama 默认端口",
                symptom="查询端口归属",
                diagnosis="",
                solution="11434 为 ollama 默认监听端口",
            ),
            EnvFingerprint("kylin", "v11", "sp3", "4.19.90-25.10", "x86_64",
                           {"nginx": "1.24", "python3": "3.11"}, "2026-11-12T00:00:00+00:00", "pass"),
            2, 0,
        ),
        (
            MemoryRecord(
                id="", kind="episodic",
                title="清理日志释放磁盘",
                symptom="磁盘空间不足，/var 写满",
                diagnosis="/var/log 下日志未轮转",
                solution="journalctl --vacuum-size=200M",
            ),
            EnvFingerprint("kylin", "v11", "sp3", "4.19.90-25.10", "x86_64",
                           {"nginx": "1.24", "python3": "3.11"}, "2026-11-05T00:00:00+00:00", "pass"),
            1, 0,
        ),
    ]
    for rec, fp, succ, fail in rows:
        rec.success_count = succ
        rec.fail_count = fail
        rec.last_verified_at = fp.captured_at
        mid = store.add(rec)
        store.put_env(mid, fp)


def main() -> None:
    store = MemoryStore(conn=connect(":memory:"))
    seed(store)

    index = BM25Index()
    envs = store.all_envs()
    for rec in store.list(status="active"):
        index.add(rec.id, f"{rec.title} {rec.symptom} {rec.solution}")
    index.build()

    query = "ollama 起不来 11434 端口"
    top = index.max_score(query)
    hits = index.search(query, k=5)

    scorer = Scorer()
    results = []
    for mid, raw in hits:
        rec = store.get(mid)
        fp = envs.get(mid) or CUR
        levels = match(fp, CUR)
        cand = scorer.apply(
            Candidate(
                memory={
                    "id": mid,
                    "title": rec.title,
                    "verify_rate": rec.verify_rate,
                    "status": rec.status,
                    "last_verified_at": rec.last_verified_at,
                }
            ),
            sim=normalize_sim(raw, top),
            levels=levels,
            has_dep_requirement=bool(fp.deps),
        )
        cand.memory["levels"] = levels
        results.append(cand)

    results.sort(key=lambda c: -c.score)

    print(f"查询：{query}")
    print(f"当前环境：{CUR.key()}")
    print(f"召回 {len(results)} 条\n")
    for i, c in enumerate(results, 1):
        print(f"{i}. {c.memory['title']}")
        print(f"   {scorer.explain(c)}")
        print(f"   环境比对：{c.memory['levels']}\n")


if __name__ == "__main__":
    main()
