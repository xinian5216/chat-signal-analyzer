#!/usr/bin/env python
"""Issue #20 互动结构诊断 CLI（纯离线、确定性、0 Jev / 0 网络）。

用法：

    python scripts/run_interaction_diagnostics.py

    # 指定场景集 / 输出目录
    python scripts/run_interaction_diagnostics.py \
        --scenarios evaluation/interaction_scenarios_v0.4.json \
        --out-dir evaluation/reports/issue20

产物（默认写到 gitignored 的 evaluation/reports/issue20/，绝不提交）：

    report.json / report.md        机器可读 + 人工可读报告
    scenarios.csv                  I1~I24 约束检查
    gap_thresholds.csv             re-engagement gap 候选阈值比较

输出不含时间戳：相同输入重复运行得到逐字节相同的结果。
退出码：0 = 全部场景约束（含 prefix invariance）通过；
2 = 有失败场景或输入不合法。
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

import interaction_diagnostics as idiag  # noqa: E402

DEFAULT_SCENARIOS = ROOT / "evaluation" / "interaction_scenarios_v0.4.json"
DEFAULT_OUT = ROOT / "evaluation" / "reports" / "issue20"


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Issue #20 interaction dynamics diagnostics "
                    "(offline, deterministic)")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    try:
        report = idiag.build_report(args.scenarios)
    except idiag.DiagnosticError as exc:
        print(f"diagnostic error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(idiag.render_markdown(report),
                                       encoding="utf-8")

    _write_csv(out_dir / "scenarios.csv", [
        "id", "scenario", "passed", "event_count", "event_types",
    ], [[r["id"], r.get("scenario") or "", int(r["passed"]), r["event_count"],
          json.dumps(r["event_types"], ensure_ascii=False, sort_keys=True)]
         for r in report["scenarios"]])

    _write_csv(out_dir / "gap_thresholds.csv", [
        "id", "scenario", "2h", "6h", "12h", "24h",
    ], [[row["id"], row.get("scenario") or "", row["2h"], row["6h"],
          row["12h"], row["24h"]]
         for row in report["gap_thresholds"]["rows"]])

    print(f"interaction diagnostics written to: {out_dir}")
    print(f"scenarios: {report['scenarios_passed']} / "
          f"{report['scenarios_total']} passed")
    for r in report["scenarios"]:
        if not r["passed"]:
            failed = [c["name"] for c in r["checks"] if not c["passed"]]
            print(f"  FAILED {r['id']}: {failed}", file=sys.stderr)
    if report["scenarios_passed"] != report["scenarios_total"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
