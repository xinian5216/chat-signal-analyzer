"""Context Builder v2 测试：turn-aware + 有界预算的上下文选择。

全部 mock，绝不调用真实 Jev API。覆盖需求：

1.  previous 5 机械截断被替换（完整保留 turn，不再拦腰切断）
2.  连续同 speaker 消息形成 turn
3.  TA 回复“我”连续三条 → 一个 me turn，预算允许时完整保留
4.  上下文包含双方消息
5.  “我”的消息永远不是 target
6.  context 永远没有未来消息
7.  target 本身不进入自己的 context
8.  max turns 生效
9.  max messages 生效
10. max chars 生效
11. 单个超长 turn 的确定性裁剪（含 P0：紧邻消息整条保留、不截断）
12. target 自身再长也不得被 context budget 截断
13. pure media 仍不是 target
14. media neutral marker 可以进入 context
15. context 中媒体仍触发 MEDIA_RULE_CLAUSE
16. 中文 / 英文 / 多行 / URL / JSON 不影响 turn grouping
17. serial 与 concurrent 模式构造出的 state 完全一致
18. 同输入重复运行 context 完全一致
19. v2.2 cache key 与旧 v2.1 不同
20. cache hit 后仍然 0 API call
21. Context Builder 本身绝不调用 Jev
"""

import json
from pathlib import Path
from types import SimpleNamespace

import analyzer
import context_builder
from analyzer import analyze_messages, build_state
from context_builder import (
    CONTEXT_MAX_CHARS,
    CONTEXT_MAX_MESSAGES,
    CONTEXT_MAX_TURNS,
    debug_context_view,
    group_turns,
    select_context,
)
from parser import parse_chat
from privacy import mask_messages
from storage import make_cache_key

# ---------------------------------------------------------------------------
# 测试替身（绝不调用真实 Jev API）
# ---------------------------------------------------------------------------


class _FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class RecordingClient:
    """记录每次 system_one 调用的 target 与完整 state。"""

    def __init__(self):
        self.calls = 0
        self.states: list[dict] = []

    def system_one(self, state, questions):
        self.calls += 1
        self.states.append(state)
        answers = {
            "emotion": _FakeAnswer(choice="calm", probabilities={"calm": 1.0},
                                   confidence=0.9),
            "intent": _FakeAnswer(choice="other", probabilities={"other": 1.0},
                                  confidence=0.9),
            "warmth": _FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "engagement": _FakeAnswer(score=2.0, probabilities={},
                                      confidence=0.9),
            "special_attention": _FakeAnswer(score=1.0, probabilities={},
                                             confidence=0.9),
            "relationship_evidence_strength": _FakeAnswer(
                score=2.0, probabilities={}, confidence=0.9),
            "relational_ease": _FakeAnswer(score=2.0, probabilities={},
                                           confidence=0.9),
            "romantic_signal": _FakeAnswer(noul=0.1),
            "distancing_signal": _FakeAnswer(noul=0.1),
        }
        return SimpleNamespace(answers=answers, model="fake")


def _msg(speaker, text, time=None):
    return {"speaker": speaker, "text": text, "time": time}


def _analyze(messages, **kwargs):
    client = RecordingClient()
    results = analyze_messages(client, messages, **kwargs)
    return client, results


def _ctx_texts(state):
    return [c["text"] for c in state["conversation_context"]]


# ---------------------------------------------------------------------------
# 1~4：turn 语义 / 完整保留 / 双方混合
# ---------------------------------------------------------------------------

TURN_CHAT = [
    _msg("me", "在吗", "21:00"),
    _msg("them", "在", "21:01"),
    _msg("me", "周末出来玩吗", "21:02"),
    _msg("them", "看情况", "21:03"),
    _msg("me", "我周六有空", "21:04"),
    _msg("me", "周日也有空", "21:05"),
    _msg("me", "你定时间就行", "21:06"),
    _msg("them", "那周六下午吧", "21:07"),
]


def test_previous_five_replaced_by_complete_turns():
    """需求 1：不再机械取最近 5 条——me 三连 turn 完整保留，窗口为 7 条。"""
    client, _ = _analyze(TURN_CHAT)
    assert client.calls == 3                       # 只有 TA 文本消息是 target
    last = client.states[-1]
    # 旧算法会给最近 5 条（把 me 三连拦腰切断且丢掉开头）；新算法 5 个 turn 全保留
    assert _ctx_texts(last) == [m["text"] for m in TURN_CHAT[:-1]]
    assert len(last["conversation_context"]) == 7  # > 5，且包含完整 me turn
    texts = _ctx_texts(last)
    assert texts[4:7] == ["我周六有空", "周日也有空", "你定时间就行"]  # 三连未被切断


def test_group_turns_merges_consecutive_same_speaker():
    """需求 2：连续同 speaker 消息组成一个 turn。"""
    turns = group_turns([
        _msg("me", "a"), _msg("me", "b"),
        _msg("them", "c"), _msg("them", "d"), _msg("them", "e"),
        _msg("me", "f"),
    ])
    assert [t["speaker"] for t in turns] == ["me", "them", "me"]
    assert [len(t["messages"]) for t in turns] == [2, 3, 1]


def test_ta_reply_to_my_burst_keeps_full_me_turn():
    """需求 3：TA 回复“我”连续三条 → 三条同属一个 me turn，完整保留。"""
    prefix = TURN_CHAT[:-1]
    ctx = select_context(prefix)
    assert [c["text"] for c in ctx] == [m["text"] for m in prefix]
    view = debug_context_view(7, ctx)
    assert view["turn_speakers"][-1] == "me"
    assert view["turn_count"] == 5


def test_context_contains_both_speakers():
    """需求 4：上下文是双方混合消息。"""
    msgs = [_msg("me", "你好"), _msg("them", "嗨"), _msg("me", "在干嘛"),
            _msg("them", "刚下班")]
    client, _ = _analyze(msgs)
    seen = set()
    for state in client.states:
        speakers = {c["speaker"] for c in state["conversation_context"]}
        assert speakers <= {"me", "them"}
        seen |= speakers
    assert seen == {"me", "them"}   # 至少一个 target 的上下文同时包含双方


# ---------------------------------------------------------------------------
# 5~7：target 语义 / 不偷看未来
# ---------------------------------------------------------------------------

FUTURE_CHAT = [
    _msg("me", "问题一", "09:00"),
    _msg("them", "回答一", "09:05"),
    _msg("me", "问题二", "09:06"),
    _msg("them", "回答二", "09:10"),
    _msg("me", "问题三", "09:11"),
    _msg("them", "回答三", "09:20"),
]


def test_me_messages_are_never_targets():
    """需求 5：“我”的消息可以进 context，但永远不是 Jev target。"""
    client, results = _analyze(FUTURE_CHAT)
    assert client.calls == 3                       # 只有 3 条 TA 消息
    assert all(t["speaker"] == "them" for t in
               (s["target_message"] for s in client.states))
    assert all(e["speaker"] == "them" for e in results)
    assert all("问题" not in s["target_message"]["text"] for s in client.states)


def test_context_never_contains_future_messages():
    """需求 6：上下文只能来自 target 之前（不偷看未来）。"""
    client, _ = _analyze(FUTURE_CHAT)
    texts = [m["text"] for m in FUTURE_CHAT]
    for state in client.states:
        target_pos = texts.index(state["target_message"]["text"])
        prefix_texts = texts[:target_pos]
        ctx_texts = _ctx_texts(state)
        # context 是 prefix 的顺序子序列
        it = iter(prefix_texts)
        assert all(any(c == p for p in it) for c in ctx_texts)


def test_target_itself_never_in_context():
    """需求 7：当前 target 不得重复出现在自己的 context 里。"""
    client, _ = _analyze(FUTURE_CHAT)
    for state in client.states:
        assert state["target_message"]["text"] not in _ctx_texts(state)


# ---------------------------------------------------------------------------
# 8~11：三种预算 + 超长 turn 确定性裁剪
# ---------------------------------------------------------------------------


def test_max_turns_applied():
    """需求 8：turn 预算生效（完整 turn 粒度）。"""
    prefix = [_msg("me" if i % 2 == 0 else "them", f"m{i}") for i in range(12)]
    ctx = select_context(prefix, max_turns=3)
    assert len(ctx) == 3
    assert [c["text"] for c in ctx] == ["m9", "m10", "m11"]


def test_max_messages_applied():
    """需求 9：消息预算生效（单 turn 内从靠近 target 的一侧取）。"""
    prefix = [_msg("me", f"burst{i}") for i in range(10)]  # 一个 turn
    ctx = select_context(prefix, max_messages=4)
    assert [c["text"] for c in ctx] == ["burst6", "burst7", "burst8", "burst9"]


def test_max_chars_applied():
    """需求 10：字符预算生效。"""
    prefix = [_msg("me", f"{'x' * 100}{i}") for i in range(6)]
    ctx = select_context(prefix, max_chars=250)
    # 每条 101 字符（100 x + 序号）：2 条 = 202 ≤ 250，3 条 = 303 > 250
    assert len(ctx) == 2
    assert ctx[-1]["text"] == "x" * 100 + "5"


def test_oversized_turn_trimmed_deterministically():
    """需求 11：单个超长 turn 从靠近 target 的一侧确定性裁剪；重复运行一致。"""
    prefix = [_msg("me", f"{'y' * 100}{i}") for i in range(5)]
    runs = [select_context(prefix, max_chars=250) for _ in range(3)]
    expected = ["y" * 100 + "3", "y" * 100 + "4"]
    for ctx in runs:
        assert [c["text"] for c in ctx] == expected
    assert [[c["text"] for c in ctx] for ctx in runs] == [expected] * 3


def test_p0_nearest_message_kept_whole_even_over_char_budget():
    """P0：紧邻 target 的上一条消息整条保留（不做消息内截断）。"""
    long_text = "z" * 5000
    prefix = [_msg("me", "更早的", "10:00"), _msg("them", long_text, "10:01")]
    ctx = select_context(prefix, max_chars=100)
    assert len(ctx) == 1
    assert ctx[0]["text"] == long_text             # 整条保留，未被截断


# ---------------------------------------------------------------------------
# 12：target 自身不受上下文预算约束
# ---------------------------------------------------------------------------


def test_target_never_truncated_by_context_budget():
    """需求 12：target 再长也完整；上下文预算只约束 conversation_context。"""
    huge_target = "长" * 9000
    prefix = [_msg("me", "a"), _msg("them", "b"), _msg("me", "c")]
    state = build_state(prefix, _msg("them", huge_target))
    assert state["target_message"]["text"] == huge_target
    assert len(state["conversation_context"]) == 3  # 上下文照常（不受 target 长度挤压）
    # 字符预算只数 context：context 总字符 3，远在预算内
    assert sum(len(c["text"]) for c in state["conversation_context"]) == 3


# ---------------------------------------------------------------------------
# 13~15：媒体规则保持不变
# ---------------------------------------------------------------------------

MEDIA_CHAT = """我: 看这个
TA: [图片] 微信图片_20260908.dat
我: 怎么样
TA: 你觉得呢"""


def test_pure_media_still_not_target():
    """需求 13：纯媒体消息仍不是 target（0 API、不进 results）。"""
    msgs = mask_messages(parse_chat(MEDIA_CHAT))
    client, results = _analyze(msgs)
    assert client.calls == 1
    assert client.states[0]["target_message"]["text"] == "你觉得呢"
    assert all("[发送了一张图片" not in e["text"] for e in results)


def test_media_marker_can_enter_context():
    """需求 14：媒体中性 marker 可以出现在 context 中。"""
    msgs = mask_messages(parse_chat(MEDIA_CHAT))
    client, _ = _analyze(msgs)
    ctx_texts = _ctx_texts(client.states[0])
    assert "[发送了一张图片，内容未知]" in ctx_texts


def test_media_marker_in_context_triggers_media_clause():
    """需求 15：context 中含媒体 marker 时 MEDIA_RULE_CLAUSE 保持生效。"""
    target = _msg("them", "你觉得呢")
    ctx = [_msg("me", "看这个"), _msg("them", "[发送了一张图片，内容未知]")]
    state = build_state(ctx, target)
    assert state["analysis_rule"] == analyzer.ANALYSIS_RULE + analyzer.MEDIA_RULE_CLAUSE
    # 无媒体时仍是基础 rule
    state2 = build_state([_msg("me", "看这个")], target)
    assert state2["analysis_rule"] == analyzer.ANALYSIS_RULE


# ---------------------------------------------------------------------------
# 16：内容不影响 turn 分组
# ---------------------------------------------------------------------------


def test_content_does_not_affect_turn_grouping():
    """需求 16：中文 / 英文 / 多行 / URL / JSON / 代码不改变 turn 划分。"""
    tricky = [
        _msg("me", "你看这个 https://example.com/a?b=1 行不行"),
        _msg("me", '{\n  "code": 200,\n  "ok": true\n}'),
        _msg("me", "# 标题\n\n- 列表\n\n```python\nprint('hi')\n```"),
        _msg("me", "行 16:30到大悦城？版本1:30应该能好"),
        _msg("them", "OK: noted\nsee you at 16:30"),
        _msg("them", "收到～"),
    ]
    turns = group_turns(tricky)
    assert [t["speaker"] for t in turns] == ["me", "them"]
    assert len(turns[0]["messages"]) == 4 and len(turns[1]["messages"]) == 2
    # 消息原文不被修改
    assert turns[0]["messages"][1]["text"] == tricky[1]["text"]


# ---------------------------------------------------------------------------
# 17~18：确定性与并发一致性
# ---------------------------------------------------------------------------


def _big_chat(n=24):
    msgs = []
    for i in range(n):
        speaker = "me" if i % 3 else "them"
        msgs.append(_msg(speaker, f"消息{i:02d}-{'字' * (i % 7)}"))
    return msgs


def test_serial_and_concurrent_states_identical():
    """需求 17：serial 与 concurrent 构造出的 state 完全一致。"""
    msgs = _big_chat()
    serial_client, _ = _analyze(msgs, max_workers=1)
    par_client, _ = _analyze(msgs, max_workers=4)
    assert serial_client.calls == par_client.calls
    dump = lambda cl: json.dumps(  # noqa: E731 - 测试内短 lambda
        cl.states, ensure_ascii=False, sort_keys=True, default=str)
    assert dump(serial_client) == dump(par_client)


def test_repeated_runs_produce_identical_context():
    """需求 18：同输入重复运行 context 完全一致。"""
    msgs = _big_chat()
    c1, _ = _analyze(msgs)
    c2, _ = _analyze(msgs)
    assert [_ctx_texts(s) for s in c1.states] == [_ctx_texts(s) for s in c2.states]
    # 选择函数本身幂等：对选中窗口再选一次结果不变
    prefix = [_msg("me" if i % 2 else "them", f"t{i}-{'x' * (50 + i)}")
              for i in range(30)]
    once = select_context(prefix)
    twice = select_context(once)
    assert [c["text"] for c in twice] == [c["text"] for c in once]


# ---------------------------------------------------------------------------
# 19~20：缓存
# ---------------------------------------------------------------------------


def test_v22_cache_key_differs_from_v21():
    """需求 19：SCHEMA_VERSION=v2.2，同 state/schema 下与 v2.1 key 不同。"""
    assert analyzer.SCHEMA_VERSION == "chat-signal-v2.2"
    state = build_state([_msg("me", "在吗")], _msg("them", "在"))
    schema = analyzer.build_questions_schema()
    key_v22 = make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                             analyzer.SCHEMA_VERSION)
    key_v21 = make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                             "chat-signal-v2.1")
    assert key_v22 != key_v21


def test_cache_hit_still_zero_api_calls(tmp_path):
    """需求 20：缓存命中后仍然 0 次 API 调用。"""
    from storage import Cache

    cache = Cache(tmp_path / "cache.db")
    msgs = FUTURE_CHAT
    c1, r1 = _analyze(msgs, cache=cache)
    assert c1.calls == 3 and all(e["cached"] is False for e in r1)

    c2, r2 = _analyze(msgs, cache=cache)
    assert c2.calls == 0                                # 全部命中缓存
    assert all(e["cached"] is True for e in r2)
    assert r1[0]["result"] == r2[0]["result"]


# ---------------------------------------------------------------------------
# 21：Context Builder 是纯逻辑，绝不调用 Jev
# ---------------------------------------------------------------------------


def test_context_builder_never_calls_jev():
    """需求 21：context_builder 模块不含任何 API / 网络调用能力。"""
    src = Path(context_builder.__file__).read_text(encoding="utf-8")
    for token in ("system_one", "typesafe", "requests", "httpx", "urllib",
                  "socket", "TypeSafeClient", "import os", "subprocess"):
        assert token not in src, f"context_builder.py 不应包含 {token!r}"


# ---------------------------------------------------------------------------
# 附：debug 辅助只含结构统计，不含聊天文本
# ---------------------------------------------------------------------------


def test_debug_context_view_contains_no_message_text():
    view = debug_context_view(3, select_context(TURN_CHAT[:-1]))
    assert view["target_index"] == 3
    assert view["context_message_count"] == 7
    assert view["turn_count"] == 5
    assert view["char_count"] == sum(len(m["text"]) for m in TURN_CHAT[:-1])
    assert view["speaker_sequence"] == [m["speaker"] for m in TURN_CHAT[:-1]]
    assert view["turn_speakers"] == ["me", "them", "me", "them", "me"]
    for value in view.values():
        assert isinstance(value, (int, list))
    for m in TURN_CHAT[:-1]:
        assert m["text"] not in repr(view)          # 绝不泄露聊天文本


def test_default_budgets_are_centralized():
    assert CONTEXT_MAX_TURNS == 8
    assert CONTEXT_MAX_MESSAGES == 12
    assert CONTEXT_MAX_CHARS == 4000
    # 默认预算下：短消息聊天不受字符/消息预算限制，只受 turn 预算约束
    prefix = [_msg("me" if i % 2 == 0 else "them", f"s{i}") for i in range(30)]
    assert len(select_context(prefix)) == CONTEXT_MAX_TURNS
    assert sum(len(c["text"]) for c in select_context(prefix)) <= CONTEXT_MAX_CHARS
