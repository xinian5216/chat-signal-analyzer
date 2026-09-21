"""缓存 key 稳定性与读写测试。"""

import sqlite3
import threading

import pytest

from analyzer import analyze_messages
from privacy import mask_messages
from storage import Cache, make_cache_key

STATE = {
    "conversation_context": [{"speaker": "me", "text": "在吗", "time": None}],
    "target_message": {"speaker": "them", "text": "在的", "time": "22:31"},
    "analysis_rule": "rule",
}
SCHEMA = {"emotion": {"type": "choice", "criteria": {"calm": "平静"}}}


def test_cache_key_is_stable():
    k1 = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    k2 = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    assert k1 == k2
    assert len(k1) == 64  # SHA256 hex


def test_cache_key_changes_with_any_input():
    base = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    assert make_cache_key({**STATE, "analysis_rule": "other"}, SCHEMA, "jev-latest", "v1") != base
    assert make_cache_key(STATE, SCHEMA, "jev-other", "v1") != base
    assert make_cache_key(STATE, SCHEMA, "jev-latest", "v2") != base


def test_cache_key_unicode_stable():
    k1 = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    # 等价但键序不同的 dict 应产生相同 key
    reordered = {"target_message": STATE["target_message"],
                 "conversation_context": STATE["conversation_context"],
                 "analysis_rule": "rule"}
    k2 = make_cache_key(reordered, SCHEMA, "jev-latest", "v1")
    assert k1 == k2


def test_cache_roundtrip(tmp_path):
    cache = Cache(tmp_path / "test_cache.db")
    key = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    assert cache.get(key) is None
    payload = {"emotion": {"choice": "calm", "probabilities": {"calm": 1.0}, "confidence": 0.9}}
    cache.set(key, payload)
    assert cache.get(key) == payload
    # 覆盖写
    cache.set(key, {"emotion": {"choice": "happy"}})
    assert cache.get(key) == {"emotion": {"choice": "happy"}}
    cache.clear()
    assert cache.get(key) is None
    cache.close()


def test_cache_survives_reopen(tmp_path):
    db = tmp_path / "test_cache.db"
    key = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    c1 = Cache(db)
    c1.set(key, {"a": 1})
    c1.close()
    c2 = Cache(db)
    assert c2.get(key) == {"a": 1}
    c2.close()


# ---------------------------------------------------------------------------
# 跨线程回归测试（Streamlit rerun 会在不同线程复用同一 Cache 实例）
# ---------------------------------------------------------------------------


def test_same_cache_instance_cross_thread_set_get(tmp_path):
    """线程 A set、线程 B get 同一个 Cache 实例，不得抛 ProgrammingError。"""
    cache = Cache(tmp_path / "thread_cache.db")
    key = make_cache_key(STATE, SCHEMA, "jev-latest", "v2")
    errors: list[Exception] = []
    got: list[dict | None] = []

    def writer():
        try:
            cache.set(key, {"from": "thread_a"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            got.append(cache.get(key))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=writer)
    t1.start()
    t1.join()
    t2 = threading.Thread(target=reader)
    t2.start()
    t2.join()

    assert errors == []
    assert got == [{"from": "thread_a"}]


def test_two_threads_sequential_get_set(tmp_path):
    """两个线程连续 get/set 同一 Cache 实例（主线程与工作线程交替）。"""
    cache = Cache(tmp_path / "thread_seq.db")
    key = make_cache_key(STATE, SCHEMA, "jev-latest", "v2")
    errors: list[Exception] = []

    def worker():
        try:
            cache.set(key, {"step": 1})
            assert cache.get(key) == {"step": 1}
            cache.set(key, {"step": 2})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    cache.set(key, {"step": 0})
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert cache.get(key) == {"step": 2}  # 主线程读取工作线程的写入
    assert errors == []


def test_many_keys_roundtrip(tmp_path):
    """多次连续创建/读取不同 key。"""
    cache = Cache(tmp_path / "many_keys.db")
    keys = [make_cache_key({**STATE, "analysis_rule": f"r{i}"}, SCHEMA, "jev-latest", "v2")
            for i in range(20)]
    for i, k in enumerate(keys):
        cache.set(k, {"i": i})
    for i, k in enumerate(keys):
        assert cache.get(k) == {"i": i}


def test_existing_db_file_still_readable(tmp_path):
    """旧格式（相同 schema）的既有缓存文件仍可正常读写——数据兼容。"""
    db = tmp_path / "legacy.db"
    key = make_cache_key(STATE, SCHEMA, "jev-latest", "v1")
    # 按旧实现的方式直接建库写数据
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS analysis_cache ("
        "key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute("INSERT INTO analysis_cache VALUES (?, ?, ?)", (key, '{"legacy": true}', 0.0))
    conn.commit()
    conn.close()

    cache = Cache(db)
    assert cache.get(key) == {"legacy": True}
    cache.set(key, {"legacy": False})
    assert cache.get(key) == {"legacy": False}


def test_clear_cross_thread(tmp_path):
    cache = Cache(tmp_path / "clear.db")
    key = make_cache_key(STATE, SCHEMA, "jev-latest", "v2")
    cache.set(key, {"x": 1})
    errors: list[Exception] = []

    def clearer():
        try:
            cache.clear()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=clearer)
    t.start()
    t.join()
    assert errors == []
    assert cache.get(key) is None


class _FakeClientA:
    """最小 FakeClient：只记录调用次数，绝不访问网络。"""

    def __init__(self):
        self.calls = 0

    def system_one(self, state, questions):
        from types import SimpleNamespace

        class A:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        self.calls += 1
        return SimpleNamespace(
            model="fake",
            answers={
                "emotion": A(choice="calm", probabilities={"calm": 1.0}, confidence=0.9),
                "intent": A(choice="other", probabilities={"other": 1.0}, confidence=0.9),
                "warmth": A(score=2.0, probabilities={}, confidence=0.9),
                "engagement": A(score=2.0, probabilities={}, confidence=0.9),
                "special_attention": A(score=1.0, probabilities={}, confidence=0.9),
                "relationship_evidence_strength": A(score=2.0, probabilities={}, confidence=0.9),
                "relational_ease": A(score=2.0, probabilities={}, confidence=0.9),
                "romantic_signal": A(noul=0.1),
                "distancing_signal": A(noul=0.1),
            },
        )


def _chat(texts_them):
    msgs = []
    for t in texts_them:
        msgs.append({"speaker": "me", "text": "开场", "time": None})
        msgs.append({"speaker": "them", "text": t, "time": None})
    return mask_messages(msgs)


def test_chat_a_then_chat_b_flow_no_thread_error(tmp_path):
    """模拟 Streamlit 连续分析：聊天 A（工作线程）→ 聊天 B（主线程）。"""
    cache = Cache(tmp_path / "flow.db")  # 主线程创建
    chat_a = _chat(["在吗", "刚下班"])
    chat_b = _chat(["晚好", "明天见"])

    errors: list[Exception] = []
    results_a = []

    def run_a():
        try:
            results_a.extend(analyze_messages(_FakeClientA(), chat_a, cache=cache))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_a)
    t.start()
    t.join()

    # 主线程继续分析聊天 B（同一 Cache 实例）
    results_b = analyze_messages(_FakeClientA(), chat_b, cache=cache)

    assert errors == []
    assert all("result" in e for e in results_a)
    assert all("result" in e for e in results_b)
    # 缓存未串聊天：A 的结果不应出现在 B 中
    assert {e["text"] for e in results_a} == {"在吗", "刚下班"}
    assert {e["text"] for e in results_b} == {"晚好", "明天见"}
    # 再跑一遍 A：应全部命中缓存，0 次 API
    client = _FakeClientA()
    analyze_messages(client, chat_a, cache=cache)
    assert client.calls == 0
