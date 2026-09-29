"""Issue #17 分数压缩诊断（score compression diagnostics）—— 纯离线研究工具。

职责：把当前分析链拆成三层分别观察，定位“趋中 / 压平”发生在哪一层：

- **Layer A（Jev structured result 层）**：emotion / intent 的 choice +
  probabilities + confidence；5 个 Score 的 score + probabilities + confidence；
  2 个 Noul 的原始概率。只读取结构，不重新判断。
- **Layer B（message_metrics 层）**：直接调用生产 ``scoring.message_metrics``
  （绝不复制公式），输出 warmth / engagement / special_attention /
  relationship_evidence_strength / relational_ease / relation_confidence /
  evidence_norm / romantic_raw / romantic_ev / distancing_raw / distancing_ev /
  message_weight / base_score。
- **Layer C（conversation aggregation 层）**：直接调用生产
  ``scoring.compute_conversation_stats``，输出 overall / recent / first_half /
  second_half / trend / effective_messages / total_weight / 各均值 /
  romantic_evidence / distancing_evidence / intent_profiles。

研究边界（与 Issue #17 一致，违者即 bug）：

- **0 生产行为变化**：不修改 scoring.py 任何常量 / 公式，不修改 Jev 问题，
  不修改 SCHEMA_VERSION；本模块只是生产函数的只读消费者。
- **0 网络 / 0 Jev 请求**：不引入任何远端 SDK / HTTP 客户端，
  不发起任何请求；本模块只是生产函数的只读消费者。
- **合成数据不是真实 Jev 输出**：synthetic structured fixtures 只能证明
  “若 Jev 给出这些原始信号，本地算法之后会发生什么”，绝不能证明真实 Jev
  在这些聊天上会给出这些结果。所有产物都带 provenance 标记。
- benchmark 通过率 / delta 都不是“准确率”。

CLI 见 ``scripts/run_score_diagnostics.py``。
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path

import scoring
from analyzer import EMOTION_OPTIONS, INTENT_OPTIONS

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class DiagnosticError(ValueError):
    """诊断数据 / 用法错误（fixture 不合法、缺字段、引用不存在等）。

    缺字段或结构不合法时**显式失败**，绝不静默生成错误结论。
    """


# ---------------------------------------------------------------------------
# 冻结的生产常量快照（只读引用，用于报告与“未改动”证明）
# ---------------------------------------------------------------------------

PRODUCTION_CONSTANTS = {
    "WEIGHT_WARMTH": scoring.WEIGHT_WARMTH,
    "WEIGHT_ENGAGEMENT": scoring.WEIGHT_ENGAGEMENT,
    "WEIGHT_SPECIAL_ATTENTION": scoring.WEIGHT_SPECIAL_ATTENTION,
    "WEIGHT_ROMANTIC": scoring.WEIGHT_ROMANTIC,
    "WEIGHT_DISTANCING_PENALTY": scoring.WEIGHT_DISTANCING_PENALTY,
    "SCORE_MAX": scoring.SCORE_MAX,
    "NOUL_NOISE_FLOOR": scoring.NOUL_NOISE_FLOOR,
    "NOUL_STRONG_MARK": scoring.NOUL_STRONG_MARK,
    "NOUL_MID_GAIN": scoring.NOUL_MID_GAIN,
    "MIN_TOTAL_WEIGHT": scoring.MIN_TOTAL_WEIGHT,
    "MISSING_CONFIDENCE_FALLBACK": scoring.MISSING_CONFIDENCE_FALLBACK,
    "RECENT_WINDOW": scoring.RECENT_WINDOW,
    "TREND_MIN_SAMPLES": scoring.TREND_MIN_SAMPLES,
    "TREND_DELTA_THRESHOLD": scoring.TREND_DELTA_THRESHOLD,
    "EFFECTIVE_MESSAGE_MIN_EVIDENCE": scoring.EFFECTIVE_MESSAGE_MIN_EVIDENCE,
}

SCORE_DIMS = (
    "warmth",
    "engagement",
    "special_attention",
    "relationship_evidence_strength",
    "relational_ease",
)
NOUL_DIMS = ("romantic_signal", "distancing_signal")
CHOICE_DIMS = ("emotion", "intent")
_CHOICE_OPTIONS = {"emotion": set(EMOTION_OPTIONS), "intent": set(INTENT_OPTIONS)}

_EPS = 1e-6


# ---------------------------------------------------------------------------
# 结构校验（严格；缺字段 / 越界一律显式失败）
# ---------------------------------------------------------------------------


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_structured_result(result, where: str,
                               *, require_synthetic_model: bool = True,
                               expectation_tolerance: float = 1e-4,
                               probability_sum_tolerance: float = 1e-4) -> None:
    """校验一条 extract_answers 同形的结构化结果是否满足诊断所需字段。

    诊断相关字段必须**完整**：choice / probabilities / confidence /
    score / Noul 概率缺一不可；Score 的 score 必须等于其概率分布的期望值
    （Jev Score 是概率分布的期望值，这是 Layer A 的语义前提）。

    容差：合成 fixture 逐位精确（默认 1e-4）；真实 Jev raw 的概率只序列化到
    2 位小数，score 与按序列化概率重算的期望值存在舍入偏差（实测 <= 0.03），
    真实数据加载时用 ``expectation_tolerance=0.05`` / ``probability_sum_tolerance``
    放宽，并把偏差作为 Layer A 的数据保真度观察记录在报告里。
    """
    problems: list[str] = []
    if not isinstance(result, dict):
        raise DiagnosticError(f"{where}: 结果必须是 object")

    for dim in CHOICE_DIMS:
        block = result.get(dim)
        if not isinstance(block, dict):
            problems.append(f"{where}.{dim}: 缺少 choice 块")
            continue
        choice = block.get("choice")
        if choice not in _CHOICE_OPTIONS[dim]:
            problems.append(f"{where}.{dim}.choice 非法：{choice!r}")
        probs = block.get("probabilities")
        if not isinstance(probs, dict) or not probs:
            problems.append(f"{where}.{dim}.probabilities 缺失或为空")
        else:
            total = 0.0
            for key, value in probs.items():
                if not _is_num(value) or value < -_EPS:
                    problems.append(f"{where}.{dim}.probabilities[{key!r}] 非法")
                else:
                    total += float(value)
            if abs(total - 1.0) > probability_sum_tolerance:
                problems.append(f"{where}.{dim}.probabilities 之和 {total:g} ≠ 1")
        conf = block.get("confidence")
        if not _is_num(conf) or not 0.0 <= float(conf) <= 1.0:
            problems.append(f"{where}.{dim}.confidence 缺失或越界：{conf!r}")

    for dim in SCORE_DIMS:
        block = result.get(dim)
        if not isinstance(block, dict):
            problems.append(f"{where}.{dim}: 缺少 score 块")
            continue
        score = block.get("score")
        if not _is_num(score) or not 0.0 - _EPS <= float(score) <= 4.0 + _EPS:
            problems.append(f"{where}.{dim}.score 缺失或越界：{score!r}")
        probs = block.get("probabilities")
        if not isinstance(probs, dict) or not probs:
            problems.append(f"{where}.{dim}.probabilities 缺失或为空")
        else:
            total, expected = 0.0, 0.0
            for key, value in probs.items():
                if not _is_num(value) or value < -_EPS:
                    problems.append(f"{where}.{dim}.probabilities[{key!r}] 非法")
                    continue
                if not str(key).isdigit() or not 0 <= int(key) <= 4:
                    problems.append(f"{where}.{dim}.probabilities 键非法：{key!r}")
                    continue
                total += float(value)
                expected += int(key) * float(value)
            if abs(total - 1.0) > probability_sum_tolerance:
                problems.append(f"{where}.{dim}.probabilities 之和 {total:g} ≠ 1")
            if _is_num(score) and abs(expected - float(score)) > expectation_tolerance:
                problems.append(
                    f"{where}.{dim}.score={float(score):g} 不等于概率分布期望值"
                    f" {expected:g}（Jev Score 必须是分布期望）")
        conf = block.get("confidence")
        if not _is_num(conf) or not 0.0 <= float(conf) <= 1.0:
            problems.append(f"{where}.{dim}.confidence 缺失或越界：{conf!r}")

    for dim in NOUL_DIMS:
        value = result.get(dim)
        if not _is_num(value) or not 0.0 <= float(value) <= 1.0:
            problems.append(f"{where}.{dim} 缺失或越界：{value!r}")

    model = result.get("model")
    if not isinstance(model, str) or not model:
        problems.append(f"{where}.model 缺失（必须标注数据来源）")
    elif require_synthetic_model and not model.startswith("synthetic"):
        problems.append(
            f"{where}.model={model!r} 未标注 synthetic——合成 fixture 不得冒充"
            "真实 Jev 输出")

    if problems:
        raise DiagnosticError(
            "结构化结果校验失败（显式失败，不静默继续）：\n  - " + "\n  - ".join(problems))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def load_conversation_fixture(path: str | Path) -> dict:
    """读取 conversation 级 synthetic fixture 并严格校验。

    文件形态::

        {"meta": {"provenance": "synthetic", ...},
         "cases": {"<case_id>": {"target_index": int,
                                 "results": [structured result, ...],
                                 "authoring_notes": {...}}}}
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiagnosticError(f"fixture 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DiagnosticError(f"fixture 文件不是合法 JSON：{path}: {exc}") from exc

    if not isinstance(raw, dict) or set(raw) != {"meta", "cases"}:
        raise DiagnosticError(f"{path}: 顶层必须且只含 meta / cases")
    meta = raw["meta"]
    if not isinstance(meta, dict) or meta.get("provenance") != "synthetic":
        raise DiagnosticError(
            f"{path}: meta.provenance 必须是 'synthetic'（合成 fixture 不得冒充"
            "真实 Jev 输出）")

    cases = raw["cases"]
    if not isinstance(cases, dict) or not cases:
        raise DiagnosticError(f"{path}: cases 必须是非空 object")

    problems: list[str] = []
    for case_id, payload in cases.items():
        where = f"cases[{case_id!r}]"
        if not isinstance(payload, dict):
            problems.append(f"{where}: 必须是 object")
            continue
        unknown = set(payload) - {"target_index", "results", "authoring_notes"}
        if unknown:
            problems.append(f"{where}: 未知字段 {sorted(unknown)}")
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            problems.append(f"{where}.results 必须是非空列表")
            continue
        target_index = payload.get("target_index")
        if (not isinstance(target_index, int) or isinstance(target_index, bool)
                or not 0 <= target_index < len(results)):
            problems.append(f"{where}.target_index 越界或缺失：{target_index!r}")
        for i, result in enumerate(results):
            try:
                validate_structured_result(result, f"{where}.results[{i}]")
            except DiagnosticError as exc:
                problems.append(str(exc))
    if problems:
        raise DiagnosticError(
            "conversation fixture 校验失败（显式失败，不静默继续）：\n  - "
            + "\n  - ".join(problems))
    return {"meta": meta, "cases": cases, "path": str(path).replace("\\", "/"),
            "sha256": _sha256(path)}


def load_pairs(path: str | Path, case_ids: set[str]) -> dict:
    """读取成对 / 对照元数据并校验引用完整性。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiagnosticError(f"pairs 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DiagnosticError(f"pairs 文件不是合法 JSON：{path}: {exc}") from exc

    problems: list[str] = []
    for key in ("meta", "families", "pairs", "singletons", "coverage_map"):
        if key not in raw:
            problems.append(f"缺少顶层字段 {key!r}")
    if problems:
        raise DiagnosticError("pairs 文件校验失败：\n  - " + "\n  - ".join(problems))

    families = raw["families"]
    pair_ids: set[str] = set()
    covered: set[str] = set()
    for p in raw["pairs"]:
        where = f"pairs[{p.get('pair_id')!r}]"
        for key in ("pair_id", "family", "baseline", "contrast", "factor",
                    "issue_bullet", "expected_distinguishers"):
            if key not in p:
                problems.append(f"{where}: 缺少字段 {key!r}")
        if p.get("pair_id") in pair_ids:
            problems.append(f"{where}: pair_id 重复")
        pair_ids.add(p.get("pair_id"))
        if p.get("family") not in families:
            problems.append(f"{where}: 未知 family {p.get('family')!r}")
        for side in ("baseline", "contrast"):
            if p.get(side) not in case_ids:
                problems.append(f"{where}.{side}: 不存在的 case id {p.get(side)!r}")
            else:
                covered.add(p[side])
        if not p.get("expected_distinguishers"):
            problems.append(f"{where}: expected_distinguishers 不能为空")
    for s in raw["singletons"]:
        where = f"singletons[{s.get('case_id')!r}]"
        for key in ("case_id", "family", "issue_bullet",
                    "expected_distinguishing_points"):
            if key not in s:
                problems.append(f"{where}: 缺少字段 {key!r}")
        if s.get("case_id") not in case_ids:
            problems.append(f"{where}: 不存在的 case id {s.get('case_id')!r}")
        else:
            covered.add(s["case_id"])
        if s.get("family") not in families:
            problems.append(f"{where}: 未知 family {s.get('family')!r}")
    missing = case_ids - covered
    if missing:
        problems.append(f"以下 case 未被 pair / singleton 覆盖：{sorted(missing)}")
    for bullet, ref in raw["coverage_map"].items():
        if ref not in pair_ids and ref not in case_ids:
            problems.append(f"coverage_map[{bullet!r}] 指向不存在的条目 {ref!r}")
    if problems:
        raise DiagnosticError(
            "pairs 文件校验失败（显式失败，不静默继续）：\n  - " + "\n  - ".join(problems))
    return {"meta": raw["meta"], "families": families, "pairs": raw["pairs"],
            "singletons": raw["singletons"], "coverage_map": raw["coverage_map"],
            "frozen_real_pairs": raw.get("frozen_real_pairs", []),
            "path": str(path).replace("\\", "/"), "sha256": _sha256(path)}


# ---------------------------------------------------------------------------
# 三层提取
# ---------------------------------------------------------------------------


def layer_a(result: dict) -> dict:
    """Layer A：Jev structured result 层（原样保留概率分布与置信度）。"""
    out: dict = {"choice": {}, "scores": {}, "noul": {}}
    for dim in CHOICE_DIMS:
        block = result[dim]
        out["choice"][dim] = {
            "choice": block["choice"],
            "probabilities": dict(block["probabilities"]),
            "confidence": block["confidence"],
        }
    for dim in SCORE_DIMS:
        block = result[dim]
        out["scores"][dim] = {
            "score": float(block["score"]),
            "probabilities": dict(block["probabilities"]),
            "confidence": block["confidence"],
        }
    out["noul"] = {
        "romantic_signal": float(result["romantic_signal"]),
        "distancing_signal": float(result["distancing_signal"]),
    }
    return out


def layer_b(result: dict, index: int = 0) -> dict:
    """Layer B：message_metrics 层（调用生产函数，不复制公式）。"""
    metrics = scoring.message_metrics({"index": index, "result": result})
    if metrics is None:
        raise DiagnosticError(
            "message_metrics() 返回 None：结果缺少聚合所需字段（显式失败，"
            "不静默生成错误结论）")
    return {
        "warmth": metrics["warmth"],
        "engagement": metrics["engagement"],
        "special_attention": metrics["special_attention"],
        "relationship_evidence_strength": metrics["evidence"],
        "relational_ease": metrics["relational_ease"],
        "relation_confidence": metrics["relation_confidence"],
        "evidence_norm": metrics["evidence_norm"],
        "romantic_raw": metrics["romantic_raw"],
        "romantic_ev": metrics["romantic_ev"],
        "distancing_raw": metrics["distancing_raw"],
        "distancing_ev": metrics["distancing_ev"],
        "message_weight": metrics["weight"],
        "base_score": metrics["base_score"],
        "warnings": list(metrics["warnings"]),
    }


def layer_c(results: list[dict]) -> dict:
    """Layer C：conversation aggregation 层（调用生产函数）。"""
    entries = [{"index": i, "result": r} for i, r in enumerate(results)]
    stats = scoring.compute_conversation_stats(entries)
    return {
        "overall": stats["overall"],
        "overall_sufficient": stats["overall_sufficient"],
        "recent": stats["recent"],
        "recent_sufficient": stats["recent_sufficient"],
        "first_half": stats["first_half"],
        "second_half": stats["second_half"],
        "trend": stats["trend"],
        "analyzed": stats["analyzed"],
        "failed": stats["failed"],
        "effective_messages": stats["effective_messages"],
        "total_weight": stats["total_weight"],
        "warmth_avg": stats["warmth_avg"],
        "engagement_avg": stats["engagement_avg"],
        "special_attention_avg": stats["special_attention_avg"],
        "relational_ease_avg": stats["relational_ease_avg"],
        "romantic_evidence": stats["romantic_evidence"],
        "distancing_evidence": stats["distancing_evidence"],
        "romantic_raw_avg": stats["romantic_raw_avg"],
        "distancing_raw_avg": stats["distancing_raw_avg"],
        "intent_profiles": dict(stats["intent_profiles"]),
    }


def analyze_case(case_id: str, fixture_case: dict) -> dict:
    """单个 benchmark case 的三层报告。"""
    results = fixture_case["results"]
    target_index = fixture_case["target_index"]
    return {
        "case_id": case_id,
        "target_index": target_index,
        "ta_messages": len(results),
        "layer_a": {
            "target": layer_a(results[target_index]),
            "messages": [layer_a(r) for r in results],
        },
        "layer_b": {
            "target": layer_b(results[target_index], target_index),
            "messages": [layer_b(r, i) for i, r in enumerate(results)],
        },
        "layer_c": layer_c(results),
    }


# ---------------------------------------------------------------------------
# 成对 delta（自动计算；沿链路逐层对比）
# ---------------------------------------------------------------------------


def _delta(a, b):
    if a is None or b is None:
        return None
    return b - a


def pair_delta(baseline: dict, contrast: dict, pair_meta: dict | None = None
               ) -> dict:
    """成对 delta = contrast − baseline（target 级 A/B 层 + 会话级 C 层）。

    输出同时给出 0~4 分制与 ×25 / ×100 的 0~100 分制，便于直接观察
    “明显 raw difference → transform 后缩小？→ base_score 后缩小？
    → weighted mean 后又缩小？”。
    """
    if pair_meta is not None:
        for key in ("baseline", "contrast"):
            expected = pair_meta.get(key)
            actual = baseline["case_id"] if key == "baseline" else contrast["case_id"]
            if expected is not None and expected != actual:
                raise DiagnosticError(
                    f"pair {pair_meta.get('pair_id')!r}: {key} 期望 {expected!r}，"
                    f"实际 {actual!r}")

    a_scores = baseline["layer_a"]["target"]["scores"]
    c_scores = contrast["layer_a"]["target"]["scores"]
    raw_score_delta = {
        dim: _delta(a_scores[dim]["score"], c_scores[dim]["score"])
        for dim in SCORE_DIMS
    }
    a_noul = baseline["layer_a"]["target"]["noul"]
    c_noul = contrast["layer_a"]["target"]["noul"]
    raw_noul_delta = {dim: _delta(a_noul[dim], c_noul[dim]) for dim in NOUL_DIMS}

    a_b = baseline["layer_b"]["target"]
    c_b = contrast["layer_b"]["target"]
    a_c = baseline["layer_c"]
    c_c = contrast["layer_c"]

    transformed_noul_delta = {
        "romantic_ev": _delta(a_b["romantic_ev"], c_b["romantic_ev"]),
        "distancing_ev": _delta(a_b["distancing_ev"], c_b["distancing_ev"]),
    }

    conv_score_delta = {
        "warmth_avg": _delta(a_c["warmth_avg"], c_c["warmth_avg"]),
        "engagement_avg": _delta(a_c["engagement_avg"], c_c["engagement_avg"]),
        "special_attention_avg": _delta(
            a_c["special_attention_avg"], c_c["special_attention_avg"]),
        "relational_ease_avg": _delta(
            a_c["relational_ease_avg"], c_c["relational_ease_avg"]),
        "romantic_evidence": _delta(a_c["romantic_evidence"], c_c["romantic_evidence"]),
        "distancing_evidence": _delta(
            a_c["distancing_evidence"], c_c["distancing_evidence"]),
    }

    raw_abs_max_4 = max(abs(v) for v in raw_score_delta.values())
    base_delta = _delta(a_b["base_score"], c_b["base_score"])
    overall_delta = _delta(a_c["overall"], c_c["overall"])
    recent_delta = _delta(a_c["recent"], c_c["recent"])

    out = {
        "pair_id": (pair_meta or {}).get("pair_id"),
        "family": (pair_meta or {}).get("family"),
        "issue_bullet": (pair_meta or {}).get("issue_bullet"),
        "factor": (pair_meta or {}).get("factor"),
        "baseline_case": baseline["case_id"],
        "contrast_case": contrast["case_id"],
        "raw_score_delta": raw_score_delta,
        "raw_score_delta_100": {k: v * 25.0 for k, v in raw_score_delta.items()},
        "raw_noul_delta": raw_noul_delta,
        "transformed_noul_delta": transformed_noul_delta,
        "weight_delta": _delta(a_b["message_weight"], c_b["message_weight"]),
        "base_score_delta": base_delta,
        "base_score_delta_100": None if base_delta is None else base_delta * 100.0,
        "conversation_score_delta": conv_score_delta,
        "total_weight_delta": _delta(a_c["total_weight"], c_c["total_weight"]),
        "effective_messages_delta": _delta(
            a_c["effective_messages"], c_c["effective_messages"]),
    }
    out["overall_delta"] = overall_delta
    out["recent_delta"] = recent_delta
    out["delta_trace"] = {
        "raw_score_abs_max_100": raw_abs_max_4 * 25.0,
        "transformed_noul_abs_max_100": max(
            (abs(v) for v in transformed_noul_delta.values()), default=0.0) * 100.0,
        "base_score_abs_100": None if base_delta is None else abs(base_delta) * 100.0,
        "overall_abs_100": None if overall_delta is None else abs(overall_delta),
        "recent_abs_100": None if recent_delta is None else abs(recent_delta),
    }
    return out


# ---------------------------------------------------------------------------
# 确定性 synthetic 压力测试（全部走生产函数）
# ---------------------------------------------------------------------------


def _stress_result(warmth, engagement, special, evidence, ease, romantic,
                   distancing, conf) -> dict:
    """压力测试用的最小合法结构化结果（与 fixture 同形）。"""

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

    def block(score: float) -> dict:
        return {"score": float(score), "probabilities": dist(score),
                "confidence": float(conf)}

    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": float(conf)},
        "intent": {"choice": "continue_topic",
                   "probabilities": {"continue_topic": 1.0},
                   "confidence": float(conf)},
        "warmth": block(warmth),
        "engagement": block(engagement),
        "special_attention": block(special),
        "relationship_evidence_strength": block(evidence),
        "relational_ease": block(ease),
        "romantic_signal": float(romantic),
        "distancing_signal": float(distancing),
        "model": "synthetic-issue17-stress",
    }


# 高信息事件与两类“普通”填充消息的固定画像（确定性，不随运行变化）
_SALIENT = dict(warmth=4.0, engagement=4.0, special=4.0, evidence=4.0, ease=3.0,
                romantic=0.9, distancing=0.05, conf=0.9)
_FILLERS = {
    "low": dict(warmth=1.0, engagement=1.0, special=0.5, evidence=0.5, ease=1.5,
                romantic=0.1, distancing=0.1, conf=0.6),
    "mid": dict(warmth=2.0, engagement=2.0, special=1.0, evidence=1.5, ease=2.0,
                romantic=0.15, distancing=0.1, conf=0.7),
}
DILUTION_COUNTS = (1, 5, 10, 30, 100)


def stress_weighted_mean_dilution() -> dict:
    """压力测试 A：固定一条高信息事件，混入 1/5/10/30/100 条普通消息。

    观察“少量强事件被大量普通消息平均掉的速度”（weighted mean 行为）。
    """
    salient = _stress_result(**_SALIENT)
    salient_only = layer_c([salient])
    rows = []
    for kind, filler in _FILLERS.items():
        for n in DILUTION_COUNTS:
            mixed = layer_c([salient] + [_stress_result(**filler)] * n)
            rows.append({
                "filler_kind": kind,
                "n_fillers": n,
                "salient_only_overall": salient_only["overall"],
                "mixed_overall": mixed["overall"],
                "overall_delta": _delta(salient_only["overall"], mixed["overall"]),
                "mixed_recent": mixed["recent"],
                "recent_delta": _delta(salient_only["overall"], mixed["recent"]),
                "total_weight": mixed["total_weight"],
                "effective_messages": mixed["effective_messages"],
                "filler_base_score_x100": layer_b(_stress_result(**filler))
                ["base_score"] * 100.0,
            })
    return {
        "salient_profile": dict(_SALIENT),
        "filler_profiles": {k: dict(v) for k, v in _FILLERS.items()},
        "dilution_counts": list(DILUTION_COUNTS),
        "salient_only": {"overall": salient_only["overall"],
                         "base_score_x100": layer_b(salient)["base_score"] * 100.0,
                         "message_weight": layer_b(salient)["message_weight"]},
        "rows": rows,
    }


WEIGHT_SENSITIVITY_EVIDENCE = (0.0, 0.5, 1.0, 2.0, 3.0, 4.0)
WEIGHT_SENSITIVITY_CONFIDENCE = (0.3, 0.5, 0.7, 0.9, 1.0)


def stress_message_weight_sensitivity() -> dict:
    """压力测试 B：固定 base signal，扫描 evidence × confidence 的 weight 曲面。

    message_weight = evidence/4 × relation_confidence（生产公式，只读调用）。
    """
    rows = []
    for evidence in WEIGHT_SENSITIVITY_EVIDENCE:
        for conf in WEIGHT_SENSITIVITY_CONFIDENCE:
            result = _stress_result(warmth=2.0, engagement=2.0, special=2.0,
                                    evidence=evidence, ease=2.0, romantic=0.1,
                                    distancing=0.05, conf=conf)
            metrics = layer_b(result)
            rows.append({
                "relationship_evidence_strength": evidence,
                "relation_confidence": conf,
                "evidence_norm": metrics["evidence_norm"],
                "message_weight": metrics["message_weight"],
                "base_score_x100": metrics["base_score"] * 100.0,
            })
    return {
        "evidence_grid": list(WEIGHT_SENSITIVITY_EVIDENCE),
        "confidence_grid": list(WEIGHT_SENSITIVITY_CONFIDENCE),
        "rows": rows,
    }


def stress_noul_transform_curve(step: float = 0.01) -> dict:
    """压力测试 C：p=0.00~1.00 确定性采样 raw → transformed evidence。"""
    if step <= 0:
        raise DiagnosticError("step 必须为正")
    steps = int(round(1.0 / step))
    curve = []
    for i in range(steps + 1):
        p = round(i * step, 6)
        curve.append({"p": p, "evidence": scoring.transform_noul_evidence(p)})
    floor = scoring.NOUL_NOISE_FLOOR
    mark = scoring.NOUL_STRONG_MARK
    gain = scoring.NOUL_MID_GAIN
    mid_slope = gain / (mark - floor)
    strong_slope = (1.0 - gain) / (1.0 - mark)
    return {
        "step": step,
        "curve": curve,
        "segments": {
            "noise_floor": {
                "raw_interval": [0.0, floor],
                "evidence_interval": [0.0, 0.0],
                "slope": 0.0,
            },
            "mid_gain": {
                "raw_interval": [floor, mark],
                "evidence_interval": [0.0, gain],
                "slope": mid_slope,
            },
            "strong_mark": {
                "raw_interval": [mark, 1.0],
                "evidence_interval": [gain, 1.0],
                "slope": strong_slope,
            },
        },
        "compression_facts": {
            "zero_zone_raw_width": floor,
            "mid_band_raw_width": mark - floor,
            "mid_band_evidence_width": gain,
            "strong_band_raw_width": 1.0 - mark,
            "strong_band_evidence_width": 1.0 - gain,
            "strong_vs_mid_slope_ratio": strong_slope / mid_slope,
        },
    }


# 同一 score、不同分布的确定性对照（Jev Score 是概率分布的期望值）
_SAME_SCORE_CASES = [
    {"label": "certain", "score": 2.0,
     "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0, "4": 0.0},
     "confidence": 0.8},
    {"label": "bimodal", "score": 2.0,
     "probabilities": {"0": 0.0, "1": 0.5, "2": 0.0, "3": 0.5, "4": 0.0},
     "confidence": 0.8},
    {"label": "spread", "score": 2.0,
     "probabilities": {"0": 0.25, "1": 0.0, "2": 0.5, "3": 0.0, "4": 0.25},
     "confidence": 0.8},
]


def _distribution_variance(probabilities: dict) -> float:
    mean = sum(int(k) * v for k, v in probabilities.items())
    return sum(v * (int(k) - mean) ** 2 for k, v in probabilities.items())


def _distribution_entropy(probabilities: dict) -> float:
    total = 0.0
    for v in probabilities.values():
        if v > 0:
            total -= v * math.log2(v)
    return total


def stress_same_score_distributions() -> dict:
    """压力测试 D：不同概率分布、同一 score——只看 score 会隐藏什么。"""
    rows = []
    for case in _SAME_SCORE_CASES:
        result = _stress_result(warmth=case["score"], engagement=2.0, special=2.0,
                                evidence=2.0, ease=2.0, romantic=0.1,
                                distancing=0.05, conf=case["confidence"])
        result["warmth"] = {"score": case["score"],
                            "probabilities": dict(case["probabilities"]),
                            "confidence": case["confidence"]}
        metrics = layer_b(result)
        rows.append({
            "label": case["label"],
            "score": case["score"],
            "probabilities": dict(case["probabilities"]),
            "confidence": case["confidence"],
            "variance": _distribution_variance(case["probabilities"]),
            "entropy_bits": _distribution_entropy(case["probabilities"]),
            "base_score_x100": metrics["base_score"] * 100.0,
            "message_weight": metrics["message_weight"],
        })
    return {
        "rows": rows,
        "finding_template": (
            "同一 score=2.0 下 base_score / message_weight 完全一致（confidence 相同时）："
            "score 本身不携带分布信息，probabilities 的方差 / 熵差异全部被隐藏。"),
    }


# ---------------------------------------------------------------------------
# 分布汇总
# ---------------------------------------------------------------------------


def summarize(values: list[float]) -> dict:
    """count / min / median / max / mean / range（空列表显式返回 count=0）。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0, "min": None, "median": None, "max": None,
                "mean": None, "range": None}
    return {
        "count": len(vals),
        "min": min(vals),
        "median": statistics.median(vals),
        "max": max(vals),
        "mean": sum(vals) / len(vals),
        "range": max(vals) - min(vals),
    }


def _range_overlap(a: dict, b: dict) -> float | None:
    if a["count"] == 0 or b["count"] == 0:
        return None
    return max(0.0, min(a["max"], b["max"]) - max(a["min"], b["min"]))


def distribution_section(case_reports: dict, pairs_doc: dict,
                         pair_deltas: list[dict]) -> dict:
    """各关系模式的分布汇总 + 模式间重叠（overall 与主要维度）。"""
    case_family = {}
    for p in pairs_doc["pairs"]:
        case_family[p["baseline"]] = p["family"]
        case_family[p["contrast"]] = p["family"]
    for s in pairs_doc["singletons"]:
        case_family[s["case_id"]] = s["family"]

    by_family: dict[str, list[str]] = {}
    for case_id, family in sorted(case_family.items()):
        by_family.setdefault(family, []).append(case_id)

    metrics = {
        "overall": lambda r: r["layer_c"]["overall"],
        "recent": lambda r: r["layer_c"]["recent"],
        "warmth_avg": lambda r: r["layer_c"]["warmth_avg"],
        "engagement_avg": lambda r: r["layer_c"]["engagement_avg"],
        "special_attention_avg": lambda r: r["layer_c"]["special_attention_avg"],
        "romantic_evidence": lambda r: r["layer_c"]["romantic_evidence"],
        "distancing_evidence": lambda r: r["layer_c"]["distancing_evidence"],
        "total_weight": lambda r: r["layer_c"]["total_weight"],
        "target_base_score_x100": lambda r:
            r["layer_b"]["target"]["base_score"] * 100.0,
    }

    families_out = {}
    for family, ids in sorted(by_family.items()):
        entry = {"case_ids": ids, "metrics": {}}
        for name, getter in metrics.items():
            entry["metrics"][name] = summarize(
                [getter(case_reports[cid]) for cid in ids])
        families_out[family] = entry

    overlap = {}
    metric_names = ("overall", "special_attention_avg", "romantic_evidence",
                    "target_base_score_x100")
    names = sorted(families_out)
    for name in metric_names:
        pairs_overlap = {}
        for i, fa in enumerate(names):
            for fb in names[i + 1:]:
                a = families_out[fa]["metrics"][name]
                b = families_out[fb]["metrics"][name]
                pairs_overlap[f"{fa}|{fb}"] = {
                    "overlap_width": _range_overlap(a, b),
                    "range_a": None if a["count"] == 0 else [a["min"], a["max"]],
                    "range_b": None if b["count"] == 0 else [b["min"], b["max"]],
                    "median_gap": (
                        None if a["count"] == 0 or b["count"] == 0
                        else abs(a["median"] - b["median"])),
                }
        overlap[name] = pairs_overlap

    pair_deltas_by_family: dict[str, list[float]] = {}
    for d in pair_deltas:
        if d.get("overall_delta") is None:
            continue
        pair_deltas_by_family.setdefault(d.get("family") or "unknown", []).append(
            d["overall_delta"])
    return {
        "by_family": families_out,
        "family_overlap": overlap,
        "paired_overall_delta_by_family": {
            family: {"deltas": vals, "summary": summarize(vals)}
            for family, vals in sorted(pair_deltas_by_family.items())},
    }


# ---------------------------------------------------------------------------
# 真实 Jev raw 输出（既存、已冻结、gitignored）——可选研究输入
# ---------------------------------------------------------------------------


def load_real_raw(path: str | Path) -> dict:
    """读取既存的真实 Jev raw 输出（只读研究用；不写回、不提交）。

    只接受带 ``results`` 的 raw 文件；结果按 extract_answers 形状严格校验，
    但 model 字段必须是**真实**模型标签（合成数据不得混入真实层结论）。
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiagnosticError(f"real raw 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DiagnosticError(f"real raw 文件不是合法 JSON：{path}: {exc}") from exc
    results = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(results, dict) or not results:
        raise DiagnosticError(f"{path}: 缺少 results（不是可识别的 raw 输出）")
    problems = []
    for case_id, result in results.items():
        where = f"results[{case_id!r}]"
        if not isinstance(result, dict) or result.get("error"):
            continue
        try:
            validate_structured_result(result, where,
                                       require_synthetic_model=False,
                                       expectation_tolerance=0.05,
                                       probability_sum_tolerance=0.02)
        except DiagnosticError as exc:
            problems.append(str(exc))
        model = result.get("model") if isinstance(result, dict) else None
        if isinstance(model, str) and model.startswith("synthetic"):
            problems.append(f"{where}: model={model!r} 是合成数据，不得混入真实层结论")
    if problems:
        raise DiagnosticError(
            f"real raw 校验失败（{path}）：\n  - " + "\n  - ".join(problems))
    meta = raw.get("meta") if isinstance(raw, dict) else None
    return {"meta": meta if isinstance(meta, dict) else {},
            "cases": raw.get("cases"), "results": results,
            "path": str(path).replace("\\", "/"), "sha256": _sha256(path)}


def real_raw_section(loaded_raws: list[dict], frozen_pairs: list[dict]) -> dict:
    """真实 raw 输出上的 Layer A/B 诊断（单 target 运行 → 无 Layer C）。"""
    if not loaded_raws:
        return {
            "available": False,
            "note": (
                "在 Issue #17 的 no-live-API 约束下，未提供任何既存的真实 Jev "
                "raw 输出；无法仅凭 synthetic fixtures 对真实 Jev 的 case-level "
                "separability 作实证结论。"),
        }
    files = []
    all_targets = []
    for loaded in loaded_raws:
        per_case = {}
        file_targets = []
        for case_id, result in sorted(loaded["results"].items()):
            if not isinstance(result, dict) or result.get("error"):
                continue
            a = layer_a(result)
            b = layer_b(result)
            per_case[case_id] = {"layer_a_target": a, "layer_b_target": b}
            all_targets.append(b)
            file_targets.append(b)
        files.append({
            "path": loaded["path"],
            "sha256": loaded["sha256"],
            "meta": loaded.get("meta") or {},
            "case_count": len(per_case),
            "cases": per_case,
            "target_metric_summary": {
                **{dim: summarize([t[dim] for t in file_targets])
                   for dim in SCORE_DIMS},
                "romantic_raw": summarize([t["romantic_raw"] for t in file_targets]),
                "distancing_raw": summarize([t["distancing_raw"] for t in file_targets]),
                "romantic_ev": summarize([t["romantic_ev"] for t in file_targets]),
                "distancing_ev": summarize([t["distancing_ev"] for t in file_targets]),
                "message_weight": summarize([t["message_weight"] for t in file_targets]),
                "base_score_x100": summarize(
                    [t["base_score"] * 100.0 for t in file_targets]),
            },
        })

    dim_summary = {}
    for dim in SCORE_DIMS:
        dim_summary[dim] = summarize(
            [t[dim] for t in all_targets])
    dim_summary["romantic_raw"] = summarize(
        [t["romantic_raw"] for t in all_targets])
    dim_summary["distancing_raw"] = summarize(
        [t["distancing_raw"] for t in all_targets])
    dim_summary["romantic_ev"] = summarize(
        [t["romantic_ev"] for t in all_targets])
    dim_summary["distancing_ev"] = summarize(
        [t["distancing_ev"] for t in all_targets])
    dim_summary["message_weight"] = summarize(
        [t["message_weight"] for t in all_targets])
    dim_summary["base_score_x100"] = summarize(
        [t["base_score"] * 100.0 for t in all_targets])

    # Layer A 数据保真度观察：真实 Jev 的概率只序列化到 2 位小数，
    # score 与按序列化概率重算的期望值之间存在舍入偏差（不是模型错误）。
    serialization_deviation: dict[str, list[float]] = {}
    for loaded in loaded_raws:
        for case_id, result in sorted(loaded["results"].items()):
            if not isinstance(result, dict) or result.get("error"):
                continue
            for dim in SCORE_DIMS:
                block = result[dim]
                probs = {int(k): v for k, v in dict(block["probabilities"]).items()}
                expected = sum(k * v for k, v in probs.items())
                serialization_deviation.setdefault(dim, []).append(
                    abs(float(block["score"]) - expected))
    serialization_summary = {
        dim: summarize(vals) for dim, vals in sorted(serialization_deviation.items())}

    by_id = {}
    for f in files:
        for case_id, payload in f["cases"].items():
            by_id.setdefault(case_id, (f, payload))

    pair_rows = []
    for pair in frozen_pairs:
        b = by_id.get(pair["baseline"])
        c = by_id.get(pair["contrast"])
        if b is None or c is None or b[0] is not c[0]:
            pair_rows.append({
                "pair_id": pair["pair_id"], "available": False,
                "issue_bullet": pair["issue_bullet"],
                "note": "两侧行不在同一份 raw 输出中或缺失，未计算",
            })
            continue
        a_scores = b[1]["layer_a_target"]["scores"]
        c_scores = c[1]["layer_a_target"]["scores"]
        raw_delta = {dim: _delta(a_scores[dim]["score"], c_scores[dim]["score"])
                     for dim in SCORE_DIMS}
        a_noul = b[1]["layer_a_target"]["noul"]
        c_noul = c[1]["layer_a_target"]["noul"]
        bb, cb = b[1]["layer_b_target"], c[1]["layer_b_target"]
        pair_rows.append({
            "pair_id": pair["pair_id"],
            "available": True,
            "issue_bullet": pair["issue_bullet"],
            "factor": pair["factor"],
            "raw_score_delta": raw_delta,
            "raw_noul_delta": {dim: _delta(a_noul[dim], c_noul[dim])
                               for dim in NOUL_DIMS},
            "transformed_noul_delta": {
                "romantic_ev": _delta(bb["romantic_ev"], cb["romantic_ev"]),
                "distancing_ev": _delta(bb["distancing_ev"], cb["distancing_ev"])},
            "weight_delta": _delta(bb["message_weight"], cb["message_weight"]),
            "base_score_delta_100": _delta(bb["base_score"], cb["base_score"]) * 100.0,
            "delta_trace": {
                "raw_score_abs_max_100": max(abs(v) for v in raw_delta.values()) * 25.0,
                "base_score_abs_100": abs(_delta(bb["base_score"],
                                                 cb["base_score"])) * 100.0,
            },
        })

    return {
        "available": True,
        "files": files,
        "target_metric_summary": dim_summary,
        "aggregate_note": (
            "聚合分布跨多个 schema 时代（各文件 meta.schema_version 不同，"
            "v3.2 重定义过 engagement）：只作描述性参考；成对 delta 均在同一份"
            " raw 内计算，schema 同质。"),
        "score_expectation_serialization_deviation": serialization_summary,
        "frozen_pairs": pair_rows,
        "limitations": [
            "既存真实运行按 case 只分析 1 个 TA target（only_indices）："
            "真实数据只能诊断 Layer A 与单消息 Layer B，无法重建真实 Layer C 聚合。",
            "真实 raw 只覆盖既有案例集（main34 / distancing / phase2 / contrast），"
            "不含 v0.4 relationship benchmark 的 43 个新案例。",
            "raw 文件位于 gitignored 的 evaluation/reports/，只读研究，永不提交。",
        ],
    }


# ---------------------------------------------------------------------------
# 报告构建 / Markdown 渲染
# ---------------------------------------------------------------------------

REPORT_DISCLAIMER = (
    "本报告是 Issue #17 的离线诊断产物：synthetic fixture 部分只证明"
    "“若 Jev 给出这些原始信号，本地算法之后会发生什么”；真实 Jev 部分只来自"
    "既存、已冻结的 raw 输出。benchmark 指标都不是科学准确率。")


def build_report(cases_path: str | Path, conversations_path: str | Path,
                 pairs_path: str | Path,
                 real_raws: list[dict] | None = None) -> dict:
    """构建完整诊断报告（确定性：相同输入 → 相同输出，无时间戳）。"""
    conversations = load_conversation_fixture(conversations_path)
    case_ids = set(conversations["cases"])
    pairs_doc = load_pairs(pairs_path, case_ids)

    cases_path = Path(cases_path)
    case_reports = {
        case_id: analyze_case(case_id, payload)
        for case_id, payload in sorted(conversations["cases"].items())
    }

    pair_deltas = [
        pair_delta(case_reports[p["baseline"]], case_reports[p["contrast"]], p)
        for p in pairs_doc["pairs"]
    ]

    report = {
        "meta": {
            "generator": "score_diagnostics (issue #17 research tooling)",
            "issue": "xinian5216/chat-signal-analyzer#17",
            "schema_version": "chat-signal-v3.3",
            "production_constants": dict(PRODUCTION_CONSTANTS),
            "inputs": {
                "cases_file": str(cases_path).replace("\\", "/"),
                "cases_sha256": _sha256(cases_path),
                "conversations_file": conversations["path"],
                "conversations_sha256": conversations["sha256"],
                "pairs_file": pairs_doc["path"],
                "pairs_sha256": pairs_doc["sha256"],
            },
            "provenance": {
                "conversation_fixtures": "synthetic（人工合成，非真实 Jev 输出）",
                "real_raw": [r["path"] for r in (real_raws or [])],
            },
            "disclaimer": REPORT_DISCLAIMER,
        },
        "cases": case_reports,
        "pairs": pair_deltas,
        "distributions": distribution_section(case_reports, pairs_doc, pair_deltas),
        "stress": {
            "weighted_mean_dilution": stress_weighted_mean_dilution(),
            "message_weight_sensitivity": stress_message_weight_sensitivity(),
            "noul_transform_curve": stress_noul_transform_curve(),
            "same_score_distributions": stress_same_score_distributions(),
        },
        "real_raw": real_raw_section(real_raws or [],
                                     pairs_doc["frozen_real_pairs"]),
    }
    return report


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def render_markdown(report: dict) -> str:
    """人类可读的诊断报告（不含任何聊天正文；case 只出现 ID 与类别）。"""
    lines: list[str] = []
    add = lines.append
    add("# Issue #17 分数压缩诊断报告（score compression diagnostics）")
    add("")
    add(f"> {REPORT_DISCLAIMER}")
    add("")
    add("## 0. 输入与生产常量")
    add("")
    inputs = report["meta"]["inputs"]
    for key in ("cases_file", "conversations_file", "pairs_file"):
        add(f"- {key}: `{inputs[key]}` (sha256 `{inputs[key.replace('file', 'sha256')][:16]}…`)")
    add(f"- schema_version: `{report['meta']['schema_version']}`")
    add("- production constants（只读快照，未修改）：")
    for name, value in sorted(report["meta"]["production_constants"].items()):
        add(f"  - {name} = {value}")
    add("")

    add("## 1. 成对对照（Layer A → B → C 的 delta 链）")
    add("")
    for d in report["pairs"]:
        add(f"### {d['pair_id']}（{d['issue_bullet']}）")
        add("")
        add(f"- factor: {d['factor']}")
        add(f"- baseline `{d['baseline_case']}` → contrast `{d['contrast_case']}`")
        add("")
        add("| 层级 | 指标 | delta（contrast − baseline） |")
        add("|---|---|---|")
        for dim, value in d["raw_score_delta"].items():
            add(f"| A raw | {dim} | {_fmt(value)} (0~4) / {_fmt(value * 25.0, 2)} (0~100) |")
        for dim, value in d["raw_noul_delta"].items():
            add(f"| A raw | {dim} | {_fmt(value)} |")
        for dim, value in d["transformed_noul_delta"].items():
            add(f"| B transform | {dim} | {_fmt(value)} |")
        add(f"| B | message_weight | {_fmt(d['weight_delta'])} |")
        add(f"| B | base_score | {_fmt(d['base_score_delta'])} / {_fmt(d['base_score_delta_100'], 2)} (0~100) |")
        add(f"| C | overall | {_fmt(d['overall_delta'], 2)} (0~100) |")
        add(f"| C | recent | {_fmt(d['recent_delta'], 2)} (0~100) |")
        add(f"| C | total_weight | {_fmt(d['total_weight_delta'])} |")
        trace = d["delta_trace"]
        add(f"| trace | raw max → base → overall（0~100） | "
            f"{_fmt(trace['raw_score_abs_max_100'], 2)} → "
            f"{_fmt(trace['base_score_abs_100'], 2)} → "
            f"{_fmt(trace['overall_abs_100'], 2)} |")
        add("")

    add("## 2. 分布汇总（按关系模式）")
    add("")
    for family, entry in report["distributions"]["by_family"].items():
        add(f"### {family}（{len(entry['case_ids'])} cases）")
        add("")
        add("| 指标 | count | min | median | max | mean | range |")
        add("|---|---|---|---|---|---|---|")
        for name, s in entry["metrics"].items():
            add(f"| {name} | {s['count']} | {_fmt(s['min'], 2)} | {_fmt(s['median'], 2)} "
                f"| {_fmt(s['max'], 2)} | {_fmt(s['mean'], 2)} | {_fmt(s['range'], 2)} |")
        add("")
    add("### overall 的模式间重叠（min-max 区间交叠宽度 / 中位数差）")
    add("")
    add("| 模式对 | overlap_width | median_gap |")
    add("|---|---|---|")
    for pair_name, s in report["distributions"]["family_overlap"]["overall"].items():
        add(f"| {pair_name} | {_fmt(s['overlap_width'], 2)} | {_fmt(s['median_gap'], 2)} |")
    add("")

    add("## 3. 压力测试")
    add("")
    add("### A. weighted-mean dilution（1 条强事件混入 N 条普通消息）")
    add("")
    add("| filler | N | salient-only overall | mixed overall | delta | mixed recent | total_weight |")
    add("|---|---|---|---|---|---|---|")
    for row in report["stress"]["weighted_mean_dilution"]["rows"]:
        add(f"| {row['filler_kind']} | {row['n_fillers']} | "
            f"{_fmt(row['salient_only_overall'], 2)} | {_fmt(row['mixed_overall'], 2)} "
            f"| {_fmt(row['overall_delta'], 2)} | {_fmt(row['mixed_recent'], 2)} "
            f"| {_fmt(row['total_weight'])} |")
    add("")
    add("### B. message_weight 敏感性（evidence × confidence）")
    add("")
    add("| evidence | confidence | evidence_norm | message_weight |")
    add("|---|---|---|---|")
    for row in report["stress"]["message_weight_sensitivity"]["rows"]:
        add(f"| {_fmt(row['relationship_evidence_strength'])} | "
            f"{_fmt(row['relation_confidence'])} | {_fmt(row['evidence_norm'])} "
            f"| {_fmt(row['message_weight'])} |")
    add("")
    curve = report["stress"]["noul_transform_curve"]
    add("### C. transform_noul_evidence 曲线")
    add("")
    add("| 区段 | raw 区间 | evidence 区间 | 斜率 |")
    add("|---|---|---|---|")
    for name in ("noise_floor", "mid_gain", "strong_mark"):
        seg = curve["segments"][name]
        add(f"| {name} | [{_fmt(seg['raw_interval'][0])}, {_fmt(seg['raw_interval'][1])}] "
            f"| [{_fmt(seg['evidence_interval'][0])}, {_fmt(seg['evidence_interval'][1])}] "
            f"| {_fmt(seg['slope'])} |")
    add("")
    add("采样点（step = %s）：" % curve["step"])
    add("")
    add("| p | evidence |")
    add("|---|---|")
    for row in curve["curve"]:
        if round(row["p"] * 100) % 5 == 0:
            add(f"| {_fmt(row['p'], 2)} | {_fmt(row['evidence'])} |")
    add("")
    add("### D. 同 score 不同分布")
    add("")
    add("| 分布 | score | variance | entropy(bits) | base_score(0~100) | message_weight |")
    add("|---|---|---|---|---|---|")
    for row in report["stress"]["same_score_distributions"]["rows"]:
        add(f"| {row['label']} | {_fmt(row['score'])} | {_fmt(row['variance'])} "
            f"| {_fmt(row['entropy_bits'])} | {_fmt(row['base_score_x100'], 2)} "
            f"| {_fmt(row['message_weight'])} |")
    add("")

    add("## 4. 真实 Jev raw 输出层（既存冻结数据，可选）")
    add("")
    real = report["real_raw"]
    if not real.get("available"):
        add(f"- 未提供真实 raw：{real['note']}")
    else:
        add("提供的 raw 文件：")
        for f in real["files"]:
            add(f"- `{f['path']}`（sha256 `{f['sha256'][:16]}…`，"
                f"{f['case_count']} cases，meta={json.dumps(f['meta'], ensure_ascii=False)[:120]}）")
        add("")
        add(f"注意：{real['aggregate_note']}")
        add("")
        add("各文件的 target 级摘要（schema 同质）：")
        add("")
        add("| 文件 | schema | n | warmth med | special med | evidence med | base(0~100) med |")
        add("|---|---|---|---|---|---|---|")
        for f in real["files"]:
            s = f["target_metric_summary"]
            add(f"| `{f['path']}` | {(f['meta'] or {}).get('schema_version', 'unknown')} "
                f"| {f['case_count']} | {_fmt(s['warmth']['median'], 2)} "
                f"| {_fmt(s['special_attention']['median'], 2)} "
                f"| {_fmt(s['relationship_evidence_strength']['median'], 2)} "
                f"| {_fmt(s['base_score_x100']['median'], 2)} |")
        add("")
        add("真实 target 级指标分布（聚合，混合 schema 时代）：")
        add("")
        add("| 指标 | count | min | median | max | mean | range |")
        add("|---|---|---|---|---|---|---|")
        for name, s in real["target_metric_summary"].items():
            add(f"| {name} | {s['count']} | {_fmt(s['min'], 2)} | {_fmt(s['median'], 2)} "
                f"| {_fmt(s['max'], 2)} | {_fmt(s['mean'], 2)} | {_fmt(s['range'], 2)} |")
        add("")
        add("Layer A 序列化保真度：|score − E[序列化概率]|（概率只存 2 位小数的舍入偏差）")
        add("")
        add("| dim | count | min | median | max | mean |")
        add("|---|---|---|---|---|---|")
        for name, s in real["score_expectation_serialization_deviation"].items():
            add(f"| {name} | {s['count']} | {_fmt(s['min'], 4)} | {_fmt(s['median'], 4)} "
                f"| {_fmt(s['max'], 4)} | {_fmt(s['mean'], 4)} |")
        add("")
        add("既有冻结案例集上的成对 raw delta（真实 Jev）：")
        add("")
        for row in real["frozen_pairs"]:
            if not row.get("available"):
                add(f"- {row['pair_id']}: 未计算（{row['note']}）")
                continue
            add(f"- **{row['pair_id']}**（{row['issue_bullet']}）：")
            deltas = "；".join(f"{k} {v:+.2f}" for k, v in row["raw_score_delta"].items()
                               if v is not None)
            add(f"  - raw score delta：{deltas}")
            add(f"  - raw noul delta：{row['raw_noul_delta']}")
            add(f"  - transformed noul delta：{row['transformed_noul_delta']}")
            add(f"  - weight delta {_fmt(row['weight_delta'])}；"
                f"base_score delta {_fmt(row['base_score_delta_100'], 2)}（0~100）")
            add(f"  - trace：raw {_fmt(row['delta_trace']['raw_score_abs_max_100'], 2)}"
                f" → base {_fmt(row['delta_trace']['base_score_abs_100'], 2)}（0~100）")
        add("")
        for limitation in real["limitations"]:
            add(f"- 限制：{limitation}")
    add("")
    add("---")
    add("")
    add("*Diagnostic artifact：不进入生产 UI；不构成科学准确率主张。*")
    add("")
    return "\n".join(lines)
