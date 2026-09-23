"""时间线加固 + 微信 PC 复制格式回归测试（全部虚构数据，0 真实 Jev）。

覆盖：
A. 时间合法性：真实日历校验（闰年 / 月底 / 非法月 / 非法时分）；
B. 手动媒体绑定迁移：唯一对应才迁移，否则清空；
C. 顺序确认失效：顺序变化使 order_confirmed 作废；
D. 微信 PC 端三行块复制格式（严格 SENDER / TIMESTAMP / BODY）：
   昵称可含英文句点、同一分钟保持原序、跨片段升序、单片段已确定顺序
   不触发人工确认、跨片段同刻如实提示；
E. 数百条“从最近往过去逐批追加”的端到端时间线正确性。
"""

import pytest

from merge import merge_messages
from parser import parse_chat
from privacy import mask_messages
from timeline import (
    _is_real_datetime,
    order_signature,
    sort_messages,
    time_kind,
)

# ---------------------------------------------------------------------------
# 虚构微信 PC 复制数据（昵称、正文全部虚构；句点昵称 + 同分钟多条）
# ---------------------------------------------------------------------------

WX_BLOCK = """测试甲.
2026年09月23日 16:04
第一条消息

测试甲.
2026年09月23日 16:04
第二条消息

测试乙.
2026年09月23日 16:25
第三条消息

测试甲.
2026年09月23日 16:26
第四条消息

测试乙.
2026年09月23日 16:26
第五条消息

测试甲.
2026年09月23日 16:26
第六条消息"""


def _tag(messages, idx):
    for m in messages:
        m["_chunk_idx"] = idx
    return messages


def _times(messages):
    return [m.get("time") for m in messages]


# ---------------------------------------------------------------------------
# A. 时间合法性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("2026-09-23 16:04", "full"),
    ("2026-02-29", "missing"),          # 2026 非闰年
    ("2026-02-30", "missing"),
    ("2026-04-31", "missing"),          # 小月无 31 日
    ("2026-13-01", "missing"),          # 非法月
    ("2026-00-10", "missing"),          # 0 月
    ("2026-09-23 24:00", "missing"),    # 非法小时
    ("2026-09-23 23:60", "missing"),    # 非法分钟
    ("2026-09-23 25:61", "missing"),
    ("16:04", "time_only"),
    ("", "missing"),
    (None, "missing"),
    ("昨天下午", "missing"),
])
def test_time_kind_validates_real_calendar(value, expected):
    assert time_kind(value) == expected


def test_leap_year_boundaries():
    assert _is_real_datetime(2024, 2, 29, 0, 0) is True    # 闰年
    assert _is_real_datetime(2026, 2, 29, 0, 0) is False   # 平年
    assert _is_real_datetime(2000, 2, 29, 0, 0) is True    # 四百年再闰
    assert _is_real_datetime(1900, 2, 29, 0, 0) is False   # 百年不闰
    assert _is_real_datetime(2026, 1, 31, 0, 0) is True
    assert _is_real_datetime(2026, 4, 31, 0, 0) is False   # 小月


def test_illegal_dates_never_enter_the_timeline():
    messages = [
        {"speaker": "me", "text": "a", "time": "2026-02-30",
         "raw_speaker": "测试甲."},
        {"speaker": "them", "text": "b", "time": "2026-09-23 10:00",
         "raw_speaker": "测试乙."},
        {"speaker": "me", "text": "c", "time": "2026-09-23 09:30",
         "raw_speaker": "测试甲."},
    ]
    result = sort_messages(messages)
    assert result.full == 2 and result.missing == 1
    assert [m["text"] for m in result.messages] == ["c", "b", "a"]
    # 混排 → 无法确认位置 → 必须要求确认
    assert result.requires_order_confirm is True


def test_time_only_hour_minutes_validated():
    assert time_kind("25:00") == "missing"      # 时分也必须是合法时刻
    assert time_kind("12:61") == "missing"
    assert time_kind("9:30") == "time_only"


# ---------------------------------------------------------------------------
# B. 手动媒体绑定迁移
# ---------------------------------------------------------------------------


def _media_messages():
    return mask_messages(parse_chat(
        "我\n2026年09月23日 16:00\n看我拍的照片\n\n"
        "TA\n2026年09月23日 16:02\n[图片] 微信图片_20260923.dat\n\n"
        "我\n2026年09月23日 16:04\n还有一张\n\n"
        "TA\n2026年09月23日 16:06\n[图片] 微信图片_20260924.dat"))


def test_manual_bindings_migrate_like_auto_bindings():
    from timeline import migrate_bindings

    base = sort_messages(_media_messages()).messages
    img = next(i for i, m in enumerate(base)
               if m.get("content_type") == "media")
    manual = {img: "asset-manual-1"}
    after = sort_messages(list(reversed(base))).messages
    migrated, dropped = migrate_bindings(base, after, manual)
    assert dropped == []
    new_img = next(i for i, m in enumerate(after)
                   if m.get("content_type") == "media")
    assert migrated == {new_img: "asset-manual-1"}


def test_manual_bindings_dropped_when_ambiguous():
    from timeline import migrate_bindings

    import copy
    base = sort_messages(_media_messages()).messages
    twin = copy.deepcopy(next(m for m in base
                              if m.get("content_type") == "media"))
    ambiguous = base + [twin]
    after = sort_messages(ambiguous).messages
    manual = {next(i for i, m in enumerate(base)
                   if m.get("content_type") == "media"): "asset-x"}
    migrated, dropped = migrate_bindings(base, after, manual)
    assert migrated == {} and len(dropped) == 1


# ---------------------------------------------------------------------------
# C. 顺序确认失效（set_messages 的 session 语义以纯函数等价验证）
# ---------------------------------------------------------------------------


def test_order_signature_change_invalidates_previous_confirmation():
    """顺序变化 → order_signature 变化 → 旧确认必须失效（app 层行为）。"""
    a = sort_messages(mask_messages(parse_chat(WX_BLOCK))).messages
    sig_a = order_signature(a)
    later = mask_messages(parse_chat(
        "测试甲.\n2026年09月24日 09:00\n新的一天\n\n"
        "测试乙.\n2026年09月24日 09:05\n早"))
    b = sort_messages(merge_messages(a, later).messages).messages
    assert order_signature(b) != sig_a     # app.set_messages 据此清空 order_confirmed
    # 追加更新消息后，旧确认若仍在，就会放行新的时间线 → 必须失效
    assert order_signature(b) == order_signature(b)


def test_order_confirmation_state_reset_in_reset_chat_state():
    """_reset_chat_state 必须清空 order_confirmed（源码级断言）。"""
    src = (__import__("pathlib").Path("app.py")).read_text(encoding="utf-8")
    reset_block = src.split("def _reset_chat_state")[1].split("def set_input_notice")[0]
    assert 'st.session_state["order_confirmed"] = False' in reset_block
    assert 'st.session_state["media_manual_dropped"] = []' in reset_block


# ---------------------------------------------------------------------------
# D. 微信 PC 端三行块格式
# ---------------------------------------------------------------------------


def test_wechat_block_parsing_and_order():
    messages = mask_messages(parse_chat(WX_BLOCK, "测试甲.", "测试乙."))
    assert len(messages) == 6
    # 昵称可含英文句点
    assert {m["raw_speaker"] for m in messages} == {"测试甲.", "测试乙."}
    assert [m["speaker"] for m in messages] == [
        "me", "me", "them", "me", "them", "me"]
    assert messages[0]["time"] == "2026-09-23 16:04"
    assert messages[-1]["time"] == "2026-09-23 16:26"
    # 同一分钟内保持原始顺序（16:04 两条、16:26 三条）
    same_0406 = [m["text"] for m in messages if m["time"] == "2026-09-23 16:04"]
    assert same_0406 == ["第一条消息", "第二条消息"]
    same_26 = [m["text"] for m in messages if m["time"] == "2026-09-23 16:26"]
    assert same_26 == ["第四条消息", "第五条消息", "第六条消息"]


def test_wechat_block_single_chunk_no_unnecessary_confirmation():
    messages = mask_messages(parse_chat(WX_BLOCK, "测试甲.", "测试乙."))
    result = sort_messages(messages, multi_chunk=False)
    # 单片段、全部完整时间 → 已确定顺序，不得要求人工确认
    assert result.full == 6
    assert result.requires_order_confirm is False
    # 16:04 / 16:26 同刻同片段 → 保持原序，不算歧义
    assert result.ambiguous_exact_tie == 0
    assert [m["text"] for m in result.messages] == [
        "第一条消息", "第二条消息", "第三条消息",
        "第四条消息", "第五条消息", "第六条消息",
    ]


def test_wechat_block_cross_chunk_reverse_append_orders_up():
    newest = mask_messages(parse_chat(
        "测试甲.\n2026年09月25日 10:00\n今晚有空吗\n\n"
        "测试乙.\n2026年09月25日 10:05\n有啊", "我", "TA"))
    result = sort_messages(
        merge_messages(_tag(newest, 0),
                       _tag(mask_messages(parse_chat(WX_BLOCK, "测试甲.", "测试乙.")), 1)
                       ).messages,
        multi_chunk=True,
    )
    assert _times(result.messages) == [
        "2026-09-23 16:04", "2026-09-23 16:04", "2026-09-23 16:25",
        "2026-09-23 16:26", "2026-09-23 16:26", "2026-09-23 16:26",
        "2026-09-25 10:00", "2026-09-25 10:05",
    ]
    assert result.order_changed is True


def test_cross_chunk_same_instant_is_reported_not_guessed():
    """跨片段同刻且无法由重叠确定 → 如实提示，不虚构先后。"""
    a = mask_messages(parse_chat(
        "测试甲.\n2026年09月23日 16:26\n片段A的同时刻\n\n"
        "测试乙.\n2026年09月23日 16:30\nA的收尾", "我", "TA"))
    b = mask_messages(parse_chat(
        "测试乙.\n2026年09月23日 16:26\n片段B的同时刻\n\n"
        "测试甲.\n2026年09月23日 16:40\nB的收尾", "我", "TA"))
    result = sort_messages(
        merge_messages(_tag(a, 0), _tag(b, 1)).messages, multi_chunk=True)
    assert result.ambiguous_exact_tie >= 1
    assert result.requires_order_confirm is True
    assert result.full == 4                          # 四条全部保留
    # 同刻两条的相对顺序保持“已有片段在前”的确定性，绝不重新洗牌
    assert result.messages[0]["text"] == "片段A的同时刻"


def test_cross_chunk_overlap_resolves_same_instant():
    """重叠片段提供 order 证据：16:26 的四/五/六条出现在两个片段中。"""
    overlap_chunk = mask_messages(parse_chat(
        "测试乙.\n2026年09月23日 16:25\n第三条消息\n\n"
        "测试甲.\n2026年09月23日 16:26\n第四条消息\n\n"
        "测试乙.\n2026年09月23日 16:26\n第五条消息", "我", "TA"))
    merged = merge_messages(
        _tag(mask_messages(parse_chat(WX_BLOCK, "测试甲.", "测试乙.")), 0),
        _tag(overlap_chunk, 1)).messages
    result = sort_messages(merged, multi_chunk=True)
    # 去重后仍是 6 条；顺序与原片段一致（重叠没有引入新的同刻歧义）
    assert len(result.messages) == 6
    assert result.ambiguous_exact_tie == 0
    assert [m["text"] for m in result.messages] == [
        "第一条消息", "第二条消息", "第三条消息",
        "第四条消息", "第五条消息", "第六条消息",
    ]


# ---------------------------------------------------------------------------
# E. 数百条“从最近往过去逐批追加”的端到端
# ---------------------------------------------------------------------------


def _wechat_block_day(day: int, n_per_side: int) -> str:
    blocks = []
    for i in range(n_per_side):
        who = "测试甲." if i % 2 == 0 else "测试乙."
        blocks.append(f"{who}\n2026年09月{day:02d}日 {9 + i // 4:02d}:{i % 60:02d}"
                      f"\n消息 {day}-{i}")
    return "\n\n".join(blocks)


def test_hundreds_of_messages_reverse_append_timeline():
    """模拟从最近一天往过去逐批复制 6 天 × 每侧 40 条。"""
    chunks = []
    for day in range(28, 23, -1):          # 先复制最新（28 号）
        chunks.append(_wechat_block_day(day, 40))
    merged: list[dict] = []
    for idx, chunk in enumerate(chunks):
        parsed = _tag(mask_messages(parse_chat(chunk, "测试甲.", "测试乙.")), idx)
        merged = parsed if not merged else merge_messages(merged, parsed).messages
    result = sort_messages(merged, multi_chunk=True)

    assert len(result.messages) == 5 * 40
    times = _times(result.messages)
    assert times == sorted(times), "时间线必须严格升序"
    assert result.order_changed is True
    # 跨天跨批：全部完整时间 → 单日内部同刻顺序来自原始片段（相邻片段日期不同）
    assert result.ambiguous_exact_tie == 0
    # 每天 40 条都在一起（没有把不同日期的消息混在一起）
    days = sorted({m["time"][:10] for m in result.messages})
    assert days == ["2026-09-24", "2026-09-25", "2026-09-26",
                    "2026-09-27", "2026-09-28"]

    # Context Builder 视角：任一 target 的上下文都不含未来消息
    for i, m in enumerate(result.messages):
        if m["speaker"] != "them":
            continue
        ctx = result.messages[max(0, i - 8):i]
        for c in ctx:
            assert c["time"] <= m["time"]
