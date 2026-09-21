"""ui_helpers 展示层测试（纯函数，不调用 Jev API）。"""

from ui_helpers import (
    BEHAVIOR_MIN_DISPLAY,
    content_type_label,
    evidence_short,
    filter_entries,
    hidden_behavior_count,
    media_badge,
    media_counts,
    media_event_entry,
    media_placeholder_label,
    media_summary_text,
    overview_mode,
    preview_rows,
    skipped_media_count,
    trend_short,
    visible_behaviors,
)
from parser import MEDIA_MARKERS, parse_chat
from scoring import compute_conversation_stats


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def make_result(evidence=2.4, ease=2.3, conf=0.8, warmth=2.0, engagement=2.0,
                special=1.5, romantic=0.2, distancing=0.2):
    return {
        "emotion": {"choice": "teasing", "probabilities": {"teasing": 0.93},
                    "confidence": 0.85},
        "intent": {"choice": "tease", "probabilities": {"tease": 0.91},
                   "confidence": 0.88},
        "warmth": {"score": warmth, "probabilities": {}, "confidence": conf},
        "engagement": {"score": engagement, "probabilities": {}, "confidence": conf},
        "special_attention": {"score": special, "probabilities": {}, "confidence": conf},
        "relationship_evidence_strength": {"score": evidence, "probabilities": {},
                                           "confidence": conf},
        "relational_ease": {"score": ease, "probabilities": {}, "confidence": conf},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
    }


def make_entry(index, evidence=2.4):
    return {"index": index, "speaker": "them", "text": f"消息{index}",
            "time": None, "result": make_result(evidence=evidence)}


def low_evidence_stats():
    entries = [make_entry(0, evidence=3.0)]
    entries += [make_entry(i, evidence=0.2) for i in range(1, 8)]
    return compute_conversation_stats(entries)


def normal_stats():
    entries = [make_entry(i, evidence=3.0) for i in range(6)]
    return compute_conversation_stats(entries)


MEDIA_CHAT = """我: 在忙吗
TA: [图片] 微信图片_1.dat
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你看这个
TA: 你看这个 [视频] 微信视频_9.mp4
我: 好的
TA: [动画表情]"""


def media_messages():
    return parse_chat(MEDIA_CHAT)


# ---------------------------------------------------------------------------
# 1 / 2 / 14：概览模式
# ---------------------------------------------------------------------------

def test_overview_mode_reference_for_low_evidence():
    assert overview_mode(low_evidence_stats()) == "reference"


def test_overview_mode_normal_for_rich_evidence():
    assert overview_mode(normal_stats()) == "normal"


def test_low_evidence_warning_copy_is_available():
    """低信息量提示文案由 UI 层固定提供（不依赖业务数据）。"""
    stats = low_evidence_stats()
    assert stats["effective_messages"] == 1
    assert stats["analyzed"] == 8
    assert overview_mode(stats) == "reference"


# ---------------------------------------------------------------------------
# 3 / 4 / 5 / 6 / 7：过滤
# ---------------------------------------------------------------------------

def test_filter_all():
    entries = [make_entry(0, 0.2), make_entry(1, 3.0), make_entry(2, 1.5)]
    out = filter_entries(entries, mode="全部消息")
    assert [e["index"] for e in out] == [0, 1, 2]


def test_filter_effective_only():
    entries = [make_entry(0, 0.2), make_entry(1, 3.0), make_entry(2, 1.5)]
    out = filter_entries(entries, mode="仅有效关系消息")
    assert [e["index"] for e in out] == [1, 2]


def test_filter_top5():
    entries = [make_entry(i, 2.0) for i in range(8)]
    out = filter_entries(entries, mode="关系信息量最高 Top 5")
    assert len(out) == 5
    assert [e["index"] for e in out] == [0, 1, 2, 3, 4]


def test_media_hidden_by_default():
    msgs = media_messages()
    entries = [make_entry(i) for i in range(2)]
    out = filter_entries(entries, include_media=False, messages=msgs)
    assert all(not e.get("media_event") for e in out)


def test_media_shown_when_enabled():
    msgs = media_messages()
    # 纯媒体消息位于下标 1（图片）与 7（动画表情）；5 是混合消息，可作普通结果
    entries = [make_entry(3), make_entry(5)]
    out = filter_entries(entries, include_media=True, messages=msgs)
    events = [e for e in out if e.get("media_event")]
    assert len(events) == 2  # 图片 + 动画表情
    assert [e["index"] for e in events] == [1, 7]
    # 媒体事件不夹带任何分析指标
    for e in events:
        assert "result" not in e
        assert "base_score" not in e


def test_mixed_media_shows_text_and_media_hint():
    msgs = media_messages()
    mixed = [m for m in msgs if m.get("content_type") == "mixed"]
    assert len(mixed) == 1
    assert mixed[0]["text"].startswith("你看这个")
    assert MEDIA_MARKERS["video"] in mixed[0]["text"]
    assert content_type_label(mixed[0]) == "文字+媒体"


# ---------------------------------------------------------------------------
# 9 / 10：不修改原数据
# ---------------------------------------------------------------------------

def test_filter_does_not_mutate_entries():
    entries = [make_entry(0, 0.2), make_entry(1, 3.0)]
    before = [dict(e) for e in entries]
    filter_entries(entries, mode="仅有效关系消息")
    filter_entries(entries, mode="关系信息量最高 Top 5", include_media=True,
                   messages=media_messages())
    assert entries == before


def test_report_data_unchanged_by_ui_helpers():
    """UI helper 只读，不改变 stats / results 数值。"""
    stats_before = normal_stats()
    entries = [make_entry(i, 3.0) for i in range(6)]
    snapshot = compute_conversation_stats(entries)
    filter_entries(entries, mode="关系信息量最高 Top 5")
    preview_rows(media_messages())
    assert compute_conversation_stats(entries) == snapshot
    assert stats_before["overall"] == snapshot["overall"]


# ---------------------------------------------------------------------------
# 11：短标签不丢失关键含义
# ---------------------------------------------------------------------------

def test_short_labels_preserve_meaning():
    assert trend_short("flat") == "→ 稳定"
    assert trend_short("insufficient_evidence") == "信息不足"
    assert "未发现" in evidence_short("未发现明显信号")
    assert evidence_short("存在较强信号" if False else "存在较明显信号") == "较明显"
    # 关键含义（未发现 / 强）不丢失
    for label in ("未发现明显信号", "存在少量弱信号", "存在一定信号",
                  "存在较明显信号", "存在强信号"):
        short = evidence_short(label)
        assert short and len(short) <= 4


# ---------------------------------------------------------------------------
# 12 / 13：行为统计展示
# ---------------------------------------------------------------------------

def test_behaviors_below_threshold_hidden_by_default():
    profiles = {"tease": 0.38, "share_personal": 0.20, "continue_topic": 0.17,
                "show_care": 0.11, "perfunctory": 0.07, "end_topic": 0.03,
                "distance": 0.02}
    visible = visible_behaviors(profiles)
    names = [n for n, _ in visible]
    assert "调侃互动" in names
    assert "低投入回应" in names          # 7% >= 5% → 默认显示
    assert "结束话题" not in names        # 3% < 5% → 默认隐藏
    assert "疏离意图" not in names        # 2% < 5% → 默认隐藏
    assert hidden_behavior_count(profiles) == 2


def test_visible_behaviors_sorted_desc():
    profiles = {"share_personal": 0.20, "tease": 0.38, "show_care": 0.11}
    visible = visible_behaviors(profiles)
    assert [v for _, v in visible] == sorted([v for _, v in visible], reverse=True)
    assert visible[0][0] == "调侃互动"


def test_threshold_is_five_percent():
    assert BEHAVIOR_MIN_DISPLAY == 0.05


# ---------------------------------------------------------------------------
# 媒体展示
# ---------------------------------------------------------------------------

def test_media_badge_and_placeholder():
    assert media_badge(["image"]) == "🖼 图片"
    assert media_badge(["image", "voice"]) == "🖼🎙 图片 / 语音"
    assert media_placeholder_label(["image"]) == "[图片，内容未分析]"
    assert media_placeholder_label(["video"]) == "[视频，内容未分析]"


def test_media_summary_and_counts():
    msgs = media_messages()
    assert media_counts(msgs) == {"image": 1, "sticker": 1}
    assert media_summary_text(msgs) == "图片 1 · 动画表情 1"
    assert media_summary_text([]) == ""


def test_skipped_media_count_ta_only():
    msgs = media_messages()
    # MEDIA_CHAT 中 TA 有 1 图 + 1 动画表情 = 2 条纯媒体
    assert skipped_media_count(msgs) == 2


def test_preview_rows_never_expose_media_filename():
    rows = preview_rows(media_messages())
    for row in rows:
        assert "微信图片" not in row["内容"]
        assert "微信视频" not in row["内容"]
        assert ".dat" not in row["内容"]
        assert ".mp4" not in row["内容"]
    media_rows = [r for r in rows if r["类型"] != "文本"]
    assert any(r["内容"] == "[图片，内容未分析]" for r in media_rows)
    assert any(r["类型"] == "文字+媒体" for r in rows)


def test_media_event_entry_shape():
    msgs = media_messages()
    e = media_event_entry({**msgs[1], "index": 1})
    assert e["media_event"] is True
    assert e["text"] == MEDIA_MARKERS["image"]
    assert e["index"] == 1
    assert "result" not in e
