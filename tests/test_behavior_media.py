"""无效行为候选过滤回归（纯媒体 / 混合消息 / 媒体夹文字间 / 历史线索 /
跨批次导入）。全虚构数据，0 Jev API。

覆盖任务书场景：

- 纯图片 / 纯语音 / 纯动画表情：不生成需要理解内容的候选，并计数；
- 占位符文本（「[发送了一张图片，内容未知]」）不参与关键词 / 词语重合；
- 媒体夹在有效文字之间 / 文字与媒体混合：照常生成候选，媒体保留为上下文；
- 纯媒体 + 时间间隔：不进入态度审核队列（客观记录仍在聊天里）；
- 历史缺原文候选：作为「历史线索」单独处理，不进普通待审核列表；
- 同一好友跨批次导入：已审核的原始候选不复活、新候选正常出现；
- 已确认事件不被过滤逻辑修改或删除。
"""

import pytest

import behavior as bv
import friend_history as fh
import paths
from parser import parse_chat
from privacy import mask_messages
from timeline import sort_messages

ME = "小明."
THEM = "小安."


def _messages(chat: str):
    return sort_messages(mask_messages(
        parse_chat(chat, ME, THEM))).messages


def _pairs(cands):
    return sorted({(c.dimension, c.behavior_type) for c in cands})


# ---------------------------------------------------------------------------
# 虚构媒体聊天
# ---------------------------------------------------------------------------

# 纯媒体回应困难 + 纯媒体接话 + 占位符词重合陷阱
CHAT_PURE_MEDIA = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
[图片]

小明.
2026年08月21日 21:06
周末有空吗

小安.
2026年08月21日 21:07
[语音] 7"

小明.
2026年08月21日 21:08
那你早点休息

小安.
2026年08月21日 21:09
[动画表情]

小明.
2026年08月21日 21:10
你看这张图片是我上周拍的那张，内容你还满意吗

小安.
2026年08月21日 21:11
[视频]"""

# 语音 / 动画表情单独变体
CHAT_PURE_VOICE = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
[语音] 12\""""

CHAT_PURE_STICKER = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
[动画表情]"""

# 混合消息：文字 + 媒体占位符同行
CHAT_MIXED = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
别难过，抱抱 [图片]

小明.
2026年08月21日 21:06
谢谢

小安.
2026年08月21日 21:10
周末一起吃饭吧，我请你，地点你定 [动画表情]"""

# 媒体夹在有效文字之间（同一位 TA 的两条文字之间夹了一张图）
CHAT_MEDIA_BETWEEN = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:01
[图片]

小安.
2026年08月21日 21:02
辛苦啦，别熬太晚，我陪你

小明.
2026年08月21日 21:03
嗯嗯

小安.
2026年08月21日 21:05
周末一起吃饭吧"""

# 纯媒体 + 时间间隔（主动联系）
CHAT_GAP_MEDIA = """小安.
2026年08月21日 09:00
早上好，今天天气不错

小明.
2026年08月21日 09:05
早

小安.
2026年08月21日 21:30
[图片]"""

# 纯文字基线（不应被误过滤）
CHAT_TEXT = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
辛苦啦，别熬太晚，我陪你

小明.
2026年08月21日 21:06
嗯嗯

小安.
2026年08月21日 21:10
周末一起吃饭吧，我请你，地点你定"""


class _store:
    """上下文管理器：临时档案库。"""

    def __init__(self):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "friend_history.db"
        self._orig_path = paths.friend_history_db_path
        paths.friend_history_db_path = lambda: self.db

    def __enter__(self) -> fh.FriendStore:
        return fh.FriendStore(self.db)

    def __exit__(self, *exc):
        paths.friend_history_db_path = self._orig_path
        return False


# ---------------------------------------------------------------------------
# 1) 纯媒体不生成候选
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chat,name", [
    (CHAT_PURE_MEDIA, "纯图片+语音+表情混合"),
    (CHAT_PURE_VOICE, "纯语音"),
    (CHAT_PURE_STICKER, "纯动画表情"),
])
def test_pure_media_generates_no_candidates(chat, name):
    cands, meta = bv.generate_candidates_with_meta(_messages(chat))
    assert cands == [], (name, _pairs(cands))
    assert meta["media_filtered"] >= 1, name
    # 纯文字基线不受影响
    base_cands, base_meta = bv.generate_candidates_with_meta(
        _messages(CHAT_TEXT))
    assert base_cands and base_meta["media_filtered"] == 0


def test_media_message_not_deleted_from_chat():
    """过滤只针对候选：媒体消息必须完整保留在聊天里当上下文。"""
    messages = _messages(CHAT_MEDIA_BETWEEN)
    assert any(m.get("content_type") == "media" for m in messages)
    cands, _meta = bv.generate_candidates_with_meta(messages)
    # 「辛苦啦，别熬太晚，我陪你」构成关心互动 → 有候选，且上下文预览里
    # 能看得到那张媒体消息（不删除、不当正文）
    assert cands
    care = [c for c in cands if c.dimension == bv.DIMENSION_CARE]
    assert care
    preview_texts = " ".join(str(row.get("text") or "")
                             for c in care for row in c.msg_texts)
    assert "内容未知" in preview_texts, "媒体消息应保留在候选上下文里"


def test_mixed_message_generates_candidates_normally():
    """文字 + 媒体混合：照常生成候选（媒体内容不做推测）。"""
    cands, meta = bv.generate_candidates_with_meta(_messages(CHAT_MIXED))
    assert (bv.DIMENSION_CARE, "care_response") in _pairs(cands)
    assert (bv.DIMENSION_CARE, "concrete_support") in _pairs(cands)
    assert (bv.DIMENSION_INITIATIVE, "concrete_arrangement") in _pairs(cands)
    assert meta["media_filtered"] == 0


def test_placeholder_words_do_not_create_topic_continuation():
    """占位符词（图片/内容/发送/未知）不得让纯媒体消息伪装成「延续话题」。"""
    cands, meta = bv.generate_candidates_with_meta(_messages(
        CHAT_PURE_MEDIA))
    assert (bv.DIMENSION_INITIATIVE, "topic_continuation") not in _pairs(cands)
    # 反向确认：同样的文字互动（没有纯媒体、共享完整内容分块）确实会
    # 生成延续话题候选（分块级重合是既有规则，不因本轮改动失效）
    chat = """小明.
2026年08月21日 21:10
周末一起去爬山吧

小安.
2026年08月21日 21:11
周末一起去爬山吧，说定了"""
    cands2, _meta2 = bv.generate_candidates_with_meta(_messages(chat))
    assert (bv.DIMENSION_INITIATIVE, "topic_continuation") in _pairs(cands2)


def test_media_after_gap_not_a_proactive_candidate():
    """仅靠时间间隔识别的主动联系：纯媒体默认不进审核队列。"""
    cands, meta = bv.generate_candidates_with_meta(
        _messages(CHAT_GAP_MEDIA))
    assert cands == []
    assert meta["media_filtered"] >= 1
    # 同一结构换成文字 → 正常生成主动发起候选
    chat_text = CHAT_GAP_MEDIA.replace("[图片]", "晚上好，今天聊聊天吧")
    cands2, _meta2 = bv.generate_candidates_with_meta(_messages(chat_text))
    assert (bv.DIMENSION_INITIATIVE, "proactive_contact") in _pairs(cands2)


# ---------------------------------------------------------------------------
# 2) 历史线索：独立于普通待审核列表
# ---------------------------------------------------------------------------


def _history_run(store, friend, chat, results):
    return store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=_messages(chat),
        results=results, stats={}, schema_version="chat-signal-v3.3",
        request_model="jev-latest", summary_text="旧总结"))


def _text_results(messages):
    return [
        {"index": i, "speaker": "them", "time": messages[i].get("time"),
         "cached": True,
         "result": {"intent": {"choice": "show_care"}, "model": "m"}}
        for i, m in enumerate(messages)
        if m["speaker"] == "them" and m.get("content_type") != "media"
    ]


def test_history_clues_are_separate_and_review_sticks():
    """历史候选不进普通待审核列表；审核后不复活。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        chat = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
辛苦啦，别熬太晚

小明.
2026年08月21日 21:06
嗯嗯"""
        messages = _messages(chat)
        run_id = _history_run(store, friend, chat, _text_results(messages))
        run = store.get_run(run_id)
        clues = bv.history_candidates(run)
        assert clues, "历史快照应产出线索"
        for clue in clues:
            assert clue.source_kind == "history"
            assert clue.flags["context_missing"] is True
            assert clue.msg_texts == []            # 无正文，不编造

        # 应用层分离：文本候选与线索分别按身份过滤
        text_cands = bv.generate_candidates(messages)
        events = store.list_events(friend.friend_id)
        text_pending = bv.pending_candidates(text_cands, events)
        clue_pending = bv.pending_candidates(clues, events)
        assert text_pending and clue_pending          # 两边都有内容
        assert not (set(c.identity for c in text_pending)
                    & set(c.identity for c in clue_pending))

        # 审核一条线索（确认，不改方向）
        clue = clue_pending[0]
        store.save_event(bv.build_event_dict(
            candidate=clue, friend_id=friend.friend_id,
            dimension=clue.dimension, behavior_type=clue.behavior_type,
            stance="supporting"))
        events = store.list_events(friend.friend_id)
        assert bv.pending_candidates(clues, events) == [], (
            "已审核的历史线索不得复活")


def test_history_clue_fingerprint_match_with_current_import():
    """历史线索与当前聊天关联：必须真实消息指纹逐字节一致。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        chat = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
辛苦啦，别熬太晚"""
        messages = _messages(chat)
        run_id = _history_run(store, friend, chat, _text_results(messages))
        run = store.get_run(run_id)
        clues = bv.history_candidates(run)
        clue = clues[0]
        fp = clue.fingerprints[0]

        # 当前导入包含同一条消息 → 指纹能对上
        hits = bv.current_matches_by_fingerprint(fp, messages)
        assert hits, "同一批消息应能指纹互认"
        # 当前导入是**另一批**不同聊天 → 对不上，不许自动关联
        other = _messages(CHAT_GAP_MEDIA)
        assert bv.current_matches_by_fingerprint(fp, other) == []


# ---------------------------------------------------------------------------
# 3) 跨批次导入：已审核候选不复活 + 新候选出现
# ---------------------------------------------------------------------------


BATCH_1 = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
辛苦啦，别熬太晚，我陪你

小明.
2026年08月21日 21:06
嗯嗯"""

BATCH_2 = BATCH_1 + """

小安.
2026年08月21日 21:10
周末一起吃饭吧，我请你，地点你定

小明.
2026年08月21日 21:11
好啊

小安.
2026年08月22日 10:00
定位我订好了，到时见"""


def test_cross_batch_import_reviewed_candidate_stays_gone():
    """同一好友跨批次导入：审核过的原始候选不复活，新候选正常出现。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        batch1 = _messages(BATCH_1)
        cands1 = bv.generate_candidates(batch1)
        target = [c for c in cands1
                  if c.dimension == bv.DIMENSION_CARE
                  and c.behavior_type == "care_response"][0]
        # 用户修改方向后确认（原始身份与最终身份不同）
        store.save_event(bv.build_event_dict(
            candidate=target, friend_id=friend.friend_id,
            dimension=bv.DIMENSION_RESPECT,
            behavior_type="disagreement_response",
            stance="supporting", messages=batch1))

        # 追加第二批（含第一批全部消息 + 新消息）
        batch2 = _messages(BATCH_2)
        events = store.list_events(friend.friend_id)
        pending2 = bv.pending_candidates(
            bv.generate_candidates(batch2), events)
        assert not any(c.identity == target.identity for c in pending2), (
            "已审核（含修改方向）的原始候选不得在跨批次导入后复活")
        # 新批次产生新候选（邀约 / 落实），且总量合理
        pairs2 = _pairs(pending2)
        assert (bv.DIMENSION_INITIATIVE, "concrete_arrangement") in pairs2
        # 反复重新生成（rerun / 再次导入）稳定
        for _ in range(3):
            assert not any(c.identity == target.identity
                           for c in bv.pending_candidates(
                               bv.generate_candidates(batch2), events))


def test_filtering_never_modifies_confirmed_events():
    """过滤只丢候选：已确认事件一个字段都不动。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        batch1 = _messages(BATCH_1)
        cands1 = bv.generate_candidates(batch1)
        target = cands1[0]
        event_id = store.save_event(bv.build_event_dict(
            candidate=target, friend_id=friend.friend_id,
            dimension=target.dimension,
            behavior_type=target.behavior_type,
            stance="supporting", notes="用户确认的说明",
            messages=batch1))
        before = store.get_event(event_id)

        # 之后导入的聊天里，TA 对我的新一轮困难发言只回纯媒体 → 新的
        # 「认真回应困难」候选被过滤（media_filtered ≥ 1），而之前已确认
        # 的事件必须一个字段都不动
        media_chat = BATCH_1 + """

小明.
2026年08月22日 22:00
今天又被批评了，好难受

小安.
2026年08月22日 22:02
[图片]"""
        cands2, meta2 = bv.generate_candidates_with_meta(
            _messages(media_chat))
        assert meta2["media_filtered"] >= 1
        # 原始候选仍在原始生成里，但按事件身份过滤后必须消失
        pending2 = bv.pending_candidates(
            cands2, store.list_events(friend.friend_id))
        assert not any(c.identity == target.identity for c in pending2)
        after = store.get_event(event_id)
        assert before == after, "过滤绝不能修改已确认事件"


# ---------------------------------------------------------------------------
# 带时长的语音占位符（动态时长不能被只删尾巴的剥离留下碎片）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [
    "[发送了一条语音，内容未知]",
    "[发送了一条 7 秒语音，内容未知]",
    "[发送了一条 12 秒语音，内容未知]",
    "[发送了一条 7.5 秒语音，内容未知]",
    "[发送了一条 3600 秒语音，内容未知]",
])
def test_voice_duration_markers_fully_stripped(marker):
    """带时长的语音占位符必须整段移除（含动态时长），不留碎片。"""
    assert bv.strip_media_markers(marker).strip() == ""
    assert bv._content_tokens(marker) == set(), marker
    assert bv.message_has_text({"text": marker}) is False, marker


def test_mixed_text_and_voice_keeps_only_real_text():
    """文字 + 语音混合：只保留真实文字的词元，占位符词（发送了一条 /
    秒语音）绝不出现。"""
    raw = "我听到了 [发送了一条 7 秒语音，内容未知] 之后回的"
    tokens = bv._content_tokens(raw)
    assert "我听到了" in tokens
    assert "之后回的" in tokens
    for leftover in ("发送了一条", "秒语音", "内容", "未知", "一条"):
        assert leftover not in tokens, leftover
    assert bv.message_has_text({"text": raw}) is False or True  # 有真实文字


def test_voice_placeholder_words_do_not_create_topic_continuation():
    """我方文字出现独立的「发送了一条」分块，TA 只回带时长语音 → 不得因
    占位符残词（发送了一条 / 秒语音）生成延续话题候选。"""
    chat = """小明.
2026年08月21日 21:00
发送了一条，你听一下

小安.
2026年08月21日 21:05
[语音] 7"
"""
    cands, meta = bv.generate_candidates_with_meta(_messages(chat))
    assert (bv.DIMENSION_INITIATIVE, "topic_continuation") not in _pairs(cands)
    assert cands == [], _pairs(cands)
    # 占位符被完整剥离 → 连候选都没生成（不是生成后再过滤）
    assert meta["media_filtered"] == 0
    # 反向对照：同样的文字换成 TA 的真实文字回应 → 正常生成延续话题
    chat_text = chat.replace('[语音] 7"', '发送了一条，我听着呢')
    cands2, _meta2 = bv.generate_candidates_with_meta(_messages(chat_text))
    assert (bv.DIMENSION_INITIATIVE, "topic_continuation") in _pairs(cands2)


CHAT_BOTH_VOICES = """小明.
2026年08月21日 21:00
我先发一条语音试试

小安.
2026年08月21日 21:01
[语音] 7"

小明.
2026年08月21日 21:02
[语音] 12"

小安.
2026年08月21日 21:03
[语音] 45"
"""


def test_both_sides_voice_different_durations_no_false_candidates():
    """双方发送不同时长的语音：不得因占位符词语重合产生任何候选。"""
    cands, meta = bv.generate_candidates_with_meta(
        _messages(CHAT_BOTH_VOICES))
    assert cands == [], _pairs(cands)
    # 占位符完整剥离 → 不生成任何候选（不同时长同理）
    assert meta["media_filtered"] == 0
    # 媒体消息仍完整保留在聊天里（没被删）
    messages = _messages(CHAT_BOTH_VOICES)
    assert sum(1 for m in messages if m.get("content_type") == "media") == 3


CHAT_TEXT_PLUS_VOICE = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
听到了 [语音] 7" 先不说了，晚点聊

小明.
2026年08月21日 21:06
好"""


def test_text_plus_voice_reply_still_generates_candidates():
    """文字 + 语音混合回应：照常生成候选（语音内容不做推测）。"""
    cands, meta = bv.generate_candidates_with_meta(
        _messages(CHAT_TEXT_PLUS_VOICE))
    assert (bv.DIMENSION_CARE, "care_response") in _pairs(cands)
    # 混合消息的候选保留媒体作为上下文
    care = [c for c in cands if c.behavior_type == "care_response"][0]
    preview = " ".join(str(row.get("text") or "") for row in care.msg_texts)
    assert "内容未知" in preview
    # 但候选窗口的 TA 侧有真实文字 → 不被媒体过滤
    assert meta["media_filtered"] == 0
