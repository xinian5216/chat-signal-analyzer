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
    真实模式对每个案例**只分析指定的 TA target**（使用 only_indices），
    不会对案例内其他 TA 历史消息发起请求——34 个案例 = 34 次请求。
    运行前会打印预计请求数量。

    --report PATH        聚合评估报告（含 meta：schema/模型/评估配置），
                         可直接被 --compare 读取；
    --raw-report PATH    原始模型输出（与聚合报告分开存放）。

benchmark 通过率是人工定义案例上的 regression / evaluation 指标，
不是“科学准确率”。详见 evaluation/README.md。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluation as ev  # noqa: E402


def _run_fixtures(report_path: str | None, cases_path: str | None = None,
                  fixture_path: str | None = None) -> int:
    cases = ev.load_cases(cases_path)
    results = ev.load_fixture_results(fixture_path)
    aggregate = ev.evaluate_cases(cases, results)
    print(ev.format_report(aggregate))
    if report_path:
        _write_json(report_path, aggregate)
        print(f"report written: {report_path}")
    return 0 if aggregate["passed_cases"] == aggregate["total_cases"] else 1


def _case_set_identity(report: dict) -> dict | None:
    """从聚合报告中提取案例集身份；没有 meta 身份信息时返回 None。"""
    meta = report.get("meta")
    if not isinstance(meta, dict):
        return None
    return {
        "sha": meta.get("benchmark_sha256"),
        "count": meta.get("benchmark_cases"),
        "schema": meta.get("schema_version"),
        "file": meta.get("benchmark_cases_file"),
    }


def _compare(baseline_path: str, candidate_path: str) -> int:
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    base_id = _case_set_identity(baseline)
    cand_id = _case_set_identity(candidate)
    if (base_id is None or cand_id is None
            or not base_id.get("sha") or not cand_id.get("sha")):
        print("refusing to compare: 报告缺少 benchmark 案例集身份"
              "（meta.benchmark_sha256 / benchmark_cases）。", file=sys.stderr)
        print("请用带 meta 的新版报告重新生成；历史报告可用 "
              "--from-raw <raw.json> --cases <cases.json> 离线重建。",
              file=sys.stderr)
        return 3
    if (base_id["sha"] != cand_id["sha"]
            or base_id["count"] != cand_id["count"]):
        print("refusing to compare: 两份报告的 benchmark 案例集不一致——"
              "无法定义同一把尺子上的“改善率”。", file=sys.stderr)
        print(f"  baseline : {base_id['file']} "
              f"({base_id['count']} cases, sha256 {base_id['sha'][:16]}…)",
              file=sys.stderr)
        print(f"  candidate: {cand_id['file']} "
              f"({cand_id['count']} cases, sha256 {cand_id['sha'][:16]}…)",
              file=sys.stderr)
        return 3

    base_schema, cand_schema = base_id.get("schema"), cand_id.get("schema")
    if base_schema and cand_schema and base_schema != cand_schema:
        print("!! SCHEMA SEMANTICS CHANGED !!")
        print(f"  baseline schema : {base_schema}")
        print(f"  candidate schema: {cand_schema}")
        print("  两份报告的问题语义已经不同：以下只展示**中性的约束差异**，")
        print("  不得解读为改善或回归；通过率也不是同一把尺子上的比较。")
    elif not (base_schema and cand_schema):
        print("!! SCHEMA UNKNOWN !!")
        print(f"  baseline schema : {base_schema or 'unknown'}")
        print(f"  candidate schema: {cand_schema or 'unknown'}")
        print("  至少一份报告的 schema 版本无法确认：以下只展示中性的约束差异。")

    diff = ev.compare_reports(baseline, candidate)
    print(f"baseline pass rate : {diff['baseline_pass_rate']:.1%}")
    print(f"candidate pass rate: {diff['candidate_pass_rate']:.1%}")
    only_in_base = diff["resolved_failures"]
    only_in_cand = diff["introduced_failures"]
    cross_schema = not (base_schema and cand_schema and
                        base_schema == cand_schema)
    if cross_schema:
        print("constraint differences (neutral, cross-schema):")
        print("  only in baseline:")
        for item in only_in_base:
            print(f"    - {item}")
        print("  only in candidate:")
        for item in only_in_cand:
            print(f"    + {item}")
    else:
        if only_in_base:
            print("resolved failures (improvement):")
            for item in only_in_base:
                print(f"  - {item}")
        if only_in_cand:
            print("introduced failures (regression):")
            for item in only_in_cand:
                print(f"  + {item}")
    if cross_schema:
        print("regression verdict: not applicable（schema 语义不一致，"
              "不做改善/回归判定）")
        return 0
    print("regression:", "YES" if diff["regression"] else "none")
    return 1 if diff["regression"] else 0


_CANON_FIELDS = ("id", "category", "me", "them", "chat", "target")


def _case_identity(case: dict) -> dict:
    """案例的身份指纹（不含预期/说明——允许这些不同）。"""
    return {field: case.get(field) for field in _CANON_FIELDS}


def _check_raw_cases_match(payload_cases, cases, raw_path: str) -> None:
    """确保 raw 输出对应的案例与待评分案例是同一批（同 ID/顺序/聊天/target/身份）。

    只允许预期与说明文字不同；同名但内容不同的案例一律拒绝复用，
    防止给旧输出错误地套新约束。
    """
    if not isinstance(payload_cases, list) or not payload_cases:
        raise ev.EvaluationError(
            f"raw 文件缺少 cases 清单，无法验证案例一致性：{raw_path}")
    if len(payload_cases) != len(cases):
        raise ev.EvaluationError(
            f"raw 文件案例数（{len(payload_cases)}）与待评分案例数"
            f"（{len(cases)}）不一致：{raw_path}")
    for raw_case, case in zip(payload_cases, cases):
        if _case_identity(raw_case) != _case_identity(case):
            raise ev.EvaluationError(
                f"案例不一致（id={case.get('id')!r}）：raw 中的案例与 "
                f"--cases 指定的案例在 id/顺序/聊天/target/身份上不同，"
                f"拒绝复用该输出。")


def _regenerate_from_raw(raw_path: str, cases_path: str | None,
                         report_path: str | None,
                         assume_schema: str | None = None,
                         schema_from_report: str | None = None) -> int:
    """离线从原始结果重建聚合报告（不调用任何 API；用于修复 meta 或迁移旧数据）。

    schema 版本溯源（绝不猜测）：优先级为 raw 文件自带 meta >
    --schema-from-report 指向的报告 meta > --assume-schema 用户显式指定；
    都无法确认时记为 "unknown" 并显式告警。当前代码的 SCHEMA_VERSION
    只能用于**本次**真实运行，不得用来给旧输出重新贴版本。
    """
    payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    raw_results = payload.get("results")
    if not isinstance(raw_results, dict) or not raw_results:
        print(f"raw 文件没有 results：{raw_path}", file=sys.stderr)
        return 2

    schema, schema_source = _resolve_schema_version(
        payload, assume_schema, schema_from_report)
    if schema is None:
        print("warning: 无法确认该 raw 输出对应的 schema 版本，"
              "schema_version 标记为 unknown（不猜测）。"
              "可用 --assume-schema 或 --schema-from-report 显式提供。",
              file=sys.stderr)

    cases = ev.load_cases(cases_path)
    _check_raw_cases_match(payload.get("cases"), cases, raw_path)
    aggregate = ev.evaluate_cases(cases, raw_results)
    model = None
    payload_meta = payload.get("meta")
    if isinstance(payload_meta, dict) and payload_meta.get("model"):
        model = payload_meta["model"]
    if not model:
        model = next(
            (r.get("model") for r in raw_results.values()
             if isinstance(r, dict) and r.get("model")), None)
    if not model:
        from analyzer import DEFAULT_MODEL
        model = DEFAULT_MODEL
    failed = sum(1 for r in raw_results.values()
                 if not isinstance(r, dict) or r.get("error"))
    meta = _report_meta(cases, model=model, mode="real-offline-regen",
                        schema_version=schema,
                        failed=failed,
                        cases_path=cases_path or ev.DEFAULT_CASES_PATH)
    meta["schema_source"] = schema_source
    aggregate["meta"] = meta
    print(ev.format_report(aggregate))
    print(f"cases file: {meta['benchmark_cases_file']}; "
          f"cases sha256: {meta['benchmark_sha256'][:16]}…; "
          f"model: {model}; schema_version: {schema} "
          f"(source: {schema_source})")
    if report_path:
        _write_json(report_path, aggregate)
        print(f"regenerated aggregate report written: {report_path}")
    return 0 if aggregate["passed_cases"] == aggregate["total_cases"] else 1


_SCHEMA_PATTERN = re.compile(r"^chat-signal-v\d+(\.\d+)?$")


def _resolve_schema_version(payload: dict, assume_schema: str | None,
                            schema_from_report: str | None
                            ) -> tuple[str | None, str]:
    """按可信优先级确定 raw 输出的 schema 版本；返回 (schema|None, source)。"""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        candidate = meta.get("schema_version")
        if isinstance(candidate, str) and _SCHEMA_PATTERN.match(candidate):
            return candidate, "raw-meta"
    if schema_from_report:
        try:
            report = json.loads(Path(schema_from_report)
                                .read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ev.EvaluationError(
                f"--schema-from-report 无法读取：{exc}") from exc
        report_meta = report.get("meta")
        candidate = report_meta.get("schema_version") \
            if isinstance(report_meta, dict) else None
        if isinstance(candidate, str) and _SCHEMA_PATTERN.match(candidate):
            return candidate, "report-meta"
        raise ev.EvaluationError(
            "--schema-from-report 的报告没有可识别的 meta.schema_version")
    if assume_schema:
        if not _SCHEMA_PATTERN.match(assume_schema):
            raise ev.EvaluationError(
                f"--assume-schema 版本格式不合法：{assume_schema!r}"
                "（应为 chat-signal-vX[.Y]）")
        return assume_schema, "user-flag"
    return None, "unknown"


def estimate_live_requests(cases: list[dict]) -> dict:
    """运行前的预计请求量：每个案例只分析 1 个 target → N 个案例 = N 次请求。"""
    return {
        "cases": len(cases),
        "targets_per_case": 1,
        "estimated_live_requests": len(cases),
        "cache": "off（评估不用缓存，保证测量确定）",
    }


def resolve_target_index(case: dict, history: list[dict]) -> int:
    """把 case 的 target 文本解析为**原始消息下标**（确定后只认下标）。

    选取规则（确定性）：them 名下的文本消息且文本等于 target；若文本在
    历史中多次出现取第一个下标并向 stderr 提示。返回后，结果选择一律用
    下标比对，不再按文本重配（避免重复文本错配）。
    """
    matches = [
        i for i, m in enumerate(history)
        if m.get("speaker") == "them"
        and m.get("text") == case["target"]
        and m.get("content_type") != "media"
    ]
    if not matches:
        raise ev.EvaluationError(
            f"case {case['id']}: target 未出现在 them 文本消息中")
    if len(matches) > 1:
        print(f"warning: case {case['id']} 的 target 文本在历史中出现 "
              f"{len(matches)} 次，使用第一个下标 {matches[0]}",
              file=sys.stderr)
    return matches[0]


def collect_real_results(cases: list[dict], analyze_fn) -> tuple[dict, list[str]]:
    """逐案例采集真实结果（与评估/落盘解耦）。

    单条请求失败 / 结果缺失只记录该 case，不中断整个基线。
    """
    raw_results: dict[str, dict] = {}
    failures: list[str] = []
    for case in cases:
        case_id = case["id"]
        history = ev._case_history(case)
        try:
            target_index = resolve_target_index(case, history)
        except ev.EvaluationError as exc:
            raw_results[case_id] = {"error": f"case setup failed: {exc}",
                                    "error_kind": "case_setup"}
            failures.append(case_id)
            continue
        try:
            entries = analyze_fn(history, {target_index})
        except Exception as exc:  # 单条失败不中断基线
            raw_results[case_id] = {
                "error": f"request failed: {type(exc).__name__}",
                "error_kind": "api_error"}
            failures.append(case_id)
            continue
        entry = next(
            (e for e in (entries or []) if e.get("index") == target_index),
            None)
        if entry is None:
            raw_results[case_id] = {
                "error": "target entry missing from analysis results",
                "error_kind": "api_error"}
            failures.append(case_id)
            continue
        if entry.get("error"):
            raw_results[case_id] = {"error": entry["error"],
                                    "error_kind": "api_error"}
            failures.append(case_id)
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            raw_results[case_id] = {
                "error": "analysis returned no result payload",
                "error_kind": "api_error"}
            failures.append(case_id)
            continue
        raw_results[case_id] = {
            **result,
            "conversation_context": entry.get("context") or [],
        }
    return raw_results, failures


def run_real_evaluation(
    cases: list[dict],
    analyze_fn,
    *,
    model: str,
    mode: str = "real",
    schema_version: str | None = None,
    cases_path: str | Path | None = None,
) -> tuple[dict, dict, list[str]]:
    """真实模式的编排核心（与网络/门禁解耦，便于完全 mock 测试）。

    参数:
        cases: benchmark 案例列表。
        analyze_fn: (messages, only_indices) -> entries；真实实现内部
            调用 ``analyze_messages(client, messages,
            only_indices={target_index})``。测试可注入 fake。

    行为:
        - 每个案例**只**请求其 target 对应的那一个 them 消息；
        - 结果按原始消息 index 选择，不按文本匹配；
        - 单条请求失败 / 结果缺失只记录该 case，不中断整个基线。

    返回:
        (aggregate, raw_results, failed_case_ids)
    """
    raw_results, failures = collect_real_results(cases, analyze_fn)
    aggregate = ev.evaluate_cases(cases, raw_results)
    aggregate["meta"] = _report_meta(cases, model=model, mode=mode,
                                     schema_version=schema_version,
                                     failed=len(failures),
                                     cases_path=cases_path)
    return aggregate, raw_results, failures


def _report_meta(cases: list[dict], *, model: str, mode: str,
                 schema_version: str | None, failed: int,
                 cases_path: str | Path | None = None) -> dict:
    """报告 meta：记录 schema、模型与**实际使用的**案例文件（便于对比与复现）。"""
    from context_builder import (CONTEXT_MAX_CHARS, CONTEXT_MAX_MESSAGES,
                                 CONTEXT_MAX_TURNS)

    cases_file = Path(cases_path) if cases_path else ev.DEFAULT_CASES_PATH
    return {
        "mode": mode,
        "model": model,
        "schema_version": schema_version,
        "benchmark_cases": len(cases),
        "benchmark_cases_file": str(cases_file).replace("\\", "/"),
        "benchmark_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
        "context_builder": {
            "max_turns": CONTEXT_MAX_TURNS,
            "max_messages": CONTEXT_MAX_MESSAGES,
            "max_chars": CONTEXT_MAX_CHARS,
        },
        "failed_cases": failed,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }


def _gate_real(confirmed: bool) -> str | None:
    """真实模式门禁；返回 None 表示放行，否则给出拒绝原因。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return ("refusing to run --real under pytest "
                "(benchmark real mode is human-only)")
    if os.environ.get("CI"):
        return ("refusing to run --real in a CI environment "
                "(benchmark real mode is human-only)")
    if not confirmed:
        return "refusing to run --real without explicit --yes-run-live-api"
    if not os.environ.get("TYPESAFE_API_KEY"):
        return "refusing to run --real without TYPESAFE_API_KEY"
    return None


def validate_cases_for_real(cases: list[dict]) -> list[str]:
    """批次级前置校验：任一案例无法定位 target 即拒绝整批（零请求）。

    返回问题描述列表（空 = 可以启动）。每条含 case id 与原因。
    """
    problems: list[str] = []
    for case in cases:
        history = ev._case_history(case)
        if not history:
            problems.append(f"{case['id']}: 聊天无法解析出任何消息")
            continue
        try:
            resolve_target_index(case, history)
        except ev.EvaluationError as exc:
            problems.append(f"{case['id']}: {exc}")
    return problems


def _run_real(confirmed: bool, report_path: str | None,
              raw_report_path: str | None = None,
              cases_path: str | None = None) -> int:
    """真实模式：人工显式确认 + API key 才允许调用 Jev。"""
    refusal = _gate_real(confirmed)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    from analyzer import (DEFAULT_MODEL, SCHEMA_VERSION, analyze_messages)
    from privacy import mask_messages

    cases = ev.load_cases(cases_path)

    # 前置校验：在任何 API 请求（甚至 client 构造）之前完成。
    problems = validate_cases_for_real(cases)
    if problems:
        print("refusing to start this batch: 案例设置错误（未发出任何请求）",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("请修正案例数据后重试；案例设置错误计为 case_setup_error，"
              "不计为 api_error。", file=sys.stderr)
        return 4

    from typesafe_sdk import TypeSafeClient

    client = TypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"],
                            model=DEFAULT_MODEL)
    estimate = estimate_live_requests(cases)
    print(f"planned live requests: {estimate['estimated_live_requests']} "
          f"({estimate['cases']} cases x {estimate['targets_per_case']} "
          f"target; {estimate['cache']})", file=sys.stderr)

    def analyze_fn(messages, only_indices):
        masked = mask_messages(messages)
        return analyze_messages(client, masked, only_indices=only_indices)

    # 原始输出**先**落盘：聚合阶段即使崩溃也不丢失已付费的模型响应
    # （历史事故：一次真实基线在第 34/34 次请求完成后因聚合缺键崩溃，
    #  34 次真实响应全部丢失）。
    raw_results, failures = collect_real_results(cases, analyze_fn)
    raw_target = raw_report_path or _default_raw_path()
    _write_json(raw_target, {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "model": DEFAULT_MODEL,
            "benchmark_cases_file": str(cases_path or ev.DEFAULT_CASES_PATH)
            .replace("\\", "/"),
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        },
        "cases": cases,
        "results": raw_results,
    })
    print(f"raw model outputs written: {raw_target}")
    aggregate = ev.evaluate_cases(cases, raw_results)
    aggregate["meta"] = _report_meta(cases, model=DEFAULT_MODEL, mode="real",
                                     schema_version=SCHEMA_VERSION,
                                     failed=len(failures),
                                     cases_path=cases_path
                                     or ev.DEFAULT_CASES_PATH)

    print(ev.format_report(aggregate))
    print(f"schema version: {SCHEMA_VERSION}; "
          f"model: {DEFAULT_MODEL}; cache: off")
    print(f"cases file: {aggregate['meta']['benchmark_cases_file']}; "
          f"cases sha256: {aggregate['meta']['benchmark_sha256'][:16]}…")
    if failures:
        print(f"failed cases (recorded, baseline continued): "
              f"{len(failures)} → {', '.join(failures)}")
    if report_path:
        _write_json(report_path, aggregate)
        print(f"aggregate report written: {report_path} "
              f"(readable by --compare)")
    return 0 if aggregate["passed_cases"] == aggregate["total_cases"] else 1


def _write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")


def _default_raw_path() -> str:
    """未指定 --raw-report 时的默认原始输出位置（gitignored 目录）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return str(ev.EVALUATION_DIR / "reports" / f"raw_{stamp}.json")


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
                        help="聚合评估报告（含 meta；可被 --compare 读取）")
    parser.add_argument("--raw-report", metavar="PATH",
                        help="原始模型输出（单独存放，不参与 compare）")
    parser.add_argument("--cases", metavar="PATH",
                        help="benchmark 案例文件（默认 evaluation/cases.json；"
                             "v3 起可用 evaluation/cases_distancing.json）")
    parser.add_argument("--fixture-file", metavar="PATH",
                        help="fixture 结果文件（默认 v2.2 合成基线）")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"),
                        help="比较两份评估报告（constraint 维度；案例集身份"
                             "不一致或 schema 语义变化会被拒绝/显式警告）")
    parser.add_argument("--from-raw", metavar="PATH",
                        help="离线从原始结果重建聚合报告（不调用 API）")
    parser.add_argument("--assume-schema", metavar="VERSION",
                        help="为 --from-raw 显式指定 raw 输出的 schema 版本"
                             "（chat-signal-vX[.Y]；无法从 raw/配套报告确认时"
                             "才会用到，否则记 unknown）")
    parser.add_argument("--schema-from-report", metavar="PATH",
                        help="从一份已冻结/同批次报告的 meta.schema_version "
                             "推断 raw 的 schema 版本")
    args = parser.parse_args(argv)

    if args.compare:
        return _compare(args.compare[0], args.compare[1])
    if args.from_raw:
        return _regenerate_from_raw(args.from_raw, args.cases, args.report,
                                    args.assume_schema,
                                    args.schema_from_report)
    if args.real:
        return _run_real(args.yes_run_live_api, args.report,
                         args.raw_report, args.cases)
    return _run_fixtures(args.report, args.cases, args.fixture_file)


if __name__ == "__main__":
    raise SystemExit(main())
