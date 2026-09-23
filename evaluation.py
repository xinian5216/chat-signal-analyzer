"""离线评估 harness：用人工定义的 benchmark 约束衡量 SignalLens 的分析质量。

设计原则（重要）：

- **纯离线、deterministic**：本模块绝不调用 Jev，也绝不用第二个模型去判断
  Jev 对不对。Evaluator 只做“约束检查”。
- **每个 case 只描述期望与禁止项**，不写死总分（Jev 输出有概率性，我们关心
  的是判断方向与是否过度推断）：

  - Choice 维度（emotion / intent）：结果必须落在人工设定的允许集合内；
  - Score 维度（5 个）：结果必须落在人工设定的 [min, max] 区间；
  - Noul 维度（romantic_signal / distancing_signal）：概率必须满足
    min_probability / max_probability；
  - ``must_not_infer``：反过度推断标签（关心≠喜欢、晚上聊天≠暧昧、
    回复慢≠疏远……），逐条映射为确定性检查；
  - context 检查：校验 Context Builder v2 送出的窗口不包含未来消息、
    且包含目标消息“正在回应”的内容（turn 级）。

- benchmark 通过率不是“科学准确率”，只是**人工定义案例上的
  regression / evaluation 指标**。修改 Jev questions / Context Builder /
  scoring 之前必须先跑 benchmark，报告 baseline vs candidate。

数据文件（全部虚构，无真实昵称 / 隐私信息）：

- ``evaluation/cases.json``：benchmark 案例集；
- ``evaluation/fixtures/baseline_v2.2.json``：合成 baseline 结果
  （用于 CI / 框架自测；不代表真实 Jev 输出）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from parser import parse_chat

EVALUATION_DIR = Path(__file__).resolve().parent / "evaluation"
DEFAULT_CASES_PATH = EVALUATION_DIR / "cases.json"
DEFAULT_FIXTURE_PATH = EVALUATION_DIR / "fixtures" / "baseline_v2.2.json"

# ---------------------------------------------------------------------------
# 维度定义（与 analyzer 的 9 个问题一一对应；只读，不修改问题本身）
# ---------------------------------------------------------------------------

CHOICE_DIMS = ("emotion", "intent")
SCORE_DIMS = (
    "warmth",
    "engagement",
    "special_attention",
    "relationship_evidence_strength",
    "relational_ease",
)
NOUL_DIMS = ("romantic_signal", "distancing_signal")
ALL_DIMS = CHOICE_DIMS + SCORE_DIMS + NOUL_DIMS

# 允许集合来自 analyzer 的题目定义（import 只为复用选项表，不执行任何请求）
from analyzer import (  # noqa: E402 - 维度常量之后统一导入，保证文档顺序
    EMOTION_OPTIONS,
    INTENT_OPTIONS,
)
CHOICE_OPTIONS = {"emotion": set(EMOTION_OPTIONS), "intent": set(INTENT_OPTIONS)}

# must_not_infer 标签 → 确定性检查。标签是“反过度推断契约”的名字，
# 具体阈值集中在此，便于人工审查与长期维护。
# 每条规则返回 (violated: bool, detail: str)。
MUST_NOT_INFER_RULES = {
    "romantic_from_politeness": ("romantic_signal", 0.5),
    "romantic_from_care_alone": ("romantic_signal", 0.5),
    "romantic_from_teasing": ("romantic_signal", 0.5),
    "romantic_from_invitation": ("romantic_signal", 0.6),
    "romantic_from_late_night": ("romantic_signal", 0.5),
    "romantic_from_self_disclosure": ("romantic_signal", 0.5),
    "romantic_from_familiarity": ("romantic_signal", 0.5),
    "romantic_from_media": ("romantic_signal", 0.4),
    "special_attention_overclaim": ("special_attention", 3.0),
    "distancing_from_slow_reply": ("distancing_signal", 0.4),
    "distancing_from_short_reply": ("distancing_signal", 0.4),
    "perfunctory_from_short_reply": ("engagement", 1.0),
    "single_incident_overgeneralization": (
        "relationship_evidence_strength", 3.0),
}
# perfunctory_from_short_reply 是下界检查（engagement 不得低于该值），
# 其它均为上界检查。
_MUST_NOT_LOWER_BOUND = {"perfunctory_from_short_reply"}

# false-positive / 失败分类（聚合统计用）
FP_ROMANTIC = "romantic_false_positive"
FP_ROMANTIC_MISS = "romantic_miss"
FP_DISTANCING = "distancing_false_positive"
FP_DISTANCING_MISS = "distancing_miss"
FP_SPECIAL_ATTENTION = "special_attention_overclaim"
FP_SPECIAL_ATTENTION_UNDER = "special_attention_undervalue"
FP_WARMTH = "warmth_estimation"
FP_ENGAGEMENT = "engagement_estimation"
FP_EVIDENCE = "relationship_evidence_estimation"
FP_EASE = "relational_ease_estimation"
# 每个 Score 维度的 (over, under) 失败分类——真实结果首次触发任何
# 一侧都必须有分类，聚合统计不得因缺键崩溃（历史事故：缺
# special_attention 映射导致一次真实基线在第 34/34 次请求完成后崩溃）。
_SCORE_FAILURE_CATEGORIES: dict[str, tuple[str, str]] = {
    "warmth": (FP_WARMTH + "_over", FP_WARMTH + "_under"),
    "engagement": (FP_ENGAGEMENT + "_over", FP_ENGAGEMENT + "_under"),
    "special_attention": (FP_SPECIAL_ATTENTION, FP_SPECIAL_ATTENTION_UNDER),
    "relationship_evidence_strength": (FP_EVIDENCE + "_over",
                                       FP_EVIDENCE + "_under"),
    "relational_ease": (FP_EASE + "_over", FP_EASE + "_under"),
}
FP_CONTEXT = "context_misunderstanding"
FP_LEAKAGE = "future_message_leakage"
FP_MEDIA_GUESSING = "media_guessing"
FP_MISSING_RESULT = "missing_result"
FP_API_ERROR = "api_error"

KNOWN_TAGS = {"media_unknown", "anti_overclaim", "ambiguity_tolerant",
              "true_positive", "needs_context", "negative_direction"}

# 案例文本中的隐私形态（benchmark 必须干净：虚构内容、无真实 PII）
_PII_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),            # email
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),           # 手机号
    re.compile(r"\b\d{17}[\dxX]\b"),                   # 身份证
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]*"),   # URL
]
# RFC 2606 / 保留域名：文档示例域名不构成真实隐私信息
_RESERVED_HOSTS = ("example.com", "example.net", "example.org",
                   "example.internal", "example.edu", "localhost", "invalid")


def _has_pii(text: str) -> str | None:
    """返回命中的隐私形态描述；干净则返回 None。"""
    for pattern in _PII_PATTERNS[:-1]:
        if pattern.search(text):
            return pattern.pattern
    for match in _PII_PATTERNS[-1].finditer(text):
        url = match.group(0)
        host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        if host.lower() not in _RESERVED_HOSTS:
            return f"url:{url[:40]}"
    return None


class EvaluationError(ValueError):
    """benchmark 数据 / harness 使用错误（case schema、文件缺失等）。"""


# ---------------------------------------------------------------------------
# case schema 校验
# ---------------------------------------------------------------------------

_CASE_REQUIRED = ("id", "category", "title", "chat", "me", "them", "target",
                  "expectations")
_CASE_KNOWN_KEYS = _CASE_REQUIRED + (
    "must_not_infer", "acceptable_ambiguity", "notes", "context_checks",
    "tags")


def validate_case(case: dict, position: int | None = None) -> None:
    """校验单个 benchmark case 的 schema；不合法时抛出 EvaluationError。"""
    where = f"case #{position}" if position is not None else f"case {case.get('id')!r}"
    if not isinstance(case, dict):
        raise EvaluationError(f"{where}: 必须是 JSON object")
    for key in _CASE_REQUIRED:
        if key not in case:
            raise EvaluationError(f"{where}: 缺少必填字段 {key!r}")
    unknown = set(case) - set(_CASE_KNOWN_KEYS)
    if unknown:
        raise EvaluationError(f"{where}: 未知字段 {sorted(unknown)}")
    for key in ("id", "category", "title", "chat", "target"):
        if not isinstance(case[key], str) or not case[key].strip():
            raise EvaluationError(f"{where}: {key!r} 必须是非空字符串")
    for key in ("me", "them"):
        names = case[key]
        if (not isinstance(names, list) or not names
                or not all(isinstance(n, str) and n.strip() for n in names)):
            raise EvaluationError(f"{where}: {key!r} 必须是非空字符串列表")

    expectations = case["expectations"]
    if not isinstance(expectations, dict) or not expectations:
        raise EvaluationError(f"{where}: expectations 必须是非空 object")
    for dim, spec in expectations.items():
        if dim not in ALL_DIMS:
            raise EvaluationError(f"{where}: 未知维度 {dim!r}")
        if not isinstance(spec, dict):
            raise EvaluationError(f"{where}: {dim} 期望必须是 object")
        if dim in CHOICE_DIMS:
            allowed = spec.get("allowed")
            if (not isinstance(allowed, list) or not allowed
                    or not all(isinstance(a, str) for a in allowed)):
                raise EvaluationError(f"{where}: {dim} 需要非空 allowed 列表")
            unknown_opts = set(allowed) - CHOICE_OPTIONS[dim]
            if unknown_opts:
                raise EvaluationError(
                    f"{where}: {dim} allowed 含未知选项 {sorted(unknown_opts)}")
            if set(spec) - {"allowed"}:
                raise EvaluationError(f"{where}: {dim} 只支持 allowed")
        elif dim in SCORE_DIMS:
            low, high = spec.get("min"), spec.get("max")
            for bound in (low, high):
                if bound is not None and not isinstance(bound, (int, float)):
                    raise EvaluationError(f"{where}: {dim} 边界必须是数字")
            if low is None and high is None:
                raise EvaluationError(f"{where}: {dim} 至少需要 min 或 max")
            if low is not None and high is not None and low > high:
                raise EvaluationError(f"{where}: {dim} min > max")
            if set(spec) - {"min", "max"}:
                raise EvaluationError(f"{where}: {dim} 只支持 min/max")
        else:  # NOUL
            low, high = spec.get("min_probability"), spec.get("max_probability")
            for bound in (low, high):
                if bound is not None and (
                        not isinstance(bound, (int, float))
                        or not 0.0 <= bound <= 1.0):
                    raise EvaluationError(
                        f"{where}: {dim} 概率边界必须在 0~1 之间")
            if low is None and high is None:
                raise EvaluationError(
                    f"{where}: {dim} 至少需要 min_probability 或 max_probability")
            if low is not None and high is not None and low > high:
                raise EvaluationError(f"{where}: {dim} 概率下界 > 上界")
            if set(spec) - {"min_probability", "max_probability"}:
                raise EvaluationError(f"{where}: {dim} 只支持 *_probability")

    for tag in case.get("must_not_infer", []):
        if tag not in MUST_NOT_INFER_RULES:
            raise EvaluationError(
                f"{where}: 未知 must_not_infer 标签 {tag!r}；"
                f"已知：{sorted(MUST_NOT_INFER_RULES)}")
    for tag in case.get("tags", []):
        if tag not in KNOWN_TAGS:
            raise EvaluationError(f"{where}: 未知 tag {tag!r}")

    checks = case.get("context_checks", {})
    if not isinstance(checks, dict):
        raise EvaluationError(f"{where}: context_checks 必须是 object")
    if set(checks) - {"must_contain", "no_future_leakage"}:
        raise EvaluationError(
            f"{where}: context_checks 只支持 must_contain / no_future_leakage")
    if "must_contain" in checks and not all(
            isinstance(t, str) and t for t in checks["must_contain"]):
        raise EvaluationError(f"{where}: context_checks.must_contain 必须是字符串列表")

    hit = _has_pii(case["chat"])
    if hit is None:
        hit = _has_pii(case["target"])
    if hit is not None:
        raise EvaluationError(f"{where}: benchmark 文本疑似含真实隐私信息：{hit}")


def load_cases(path: str | Path | None = None) -> list[dict]:
    """读取并校验 benchmark 案例集（保持文件顺序，保证确定性）。"""
    path = Path(path) if path else DEFAULT_CASES_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"benchmark 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"benchmark 文件不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise EvaluationError(f"benchmark 文件必须是非空数组：{path}")
    seen: set[str] = set()
    for i, case in enumerate(raw):
        validate_case(case, i)
        if case["id"] in seen:
            raise EvaluationError(f"case id 重复：{case['id']!r}")
        seen.add(case["id"])
    return raw


# ---------------------------------------------------------------------------
# 结果校验（fixture / real run 产出的分析结果）
# ---------------------------------------------------------------------------


def validate_result(result: dict, where: str = "result") -> None:
    """校验单条分析结果是否符合 extract_answers 的形状。"""
    if not isinstance(result, dict):
        raise EvaluationError(f"{where}: 结果必须是 object")
    for dim in CHOICE_DIMS:
        _require(result, dim, where)
        choice = result[dim].get("choice")
        if choice not in CHOICE_OPTIONS[dim]:
            raise EvaluationError(f"{where}: {dim}.choice 非法：{choice!r}")
        if not isinstance(result[dim].get("probabilities", {}), dict):
            raise EvaluationError(f"{where}: {dim}.probabilities 必须是 object")
    for dim in SCORE_DIMS:
        _require(result, dim, where)
        score = result[dim].get("score")
        if not isinstance(score, (int, float)) or not 0.0 <= score <= 4.0:
            raise EvaluationError(f"{where}: {dim}.score 必须是 0~4 的数字")
    for dim in NOUL_DIMS:
        _require(result, dim, where)
        value = result[dim]
        if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            raise EvaluationError(f"{where}: {dim} 必须是 0~1 的概率值")


def _require(result: dict, key: str, where: str) -> None:
    if key not in result:
        raise EvaluationError(f"{where}: 缺少维度 {key!r}")


def load_fixture_results(path: str | Path | None = None) -> dict:
    """读取 fixture 结果集（case id → 分析结果；可附 conversation_context）。"""
    path = Path(path) if path else DEFAULT_FIXTURE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"fixture 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"fixture 文件不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise EvaluationError(f"fixture 文件必须是非空 object：{path}")
    for case_id, result in raw.items():
        validate_result(result, f"fixture[{case_id}]")
    return raw


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def _check_result_present(case: dict, result) -> list[dict]:
    if result is None:
        return [{"name": "result_present", "passed": False,
                 "detail": "缺少该 case 的分析结果",
                 "failure": FP_MISSING_RESULT}]
    if isinstance(result, dict) and result.get("error"):
        return [{"name": "result_present", "passed": False,
                 "detail": "分析失败：该次请求返回 error",
                 "failure": FP_API_ERROR}]
    return [{"name": "result_present", "passed": True, "detail": ""}]


def _check_expectations(case: dict, result: dict) -> list[dict]:
    constraints: list[dict] = []
    expectations = case["expectations"]
    for dim in CHOICE_DIMS:
        if dim not in expectations:
            continue
        allowed = expectations[dim]["allowed"]
        actual = result[dim]["choice"]
        ok = actual in allowed
        constraints.append({
            "name": f"{dim}_allowed", "passed": ok,
            "detail": "" if ok else f"{dim}={actual!r} 不在允许集合 {allowed}",
            "failure": None if ok else FP_CONTEXT,
        })
    for dim in SCORE_DIMS:
        if dim not in expectations:
            continue
        spec = expectations[dim]
        actual = float(result[dim]["score"])
        low, high = spec.get("min"), spec.get("max")
        ok = (low is None or actual >= low) and (high is None or actual <= high)
        failure = None
        detail = ""
        if not ok:
            if high is not None and actual > high:
                failure = _score_over_category(dim)
            elif low is not None and actual < low:
                failure = _score_under_category(dim)
            detail = f"{dim}={actual:g} 不在 [{low}, {high}]"
        constraints.append({
            "name": f"{dim}_range", "passed": ok, "detail": detail,
            "failure": failure,
        })
    for dim in NOUL_DIMS:
        if dim not in expectations:
            continue
        spec = expectations[dim]
        actual = float(result[dim])
        low = spec.get("min_probability")
        high = spec.get("max_probability")
        ok = (low is None or actual >= low) and (high is None or actual <= high)
        failure = None
        detail = ""
        if not ok:
            if high is not None and actual > high:
                failure = FP_ROMANTIC if dim == "romantic_signal" else FP_DISTANCING
            else:
                failure = FP_ROMANTIC_MISS if dim == "romantic_signal" \
                    else FP_DISTANCING_MISS
            bounds = [b for b in (low, high) if b is not None]
            detail = f"{dim}={actual:g} 不在 [{bounds[0]}, {bounds[-1]}]"
        constraints.append({
            "name": f"{dim}_probability", "passed": ok, "detail": detail,
            "failure": failure,
        })
    return constraints


def _score_over_category(dim: str) -> str:
    return _SCORE_FAILURE_CATEGORIES[dim][0]


def _score_under_category(dim: str) -> str:
    return _SCORE_FAILURE_CATEGORIES[dim][1]


def _check_must_not_infer(case: dict, result: dict) -> list[dict]:
    constraints: list[dict] = []
    tags = case.get("must_not_infer", [])
    if not tags:
        return constraints
    is_media_case = "media_unknown" in case.get("tags", [])
    for tag in tags:
        dim, bound = MUST_NOT_INFER_RULES[tag]
        if dim in CHOICE_DIMS or dim in SCORE_DIMS:
            actual = float(result[dim]["score"])
        else:
            actual = float(result[dim])
        if tag in _MUST_NOT_LOWER_BOUND:
            ok, detail = actual >= bound, f"{dim}={actual:g} 低于下界 {bound:g}"
        else:
            ok, detail = actual <= bound, f"{dim}={actual:g} 高于上界 {bound:g}"
        failure = None
        if not ok:
            if tag.startswith("romantic_"):
                failure = FP_ROMANTIC
            elif tag.startswith("distancing_"):
                failure = FP_DISTANCING
            elif tag == "special_attention_overclaim":
                failure = FP_SPECIAL_ATTENTION
            if is_media_case and failure in (FP_ROMANTIC, FP_SPECIAL_ATTENTION):
                failure = FP_MEDIA_GUESSING
        constraints.append({
            "name": f"no_{tag}", "passed": ok,
            "detail": "" if ok else f"{tag}: {detail}",
            "failure": failure,
        })
    return constraints


def _case_history(case: dict) -> list[dict]:
    """解析案例聊天得到消息历史（fixture 模式下做结构检查；只读 parser）。

    先用默认名解析；若没有任何消息映射到 me/them（例如案例使用虚构昵称
    小柯 / 阿柚），再用案例声明的 me/them 名字重试。解析失败返回 []。
    """
    def _try_parse(chat: str, my, them) -> list[dict] | None:
        try:
            return parse_chat(chat, my_name=my, them_name=them)
        except Exception:
            return None

    history = _try_parse(case["chat"], None, None)
    if history is None:
        return []
    if any(m["speaker"] in ("me", "them") for m in history):
        return history
    for my in case["me"]:
        for them in case["them"]:
            parsed = _try_parse(case["chat"], my, them)
            if parsed and any(m["speaker"] in ("me", "them") for m in parsed):
                return parsed
    return history


def _check_context(case: dict, result: dict) -> list[dict]:
    checks = case.get("context_checks", {})
    if not checks:
        return []
    constraints: list[dict] = []
    context = result.get("conversation_context") if isinstance(result, dict) \
        else None

    if checks.get("no_future_leakage"):
        if context is None:
            constraints.append({
                "name": "context_no_future_leakage", "passed": True,
                "detail": "（未提供 conversation_context，跳过结构性检查）"})
        else:
            history = _case_history(case)
            target = case["target"]
            texts = [m["text"] for m in history]
            target_pos = texts.index(target) if target in texts else len(history)
            past = {(m["speaker"], m["text"]) for m in history[:target_pos]}
            future_or_unknown = [
                c for c in context
                if (c.get("speaker"), c.get("text")) not in past
            ]
            self_reference = any(c.get("text") == target for c in context)
            ok = not future_or_unknown and not self_reference
            detail = ""
            if not ok:
                detail = ("上下文包含目标消息自身" if self_reference
                          else "上下文包含目标消息之前不存在的消息（未来消息泄漏）")
            constraints.append({
                "name": "context_no_future_leakage", "passed": ok,
                "detail": detail, "failure": None if ok else FP_LEAKAGE})

    for text in checks.get("must_contain", []):
        present = context is not None and any(
            c.get("text") == text for c in context)
        constraints.append({
            "name": f"context_must_contain[{text[:12]}]",
            "passed": present,
            "detail": "" if present else f"上下文缺少应包含的内容：{text}",
            "failure": None if present else FP_CONTEXT})
    return constraints


# ---------------------------------------------------------------------------
# case / 汇总评估
# ---------------------------------------------------------------------------


def evaluate_case(case: dict, result: dict | None) -> dict:
    """评估单个 case；返回逐条约束结果与 false-positive 分类。"""
    presence = _check_result_present(case, result)
    constraints: list[dict] = list(presence)
    if result is None or (isinstance(result, dict) and result.get("error")):
        return {
            "case_id": case["id"],
            "category": case["category"],
            "passed": False,
            "constraints": constraints,
            "failures": [c["failure"] for c in constraints if not c["passed"]],
        }
    validate_result(result, f"case {case['id']}")
    constraints += _check_expectations(case, result)
    constraints += _check_must_not_infer(case, result)
    constraints += _check_context(case, result)
    failures = [c["failure"] for c in constraints if not c["passed"]]
    return {
        "case_id": case["id"],
        "category": case["category"],
        "passed": not failures,
        "constraints": constraints,
        "failures": [f for f in failures if f],
    }


def evaluate_cases(cases: list[dict], results: dict) -> dict:
    """评估整个 benchmark；返回聚合统计（确定性：按 case 文件顺序）。"""
    reports = [evaluate_case(case, results.get(case["id"])) for case in cases]
    total_constraints = sum(len(r["constraints"]) for r in reports)
    passed_constraints = sum(
        1 for r in reports for c in r["constraints"] if c["passed"])

    dimension_failures: dict[str, int] = {}
    fp_categories: dict[str, int] = {}
    for report in reports:
        for constraint in report["constraints"]:
            if not constraint["passed"]:
                dimension_failures[constraint["name"]] = \
                    dimension_failures.get(constraint["name"], 0) + 1
        for failure in report["failures"]:
            fp_categories[failure] = fp_categories.get(failure, 0) + 1

    failed_cases = [r["case_id"] for r in reports if not r["passed"]]
    return {
        "total_cases": len(reports),
        "passed_cases": len(reports) - len(failed_cases),
        "failed_cases": failed_cases,
        "total_constraints": total_constraints,
        "passed_constraints": passed_constraints,
        "constraint_pass_rate": (
            passed_constraints / total_constraints if total_constraints else 1.0),
        "dimension_failure_counts": dict(sorted(dimension_failures.items())),
        "false_positive_counts": dict(sorted(fp_categories.items())),
        "cases": reports,
    }


def format_report(aggregate: dict) -> str:
    """人类可读的评估摘要（只含计数与分类，不含任何聊天文本）。"""
    lines = [
        "=== SignalLens benchmark evaluation ===",
        f"cases: {aggregate['passed_cases']}/{aggregate['total_cases']} passed",
        f"constraints: {aggregate['passed_constraints']}/"
        f"{aggregate['total_constraints']} passed "
        f"({aggregate['constraint_pass_rate']:.1%})",
    ]
    if aggregate["failed_cases"]:
        lines.append(f"failed case ids: {', '.join(aggregate['failed_cases'])}")
    if aggregate["dimension_failure_counts"]:
        lines.append("constraint failures by type:")
        for name, count in aggregate["dimension_failure_counts"].items():
            lines.append(f"  - {name}: {count}")
    if aggregate["false_positive_counts"]:
        lines.append("false-positive / failure categories:")
        for name, count in aggregate["false_positive_counts"].items():
            lines.append(f"  - {name}: {count}")
    lines.append("note: benchmark metrics are human-defined regression "
                 "signals, not scientific accuracy.")
    return "\n".join(lines)


def compare_reports(baseline: dict, candidate: dict) -> dict:
    """比较两份评估聚合：哪些失败消失（改善）/ 新增（regression）。

    improvement / regression 只能按“人工定义约束的满足情况”计算，
    绝不把模型输出数值变化本身当作改善。
    """
    def _failures(agg: dict) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for report in agg["cases"]:
            for constraint in report["constraints"]:
                if not constraint["passed"]:
                    key = f"{report['case_id']}::{constraint['name']}"
                    out.setdefault(key, []).append(constraint["detail"])
        return out

    base, cand = _failures(baseline), _failures(candidate)
    resolved = sorted(set(base) - set(cand))
    introduced = sorted(set(cand) - set(base))
    return {
        "baseline_pass_rate": baseline["constraint_pass_rate"],
        "candidate_pass_rate": candidate["constraint_pass_rate"],
        "resolved_failures": resolved,
        "introduced_failures": introduced,
        "regression": bool(introduced),
    }
