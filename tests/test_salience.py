"""Issue #19 测试：salience 通道（事件 schema / 身份去重 / 方向规则 / counter
语义 / unsupported / 稀释 / 离群 / 重复证据 / 冲突并存 / 假阳性闸门 / D5 /
S1~S15 场景 / Profile 集成 / baseline）。全部离线、确定性、虚构数据。"""

import json
from pathlib import Path

import pytest

import relationship_profile as rp
import salience as sal
import salience_diagnostics as sdiag
import scoring
from relationship_profile import build_profile

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = REPO_ROOT / "evaluation" / "salience_scenarios_v0.4.json"
CONV_PATH = (REPO_ROOT / "evaluation" / "fixtures"
             / "relationship_conversations_v0.4_synthetic.json")

_CONV = json.loads(CONV_PATH.read_text(encoding="utf-8"))["cases"]
_SCENARIO_DOC = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))

# 普通消息 / 显著消息 / 反向消息 的固定画像（虚构数值）
ORDINARY = {"warmth": 2.0, "engagement": 2.1, "special": 1.2, "evidence": 1.5,
            "ease": 2.2, "romantic": 0.08, "distancing": 0.06, "conf": 0.75}
SPECIAL_EVENT = {"warmth": 2.6, "engagement": 3.0, "special": 3.6,
                 "evidence": 3.2, "ease": 2.4, "romantic": 0.2,
                 "distancing": 0.04, "conf": 0.85}


def make_entries(specs):
    return [{"index": i, "result": sdiag.materialize_result(s)}
            for i, s in enumerate(specs)]


def case_entries(case_id):
    payload = _CONV[case_id]
    return [{"index": i, "result": r}
            for i, r in enumerate(payload["results"])]


def build(entries):
    sal_out = sal.build_salience(entries)
    profile = build_profile(entries, salience=sal_out)
    return sal_out, profile


# ---------------------------------------------------------------------------
# 事件 schema / 身份 / 确定性
# ---------------------------------------------------------------------------

REQUIRED_EVENT_FIELDS = {
    "event_id", "dimension", "event_class", "direction", "source",
    "message_index", "metric", "value", "scale", "confidence",
    "relationship_evidence_strength", "salience_level", "reason",
    "alternative_explanation", "limitations",
}


class TestEventSchema:
    def test_event_fields_complete(self):
        sal_out, _ = build(make_entries([ORDINARY, SPECIAL_EVENT]))
        events = sal_out["events"]
        assert events
        for e in events:
            assert REQUIRED_EVENT_FIELDS <= set(e), e["event_id"]
            assert e["direction"] in (sal.DIRECTION_SUPPORTING,
                                      sal.DIRECTION_COUNTER)
            assert e["dimension"] in rp.DIMENSION_ORDER
            assert e["salience_level"] in (sal.SALIENCE_LEVEL_EXPLICIT,
                                           sal.SALIENCE_LEVEL_STRONG,
                                           sal.SALIENCE_LEVEL_MODERATE)
            assert e["source"] == sal.SOURCE_JEV_METRIC
            assert e["reason"] and e["alternative_explanation"] and e["limitations"]

    def test_no_global_valence_fields(self):
        sal_out, _ = build(make_entries([ORDINARY, SPECIAL_EVENT]))
        for e in sal_out["events"]:
            for field in ("valence", "global_direction", "positive", "negative",
                          "boost", "score_impact", "weight", "net", "combined"):
                assert field not in e, field

    def test_event_identity_dedup_on_duplicate_messages(self):
        entries = make_entries([SPECIAL_EVENT])
        doubled = [dict(entries[0]), dict(entries[0])]  # 同 index 重复计算路径
        single = sal.build_salience(entries)
        dup = sal.build_salience(doubled)
        assert len(single["events"]) == len(dup["events"]) == 1
        assert dup["diagnostics"]["duplicate_suppressed"] == 1
        assert single["events"][0]["event_id"] == dup["events"][0]["event_id"]

    def test_same_message_multiple_dimensions_not_merged(self):
        spec = {"warmth": 3.2, "engagement": 3.4, "special": 3.6, "evidence": 3.6,
                "ease": 2.4, "romantic": 0.9, "distancing": 0.05, "conf": 0.85}
        sal_out, _ = build(make_entries([spec]))
        classes = sorted(e["event_class"] for e in sal_out["events"])
        assert classes == ["explicit_care_behavior", "explicit_romantic_signal",
                           "explicit_special_attention"]
        ids = [e["event_id"] for e in sal_out["events"]]
        assert len(ids) == len(set(ids))

    def test_deterministic_output(self):
        entries = make_entries([ORDINARY, SPECIAL_EVENT, ORDINARY])
        first = sal.build_salience(entries)
        second = sal.build_salience(entries)
        assert first == second


# ---------------------------------------------------------------------------
# 方向规则（evidence / message_weight / legacy overall 不决定方向）
# ---------------------------------------------------------------------------


class TestDirectionRules:
    def test_direction_mapping_dimension_scoped(self):
        sal_out, _ = build(make_entries([SPECIAL_EVENT, ORDINARY]))
        e = sal_out["events"][0]
        assert e["dimension"] == "special_attention"
        assert e["direction"] == sal.DIRECTION_SUPPORTING
        assert sal_out["dimensions"]["special_attention"]["salient_events"]
        # 其它维度不得“搭车”获得事件
        assert not sal_out["dimensions"]["romantic"]["salient_events"]
        assert not sal_out["dimensions"]["care_responsiveness"]["salient_events"]

    def test_evidence_strength_is_not_direction(self):
        low = dict(SPECIAL_EVENT, evidence=1.2)
        high = dict(SPECIAL_EVENT, evidence=4.0)
        e_low = sal.build_salience(make_entries([low]))["events"]
        e_high = sal.build_salience(make_entries([high]))["events"]
        assert [e["event_class"] for e in e_low] == \
            [e["event_class"] for e in e_high]
        assert [e["direction"] for e in e_low] == \
            [e["direction"] for e in e_high]
        # evidence 只是 eligibility / 信息量上下文：低于 1.0 不产生事件
        assert sal.build_salience(
            make_entries([dict(SPECIAL_EVENT, evidence=0.5)]))["events"] == []

    def test_message_weight_and_confidence_not_direction(self):
        weak_conf = dict(SPECIAL_EVENT, conf=0.3)
        strong_conf = dict(SPECIAL_EVENT, conf=0.95)
        e_weak = sal.build_salience(make_entries([weak_conf]))["events"]
        e_strong = sal.build_salience(make_entries([strong_conf]))["events"]
        assert [e["direction"] for e in e_weak] == \
            [e["direction"] for e in e_strong]
        assert [e["salience_level"] for e in e_weak] == \
            [e["salience_level"] for e in e_strong]
        # confidence 只进上下文字段，不改变方向 / 分级
        assert e_weak[0]["confidence"] == pytest.approx(0.3)
        assert e_strong[0]["confidence"] == pytest.approx(0.95)

    def test_legacy_overall_never_referenced(self):
        sal_out, _ = build(make_entries([SPECIAL_EVENT] + [ORDINARY] * 30))
        for e in sal_out["events"]:
            assert "overall" not in json.dumps(e, ensure_ascii=False)
            assert "base_score" not in json.dumps(e, ensure_ascii=False)
        notes = " ".join(sal_out["diagnostics"]["notes"])
        assert "overall" in notes  # 明确声明不参与方向判断


# ---------------------------------------------------------------------------
# counter 语义（absence ≠ counter；只有明确负向档产生事件）
# ---------------------------------------------------------------------------


class TestCounterSemantics:
    def test_absence_is_not_counter(self):
        spec = {"warmth": 1.5, "engagement": 1.9, "special": 0.5, "evidence": 1.8,
                "ease": 1.5, "romantic": 0.03, "distancing": 0.05, "conf": 0.75}
        sal_out, _ = build(make_entries([spec] * 3))
        assert sal_out["events"] == []

    def test_weak_negative_observation_not_event(self):
        spec = dict(ORDINARY, engagement=1.8, warmth=1.8, ease=1.6)
        sal_out, _ = build(make_entries([spec] * 3))
        assert sal_out["events"] == []
        # #18 的维度 counter evidence 语义不变（弱观察仍是证据条目，不是事件）
        _, profile = build(make_entries([spec] * 3))
        assert profile["dimensions"]["initiative_engagement"]["counter_count"] == 3

    def test_strict_negative_band_events(self):
        spec = {"warmth": 0.4, "engagement": 0.5, "special": 0.8, "evidence": 2.6,
                "ease": 0.6, "romantic": 0.04, "distancing": 0.2, "conf": 0.8}
        sal_out, _ = build(make_entries([spec]))
        classes = sorted(e["event_class"] for e in sal_out["events"])
        assert classes == ["cold_or_rejecting_response",
                           "low_investment_or_refusal",
                           "stiff_or_unfamiliar_interaction"]
        for e in sal_out["events"]:
            assert e["direction"] == sal.DIRECTION_COUNTER

    def test_special_romantic_withdrawal_have_no_counter_channel(self):
        spec = dict(ORDINARY, special=0.2, romantic=0.01, distancing=0.01)
        sal_out, _ = build(make_entries([spec] * 3))
        for key in ("special_attention", "romantic", "withdrawal"):
            assert sal_out["dimensions"][key]["counter_events"] == []


# ---------------------------------------------------------------------------
# unsupported 事件类 / 畸形输入 / diagnostics
# ---------------------------------------------------------------------------


class TestUnsupportedAndRobustness:
    def test_boundary_pressure_never_gets_events(self):
        _, profile = build(make_entries([SPECIAL_EVENT] * 2))
        bp = profile["dimensions"]["boundary_pressure"]
        assert bp["status"] == rp.STATUS_UNSUPPORTED
        assert bp["salient_events"] == [] and bp["counter_events"] == []

    def test_reserved_event_classes_never_emitted(self):
        for case in _SCENARIO_DOC["cases"]:
            sal_out = sal.build_salience(sdiag.scenario_entries(case))
            for e in sal_out["events"]:
                assert e["event_class"] not in sal.RESERVED_EVENT_CLASSES

    def test_malformed_entries_are_skipped_not_crashed(self):
        entries = make_entries([ORDINARY, SPECIAL_EVENT])
        entries.append({"index": 9, "result": None, "error": "boom"})
        entries.append({"index": 10, "result": {}})
        sal_out = sal.build_salience(entries)
        assert len(sal_out["events"]) == 1

    def test_empty_results(self):
        sal_out = sal.build_salience([])
        assert sal_out["events"] == []
        assert "没有可分析消息" in sal_out["baseline_summary"]

    def test_unclassified_high_information_is_diagnostics_only(self):
        spec = dict(ORDINARY, evidence=3.6, special=2.0, warmth=2.0)
        sal_out, profile = build(make_entries([spec]))
        assert sal_out["events"] == []
        unclassified = sal_out["diagnostics"]["unclassified_high_information"]
        assert [u["message_index"] for u in unclassified] == [0]
        # 只进 limitation / diagnostics，绝不成为用户结论
        text = profile["summary"]["text"]
        assert "无法定向" in " ".join(profile["limitations"])
        assert "正向" not in text and "负向" not in text


# ---------------------------------------------------------------------------
# Dilution（§21）/ Outlier（§7.2）/ Repeated（§18）/ Conflict（§7.4）
# ---------------------------------------------------------------------------


class TestDilution:
    @pytest.mark.parametrize("n", [0, 1, 5, 10, 30, 100])
    def test_event_retained_at_every_dilution(self, n):
        entries = make_entries([SPECIAL_EVENT] + [ORDINARY] * n)
        stats = scoring.compute_conversation_stats(entries)
        sal_out = sal.build_salience(entries, stats=stats)
        special = sal_out["dimensions"]["special_attention"]
        assert special["salient_count"] == 1, n
        assert special["salient_tier"] == sal.TIER_SINGLE
        assert special["salient_events"][0]["event_class"] == \
            "explicit_special_attention"

    def test_legacy_overall_dilutes_but_baseline_stable(self):
        overalls = []
        for n in (1, 5, 10, 30, 100):
            entries = make_entries([SPECIAL_EVENT] + [ORDINARY] * n)
            stats = scoring.compute_conversation_stats(entries)
            sal_out = sal.build_salience(entries, stats=stats)
            overalls.append(stats["overall"])
            baseline = sal_out["dimensions"]["special_attention"]["baseline"]
            assert baseline["value"] == pytest.approx(ORDINARY["special"])
            assert baseline["excluded_event_messages"] == 1
        # legacy overall 允许继续下降（成功标准不是 overall 不下降）
        assert overalls == sorted(overalls, reverse=True)
        assert overalls[0] - overalls[-1] > 5


class TestOutlierSafety:
    def test_single_extreme_cannot_control_baseline(self):
        extreme = {"warmth": 4.0, "engagement": 4.0, "special": 4.0,
                   "evidence": 4.0, "ease": 4.0, "romantic": 0.95,
                   "distancing": 0.02, "conf": 0.95}
        sal_out, profile = build(make_entries([ORDINARY] * 30 + [extreme]))
        baseline = sal_out["dimensions"]["special_attention"]["baseline"]
        assert baseline["value"] == pytest.approx(ORDINARY["special"])
        assert sal_out["events"]  # 事件可见
        # 单条不外推为长期结论
        assert "不外推为长期关系模式" in profile["summary"]["text"]

    def test_baseline_absent_when_all_messages_are_events(self):
        sal_out, _ = build(make_entries([SPECIAL_EVENT] * 2))
        baseline = sal_out["dimensions"]["special_attention"]["baseline"]
        assert baseline["value"] is None
        assert baseline["ordinary_messages"] == 0
        assert "无法给出普通互动基线" in sal_out["baseline_summary"]


class TestRepeatedEvidence:
    @pytest.mark.parametrize("k,expected_tier", [
        (1, sal.TIER_SINGLE), (2, sal.TIER_MULTIPLE), (3, sal.TIER_REPEATED),
        (5, sal.TIER_REPEATED),
    ])
    def test_capped_qualitative_tiers(self, k, expected_tier):
        entries = make_entries([SPECIAL_EVENT] * k + [ORDINARY] * 5)
        sal_out = sal.build_salience(entries)
        special = sal_out["dimensions"]["special_attention"]
        assert special["salient_count"] == k
        assert special["salient_tier"] == expected_tier  # ≥3 封顶，不线性升级

    def test_no_pseudo_precision_in_phrases(self):
        sal_out, _ = build(make_entries([SPECIAL_EVENT] * 3))
        phrase = sal_out["dimensions"]["special_attention"]["salient_phrase"]
        assert "%" not in phrase and "概率" not in phrase
        assert "统计独立性" in phrase  # 明说不声称统计独立性
        assert "3 条不同消息" in phrase


class TestConflictCoexistence:
    def test_supporting_and_counter_coexist_without_averaging(self):
        spec = {"warmth": 0.4, "engagement": 0.5, "special": 0.8, "evidence": 2.6,
                "ease": 0.8, "romantic": 0.04, "distancing": 0.3, "conf": 0.8}
        sal_out, profile = build(make_entries([SPECIAL_EVENT, ORDINARY, spec]))
        salient_n = sum(len(sal_out["dimensions"][k]["salient_events"])
                        for k in rp.DIMENSION_ORDER)
        counter_n = sum(len(sal_out["dimensions"][k]["counter_events"])
                        for k in rp.DIMENSION_ORDER)
        assert salient_n == 1 and counter_n == 3
        assert "未做加权抵消或平均" in profile["summary"]["text"]
        # 输出里没有任何“净分 / 抵消 / 平均”字段
        dumped = json.dumps(sal_out["dimensions"], ensure_ascii=False)
        for banned in ("net", "combined", "averaged", "balance", "抵消成"):
            assert banned not in dumped


# ---------------------------------------------------------------------------
# 假阳性闸门（§33）
# ---------------------------------------------------------------------------


class TestFalsePositiveGate:
    @pytest.mark.parametrize("case_id,forbidden", [
        ("rb_neg_group_invite", ["explicit_romantic_signal",
                                 "explicit_special_attention"]),
        ("rb_neg_health_polite", ["explicit_romantic_signal",
                                  "explicit_special_attention"]),
        ("rb_rom_teasing_flirt", ["explicit_romantic_signal"]),
        ("rb_fam_mutual_teasing", ["explicit_romantic_signal"]),
        ("rb_dis_topic_close", ["explicit_relationship_withdrawal"]),
    ])
    def test_fixture_cases_produce_no_forbidden_events(self, case_id, forbidden):
        sal_out, _ = build(case_entries(case_id))
        for e in sal_out["events"]:
            assert e["event_class"] not in forbidden, (case_id, e["event_class"])

    def test_ordinary_warmth_no_special(self):
        spec = dict(ORDINARY, warmth=2.8, engagement=3.0)
        sal_out, _ = build(make_entries([spec] * 3))
        assert sal_out["events"] == []

    def test_repeated_short_replies_no_salience(self):
        spec = dict(ORDINARY, warmth=1.6, engagement=1.4, special=0.6,
                    evidence=1.2, romantic=0.05)
        sal_out, _ = build(make_entries([spec] * 40))
        assert sal_out["events"] == []


# ---------------------------------------------------------------------------
# D5 强制约束（§15）
# ---------------------------------------------------------------------------


class TestD5Safety:
    def test_pressure_message_not_laundered_as_positive(self):
        # #17 D5 fixture 实测数值：高 engagement / 高 evidence / 高 special
        spec = {"warmth": 2.4, "engagement": 3.4, "special": 3.2, "evidence": 3.4,
                "ease": 2.2, "romantic": 0.55, "distancing": 0.1, "conf": 0.8}
        sal_out, profile = build(make_entries([ORDINARY, spec]))
        events = sal_out["events"]
        classes = {e["event_class"] for e in events}
        # romantic 0.55 只是弱信号：绝不升级为 romantic 事件
        assert "explicit_romantic_signal" not in classes
        # 不生成任何全局正向事件（词汇表里根本不存在）
        assert not any("positive" in e["event_class"] or "closeness" in e["event_class"]
                       or "relationship" in e["event_class"] for e in events)
        # special 事件（若保留）必须携带 D5 限制与替代解释
        for e in events:
            assert any("高信息量 ≠ 正向关系信号" in note for note in e["limitations"])
            assert e["alternative_explanation"]
        # boundary pressure 依旧 unsupported 且无伪造事件
        bp = profile["dimensions"]["boundary_pressure"]
        assert bp["status"] == rp.STATUS_UNSUPPORTED
        assert bp["salient_events"] == [] and bp["counter_events"] == []
        # 用户可见文本无正向关系结论
        text = profile["summary"]["text"] + " ".join(
            d["conclusion"] for d in profile["dimensions"].values())
        for banned in ("更强", "更好", "更健康", "关系健康", "尊重边界",
                       "喜欢概率", "恋爱可能性", "正向"):
            assert banned not in text, banned

    def test_scenario_s14_expectations(self):
        case = next(c for c in _SCENARIO_DOC["cases"] if c["scenario"] == "S14")
        report = sdiag.evaluate_scenario(case)
        assert report["passed"], [c for c in report["checks"] if not c["passed"]]


# ---------------------------------------------------------------------------
# S1~S15 场景约束（冻结期望）
# ---------------------------------------------------------------------------


class TestScenarios:
    @pytest.mark.parametrize("case",
                             _SCENARIO_DOC["cases"],
                             ids=[c["id"] for c in _SCENARIO_DOC["cases"]])
    def test_scenario_expectations(self, case):
        report = sdiag.evaluate_scenario(case)
        failed = [c for c in report["checks"] if not c["passed"]]
        assert report["passed"], failed


# ---------------------------------------------------------------------------
# Profile 集成（§24/§25）与 baseline
# ---------------------------------------------------------------------------


class TestProfileIntegration:
    def test_slots_empty_without_salience(self):
        profile = build_profile(make_entries([SPECIAL_EVENT, ORDINARY]))
        for key in rp.DIMENSION_ORDER:
            assert profile["dimensions"][key]["salient_events"] == []
            assert profile["dimensions"][key]["counter_events"] == []
        assert profile["reserved"]["salient_events"] == []

    def test_slots_filled_per_dimension(self):
        sal_out, profile = build(make_entries([SPECIAL_EVENT, ORDINARY]))
        special = profile["dimensions"]["special_attention"]
        assert len(special["salient_events"]) == 1
        assert special["salient_phrase"]
        # 事件不出现在错误的维度
        for key in rp.DIMENSION_ORDER:
            if key == "special_attention":
                continue
            assert profile["dimensions"][key]["salient_events"] == []
        # reserved 扁平列表 = 各维度之和（不重复保存第三份）
        assert len(profile["reserved"]["salient_events"]) == 1
        assert len(profile["reserved"]["counter_events"]) == 0

    def test_salient_event_does_not_break_status_semantics(self):
        # 单条明确事件（单消息会话）：status 仍是 evidence_limited + 覆盖有限
        sal_out, profile = build(make_entries([SPECIAL_EVENT]))
        dim = profile["dimensions"]["special_attention"]
        assert dim["status"] == rp.STATUS_EVIDENCE_LIMITED
        assert "覆盖有限" in dim["conclusion"]
        assert dim["reliability"]["level"] != rp.CONF_LEVEL_HIGH
        assert len(dim["salient_events"]) == 1

    def test_baseline_excludes_event_messages_exactly(self):
        entries = make_entries([ORDINARY] * 4 + [SPECIAL_EVENT])
        sal_out = sal.build_salience(entries)
        baseline = sal_out["dimensions"]["special_attention"]["baseline"]
        assert baseline["ordinary_messages"] == 4
        assert baseline["excluded_event_messages"] == 1
        assert baseline["value"] == pytest.approx(ORDINARY["special"])

    def test_noul_baseline_counts_only_weak_on_ordinary(self):
        event = dict(ORDINARY, romantic=0.92, evidence=3.0)
        weak = dict(ORDINARY, romantic=0.45)
        entries = make_entries([weak, weak, event])
        sal_out = sal.build_salience(entries)
        baseline = sal_out["dimensions"]["romantic"]["baseline"]
        assert baseline["clear_signal_count"] == 0   # 明确档消息 = 事件，已排除
        assert baseline["weak_signal_count"] == 2

    def test_no_new_composite_score(self):
        sal_out, profile = build(make_entries([SPECIAL_EVENT, ORDINARY]))
        for blob in (json.dumps(sal_out, ensure_ascii=False),
                     json.dumps(profile, ensure_ascii=False)):
            # 结构上不得出现任何综合分键；措辞上不得出现数值输出形式
            # （“禁止解读为喜欢概率”类否定句是应有措辞，不禁——与 #18 约定一致）
            for banned in ("composite", "salience_score", "overall_salience",
                           "喜欢概率：", "恋爱可能性", "关系健康度：",
                           "综合关系分：", "salience_level\": \"87"):
                assert banned not in blob, banned
