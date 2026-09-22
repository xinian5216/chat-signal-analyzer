"""昵称映射链路回归测试（f16625b 回归的修复）。

真实缺陷（392 条样本）：participant detection 正常（参与者 = 2），但应用
“我 = 陌寒. / TA = 赵老狗”后统计变成 我 0 条、TA 0 条、unknown 392 条。

链路：``parse_chat`` → ``mask_messages`` → ``detect_participants`` →
**用选中的昵称重建** → canonical speaker（me / them / unknown）。

规则（严禁根据内容猜人）：只比对结构化 ``raw_speaker``，仅做 strip 后的
精确比较；``陌寒.`` 带句点本身就是合法昵称。

昵称为虚构样本，不含任何真实私人聊天内容。
"""

from collections import Counter

from merge import merge_messages
from parser import detect_participants, parse_chat
from privacy import mask_messages

ME_NICK = "陌寒."
TA_NICK = "赵老狗"

NICKNAME_FIXTURE = """陌寒.
2026年08月14日 11:33
她我觉得挺好的

赵老狗
2026年08月14日 11:34
行

赵老狗
2026年08月14日 11:34
我看下学期的老师都不太认识

陌寒.
2026年08月14日 11:34
挺负责的"""

URL_AND_TIME_FIXTURE = """陌寒.
2026年08月14日 11:35
https://www.xinian5216.com 行 16:30到商场？

赵老狗
2026年08月14日 11:36
行那就16:30吧"""


def _rebuild(chunks, my_name, them_name):
    """与 ``app.rebuild_messages_from_chunks`` 等价的纯本地重建。

    昵称由调用方**显式传入**——这正是 f16625b 回归的修复点：绝不在函数
    内部读取尚未写入的映射状态。
    """
    merged: list[dict] = []
    for chunk in chunks:
        parsed = mask_messages(parse_chat(chunk, my_name, them_name))
        merged = parsed if not merged else merge_messages(merged, parsed).messages
    return merged


def _counts(messages):
    return Counter(m["speaker"] for m in messages)


# ---------------------------------------------------------------------------
# 分阶段断言：parse → participants → 重建
# ---------------------------------------------------------------------------


def test_first_parse_raw_speaker_is_correct():
    """初次 parse（未指定昵称）raw_speaker 必须正确，speaker 全是 unknown。"""
    msgs = mask_messages(parse_chat(NICKNAME_FIXTURE))
    assert [m["raw_speaker"] for m in msgs] == [ME_NICK, TA_NICK, TA_NICK, ME_NICK]
    assert [m["speaker"] for m in msgs] == ["unknown"] * 4
    assert _counts(msgs) == {"unknown": 4}


def test_mask_messages_preserves_raw_speaker():
    msgs = mask_messages(parse_chat(NICKNAME_FIXTURE))
    assert detect_participants(msgs) == [ME_NICK, TA_NICK]


def test_mapping_produces_me_and_them():
    msgs = _rebuild([NICKNAME_FIXTURE], ME_NICK, TA_NICK)
    assert [m["speaker"] for m in msgs] == ["me", "them", "them", "me"]
    assert [m["raw_speaker"] for m in msgs] == [ME_NICK, TA_NICK, TA_NICK, ME_NICK]
    assert _counts(msgs) == {"me": 2, "them": 2}


def test_no_unknown_when_all_messages_have_raw_speaker():
    """participants == 两个昵称且全部消息都有 raw_speaker → unknown 必须为 0。"""
    msgs = _rebuild([NICKNAME_FIXTURE], ME_NICK, TA_NICK)
    assert detect_participants(msgs) == [ME_NICK, TA_NICK]
    assert [m for m in msgs if m["speaker"] == "unknown"] == []


def test_rebuild_does_not_lose_raw_speaker():
    msgs = _rebuild([NICKNAME_FIXTURE], ME_NICK, TA_NICK)
    assert all(m["raw_speaker"] in (ME_NICK, TA_NICK) for m in msgs)
    assert all(m["raw_speaker"] not in (None, "", "unknown") for m in msgs)


# ---------------------------------------------------------------------------
# 昵称比较规则：strip + 精确
# ---------------------------------------------------------------------------


def test_nickname_compared_exactly_after_strip():
    """只做 strip + 精确比较：不删句点、不 lower、不模糊、不 substring。"""
    padded = "   " + NICKNAME_FIXTURE + "   \n\n"
    msgs = _rebuild([padded], ME_NICK, TA_NICK)
    assert [m["speaker"] for m in msgs] == ["me", "them", "them", "me"]

    # 拼写不同（少一个句点）不视为同一昵称
    other = _rebuild([NICKNAME_FIXTURE], "陌寒", TA_NICK)
    assert other[0]["speaker"] == "unknown"
    assert other[1]["speaker"] == "them"


def test_mapping_never_guesses_from_content_or_position():
    chat = (
        "赵老狗\n2026年08月14日 11:33\n我是说我今天有空\n\n"
        "陌寒.\n2026年08月14日 11:34\n我也是"
    )
    msgs = _rebuild([chat], ME_NICK, TA_NICK)
    assert [m["speaker"] for m in msgs] == ["them", "me"]


def test_mapping_with_only_one_side_keeps_existing_semantics():
    """只指定一侧时，其余具名发言人归入对面（项目既有语义，未改动）。"""
    msgs = _rebuild([NICKNAME_FIXTURE], ME_NICK, None)
    assert [m["speaker"] for m in msgs] == ["me", "them", "them", "me"]


# ---------------------------------------------------------------------------
# URL / 16:30 样本 + 映射（确认与其它修复叠加后仍正确）
# ---------------------------------------------------------------------------


def test_url_and_time_bodies_keep_mapping():
    msgs = _rebuild([URL_AND_TIME_FIXTURE], ME_NICK, TA_NICK)
    assert detect_participants(msgs) == [ME_NICK, TA_NICK]
    assert [m["speaker"] for m in msgs] == ["me", "them"]
    assert [m for m in msgs if m["speaker"] == "unknown"] == []
    # URL 由 privacy 层脱敏（正文结构仍完整保留），16:30 句子原样保留
    assert msgs[0]["text"] == "<URL> 行 16:30到商场？"
    assert msgs[1]["text"] == "行那就16:30吧"


def test_chunk_append_then_remap_keeps_speaker():
    """多片段重建：昵称显式传入后，原始消息不会全部变 unknown。"""
    chunk_a = NICKNAME_FIXTURE
    chunk_b = (
        "赵老狗\n2026年08月14日 11:40\n好的\n\n"
        "陌寒.\n2026年08月14日 11:41\n行 16:30到商场？"
    )
    merged_first_parse = _rebuild([chunk_a], None, None)
    assert _counts(merged_first_parse) == {"unknown": 4}

    msgs = _rebuild([chunk_a, chunk_b], ME_NICK, TA_NICK)
    assert [m["speaker"] for m in msgs] == [
        "me", "them", "them", "me", "them", "me"
    ]
    assert _counts(msgs) == {"me": 3, "them": 3}
    assert [m["raw_speaker"] for m in msgs] == [
        ME_NICK, TA_NICK, TA_NICK, ME_NICK, TA_NICK, ME_NICK
    ]
