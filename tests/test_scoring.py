"""scoring v2 测试。"""

import pytest

import scoring
from scoring import (
    INTENT_PROFILE_LABELS,
    TREND_LABELS,
    compute_conversation_stats,
    confidence_label,
    evidence_level_label,
    message_metrics,
    noul_label,
    total_evidence_label,
    transform_noul_evidence,
)


def make_result(warmth=2.0, engagement=2.0, special=1.0,
                romantic=0.1, distancing=0.1, evidence=2.0,
                intent_probs=None, conf=0.8, econf=0.8):
    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0}, "confidence": 0.8},
        "intent": {
            "choice": "continue_topic",
            "probabilities": intent_probs or {"continue_topic": 1.0},
            "confidence": 0.8,
        },
        "warmth": {"score": warmth, "probabilities": {}, "confidence": conf},
        "engagement": {"score": engagement, "probabilities": {}, "confidence": conf},
        "special_attention": {"score": special, "probabilities": {}, "confidence": conf},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
        "relationship_evidence_strength": {
            "score": evidence, "probabilities": {}, "confidence": econf
        },
    }


def make_entry(result=None, error=None, index=0):
    if error:
        return {"index": index, "error": error}
    return {"index": index, "result": result if result is not None else make_result()}


# ---------------------------------------------------------------------------
# Noul → evidence 转换
# ---------------------------------------------------------------------------


def test_transform_noul_noise_floor():
    assert transform_noul_evidence(0.10) == 0.0
    assert transform_noul_evidence(0.30) == 0.0
    assert transform_noul_evidence(0.20) == 0.0  # distancing=0.20 场景
    assert transform_noul_evidence(None) == 0.0


def test_transform_noul_mid_range():
    assert transform_noul_evidence(0.50) == pytest.approx(0.175, abs=1e-6)
    assert 0.0 < transform_noul_evidence(0.40) < transform_noul_evidence(0.50)


def test_transform_noul_strong_range():
    assert transform_noul_evidence(0.70) == pytest.approx(0.35, abs=1e-6)
    assert transform_noul_evidence(0.85) == pytest.approx(0.675, abs=1e-6)
    assert transform_noul_evidence(0.90) > 0.6  # 明显证据
    assert transform_noul_evidence(1.00) == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# message_weight
# ---------------------------------------------------------------------------


def test_weight_zero_when_evidence_zero():
    m = message_metrics(make_entry(make_result(evidence=0.0)))
    assert m["weight"] == 0.0


def test_weight_high_when_evidence_and_confidence_high():
    m = message_metrics(make_entry(make_result(evidence=4.0, econf=0.95, conf=0.95)))
    assert m["weight"] == pytest.approx(0.95, abs=1e-6)


def test_weight_uses_mean_of_three_confidences():
    r = make_result(evidence=4.0)
    r["warmth"]["confidence"] = 0.9
    r["engagement"]["confidence"] = 0.6
    r["special_attention"]["confidence"] = 0.9
    m = message_metrics(make_entry(r))
    assert m["relation_confidence"] == pytest.approx(0.8, abs=1e-6)
    assert m["weight"] == pytest.approx(0.8, abs=1e-6)


def test_missing_confidence_falls_back_with_warning():
    r = make_result(evidence=4.0)
    r["warmth"]["confidence"] = None
    m = message_metrics(make_entry(r))
    assert m is not None
    assert m["relation_confidence"] == pytest.approx((0.5 + 0.8 + 0.8) / 3, abs=1e-6)
    assert any("confidence" in w for w in m["warnings"])


# ---------------------------------------------------------------------------
# 低信息量短回复不稀释总体
# ---------------------------------------------------------------------------


def test_low_evidence_messages_do_not_dilute_overall():
    # 10 条“哦/嗯/哈哈”级别消息 + 1 条高 evidence、高亲近消息
    lows = [
        make_entry(make_result(warmth=1.0, engagement=1.0, special=1.0,
                               romantic=0.1, distancing=0.1,
                               evidence=0.2, conf=0.8), index=i)
        for i in range(10)
    ]
    high = make_entry(make_result(warmth=4.0, engagement=4.0, special=4.0,
                                  romantic=0.95, distancing=0.05,
                                  evidence=4.0, conf=0.9), index=10)
    stats = compute_conversation_stats(lows + [high])
    # 旧等权算法会给出约 (10*低分 + 100)/11 的稀释结果；新算法应接近强信号消息
    assert stats["overall"] is not None
    assert stats["overall"] > 70.0
    assert stats["effective_messages"] == 1
    # 大量低 evidence 消息的权重应接近 0
    low_metrics = [message_metrics(e) for e in lows]
    assert all(m["weight"] < 0.1 for m in low_metrics)


def test_base_score_uses_transformed_not_raw_noul():
    # romantic 原始概率 0.2 → evidence 0 → base_score 不受影响
    m = message_metrics(make_entry(make_result(
        warmth=1.0, engagement=1.0, special=1.0, romantic=0.20, distancing=0.20,
        evidence=3.0,
    )))
    expected = 0.30 * 0.25 + 0.25 * 0.25 + 0.25 * 0.25  # 无 romantic 加分、无 distancing 扣分
    assert m["base_score"] == pytest.approx(expected, abs=1e-6)
    assert m["romantic_ev"] == 0.0 and m["distancing_ev"] == 0.0


# ---------------------------------------------------------------------------
# 有效证据不足
# ---------------------------------------------------------------------------


def test_insufficient_total_weight_returns_none():
    entries = [make_entry(make_result(evidence=0.3, conf=0.8), index=i) for i in range(3)]
    stats = compute_conversation_stats(entries)
    assert stats["overall"] is None
    assert stats["overall_sufficient"] is False
    assert stats["trend"] == "insufficient_samples"  # 3 < 6


def test_all_failed_returns_empty_stats():
    stats = compute_conversation_stats([{"index": 0, "error": "x"}])
    assert stats["overall"] is None
    assert stats["analyzed"] == 0
    assert stats["failed"] == 1


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------


def _half_entry(base, index):
    """构造指定 base_score、且半段权重足够的消息。

    三项 Score 同分 w 时：base = 0.8*(w/4) → w = base*4/0.8；
    evidence=2, conf=0.9 → 单条 weight=0.45，3 条半段 Σw=1.35 >= 0.5。
    """
    w = base * 4 / 0.8
    return make_entry(make_result(warmth=w, engagement=w, special=w,
                                  romantic=0.0, distancing=0.0,
                                  evidence=2.0, conf=0.9), index=index)


def test_trend_insufficient_samples_below_six():
    entries = [_half_entry(0.5, i) for i in range(5)]
    stats = compute_conversation_stats(entries)
    assert stats["trend"] == "insufficient_samples"


def test_trend_up_and_down():
    up = [_half_entry(0.2, i) for i in range(3)] + [_half_entry(0.9, i + 3) for i in range(3)]
    stats = compute_conversation_stats(up)
    assert stats["trend"] == "up"
    assert stats["second_half"] - stats["first_half"] >= 8.0

    down = [_half_entry(0.9, i) for i in range(3)] + [_half_entry(0.2, i + 3) for i in range(3)]
    stats = compute_conversation_stats(down)
    assert stats["trend"] == "down"


def test_trend_flat_and_insufficient_evidence():
    flat = [_half_entry(0.5, i) for i in range(6)]
    assert compute_conversation_stats(flat)["trend"] == "flat"

    # 前半段全部 evidence=0 → 半段权重不足
    low = [make_entry(make_result(evidence=0.0), index=i) for i in range(3)]
    ok = [_half_entry(0.5, i + 3) for i in range(3)]
    assert compute_conversation_stats(low + ok)["trend"] == "insufficient_evidence"


# ---------------------------------------------------------------------------
# conversation-level intent 行为统计（概率分布加权，非 top-1 计数）
# ---------------------------------------------------------------------------


def test_intent_profiles_use_distribution_and_weight():
    # 消息 A：weight 高，ask=0.6 / tease=0.4；消息 B：weight 低，ask=0.0 / tease=1.0
    a = make_entry(make_result(evidence=4.0, conf=0.9, econf=0.9,
                               intent_probs={"ask_information": 0.6, "tease": 0.4}), index=0)
    b = make_entry(make_result(evidence=0.5, conf=0.9, econf=0.9,
                               intent_probs={"ask_information": 0.0, "tease": 1.0}), index=1)
    stats = compute_conversation_stats([a, b])
    profiles = stats["intent_profiles"]
    # A 权重 = 1.0*0.9 = 0.9，B 权重 = 0.125*0.9 = 0.1125
    w_a, w_b = 0.9, 0.1125
    total = w_a + w_b
    # 使用完整概率分布加权，而非 top-1 计数
    assert profiles["ask_information"] == pytest.approx(0.6 * w_a / total, abs=1e-6)
    assert profiles["tease"] == pytest.approx((0.4 * w_a + 1.0 * w_b) / total, abs=1e-6)
    assert set(profiles) == set(INTENT_PROFILE_LABELS)


def test_intent_profiles_empty_when_weight_insufficient():
    entries = [make_entry(make_result(evidence=0.2), index=i) for i in range(2)]
    assert compute_conversation_stats(entries)["intent_profiles"] == {}


# ---------------------------------------------------------------------------
# 三项 Score 加权均值（不被低信息量消息拉低）
# ---------------------------------------------------------------------------


def test_score_averages_are_weighted():
    high = make_entry(make_result(warmth=4.0, engagement=4.0, special=4.0,
                                  evidence=4.0, conf=0.95, econf=0.95), index=0)
    low = make_entry(make_result(warmth=0.0, engagement=0.0, special=0.0,
                                 evidence=0.2, conf=0.95, econf=0.95), index=1)
    stats = compute_conversation_stats([high, low])
    assert stats["warmth_avg"] > 3.5
    assert stats["engagement_avg"] > 3.5
    assert stats["special_attention_avg"] > 3.5


# ---------------------------------------------------------------------------
# 标签与配置
# ---------------------------------------------------------------------------


def test_evidence_level_labels():
    assert evidence_level_label(0.10) == "未发现明显信号"
    assert evidence_level_label(0.20) == "存在少量弱信号"
    assert evidence_level_label(0.50) == "存在一定信号"
    assert evidence_level_label(0.70) == "存在较明显信号"
    assert evidence_level_label(0.90) == "存在强信号"
    assert evidence_level_label(None) == "有效信息不足"


def test_total_evidence_label():
    assert total_evidence_label(4.0) == "较高"
    assert total_evidence_label(1.5) == "中等"
    assert total_evidence_label(0.2) == "较低"


def test_trend_labels_cover_all_codes():
    for code in ("up", "flat", "down", "insufficient_samples", "insufficient_evidence"):
        assert code in TREND_LABELS


def test_confidence_labels():
    assert confidence_label(0.85) == "结论较明确"
    assert confidence_label(0.60) == "存在一定歧义"
    assert confidence_label(0.30) == "难以判断"
    assert confidence_label(None) == "难以判断"


def test_noul_labels():
    assert noul_label(0.85) == "明显存在该信号"
    assert noul_label(0.50) == "不确定"
    assert noul_label(0.10) == "缺乏明显该信号"


def test_weights_are_normalized_anchors():
    # 权重集中配置，防止被无意改动
    assert scoring.WEIGHT_WARMTH == 0.30
    assert scoring.WEIGHT_ENGAGEMENT == 0.25
    assert scoring.WEIGHT_SPECIAL_ATTENTION == 0.25
    assert scoring.WEIGHT_ROMANTIC == 0.20
    assert scoring.WEIGHT_DISTANCING_PENALTY == 0.15


# ---------------------------------------------------------------------------
# 旧 v1 结果（无 evidence 字段）兼容性
# ---------------------------------------------------------------------------


def test_v1_shaped_result_returns_none_metrics():
    v1_result = {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0}, "confidence": 0.8},
        "intent": {"choice": "continue_topic", "probabilities": {"continue_topic": 1.0}, "confidence": 0.8},
        "warmth": {"score": 3.0, "probabilities": {}, "confidence": 0.8},
        "engagement": {"score": 3.0, "probabilities": {}, "confidence": 0.8},
        "special_attention": {"score": 2.0, "probabilities": {}, "confidence": 0.8},
        "romantic_signal": 0.1,
        "distancing_signal": 0.1,
        # 没有 relationship_evidence_strength 字段（v1 缓存形状）
    }
    assert message_metrics(make_entry(v1_result)) is None
    stats = compute_conversation_stats([make_entry(v1_result)])
    assert stats["overall"] is None
    assert stats["analyzed"] == 0  # 不计入 v2 聚合
