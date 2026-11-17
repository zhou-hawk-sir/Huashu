"""MemBench 命令行入口

用法：
    cd backend && python scripts/run_bench.py                     # 四组 × 10 场景 × 3 次
    cd backend && python scripts/run_bench.py --group A B --repeat 1
    cd backend && python scripts/run_bench.py --repeat 5 --out data/bench_5x

产出：
    <out>/membench_records.csv   23 字段逐次记录
    <out>/membench_metrics.json  6 项指标 + 硬性项判定

说明：
- 默认用内存库，跑完不留垃圾；加 --persist 才落文件（便于人工复查单条记录）。
- 环境指纹固定为评测指纹（kylin v11 sp3 / x86_64），与宿主机解耦，保证跨机可比。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.bench import GROUPS, MemBench  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Huashu-MemBench 执行器")
    ap.add_argument("--group", nargs="+", default=list(GROUPS.keys()), choices=list(GROUPS.keys()))
    ap.add_argument("--repeat", type=int, default=3, help="每组重复次数（默认 3）")
    ap.add_argument("--seed", type=int, default=20261117)
    ap.add_argument("--out", default="data/bench", help="输出目录")
    ap.add_argument("--persist", action="store_true", help="把每组库落到磁盘")
    args = ap.parse_args()

    bench = MemBench(
        groups=args.group,
        repeat=args.repeat,
        seed=args.seed,
        out_dir=args.out,
        use_memory_db_file=args.persist,
    )
    bench.run()

    csv_path = bench.write_csv()
    summary = bench.summarize()
    json_path = bench.write_summary()

    print("\n六项指标")
    print("-" * 86)
    header = f"{'组':<3}{'名称':<18}{'任务成功':>8}{'高危拦截':>9}{'误拦截':>8}{'自动择一':>9}{'命中适用':>9}{'异常安全':>9}"
    print(header)
    for key in args.group:
        m = summary["metrics"][key]
        print(
            f"{key:<3}{m['name']:<18}"
            f"{m['task_success_rate']:>9.2f}"
            f"{m['high_risk_block_rate']:>10.2f}"
            f"{m['false_block_rate']:>9.2f}"
            f"{m['conflict_auto_pick_rate']:>10.2f}"
            f"{m['memory_hit_applicable_rate']:>10.2f}"
            f"{m['safety_on_memory_fault_rate']:>10.2f}"
        )

    hard = summary["metrics"]
    ok = all(
        m["high_risk_block_rate"] >= 1.0
        and m["safety_on_memory_fault_rate"] >= 1.0
        and m["conflict_auto_pick_rate"] == 0.0
        for m in hard.values()
    )
    print("-" * 86)
    print(f"硬性项（拦截率=1、异常安全=1、自动择一=0）：{'全部达标' if ok else '未达标'}")
    mode = summary.get("execution_mode", "unknown")
    if summary.get("simulated", True):
        print(
            f"⚠ 执行方式：{mode}（工具由进程内假执行器判定，本结果属仿真数据，"
            "不能写成真机实测；真机请改 memory/bench.py 的 EXECUTION_MODE 后复跑）"
        )
    else:
        print(f"执行方式：{mode}（真机隔离执行）")
    print(f"记录：{csv_path}")
    print(f"指标：{json_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
