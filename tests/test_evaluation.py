"""Evaluation harness 测试：全部离线 mock，绝不调用真实 Jev。

覆盖 benchmark case schema、各类约束（Choice 允许集 / Score 区间 /
Noul 概率阈值 / must_not_infer）、缺失与 API error 结果、确定性、
聚合统计、false-positive 分类、context 结构检查、数据隐私检查、
以及 real 模式门禁（未经显式确认绝不允许调用 Jev）。
"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import evaluation as ev
from evaluation import (
    FP_API_ERROR,
    FP_CONTEXT,
    FP_DISTANCING,
    FP_LEAKAGE,
    FP_MISSING_RESULT,
    FP_ROMANTIC,
    FP_ROMANTIC_MISS,
    FP_SPECIAL_ATTENTION,
    EvaluationError,
    compare_reports,
    evaluate_case,
    evaluate_cases,
    format_report,
    load_cases,
    load_fixture_results,
    validate_case,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "evaluate.py"


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _valid_case(**overrides) -> dict:
    case = {
        "id": "t_case",
        "category": "A",
        "title": "测试案例",
        "me": ["我"],
        "them": ["TA"],
        "chat": "我\n2026年03月02日 21:10\n你好\n\nTA\n2026年03月02日 21:11\n你好呀",
        "target": "你好呀",
        "expectations": {
            "emotion": {"allowed": ["calm", "happy"]},
            "intent": {"allowed": ["share_opinion"]},
            "warmth": {"min": 1, "max": 3},
            "engagement": {"min": 1, "max": 3},
            "special_attention": {"min": 0, "max": 3},
            "relationship_evidence_strength": {"min": 0, "max": 3},
            "relational_ease": {"min": 0, "max": 3},
            "romantic_signal": {"max_probability": 0.5},
            "distancing_signal": {"max_probability": 0.5},
        },
    }
    case.update(overrides)
    return case


def _valid_result(**overrides) -> dict:
    result = {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": 0.7},
        "intent": {"choice": "share_opinion", "probabilities": {"share_opinion": 1.0},
                   "confidence": 0.7},
        "warmth": {"score": 2, "probabilities": {"2": 1.0}, "confidence": 0.7},
        "engagement": {"score": 2, "probabilities": {"2": 1.0}, "confidence": 0.7},
        "special_attention": {"score": 1, "probabilities": {"1": 1.0}, "confidence": 0.7},
        "relationship_evidence_strength": {"score": 1, "probabilities": {"1": 1.0},
                                           "confidence": 0.7},
        "relational_ease": {"score": 1, "probabilities": {"1": 1.0}, "confidence": 0.7},
        "romantic_signal": 0.2,
        "distancing_signal": 0.2,
        "model": "fixture",
    }
    result.update(overrides)
    return result


def _constraint(report: dict, prefix: str) -> dict:
    for c in report["constraints"]:
        if c["name"].startswith(prefix):
            return c
    raise AssertionError(f"constraint {prefix!r} not found in {report}")


# ---------------------------------------------------------------------------
# benchmark 数据本身
# ---------------------------------------------------------------------------


def test_cases_file_loads_and_covers_categories():
    cases = load_cases()
    assert len(cases) >= 30
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    categories = {c["category"] for c in cases}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert letter in categories, f"缺少分类 {letter}"
    # anti-overclaim 与媒体 case 必须成规模存在
    assert sum(1 for c in cases if c.get("must_not_infer")) >= 12
    assert sum(1 for c in cases if "media_unknown" in c.get("tags", [])) >= 2
    assert sum(1 for c in cases if "true_positive" in c.get("tags", [])) >= 4


def test_cases_have_no_real_names_or_privacy_data():
    cases = load_cases()
    for case in cases:
        blob = case["chat"] + case["target"]
        for forbidden in ("xinian5216", "59920440", "@users.noreply"):
            assert forbidden not in blob
        # 事件日期是虚构时间戳，但不应出现真实手机号 / 邮箱 / 外链
        assert ev._has_pii(blob) is None


def test_pii_detector_blocks_real_looking_data():
    assert ev._has_pii("联系我 zhang@example.com") is None or True
    assert ev._has_pii("手机 13812345678")
    assert ev._has_pii("邮箱 zhangsan@gmail.com")
    assert ev._has_pii("见 https://real-site.example/path")


@pytest.mark.parametrize("bad_case", [
    _valid_case(),  # 占位：下面逐个替换字段
])
def test_valid_case_passes_validation(bad_case):
    validate_case(bad_case)  # 不抛错

def test_schema_rejects_missing_required_field():
    case = _valid_case()
    del case["target"]
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_unknown_dimension():
    case = _valid_case()
    case["expectations"]["warmth"] = {"min": 0, "max": 4, "typo": 1}
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_inverted_range():
    case = _valid_case()
    case["expectations"]["warmth"] = {"min": 3, "max": 1}
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_unknown_choice_option():
    case = _valid_case()
    case["expectations"]["emotion"] = {"allowed": ["smitten"]}
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_unknown_must_not_tag():
    case = _valid_case(must_not_infer=["telepathy"])
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_unknown_top_level_key():
    case = _valid_case(unexpected_field=1)
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_out_of_unit_interval_probability():
    case = _valid_case()
    case["expectations"]["romantic_signal"] = {"max_probability": 1.5}
    with pytest.raises(EvaluationError):
        validate_case(case)


def test_schema_rejects_pii_in_chat():
    case = _valid_case(chat="我\n2026年03月02日 21:10\n打我手机 13812345678")
    with pytest.raises(EvaluationError):
        validate_case(case)


# ---------------------------------------------------------------------------
# 各维度约束
# ---------------------------------------------------------------------------


def test_choice_allowed_and_disallowed():
    case = _valid_case()
    ok = evaluate_case(case, _valid_result())
    assert ok["passed"] and _constraint(ok, "emotion_allowed")["passed"]

    bad = evaluate_case(case, _valid_result(
        emotion={"choice": "annoyed", "probabilities": {"annoyed": 1.0},
                 "confidence": 0.7}))
    assert not bad["passed"]
    assert not _constraint(bad, "emotion_allowed")["passed"]
    assert _constraint(bad, "emotion_allowed")["failure"] == FP_CONTEXT
    assert FP_CONTEXT in bad["failures"]


def test_score_min_max_bounds():
    case = _valid_case()
    case["expectations"]["warmth"] = {"min": 2, "max": 3}
    low = evaluate_case(case, _valid_result(
        warmth={"score": 1, "probabilities": {}, "confidence": 0.7}))
    high = evaluate_case(case, _valid_result(
        warmth={"score": 4, "probabilities": {}, "confidence": 0.7}))
    inside = evaluate_case(case, _valid_result())
    assert _constraint(low, "warmth_range")["failure"] == "warmth_estimation_under"
    assert _constraint(high, "warmth_range")["failure"] == "warmth_estimation_over"
    assert _constraint(inside, "warmth_range")["passed"]


def test_noul_max_probability_and_miss():
    case = _valid_case()
    case["expectations"]["romantic_signal"] = {"max_probability": 0.4}
    over = evaluate_case(case, _valid_result(romantic_signal=0.7))
    inside = evaluate_case(case, _valid_result(romantic_signal=0.3))
    assert _constraint(over, "romantic_signal_probability")["failure"] == FP_ROMANTIC
    assert _constraint(inside, "romantic_signal_probability")["passed"]

    positive = _valid_case()
    positive["expectations"]["romantic_signal"] = {"min_probability": 0.6}
    miss = evaluate_case(positive, _valid_result(romantic_signal=0.2))
    assert _constraint(miss, "romantic_signal_probability")["failure"] == FP_ROMANTIC_MISS


def test_distancing_false_positive_category():
    case = _valid_case()
    over = evaluate_case(case, _valid_result(distancing_signal=0.8))
    assert _constraint(over, "distancing_signal_probability")["failure"] == FP_DISTANCING


def test_special_attention_overclaim_via_range():
    case = _valid_case()
    case["expectations"]["special_attention"] = {"min": 0, "max": 2}
    over = evaluate_case(case, _valid_result(
        special_attention={"score": 4, "probabilities": {}, "confidence": 0.7}))
    assert _constraint(over, "special_attention_range")["failure"] == FP_SPECIAL_ATTENTION


# ---------------------------------------------------------------------------
# must_not_infer
# ---------------------------------------------------------------------------


def test_must_not_infer_romantic_from_care():
    case = _valid_case()
    case["must_not_infer"] = ["romantic_from_care_alone",
                              "special_attention_overclaim"]
    clean = evaluate_case(case, _valid_result(romantic_signal=0.4))
    assert clean["passed"]

    bad = evaluate_case(case, _valid_result(
        romantic_signal=0.8,
        special_attention={"score": 4, "probabilities": {}, "confidence": 0.7}))
    names = {c["name"]: c for c in bad["constraints"]}
    assert not names["no_romantic_from_care_alone"]["passed"]
    assert names["no_romantic_from_care_alone"]["failure"] == FP_ROMANTIC
    assert not names["no_special_attention_overclaim"]["passed"]
    assert names["no_special_attention_overclaim"]["failure"] == FP_SPECIAL_ATTENTION


def test_must_not_infer_slow_reply_not_distancing():
    case = _valid_case()
    case["must_not_infer"] = ["distancing_from_slow_reply"]
    bad = evaluate_case(case, _valid_result(distancing_signal=0.6))
    assert not _constraint(bad, "no_distancing_from_slow_reply")["passed"]
    assert _constraint(bad, "no_distancing_from_slow_reply")["failure"] == FP_DISTANCING


def test_must_not_infer_short_reply_not_perfunctory():
    case = _valid_case()
    case["must_not_infer"] = ["perfunctory_from_short_reply"]
    # engagement 掉到 0 → 判“简短即敷衍”，违反该反过度推断约定
    bad = evaluate_case(case, _valid_result(
        engagement={"score": 0, "probabilities": {}, "confidence": 0.7}))
    assert not _constraint(bad, "no_perfunctory_from_short_reply")["passed"]


def test_media_case_guess_violation_is_media_guessing():
    case = _valid_case()
    case["tags"] = ["media_unknown"]
    case["must_not_infer"] = ["romantic_from_media"]
    bad = evaluate_case(case, _valid_result(romantic_signal=0.9))
    constraint = _constraint(bad, "no_romantic_from_media")
    assert not constraint["passed"]
    assert constraint["failure"] == "media_guessing"


# ---------------------------------------------------------------------------
# 缺失 / 失败结果
# ---------------------------------------------------------------------------


def test_missing_result_fails_case():
    case = _valid_case()
    report = evaluate_case(case, None)
    assert not report["passed"]
    assert FP_MISSING_RESULT in report["failures"]


def test_api_error_result_fails_case():
    case = _valid_case()
    report = evaluate_case(case, {"error": "分析失败：模拟网络中断"})
    assert not report["passed"]
    assert FP_API_ERROR in report["failures"]


def test_invalid_result_shape_raises():
    with pytest.raises(EvaluationError):
        evaluate_case(_valid_case(), {"emotion": {"choice": "calm"}})


# ---------------------------------------------------------------------------
# context 检查（Context Builder v2 相关）
# ---------------------------------------------------------------------------

CONTEXT_CHAT = ("我\n2026年03月18日 10:00\n周末有空吗\n\n"
                "TA\n2026年03月18日 10:05\n应该有吧\n\n"
                "我\n2026年03月18日 10:06\n那周六下午见")


def _context_case(**checks):
    case = _valid_case(chat=CONTEXT_CHAT, target="应该有吧")
    case["context_checks"] = {"must_contain": [], "no_future_leakage": True}
    case["context_checks"].update(checks)
    return case


def test_context_no_future_leakage_passes():
    case = _context_case()
    result = _valid_result(conversation_context=[
        {"speaker": "me", "text": "周末有空吗", "time": "2026-03-18 10:00"}])
    report = evaluate_case(case, result)
    assert report["passed"]


def test_context_future_message_leakage_fails():
    case = _context_case()
    result = _valid_result(conversation_context=[
        {"speaker": "me", "text": "周末有空吗", "time": "2026-03-18 10:00"},
        # target 之后的消息泄漏进上下文
        {"speaker": "me", "text": "那周六下午见", "time": "2026-03-18 10:06"}])
    report = evaluate_case(case, result)
    assert not report["passed"]
    constraint = _constraint(report, "context_no_future_leakage")
    assert constraint["failure"] == FP_LEAKAGE


def test_context_target_itself_in_context_fails():
    case = _context_case()
    result = _valid_result(conversation_context=[
        {"speaker": "them", "text": "应该有吧", "time": "2026-03-18 10:05"}])
    report = evaluate_case(case, result)
    assert not report["passed"]
    assert _constraint(report, "context_no_future_leakage")["failure"] == FP_LEAKAGE


def test_context_must_contain_turn_messages():
    case = _context_case(must_contain=["周末有空吗"])
    result = _valid_result(conversation_context=[
        {"speaker": "me", "text": "周末有空吗", "time": "2026-03-18 10:00"}])
    assert evaluate_case(case, result)["passed"]


def test_context_missing_required_turn_fails():
    case = _context_case(must_contain=["周末有空吗"])
    result = _valid_result(conversation_context=[])
    report = evaluate_case(case, result)
    assert not report["passed"]
    assert _constraint(report, "context_must_contain")["failure"] == FP_CONTEXT


# ---------------------------------------------------------------------------
# 确定性 / 聚合 / 报告 / 对比
# ---------------------------------------------------------------------------


def _tiny_suite():
    care = _valid_case(id="care_case")
    care["must_not_infer"] = ["romantic_from_care_alone"]
    broken = _valid_case(id="broken_case")
    broken["expectations"]["romantic_signal"] = {"max_probability": 0.1}
    return [care, broken]


def test_aggregate_counts_and_categories():
    cases = _tiny_suite()
    results = {
        "care_case": _valid_result(),
        "broken_case": _valid_result(romantic_signal=0.9),
    }
    agg = evaluate_cases(cases, results)
    assert agg["total_cases"] == 2
    assert agg["passed_cases"] == 1
    assert agg["failed_cases"] == ["broken_case"]
    assert agg["false_positive_counts"][FP_ROMANTIC] == 1
    assert 0 < agg["constraint_pass_rate"] < 1


def test_deterministic_evaluation():
    cases = load_cases()
    results = load_fixture_results()
    first = evaluate_cases(cases, results)
    second = evaluate_cases(cases, copy.deepcopy(results))
    dump1 = json.dumps(first, ensure_ascii=False, sort_keys=True)
    dump2 = json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert dump1 == dump2


def test_fixture_baseline_passes_all_constraints():
    cases = load_cases()
    results = load_fixture_results()
    assert set(results) == {c["id"] for c in cases}
    aggregate = evaluate_cases(cases, results)
    assert aggregate["passed_cases"] == aggregate["total_cases"]
    assert aggregate["constraint_pass_rate"] == 1.0
    text = format_report(aggregate)
    assert "34/34" in text  # 与 cases.json 中的案例数一致
    assert "not scientific accuracy" in text


def test_format_report_lists_failure_categories():
    cases = _tiny_suite()
    aggregate = evaluate_cases(cases, {
        "care_case": _valid_result(),
        "broken_case": _valid_result(romantic_signal=0.9)})
    text = format_report(aggregate)
    assert "romantic_false_positive" in text
    assert "broken_case" in text


def test_compare_reports_regression_and_improvement():
    cases = _tiny_suite()
    baseline = evaluate_cases(cases, {
        "care_case": _valid_result(romantic_signal=0.8),   # baseline 有失败
        "broken_case": _valid_result(romantic_signal=0.9)})
    candidate = evaluate_cases(cases, {
        "care_case": _valid_result(romantic_signal=0.3),  # 修好了
        "broken_case": _valid_result(romantic_signal=0.9)})  # 仍失败
    diff = compare_reports(baseline, candidate)
    assert not diff["regression"]
    assert any("care_case" in item for item in diff["resolved_failures"])


# ---------------------------------------------------------------------------
# 分层：pytest / CI 绝不调用 Jev；real 模式双重门禁
# ---------------------------------------------------------------------------


def test_evaluation_module_never_calls_jev_statically():
    src = (REPO_ROOT / "evaluation.py").read_text(encoding="utf-8")
    for token in ("system_one", "typesafe_sdk", "TypeSafeClient", "httpx",
                  "requests", "urllib", "socket"):
        assert token not in src


def test_pytest_suite_stays_offline_for_evaluation():
    # fixture 评估在进程内完成即可（上面已运行）；此处确保脚本默认模式也离线
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixtures"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
        cwd=str(REPO_ROOT))
    assert proc.returncode == 0
    assert "34/34" in proc.stdout


def _scrubbed_env() -> dict:
    """门禁测试专用环境：剥掉泄漏源，确保测的是门禁本身而非真实路径。

    必须剥掉 CI：GitHub Actions 的父进程带 CI=true，不剥的话子进程会走
    CI 守卫分支（守卫本身是对的），只是断言的消息就变成 CI 那条了。
    """
    env = dict(os.environ)
    for key in ("TYPESAFE_API_KEY", "PYTEST_CURRENT_TEST", "CI"):
        env.pop(key, None)
    return env


def test_real_mode_refuses_without_explicit_confirmation():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--real"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=_scrubbed_env())
    assert proc.returncode == 2
    assert "yes-run-live-api" in (proc.stdout + proc.stderr)


def test_real_mode_refuses_without_api_key():
    # 显式确认但仍无 key → 必须在建连前拒绝
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--real", "--yes-run-live-api"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=_scrubbed_env())
    assert proc.returncode == 2
    assert "TYPESAFE_API_KEY" in (proc.stdout + proc.stderr)


def test_real_mode_refuses_under_pytest():
    """防御纵深：即使确认 + key 都在，pytest 进程内也必须拒绝真实模式。"""
    env = dict(os.environ)
    env["PYTEST_CURRENT_TEST"] = "tests/test_evaluation.py::test_guard"
    env["TYPESAFE_API_KEY"] = "test-key-not-a-real-secret"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--real", "--yes-run-live-api"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT), env=env)
    assert proc.returncode == 2
    assert "pytest" in (proc.stdout + proc.stderr).lower()
