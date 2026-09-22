"""多片段追加：本地 fingerprint、合并与去重（0 Jev API）。

微信一次复制带出的条数有限，用户分几次复制后逐段“追加到当前聊天”。
这里覆盖：

- fingerprint 组成与确定性；
- 重叠片段去重（A 1~100 + B 90~190 ≠ 201 条）；
- 无时间戳消息的保守去重（“嗯” “嗯” 不得被误删）；
- 本地 metadata 不进入 Jev state；
- 500 / 1000 条消息的解析性能。
"""

import time

import pytest

from merge import message_fingerprint, merge_messages
from parser import parse_chat

# 虚构昵称的合成微信聊天（每条 4 行：昵称 / 时间 / 正文 / 空行）
NAMES = ("用户A", "用户B")


def _wechat_text(count: int, prefix: str = "msg") -> str:
    lines: list[str] = []
    for i in range(count):
        lines.append(NAMES[i % 2])
        lines.append(f"2026年08月{1 + i % 28:02d}日 {i % 24:02d}:{i % 60:02d}")
        lines.append(f"{prefix} {i} 行 16:30 集合 https://example.com/{i}")
        lines.append("")
    return "\n".join(lines)


def _message(raw_speaker="用户A", time=None, text="嗯", kinds=None):
    return {
        "speaker": "them" if raw_speaker == "用户B" else "me",
        "text": text,
        "time": time,
        "raw_speaker": raw_speaker,
        "content_type": "text",
        "media_kinds": kinds or [],
    }


def _blocks(text: str) -> list[str]:
    return text.split("\n\n")


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_and_content_based():
    a = _message("用户A", "2026-08-21 13:43", "在吗")
    b = _message("用户A", "2026-08-21 13:43", "在吗")
    assert message_fingerprint(a) == message_fingerprint(b)
    # 空白差异不影响（规范化）
    c = _message(" 用户A ", "2026-08-21 13:43", " 在吗 ")
    assert message_fingerprint(c) == message_fingerprint(a)


def test_fingerprint_distinguishes_real_differences():
    base = _message("用户A", "2026-08-21 13:43", "在吗")
    assert message_fingerprint(_message("用户B", "2026-08-21 13:43", "在吗")) \
        != message_fingerprint(base)
    assert message_fingerprint(_message("用户A", "2026-08-21 13:44", "在吗")) \
        != message_fingerprint(base)
    assert message_fingerprint(_message("用户A", "2026-08-21 13:43", "在的")) \
        != message_fingerprint(base)
    # 同一时间戳下“嗯 嗯”是两条真实消息
    assert message_fingerprint(_message("用户A", None, "嗯")) != \
        message_fingerprint(_message("用户B", None, "嗯"))


# ---------------------------------------------------------------------------
# 合并与去重
# ---------------------------------------------------------------------------


def test_append_non_overlapping_chunks():
    a = parse_chat(_wechat_text(50, "A"))
    b = parse_chat(_wechat_text(50, "B"))
    result = merge_messages(a, b)
    assert result.chunk_size == 50
    assert result.duplicates == 0
    assert result.added == 50
    assert result.total == 100
    assert len(result.messages) == 100


def test_append_overlapping_chunks_dedup():
    full = parse_chat(_wechat_text(200))
    chunk_a = full[:100]
    # 片段 B 与 A 在末尾重叠 10 条，并向后延伸 10 条
    chunk_b = full[90:110]
    result = merge_messages(chunk_a, chunk_b)
    assert result.chunk_size == 20
    assert result.duplicates == 10          # 重叠的 90~100
    assert result.added == 10               # 101~110
    assert result.total == 110
    assert len(result.messages) == 110


def test_append_same_chunk_twice_adds_nothing():
    a = parse_chat(_wechat_text(30))
    result = merge_messages(a, a)
    assert result.added == 0
    assert result.duplicates == 30
    assert result.total == 30


def test_merge_does_not_mutate_inputs():
    a = parse_chat(_wechat_text(10))
    b = parse_chat(_wechat_text(10))
    before_a, before_b = list(a), list(b)
    merge_messages(a, b)
    assert a == before_a and b == before_b


def test_no_timestamp_adjacent_duplicate_is_dropped():
    existing = [_message("用户A", None, "嗯"), _message("用户A", None, "嗯")]
    result = merge_messages(existing, [_message("用户A", None, "嗯")])
    assert result.duplicates == 1
    assert result.adjacent_duplicates == 1
    assert result.total == 2


def test_no_timestamp_two_real_replies_are_kept():
    """宁可保留重复，也不要误删两条真实相同回复。"""
    existing = [_message("用户A", None, "嗯"), _message("用户A", None, "嗯")]
    result = merge_messages(existing, [_message("用户A", None, "嗯")])
    assert result.total >= 2


def test_no_timestamp_non_adjacent_is_kept():
    existing = [_message("用户A", None, "嗯"), _message("用户A", None, "哈")]
    result = merge_messages(existing, [_message("用户A", None, "嗯")])
    assert result.duplicates == 0
    assert result.total == 3


def test_no_timestamp_different_speaker_is_kept():
    existing = [_message("用户A", None, "嗯")]
    result = merge_messages(existing, [_message("用户B", None, "嗯")])
    assert result.duplicates == 0
    assert result.total == 2


def test_no_timestamp_previous_has_timestamp_is_kept():
    """上一条有时间戳、这一条没有 → 无法证明重复，保留。"""
    existing = [_message("用户A", "2026-08-21 13:43", "嗯")]
    result = merge_messages(existing, [_message("用户A", None, "嗯")])
    assert result.duplicates == 0
    assert result.total == 2


def test_media_voice_durations_are_distinct_messages():
    a = parse_chat("用户A\n2026年08月21日 13:43\n[语音] 7\"")
    b = parse_chat("用户A\n2026年08月21日 13:43\n[语音] 8\"")
    result = merge_messages(a, b)
    assert result.added == 1          # 时长不同 → 不同消息
    assert result.total == 2
    result2 = merge_messages(a, a)
    assert result2.duplicates == 1     # 完全相同 → 重复


# ---------------------------------------------------------------------------
# 本地 metadata 绝不进入 Jev state
# ---------------------------------------------------------------------------


def test_metadata_never_enters_jev_state():
    import analyzer

    target = {"speaker": "them", "text": "在吗", "time": "2026-08-21 13:43",
              "raw_speaker": "用户B"}
    context = [{"speaker": "me", "text": "在吗", "time": None}]
    state = analyzer.build_state(context, target)
    assert set(state["target_message"]) == {"speaker", "text", "time", "raw_speaker"}
    assert "fingerprint" not in state["target_message"]
    assert "chunk_id" not in state["target_message"]
    assert "duration_seconds" not in state["target_message"]
    for key in state:
        assert key in ("conversation_context", "target_message", "analysis_rule")


def test_fingerprint_is_local_only():
    """fingerprint 不是 message 字段：合并结果里不会凭空多出本地 metadata。"""
    msgs = parse_chat(_wechat_text(5))
    result = merge_messages(msgs, msgs)
    for m in result.messages:
        assert "fingerprint" not in m
        assert "chunk_id" not in m
        assert "source" not in m


# ---------------------------------------------------------------------------
# 性能
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [500, 1000])
def test_large_sample_parse_is_fast(count):
    text = _wechat_text(count)
    start = time.perf_counter()
    msgs = parse_chat(text)
    elapsed = time.perf_counter() - start
    assert len(msgs) == count
    # 普通电脑上解析应明显小于 1 秒；这里用宽松阈值避免 CI 抖动
    assert elapsed < 3.0, f"{count} 条解析耗时 {elapsed:.2f}s"


def test_large_sample_merge_is_fast():
    full = parse_chat(_wechat_text(500))
    start = time.perf_counter()
    result = merge_messages(full, full[400:])
    elapsed = time.perf_counter() - start
    assert result.duplicates == 100
    assert result.total == 500
    assert elapsed < 3.0, f"合并耗时 {elapsed:.2f}s"


def test_blank_lines_do_not_make_parsing_quadratic():
    """大量空行不得把解析拖成 O(n²)。"""
    blocks = [
        f"{NAMES[i % 2]}\n2026年08月{1 + i % 28:02d}日 {i % 24:02d}:{i % 60:02d}\n"
        f"消息 {i} 行 16:30 集合"
        for i in range(300)
    ]
    text = ("\n" * 6).join(blocks)          # 每两条消息之间 5 个空行
    start = time.perf_counter()
    msgs = parse_chat(text)
    elapsed = time.perf_counter() - start
    assert len(msgs) == 300
    assert elapsed < 3.0, f"空行版耗时 {elapsed:.2f}s"
