"""relationship_profile（Relationship Profile v2）测试。

两部分：
1. 单元语义：schema / status / coverage / reliability / strength /
   counter 语义（低分 ≠ 反证）/ unsupported / 概率不确定性 / 缺字段降级 /
   确定性 / 无新综合分数；
2. #17 relationship benchmark 语义不变量 A~I（合成 fixture，全虚构）。
"""

import json
from pathlib import Path

import pytest

import relationship_profile as rp
import scoring
from relationship_profile import build_profile

REPO_ROOT = Path(__file__).resolve().parent.parent
CONV_PATH = (REPO_ROOT / "evaluation" / "fixtures"
             / "relationship_conversations_v0.4_synthetic.json")

_CONV = json.loads(CONV_PATH.read_text(encoding="utf-8"))["cases"]


def case_results(case_id: str) -> list[dict]:
    payload = _CONV[case_id]
    return [{"index": i, "result": r}
            for i, r in enumerate(payload["results"])]


def case_profile(case_id: str) -> dict:
    return build_profile(case_results(case_id))


# ---------------------------------------------------------------------------
# 测试用结果构造器（虚构数值）
# ---------------------------------------------------------------------------


def make_result(warmth=2.0, engagement=2.0, special=1.5, evidence=2.4, ease=2.3,
                romantic=0.2, distancing=0.2, conf=0.8,
                warmth_probs=None, engagement_probs=None):
    def dist(score):
        lo, hi = int(score // 1), int(-(-score // 1))
        out = {str(i): 0.0 for i in range(5)}
        if lo == hi:
            out[str(lo)] = 1.0
        else:
            frac = score - lo
            out[str(lo)] = 1.0 - frac
            out[str(hi)] = frac
        return out

    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": conf},
        "intent": {"choice": "continue_topic",
                   "probabilities": {"continue_topic": 0.7, "show_care": 0.1,
                                     "other": 0.2},
                   "confidence": conf},
        "warmth": {"score": warmth,
                   "probabilities": warmth_probs or dist(warmth),
                   "confidence": conf},
        "engagement": {"score": engagement,
                       "probabilities": engagement_probs or dist(engagement),
                       "confidence": conf},
        "special_attention": {"score": special, "probabilities": dist(special),
                              "confidence": conf},
        "relationship_evidence_strength": {"score": evidence,
                                           "probabilities": dist(evidence),
                                           "confidence": conf},
        "relational_ease": {"score": ease, "probabilities": dist(ease),
                            "confidence": conf},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
        "model": "fake",
    }


def make_entries(specs, start=0):
    return [{"index": start + i, "speaker": "them", "time": f"22:3{i}",
             "text": f"消息{i}", "result": make_result(**spec)}
            for i, spec in enumerate(specs)]


# ---------------------------------------------------------------------------
# schema / 无综合分数
# ---------------------------------------------------------------------------


def test_profile_schema_shape():
    profile = case_profile("rb_fam_close_no_special")
    assert profile["version"] == "relationship-profile-v2"
    assert profile["schema_version"] == "chat-signal-v3.3"
    assert set(profile["dimensions"]) == set(rp.DIMENSION_ORDER)
    for key in rp.DIMENSION_ORDER:
        dim = profile["dimensions"][key]
        for field in ("key", "label", "status", "conclusion", "strength",
                      "coverage", "direction", "reliability", "supporting_count",
                      "counter_count", "counter_evidence_available", "evidence",
                      "signal", "uncertainty", "facets", "capability",
                      "limitations"):
            assert field in dim, f"{key} 缺少 {field}"
        assert dim["status"] in (rp.STATUS_SUFFICIENT, rp.STATUS_INSUFFICIENT,
                                 rp.STATUS_UNSUPPORTED)
        assert dim["direction"] in (rp.DIR_SUPPORTING, rp.DIR_COUNTER,
                                    rp.DIR_MIXED, rp.DIR_NONE, rp.DIR_UNKNOWN)
        for field in ("eligible_messages", "supporting_messages",
                      "analyzed_messages", "coverage_ratio"):
            assert field in dim["coverage"]
        for field in ("level", "confidence", "proxy", "basis"):
            assert field in dim["reliability"]
        # #19 / #20 预留接口存在且本版本为空
        assert dim["salient_events"] == []
        assert dim["counter_events"] == []
        assert dim["interaction_events"] == []
        assert dim["baseline"] == {}
    assert profile["reserved"]["evidence_source_types"] == list(
        rp.EVIDENCE_SOURCE_TYPES)
    assert profile["reserved"]["salient_events"] == []
    assert profile["reserved"]["interaction_events"] == []


def test_profile_has_no_new_composite_score():
    """禁止六个换皮 overall：不得出现任何新的综合分数字段。"""
    profile = case_profile("rb_rom_expression")
    for banned in ("score", "total", "composite", "index", "overall",
                   "percentile"):
        assert banned not in profile, banned
    for key, dim in profile["dimensions"].items():
        for banned in ("score", "total", "composite", "overall"):
            assert banned not in dim, f"{key} 含 {banned}"
    # overall_legacy 只是透传生产聚合，不是画像计算产物
    legacy = profile["overall_legacy"]
    assert set(legacy) >= {"overall", "recent", "trend", "role", "conflict_note"}
    assert "辅助参考" in legacy["role"]


# ---------------------------------------------------------------------------
# strength / coverage / direction / reliability 语义
# ---------------------------------------------------------------------------


def test_strength_uses_production_stats_and_existing_labels():
    results = case_results("rb_fam_close_no_special")
    stats = scoring.compute_conversation_stats(results)
    profile = build_profile(results, stats=stats)
    fam = profile["dimensions"]["familiarity"]
    assert fam["strength"]["value"] == pytest.approx(
        round(stats["relational_ease_avg"], 4))
    assert fam["strength"]["level"] == \
        scoring.relational_ease_label(stats["relational_ease_avg"])
    care = profile["dimensions"]["care_responsiveness"]
    assert care["strength"]["level"] == \
        scoring.score_level_label(stats["warmth_avg"])


def test_coverage_and_reliability_are_separate():
    entries = make_entries([
        {"warmth": 3.5, "engagement": 3.5, "special": 3.5, "evidence": 3.0,
         "conf": 0.95},
        {"warmth": 2.0, "engagement": 2.0, "special": 1.0, "evidence": 0.5,
         "conf": 0.5},
        {"warmth": 2.0, "engagement": 2.0, "special": 1.0, "evidence": 0.5,
         "conf": 0.5},
    ])
    care = build_profile(entries)["dimensions"]["care_responsiveness"]
    assert care["coverage"]["eligible_messages"] == 1
    assert care["coverage"]["analyzed_messages"] == 3
    assert care["coverage"]["coverage_ratio"] == pytest.approx(1 / 3, abs=1e-4)
    # 1 条高置信消息 ≠ 高可靠：封顶“较低”
    assert care["reliability"]["level"] == rp.CONF_LEVEL_LOW
    assert care["reliability"]["confidence"] == pytest.approx(0.95)


def test_reliability_caps_are_deterministic():
    def profile_for(n_effective, n_low):
        specs = [{"warmth": 3.5, "engagement": 3.0, "special": 3.0,
                  "evidence": 3.0, "conf": 0.9}] * n_effective
        specs += [{"warmth": 2.0, "engagement": 2.0, "special": 1.0,
                   "evidence": 0.5, "conf": 0.9}] * n_low
        return build_profile(make_entries(specs))["dimensions"][
            "care_responsiveness"]

    # 2 条高置信：不高于“中等”（< RELIABILITY_HIGH_MIN_MESSAGES）
    assert profile_for(2, 0)["reliability"]["level"] == rp.CONF_LEVEL_MID
    # 3 条高置信全覆盖：允许“较高”
    assert profile_for(3, 0)["reliability"]["level"] == rp.CONF_LEVEL_HIGH
    # 3 条有效 + 3 条低信息 → 覆盖 0.5，恰好不低于阈值 → “较高”
    assert profile_for(3, 3)["reliability"]["level"] == rp.CONF_LEVEL_HIGH
    # 3 条有效 + 4 条低信息 → 覆盖 < 0.5 → 封顶“中等”
    assert profile_for(3, 4)["reliability"]["level"] == rp.CONF_LEVEL_MID
    # 1 条 → 封顶“较低”
    assert profile_for(1, 5)["reliability"]["level"] == rp.CONF_LEVEL_LOW


def test_low_score_is_not_counter_evidence():
    """absence ≠ counter：special / romantic / withdraw 无反证通道。"""
    entries = make_entries([
        {"special": 0.2, "romantic": 0.05, "distancing": 0.05},
        {"special": 0.4, "romantic": 0.10, "distancing": 0.10},
        {"special": 0.6, "romantic": 0.15, "distancing": 0.15},
    ])
    profile = build_profile(entries)
    for key in ("special_attention", "romantic", "withdrawal"):
        dim = profile["dimensions"][key]
        assert dim["counter_evidence_available"] is False, key
        assert dim["counter_count"] == 0, key
        assert dim["direction"] in (rp.DIR_NONE, rp.DIR_UNKNOWN), key
        assert not any(item["role"] == rp.ROLE_COUNTER
                       for item in dim["evidence"]), key


def test_affirmative_negative_levels_are_counter_evidence():
    """量表负向档（拒绝 / 敷衍 / 冷淡）是可观察行为，允许计为 counter。"""
    entries = make_entries([
        {"warmth": 0.5, "engagement": 0.8, "evidence": 2.0, "ease": 0.5},
        {"warmth": 2.0, "engagement": 1.2, "evidence": 2.0, "ease": 2.0},
        {"warmth": 2.0, "engagement": 2.0, "evidence": 2.0, "ease": 2.0},
    ])
    profile = build_profile(entries)
    care = profile["dimensions"]["care_responsiveness"]
    assert care["counter_count"] == 1  # warmth 0.5 < 1.0（冷淡档）
    assert care["direction"] == rp.DIR_COUNTER
    init = profile["dimensions"]["initiative_engagement"]
    assert init["counter_count"] == 2   # engagement 0.8 / 1.2 < 2.0（敷衍档）
    fam = profile["dimensions"]["familiarity"]
    assert fam["counter_count"] == 1    # ease 0.5 < 1.0（生疏档）
    # 中性 / 纯事务（warmth 2.0）不计反证
    assert all(item["value"] < 1.0 for item in care["evidence"]
               if item["role"] == rp.ROLE_COUNTER)


def test_mixed_direction_is_reported():
    entries = make_entries([
        {"warmth": 3.5, "engagement": 3.5, "evidence": 3.0},
        {"warmth": 0.5, "engagement": 0.5, "evidence": 3.0},
    ])
    care = build_profile(entries)["dimensions"]["care_responsiveness"]
    assert care["direction"] == rp.DIR_MIXED
    assert care["supporting_count"] == 1
    assert care["counter_count"] == 1


# ---------------------------------------------------------------------------
# insufficient / unsupported 是一等状态
# ---------------------------------------------------------------------------


def test_insufficient_is_not_mid_level():
    entries = make_entries([{"warmth": 3.5, "engagement": 3.5,
                             "evidence": 3.0}])  # 仅 1 条有效证据
    profile = build_profile(entries)
    for key in rp.DIMENSION_ORDER:
        if key == "boundary_pressure":
            continue
        dim = profile["dimensions"][key]
        assert dim["status"] == rp.STATUS_INSUFFICIENT, key
        assert "数据不足" in dim["conclusion"], key
        assert dim["direction"] == rp.DIR_UNKNOWN, key
        for mid in ("一般", "较强", "强", "自然熟悉", "中等偏强"):
            assert mid not in dim["conclusion"], key


def test_unsupported_boundary_pressure():
    profile = case_profile("rb_dis_pressure_after_refusal")
    dim = profile["dimensions"]["boundary_pressure"]
    assert dim["status"] == rp.STATUS_UNSUPPORTED
    assert dim["direction"] == rp.DIR_UNKNOWN
    assert dim["strength"]["level"] == "无法判断"
    assert "不支持" in dim["conclusion"]
    assert any("没有足够直接指标" in note for note in dim["limitations"])
    assert any("高信息量 ≠ 正向关系信号" in note for note in dim["limitations"])
    # unsupported 不得显示成“低”
    assert "低" not in dim["conclusion"]
    assert dim["reliability"]["level"] == rp.CONF_LEVEL_NA


# ---------------------------------------------------------------------------
# Noul 双轨与弱信号
# ---------------------------------------------------------------------------


def test_noul_weak_signal_kept_as_trace_not_conclusion():
    entries = make_entries([
        {"romantic": 0.18, "evidence": 2.5},
        {"romantic": 0.22, "evidence": 2.5},
        {"romantic": 0.10, "evidence": 2.5},
    ])
    dim = build_profile(entries)["dimensions"]["romantic"]
    assert dim["conclusion"].startswith("未发现明确证据")
    assert dim["supporting_count"] == 0
    # raw 差异保留在 signal / uncertainty（trace），但不构成结论
    assert dim["signal"]["raw_max"] == pytest.approx(0.22)
    assert dim["uncertainty"]["raw_max"] == pytest.approx(0.22)
    assert not any(item["role"] == rp.ROLE_SUPPORTING for item in dim["evidence"])


def test_noul_weak_band_produces_trace_items():
    entries = make_entries([
        {"romantic": 0.45, "evidence": 2.5},
        {"romantic": 0.10, "evidence": 2.5},
    ])
    dim = build_profile(entries)["dimensions"]["romantic"]
    assert dim["supporting_count"] == 0
    traces = [item for item in dim["evidence"] if item["role"] == rp.ROLE_WEAK_TRACE]
    assert len(traces) == 1
    assert traces[0]["value"] == pytest.approx(0.45)
    assert "不构成结论" in dim["conclusion"]


def test_noul_clear_signal_is_supporting():
    entries = make_entries([
        {"romantic": 0.85, "evidence": 3.5},
        {"romantic": 0.10, "evidence": 2.5},
    ])
    dim = build_profile(entries)["dimensions"]["romantic"]
    assert dim["supporting_count"] == 1
    assert "明确浪漫" in dim["conclusion"]
    assert dim["direction"] == rp.DIR_SUPPORTING


# ---------------------------------------------------------------------------
# 概率不确定性（same-score different-distribution）
# ---------------------------------------------------------------------------


def test_same_score_different_distribution_is_distinguished():
    certain = make_entries([{"warmth": 2.0,
                             "warmth_probs": {"0": 0.0, "1": 0.0, "2": 1.0,
                                              "3": 0.0, "4": 0.0},
                             "evidence": 2.5},
                            {"warmth": 2.0,
                             "warmth_probs": {"0": 0.0, "1": 0.0, "2": 1.0,
                                              "3": 0.0, "4": 0.0},
                             "evidence": 2.5}])
    polar = make_entries([{"warmth": 2.0,
                           "warmth_probs": {"0": 0.5, "1": 0.0, "2": 0.0,
                                            "3": 0.0, "4": 0.5},
                           "evidence": 2.5},
                          {"warmth": 2.0,
                           "warmth_probs": {"0": 0.5, "1": 0.0, "2": 0.0,
                                            "3": 0.0, "4": 0.5},
                           "evidence": 2.5}])
    dim_a = build_profile(certain)["dimensions"]["care_responsiveness"]
    dim_b = build_profile(polar)["dimensions"]["care_responsiveness"]
    assert dim_a["strength"]["value"] == dim_b["strength"]["value"]
    assert dim_a["uncertainty"]["mean_variance"] == pytest.approx(0.0)
    assert dim_b["uncertainty"]["mean_variance"] == pytest.approx(4.0)
    assert dim_a["uncertainty"]["mean_entropy_bits"] == pytest.approx(0.0)
    assert dim_b["uncertainty"]["mean_entropy_bits"] == pytest.approx(1.0)


def test_distribution_math_is_exact():
    assert rp.distribution_variance({"2": 1.0}) == pytest.approx(0.0)
    assert rp.distribution_variance({"0": 0.5, "4": 0.5}) == pytest.approx(4.0)
    assert rp.distribution_entropy_bits({"2": 1.0}) == pytest.approx(0.0)
    assert rp.distribution_entropy_bits({"0": 0.5, "4": 0.5}) == \
        pytest.approx(1.0)
    assert rp.distribution_variance({}) is None
    assert rp.distribution_entropy_bits({"a": 1.0}) is not None  # Choice 也可用


# ---------------------------------------------------------------------------
# 缺字段 / 旧缓存降级
# ---------------------------------------------------------------------------


V1_RESULT = {
    "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                "confidence": 0.8},
    "intent": {"choice": "continue_topic",
               "probabilities": {"continue_topic": 1.0}, "confidence": 0.8},
    "warmth": {"score": 3.0, "probabilities": {}, "confidence": 0.8},
    "engagement": {"score": 3.0, "probabilities": {}, "confidence": 0.8},
    "special_attention": {"score": 2.0, "probabilities": {}, "confidence": 0.8},
    "romantic_signal": 0.1,
    "distancing_signal": 0.1,
}


def test_old_cache_results_degrade_without_crash():
    entries = [{"index": 0, "result": V1_RESULT},
               {"index": 1, "result": make_result(evidence=3.0)}]
    profile = build_profile(entries)
    assert any("旧缓存" in note for note in profile["limitations"])


def test_all_missing_fields_yields_insufficient_everywhere():
    entries = [{"index": i, "result": V1_RESULT} for i in range(3)]
    profile = build_profile(entries)
    for key in rp.DIMENSION_ORDER:
        if key == "boundary_pressure":
            continue
        assert profile["dimensions"][key]["status"] == rp.STATUS_INSUFFICIENT
    assert profile["overall_legacy"]["overall"] is None


def test_missing_probabilities_degrades_uncertainty():
    result = make_result()
    for dim in ("warmth", "engagement", "special_attention",
                "relationship_evidence_strength", "relational_ease"):
        result[dim]["probabilities"] = {}
    entries = [{"index": 0, "result": result},
               {"index": 1, "result": result}]
    dim = build_profile(entries)["dimensions"]["care_responsiveness"]
    assert dim["uncertainty"]["available"] is False
    assert "旧缓存" in dim["uncertainty"]["note"]


def test_error_entries_are_excluded():
    entries = [{"index": 0, "error": "boom"},
               {"index": 1, "result": make_result(evidence=3.0)},
               {"index": 2, "result": make_result(evidence=3.0)}]
    profile = build_profile(entries)
    care = profile["dimensions"]["care_responsiveness"]
    assert care["coverage"]["analyzed_messages"] == 2


# ---------------------------------------------------------------------------
# 可追溯性 / 确定性 / 文案
# ---------------------------------------------------------------------------


def test_evidence_items_are_traceable():
    entries = make_entries([
        {"warmth": 3.5, "engagement": 3.4, "special": 3.2, "evidence": 3.0},
        {"warmth": 2.0, "engagement": 2.0, "special": 1.0, "evidence": 2.0},
    ])
    profile = build_profile(entries)
    for key in rp.DIMENSION_ORDER:
        for item in profile["dimensions"][key]["evidence"]:
            assert isinstance(item["message_index"], int)
            assert item["metric"]
            assert item["value"] is not None
            assert item["reason"]
            assert item["source"] in rp.EVIDENCE_SOURCE_TYPES
            assert item["role"] in (rp.ROLE_SUPPORTING, rp.ROLE_COUNTER,
                                    rp.ROLE_WEAK_TRACE)
    care = profile["dimensions"]["care_responsiveness"]
    assert care["evidence"][0]["message_index"] == 0
    assert care["evidence"][0]["metric"] == "warmth"


def test_profile_is_deterministic():
    results = case_results("rb_special_only_you")
    a = json.dumps(build_profile(results), sort_keys=True)
    b = json.dumps(build_profile(results), sort_keys=True)
    assert a == b


def test_summary_and_conclusions_have_no_mind_reading_wording():
    for case_id in ("rb_fam_close_no_special", "rb_rom_expression",
                    "rb_dis_pressure_after_refusal", "rb_neg_health_polite"):
        profile = case_profile(case_id)
        text = profile["summary"]["text"] + "".join(
            dim["conclusion"] for dim in profile["dimensions"].values())
        for banned in ("喜欢概率", "恋爱可能性", "关系健康度", "心理",
                       "真实想法", "肯定喜欢", "爱", "人格"):
            assert banned not in text, f"{case_id}: {banned}"


def test_profile_does_not_modify_scoring_constants():
    before = {name: getattr(scoring, name) for name in (
        "WEIGHT_WARMTH", "WEIGHT_ENGAGEMENT", "WEIGHT_SPECIAL_ATTENTION",
        "WEIGHT_ROMANTIC", "WEIGHT_DISTANCING_PENALTY", "NOUL_NOISE_FLOOR",
        "NOUL_STRONG_MARK", "NOUL_MID_GAIN")}
    case_profile("rb_rom_expression")
    after = {name: getattr(scoring, name) for name in before}
    assert before == after
    assert after["WEIGHT_WARMTH"] == 0.30
    assert after["NOUL_NOISE_FLOOR"] == 0.30


# ---------------------------------------------------------------------------
# #17 relationship benchmark 语义不变量 A~I
# ---------------------------------------------------------------------------


class TestBenchmarkInvariants:
    def test_a_high_familiarity_low_romantic(self):
        for case_id in ("rb_fam_close_no_special", "rb_rom_familiar_no_romance"):
            profile = case_profile(case_id)
            fam = profile["dimensions"]["familiarity"]
            romantic = profile["dimensions"]["romantic"]
            assert fam["status"] == rp.STATUS_SUFFICIENT
            assert fam["strength"]["value"] >= rp.SUPPORT_MIN
            assert fam["supporting_count"] >= 1
            assert romantic["supporting_count"] == 0
            assert romantic["conclusion"].startswith("未发现明确证据")
            assert "互动熟悉度高不代表特殊关注或浪漫信号。" \
                in profile["summary"]["lines"]

    def test_b_high_engagement_low_special(self):
        for case_id in ("rb_neg_group_invite", "rb_init_topic"):
            profile = case_profile(case_id)
            init = profile["dimensions"]["initiative_engagement"]
            special = profile["dimensions"]["special_attention"]
            assert init["supporting_count"] >= 1, case_id
            assert special["supporting_count"] == 0, case_id
            if special["strength"]["value"] is not None:
                assert special["strength"]["value"] < 2.0, case_id
            assert "未发现明确的特别关注证据" in special["conclusion"], case_id

    def test_c_special_present_romantic_insufficient(self):
        profile = case_profile("rb_special_only_you")
        special = profile["dimensions"]["special_attention"]
        romantic = profile["dimensions"]["romantic"]
        assert special["supporting_count"] >= 1
        assert special["direction"] == rp.DIR_SUPPORTING
        assert romantic["conclusion"].startswith("未发现明确证据")
        assert "特殊关注不自动等于浪漫信号。" in profile["summary"]["lines"]

    def test_d_explicit_romantic_differs_from_teasing(self):
        explicit = case_profile("rb_rom_expression")["dimensions"]["romantic"]
        teasing = case_profile("rb_rom_teasing_flirt")["dimensions"]["romantic"]
        friendly = case_profile("rb_fam_mutual_teasing")["dimensions"]["romantic"]
        assert explicit["supporting_count"] >= 1
        assert "明确浪漫" in explicit["conclusion"]
        for other in (teasing, friendly):
            assert other["supporting_count"] == 0
            assert other["conclusion"].startswith("未发现明确证据")
        assert explicit["conclusion"] != teasing["conclusion"]

    def test_e_natural_closing_is_not_withdrawal(self):
        dim = case_profile("rb_dis_topic_close")["dimensions"]["withdrawal"]
        assert dim["supporting_count"] == 0
        assert "明确" not in dim["conclusion"] or dim["conclusion"].startswith(
            "未发现明确证据")
        assert "疏离明显" not in dim["conclusion"]

    def test_f_low_engagement_is_not_withdrawal(self):
        profile = case_profile("rb_dis_low_investment")
        withdrawal = profile["dimensions"]["withdrawal"]
        assert withdrawal["supporting_count"] == 0
        assert withdrawal["conclusion"] in (
            "未发现明确证据", "数据不足，无法判断（有效证据不足 2 条）",
            "未发现明确证据；存在弱信号痕迹（不构成结论，仅保留 raw 差异供诊断）")
        assert "疏离" not in withdrawal["conclusion"]

    def test_g_polite_care_does_not_upgrade(self):
        profile = case_profile("rb_neg_health_polite")
        care = profile["dimensions"]["care_responsiveness"]
        special = profile["dimensions"]["special_attention"]
        romantic = profile["dimensions"]["romantic"]
        assert care["supporting_count"] >= 1  # 关心被识别
        assert special["supporting_count"] == 0
        # 加权均值可能因 Σweight 不足为 None（生产既有行为）；有值时必须弱
        if special["strength"]["value"] is not None:
            assert special["strength"]["value"] < 2.0
        assert romantic["conclusion"].startswith("未发现明确证据")

    def test_h_d5_pressure_not_shown_as_positive(self):
        profile = case_profile("rb_dis_pressure_after_refusal")
        boundary = profile["dimensions"]["boundary_pressure"]
        assert boundary["status"] == rp.STATUS_UNSUPPORTED
        assert "不支持" in boundary["conclusion"]
        assert any("高信息量 ≠ 正向关系信号" in n for n in boundary["limitations"])
        # legacy base_score 偏高（63.6 > 48.3）不得产出正向结论
        assert profile["overall_legacy"]["overall"] > 60
        text = profile["summary"]["text"] + "".join(
            d["conclusion"] for d in profile["dimensions"].values())
        for banned in ("更强", "更好", "更健康", "关系健康", "尊重边界"):
            assert banned not in text, banned
        # 对照：尊重边界的一侧也不因 legacy 较低被写成负向结论
        respect = case_profile("rb_dis_respect_boundary")
        assert respect["dimensions"]["boundary_pressure"]["status"] \
            == rp.STATUS_UNSUPPORTED

    def test_i_insufficient_cases_show_insufficient(self):
        for case_id in ("rb_fam_smalltalk", "rb_dis_low_investment"):
            profile = case_profile(case_id)
            for key in rp.DIMENSION_ORDER:
                if key == "boundary_pressure":
                    continue
                dim = profile["dimensions"][key]
                assert dim["status"] == rp.STATUS_INSUFFICIENT, (case_id, key)
                assert "数据不足" in dim["conclusion"], (case_id, key)
