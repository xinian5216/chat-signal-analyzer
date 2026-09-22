"""TA target / conversation_context 语义测试（需求 9 / 10 + 语音）。

全部 mock，绝不调用真实 Jev API。覆盖：

- “我”的消息用于理解 TA 在回应什么 → 进入 conversation_context；
- “我”的消息永远不是 target：不产生 API 请求、不产生 relationship score、
  不进 overall 分子、不进 effective_messages；
- previous 5 上下文是**双方混合**消息，不是 previous 5 条 TA 消息；
- 纯语音消息不是 target，只以中性 marker（含时长）出现在上下文里。
"""

import analyzer
from parser import MEDIA_KIND_VOICE, parse_chat
from privacy import mask_messages
from scoring import compute_conversation_stats

WECHAT_CHAT = """我
2026年08月21日 09:00
你明天还上班吗

TA
2026年08月21日 09:05
是啊

我
2026年08月21日 09:06
几点起

TA
2026年08月21日 09:20
八点多吧，看情况"""

VOICE_CHAT = """我
2026年08月22日 16:20
在忙吗

TA
2026年08月22日 16:25
[语音] 7"

我
2026年08月22日 16:26
听到了

TA
2026年08月22日 16:28
那就好"""


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class RecordingClient:
    """记录每次 system_one 调用的最小 FakeClient（绝不复用真实 API）。"""

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
            "emotion": FakeAnswer(choice="calm", probabilities={"calm": 1.0},
                                  confidence=0.9),
            "intent": FakeAnswer(choice="other", probabilities={"other": 1.0},
                                 confidence=0.9),
            "warmth": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "engagement": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
            "special_attention": FakeAnswer(score=1.0, probabilities={},
                                            confidence=0.9),
            "relationship_evidence_strength": FakeAnswer(score=2.0,
                                                         probabilities={},
                                                         confidence=0.9),
            "relational_ease": FakeAnswer(score=2.0, probabilities={},
                                          confidence=0.9),
            "romantic_signal": FakeAnswer(noul=0.1),
            "distancing_signal": FakeAnswer(noul=0.1),
        }
        return NS(answers=answers, model="fake")


def _analyze(chat: str):
    client = RecordingClient()
    messages = mask_messages(parse_chat(chat))
    results = analyzer.analyze_messages(client, messages)
    return client, messages, results


# ---------------------------------------------------------------------------
# “我”的消息：进 context，绝不做 target
# ---------------------------------------------------------------------------


def test_my_messages_enter_ta_context():
    client, messages, results = _analyze(WECHAT_CHAT)
    assert client.calls == 2                     # 只有 2 条 TA 文本消息
    # 分析 TA“是啊”时，上下文必须包含“我：你明天还上班吗”
    ctx_texts = [c["text"] for c in client.contexts[0]]
    assert ctx_texts == ["你明天还上班吗"]
    assert [c["speaker"] for c in client.contexts[0]] == ["me"]


def test_my_messages_are_never_targets():
    client, messages, results = _analyze(WECHAT_CHAT)
    assert all(e["speaker"] == "them" for e in results)
    assert all(t["speaker"] == "them" for t in client.targets)
    assert all("上班吗" not in t for t in client.target_texts)
    assert all("几点起" not in t for t in client.target_texts)


def test_my_messages_produce_no_scores_or_effective_messages():
    client, messages, results = _analyze(WECHAT_CHAT)
    stats = compute_conversation_stats(results)
    # 只有 TA 的 2 条消息进入统计分母
    assert stats["analyzed"] == 2
    assert stats["effective_messages"] <= stats["analyzed"]
    assert stats["overall"] is not None
    # 我的消息不进入 overall 分子：删掉我的消息后重算，overall 不变
    client2 = RecordingClient()
    only_them = [m for m in messages if m["speaker"] == "them"]
    results2 = analyzer.analyze_messages(client2, only_them)
    stats2 = compute_conversation_stats(results2)
    assert abs(stats["overall"] - stats2["overall"]) < 1e-9


def test_previous_context_is_mixed_not_only_ta():
    """previous 5 上下文是双方混合消息（不是 previous 5 条 TA 消息）。"""
    chat = "\n\n".join(
        f"{speaker}\n2026年08月21日 1{i}:00\n内容 {i}"
        for i, speaker in enumerate(
            ["我", "TA"] * 6
        )
    )
    client, messages, results = _analyze(chat)
    last_ctx = client.contexts[-1]
    assert len(last_ctx) == 5                      # 默认 previous 5
    assert {c["speaker"] for c in last_ctx} == {"me", "them"}
    assert sum(1 for c in last_ctx if c["speaker"] == "me") >= 2
    assert sum(1 for c in last_ctx if c["speaker"] == "them") >= 2


def test_context_never_contains_future_messages():
    """上下文只能包含目标消息之前的消息（不偷看未来）。"""
    client, messages, results = _analyze(WECHAT_CHAT)
    assert [c["text"] for c in client.contexts[0]] == ["你明天还上班吗"]
    assert [c["text"] for c in client.contexts[1]] == [
        "你明天还上班吗", "是啊", "几点起"
    ]


# ---------------------------------------------------------------------------
# 语音：不是 target，只以中性 marker 进 context
# ---------------------------------------------------------------------------


def test_voice_is_not_a_target():
    client, messages, results = _analyze(VOICE_CHAT)
    voice = [m for m in messages if m.get("media_kinds") == [MEDIA_KIND_VOICE]]
    assert len(voice) == 1
    assert voice[0]["content_type"] == "media"
    assert voice[0]["duration_seconds"] == 7
    # TA 文本消息只有“那就好”（[语音] 被跳过）
    assert client.calls == 1
    assert client.target_texts == ["那就好"]
    # 语音不产生任何 relationship score / 不进 results
    assert all("语音" not in e["text"] for e in results)


def test_voice_marker_with_duration_in_context():
    client, messages, results = _analyze(VOICE_CHAT)
    ctx = client.contexts[0]
    markers = [c["text"] for c in ctx if c["text"].startswith("[发送了")]
    assert markers == ["[发送了一条 7 秒语音，内容未知]"]
    # 上下文中不得出现时长推断、不得出现原始占位符
    for c in ctx:
        assert "[语音]" not in c["text"]
        assert "7" not in c["text"].replace("[发送了一条 7 秒语音，内容未知]", "")


def test_voice_duration_never_enters_jev_state():
    client, messages, results = _analyze(VOICE_CHAT)
    for target in client.targets:
        assert "duration_seconds" not in target
        assert set(target) == {"speaker", "text", "time"}
    for ctx in client.contexts:
        for c in ctx:
            assert set(c) == {"speaker", "text", "time"}


def test_raw_speaker_never_enters_jev_state_or_cache_key():
    """原始昵称只供本地映射；远端 state 与 cache key 都不得依赖它。"""
    context = [{"speaker": "me", "text": "在忙吗", "time": None,
                "raw_speaker": "本地昵称A"}]
    target_a = {"speaker": "them", "text": "在啊", "time": None,
                "raw_speaker": "本地昵称B"}
    target_b = {**target_a, "raw_speaker": "另一个昵称"}

    state_a = analyzer.build_state(context, target_a)
    state_b = analyzer.build_state(context, target_b)
    assert state_a == state_b
    assert set(state_a["target_message"]) == {"speaker", "text", "time"}
    assert set(state_a["conversation_context"][0]) == {"speaker", "text", "time"}

    from storage import make_cache_key
    schema = analyzer.build_questions_schema()
    key_a = make_cache_key(state_a, schema, analyzer.DEFAULT_MODEL,
                           analyzer.SCHEMA_VERSION)
    key_b = make_cache_key(state_b, schema, analyzer.DEFAULT_MODEL,
                           analyzer.SCHEMA_VERSION)
    assert key_a == key_b


def test_voice_state_gets_media_clause():
    """带时长语音 marker 的 state 仍走媒体条款（禁止猜测媒体内容）。"""
    target = {"speaker": "them", "text": "那就好", "time": None,
              "raw_speaker": "TA"}
    ctx = [{"speaker": "me", "text": "在忙吗", "time": None},
           {"speaker": "them", "text": "[发送了一条 7 秒语音，内容未知]",
            "time": None}]
    state = analyzer.build_state(ctx, target)
    assert state["analysis_rule"] == analyzer.ANALYSIS_RULE + analyzer.MEDIA_RULE_CLAUSE
    assert state["analysis_rule"] != analyzer.ANALYSIS_RULE


def test_voice_not_counted_in_stats():
    client, messages, results = _analyze(VOICE_CHAT)
    stats = compute_conversation_stats(results)
    assert stats["analyzed"] == 1
    assert stats["effective_messages"] <= 1
