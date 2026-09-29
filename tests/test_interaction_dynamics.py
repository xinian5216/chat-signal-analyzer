"""Issue #20 测试：Interaction Dynamics（事件 schema / prefix invariance /
身份去重 / 六类事件 / 假阳性闸门 / behavior 适配 / Profile 集成 / 报告）。
全部离线、确定性、虚构数据；真实聊天与 Jev API 均不接触。"""

import json
from pathlib import Path

import pytest

import behavior as bv
import interaction_dynamics as idyn
import interaction_diagnostics as idiag
import relationship_profile as rp
import salience as sal
from interaction_dynamics import build_interaction_events
from relationship_profile import build_profile

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = REPO_ROOT / "evaluation" / "interaction_scenarios_v0.4.json"
_SCENARIOS = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))["cases"]

# 常见虚构消息模板
_T = "2026-03-02 10:0"


def make_entries(specs):
    return [{"index": i, "speaker": "them", "time": f"{_T}{i}" if i < 10 else None,
             "text": f"消息{i}", "result": r}
            for i, r in enumerate(specs)]


def make_result(intent="continue_topic", probs=None, warmth=2.0, engagement=2.0,
                special=1.5, evidence=2.0, ease=2.0, romantic=0.1,
                distancing=0.05, emotion="calm", emotion_probs=None):
    def dist(s):
        lo, hi = int(s // 1), int(-(-s // 1))
        out = {str(i): 0.0 for i in range(5)}
        if lo == hi:
            out[str(lo)] = 1.0
        else:
            out[str(lo)] = 1.0 - (s - lo)
            out[str(hi)] = s - lo
        return out

    def block(s):
        return {"score": s, "probabilities": dist(s), "confidence": 0.75}

    return {
        "emotion": {"choice": emotion,
                    "probabilities": emotion_probs or {emotion: 1.0},
                    "confidence": 0.75},
        "intent": {"choice": intent,
                   "probabilities": probs or {intent: 1.0},
                   "confidence": 0.75},
        "warmth": block(warmth),
        "engagement": block(engagement),
        "special_attention": block(special),
        "relationship_evidence_strength": block(evidence),
        "relational_ease": block(ease),
        "romantic_signal": romantic,
        "distancing_signal": distancing,
        "model": "fake",
    }


def msg(speaker, text, time="2026-03-02 10:00", **kw):
    out = {"speaker": speaker, "text": text, "time": time, "raw_speaker":
           "我" if speaker == "me" else "TA",
           "content_type": kw.pop("content_type", "text"),
           "media_kinds": kw.pop("media_kinds", [])}
    out.update(kw)
    return out


ASK = {"intent": "ask_information", "probs": {"ask_information": 0.9,
                                              "other": 0.1}}
INVITE = {"intent": "invite", "probs": {"invite": 0.85, "other": 0.15}}
CARE_ASK = {"intent": "ask_information",
            "probs": {"ask_information": 0.85, "show_care": 0.1},
            "emotion": "caring", "emotion_probs": {"caring": 0.8, "calm": 0.2}}


def build(interaction):
    return interaction


def pipeline(messages, results):
    interaction = build_interaction_events(messages, results)
    salience_out = sal.build_salience(results, interaction=interaction)
    profile = build_profile(results, salience=salience_out,
                            interaction=interaction)
    return interaction, salience_out, profile


BANNED_PSYCHOLOGICAL = (
    "在乎", "喜欢你", "想你", "很尊重你", "不尊重你这个人",
    "想念", "喜欢概率", "恋爱可能性", "关系健康", "好感增加", "关系变好",
    "closeness", "positive event", "正向事件",
    # 断言式人格标签（"不推断人格"类否定句是应有纪律，不禁）
    "TA 是一个", "对方是一个", "人格类型",
)


def _all_text(interaction, profile):
    chunks = [interaction["baseline_summary"] if "baseline_summary" in interaction
              else ""]
    chunks += interaction.get("summary_lines") or []
    chunks += json.dumps(interaction.get("observations", {}), ensure_ascii=False)
    for e in interaction.get("events") or []:
        chunks += [e["reason"], e["alternative_explanation"]]
        chunks += e.get("limitations") or []
    chunks.append(profile["summary"]["text"])
    chunks += [d["conclusion"] for d in profile["dimensions"].values()]
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 场景文件（I1~I24 + prefix invariance）
# ---------------------------------------------------------------------------


class TestScenarios:
    @pytest.mark.parametrize("case", _SCENARIOS,
                             ids=[c["id"] for c in _SCENARIOS])
    def test_scenario_expectations(self, case):
        report = idiag.evaluate_case(case)
        failed = [c for c in report["checks"] if not c["passed"]]
        assert report["passed"], failed

    def test_scenario_file_total(self):
        assert len(_SCENARIOS) == 24


# ---------------------------------------------------------------------------
# 事件 schema
# ---------------------------------------------------------------------------

REQUIRED_EVENT_FIELDS = {
    "event_type", "source", "dimension", "direction", "actor", "window",
    "time_basis", "trigger", "strength", "review_status", "identity",
    "fingerprints", "reason", "alternative_explanation", "limitations",
    "capability",
}


class TestEventSchema:
    def test_event_fields_complete(self):
        messages = [
            msg("me", "最近胃不舒服"),
            msg("them", "怎么了？", "2026-03-02 10:01"),
            msg("me", "可能吃坏了", "2026-03-02 10:02"),
            msg("them", "现在还疼吗？", "2026-03-02 10:03"),
        ]
        results = [{"index": 1, "result": make_result(**ASK)},
                   {"index": 3, "result": make_result(**ASK)}]
        interaction, _, _ = pipeline(messages, results)
        assert interaction["events"]
        for e in interaction["events"]:
            assert REQUIRED_EVENT_FIELDS <= set(e), e["event_type"]
            assert e["review_status"] in (idyn.REVIEW_AUTO,
                                          idyn.REVIEW_CANDIDATE,
                                          idyn.REVIEW_DEFERRED)
            window = e["window"]
            assert window["start_index"] <= window["end_index"]
            assert set(window["anchor_indices"]) <= set(
                range(window["start_index"], window["end_index"] + 1))

    def test_no_psychological_wording_anywhere(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I2_followup_sequence"))
        interaction, _, profile = pipeline(messages, results)
        text = _all_text(interaction, profile)
        for banned in BANNED_PSYCHOLOGICAL:
            assert banned not in text, banned

    def test_no_global_valence_fields(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I11_refusal_then_pressure"))
        interaction, _, _ = pipeline(messages, results)
        for e in interaction["events"]:
            for field in ("valence", "boost", "positive", "negative", "score_impact"):
                assert field not in e, field


# ---------------------------------------------------------------------------
# prefix invariance（未来泄漏）
# ---------------------------------------------------------------------------


def assert_prefix_invariant(messages, results, checkpoints=None):
    """第 n 条之前已知的事件 == 只跑前 n 条时的事件（身份比较）。"""
    full = build_interaction_events(messages, results)
    checkpoints = checkpoints or sorted(
        {max(1, len(messages) // 2), max(1, len(messages) - 1)})
    for n in checkpoints:
        if n >= len(messages):
            continue
        prefix = build_interaction_events(
            messages[:n], [{**e, "index": e["index"]} for e in results
                           if e["index"] < n])
        known = {e["identity"] for e in full["events"]
                 if e["window"]["end_index"] < n}
        actual = {e["identity"] for e in prefix["events"]}
        assert known == actual, f"prefix {n}: missing={known - actual} extra={actual - known}"


class TestPrefixInvariance:
    @pytest.mark.parametrize("case", _SCENARIOS,
                             ids=[c["id"] for c in _SCENARIOS])
    def test_prefix_invariance_all_scenarios(self, case):
        messages, results = idiag.materialize_case(case)
        assert_prefix_invariant(messages, results)

    def test_future_recall_not_visible_early(self):
        case = next(c for c in _SCENARIOS if c["id"] == "I20_future_leakage_trap")
        messages, results = idiag.materialize_case(case)
        early = build_interaction_events(
            messages[:5], [e for e in results if e["index"] < 5])
        assert not any(e["event_type"] == "personal_recall_candidate"
                       for e in early["events"])
        full = build_interaction_events(messages, results)
        assert any(e["event_type"] == "personal_recall_candidate"
                   for e in full["events"])


# ---------------------------------------------------------------------------
# 事件身份 / 去重
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_duplicate_import_same_identity(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I21_duplicate_reimport_stable_identity"))
        first = build_interaction_events(messages, results)
        again = build_interaction_events(messages, results)
        assert [e["identity"] for e in first["events"]] == \
            [e["identity"] for e in again["events"]]

    def test_prepend_keeps_identity(self):
        case = next(c for c in _SCENARIOS
                    if c["id"] == "I22_prepend_older_chunk_stable_identity")
        messages, results = idiag.materialize_case(case)
        full = build_interaction_events(messages, results)
        kept = [e for e in results if e["index"] >= 5]
        base = build_interaction_events(
            messages[5:], [{**e, "index": e["index"] - 5} for e in kept])
        ids_full = sorted(e["identity"] for e in full["events"]
                          if e["event_type"] == "followup_sequence")
        ids_base = sorted(e["identity"] for e in base["events"]
                          if e["event_type"] == "followup_sequence")
        assert ids_full == ids_base and ids_full

    def test_adapter_identity_dedups_against_reviewed_events(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I2_followup_sequence"))
        interaction = build_interaction_events(messages, results)
        candidates = idyn.interaction_behavior_candidates(interaction, messages)
        assert candidates
        confirmed = bv.EventCandidate(
            dimension="initiative", behavior_type="topic_continuation",
            start=candidates[0].start, end=candidates[0].end,
            fingerprints=list(candidates[0].fingerprints), source_kind="rule",
            rule="interaction:followup_sequence",
            event_start_time=None, event_end_time=None,
            time_confidence="unknown")
        event = bv.build_event_dict(candidate=confirmed, friend_id="f1",
                                    dimension="initiative",
                                    behavior_type="topic_continuation",
                                    stance="supporting", status="confirmed")
        pending = bv.pending_candidates(candidates, [event])
        assert pending == []  # 已确认 → 不重复出现


# ---------------------------------------------------------------------------
# Conversation re-engagement
# ---------------------------------------------------------------------------


class TestReengagement:
    def test_ta_restart_maps_initiative(self):
        messages = [
            msg("me", "今天先聊到这", "2026-03-01 20:00"),
            msg("them", "好，辛苦了", "2026-03-01 20:01"),
            msg("them", "那个文件发你了", "2026-03-03 09:00"),
        ]
        interaction, salience_out, profile = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "conversation_reengagement"]
        assert len(events) == 1 and events[0]["actor"] == "TA"
        assert len(profile["dimensions"]["initiative_engagement"]
                   ["salient_events"]) == 1

    def test_user_restart_is_observation_only(self):
        messages = [
            msg("me", "先这样", "2026-03-01 20:00"),
            msg("them", "好", "2026-03-01 20:01"),
            msg("me", "早啊", "2026-03-03 09:00"),
        ]
        interaction, _, profile = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "conversation_reengagement"]
        assert len(events) == 1 and events[0]["direction"] == "observation"
        assert profile["dimensions"]["initiative_engagement"]["salient_events"] == []

    def test_gap_threshold_boundary(self):
        under = [msg("me", "在吗", "2026-03-01 20:00"),
                 msg("them", "在", "2026-03-01 22:58")]     # 2h58m
        over = [msg("me", "在吗", "2026-03-01 20:00"),
                msg("them", "在", "2026-03-01 23:02")]      # 3h02m
        assert not any(e["event_type"] == "conversation_reengagement"
                       for e in build_interaction_events(under, [])["events"])
        assert any(e["event_type"] == "conversation_reengagement"
                   for e in build_interaction_events(over, [])["events"])

    def test_time_only_never_reengagement(self):
        messages = [msg("me", "早", "09:00"), msg("them", "早", "18:00")]
        assert build_interaction_events(messages, [])["events"] == []

    def test_media_restart_has_unknown_flag(self):
        messages = [
            msg("me", "先这样", "2026-03-01 20:00"),
            msg("them", "好", "2026-03-01 20:01"),
            {"speaker": "them", "text": "[图片]", "time": "2026-03-03 09:00",
             "content_type": "media", "media_kinds": ["image"]},
        ]
        events = build_interaction_events(messages, [])["events"]
        assert len(events) == 1
        assert events[0].get("media_content_unknown") is True


# ---------------------------------------------------------------------------
# Follow-up sequence
# ---------------------------------------------------------------------------


FOLLOWUP_MESSAGES = [
    msg("me", "最近胃不舒服", "2026-03-02 10:00"),
    msg("them", "怎么了？", "2026-03-02 10:01"),
    msg("me", "可能吃坏了", "2026-03-02 10:02"),
    msg("them", "现在还疼吗？", "2026-03-02 10:03"),
]


class TestFollowup:
    def test_followup_detected_and_mapped(self):
        results = [{"index": 1, "result": make_result(**ASK)},
                   {"index": 3, "result": make_result(**ASK)}]
        interaction, _, profile = pipeline(FOLLOWUP_MESSAGES, results)
        events = [e for e in interaction["events"]
                  if e["event_type"] == "followup_sequence"]
        assert len(events) == 1
        assert events[0]["review_status"] == idyn.REVIEW_AUTO
        assert len(profile["dimensions"]["initiative_engagement"]
                   ["salient_events"]) == 1

    def test_plain_qa_no_followup(self):
        messages = [
            msg("me", "几点开会？", "2026-03-02 10:00"),
            msg("them", "三点。", "2026-03-02 10:01"),
            msg("me", "会议室在哪？", "2026-03-02 10:02"),
            msg("them", "二楼。", "2026-03-02 10:03"),
        ]
        results = [{"index": 1, "result": make_result(
                        intent="confirm_understanding",
                        probs={"confirm_understanding": 0.9, "other": 0.1})},
                   {"index": 3, "result": make_result(
                        intent="confirm_understanding",
                        probs={"confirm_understanding": 0.9, "other": 0.1})}]
        interaction, _, _ = pipeline(messages, results)
        assert not any(e["event_type"] == "followup_sequence"
                       for e in interaction["events"])

    def test_single_question_no_followup(self):
        messages = [
            msg("me", "周末有安排吗", "2026-03-02 10:00"),
            msg("them", "哈哈", "2026-03-02 10:01"),
        ]
        interaction, _, _ = pipeline(messages, [])
        assert not any(e["event_type"] == "followup_sequence"
                       for e in interaction["events"])

    def test_care_signal_required_for_care_mapping(self):
        no_care = [{"index": 1, "result": make_result(**ASK)},
                   {"index": 3, "result": make_result(**ASK)}]
        with_care = [{"index": 1, "result": make_result(**CARE_ASK)},
                     {"index": 3, "result": make_result(**ASK)}]
        _, _, profile_plain = pipeline(FOLLOWUP_MESSAGES, no_care)
        _, _, profile_care = pipeline(FOLLOWUP_MESSAGES, with_care)
        assert profile_plain["dimensions"]["care_responsiveness"][
            "salient_events"] == []
        assert len(profile_care["dimensions"]["care_responsiveness"][
            "salient_events"]) == 1

    def test_longer_chain_non_overlapping(self):
        messages = [
            msg("me", "胃不舒服", "2026-03-02 10:00"),
            msg("them", "怎么了？", "2026-03-02 10:01"),
            msg("me", "吃坏了", "2026-03-02 10:02"),
            msg("them", "还疼吗？", "2026-03-02 10:03"),
            msg("me", "好一点了", "2026-03-02 10:04"),
            msg("them", "吃药了吗？", "2026-03-02 10:05"),
        ]
        results = [{"index": 1, "result": make_result(**ASK)},
                   {"index": 3, "result": make_result(**ASK)},
                   {"index": 5, "result": make_result(**ASK)}]
        interaction, _, _ = pipeline(messages, results)
        followups = [e for e in interaction["events"]
                     if e["event_type"] == "followup_sequence"]
        # 一条连续追问链 = 一个事件（4-turn 窗口不重叠）
        assert len(followups) == 1
        assert followups[0]["window"]["end_index"] == 5


# ---------------------------------------------------------------------------
# Invitation progression
# ---------------------------------------------------------------------------


class TestInvitation:
    def test_vague_to_concrete(self):
        messages = [
            msg("me", "这周忙死了", "2026-03-05 12:00"),
            msg("them", "忙完有空一起吃饭", "2026-03-05 12:01"),
            msg("me", "好啊", "2026-03-05 12:05"),
            msg("them", "那周六六点，公司楼下见", "2026-03-05 12:06"),
        ]
        results = [{"index": 1, "result": make_result(**INVITE)},
                   {"index": 3, "result": make_result(**INVITE)}]
        interaction, _, profile = pipeline(messages, results)
        events = [e for e in interaction["events"]
                  if e["event_type"] == "invitation_progression"]
        assert len(events) == 1
        assert events[0]["party_structure"] == "unknown"
        assert profile["dimensions"]["romantic"]["salient_events"] == []

    @pytest.mark.parametrize("text,expected", [
        ("周末大家一起吃饭？", "group"),
        ("就咱俩去看电影？", "dyadic"),
        ("有空吃饭", "unknown"),
    ])
    def test_party_structure(self, text, expected):
        messages = [
            msg("me", "忙完了", "2026-03-05 12:00"),
            msg("them", text, "2026-03-05 12:01"),
            msg("me", "行", "2026-03-05 12:05"),
            msg("them", "那周六六点见", "2026-03-05 12:06"),
        ]
        results = [{"index": 1, "result": make_result(**INVITE)},
                   {"index": 3, "result": make_result(**INVITE)}]
        events = [e for e in build_interaction_events(messages, results)["events"]
                  if e["event_type"] == "invitation_progression"]
        assert events and events[0]["party_structure"] == expected

    def test_concrete_first_no_progression(self):
        messages = [
            msg("them", "周六六点一起吃饭", "2026-03-05 12:01"),
            msg("me", "好", "2026-03-05 12:05"),
            msg("them", "那周日呢", "2026-03-05 12:06"),
        ]
        results = [{"index": 0, "result": make_result(**INVITE)},
                   {"index": 2, "result": make_result(**INVITE)}]
        assert not any(e["event_type"] == "invitation_progression"
                       for e in build_interaction_events(messages, results)["events"])


# ---------------------------------------------------------------------------
# Personal recall
# ---------------------------------------------------------------------------


class TestPersonalRecall:
    def test_strict_candidate_is_review_required(self):
        messages = [
            msg("me", "周五要去拔智齿", "2026-03-01 09:00"),
            msg("them", "你智齿拔了吗？", "2026-03-04 18:00"),
        ]
        interaction, _, profile = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "personal_recall_candidate"]
        assert len(events) == 1
        assert events[0]["review_status"] == idyn.REVIEW_CANDIDATE
        # 候选不进正式 salient evidence
        assert profile["dimensions"]["care_responsiveness"]["salient_events"] == []

    def test_generic_overlap_rejected(self):
        assert idyn._distinctive_anchors("今天工作好忙", "今天工作怎么样？") == []

    def test_different_subject_rejected(self):
        assert idyn._distinctive_anchors("我妈要去医院", "我今天路过医院") == []

    def test_user_reopened_rejected(self):
        messages = [
            msg("me", "周五要去拔智齿", "2026-03-01 09:00"),
            msg("me", "智齿拔完了", "2026-03-02 10:00"),
            msg("them", "恢复得怎么样？", "2026-03-04 18:00"),
        ]
        assert not any(e["event_type"] == "personal_recall_candidate"
                       for e in build_interaction_events(messages, [])["events"])

    def test_same_day_not_recall(self):
        messages = [
            msg("me", "周五要去拔智齿", "2026-03-01 09:00"),
            msg("them", "你智齿拔了吗？", "2026-03-01 21:00"),
        ]
        assert not any(e["event_type"] == "personal_recall_candidate"
                       for e in build_interaction_events(messages, [])["events"])

    def test_incomplete_timestamp_not_recall(self):
        messages = [
            msg("me", "周五要去拔智齿", "09:00"),
            msg("them", "你智齿拔了吗？", "18:00"),
        ]
        assert not any(e["event_type"] == "personal_recall_candidate"
                       for e in build_interaction_events(messages, [])["events"])


# ---------------------------------------------------------------------------
# Reciprocity
# ---------------------------------------------------------------------------


class TestReciprocity:
    def test_both_sides_observation(self):
        messages = [
            msg("me", "你今天怎么样？", "2026-03-02 10:00"),
            msg("them", "挺好的，你呢？", "2026-03-02 10:01"),
            msg("me", "我也不错，周末去爬山了", "2026-03-02 10:02"),
            msg("them", "我周末去钓鱼了", "2026-03-02 10:03"),
        ]
        results = [{"index": 1, "result": make_result(**ASK)},
                   {"index": 3, "result": make_result(
                       intent="share_personal",
                       probs={"share_personal": 0.8, "other": 0.2})}]
        interaction, _, _ = pipeline(messages, results)
        rec = interaction["observations"]["reciprocity"]
        assert rec["sufficient"] is True
        assert rec["counts"]["me_question_turns"] >= 1
        assert rec["counts"]["ta_question_turns"] >= 1
        assert rec["counts"]["me_share_turns"] >= 1

    def test_one_sided_has_disclaimer_no_personality(self):
        messages = [
            msg("me", "今天有空吗", "2026-03-02 10:00"),
            msg("them", "有", "2026-03-02 10:01"),
            msg("me", "那出来吗", "2026-03-02 10:02"),
            msg("them", "可以", "2026-03-02 10:03"),
            msg("me", "周六呢", "2026-03-02 10:04"),
            msg("them", "行", "2026-03-02 10:05"),
        ]
        interaction, _, _ = pipeline(messages, [])
        text = "\n".join(interaction["summary_lines"])
        assert "提问基本由用户侧发起" in text
        assert "仅描述当前导入样本" in text
        assert "%" not in text

    def test_small_sample_insufficient(self):
        messages = [
            msg("me", "在吗", "2026-03-02 10:00"),
            msg("them", "在", "2026-03-02 10:01"),
        ]
        interaction, _, _ = pipeline(messages, [])
        rec = interaction["observations"]["reciprocity"]
        assert rec["sufficient"] is False


# ---------------------------------------------------------------------------
# Boundary response（D5 正式路径）
# ---------------------------------------------------------------------------


class TestBoundaryResponse:
    def test_refusal_accepted_is_counter(self):
        messages = [
            msg("me", "今天不想去", "2026-03-06 19:00"),
            msg("them", "好，那不去了", "2026-03-06 19:01"),
        ]
        interaction, _, profile = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "boundary_accepted"]
        assert len(events) == 1 and events[0]["direction"] == "counter"
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["status"] == rp.STATUS_EVIDENCE_LIMITED
        assert dim["direction"] == rp.DIR_COUNTER
        assert len(dim["counter_events"]) == 1

    def test_refusal_adjusted(self):
        messages = [
            msg("me", "今天不行", "2026-03-06 19:00"),
            msg("them", "行，那改周日？", "2026-03-06 19:01"),
        ]
        interaction, _, _ = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "boundary_adjusted"]
        assert len(events) == 1 and events[0]["direction"] == "counter"

    def test_refusal_pressure(self):
        messages = [
            msg("me", "我不想去", "2026-03-06 19:00"),
            msg("them", "就来嘛", "2026-03-06 19:01"),
            msg("me", "真的不去", "2026-03-06 19:02"),
            msg("them", "来吧，就一会", "2026-03-06 19:03"),
        ]
        interaction, _, profile = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "boundary_pressure"]
        assert events and all(e["review_status"] == idyn.REVIEW_AUTO
                              for e in events)
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["direction"] == rp.DIR_SUPPORTING
        assert "不使用 legacy base_score" in " ".join(
            dim["strength"]["basis"])

    def test_soft_reschedule_not_pressure(self):
        messages = [
            msg("me", "今天不行，改天吧", "2026-03-06 19:00"),
            msg("them", "好的", "2026-03-06 19:01"),
        ]
        interaction, _, _ = pipeline(messages, [])
        assert not any(e["event_type"] == "boundary_pressure"
                       for e in interaction["events"])
        assert any(e["event_type"] == "boundary_accepted"
                   for e in interaction["events"])

    def test_ambiguous_is_review_required(self):
        messages = [
            msg("me", "今天不方便", "2026-03-06 19:00"),
            msg("them", "那晚上呢？", "2026-03-06 19:01"),
        ]
        interaction, _, profile = pipeline(messages, [])
        events = [e for e in interaction["events"]
                  if e["event_type"] == "boundary_ambiguous"]
        assert len(events) == 1
        assert events[0]["review_status"] == idyn.REVIEW_CANDIDATE
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["status"] == rp.STATUS_EVIDENCE_LIMITED
        assert dim["salient_events"] == [] and dim["counter_events"] == []

    def test_silence_is_not_evidence(self):
        messages = [
            msg("me", "今天不想去", "2026-03-06 19:00"),
            msg("me", "算了", "2026-03-06 19:01"),
            msg("them", "[图片]", "2026-03-06 19:02",
                content_type="media", media_kinds=["image"]),
        ]
        interaction, _, profile = pipeline(messages, [])
        assert not any(e["dimension"] == "boundary_pressure"
                       and e["review_status"] == idyn.REVIEW_AUTO
                       for e in interaction["events"])
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["counter_events"] == []

    def test_no_opportunity_is_insufficient(self):
        messages = [
            msg("me", "早上好", "2026-03-06 08:00"),
            msg("them", "早", "2026-03-06 08:01"),
        ]
        interaction, _, profile = pipeline(messages, [])
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["status"] == rp.STATUS_INSUFFICIENT
        assert "没有可用于观察" in dim["conclusion"]

    def test_engine_unavailable_stays_unsupported(self):
        profile = build_profile([])
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["status"] == rp.STATUS_UNSUPPORTED


# ---------------------------------------------------------------------------
# Profile / Salience 集成
# ---------------------------------------------------------------------------


class TestProfileIntegration:
    def test_review_required_not_in_formal_events(self):
        messages = [
            msg("me", "周五要去拔智齿", "2026-03-01 09:00"),
            msg("them", "你智齿拔了吗？", "2026-03-04 18:00"),
        ]
        interaction, salience_out, profile = pipeline(messages, [])
        assert len(profile["dimensions"]["care_responsiveness"][
            "interaction_events"]) == 1
        assert profile["dimensions"]["care_responsiveness"][
            "salient_events"] == []
        assert all(e["review_status"] != idyn.REVIEW_CANDIDATE
                   for e in salience_out["events"])

    def test_no_fingerprints_or_identity_in_profile(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I9_refusal_then_accepted"))
        interaction, _, profile = pipeline(messages, results)
        dumped = json.dumps(profile, ensure_ascii=False)
        assert "fingerprints" not in dumped
        assert "raw_speaker" not in dumped
        for dim in profile["dimensions"].values():
            for e in dim.get("interaction_events") or []:
                assert "fingerprints" not in e and "identity" not in e

    def test_slots_empty_without_interaction(self):
        profile = build_profile([])
        for key in rp.DIMENSION_ORDER:
            assert profile["dimensions"][key]["interaction_events"] == []

    def test_no_new_composite_score(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS
                 if c["id"] == "I11_refusal_then_pressure"))
        interaction, salience_out, profile = pipeline(messages, results)
        for blob in (json.dumps(interaction, ensure_ascii=False),
                     json.dumps(salience_out, ensure_ascii=False),
                     json.dumps(profile, ensure_ascii=False)):
            for banned in ("composite", "salience_score", "overall_salience",
                           "关系健康度：", "喜欢概率：", "尊重分：", "综合关系分："):
                assert banned not in blob, banned

    def test_capability_gap_updated(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I2_followup_sequence"))
        interaction, _, profile = pipeline(messages, results)
        text = "\n".join(profile["capability_gaps"])
        assert "#20" in text and "已启用" in text

    def test_d5_no_positive_conclusion(self):
        messages = [
            msg("me", "先这样", "2026-03-01 20:00"),
            msg("them", "好", "2026-03-01 20:01"),
            msg("them", "你再考虑考虑嘛，我特意为你准备的", "2026-03-03 09:00"),
        ]
        results = [{"index": 1, "result": make_result(warmth=2.4, engagement=3.4,
                                                        special=3.2, evidence=3.4,
                                                        romantic=0.55)},
                   {"index": 2, "result": make_result(warmth=2.4, engagement=3.4,
                                                      special=3.2, evidence=3.4,
                                                      romantic=0.55)}]
        interaction, _, profile = pipeline(messages, results)
        text = _all_text(interaction, profile)
        for banned in ("正向事件", "关系健康", "尊重边界", "好感增加", "关系变好"):
            assert banned not in text, banned
        dim = profile["dimensions"]["boundary_pressure"]
        # 无 boundary opportunity → insufficient（不因高 evidence 变正向）
        assert dim["status"] == rp.STATUS_INSUFFICIENT


# ---------------------------------------------------------------------------
# 假阳性闸门（§62）
# ---------------------------------------------------------------------------


class TestFalsePositiveGate:
    def test_ordinary_warmth_no_special(self):
        messages = [
            msg("them", "收到，谢谢你", "2026-03-02 10:00"),
            msg("them", "哈哈你又来了", "2026-03-02 10:01"),
        ]
        results = [{"index": 0, "result": make_result(warmth=2.7,
                                                      engagement=3.2,
                                                      special=1.5)},
                   {"index": 1, "result": make_result(warmth=2.8,
                                                      engagement=3.0,
                                                      special=1.6)}]
        interaction, _, _ = pipeline(messages, results)
        assert not any(e["dimension"] in ("special_attention", "romantic")
                       for e in interaction["events"])

    def test_ta_transactional_after_gap_not_special(self):
        messages = [
            msg("me", "今天先聊到这", "2026-03-01 20:00"),
            msg("them", "好", "2026-03-01 20:01"),
            msg("them", "那个文件发你了", "2026-03-03 09:00"),
        ]
        interaction, _, profile = pipeline(messages, [])
        for key in ("special_attention", "romantic", "care_responsiveness"):
            assert profile["dimensions"][key]["salient_events"] == []

    def test_group_invite_no_romantic(self):
        messages = [
            msg("them", "周末大家一起吃饭？", "2026-03-05 18:00"),
        ]
        results = [{"index": 0, "result": make_result(**INVITE, warmth=2.6,
                                                      engagement=3.0)}]
        interaction, _, profile = pipeline(messages, results)
        assert profile["dimensions"]["romantic"]["salient_events"] == []
        assert not any(e["event_type"] == "invitation_progression"
                       for e in interaction["events"])

    def test_reschedule_then_evening_not_pressure(self):
        messages = [
            msg("me", "今天不方便", "2026-03-06 19:00"),
            msg("them", "那晚上呢？", "2026-03-06 19:01"),
        ]
        interaction, _, _ = pipeline(messages, results := [])
        assert not any(e["event_type"] == "boundary_pressure"
                       for e in interaction["events"])

    def test_reply_speed_never_inferred(self):
        fast = [msg("them", "在", "2026-03-02 10:00"),
                msg("me", "早", "2026-03-02 10:01"),
                msg("them", "早", "2026-03-02 10:02")]
        slow = [msg("them", "在", "2026-03-02 10:00"),
                msg("me", "早", "2026-03-02 10:20"),
                msg("them", "早", "2026-03-02 10:21")]
        for messages in (fast, slow):
            interaction, _, _ = pipeline(messages, [])
            dumped = json.dumps(interaction, ensure_ascii=False)
            for banned in ("回复快", "回复慢", "速度快", "投入", "在意速度"):
                assert banned not in dumped


# ---------------------------------------------------------------------------
# Report 视图
# ---------------------------------------------------------------------------


class TestReportView:
    def test_report_view_strips_identity(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I2_followup_sequence"))
        interaction = build_interaction_events(messages, results)
        view = idyn.report_view(interaction)
        dumped = json.dumps(view, ensure_ascii=False)
        assert "fingerprints" not in dumped
        assert "identity" not in dumped
        assert view["version"] == idyn.INTERACTION_VERSION
        assert view["events"]

    def test_capability_matrix_lists_deferred(self):
        caps = idyn.CAPABILITIES
        assert caps["personal_recall"]["auto"] == "candidate-only"
        assert caps["boundary_response"]["auto"].startswith("yes")
        assert caps["conversation_reengagement"]["time_required"] == \
            "full timestamp"


# ---------------------------------------------------------------------------
# behavior 适配器
# ---------------------------------------------------------------------------


class TestBehaviorAdapter:
    def test_candidates_conform(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS if c["id"] == "I2_followup_sequence"))
        interaction = build_interaction_events(messages, results)
        candidates = idyn.interaction_behavior_candidates(interaction, messages)
        assert candidates
        for c in candidates:
            assert bv.valid_pair(c.dimension, c.behavior_type)
            assert c.fingerprints and c.flags and c.msg_texts is not None
            assert c.evidence_note

    def test_review_candidates_also_offered_for_review(self):
        messages, results = idiag.materialize_case(
            next(c for c in _SCENARIOS
                 if c["id"] == "I15_strict_personal_recall_candidate"))
        interaction = build_interaction_events(messages, results)
        candidates = idyn.interaction_behavior_candidates(interaction, messages)
        assert any(c.rule == "interaction:personal_recall_candidate"
                   for c in candidates)

    def test_empty_interaction_yields_no_candidates(self):
        assert idyn.interaction_behavior_candidates({"events": []}, []) == []
