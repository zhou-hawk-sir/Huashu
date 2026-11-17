"""把 MemBench 指标画成图（开发期用，不进运行时）

读取 `data/bench/membench_metrics.json`，产出：
    data/bench/metrics.png   六项指标分组柱状图
    data/bench/metrics.md    指标表（可直接贴进报告）

matplotlib 只装在开发机；没有就退化为打印 Markdown 表，不报错。

用法：
    cd backend && python scripts/plot_metrics.py
    cd backend && python scripts/plot_metrics.py --input data/bench/membench_metrics.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

METRICS = [
    ("task_success_rate", "任务成功率"),
    ("memory_hit_applicable_rate", "记忆命中适用率"),
    ("high_risk_block_rate", "越权/高危拦截率"),
    ("false_block_rate", "误拦截率"),
    ("conflict_auto_pick_rate", "冲突自动择一率"),
    ("safety_on_memory_fault_rate", "记忆异常时安全通过率"),
]


MODE_LABEL = {
    "in-process": "进程内模拟执行（仿真数据，非真机实测）",
    "bwrap": "bwrap 隔离执行（真机实测）",
}


def mode_text(summary: dict) -> str:
    mode = summary.get("execution_mode", "unknown")
    return MODE_LABEL.get(mode, f"未知执行方式：{mode}")


def to_markdown(summary: dict) -> str:
    groups = summary.get("group_order") or list(summary["metrics"].keys())
    head = "| 指标 | " + " | ".join(f"{g} 组" for g in groups) + " |"
    sep = "|---" * (len(groups) + 1) + "|"
    rows = [head, sep]
    for key, label in METRICS:
        cells = [f"{summary['metrics'][g].get(key, 0):.3f}" for g in groups]
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    rows.append("")
    rows.append(
        f"> 环境：{summary.get('env', {}).get('os_id')} {summary.get('env', {}).get('os_version')}"
        f" / {summary.get('env', {}).get('arch')}；"
        f"每组 {summary.get('repeat')} 次重复，种子 {summary.get('seed')}。"
    )
    rows.append(f"> 数据来源：{mode_text(summary)}。")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/bench/membench_metrics.json")
    ap.add_argument("--out", default="data/bench")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"找不到指标文件：{src}，先跑 scripts/run_bench.py")
        return 1
    summary = json.loads(src.read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = to_markdown(summary)
    (out_dir / "metrics.md").write_text(md + "\n", encoding="utf-8")
    print(md)

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("\n（未安装 matplotlib，跳过出图；表格已写入 metrics.md）")
        return 0

    groups = summary.get("group_order") or list(summary["metrics"].keys())
    labels = [f"{g} 组" for g in groups]
    series = [(key, label) for key, label in METRICS]

    # 中文字体要在建图之前设好，否则标题会掉成方块
    for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei"):
        try:
            plt.rcParams["font.sans-serif"] = [name]
            plt.rcParams["axes.unicode_minus"] = False
            break
        except Exception:
            continue

    fig, axes = plt.subplots(2, 3, figsize=(13, 6.4))
    for ax, (key, label) in zip(axes.flat, series):
        values = [summary["metrics"][g].get(key, 0) for g in groups]
        bars = ax.bar(labels, values, color=["#9aa7bd", "#7aa6ec", "#6a4bd8", "#2f6bd8"])
        ax.set_title(label, fontsize=11)
        ax.set_ylim(0, 1.05)
        ax.grid(axis="y", alpha=0.25, linestyle=":")
        for b, v in zip(bars, values):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}",
                    ha="center", fontsize=9)
    title = "Huashu-MemBench 2×2 对照结果（A 基线 / B 机制 / C 治理 / D 完整）"
    if summary.get("simulated", True):
        title += "\n［仿真数据 · 进程内判定，非真机实测］"
    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.92))

    target = out_dir / "metrics.png"
    fig.savefig(target, dpi=160)
    print(f"\n图已保存：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
