#!/usr/bin/env python
"""SignalLens 离线评估 CLI。

用法（全部确定性、离线优先）：

    python scripts/evaluate.py --fixtures                 # fixture 模式（默认）
    python scripts/evaluate.py --fixtures --report evaluation/reports/base.json
    python scripts/evaluate.py --compare baseline.json candidate.json

真实模式（人工专用；**测试 / CI 永远不会触发**）：

    python scripts/evaluate.py --real --yes-run-live-api

    --real 必须同时满足两个条件才会运行：
      1. 显式传入 ``--yes-run-live-api``；
      2. 环境变量 TYPESAFE_API_KEY 已设置。
    真实模式会调用 Jev 分析 benchmark 案例并保存匿名结果（虚构案例），
    用于生成 / 对比 v2.2 之后的 baseline 与 candidate。

benchmark 通过率是人工定义案例上的 regression / evaluation 指标，
不是“科学准确率”。详见 evaluation/README.md。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluation as ev  # noqa: E402


def _run_fixtures(report_path: str | None) -> int:
    cases = ev.load_cases()
    results = ev.load_fixture_results()
    aggregate = ev.evaluate_cases(cases, results)
    print(ev.format_report(aggregate))
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"report written: {path}")
    return 0 if aggregate["passed_cases"] == aggregate["total_cases"] else 1


def _compare(baseline_path: str, candidate_path: str) -> int:
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    diff = ev.compare_reports(baseline, candidate)
    print(f"baseline pass rate : {diff['baseline_pass_rate']:.1%}")
    print(f"candidate pass rate: {diff['candidate_pass_rate']:.1%}")
    if diff["resolved_failures"]:
        print("resolved failures (improvement):")
        for item in diff["resolved_failures"]:
            print(f"  - {item}")
    if diff["introduced_failures"]:
        print("introduced failures (regression):")
        for item in diff["introduced_failures"]:
            print(f"  + {item}")
    print("regression:" , "YES" if diff["regression"] else "none")
    return 1 if diff["regression"] else 0


def _run_real(confirmed: bool, report_path: str | None) -> int:
    """真实模式：人工显式确认 + API key 才允许调用 Jev。"""
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        # 防御纵深：测试进程内（含 CI）永远不允许真实模式触网
        print("refusing to run --real under pytest "
              "(benchmark real mode is human-only)", file=sys.stderr)
        return 2
    if os.environ.get("CI"):
        # GitHub Actions 等处 CI=true：即使有人显式传了确认与 key 也拒绝
        print("refusing to run --real in a CI environment "
              "(benchmark real mode is human-only)", file=sys.stderr)
        return 2
    if not confirmed:
        print("refusing to run --real without explicit --yes-run-live-api",
              file=sys.stderr)
        return 2
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("refusing to run --real without TYPESAFE_API_KEY", file=sys.stderr)
        return 2
    print("real mode: this will call the live TypeSafe Jev API for every "
          "benchmark case", file=sys.stderr)

    from analyzer import DEFAULT_MODEL, SCHEMA_VERSION, analyze_messages, \
        extract_answers  # noqa: F401 - extract_answers 供下游校验形状
    from privacy import mask_messages
    from typesafe_sdk import TypeSafeClient

    client = TypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"],
                            model=DEFAULT_MODEL)
    cases = ev.load_cases()
    results: dict[str, dict] = {}
    for case in cases:
        messages = mask_messages(
            ev._case_history(case))  # 复用评估侧的解析（案例格式固定）
        entries = analyze_messages(client, messages)
        entry = next(
            (e for e in entries if e["text"] == case["target"]), None)
        if entry is None:
            results[case["id"]] = {"error": "target message was not analyzed"}
            continue
        results[case["id"]] = {
            **entry["result"],
            "conversation_context": entry["context"],
        }
    aggregate = ev.evaluate_cases(cases, results)
    print(ev.format_report(aggregate))
    print(f"schema version: {SCHEMA_VERSION}")
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"cases": cases, "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"raw results written: {path}")
    return 0 if aggregate["passed_cases"] == aggregate["total_cases"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SignalLens benchmark evaluator")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fixtures", action="store_true",
                      help="使用 evaluation/fixtures 下的合成结果（默认）")
    mode.add_argument("--real", action="store_true",
                      help="真实调用 Jev（人工专用，需额外确认与 API key）")
    parser.add_argument("--yes-run-live-api", action="store_true",
                        help="显式确认允许真实模式调用 Jev")
    parser.add_argument("--report", metavar="PATH",
                        help="把评估汇总写入 JSON 文件")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"),
                        help="比较两份评估报告（constraint 维度）")
    args = parser.parse_args(argv)

    if args.compare:
        return _compare(args.compare[0], args.compare[1])
    if args.real:
        return _run_real(args.yes_run_live_api, args.report)
    return _run_fixtures(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
