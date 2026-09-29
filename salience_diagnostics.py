"""Issue #19 salience 诊断与候选算法比较（研究工具；0 生产改动、0 Jev、0 网络）。

职责：
1. 把 `evaluation/salience_scenarios_v0.4.json` 的 message spec 确定性物化为
   与 `extract_answers` 同形的 structured results，并对生产 `salience.py` /
   `relationship_profile.py` 的输出做**约束检查**（不是模型判分）；
2. 离线比较 Issue #19 要求的候选聚合算法（weighted mean / top-k / percentile /
   baseline+boost / capped cumulative / event-class channel），输出
   retention / outlier / repeated / counter / false-positive / D5 / duplication /
   interpretability 八个维度的实测数据；
3. 生成 dilution matrix（强事件 + 0/1/5/10/30/100 条普通消息）。

约束（与 score_diagnostics 相同约定）：完全离线、确定性、输出不含时间戳、
重复运行逐字节一致；**不改变任何生产结果**；候选算法只存在于本研究模块。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import scoring
import salience as sal
import relationship_profile as rp

ROOT = Path(__file__).resolve().parent
DEFAULT_SCENARIOS = ROOT / "evaluation" / "salience_scenarios_v0.4.json"


class DiagnosticError(ValueError):
    """场景 / 输入不合法时显式失败（绝不静默出结论）。"""


# ---------------------------------------------------------------------------
# 场景物化（确定性：score = 数值，probabilities = 两档插值分布）
# ---------------------------------------------------------------------------


def materialize_result(spec: dict) -> dict:
    """message spec → 与 fixture 同形的最小合法 structured result。"""

    def dist(score: float) -> dict:
        s = max(0.0, min(4.0, float(score)))
        lo, hi = math.floor(s), math.ceil(s)
        out = {str(i): 0.0 for i in range(5)}
        if lo == hi:
            out[str(lo)] = 1.0
        else:
            frac = round(s - lo, 6)
            out[str(lo)] = round(1.0 - frac, 6)
            out[str(hi)] = frac
        return out

    conf = float(spec.get("conf", 0.75))

    def block(key: str, default: float) -> dict:
        score = float(spec.get(key, default))
        return {"score": score, "probabilities": dist(score), "confidence": conf}

    intent_choice = spec.get("intent_choice", "continue_topic")
    intent_probs = spec.get("intent_probs") or {intent_choice: 1.0}
    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": conf},
        "intent": {"choice": intent_choice,
                   "probabilities": {k: float(v) for k, v in intent_probs.items()},
                   "confidence": conf},
        "warmth": block("warmth", 2.0),
        "engagement": block("engagement", 2.0),
        "special_attention": block("special", 1.5),
        "relationship_evidence_strength": block("evidence", 2.4),
        "relational_ease": block("ease", 2.3),
        "romantic_signal": float(spec.get("romantic", 0.1)),
        "distancing_signal": float(spec.get("distancing", 0.05)),
        "model": "synthetic-issue19-scenario",
    }


def materialize_messages(messages: list[dict]) -> list[dict]:
    """展开 {"repeat": {"count": n, "message": spec}} 后逐条物化。"""
    results: list[dict] = []
    for entry in messages:
        if not isinstance(entry, dict):
            raise DiagnosticError(f"message spec 必须是 object：{entry!r}")
        if "repeat" in entry:
            repeat = entry["repeat"]
            count = int(repeat.get("count", 0))
            if count <= 0 or count > 500:
                raise DiagnosticError(f"repeat.count 非法：{count}")
            results.extend(materialize_result(repeat["message"]) for _ in range(count))
        else:
            results.append(materialize_result(entry))
    return results


def scenario_entries(case: dict) -> list[dict]:
    results = materialize_messages(case["messages"])
    return [{"index": i, "result": r} for i, r in enumerate(results)]


def load_scenarios(path: str | Path = DEFAULT_SCENARIOS) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("version") != "salience-scenarios-v0.4":
        raise DiagnosticError(f"未知场景集版本：{doc.get('version')}")
    if not doc.get("cases"):
        raise DiagnosticError("场景集为空")
    return doc


# ---------------------------------------------------------------------------
# 候选聚合算法（研究专用；绝不 import 进生产）
# ---------------------------------------------------------------------------

# 每条消息的标量“显著度”输入（direction-blind，忠实模拟数值型候选的设计缺陷）：
# 单维序列实验里取该维度的原生刻度值；跨维实验里取 max-normalized 值。


def cand_weighted_mean(values: list[float], weights: list[float]) -> float | None:
    """current weighted mean（legacy overall 的抽象：Σ v·w / Σ w）。"""
    total = sum(weights)
    if total < scoring.MIN_TOTAL_WEIGHT:
        return None
    return sum(v * w for v, w in zip(values, weights)) / total


def cand_top_k(values: list[float], k: int = 3) -> dict:
    """top-k：恒选 k 条“最高”消息（无阈值 → 噪声里也制造选择）。"""
    order = sorted(range(len(values)), key=lambda i: (-values[i], i))[:k]
    picked = sorted(order)
    if not picked:
        return {"value": None, "selected_indices": []}
    return {"value": sum(values[i] for i in picked) / len(picked),
            "selected_indices": picked}


def cand_percentile_tail(values: list[float], q: float = 0.9) -> dict:
    """percentile / tail：取分布尾部（同质噪声里也产出“尾部”）。"""
    if not values:
        return {"value": None, "selected_indices": []}
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    threshold = ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    picked = [i for i, v in enumerate(values) if v >= threshold]
    return {"value": sum(values[i] for i in picked) / len(picked),
            "selected_indices": sorted(picked)}


def cand_baseline_boost(values: list[float], threshold: float,
                        boost: float) -> dict:
    """baseline + numeric event boost（arbitrary weight 的代表）。"""
    baseline_vals = [v for v in values if v < threshold]
    events = [v for v in values if v >= threshold]
    baseline = sum(baseline_vals) / len(baseline_vals) if baseline_vals else None
    value = (baseline or 0.0) + boost * len(events)
    return {"value": value, "baseline": baseline, "event_count": len(events)}


def cand_capped_cumulative(values: list[float], threshold: float,
                           cap: float) -> dict:
    """capped cumulative evidence：超阈值部分饱和累加（有上限但仍是数值）。"""
    total = sum(max(0.0, v - threshold) for v in values if v >= threshold)
    return {"value": min(cap, total), "raw_sum": total}


def cand_event_channel(values: list[float], threshold: float) -> dict:
    """event-class channel（生产 #19 的抽象：分类计数 + capped tier）。"""
    count = sum(1 for v in values if v >= threshold)
    return {"value": None, "event_count": count,
            "tier": sal.tier_for_count(count), "retained": count >= 1}


# ---------------------------------------------------------------------------
# 期望检查（场景约束；确定性）
# ---------------------------------------------------------------------------

_BASELINE_VALUE_KEYS = {
    "special_attention": ("special_attention", "value"),
    "warmth": ("care_responsiveness", "value"),
    "engagement": ("initiative_engagement", "value"),
    "relational_ease": ("familiarity", "value"),
    "romantic_raw": ("romantic", "raw_avg"),
    "distancing_raw": ("withdrawal", "raw_avg"),
}

_FORBIDDEN_GLOBAL_FIELDS = ("valence", "global_direction", "positive", "negative",
                            "boost", "score_impact", "weight", "net", "combined",
                            "averaged", "balance")


def evaluate_scenario(case: dict) -> dict:
    """对单个场景跑生产 salience + profile 并做约束检查。"""
    entries = scenario_entries(case)
    stats = scoring.compute_conversation_stats(entries)
    salience_out = sal.build_salience(entries, stats=stats)
    profile = rp.build_profile(entries, stats=stats, salience=salience_out)
    expectation = case.get("expectation") or {}
    events = salience_out["events"]
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    by_class: dict[str, list[dict]] = {}
    for e in events:
        by_class.setdefault(e["event_class"], []).append(e)

    for cls, req in (expectation.get("event_classes") or {}).items():
        got = by_class.get(cls, [])
        ok = req.get("min", 0) <= len(got) <= req.get("max", 10 ** 6)
        if "direction" in req:
            ok = ok and all(e["direction"] == req["direction"] for e in got)
        if "dimension" in req:
            ok = ok and all(e["dimension"] == req["dimension"] for e in got)
        check(f"event_class:{cls}", ok,
              f"count={len(got)}（期望 {req.get('min', 0)}~{req.get('max', 10 ** 6)}）"
              f" direction/dimension 校验")

    for cls in expectation.get("forbidden_event_classes") or []:
        got = by_class.get(cls, [])
        check(f"forbidden:{cls}", not got, f"count={len(got)}（期望 0）")

    if "total_events" in expectation:
        req = expectation["total_events"]
        ok = req.get("min", 0) <= len(events) <= req.get("max", 10 ** 6)
        check("total_events", ok,
              f"count={len(events)}（期望 {req.get('min', 0)}~{req.get('max', 10 ** 6)}）")

    for dim_key, tier in (expectation.get("salient_tiers") or {}).items():
        got = salience_out["dimensions"][dim_key]["salient_tier"]
        check(f"salient_tier:{dim_key}", got == tier, f"tier={got}（期望 {tier}）")

    for base_key, req in (expectation.get("baseline") or {}).items():
        dim_key, field = _BASELINE_VALUE_KEYS[base_key]
        value = salience_out["dimensions"][dim_key]["baseline"].get(field)
        ok = value is not None and req.get("min", -1e9) <= value <= req.get("max", 1e9)
        check(f"baseline:{base_key}", ok,
              f"value={value}（期望 {req.get('min')}~{req.get('max')}）")

    if expectation.get("no_global_valence"):
        bad = [e["event_id"] for e in events
               if any(f in e for f in _FORBIDDEN_GLOBAL_FIELDS)
               or e.get("direction") not in (sal.DIRECTION_SUPPORTING,
                                             sal.DIRECTION_COUNTER)
               or e.get("dimension") not in rp.DIMENSION_ORDER]
        check("no_global_valence", not bad, f"违规事件：{bad}")
        mixed = [k for k in rp.DIMENSION_ORDER
                 if any(f in salience_out["dimensions"][k] for f in _FORBIDDEN_GLOBAL_FIELDS)]
        check("no_global_valence:dimension", not mixed, f"违规维度：{mixed}")

    if expectation.get("no_averaging"):
        salient_n = sum(len(salience_out["dimensions"][k]["salient_events"])
                        for k in rp.DIMENSION_ORDER)
        counter_n = sum(len(salience_out["dimensions"][k]["counter_events"])
                        for k in rp.DIMENSION_ORDER)
        check("no_averaging", salient_n > 0 and counter_n > 0,
              f"salient={salient_n} counter={counter_n}（须并存）")

    if expectation.get("d5_high_info_caveat"):
        bad = [e["event_id"] for e in events
               if (e["relationship_evidence_strength"] or 0)
               >= sal.HIGH_INFORMATION_EVIDENCE
               and not any("高信息量 ≠ 正向关系信号" in note for note in e["limitations"])]
        check("d5_high_info_caveat", not bad, f"缺 D5 限制的事件：{bad}")

    if expectation.get("boundary_pressure_unsupported"):
        bp = profile["dimensions"]["boundary_pressure"]
        ok = (bp["status"] == rp.STATUS_UNSUPPORTED
              and not bp["salient_events"] and not bp["counter_events"])
        check("boundary_pressure_unsupported", ok,
              f"status={bp['status']} events={len(bp['salient_events']) + len(bp['counter_events'])}")

    if expectation.get("unique_event_ids"):
        ids = [e["event_id"] for e in events]
        check("unique_event_ids", len(ids) == len(set(ids)),
              f"events={len(ids)} unique={len(set(ids))}")

    return {
        "id": case["id"],
        "scenario": case.get("scenario"),
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "event_count": len(events),
        "event_classes": {cls: len(v) for cls, v in sorted(by_class.items())},
        "baseline_summary": salience_out["baseline_summary"],
    }


# ---------------------------------------------------------------------------
# dilution matrix（§21）：强事件 + N 条普通消息
# ---------------------------------------------------------------------------

DILUTION_COUNTS = (0, 1, 5, 10, 30, 100)

_EVENT_SPEC = {"warmth": 2.6, "engagement": 3.0, "special": 3.6, "evidence": 3.2,
               "ease": 2.4, "romantic": 0.2, "distancing": 0.04, "conf": 0.85}
_FILLER_SPEC = {"warmth": 2.0, "engagement": 2.1, "special": 1.2, "evidence": 1.5,
                "ease": 2.2, "romantic": 0.08, "distancing": 0.06, "conf": 0.75}


def _entries(specs: list[dict]) -> list[dict]:
    return [{"index": i, "result": materialize_result(s)}
            for i, s in enumerate(specs)]


def dilution_matrix() -> dict:
    rows = []
    for n in DILUTION_COUNTS:
        entries = _entries([_EVENT_SPEC] + [_FILLER_SPEC] * n)
        stats = scoring.compute_conversation_stats(entries)
        salience_out = sal.build_salience(entries, stats=stats)
        profile = rp.build_profile(entries, stats=stats, salience=salience_out)
        special = salience_out["dimensions"]["special_attention"]
        rows.append({
            "n_fillers": n,
            "legacy_overall": stats["overall"],
            "legacy_recent": stats["recent"],
            "baseline_special": special["baseline"]["value"],
            "event_retained": special["salient_count"] >= 1,
            "salient_count": special["salient_count"],
            "event_class": (special["salient_events"][0]["event_class"]
                            if special["salient_events"] else None),
            "salient_tier": special["salient_tier"],
            "profile_conclusion": profile["dimensions"]["special_attention"]
            ["conclusion"],
        })

    # 候选算法 retention：special 序列（事件 3.6 vs 填充 1.2）
    candidate_rows = []
    for n in DILUTION_COUNTS:
        values = [3.6] + [1.2] * n
        metrics = [materialize_result(_EVENT_SPEC)] + \
            [materialize_result(_FILLER_SPEC)] * n
        weights = [scoring.message_metrics({"result": r})["weight"] for r in metrics]
        filler_only = [1.2] * n
        filler_weights = weights[1:]
        row = {"n_fillers": n}
        for name, fn in (
            ("weighted_mean", lambda vs, ws: cand_weighted_mean(vs, ws)),
            ("top_k", lambda vs, ws: cand_top_k(vs, 3)["value"]),
            ("percentile", lambda vs, ws: cand_percentile_tail(vs, 0.9)["value"]),
            ("baseline_boost", lambda vs, ws: cand_baseline_boost(
                vs, 3.0, 15.0)["value"]),
            ("capped_cumulative", lambda vs, ws: cand_capped_cumulative(
                vs, 3.0, 30.0)["value"]),
            ("event_channel", lambda vs, ws: cand_event_channel(vs, 3.0)
             ["event_count"]),
        ):
            row[name] = fn(values, weights)
            row[f"{name}_filler_only"] = fn(filler_only, filler_weights)
        candidate_rows.append(row)

    # retention ratio：(混合 − 仅填充) / (仅事件 − 无) ，event_channel 恒为 1
    retention = []
    for row in candidate_rows:
        entry = {"n_fillers": row["n_fillers"]}
        for name in ("weighted_mean", "top_k", "percentile", "baseline_boost",
                     "capped_cumulative", "event_channel"):
            mixed = row[name]
            base = row[f"{name}_filler_only"]
            if name == "event_channel":
                entry[name] = 1.0 if mixed and mixed >= 1 else 0.0
                continue
            solo = {"weighted_mean": 3.6, "top_k": 3.6,
                    "percentile": 3.6, "baseline_boost": 15.0,
                    "capped_cumulative": 0.6}[name]
            denom = abs(solo - (base if base is not None else 0.0)) or 1.0
            entry[name] = round(abs((mixed or 0.0) - (base or 0.0)) / denom, 4)
        retention.append(entry)

    return {"counts": list(DILUTION_COUNTS), "rows": rows,
            "candidate_rows": candidate_rows, "retention": retention,
            "event_profile": dict(_EVENT_SPEC), "filler_profile": dict(_FILLER_SPEC)}


# ---------------------------------------------------------------------------
# outlier / repeated / counter / false-positive / D5 度量
# ---------------------------------------------------------------------------

_EXTREME_SPEC = {"warmth": 4.0, "engagement": 4.0, "special": 4.0, "evidence": 4.0,
                 "ease": 2.4, "romantic": 0.95, "distancing": 0.02, "conf": 0.95}


def outlier_control() -> dict:
    """单条极端事件对 baseline 的控制力（越小越安全）。"""
    baseline = [_FILLER_SPEC] * 30
    with_extreme = baseline + [_EXTREME_SPEC]
    sal_base = sal.build_salience(_entries(baseline))
    sal_ext = sal.build_salience(_entries(with_extreme))
    base_value = sal_base["dimensions"]["special_attention"]["baseline"]["value"]
    ext_value = sal_ext["dimensions"]["special_attention"]["baseline"]["value"]
    base_values = [1.2] * 30
    ext_values = base_values + [4.0]
    base_weights = [1.0] * 30
    ext_weights = base_weights + [1.0]
    return {
        "event_channel": {"baseline_without": base_value, "baseline_with": ext_value,
                          "baseline_shift": round(abs((ext_value or 0) - (base_value or 0)), 4),
                          "events_captured": len(sal_ext["events"])},
        "weighted_mean": {
            "baseline_shift": round(abs(cand_weighted_mean(ext_values, ext_weights)
                                        - cand_weighted_mean(base_values, base_weights)), 4)},
        "top_k": {
            "baseline_shift": round(abs(cand_top_k(ext_values, 3)["value"]
                                        - cand_top_k(base_values, 3)["value"]), 4)},
        "percentile": {
            "baseline_shift": round(abs(cand_percentile_tail(ext_values, 0.9)["value"]
                                        - cand_percentile_tail(base_values, 0.9)["value"]), 4)},
        "baseline_boost": {
            "baseline_shift": round(abs(cand_baseline_boost(ext_values, 3.0, 15.0)["value"]
                                        - cand_baseline_boost(base_values, 3.0, 15.0)["value"]), 4)},
        "capped_cumulative": {
            "baseline_shift": round(abs(cand_capped_cumulative(ext_values, 3.0, 30.0)["value"]
                                        - cand_capped_cumulative(base_values, 3.0, 30.0)["value"]), 4)},
    }


def repeated_growth() -> dict:
    """1 / 2 / 3 / 5 条同类显著证据的结构增长（capped tier vs 数值累加）。"""
    rows = []
    for k in (1, 2, 3, 5):
        entries = _entries([_EVENT_SPEC] * k + [_FILLER_SPEC] * 10)
        salience_out = sal.build_salience(entries)
        special = salience_out["dimensions"]["special_attention"]
        values = [3.6] * k + [1.2] * 10
        rows.append({
            "events": k,
            "channel_tier": special["salient_tier"],
            "channel_count": special["salient_count"],
            "baseline_boost_value": cand_baseline_boost(values, 3.0, 15.0)["value"],
            "capped_cumulative_value": cand_capped_cumulative(values, 3.0, 30.0)["value"],
            "weighted_mean_value": cand_weighted_mean(values, [1.0] * len(values)),
        })
    return {"rows": rows}


def counter_preservation() -> dict:
    """强 supporting + 强 counter：候选能否同时表达两方向（抵消率越低越好）。"""
    positive = [3.6] + [1.2] * 4
    negative = [0.4] + [1.2] * 4
    both = [3.6, 0.4] + [1.2] * 4
    w = [1.0] * len(both)

    def _wmean(vals):
        return cand_weighted_mean(vals, [1.0] * len(vals))

    cancellation = abs(_wmean(both) - _wmean([1.2] * 6)) / \
        (abs(_wmean(positive[:1] + [1.2] * 4) - _wmean([1.2] * 5)) or 1.0)
    entries = _entries([
        {"warmth": 2.6, "engagement": 3.0, "special": 3.6, "evidence": 3.2,
         "ease": 2.4, "romantic": 0.2, "distancing": 0.04, "conf": 0.85},
        {"warmth": 0.4, "engagement": 0.5, "special": 0.8, "evidence": 2.6,
         "ease": 0.8, "romantic": 0.04, "distancing": 0.3, "conf": 0.8},
    ])
    salience_out = sal.build_salience(entries)
    salient_n = sum(len(salience_out["dimensions"][k]["salient_events"])
                    for k in rp.DIMENSION_ORDER)
    counter_n = sum(len(salience_out["dimensions"][k]["counter_events"])
                    for k in rp.DIMENSION_ORDER)
    return {
        "weighted_mean_both": round(_wmean(both), 4),
        "weighted_mean_cancellation_ratio": round(cancellation, 4),
        "event_channel": {"salient_retained": salient_n, "counter_retained": counter_n,
                          "both_visible": salient_n > 0 and counter_n > 0},
        "top_k_note": "top-k/percentile 为 direction-blind 选择，正反事件混在同一列表",
    }


def false_positive_audit() -> dict:
    """反直觉负例 + D5：各候选在“无显著证据”场景下的输出（数值越低越安全）。"""
    noise = [1.2, 1.4, 0.9, 1.3, 1.1] * 8   # S6 同质短回复
    uniform = [2.4, 2.6, 2.2, 2.5, 2.3] * 6  # S8 高熟悉普通互动
    rows = {
        "noise_top_k_selected": cand_top_k(noise, 3)["selected_indices"],
        "noise_percentile_selected": len(cand_percentile_tail(noise, 0.9)
                                         ["selected_indices"]),
        "noise_event_channel_count": cand_event_channel(noise, 3.0)["event_count"],
        "noise_baseline_boost_value": cand_baseline_boost(noise, 3.0, 15.0)["value"],
        "uniform_event_channel_count": cand_event_channel(uniform, 3.0)["event_count"],
    }
    # D5：施压消息（#17 实测数值）在 direction-blind 候选里是“正向抬升”
    pressure_values = [1.2, 1.2, 3.4]     # 普通 + 高 engagement/evidence 施压
    pressure_base = [1.2, 1.2]
    d5 = {
        "weighted_mean_with_pressure": cand_weighted_mean(
            pressure_values, [0.35, 0.35, 0.64]),
        "weighted_mean_without": cand_weighted_mean(
            pressure_base, [0.35, 0.35]),
        "top_k_selects_pressure": 2 in cand_top_k(pressure_values, 1)["selected_indices"],
        "baseline_boost_value": cand_baseline_boost(pressure_values, 3.0, 15.0)["value"],
    }
    d5["weighted_mean_positive_lift"] = round(
        (d5["weighted_mean_with_pressure"] or 0) - (d5["weighted_mean_without"] or 0), 4)
    return {"rows": rows, "d5": d5}


def duplication_robustness() -> dict:
    """同一消息重复计算路径：事件身份去重。"""
    spec = {"warmth": 3.2, "engagement": 3.4, "special": 3.6, "evidence": 3.6,
            "ease": 2.4, "romantic": 0.9, "distancing": 0.05, "conf": 0.85}
    entries = _entries([spec])
    doubled = [dict(entries[0]), dict(entries[0])]   # 同一 index 出现两次
    single = sal.build_salience(entries)
    dup = sal.build_salience(doubled)
    return {
        "single_event_count": len(single["events"]),
        "duplicated_input_event_count": len(dup["events"]),
        "duplicate_suppressed": dup["diagnostics"]["duplicate_suppressed"],
        "stable_ids": sorted({e["event_id"] for e in single["events"]})
        == sorted({e["event_id"] for e in dup["events"]}),
    }


# ---------------------------------------------------------------------------
# 汇总报告
# ---------------------------------------------------------------------------


def build_report(scenarios_path: str | Path = DEFAULT_SCENARIOS) -> dict:
    doc = load_scenarios(scenarios_path)
    scenario_reports = [evaluate_scenario(case) for case in doc["cases"]]
    comparison = comparison_matrix()
    return {
        "version": "salience-diagnostics-v1",
        "scenario_meta": doc["meta"],
        "scenarios": scenario_reports,
        "scenarios_passed": sum(1 for r in scenario_reports if r["passed"]),
        "scenarios_total": len(scenario_reports),
        "dilution": dilution_matrix(),
        "outlier": outlier_control(),
        "repeated": repeated_growth(),
        "counter": counter_preservation(),
        "false_positive": false_positive_audit(),
        "duplication": duplication_robustness(),
        "comparison": comparison,
    }


def comparison_matrix() -> list[dict]:
    """§22 比较矩阵的数据底座：每格结论引用具体度量（不做主观打分）。"""
    dil = dilution_matrix()
    out = outlier_control()
    rep = repeated_growth()
    cnt = counter_preservation()
    fp = false_positive_audit()
    retention_100 = next(r for r in dil["retention"] if r["n_fillers"] == 100)
    repeated_rows = rep["rows"]

    def _row(name, retention, outlier, counter, direction, interp, false_pos):
        return {
            "method": name,
            "retention": retention,
            "outlier_safety": outlier,
            "counter_preservation": counter,
            "direction_safety": direction,
            "interpretability": interp,
            "false_positive_control": false_pos,
        }

    return [
        _row("weighted mean (legacy)",
             f"N=100 retention ratio {retention_100['weighted_mean']}（稀释失效）",
             f"baseline shift {out['weighted_mean']['baseline_shift']}",
             f"抵消率 {cnt['weighted_mean_cancellation_ratio']}（正反互抵）",
             f"D5 抬升 +{fp['d5']['weighted_mean_positive_lift']}（direction-blind）",
             "单一分数，无法回答“为什么显著”",
             "S6 噪声不产生选择，但无事件概念"),
        _row("top-k",
             f"恒选 k 条，retention ratio {retention_100['top_k']}（稀释后只剩填充）",
             f"baseline shift {out['top_k']['baseline_shift']}（极端值必入选）",
             "正反事件混在同一列表",
             f"D5：压力消息入选 top-1 = {fp['d5']['top_k_selects_pressure']}",
             "只有排名，无“为什么显著”",
             f"S6 噪声中仍选出 {len(fp['rows']['noise_top_k_selected'])} 条（伪造显著）"),
        _row("percentile / tail",
             f"retention ratio {retention_100['percentile']}",
             f"baseline shift {out['percentile']['baseline_shift']}",
             "正反事件混在同一尾部",
             "direction-blind（同 top-k）",
             "只有分位数，无逐条解释",
             f"S6 噪声尾部仍选出 {fp['rows']['noise_percentile_selected']} 条"),
        _row("baseline + numeric boost",
             "boost 不随 N 衰减，但数值任意（+15/条）",
             f"baseline shift {out['baseline_boost']['baseline_shift']}",
             "boost 同时作用于正反（需另设符号规则 = 新权重）",
             "boost 由 evidence 驱动即 direction-blind（D5 风险）",
             "数值可解释性弱（为何 +15？）",
             f"S6 噪声 boost 值 {fp['rows']['noise_baseline_boost_value']}"),
        _row("capped cumulative",
             "饱和后不再增长（保留但不分维度）",
             f"baseline shift {out['capped_cumulative']['baseline_shift']}",
             "正反在同一饱和和里互抵",
             "direction-blind",
             "无逐条 reason",
             "阈值内噪声会被累加"),
        _row("event-class channel（选定）",
             f"retention ratio {retention_100['event_channel']}（分类保留，N 无关）",
             f"baseline shift {out['event_channel']['baseline_shift']}（事件排除出基线）",
             f"salient {cnt['event_channel']['salient_retained']} + counter "
             f"{cnt['event_channel']['counter_retained']} 并存",
             "维度内方向 + D5 限制 + 无全局效价（S14 强制）",
             "每事件带 reason / alternative_explanation / limitations",
             f"S6 噪声事件数 {fp['rows']['noise_event_channel_count']}（阈值门槛）"),
    ]


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


def _fmt(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_markdown(report: dict) -> str:
    lines = ["# Issue #19 Salience 诊断报告（salience-diagnostics-v1）", ""]
    lines += [
        "> 全部数据离线、确定性生成；合成场景只能证明“若 Jev 给出这些信号，",
        "> 本地算法会发生什么”，不构成真实 Jev 实证。",
        "",
        f"- 场景约束检查：{report['scenarios_passed']} / {report['scenarios_total']} 通过",
        "",
    ]

    lines += ["## 1. S1~S15 场景检查", ""]
    for r in report["scenarios"]:
        mark = "✓" if r["passed"] else "✗"
        lines.append(f"- {mark} **{r['id']}**：事件 {r['event_count']} 条"
                     f"（{r['event_classes'] or '无'}）")
        for c in r["checks"]:
            if not c["passed"]:
                lines.append(f"    - ✗ {c['name']}：{c['detail']}")
    lines.append("")

    lines += ["## 2. Dilution matrix（强事件 + N 条普通消息）", "",
              "| N | legacy overall | legacy recent | baseline special | 事件保留 | "
              "事件类 | tier |", "|---|---|---|---|---|---|---|"]
    for row in report["dilution"]["rows"]:
        lines.append(
            f"| {row['n_fillers']} | {_fmt(row['legacy_overall'])} | "
            f"{_fmt(row['legacy_recent'])} | {_fmt(row['baseline_special'])} | "
            f"{'是' if row['event_retained'] else '否'} | {row['event_class'] or '-'} | "
            f"{row['salient_tier']} |")
    lines.append("")
    lines += ["retention ratio（(混合−仅填充)/(仅事件−无)）：", "",
              "| N | weighted mean | top-k | percentile | boost | capped | event channel |",
              "|---|---|---|---|---|---|---|"]
    for entry in report["dilution"]["retention"]:
        lines.append(
            f"| {entry['n_fillers']} | {entry['weighted_mean']} | {entry['top_k']} | "
            f"{entry['percentile']} | {entry['baseline_boost']} | "
            f"{entry['capped_cumulative']} | {entry['event_channel']} |")
    lines.append("")

    lines += ["## 3. Outlier control（baseline shift，越小越安全）", ""]
    for name, entry in report["outlier"].items():
        lines.append(f"- {name}：{entry}")
    lines.append("")

    lines += ["## 4. Repeated evidence（1/2/3/5 条同类事件）", "",
              "| 条数 | channel tier | channel count | boost 值 | capped 值 | weighted mean |",
              "|---|---|---|---|---|---|"]
    for row in report["repeated"]["rows"]:
        lines.append(f"| {row['events']} | {row['channel_tier']} | "
                     f"{row['channel_count']} | {_fmt(row['baseline_boost_value'])} | "
                     f"{_fmt(row['capped_cumulative_value'])} | "
                     f"{_fmt(row['weighted_mean_value'])} |")
    lines.append("")

    lines += ["## 5. Counter preservation", ""]
    cnt = report["counter"]
    lines.append(f"- weighted mean 正反同现值：{cnt['weighted_mean_both']}"
                 f"（抵消率 {cnt['weighted_mean_cancellation_ratio']}）")
    lines.append(f"- event channel：salient {cnt['event_channel']['salient_retained']} "
                 f"+ counter {cnt['event_channel']['counter_retained']} 并存 = "
                 f"{cnt['event_channel']['both_visible']}")
    lines.append("")

    lines += ["## 6. False positives 与 D5", ""]
    for key, value in report["false_positive"]["rows"].items():
        lines.append(f"- {key}：{value}")
    for key, value in report["false_positive"]["d5"].items():
        lines.append(f"- D5 {key}：{value}")
    lines.append("")

    lines += ["## 7. Duplication robustness", ""]
    for key, value in report["duplication"].items():
        lines.append(f"- {key}：{value}")
    lines.append("")

    lines += ["## 8. 候选算法比较矩阵（每格引用上列实测）", "",
              "| 方法 | retention | outlier safety | counter preservation | "
              "direction safety | interpretability | false positives |",
              "|---|---|---|---|---|---|---|"]
    for row in report["comparison"]:
        lines.append(f"| {row['method']} | {row['retention']} | "
                     f"{row['outlier_safety']} | {row['counter_preservation']} | "
                     f"{row['direction_safety']} | {row['interpretability']} | "
                     f"{row['false_positive_control']} |")
    lines.append("")
    return "\n".join(lines)
