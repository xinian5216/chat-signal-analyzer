#!/usr/bin/env python
"""Issue #17 分数压缩诊断 CLI（纯离线、确定性、0 Jev / 0 网络）。

用法：

    # 合成 benchmark 三层诊断（默认输入即 v0.4 relationship benchmark）
    python scripts/run_score_diagnostics.py

    # 附带既存、已冻结的真实 Jev raw 输出（gitignored，只读研究）
    python scripts/run_score_diagnostics.py \
        --real-raw evaluation/reports/v32_contrast_raw.json \
                   evaluation/reports/v31_run2_main34_raw.json

产物（默认写到 gitignored 的 evaluation/reports/issue17/，绝不提交）：

    report.json / report.md        机器可读 + 人工可读报告
    paired_deltas.csv              成对 delta 链
    family_distributions.csv       各关系模式分布汇总
    noul_transform_curve.csv       Noul transform 曲线
    dilution_stress.csv            weighted-mean dilution
    weight_sensitivity.csv         message_weight 敏感性
    real_raw_pairs.csv             （提供 --real-raw 时）真实成对 raw delta

输出不含时间戳：相同输入重复运行得到逐字节相同的结果。
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

import score_diagnostics as sd  # noqa: E402

DEFAULT_CASES = ROOT / "evaluation" / "cases_relationship_v0.4.json"
DEFAULT_CONVERSATIONS = (ROOT / "evaluation" / "fixtures"
                         / "relationship_conversations_v0.4_synthetic.json")
DEFAULT_PAIRS = ROOT / "evaluation" / "relationship_pairs_v0.4.json"
DEFAULT_OUT = ROOT / "evaluation" / "reports" / "issue17"


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
        description="Issue #17 score-compression diagnostics (offline, deterministic)")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--conversations", default=str(DEFAULT_CONVERSATIONS))
    parser.add_argument("--pairs", default=str(DEFAULT_PAIRS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--real-raw", nargs="*", default=[],
                        metavar="RAW_JSON",
                        help="既存真实 Jev raw 输出（只读；缺文件显式失败）")
    args = parser.parse_args(argv)

    try:
        real_raws = [sd.load_real_raw(p) for p in args.real_raw]
        report = sd.build_report(args.cases, args.conversations, args.pairs,
                                 real_raws)
    except sd.DiagnosticError as exc:
        print(f"diagnostic error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(sd.render_markdown(report),
                                      encoding="utf-8")

    # 成对 delta 链
    pair_rows = []
    for d in report["pairs"]:
        pair_rows.append([
            d["pair_id"], d["family"], d["baseline_case"], d["contrast_case"],
            _num(d["raw_score_delta"]["warmth"]),
            _num(d["raw_score_delta"]["engagement"]),
            _num(d["raw_score_delta"]["special_attention"]),
            _num(d["raw_score_delta"]["relationship_evidence_strength"]),
            _num(d["raw_score_delta"]["relational_ease"]),
            _num(d["raw_noul_delta"]["romantic_signal"]),
            _num(d["raw_noul_delta"]["distancing_signal"]),
            _num(d["transformed_noul_delta"]["romantic_ev"]),
            _num(d["transformed_noul_delta"]["distancing_ev"]),
            _num(d["weight_delta"]), _num(d["base_score_delta"]),
            _num(d["overall_delta"]), _num(d["recent_delta"]),
            _num(d["total_weight_delta"]),
            _num(d["delta_trace"]["raw_score_abs_max_100"]),
            _num(d["delta_trace"]["base_score_abs_100"]),
            _num(d["delta_trace"]["overall_abs_100"]),
        ])
    _write_csv(out_dir / "paired_deltas.csv", [
        "pair_id", "family", "baseline", "contrast",
        "raw_warmth_delta", "raw_engagement_delta", "raw_special_delta",
        "raw_evidence_delta", "raw_ease_delta",
        "raw_romantic_delta", "raw_distancing_delta",
        "romantic_ev_delta", "distancing_ev_delta",
        "weight_delta", "base_score_delta", "overall_delta", "recent_delta",
        "total_weight_delta",
        "trace_raw_100", "trace_base_100", "trace_overall_100",
    ], pair_rows)

    # 分布汇总
    dist_rows = []
    for family, entry in report["distributions"]["by_family"].items():
        for metric, s in entry["metrics"].items():
            dist_rows.append([family, metric, s["count"], _num(s["min"]),
                              _num(s["median"]), _num(s["max"]),
                              _num(s["mean"]), _num(s["range"])])
    _write_csv(out_dir / "family_distributions.csv", [
        "family", "metric", "count", "min", "median", "max", "mean", "range",
    ], dist_rows)

    # Noul transform 曲线
    curve = report["stress"]["noul_transform_curve"]
    _write_csv(out_dir / "noul_transform_curve.csv", ["p", "evidence"],
               [[_num(row["p"]), _num(row["evidence"])] for row in curve["curve"]])

    # dilution / weight sensitivity
    dilution = report["stress"]["weighted_mean_dilution"]
    _write_csv(out_dir / "dilution_stress.csv", [
        "filler_kind", "n_fillers", "salient_only_overall", "mixed_overall",
        "overall_delta", "mixed_recent", "recent_delta", "total_weight",
        "effective_messages", "filler_base_score_x100",
    ], [[row["filler_kind"], row["n_fillers"], _num(row["salient_only_overall"]),
         _num(row["mixed_overall"]), _num(row["overall_delta"]),
         _num(row["mixed_recent"]), _num(row["recent_delta"]),
         _num(row["total_weight"]), row["effective_messages"],
         _num(row["filler_base_score_x100"])]
        for row in dilution["rows"]])

    weight = report["stress"]["message_weight_sensitivity"]
    _write_csv(out_dir / "weight_sensitivity.csv", [
        "relationship_evidence_strength", "relation_confidence",
        "evidence_norm", "message_weight", "base_score_x100",
    ], [[_num(row["relationship_evidence_strength"]),
         _num(row["relation_confidence"]), _num(row["evidence_norm"]),
         _num(row["message_weight"]), _num(row["base_score_x100"])]
        for row in weight["rows"]])

    # 真实 raw 成对 delta（如提供）
    real = report["real_raw"]
    if real.get("available"):
        rows = []
        for row in real["frozen_pairs"]:
            if not row.get("available"):
                rows.append([row["pair_id"], row["issue_bullet"], "0",
                             "", "", "", "", "", "", ""])
                continue
            rows.append([
                row["pair_id"], row["issue_bullet"], "1",
                _num(row["raw_score_delta"]["warmth"]),
                _num(row["raw_score_delta"]["engagement"]),
                _num(row["raw_score_delta"]["special_attention"]),
                _num(row["raw_score_delta"]["relationship_evidence_strength"]),
                _num(row["weight_delta"]),
                _num(row["base_score_delta_100"]),
                _num(row["delta_trace"]["raw_score_abs_max_100"]),
            ])
        _write_csv(out_dir / "real_raw_pairs.csv", [
            "pair_id", "issue_bullet", "available", "raw_warmth_delta",
            "raw_engagement_delta", "raw_special_delta", "raw_evidence_delta",
            "weight_delta", "base_score_delta_100", "trace_raw_100",
        ], rows)

    cases = report["cases"]
    pairs = report["pairs"]
    print(f"diagnostics written to: {out_dir}")
    print(f"cases: {len(cases)}; pairs: {len(pairs)}")
    if real.get("available"):
        print(f"real raw: yes ({len(real.get('files', []))} file(s))")
    else:
        print("real raw: not provided (no-live-API limitation)")
    for d in pairs:
        trace = d["delta_trace"]
        print(f"  {d['pair_id']}: raw {_num(trace['raw_score_abs_max_100'])} -> "
              f"base {_num(trace['base_score_abs_100'])} -> "
              f"overall {_num(trace['overall_abs_100'])} (0~100)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
