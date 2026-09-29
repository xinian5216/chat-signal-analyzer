"""Issue #20 互动结构诊断与验收工具（离线、确定性、0 Jev / 0 网络 / 0 生产影响）。

职责：
1. 把 `evaluation/interaction_scenarios_v0.4.json` 的 message spec 物化为
   与生产同形的（messages, results）；
2. 对每个场景跑**端到端**链路（interaction_dynamics → salience →
   relationship_profile）并做冻结期望约束检查；
3. **prefix invariance**：对每个场景比较“完整运行中早于第 n 条已知的事件”
   与“只运行前 n 条”的事件集合（未来泄漏检测，§10/§11/§47）；
4. gap 候选阈值比较（2h / 6h / 12h / 24h）与 capability matrix 汇总。

输出写 gitignored 的 `evaluation/reports/issue20/`（report.json / report.md /
scenarios.csv / gap_thresholds.csv），无时间戳、重复运行逐字节一致。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import salience as sal
import relationship_profile as rp
import interaction_dynamics as idyn

ROOT = Path(__file__).resolve().parent
DEFAULT_SCENARIOS = ROOT / "evaluation" / "interaction_scenarios_v0.4.json"

GAP_CANDIDATES_MINUTES = (120, 360, 720, 1440)   # 2h / 6h / 12h / 24h


class DiagnosticError(ValueError):
    """场景 / 输入不合法时显式失败（绝不静默出结论）。"""


# ---------------------------------------------------------------------------
# 物化（确定性）
# ---------------------------------------------------------------------------


def _block(score: float, conf: float) -> dict:
    s = max(0.0, min(4.0, float(score)))
    lo, hi = math.floor(s), math.ceil(s)
    out = {str(i): 0.0 for i in range(5)}
    if lo == hi:
        out[str(lo)] = 1.0
    else:
        frac = round(s - lo, 6)
        out[str(lo)] = round(1.0 - frac, 6)
        out[str(hi)] = frac
    return {"score": float(score), "probabilities": out, "confidence": conf}


def _structured_result(spec: dict) -> dict:
    conf = float(spec.get("conf", 0.75))
    intent = spec.get("intent", "continue_topic")
    emotion = spec.get("emotion", "calm")
    return {
        "emotion": {"choice": emotion,
                    "probabilities": spec.get("emotion_probs") or {emotion: 1.0},
                    "confidence": conf},
        "intent": {"choice": intent,
                   "probabilities": spec.get("intent_probs") or {intent: 1.0},
                   "confidence": conf},
        "warmth": _block(spec.get("warmth", 2.0), conf),
        "engagement": _block(spec.get("engagement", 2.0), conf),
        "special_attention": _block(spec.get("special", 1.5), conf),
        "relationship_evidence_strength": _block(spec.get("evidence", 2.0), conf),
        "relational_ease": _block(spec.get("ease", 2.0), conf),
        "romantic_signal": float(spec.get("romantic", 0.1)),
        "distancing_signal": float(spec.get("distancing", 0.05)),
        "model": "synthetic-issue20-scenario",
    }


def materialize_case(case: dict) -> tuple[list[dict], list[dict]]:
    """case → (messages, results)。TA 文本消息得到同形 structured result。"""
    messages: list[dict] = []
    results: list[dict] = []
    for i, spec in enumerate(case.get("messages") or []):
        content_type = spec.get("content_type", "text")
        messages.append({
            "speaker": spec.get("speaker", "unknown"),
            "text": spec.get("text", ""),
            "time": spec.get("time"),
            "raw_speaker": "我" if spec.get("speaker") == "me"
            else ("TA" if spec.get("speaker") == "them" else "unknown"),
            "content_type": content_type,
            "media_kinds": list(spec.get("media_kinds") or []),
        })
        if spec.get("speaker") == "them" and content_type != "media":
            results.append({
                "index": i,
                "speaker": "them",
                "text": spec.get("text", ""),
                "time": spec.get("time"),
                "context": [],
                "result": _structured_result(spec.get("result") or {}),
                "cached": False,
            })
    return messages, results


def load_scenarios(path: str | Path = DEFAULT_SCENARIOS) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("version") != "interaction-scenarios-v0.4":
        raise DiagnosticError(f"未知场景集版本：{doc.get('version')}")
    if not doc.get("cases"):
        raise DiagnosticError("场景集为空")
    return doc


# ---------------------------------------------------------------------------
# 端到端 + 期望检查
# ---------------------------------------------------------------------------


def run_pipeline(messages: list[dict],
                 results: list[dict]) -> tuple[dict, dict, dict]:
    interaction = idyn.build_interaction_events(messages, results)
    salience_out = sal.build_salience(results, interaction=interaction)
    profile = rp.build_profile(results, salience=salience_out,
                               interaction=interaction)
    return interaction, salience_out, profile


def _event_signature(event: dict) -> tuple:
    return (event["event_type"], event.get("actor"),
            event["window"]["start_index"], event["window"]["end_index"],
            event["identity"])


def prefix_invariance(messages: list[dict],
                      results: list[dict]) -> dict:
    """未来泄漏检查：完整运行中“在第 n 条前已知”的事件必须与前 n 条运行一致。"""
    full, _, _ = run_pipeline(messages, results)
    checkpoints = sorted({max(1, len(messages) // 2), max(1, len(messages) - 1)})
    failures = []
    comparisons = []
    for n in checkpoints:
        if n >= len(messages):
            continue
        prefix_messages = messages[:n]
        prefix_results = [e for e in results if e["index"] < n]
        pre, _, _ = run_pipeline(prefix_messages, prefix_results)
        known = {_event_signature(e) for e in full["events"]
                 if e["window"]["end_index"] < n}
        actual = {_event_signature(e) for e in pre["events"]}
        ok = known == actual
        comparisons.append({"n": n, "known": len(known),
                            "prefix": len(actual), "passed": ok})
        if not ok:
            failures.append({"n": n, "missing": sorted(map(str, known - actual)),
                             "extra": sorted(map(str, actual - known))})
    return {"checkpoints": comparisons, "failures": failures,
            "passed": not failures}


def evaluate_case(case: dict) -> dict:
    messages, results = materialize_case(case)
    interaction, salience_out, profile = run_pipeline(messages, results)
    expectation = case.get("expectation") or {}
    events = interaction["events"]
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    for event_type, req in (expectation.get("event_types") or {}).items():
        pool = [e for e in events if e["event_type"] == event_type]
        ok = req.get("min", 0) <= len(pool) <= req.get("max", 10 ** 6)
        for field in ("dimension", "direction", "review_status", "actor"):
            if field in req:
                ok = ok and all(e.get(field) == req[field] for e in pool)
        check(f"event:{event_type}", ok,
              f"count={len(pool)}（期望 {req.get('min', 0)}~{req.get('max', 10 ** 6)}）"
              + (f" {[(k, v) for k, v in req.items() if k not in ('min', 'max')]}"
                 if any(k not in ('min', 'max') for k in req) else ""))

    for event_type in expectation.get("forbidden_event_types") or []:
        pool = [e for e in events if e["event_type"] == event_type]
        check(f"forbidden:{event_type}", not pool, f"count={len(pool)}（期望 0）")

    for dim_key, req in (expectation.get("mapping") or {}).items():
        dim = profile["dimensions"][dim_key]
        salient = len(dim["salient_events"])
        counter = len(dim["counter_events"])
        ok = True
        detail = f"salient={salient} counter={counter}"
        if "salient_min" in req:
            ok = ok and salient >= req["salient_min"]
        if "salient_max" in req:
            ok = ok and salient <= req["salient_max"]
        if "counter_min" in req:
            ok = ok and counter >= req["counter_min"]
        if "counter_max" in req:
            ok = ok and counter <= req["counter_max"]
        check(f"mapping:{dim_key}", ok,
              detail + f"（期望 salient {req.get('salient_min', 0)}~"
              f"{req.get('salient_max', 10 ** 6)}）")

    if "boundary" in expectation:
        req = expectation["boundary"]
        dim = profile["dimensions"]["boundary_pressure"]
        pressure = sum(1 for e in events if e["event_type"] == "boundary_pressure")
        counter = sum(1 for e in events
                      if e.get("dimension") == "boundary_pressure"
                      and e.get("direction") == idyn.DIRECTION_COUNTER
                      and e.get("review_status") == idyn.REVIEW_AUTO)
        ok = ("status" not in req or dim["status"] == req["status"]) \
            and ("pressure_min" not in req or pressure >= req["pressure_min"]) \
            and ("pressure_max" not in req or pressure <= req["pressure_max"]) \
            and ("counter_min" not in req or counter >= req["counter_min"]) \
            and ("counter_max" not in req or counter <= req["counter_max"])
        check("boundary", ok,
              f"status={dim['status']} pressure={pressure} counter={counter}"
              f"（期望 {req}）")

    if "party_structure" in expectation:
        pool = [e for e in events if e["event_type"] == "invitation_progression"]
        got = pool[0].get("party_structure") if pool else None
        check("party_structure", got == expectation["party_structure"],
              f"party_structure={got}（期望 {expectation['party_structure']}）")

    observations = interaction.get("observations") or {}
    for key, req in (expectation.get("observations") or {}).items():
        if key == "reciprocity":
            rec = observations.get("reciprocity") or {}
            ok = True
            if "sufficient" in req:
                ok = ok and rec.get("sufficient") == req["sufficient"]
            counts = rec.get("counts") or {}
            for field, bound in req.items():
                if field == "sufficient":
                    continue
                base = field[:-4] if field.endswith(("_min", "_max")) else field
                if not base.endswith("_turns"):
                    base += "_turns"
                if field.endswith("_min") and counts.get(base, 0) < bound:
                    ok = False
                if field.endswith("_max") and counts.get(base, 0) > bound:
                    ok = False
            check("observations:reciprocity", ok, f"{rec.get('counts')}")
            continue
        value = observations.get(key)
        ok = ("min" not in req or (value or 0) >= req["min"]) and \
             ("max" not in req or (value or 0) <= req["max"])
        check(f"observations:{key}", ok, f"{key}={value}（期望 {req}）")

    # 全局：互动事件永不映射 romantic / special_attention 维度
    bad_dims = {e.get("dimension") for e in events}
    check("no_romantic_interaction_mapping",
          not ({"romance", "romantic"} & bad_dims),
          f"dimensions={sorted(bad_dims)}")
    check("no_special_interaction_mapping", "special_attention" not in bad_dims,
          f"dimensions={sorted(bad_dims)}")

    # prefix invariance（未来泄漏）
    invariance = prefix_invariance(messages, results)
    check("prefix_invariance", invariance["passed"],
          f"checkpoints={invariance['checkpoints']}")

    # 身份稳定性
    identity_req = expectation.get("identity_check") or {}
    if identity_req.get("duplicate_import"):
        again = idyn.build_interaction_events(messages, results)
        ids_first = [e["identity"] for e in events]
        ids_again = [e["identity"] for e in again["events"]]
        check("identity:duplicate_import", ids_first == ids_again,
              f"{len(ids_first)} vs {len(ids_again)} 条事件")
    if identity_req.get("prepend"):
        n = int(identity_req["prepend"])
        base = messages[n:]
        kept = [e for e in results if e["index"] >= n]
        # 切片后 results 必须重新编号（与前缀消息的新空间对齐）
        base_results = [{**e, "index": e["index"] - n} for e in kept]
        base_interaction = idyn.build_interaction_events(base, base_results)
        want = identity_req.get("event_type")
        if want is None:
            want = next(iter((expectation.get("event_types") or {}).keys()), None)
        ids_full = sorted(e["identity"] for e in events
                          if want is None or e["event_type"] == want)
        ids_base = sorted(e["identity"] for e in base_interaction["events"]
                          if want is None or e["event_type"] == want)
        check("identity:prepend", ids_full == ids_base and bool(ids_full),
              f"{want}: identities 一致={ids_full == ids_base} "
              f"({len(ids_full)} vs {len(ids_base)})")

    return {
        "id": case["id"],
        "scenario": case.get("scenario"),
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "event_count": len(events),
        "event_types": {t: sum(1 for e in events if e["event_type"] == t)
                        for t in sorted({e["event_type"] for e in events})},
        "diagnostics": interaction.get("diagnostics"),
        "observations": {
            "boundary_opportunities":
                observations.get("boundary_opportunities"),
            "media_text_unknown_turns":
                observations.get("media_text_unknown_turns"),
        },
    }


# ---------------------------------------------------------------------------
# gap 候选阈值比较（§14：operational rule，不是科学阈值）
# ---------------------------------------------------------------------------


def compare_gap_thresholds(cases: list[dict]) -> dict:
    """2h / 6h / 12h / 24h 阈值在各场景上的重启事件数比较。"""
    rows = []
    for case in cases:
        messages, results = materialize_case(case)
        row = {"id": case["id"], "scenario": case.get("scenario")}
        for minutes in GAP_CANDIDATES_MINUTES:
            events = idyn._detect_reengagement(messages, gap_minutes=minutes)
            row[f"{minutes // 60}h"] = len(events)
        rows.append(row)
    summary = {
        f"{minutes // 60}h": {
            "total_reengagements": sum(r[f"{minutes // 60}h"] for r in rows),
            "scenarios_with_event": sum(1 for r in rows if r[f"{minutes // 60}h"]),
        }
        for minutes in GAP_CANDIDATES_MINUTES
    }
    return {"candidates_minutes": list(GAP_CANDIDATES_MINUTES),
            "production_minutes": idyn.REENGAGEMENT_GAP_MINUTES,
            "rows": rows, "summary": summary}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def build_report(scenarios_path: str | Path = DEFAULT_SCENARIOS) -> dict:
    doc = load_scenarios(scenarios_path)
    case_reports = [evaluate_case(case) for case in doc["cases"]]
    gap = compare_gap_thresholds(doc["cases"])
    return {
        "version": "interaction-diagnostics-v1",
        "scenario_meta": doc["meta"],
        "scenarios": case_reports,
        "scenarios_passed": sum(1 for r in case_reports if r["passed"]),
        "scenarios_total": len(case_reports),
        "gap_thresholds": gap,
        "capability_matrix": idyn.CAPABILITIES,
    }


def render_markdown(report: dict) -> str:
    lines = ["# Issue #20 Interaction Dynamics 诊断报告（interaction-diagnostics-v1）", ""]
    lines += [
        "> 全部数据离线、确定性生成；合成场景只证明“若消息与 Jev 信号如此，",
        "> 本地互动结构引擎会发生什么”，不构成真实 Jev 实证。",
        "",
        f"- 场景约束检查：{report['scenarios_passed']} / "
        f"{report['scenarios_total']} 通过",
        "",
    ]
    lines += ["## 1. I1~I24 场景检查", ""]
    for r in report["scenarios"]:
        mark = "✓" if r["passed"] else "✗"
        lines.append(f"- {mark} **{r['id']}**：事件 {r['event_count']} 条"
                     f"（{r['event_types'] or '无'}）")
        for c in r["checks"]:
            if not c["passed"]:
                lines.append(f"    - ✗ {c['name']}：{c['detail']}")
    lines.append("")
    lines += ["## 2. Prefix invariance（未来泄漏）", ""]
    for r in report["scenarios"]:
        for c in r["checks"]:
            if c["name"] == "prefix_invariance" and c["passed"]:
                lines.append(f"- {r['id']}：{c['detail']}")
                break
    lines.append("")
    lines += ["## 3. Re-engagement gap 候选阈值（operational rule）", "",
              "| 场景 | 2h | 6h | 12h | 24h |", "|---|---|---|---|---|"]
    for row in report["gap_thresholds"]["rows"]:
        lines.append(f"| {row['id']} | {row['2h']} | {row['6h']} | "
                     f"{row['12h']} | {row['24h']} |")
    lines.append("")
    lines.append(f"生产门槛：{report['gap_thresholds']['production_minutes']} 分钟"
                 "（复用 behavior.INITIATIVE_GAP_MINUTES 集中锚点）。")
    lines.append("")
    lines += ["## 4. Capability matrix", "",
              "| Capability | Auto | Review required | Deferred | Time required |",
              "|---|---|---|---|---|"]
    for key, value in report["capability_matrix"].items():
        lines.append(f"| {key} | {value['auto']} | {value['review']} | "
                     f"{value['deferred']} | {value['time_required']} |")
    lines.append("")
    return "\n".join(lines)
