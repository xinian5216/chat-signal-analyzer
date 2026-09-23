"""Psychological Evidence v3 Phase 1：distancing 语义修正的回归测试。

全部 mock，绝不调用真实 Jev。覆盖：

- SCHEMA_VERSION = chat-signal-v3.0，与 v2.2 缓存 key 自然隔离；
- distancing_signal 的问题语义修正（五分类：conversation closing /
  临时状态 / 话题拒绝 / 浪漫边界 / 关系疏离），其余 8 问不变；
- 9 个问题仍在同一次 system_one 请求中；
- scoring 接口与 Noul 转换阈值不变；
- 原 34 案例中被复核标记争议的阈值未被偷偷修改；
- 新增独立疏离案例集（cases_distancing.json）的校验、真阳性数量、
  pre-registered 预期锁定，以及 CLI fixture 冒烟；
- evaluator 提示文本修正（emotion=/intent= 按维度输出）。
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import analyzer
import pytest
from analyzer import (
    DISTANCING_QUESTION,
    build_questions,
    build_questions_schema,
)
from parser import parse_chat
from privacy import mask_messages
from scoring import compute_conversation_stats, transform_noul_evidence
from storage import make_cache_key

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "scripts" / "evaluate.py"
DISTANCING_CASES = REPO_ROOT / "evaluation" / "cases_distancing.json"
DISTANCING_FIXTURES = (REPO_ROOT / "evaluation" / "fixtures"
                       / "baseline_v3_distancing.json")

# v2.2 时代的 distancing 题目文本（语义修正前）——用于证明“只改了这一问”。
DISTANCING_QUESTION_V22 = (
    "这条消息是否提供了对方正在结束交流、回避互动、降低投入"
    "或刻意拉开距离的信号？"
)


class _FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class RecordingClient:
    """记录每次 system_one 调用的 state 与 questions。"""

    def __init__(self):
        self.calls = 0
        self.states = []
        self.questions_seen = []

    def system_one(self, state, questions):
        self.calls += 1
        self.states.append(state)
        self.questions_seen.append(list(questions))
        answers = {
            "emotion": _FakeAnswer(choice="calm",
                                   probabilities={"calm": 1.0},
                                   confidence=0.9),
            "intent": _FakeAnswer(choice="other",
                                  probabilities={"other": 1.0},
                                  confidence=0.9),
            "warmth": _FakeAnswer(score=2.0, probabilities={},
                                  confidence=0.9),
            "engagement": _FakeAnswer(score=2.0, probabilities={},
                                      confidence=0.9),
            "special_attention": _FakeAnswer(score=1.0, probabilities={},
                                             confidence=0.9),
            "relationship_evidence_strength": _FakeAnswer(
                score=3.5, probabilities={}, confidence=0.9),
            "relational_ease": _FakeAnswer(score=2.0, probabilities={},
                                           confidence=0.9),
            "romantic_signal": _FakeAnswer(noul=0.1),
            "distancing_signal": _FakeAnswer(noul=0.1),
        }
        return SimpleNamespace(answers=answers, model="fake")


CHAT = """我
2026年04月01日 21:00
最近累不累

TA
2026年04月01日 21:05
有点，早点睡吧"""


# ---------------------------------------------------------------------------
# 1. schema / 缓存隔离
# ---------------------------------------------------------------------------


def test_schema_version_is_v30():
    assert analyzer.SCHEMA_VERSION == "chat-signal-v3.0"


def test_v30_cache_key_isolated_from_v22():
    state = analyzer.build_state(
        [{"speaker": "me", "text": "在吗", "time": None}],
        {"speaker": "them", "text": "在", "time": None})
    schema = build_questions_schema()
    key_v30 = make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                             analyzer.SCHEMA_VERSION)
    key_v22 = make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                             "chat-signal-v2.2")
    assert key_v30 != key_v22


# ---------------------------------------------------------------------------
# 2. distancing 语义修正（只改这一问）
# ---------------------------------------------------------------------------


def test_distancing_question_targets_relationship_withdrawal():
    assert DISTANCING_QUESTION != DISTANCING_QUESTION_V22
    for marker in ("关系", "持续的互动", "礼貌收尾", "浪漫边界", "疲劳"):
        assert marker in DISTANCING_QUESTION
    # 明确排除的会话层行为
    for marker in ("稍后再聊", "一次短回复", "单个话题"):
        assert marker in DISTANCING_QUESTION


def test_distancing_criteria_cover_all_five_buckets():
    questions = build_questions()
    question = questions["distancing_signal"]
    criteria = question.criteria
    assert set(criteria) == {"true", "false"}
    # true：持续互动/关系层面的明确疏离
    assert "明确拒绝继续保持联系" in criteria["true"]
    # false：四类非关系疏离（收尾/推迟/短回复/疲劳/话题拒绝/浪漫边界）
    for marker in ("礼貌收尾", "稍后再聊", "一次短回复", "疲劳", "浪漫边界"):
        assert marker in criteria["false"]
    # instructions 与 criteria 一致地指向关系层
    assert "别联系" in criteria["true"]


def test_other_eight_questions_unchanged():
    questions = build_questions()
    assert len(questions) == 9
    assert set(questions) == {
        "emotion", "intent", "warmth", "engagement", "special_attention",
        "relationship_evidence_strength", "relational_ease",
        "romantic_signal", "distancing_signal"}
    # 其余问题常量未被触碰（哨兵值）
    assert analyzer.ROMANTIC_QUESTION == (
        "结合当前消息和前文，这条消息是否提供了超出普通友好或礼貌范围的"
        "具体暧昧、调情或浪漫兴趣信号？"
        "普通礼貌、正常朋友关心、正常聊天不能单独算作浪漫信号。"
    )
    assert len(analyzer.EMOTION_OPTIONS) == 11
    assert len(analyzer.INTENT_OPTIONS) == 13
    assert analyzer.WARMTH_LEVELS[0] == "明显冷淡、疏离或拒绝"
    assert len(analyzer.RELATIONAL_EASE_LEVELS) == 5
    # schema 镜像形状不变（distancing 仍为 noul）
    schema = build_questions_schema()
    assert schema["distancing_signal"] == {"type": "noul"}
    assert schema["romantic_signal"] == {"type": "noul"}
    assert len(schema) == 9


# ---------------------------------------------------------------------------
# 3. 九问仍在一次 system_one 请求中；scoring 接口兼容
# ---------------------------------------------------------------------------


def test_nine_questions_in_one_system_one_call():
    client = RecordingClient()
    messages = mask_messages(parse_chat(CHAT))
    results = analyzer.analyze_messages(client, messages)
    assert client.calls == 1                          # 一个 target 一次请求
    assert len(client.questions_seen[0]) == 9         # 9 问同请求
    assert "distancing_signal" in client.questions_seen[0]
    assert len(results) == 1 and "result" in results[0]


def test_scoring_interface_and_noul_thresholds_unchanged():
    # Noul 转换阈值未动
    assert transform_noul_evidence(0.10) == 0
    assert transform_noul_evidence(0.30) == 0
    assert abs(transform_noul_evidence(0.50) - 0.175) < 1e-9
    assert transform_noul_evidence(0.70) == 0.35
    assert transform_noul_evidence(1.00) == 1
    # compute_conversation_stats 仍消费同样的 results 形状
    client = RecordingClient()
    messages = mask_messages(parse_chat(CHAT))
    results = analyzer.analyze_messages(client, messages)
    stats = compute_conversation_stats(results)
    assert stats["analyzed"] == 1
    assert stats["overall_sufficient"] is True
    assert 0.0 <= stats["overall"] <= 100.0   # overall 是 base_score*100 量表


# ---------------------------------------------------------------------------
# 4. 复核记录：争议案例的阈值未被偷偷修改
# ---------------------------------------------------------------------------


def _case(case_id):
    cases = json.loads((REPO_ROOT / "evaluation" / "cases.json")
                       .read_text(encoding="utf-8"))
    return next(c for c in cases if c["id"] == case_id)


def test_disputed_case_thresholds_locked():
    l = _case("l_friendzone")
    assert l["expectations"]["distancing_signal"]["min_probability"] == 0.5
    d = _case("d_tease_no_flirt")
    assert d["expectations"]["warmth"]["min"] == 1
    assert d["expectations"]["relationship_evidence_strength"]["max"] == 2
    g = _case("g_remembers_detail")
    assert g["expectations"]["special_attention"]["min"] == 3
    p = _case("p_ease_high_romantic_low")
    assert p["expectations"]["relational_ease"] == {"min": 4, "max": 4}
    q = _case("q_warm_high_evidence_low")
    assert q["expectations"]["warmth"]["min"] == 3


# ---------------------------------------------------------------------------
# 5. 独立疏离案例集（pre-registered）
# ---------------------------------------------------------------------------


def test_distancing_case_set_is_independent_and_preregistered():
    cases = json.loads(DISTANCING_CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 12
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) and all(i.startswith("dis_")
                                             for i in ids)
    # 真阳性（关系疏离）至少 3 个，且阈值预先固定为 >=0.6
    positives = [c for c in cases
                 if c["expectations"]["distancing_signal"]
                 .get("min_probability", 0) >= 0.6]
    assert len(positives) >= 3
    # 反过度推断（会话层行为）必须占多数，且全部有 distance 上限
    negatives = [c for c in cases
                 if c["expectations"]["distancing_signal"]
                 .get("max_probability", 1.0) <= 0.5]
    assert len(negatives) >= 9
    # 每个 case 都必须有 distancing 约束
    assert all("distancing_signal" in c["expectations"] for c in cases)
    # 浪漫边界 / 媒体 / 慢回复等反读心 case 必须在场
    joined = json.dumps(cases, ensure_ascii=False)
    for marker in ("当朋友", "[语音] 7", "刚忙完", "在忙"):
        assert marker in joined


def test_distancing_fixtures_all_pass():
    import evaluation as ev

    cases = ev.load_cases(DISTANCING_CASES)
    results = ev.load_fixture_results(DISTANCING_FIXTURES)
    assert set(results) == {c["id"] for c in cases}
    aggregate = ev.evaluate_cases(cases, results)
    assert aggregate["passed_cases"] == aggregate["total_cases"]
    assert aggregate["constraint_pass_rate"] == 1.0


def test_distancing_fixture_cli_smoke_offline():
    proc = subprocess.run(
        [sys.executable, str(CLI), "--fixtures",
         "--cases", "evaluation/cases_distancing.json",
         "--fixture-file", "evaluation/fixtures/baseline_v3_distancing.json"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
        cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    assert "15/15" in proc.stdout


# ---------------------------------------------------------------------------
# 6. evaluator 提示文本修正
# ---------------------------------------------------------------------------


def test_expectation_detail_labels_use_dimension_name():
    import evaluation as ev

    case = {
        "id": "t", "category": "A", "title": "t", "me": ["我"], "them": ["TA"],
        "chat": "我\n2026年04月01日 21:00\n你好\n\nTA\n2026年04月01日 21:01\n在的",
        "target": "在的",
        "expectations": {
            "emotion": {"allowed": ["calm"]},
            "intent": {"allowed": ["explain"]},
            "warmth": {"min": 1, "max": 3},
            "engagement": {"min": 1, "max": 3},
            "special_attention": {"min": 0, "max": 2},
            "relationship_evidence_strength": {"min": 0, "max": 2},
            "relational_ease": {"min": 0, "max": 2},
            "romantic_signal": {"max_probability": 0.5},
            "distancing_signal": {"max_probability": 0.5},
        },
    }
    result = {
        "emotion": {"choice": "annoyed", "probabilities": {}, "confidence": 0.5},
        "intent": {"choice": "other", "probabilities": {}, "confidence": 0.5},
        "warmth": {"score": 2, "probabilities": {}, "confidence": 0.5},
        "engagement": {"score": 2, "probabilities": {}, "confidence": 0.5},
        "special_attention": {"score": 1, "probabilities": {}, "confidence": 0.5},
        "relationship_evidence_strength": {"score": 1, "probabilities": {},
                                           "confidence": 0.5},
        "relational_ease": {"score": 1, "probabilities": {}, "confidence": 0.5},
        "romantic_signal": 0.1,
        "distancing_signal": 0.1,
    }
    report = ev.evaluate_case(case, result)
    details = {c["name"]: c["detail"] for c in report["constraints"]}
    assert details["emotion_allowed"].startswith("emotion=")
    assert details["intent_allowed"].startswith("intent=")
    assert "emotion/intent=" not in (details["emotion_allowed"]
                                     + details["intent_allowed"])
