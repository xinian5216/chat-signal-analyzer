"""时间线排序测试（真实导入方式的回归；全部虚构聊天，0 真实 Jev）。

覆盖：
1. 先导 9/22 再导 9/20 → 最终升序；
2. 片段内部也是倒序 → 最终升序；
3. 两批片段重叠 → 去重且顺序正确；
4. 同一分钟多条不同消息 → 全部保留；
5. 图片/语音占位符排序后绑定迁移（或安全失效）；
6. HH:mm / 无时间 → 标记不确定，不编造日期；
7. 重新选择「我 / TA」后仍保持排序；
8. Jev target 上下文无未来泄漏（反向导入场景）；
9. 587+ 条预览、分页、切换性能正常；
外加：order_signature 变化使 index 相关状态失效、媒体绑定无法唯一对应时被丢弃。
"""

import copy
import time

import pytest

import analyzer
from analyzer import analyze_messages
from merge import merge_messages
from parser import parse_chat
from privacy import mask_messages
from timeline import (
    migrate_bindings,
    order_signature,
    page_time_range,
    preview_page,
    sort_messages,
    time_kind,
)

# ---------------------------------------------------------------------------
# 虚构聊天 fixtures（全部虚构昵称与内容）
# ---------------------------------------------------------------------------

CHUNK_NEW = """我
2026年09月22日 21:10
今天终于把项目收尾了

TA
2026年09月22日 21:12
厉害啊，晚上不庆祝一下

我
2026年09月22日 21:15
庆祝啥，累瘫了"""

CHUNK_OLD = """我
2026年09月20日 09:30
早上开会又迟到十分钟

TA
2026年09月20日 09:35
哈哈哈你这不是常规操作吗

我
2026年09月20日 09:40
扎心了"""

CHUNK_OLD_REVERSED = """我
2026年09月19日 22:20
睡前刷到一只超胖的猫

TA
2026年09月19日 22:25
多胖

我
2026年09月19日 18:00
傍晚那个更胖"""

CHUNK_OVERLAP = """TA
2026年09月20日 09:35
哈哈哈你这不是常规操作吗

我
2026年09月20日 09:40
扎心了

TA
2026年09月20日 09:45
行了别演了"""

SAME_MINUTE = """TA
2026年09月21日 10:00
在吗

我
2026年09月21日 10:00
在

TA
2026年09月21日 10:00
那我直说了"""

MEDIA_CHUNK = """我
2026年09月18日 12:00
看我刚拍的照片

TA
2026年09月18日 12:05
[图片] 微信图片_20260918.dat

我
2026年09月18日 12:06
怎么样

TA
2026年09月18日 12:10
[语音] 7\""""


def _times(messages):
    return [m.get("time") for m in messages]


# ---------------------------------------------------------------------------
# 1~3：反向追加 / 片段内倒序 / 重叠去重
# ---------------------------------------------------------------------------


def test_append_older_chunk_produces_ascending_order():
    first = mask_messages(parse_chat(CHUNK_NEW))
    older = mask_messages(parse_chat(CHUNK_OLD))
    merged = merge_messages(first, older).messages
    result = sort_messages(merged)
    assert result.order_changed is True
    assert _times(result.messages) == [
        "2026-09-20 09:30", "2026-09-20 09:35", "2026-09-20 09:40",
        "2026-09-22 21:10", "2026-09-22 21:12", "2026-09-22 21:15",
    ]


def test_internally_reversed_chunk_still_sorted():
    first = mask_messages(parse_chat(CHUNK_NEW))
    reversed_chunk = mask_messages(parse_chat(CHUNK_OLD_REVERSED))
    merged = merge_messages(first, reversed_chunk).messages
    result = sort_messages(merged)
    assert result.order_changed is True
    assert _times(result.messages) == [
        "2026-09-19 18:00", "2026-09-19 22:20", "2026-09-19 22:25",
        "2026-09-22 21:10", "2026-09-22 21:12", "2026-09-22 21:15",
    ]


def test_overlapping_chunks_dedup_and_stay_sorted():
    base = mask_messages(parse_chat(CHUNK_NEW))
    older = mask_messages(parse_chat(CHUNK_OLD))
    merged = merge_messages(base, older)
    again = merge_messages(merged.messages,
                           mask_messages(parse_chat(CHUNK_OVERLAP)))
    result = sort_messages(again.messages)
    assert _times(result.messages) == [
        "2026-09-20 09:30", "2026-09-20 09:35", "2026-09-20 09:40",
        "2026-09-20 09:45",
        "2026-09-22 21:10", "2026-09-22 21:12", "2026-09-22 21:15",
    ]
    assert again.duplicates == 2      # 重叠的两条被去重


# ---------------------------------------------------------------------------
# 4：同一分钟多条消息全部保留
# ---------------------------------------------------------------------------


def test_same_minute_distinct_messages_all_kept():
    messages = mask_messages(parse_chat(SAME_MINUTE))
    result = sort_messages(messages)
    assert len(result.messages) == 3
    assert [m["text"] for m in result.messages] == [
        "那我直说了", "在吗", "在",
    ] or [m["text"] for m in result.messages] == [
        "在吗", "在", "那我直说了",
    ]
    # 稳定排序：同刻保留原始相对顺序（解析顺序 = 已知顺序）
    assert [m["text"] for m in result.messages] == [
        "在吗", "在", "那我直说了",
    ]
    # 文本不同 → 不会被误去重
    assert len({m["text"] for m in result.messages}) == 3


def test_same_minute_identical_replies_not_deduped_by_timeline():
    """时间线只排序不去重：两条真实相同的“嗯”都应保留。"""
    chat = """TA
2026年09月21日 10:00
嗯

我
2026年09月21日 10:01
那你嗯

TA
2026年09月21日 10:01
嗯"""
    messages = mask_messages(parse_chat(chat))
    result = sort_messages(messages)
    assert len(result.messages) == 3
    assert sum(1 for m in result.messages if m["text"] == "嗯") == 2


# ---------------------------------------------------------------------------
# 5~6：媒体绑定迁移 / 时间不完整
# ---------------------------------------------------------------------------


def test_media_placeholder_bindings_migrate_with_sort():
    messages = mask_messages(parse_chat(MEDIA_CHUNK))
    sorted_first = sort_messages(messages).messages   # 单片段内部已升序
    image_idx = [i for i, m in enumerate(sorted_first)
                 if "image" in (m.get("media_kinds") or [])]
    assert len(image_idx) == 1
    bindings = {image_idx[0]: "asset-abc"}
    before = sorted_first
    # 模拟一次“逆序导入后重新排序”：把列表倒序再排序回时间线
    after = sort_messages(list(reversed(before))).messages
    migrated, dropped = migrate_bindings(before, after, bindings)
    assert dropped == []
    new_image_idx = [i for i, m in enumerate(after)
                     if "image" in (m.get("media_kinds") or [])]
    assert list(migrated) == new_image_idx               # 绑定跟着消息走
    assert migrated[new_image_idx[0]] == "asset-abc"


def test_media_bindings_dropped_when_not_uniquely_identifiable():
    messages = mask_messages(parse_chat(MEDIA_CHUNK))
    base = sort_messages(messages).messages
    # 制造无法唯一对应的场景：两条完全相同的纯媒体消息
    twin = copy.deepcopy(base[image_idx] if (image_idx := next(
        i for i, m in enumerate(base) if m.get("content_type") == "media"
    )) is not None else 0)
    ambiguous = base + [twin]
    after = sort_messages(ambiguous).messages
    bindings = {next(i for i, m in enumerate(base)
                     if m.get("content_type") == "media"): "asset-x"}
    migrated, dropped = migrate_bindings(base, after, bindings)
    assert migrated == {}
    assert dropped                                # 不猜，直接失效


def test_time_only_and_missing_are_flagged_not_fabricated():
    legacy = """我
22:30
在吗

TA
22:31
在的"""
    messages = mask_messages(parse_chat(legacy))
    assert all(m["time"] is not None for m in messages)
    result = sort_messages(messages)
    assert result.full == 0
    assert result.time_only == 2 and result.missing == 0
    # 单片段：顺序就是粘贴顺序 → 不视为不确定（但摘要仍如实标记）
    assert result.requires_order_confirm is False
    assert "仅时分 2" in result.summary_text()
    # 不编造日期：时间保持原样（仍是 HH:mm）
    assert _times(result.messages) == ["22:30", "22:31"]

    # 完全没有时间的消息（time_name 旧格式 + 无时间头）
    no_time = [{"speaker": "me", "text": "a", "time": None,
                "raw_speaker": "我"},
               {"speaker": "them", "text": "b", "time": None,
                "raw_speaker": "TA"}]
    res2 = sort_messages(no_time)
    assert res2.missing == 2 and res2.full == 0
    assert res2.messages[0]["text"] == "a"           # 保留可确定的局部顺序


def test_time_only_multi_chunk_requires_confirmation():
    """多片段且含不完整时间：跨片段位置不可确定 → 必须用户确认。"""
    chunk_a = mask_messages(parse_chat(
        "我\n22:30\n在吗\n\nTA\n22:31\n在的"))
    chunk_b = mask_messages(parse_chat(
        "我\n21:00\n早上那事\n\nTA\n21:05\n知道了"))
    merged = merge_messages(_tag(chunk_a, 0), _tag(chunk_b, 1)).messages
    result = sort_messages(merged, multi_chunk=True)
    assert result.full == 0 and result.time_only == 4
    assert result.requires_order_confirm is True


def _tag(messages, idx):
    for m in messages:
        m["_chunk_idx"] = idx
    return messages


def test_cross_chunk_same_minute_is_ambiguous():
    """同一时刻跨片段、文本不同 → 顺序不可确定，必须标记且要求确认。"""
    a = mask_messages(parse_chat(
        "TA\n2026年09月21日 10:00\n在吗\n\n"
        "我\n2026年09月21日 10:00\n在"))
    b = mask_messages(parse_chat(
        "TA\n2026年09月21日 10:00\n那我直说了\n\n"
        "我\n2026年09月21日 10:01\n嗯"))
    merged = merge_messages(_tag(a, 0), _tag(b, 1)).messages
    result = sort_messages(merged, multi_chunk=True)
    assert result.full == 4
    assert result.ambiguous_exact_tie >= 1        # 10:00 跨片段
    assert result.requires_order_confirm is True
    assert len(result.messages) == 4              # 文本不同 → 全部保留


def test_time_only_messages_never_inserted_into_full_timeline():
    """仅时分的消息不得被猜日期后插到完整时间线中间。"""
    mixed = [
        {"speaker": "me", "text": "full-1", "time": "2026-09-20 09:00",
         "raw_speaker": "我"},
        {"speaker": "them", "text": "only", "time": "09:30",
         "raw_speaker": "TA"},
        {"speaker": "me", "text": "full-2", "time": "2026-09-20 10:00",
         "raw_speaker": "我"},
    ]
    result = sort_messages(mixed)
    assert [m["text"] for m in result.messages] == [
        "full-1", "full-2", "only",
    ]
    assert result.full == 2 and result.time_only == 1
    assert result.requires_order_confirm is True


def test_invalid_time_format_treated_as_missing():
    m = {"speaker": "me", "text": "x", "time": "昨天下午", "raw_speaker": "我"}
    result = sort_messages([m])
    assert result.full == 0 and result.missing == 1


# ---------------------------------------------------------------------------
# 7：重新选择身份后仍保持排序
# ---------------------------------------------------------------------------


def _rebuild_like_app(chunks, my_name, them_name):
    """与 app.rebuild_messages_from_chunks 等价（显式昵称 + 时间线排序）。"""
    merged: list[dict] = []
    for chunk in chunks:
        parsed = mask_messages(parse_chat(chunk, my_name, them_name))
        merged = parsed if not merged else merge_messages(merged, parsed).messages
    return sort_messages(merged).messages


def test_identity_remap_keeps_time_order():
    # 虚构昵称片段：未指定映射时默认名解析不了 → unknown
    nick_chunks = [
        CHUNK_NEW.replace("我", "小柯").replace("TA", "阿柚"),
        CHUNK_OLD.replace("我", "小柯").replace("TA", "阿柚"),
    ]
    unknown_first = _rebuild_like_app(nick_chunks, None, None)
    assert {m["speaker"] for m in unknown_first} == {"unknown"}
    assert _times(unknown_first) == sorted(_times(unknown_first))
    # 用户选择身份后从所有原始片段重建：仍然升序，且身份正确
    remapped = _rebuild_like_app(nick_chunks, "小柯", "阿柚")
    assert _times(remapped) == [
        "2026-09-20 09:30", "2026-09-20 09:35", "2026-09-20 09:40",
        "2026-09-22 21:10", "2026-09-22 21:12", "2026-09-22 21:15",
    ]
    assert {m["speaker"] for m in remapped} == {"me", "them"}


# ---------------------------------------------------------------------------
# 8：反向导入不得造成未来泄漏
# ---------------------------------------------------------------------------


class _RecordingClient:
    def __init__(self):
        self.states = []
        self.calls = 0

    def system_one(self, state, questions):
        self.calls += 1
        self.states.append(state)
        from types import SimpleNamespace as NS

        class _A:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        answers = {
            "emotion": _A(choice="calm", probabilities={"calm": 1.0},
                          confidence=0.9),
            "intent": _A(choice="other", probabilities={"other": 1.0},
                         confidence=0.9),
            "warmth": _A(score=2.0, probabilities={}, confidence=0.9),
            "engagement": _A(score=2.0, probabilities={}, confidence=0.9),
            "special_attention": _A(score=1.0, probabilities={},
                                    confidence=0.9),
            "relationship_evidence_strength": _A(score=2.0, probabilities={},
                                                 confidence=0.9),
            "relational_ease": _A(score=2.0, probabilities={}, confidence=0.9),
            "romantic_signal": _A(noul=0.1),
            "distancing_signal": _A(noul=0.1),
        }
        return NS(answers=answers, model="fake")


def _sorted_chat(chunks):
    merged: list[dict] = []
    for chunk in chunks:
        parsed = mask_messages(parse_chat(chunk, "我", "TA"))
        merged = parsed if not merged else merge_messages(merged, parsed).messages
    return sort_messages(merged).messages


def test_reverse_import_has_no_future_leakage_into_context():
    # 用户真实方式：先复制最新（9/22），再追加更早（9/20），再更早（9/19）
    messages = _sorted_chat([CHUNK_NEW, CHUNK_OLD, CHUNK_OLD_REVERSED])
    client = _RecordingClient()
    analyzer.analyze_messages(client, messages)
    assert client.calls >= 1
    for state in client.states:
        target_time = state["target_message"]["time"]
        context_times = [c.get("time") for c in state["conversation_context"]]
        for t in context_times:
            assert t is None or t <= target_time, (
                f"未来泄漏：target={target_time} context={context_times}")


def test_analysis_indices_match_sorted_positions():
    messages = _sorted_chat([CHUNK_NEW, CHUNK_OLD])
    client = _RecordingClient()
    results = analyzer.analyze_messages(client, messages)
    for r in results:
        assert messages[r["index"]]["text"] == r["text"]
        assert messages[r["index"]]["speaker"] == "them"


# ---------------------------------------------------------------------------
# 9/性能：587+ 条预览、分页与模式切换
# ---------------------------------------------------------------------------


def _big_chat(n=600):
    blocks = []
    day = 1
    for i in range(n):
        if i % 20 == 0 and i:
            day += 1
        who = "我" if i % 2 == 0 else "TA"
        blocks.append(f"{who}\n2026年09月{day:02d}日 09:{i % 60:02d}\n消息 {i}")
    return "\n\n".join(blocks)


def test_large_chat_preview_pagination_and_switching():
    messages = sort_messages(
        mask_messages(parse_chat(_big_chat(600)))).messages
    assert len(messages) == 600
    # 全部消息按完整时间升序（含跨天）
    assert _times(messages) == sorted(_times(messages))

    started = time.perf_counter()
    window, total, pages = preview_page(messages, mode="all", page=1,
                                         page_size=40)
    assert total == 600 and pages == 15
    assert len(window) == 40
    assert window[0][0] == 0                       # 全局 index 从 0 开始
    assert window[-1][0] == 39

    window2, _, pages2 = preview_page(messages, mode="all", page=8,
                                      page_size=40)
    assert window2[0][0] == 280                     # 全局 index 连续正确
    assert len(window2) == 40

    recent, _, _ = preview_page(messages, mode="recent", limit=15)
    assert len(recent) == 15 and recent[0][0] == 585
    earliest, _, _ = preview_page(messages, mode="earliest", limit=15)
    assert earliest[0][0] == 0

    rows_elapsed = time.perf_counter() - started
    assert rows_elapsed < 5.0                       # 预览/分页必须够快

    # 模式切换只取窗，不改原列表
    assert _times(messages) == sorted(_times(messages))
    assert page_time_range(window).startswith("2026-09-01")


def test_preview_rows_keep_global_indices():
    from ui_helpers import preview_rows

    messages = sort_messages(
        mask_messages(parse_chat(_big_chat(120)))).messages
    window, _, _ = preview_page(messages, mode="recent", limit=15)
    rows = preview_rows([m for _, m in window], limit=15,
                        start=window[0][0])
    assert rows[0]["#"] == window[0][0] + 1
    assert rows[-1]["#"] == window[-1][0] + 1
    # 编号与真实全局消息一一对应
    assert messages[int(rows[0]["#"]) - 1]["text"] == window[0][1]["text"]


# ---------------------------------------------------------------------------
# order_signature / 状态失效
# ---------------------------------------------------------------------------


def test_order_signature_changes_when_order_changes():
    a = sort_messages(mask_messages(parse_chat(CHUNK_NEW))).messages
    b = sort_messages(
        merge_messages(a, mask_messages(parse_chat(CHUNK_OLD))).messages
    ).messages
    assert order_signature(a) != order_signature(b)
    # 相同内容不同顺序 → 签名必须不同（否则 index 状态不会被失效）
    assert order_signature(b) != order_signature(list(reversed(b)))


def test_migrate_bindings_no_bindings_noop():
    migrated, dropped = migrate_bindings([], [], {})
    assert migrated == {} and dropped == []
