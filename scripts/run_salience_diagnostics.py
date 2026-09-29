#!/usr/bin/env python
"""Issue #19 salience 诊断 CLI（纯离线、确定性、0 Jev / 0 网络）。

用法：

    python scripts/run_salience_diagnostics.py

    # 指定场景集 / 输出目录
    python scripts/run_salience_diagnostics.py \
        --scenarios evaluation/salience_scenarios_v0.4.json \
        --out-dir evaluation/reports/issue19

产物（默认写到 gitignored 的 evaluation/reports/issue19/，绝不提交）：

    report.json / report.md        机器可读 + 人工可读报告
    dilution_matrix.csv            强事件 + N 条普通消息（legacy vs baseline vs 事件）
    retention_ratios.csv           各候选算法的事件保留率
    candidate_matrix.csv           候选算法八维比较矩阵
    scenarios.csv                  S1~S15 约束检查

输出不含时间戳：相同输入重复运行得到逐字节相同的结果。
退出码：0 = 全部场景约束通过；2 = 场景约束失败或输入不合法。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import salience_diagnostics as sdiag  # noqa: E402

DEFAULT_SCENARIOS = ROOT / "evaluation" / "salience_scenarios_v0.4.json"
DEFAULT_OUT = ROOT / "evaluation" / "reports" / "issue19"


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _num(value) -> str:
    return "" if value is None else f"{value:.6g}"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Issue #19 salience diagnostics (offline, deterministic)")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    try:
        report = sdiag.build_report(args.scenarios)
    except sdiag.DiagnosticError as exc:
        print(f"diagnostic error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(sdiag.render_markdown(report),
                                       encoding="utf-8")

    dil = report["dilution"]
    _write_csv(out_dir / "dilution_matrix.csv", [
        "n_fillers", "legacy_overall", "legacy_recent", "baseline_special",
        "event_retained", "salient_count", "event_class", "salient_tier",
    ], [[row["n_fillers"], _num(row["legacy_overall"]), _num(row["legacy_recent"]),
          _num(row["baseline_special"]), int(row["event_retained"]),
          row["salient_count"], row["event_class"] or "", row["salient_tier"]]
         for row in dil["rows"]])

    _write_csv(out_dir / "retention_ratios.csv", [
        "n_fillers", "weighted_mean", "top_k", "percentile", "baseline_boost",
        "capped_cumulative", "event_channel",
    ], [[row["n_fillers"], row["weighted_mean"], row["top_k"], row["percentile"],
          row["baseline_boost"], row["capped_cumulative"], row["event_channel"]]
         for row in dil["retention"]])

    _write_csv(out_dir / "candidate_matrix.csv", [
        "method", "retention", "outlier_safety", "counter_preservation",
        "direction_safety", "interpretability", "false_positive_control",
    ], [[row["method"], row["retention"], row["outlier_safety"],
          row["counter_preservation"], row["direction_safety"],
          row["interpretability"], row["false_positive_control"]]
         for row in report["comparison"]])

    _write_csv(out_dir / "scenarios.csv", [
        "id", "scenario", "passed", "event_count", "event_classes",
    ], [[r["id"], r.get("scenario") or "", int(r["passed"]), r["event_count"],
          json.dumps(r["event_classes"], ensure_ascii=False, sort_keys=True)]
         for r in report["scenarios"]])

    print(f"salience diagnostics written to: {out_dir}")
    print(f"scenarios: {report['scenarios_passed']} / {report['scenarios_total']} passed")
    for row in dil["rows"]:
        print(f"  N={row['n_fillers']:>3}: legacy overall "
              f"{_num(row['legacy_overall'])}, baseline {_num(row['baseline_special'])}, "
              f"event retained={row['event_retained']} ({row['salient_tier']})")
    if report["scenarios_passed"] != report["scenarios_total"]:
        for r in report["scenarios"]:
            if not r["passed"]:
                print(f"  FAILED: {r['id']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
