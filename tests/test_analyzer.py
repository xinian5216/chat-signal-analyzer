"""analyzer 测试：使用 mock 响应，绝不调用真实 Jev API。"""

from types import SimpleNamespace

import analyzer
from analyzer import analyze_messages, build_state, extract_answers
from privacy import mask_messages


class FakeAnswer:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_fake_response():
    answers = {
        "emotion": FakeAnswer(choice="calm", probabilities={"calm": 0.6, "teasing": 0.4}, confidence=0.7),
        "intent": FakeAnswer(choice="explain", probabilities={"explain": 0.8, "other": 0.2}, confidence=0.8),
        "warmth": FakeAnswer(score=2.0, probabilities={"0": 0.1, "1": 0.2, "2": 0.7}, confidence=0.75),
        "engagement": FakeAnswer(score=1.0, probabilities={"0": 0.1, "1": 0.9}, confidence=0.6),
        "special_attention": FakeAnswer(score=0.5, probabilities={"0": 0.5, "1": 0.5}, confidence=0.5),
        "relationship_evidence_strength": FakeAnswer(
            score=2.0, probabilities={"0": 0.0, "1": 0.2, "2": 0.8}, confidence=0.7
        ),
        "relational_ease": FakeAnswer(
            score=2.0, probabilities={"0": 0.0, "1": 0.2, "2": 0.8}, confidence=0.7
        ),
        "romantic_signal": FakeAnswer(noul=0.1),
        "distancing_signal": FakeAnswer(noul=0.9),
    }
    return SimpleNamespace(answers=answers, model="jev-test")


class FakeClient:
    def __init__(self, fail_on=None):
        self.calls = 0
        self.fail_on = fail_on or set()  # 消息下标集合，这些下标抛异常

    def system_one(self, state, questions):
        # 用目标消息文本来推断下标（测试数据里文本唯一）
        text = state["target_message"]["text"]
        self.calls += 1
        if text in self.fail_on:
            raise RuntimeError("模拟网络中断")
        return make_fake_response()


def sample_messages():
    raw = [
        {"speaker": "me", "text": "在干嘛", "time": "22:30"},
        {"speaker": "them", "text": "刚下班", "time": "22:31"},
        {"speaker": "me", "text": "累不累", "time": "22:32"},
        {"speaker": "them", "text": "有点，哈哈", "time": "22:33"},
    ]
    return mask_messages(raw)


def test_state_uses_only_past_context():
    msgs = sample_messages()
    state = build_state(msgs[:3], msgs[3])
    assert len(state["conversation_context"]) == 3
    assert state["target_message"]["text"] == "有点，哈哈"
    assert "analysis_rule" in state


def test_state_context_is_turn_bounded_not_fixed_five():
    """previous 5 机械截断已被 Context Builder v2 替换：按 turn 回溯。"""
    msgs = sample_messages()
    long_context = [
        {"speaker": "me" if i % 2 == 0 else "them", "text": f"m{i}"}
        for i in range(20)
    ]
    state = build_state(long_context, msgs[1])
    ctx = state["conversation_context"]
    # 20 条交替消息 = 20 个 turn → 取最近 CONTEXT_MAX_TURNS 个完整 turn
    assert len(ctx) == 8
    assert [c["text"] for c in ctx] == [f"m{i}" for i in range(12, 20)]
    assert [c["text"] for c in ctx] != [f"m{i}" for i in range(15, 20)]  # 不是最近 5 条


def test_extract_answers_shape():
    out = extract_answers(make_fake_response())
    assert out["emotion"]["choice"] == "calm"
    assert out["warmth"]["probabilities"]["2"] == 0.7
    assert out["relationship_evidence_strength"]["score"] == 2.0
    assert out["relational_ease"]["score"] == 2.0
    assert set(out["relational_ease"]) == {"score", "probabilities", "confidence"}
    assert out["romantic_signal"] == 0.1
    assert out["distancing_signal"] == 0.9


def test_schema_v30_only_distancing_semantics_changed():
    """v3.1：只改问题描述（intent/warmth/evidence/歧义条款），q 集合不变。

    v2→v2.1 加 relational_ease；v2.2 上下文选择+白名单；v3.0 distancing
    语义；v3.1 描述级修正。每次 bump 都让旧缓存自然失效。
    """
    from storage import make_cache_key

    schema_v31 = analyzer.build_questions_schema()
    assert "relational_ease" in schema_v31
    assert len(schema_v31) == 9
    assert schema_v31["distancing_signal"] == {"type": "noul"}

    # 模拟 v2 时代的 schema（无 relational_ease 问题）与版本号
    schema_v2 = {k: v for k, v in schema_v31.items() if k != "relational_ease"}
    state = {"conversation_context": [], "target_message": {"speaker": "them", "text": "哦"}}
    key_v2 = make_cache_key(state, schema_v2, "jev-latest", "chat-signal-v2")
    key_v30 = make_cache_key(state, schema_v31, "jev-latest",
                             "chat-signal-v3.0")
    key_v31 = make_cache_key(state, schema_v31, "jev-latest",
                             "chat-signal-v3.1")
    key_v32 = make_cache_key(state, schema_v31, "jev-latest",
                             analyzer.SCHEMA_VERSION)
    assert key_v2 != key_v30 != key_v31
    assert key_v30 != key_v31  # 同问题、同 state：仅版本不同 → key 不同
    assert analyzer.SCHEMA_VERSION == "chat-signal-v3.2"


def test_relational_ease_question_is_score_with_five_levels():
    schema = analyzer.build_questions_schema()
    assert schema["relational_ease"]["type"] == "score"
    assert len(schema["relational_ease"]["criteria"]) == 5


def test_analyze_only_them_messages_and_single_call_each():
    client = FakeClient()
    results = analyze_messages(client, sample_messages())
    assert client.calls == 2  # 只有 2 条 TA 消息
    assert len(results) == 2
    assert all(e["speaker"] == "them" for e in results)
    assert all("result" in e for e in results)
    assert [e["index"] for e in results] == [1, 3]


def test_single_failure_does_not_abort_others():
    client = FakeClient(fail_on={"刚下班"})
    results = analyze_messages(client, sample_messages())
    assert results[0]["error"]  # 第 1 条失败
    assert "result" in results[1]  # 第 2 条成功


def test_cache_avoids_repeat_api_calls(tmp_path):
    from storage import Cache

    cache = Cache(tmp_path / "cache.db")
    client = FakeClient()

    r1 = analyze_messages(client, sample_messages(), cache=cache)
    assert client.calls == 2
    assert all(e["cached"] is False for e in r1)

    client2 = FakeClient()
    r2 = analyze_messages(client2, sample_messages(), cache=cache)
    assert client2.calls == 0  # 全部命中缓存
    assert all(e["cached"] is True for e in r2)
    assert r1[0]["result"] == r2[0]["result"]


def test_only_indices_for_retry():
    client = FakeClient(fail_on={"刚下班"})
    messages = sample_messages()
    cacheless_results = analyze_messages(client, messages)
    failed = {e["index"] for e in cacheless_results if e.get("error")}

    retry_client = FakeClient()
    retried = analyze_messages(
        retry_client, messages, only_indices=failed
    )
    assert retry_client.calls == 1
    assert "result" in retried[0]


def test_classify_error_never_leaks_headers():
    # SDK 未安装异常类型时应安全降级；已安装时应映射为中文提示
    msg = analyzer.classify_error(RuntimeError("boom"))
    assert isinstance(msg, str) and msg
    assert "Authorization" not in msg and "Bearer" not in msg
