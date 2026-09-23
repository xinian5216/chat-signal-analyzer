"""Psychological Evidence v3.2 测试：engagement 语义重定义（纯 mock，不调 Jev）。

验证：
- SCHEMA_VERSION = chat-signal-v3.2，与 v3.1 缓存 key 隔离；
- engagement 的 instructions 覆盖全部六类区分与“高分门槛”条款；
- ENGAGEMENT_LEVELS 等级说明同步更新；
- 其余八个问题（warmth/evidence/emotion/intent/romantic/distancing 等）
  与 v3.1 完全一致；
- 仍 9 问一次 system_one；scoring 接口与 Noul 阈值不变；
- 预登记案例集（原 34 / 修订 34 / 疏离 15 / 修订疏离 15 / Phase 2 10 /
  对照 13）的关键 engagement 预期未被改动。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import analyzer
from analyzer import (
    build_questions,
    build_questions_schema,
)
from parser import parse_chat
from privacy import mask_messages
from scoring import compute_conversation_stats, transform_noul_evidence
from storage import make_cache_key

REPO_ROOT = Path(__file__).resolve().parents[1]

CHAT = """我
2026年07月01日 21:00
最近累不累

TA
2026年07月01日 21:05
有点，早点睡吧"""


class _FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class RecordingClient:
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


# ---------------------------------------------------------------------------
# 1. schema 与版本
# ---------------------------------------------------------------------------


def test_schema_version_is_v32():
    assert analyzer.SCHEMA_VERSION == "chat-signal-v3.2"


def test_v32_cache_key_isolated_from_v31():
    state = analyzer.build_state(
        [{"speaker": "me", "text": "在吗", "time": None}],
        {"speaker": "them", "text": "在", "time": None})
    schema = build_questions_schema()
    key_v32 = make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                             analyzer.SCHEMA_VERSION)
    for old in ("chat-signal-v3.1", "chat-signal-v3.0", "chat-signal-v2.2"):
        assert key_v32 != make_cache_key(state, schema,
                                         analyzer.DEFAULT_MODEL, old)


# ---------------------------------------------------------------------------
# 2. engagement 新语义
# ---------------------------------------------------------------------------


def test_engagement_instructions_cover_all_distinctions():
    text = analyzer.ENGAGEMENT_INSTRUCTIONS
    assert text.startswith("这条 TA 的消息在对话投入程度上处于哪一级？")
    for marker in (
            "实际参与和贡献",          # 核心定义
            "而不是 TA 是否同意当前话题",
            "更不是双方关系的亲密程度",
            "拒绝当前话题但主动追问、开启新话题",
            "暂时忙碌但同时给出具体后续安排",
            "礼貌结束当前交流",
            "单次简短回复",
            "多次缺乏实质参与的敷衍回复",
            "明确拒绝继续交流——低投入",
            "由 distancing_signal 单独判断",   # 与疏离解耦
            "提出后续计划不自动获得高分",
            "才支持较高投入度"):
        assert marker in text, marker
    # build_questions 使用同一文本
    assert build_questions()["engagement"].instructions == text


def test_engagement_levels_updated_consistently():
    levels = analyzer.ENGAGEMENT_LEVELS
    assert len(levels) == 5
    assert "明确拒绝继续对话" in levels[0]
    assert "连续简短应付" in levels[1]
    assert "追问、开启新话题、给出具体后续安排" in levels[3]
    assert "持续主动贡献内容" in levels[4]
    # schema 镜像同步
    assert build_questions_schema()["engagement"]["criteria"] == levels


# ---------------------------------------------------------------------------
# 3. 其余八问零改动（v3.1 状态哨兵）
# ---------------------------------------------------------------------------


def test_other_eight_questions_unchanged_from_v31():
    questions = build_questions()
    assert len(questions) == 9
    # v3.1 的 warmth / evidence / emotion / intent / distancing 文本原样
    for marker in ("礼貌", "情绪支持", "不自动证明亲密或浪漫兴趣",
                   "不要仅因为温暖就把分数推至最高级"):
        assert marker in questions["warmth"].instructions
    for marker in ("不衡量方向", "浪漫拒绝", "不自动代表高关系信息量"):
        assert marker in questions[
            "relationship_evidence_strength"].instructions
    clause = analyzer.AMBIGUITY_RULE_CLAUSE
    assert clause in questions["emotion"].instructions
    assert clause in questions["intent"].instructions
    assert analyzer.DISTANCING_QUESTION == (
        "这条消息是否提供了**关系层面**的疏离信号：对方正在明确减少或结束"
        "持续的互动、回避这段关系本身，或明确拒绝继续保持联系？"
        "只在“对持续互动 / 双方关系的明确疏离”上判 true；"
        "礼貌收尾、计划稍后再聊、一次短回复、当前疲劳或忙碌、"
        "对单个话题的拒绝，以及仅划定浪漫边界（如只想做朋友）都**不是**"
        "关系疏离，除非同时带有明确的减少联系或结束关系的表述"
        "（如“别再找我了”“我们还是别联系了”）。"
    )
    assert analyzer.ROMANTIC_QUESTION == (
        "结合当前消息和前文，这条消息是否提供了超出普通友好或礼貌范围的"
        "具体暧昧、调情或浪漫兴趣信号？"
        "普通礼貌、正常朋友关心、正常聊天不能单独算作浪漫信号。"
    )
    desc = analyzer.INTENT_OPTIONS["distance"]
    for marker in ("话题拒绝", "浪漫边界", "关系疏离", "本项不区分范围"):
        assert marker in desc
    # 选项集合与其余等级表不变
    assert len(analyzer.EMOTION_OPTIONS) == 11
    assert len(analyzer.INTENT_OPTIONS) == 13
    assert len(analyzer.WARMTH_LEVELS) == 5
    assert analyzer.WARMTH_LEVELS[0] == "明显冷淡、疏离或拒绝"
    assert len(analyzer.RELATIONAL_EASE_LEVELS) == 5
    assert len(analyzer.RELATIONSHIP_EVIDENCE_LEVELS) == 5


# ---------------------------------------------------------------------------
# 4. 九问合一 / scoring 接口 / Noul 阈值
# ---------------------------------------------------------------------------


def test_nine_questions_in_one_system_one_call():
    client = RecordingClient()
    messages = mask_messages(parse_chat(CHAT))
    results = analyzer.analyze_messages(client, messages)
    assert client.calls == 1
    assert len(client.questions_seen[0]) == 9
    assert "engagement" in client.questions_seen[0]
    assert len(results) == 1 and "result" in results[0]


def test_scoring_interface_and_noul_thresholds_unchanged():
    assert transform_noul_evidence(0.10) == 0
    assert transform_noul_evidence(0.30) == 0
    assert abs(transform_noul_evidence(0.50) - 0.175) < 1e-9
    assert transform_noul_evidence(0.70) == 0.35
    assert transform_noul_evidence(1.00) == 1
    client = RecordingClient()
    messages = mask_messages(parse_chat(CHAT))
    results = analyzer.analyze_messages(client, messages)
    stats = compute_conversation_stats(results)
    assert stats["analyzed"] == 1
    assert stats["overall_sufficient"] is True
    assert 0.0 <= stats["overall"] <= 100.0


# ---------------------------------------------------------------------------
# 5. 预登记案例集锁定（阈值未被动过）
# ---------------------------------------------------------------------------


def _cases(rel):
    return json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))


def test_preregistered_case_sets_locked():
    main = {c["id"]: c for c in _cases("evaluation/cases.json")}
    revised = {c["id"]: c for c in _cases("evaluation/cases_main34_v3.1.json")}
    dis = {c["id"]: c for c in _cases("evaluation/cases_distancing.json")}
    phase2 = {c["id"]: c
              for c in _cases("evaluation/cases_phase2.json")}
    contrast = {c["id"]: c
                for c in _cases("evaluation/cases_contrast_v3.2.json")}
    assert len(main) == 34 and len(revised) == 34
    assert len(dis) == 15 and len(phase2) == 10 and len(contrast) == 13
    # 对照案例的 engagement 预期（v3.2 语义的回归锚点）
    assert contrast["cs_topic_refusal_redirect"]["expectations"][
        "engagement"]["min"] == 2
    assert contrast["cs_conversation_close_plan"]["expectations"][
        "engagement"]["min"] == 2
    assert contrast["cs_explicit_busy_with_plan"]["expectations"][
        "engagement"]["min"] == 2
    assert contrast["cs_true_contact_refusal"]["expectations"][
        "engagement"]["max"] == 2
    assert contrast["cs_explicit_withdrawal"]["expectations"][
        "engagement"]["max"] == 2
    assert contrast["cs_care_validation"]["expectations"][
        "engagement"]["min"] == 2
    assert contrast["cs_care_understanding_help"]["expectations"][
        "engagement"]["min"] == 3
    # 原 34 例中相关锚点未变
    assert main["j_perfunctory_hmm"]["expectations"]["engagement"]["max"] == 2
    assert main["k_polite_close"]["expectations"]["engagement"]["min"] == 1
    assert main["y_slow_friendly_reply"]["expectations"][
        "engagement"]["min"] == 2
    # 修订集仅差预期，身份字段与锚点一致
    assert revised["y_slow_friendly_reply"]["expectations"][
        "engagement"]["min"] == 2
    # 疏离/Phase2 的 engagement 锚点未变
    assert dis["dis_single_terse"]["expectations"]["engagement"]["min"] == 1
    assert phase2["p2_responsive_support"]["expectations"][
        "engagement"]["min"] == 2


def test_case_files_chat_and_target_untouched():
    """案例身份（聊天/target/双方）在各版本间不得被动过。"""
    main = _cases("evaluation/cases.json")
    revised = _cases("evaluation/cases_main34_v3.1.json")
    assert all(a["chat"] == b["chat"] and a["target"] == b["target"]
               for a, b in zip(main, revised))
    dis = _cases("evaluation/cases_distancing.json")
    dis31 = _cases("evaluation/cases_distancing_v3.1.json")
    assert all(a["chat"] == b["chat"] and a["target"] == b["target"]
               for a, b in zip(dis, dis31))
