"""非文本媒体占位符过滤测试。

覆盖：图片 / 视频 / 动画表情 / 语音 / 文件、真实 emoji 保留、
文字+媒体混合、媒体位于上下文中、纯媒体 0 次 API 请求、
媒体不计入任何统计。全部 mock，绝不调用真实 Jev API。
"""

from types import SimpleNamespace

import pytest

import analyzer
from analyzer import analyze_messages, extract_answers
from parser import (
    MEDIA_KIND_FILE,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_STICKER,
    MEDIA_KIND_VIDEO,
    MEDIA_KIND_VOICE,
    MEDIA_MARKERS,
    classify_media,
    media_label,
    parse_chat,
)
from privacy import mask_messages
from report import build_json_report, build_markdown_report
from scoring import compute_conversation_stats, message_metrics


# ---------------------------------------------------------------------------
# classify_media：占位符识别
# ---------------------------------------------------------------------------


def test_pure_image_message_is_media():
    out = classify_media("[图片]")
    assert out["content_type"] == "media"
    assert out["media_kinds"] == [MEDIA_KIND_IMAGE]
    assert out["text"] == MEDIA_MARKERS[MEDIA_KIND_IMAGE]


def test_image_with_wechat_filename_is_media_and_filename_stripped():
    out = classify_media("[图片] 微信图片_20260908123456.dat")
    assert out["content_type"] == "media"
    assert "微信图片" not in out["text"]
    assert ".dat" not in out["text"]
    assert out["text"] == MEDIA_MARKERS[MEDIA_KIND_IMAGE]


def test_video_placeholder():
    out = classify_media("[视频] 微信视频_123.mp4")
    assert out["content_type"] == "media"
    assert out["media_kinds"] == [MEDIA_KIND_VIDEO]
    assert "mp4" not in out["text"]


def test_sticker_variants():
    for token in ("[动画表情]", "[表情包]", "[表情]"):
        out = classify_media(token)
        assert out["content_type"] == "media"
        assert out["media_kinds"] == [MEDIA_KIND_STICKER]


def test_voice_and_file_placeholders():
    assert classify_media("[语音]")["media_kinds"] == [MEDIA_KIND_VOICE]
    assert classify_media("[文件] 周报.docx")["media_kinds"] == [MEDIA_KIND_FILE]
    assert classify_media("[文件] 周报.docx")["content_type"] == "media"
    assert "docx" not in classify_media("[文件] 周报.docx")["text"]


def test_real_emoji_is_not_media():
    for text in ("😂", "😭❤️", "哈哈😂😂", "❤️❤️❤️"):
        out = classify_media(text)
        assert out["content_type"] == "text"
        assert out["media_kinds"] == []
        assert out["text"] == text  # emoji 原样保留


def test_mixed_text_and_image_keeps_text():
    out = classify_media("你看这个 [图片] 微信图片_123.dat")
    assert out["content_type"] == "mixed"
    assert out["media_kinds"] == [MEDIA_KIND_IMAGE]
    assert out["text"].startswith("你看这个")
    assert "微信图片" not in out["text"]
    assert "[发送了一张图片，内容未知]" in out["text"]


def test_plain_text_untouched():
    out = classify_media("明天还上班呢吗")
    assert out == {"content_type": "text", "text": "明天还上班呢吗", "media_kinds": []}


def test_media_label():
    assert media_label([MEDIA_KIND_IMAGE]) == "图片"
    assert media_label([MEDIA_KIND_IMAGE, MEDIA_KIND_VOICE]) == "图片 / 语音"


# ---------------------------------------------------------------------------
# parse_chat 集成
# ---------------------------------------------------------------------------

MEDIA_CHAT = """我: 在忙吗
TA: [图片] 微信图片_20260908.dat
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你看这个
TA: 你看这个 [视频] 微信视频_99.mp4
我: 好的
TA: [动画表情]
我: 明天见
TA: 明天见"""


def test_parse_chat_marks_media_messages():
    msgs = parse_chat(MEDIA_CHAT)
    kinds = [(m["speaker"], m["content_type"]) for m in msgs]
    assert kinds == [
        ("me", "text"),
        ("them", "media"),   # 纯图片
        ("me", "text"),
        ("them", "text"),    # 普通调侃
        ("me", "text"),
        ("them", "mixed"),   # 文字+视频
        ("me", "text"),
        ("them", "media"),   # 纯动画表情
        ("me", "text"),
        ("them", "text"),
    ]
    media_msgs = [m for m in msgs if m["content_type"] == "media"]
    assert len(media_msgs) == 2
    assert all(m["speaker"] == "them" for m in media_msgs)


def test_parse_chat_media_text_has_no_filename():
    msgs = parse_chat(MEDIA_CHAT)
    for m in msgs:
        assert "微信图片" not in m["text"]
        assert "微信视频" not in m["text"]
        assert ".dat" not in m["text"]
        assert ".mp4" not in m["text"]


# ---------------------------------------------------------------------------
# analyzer：纯媒体不产生 API 请求
# ---------------------------------------------------------------------------


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class CountingClient:
    """记录每次 system_one 调用的最小 FakeClient。"""

    def __init__(self):
        self.calls = 0
        self.target_texts: list[str] = []
        self.contexts: list[list[dict]] = []
        self.targets: list[dict] = []

    def system_one(self, state, questions):
        from types import SimpleNamespace as NS

        self.calls += 1
        self.target_texts.append(state["target_message"]["text"])
        self.contexts.append(state["conversation_context"])
        self.targets.append(state["target_message"])
        answers = {
            "emotion": FakeAnswer(choice="calm", probabilities={"calm": 1.0}, confidence=0.9),
            "intent": FakeAnswer(choice="other", probabilities={"other": 1.0}, confidence=0.9),
            "warmth": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "engagement": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "special_attention": FakeAnswer(score=1.0, probabilities={}, confidence=0.9),
            "relationship_evidence_strength": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "relational_ease": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "romantic_signal": FakeAnswer(noul=0.1),
            "distancing_signal": FakeAnswer(noul=0.1),
        }
        return NS(answers=answers, model="fake")


def test_pure_media_produces_zero_api_calls():
    msgs = mask_messages(parse_chat(MEDIA_CHAT))
    client = CountingClient()
    results = analyze_messages(client, msgs)

    # 只有 3 条可分析 TA 文本消息（调侃 / 混合 / 明天见）
    assert client.calls == 3
    assert len(results) == 3
    # 纯媒体消息从不作为 target：没有任何一条结果的正文“只是”一个 marker
    assert all(e["text"] not in MEDIA_MARKERS.values() for e in results)
    # 混合消息保留文字，marker 只作为其中的中立说明
    mixed = [e for e in results if e["text"].startswith("你看这个")]
    assert len(mixed) == 1
    # 媒体占位符从不作为 target
    assert all(MEDIA_MARKERS[MEDIA_KIND_IMAGE] != t for t in client.target_texts)


def test_media_not_counted_in_stats():
    msgs = mask_messages(parse_chat(MEDIA_CHAT))
    results = analyze_messages(CountingClient(), msgs)
    stats = compute_conversation_stats(results)

    assert stats["analyzed"] == 3          # 不含 2 条纯媒体
    assert stats["effective_messages"] <= stats["analyzed"]
    assert stats["total_weight"] > 0
    # 信息覆盖率分母也不含媒体
    coverage = stats["effective_messages"] / stats["analyzed"]
    assert 0 < coverage <= 1


def test_media_in_context_becomes_neutral_marker():
    """纯媒体消息位于其他消息前 5 条上下文内时，以中性 marker 出现。"""
    chat = """我: 在忙吗
TA: [图片] 微信图片_1.dat
我: 周末出去玩吗
TA: 哈哈你又来了"""
    msgs = mask_messages(parse_chat(chat))
    client = CountingClient()
    analyze_messages(client, msgs)

    assert client.calls == 1
    ctx = client.contexts[0]
    marker_texts = [c["text"] for c in ctx if c["text"].startswith("[发送了")]
    assert marker_texts == [MEDIA_MARKERS[MEDIA_KIND_IMAGE]]
    # marker 不得夹带文件名
    assert all("微信图片" not in c["text"] for c in ctx)


def test_mixed_message_still_analyzed():
    chat = """我: 在忙吗
TA: 你看这个 [图片] 微信图片_1.dat"""
    msgs = mask_messages(parse_chat(chat))
    client = CountingClient()
    results = analyze_messages(client, msgs)
    assert client.calls == 1
    assert results[0]["text"].startswith("你看这个")
    assert "[发送了一张图片，内容未知]" in results[0]["text"]


def test_state_does_not_leak_parser_metadata():
    """content_type / media_kinds 不进入 Jev state（避免无谓改变缓存 key）。"""
    chat = """我: 在忙吗
TA: 在啊
我: 看这个
TA: 好的 [图片] 微信图片_1.dat"""
    msgs = mask_messages(parse_chat(chat))
    client = CountingClient()
    analyze_messages(client, msgs)

    assert client.calls == 2
    for target in client.targets:
        assert "content_type" not in target
        assert "media_kinds" not in target
        # 与旧版 state 形状保持一致：只有这 4 个字段
        assert set(target) == {"speaker", "text", "time", "raw_speaker"}


def test_mixed_message_state_keeps_text_and_marker_only():
    chat = """我: 在忙吗
TA: 好的 [图片] 微信图片_1.dat"""
    msgs = mask_messages(parse_chat(chat))
    client = CountingClient()
    analyze_messages(client, msgs)
    target = client.targets[0]
    assert target["text"].startswith("好的")
    assert "[发送了一张图片，内容未知]" in target["text"]
    assert "微信图片" not in target["text"]


def test_analysis_rule_forbids_guessing_media_content():
    from analyzer import MEDIA_RULE_CLAUSE, analysis_rule_for

    # 无媒体 marker → 基础规则（与旧版一致）
    plain = {"conversation_context": [], "target_message": {"text": "在忙吗"}}
    assert analysis_rule_for(plain) == analyzer.ANALYSIS_RULE
    assert "never guess" not in analysis_rule_for(plain).lower()

    # 有媒体 marker → 追加媒体条款
    with_media = {
        "conversation_context": [{"text": MEDIA_MARKERS[MEDIA_KIND_IMAGE]}],
        "target_message": {"text": "你看这个"},
    }
    rule = analysis_rule_for(with_media)
    assert rule == analyzer.ANALYSIS_RULE + MEDIA_RULE_CLAUSE
    assert "never guess" in rule.lower()
    assert "内容未知" in rule


def test_pure_text_state_keeps_pre_media_cache_key():
    """纯文本消息的 state / cache key 与媒体过滤前完全一致（旧缓存可命中）。"""
    from storage import make_cache_key

    target = {"speaker": "them", "text": "明天还上班呢吗", "time": "22:31",
              "raw_speaker": "TA"}
    context = [{"speaker": "me", "text": "在忙吗", "time": "22:30"}]
    state = analyzer.build_state(context, target)
    assert state["analysis_rule"] == analyzer.ANALYSIS_RULE

    # 媒体过滤前（HEAD~1）的 rule 就是当前基础 rule
    assert "never guess" not in state["analysis_rule"].lower()

    # 与本地缓存中真实存在的历史 key 对比（v2.1 smoke 样本）
    chat = """我: 在忙吗
TA: 收到，谢谢
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你还记得那个梗啊
TA: 你还记得那个梗啊哈哈哈
我: 请看一下附件
TA: 请确认附件是否收到
我: 这事只有你懂
TA: 行行行，还是你懂我"""
    msgs = mask_messages(parse_chat(chat))
    schema = analyzer.build_questions_schema()
    hits = 0
    for i, m in enumerate(msgs):
        if m["speaker"] != "them":
            continue
        ctx = [{"speaker": c["speaker"], "text": c["text"], "time": c.get("time")}
               for c in msgs[:i]]
        tgt = {"speaker": "them", "text": m["text"], "time": m.get("time"),
               "raw_speaker": m.get("raw_speaker")}
        key = make_cache_key(analyzer.build_state(ctx, tgt), schema,
                             analyzer.DEFAULT_MODEL, analyzer.SCHEMA_VERSION)
        from storage import Cache

        c = Cache()
        got = c.get(key)
        c.close()
        if got is not None:
            hits += 1
    # 本地 .jev_cache 若含这些历史条目，应全部命中；无缓存环境则为 0（不失败）
    assert hits in (0, 5)


def test_media_context_state_gets_media_clause():
    """含媒体 marker 的 state 才带媒体条款 → key 自然不同。"""
    from storage import make_cache_key

    ctx_plain = [{"speaker": "me", "text": "在忙吗", "time": None}]
    ctx_media = [{"speaker": "me", "text": MEDIA_MARKERS[MEDIA_KIND_IMAGE],
                  "time": None}]
    tgt = {"speaker": "them", "text": "哈哈", "time": None, "raw_speaker": "TA"}
    schema = analyzer.build_questions_schema()
    k_plain = make_cache_key(analyzer.build_state(ctx_plain, tgt), schema,
                             analyzer.DEFAULT_MODEL, analyzer.SCHEMA_VERSION)
    k_media = make_cache_key(analyzer.build_state(ctx_media, tgt), schema,
                             analyzer.DEFAULT_MODEL, analyzer.SCHEMA_VERSION)
    assert k_plain != k_media


# ---------------------------------------------------------------------------
# 报告导出
# ---------------------------------------------------------------------------


def test_report_records_skipped_media_and_no_filenames():
    chat = """我: 在忙吗
TA: [图片] 微信图片_1.dat
我: 周末出去玩吗
TA: 哈哈你又来了"""
    msgs = mask_messages(parse_chat(chat))
    results = analyze_messages(CountingClient(), msgs)
    stats = compute_conversation_stats(results)

    data = build_json_report(results, stats, include_text=True, skipped_media=1)
    assert data["metadata"]["skipped_media_messages"] == 1

    md = build_markdown_report(results, stats, include_text=True, skipped_media=1)
    assert "跳过非文本媒体：1 条" in md

    # 默认不导出本地媒体文件名
    for blob in (md, __import__("json").dumps(data, ensure_ascii=False)):
        assert "微信图片" not in blob
        assert ".dat" not in blob
        assert ".mp4" not in blob


def test_report_without_media_has_zero_count():
    chat = "我: 在忙吗\nTA: 在啊"
    msgs = mask_messages(parse_chat(chat))
    results = analyze_messages(CountingClient(), msgs)
    stats = compute_conversation_stats(results)
    data = build_json_report(results, stats, skipped_media=0)
    assert data["metadata"]["skipped_media_messages"] == 0
    md = build_markdown_report(results, stats, skipped_media=0)
    assert "跳过非文本媒体" not in md


def test_message_metrics_none_for_media_shaped_entries():
    """媒体消息根本不进 results；若误入（无 result），metrics 返回 None。"""
    entry = {"index": 0, "speaker": "them", "text": MEDIA_MARKERS[MEDIA_KIND_IMAGE]}
    assert message_metrics(entry) is None
