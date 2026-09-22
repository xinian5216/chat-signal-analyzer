"""Jev 分析并发 / 上下文回归测试。

覆盖：
- 有限并发（bounded concurrency）：最大 in-flight 不超过配置；
- cache 命中不进入并发 API 队列（126 target / 42 hit → 只提交 84）；
- 并发完成顺序不影响最终 results 顺序；
- 单条异常隔离（不影响其它消息）；
- worker-local client 会在结束后关闭；
- **上下文不被并发破坏**：TA target 的 conversation_context 仍是双方混合
  前序消息，我的消息 0 次独立调用，cache key 语义不变；
- 并发 cache 写入安全（短连接 + WAL）；
- mock benchmark：100ms 延迟、20 个 miss，串行 ~2s，并发 4 明显更快。

全部 mock，绝不调用真实 Jev API。
"""

import threading
import time

import pytest

import analyzer
import storage
from analyzer import (
    JEV_CONCURRENCY_LIMIT,
    analyze_messages,
    build_questions_schema,
    effective_workers,
)
from parser import parse_chat
from privacy import mask_messages
from storage import Cache, make_cache_key


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _response():
    from types import SimpleNamespace as NS
    return NS(answers={
        "emotion": FakeAnswer(choice="calm", probabilities={"calm": 1.0},
                              confidence=0.9),
        "intent": FakeAnswer(choice="other", probabilities={"other": 1.0},
                             confidence=0.9),
        "warmth": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
        "engagement": FakeAnswer(score=2.0, probabilities={}, confidence=0.9),
        "special_attention": FakeAnswer(score=1.0, probabilities={},
                                        confidence=0.9),
        "relationship_evidence_strength": FakeAnswer(score=2.0, probabilities={},
                                                     confidence=0.9),
        "relational_ease": FakeAnswer(score=2.0, probabilities={},
                                      confidence=0.9),
        "romantic_signal": FakeAnswer(noul=0.2),
        "distancing_signal": FakeAnswer(noul=0.2),
    }, model="fake")


class LatencyClient:
    """带延迟 / in-flight 统计 / 可注入失败的 FakeClient。"""

    def __init__(self, latency=0.0, fail_on=None):
        self.latency = latency
        self.fail_on = fail_on or set()
        self.calls = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = 0
        self.states = []

    def system_one(self, state, questions):
        with self.lock:
            self.calls.append(state["target_message"]["text"])
            self.states.append(state)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency:
                time.sleep(self.latency)
            text = state["target_message"]["text"]
            if text in self.fail_on:
                raise RuntimeError("boom")
            return _response()
        finally:
            with self.lock:
                self.in_flight -= 1

    def close(self):
        self.closed += 1


class RecordingClientFactory:
    """为每个 worker 线程创建独立 client，并记录创建/关闭次数。"""

    def __init__(self, latency=0.0, fail_on=None):
        self.latency = latency
        self.fail_on = fail_on
        self.lock = threading.Lock()
        self.created: list[LatencyClient] = []

    def __call__(self):
        client = LatencyClient(latency=self.latency, fail_on=self.fail_on)
        with self.lock:
            self.created.append(client)
        return client

    @property
    def calls(self):
        out = []
        for c in self.created:
            out.extend(c.calls)
        return out

    @property
    def max_in_flight(self):
        return max((c.max_in_flight for c in self.created), default=0)


def _chat(n_pairs=8):
    lines = []
    for i in range(n_pairs):
        lines.append(f"我")
        lines.append(f"2026年08月21日 1{i % 9}:00")
        lines.append(f"我问 {i}")
        lines.append("")
        lines.append("TA")
        lines.append(f"2026年08月21日 1{i % 9}:01")
        lines.append(f"TA 回 {i}")
        lines.append("")
    return "\n".join(lines)


def _messages(n_pairs=8):
    return mask_messages(parse_chat(_chat(n_pairs), my_name="我", them_name="TA"))


# ---------------------------------------------------------------------------
# effective_workers
# ---------------------------------------------------------------------------


def test_effective_workers_defaults_and_clamp(monkeypatch):
    assert effective_workers(None) == 4
    monkeypatch.setenv("SIGNALLENS_JEV_CONCURRENCY", "1")
    assert effective_workers(None) == 1
    monkeypatch.setenv("SIGNALLENS_JEV_CONCURRENCY", "8")
    assert effective_workers(None) == 8
    monkeypatch.setenv("SIGNALLENS_JEV_CONCURRENCY", "99")
    assert effective_workers(None) == JEV_CONCURRENCY_LIMIT
    monkeypatch.setenv("SIGNALLENS_JEV_CONCURRENCY", "abc")
    assert effective_workers(None) == 4
    # 显式参数优先
    monkeypatch.setenv("SIGNALLENS_JEV_CONCURRENCY", "1")
    assert effective_workers(4) == 4
    assert effective_workers(0) == 1


# ---------------------------------------------------------------------------
# 并发行为
# ---------------------------------------------------------------------------


def test_serial_when_workers_is_one():
    client = LatencyClient(latency=0.02)
    results = analyze_messages(client, _messages(6), max_workers=1)
    assert len(results) == 6
    assert client.max_in_flight == 1
    assert len(client.calls) == 6


def test_bounded_concurrency_never_exceeds_workers():
    client = LatencyClient(latency=0.05)
    results = analyze_messages(client, _messages(12), max_workers=4)
    assert len(results) == 12
    assert client.max_in_flight <= 4
    assert client.max_in_flight > 1          # 并发确实 > 1
    assert analyzer.LAST_RUN_STATS["workers"] == 4
    assert analyzer.LAST_RUN_STATS["api_calls"] == 12


def test_concurrent_results_keep_original_order():
    client = LatencyClient(latency=0.03)
    messages = _messages(12)
    results = analyze_messages(client, messages, max_workers=4)
    # 顺序必须与原 TA targets 顺序一致（按 index 升序、文本按序）
    assert [e["index"] for e in results] == sorted(e["index"] for e in results)
    expected_texts = [m["text"] for m in messages
                      if m["speaker"] == "them" and m.get("content_type") != "media"]
    assert [e["text"] for e in results] == expected_texts


def test_cache_hits_never_enter_the_worker_pool(tmp_path):
    """126 target / 42 hit → 真正提交 API 的只有 84（这里用 8 / 4 验证）。"""

    class TmpCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    messages = _messages(8)
    cache = TmpCache()

    # 第一轮：全部 miss → 8 次请求
    client1 = LatencyClient(latency=0.01)
    first = analyze_messages(client1, messages, cache=cache, max_workers=1)
    assert len(first) == 8
    assert len(client1.calls) == 8

    # 第二轮：全部命中 → 0 次请求
    client2 = LatencyClient(latency=0.01)
    second = analyze_messages(client2, messages, cache=cache, max_workers=4)
    assert len(client2.calls) == 0
    assert len(second) == 8
    assert all(e["cached"] for e in second)
    assert analyzer.LAST_RUN_STATS["cache_hits"] == 8
    assert analyzer.LAST_RUN_STATS["api_calls"] == 0

    # 只缓存一半：只把未命中的 4 条送进并发队列
    class HalfCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "half.db")

    half_cache = HalfCache()
    client3 = LatencyClient(latency=0.01)
    ta_indices = [i for i, m in enumerate(messages) if m["speaker"] == "them"]
    half_indices = set(ta_indices[:4])
    analyze_messages(client3, messages, cache=half_cache, max_workers=1,
                     only_indices=half_indices)
    assert len(client3.calls) == 4

    client4 = LatencyClient(latency=0.01)
    full = analyze_messages(client4, messages, cache=half_cache, max_workers=4)
    assert len(full) == 8
    assert len(client4.calls) == 4                 # 只请求未命中的
    assert analyzer.LAST_RUN_STATS["cache_hits"] == 4
    assert analyzer.LAST_RUN_STATS["api_calls"] == 4


def test_exception_isolation_under_concurrency():
    client = LatencyClient(latency=0.02, fail_on={"TA 回 3", "TA 回 5"})
    results = analyze_messages(client, _messages(8), max_workers=4)
    assert len(results) == 8
    errored = {e["text"] for e in results if e.get("error")}
    assert errored == {"TA 回 3", "TA 回 5"}
    ok = [e for e in results if "result" in e]
    assert len(ok) == 6
    stats = analyzer.LAST_RUN_STATS
    assert stats["failed"] == 2
    assert stats["rate_limit"] == 0


def test_worker_local_clients_are_closed():
    factory = RecordingClientFactory()
    messages = _messages(8)
    results = analyze_messages(
        LatencyClient(), messages, max_workers=3, client_factory=factory
    )
    assert len(results) == 8
    assert len(factory.created) >= 1
    assert all(c.closed >= 1 for c in factory.created)
    assert len(factory.calls) == 8


def test_progress_callback_reports_misses_and_workers():
    seen = []
    client = LatencyClient(latency=0.01)
    analyze_messages(client, _messages(6), max_workers=3,
                     progress_cb=lambda done, total, info=None: seen.append(
                         (done, total, dict(info or {}))))
    assert seen, "progress callback never fired"
    assert all(total == 6 for _, total, _ in seen)
    infos = [info for _, _, info in seen]
    assert any(i.get("misses") == 6 for i in infos)
    assert any(i.get("workers") == 3 for i in infos)
    assert seen[-1][0] == 6


# ---------------------------------------------------------------------------
# 上下文 / 语义不被并发破坏（本轮新增的关键回归）
# ---------------------------------------------------------------------------

CONTEXT_CHAT = """我
2026年08月21日 21:00
你最近是不是挺忙的

TA
2026年08月21日 21:05
有一点

我
2026年08月21日 21:06
感觉你好几天都没怎么上线

TA
2026年08月21日 21:20
最近事情确实挺多的"""


def test_context_is_mixed_previous_messages():
    client = LatencyClient()
    messages = mask_messages(parse_chat(CONTEXT_CHAT, my_name="我", them_name="TA"))
    results = analyze_messages(client, messages, max_workers=2)

    assert len(results) == 2
    # 我的消息 0 次独立 system_one
    assert client.calls == ["有一点", "最近事情确实挺多的"]

    last = results[-1]
    ctx_texts = [c["text"] for c in last["context"]]
    assert ctx_texts == ["你最近是不是挺忙的", "有一点", "感觉你好几天都没怎么上线"]
    assert [c["speaker"] for c in last["context"]] == ["me", "them", "me"]
    # 状态里的上下文同样按原顺序包含双方消息
    state_ctx = [c["text"] for c in client.states[-1]["conversation_context"]]
    assert state_ctx == ctx_texts


def test_target_state_fields_unchanged_by_concurrency():
    client = LatencyClient()
    serial = analyze_messages(LatencyClient(), _messages(6), max_workers=1)
    parallel = analyze_messages(LatencyClient(), _messages(6), max_workers=4)
    for a, b in zip(serial, parallel):
        assert set(a) == set(b)
        assert a["index"] == b["index"] and a["text"] == b["text"]
        assert (a.get("result") is None) == (b.get("result") is None)


def test_cache_key_semantics_unchanged_by_concurrency(tmp_path):
    """并发前后 state / cache key 必须完全一致（不破坏已有缓存）。"""
    class TmpCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    messages = _messages(6)
    serial = analyze_messages(LatencyClient(), messages, cache=TmpCache(),
                              max_workers=1)
    parallel = analyze_messages(LatencyClient(), messages, cache=TmpCache(),
                                max_workers=4)
    assert [e["cached"] for e in parallel] == [True] * 6

    schema = build_questions_schema()
    for entry in parallel:
        target = {"speaker": "them", "text": entry["text"],
                  "time": entry.get("time"), "raw_speaker": "TA"}
        state = analyzer.build_state(entry["context"], target)
        key = make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                             analyzer.SCHEMA_VERSION)
        assert TmpCache().get(key) is not None


def test_debug_target_view_helper():
    """测试辅助：能查看某个 target 的 target_text / conversation_context。"""
    client = LatencyClient()
    messages = mask_messages(parse_chat(CONTEXT_CHAT, my_name="我", them_name="TA"))
    results = analyze_messages(client, messages, max_workers=2)
    view = analyzer.debug_target_view(results[-1])
    assert view["target_text"] == "最近事情确实挺多的"
    assert view["conversation_context"] == [
        "你最近是不是挺忙的", "有一点", "感觉你好几天都没怎么上线"]


def test_real_sdk_client_without_factory_runs_serially():
    """安全网：没有 client_factory 时，不把真实 SDK client 给多个线程共享。"""
    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError:                      # pragma: no cover - SDK 未安装
        pytest.skip("typesafe_sdk 未安装")

    class _StubSDK(TypeSafeClient):
        """只覆盖 __init__ / system_one，不建真实连接。"""

        def __init__(self):                  # noqa: D107 - 跳过真实初始化
            self.calls = []
            self.max_in_flight = 0
            self.in_flight = 0
            self._lock = threading.Lock()

        def system_one(self, state, questions):
            with self._lock:
                self.calls.append(state)
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                return _response()
            finally:
                with self._lock:
                    self.in_flight -= 1

    client = _StubSDK()
    results = analyze_messages(client, _messages(6), max_workers=4)
    assert len(results) == 6
    # 未提供 factory → 自动降级为串行（不共享 transport）
    assert client.max_in_flight == 1
    assert analyzer.LAST_RUN_STATS["shared_client"] is True
    assert analyzer.LAST_RUN_STATS["workers"] == 1


# ---------------------------------------------------------------------------
# 并发 cache 写入
# ---------------------------------------------------------------------------


def test_concurrent_cache_writes_are_safe(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    schema = build_questions_schema()
    errors = []

    def writer(n):
        try:
            for i in range(20):
                state = {"conversation_context": [],
                         "target_message": {"speaker": "them",
                                            "text": f"t{n}-{i}", "time": None,
                                            "raw_speaker": "TA"}}
                cache.set(make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                                         analyzer.SCHEMA_VERSION),
                          {"model": "fake"})
        except Exception as exc:      # pragma: no cover - 只用于失败报告
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    state = {"conversation_context": [],
             "target_message": {"speaker": "them", "text": "t0-19", "time": None,
                                "raw_speaker": "TA"}}
    assert cache.get(make_cache_key(state, schema, analyzer.DEFAULT_MODEL,
                                    analyzer.SCHEMA_VERSION)) == {"model": "fake"}


# ---------------------------------------------------------------------------
# mock benchmark
# ---------------------------------------------------------------------------


def _benchmark(workers, n=20, latency=0.1):
    client = LatencyClient(latency=latency)
    start = time.perf_counter()
    results = analyze_messages(client, _messages(n), max_workers=workers)
    return time.perf_counter() - start, results, client


def test_mock_benchmark_serial_vs_concurrent():
    serial_wall, serial_results, serial_client = _benchmark(1)
    assert len(serial_results) == 20
    assert serial_client.max_in_flight == 1
    # 串行理论约 2s（CI 上给足余量，只验证量级）
    assert serial_wall > 1.0

    par_wall, par_results, par_client = _benchmark(4)
    assert par_client.max_in_flight <= 4
    assert par_client.max_in_flight > 1
    # 并发 4 应明显快于串行（理论 ~0.5s；CI 抖动下只要求显著下降）
    assert par_wall < serial_wall * 0.7, (serial_wall, par_wall)
    assert [e["text"] for e in par_results] == [e["text"] for e in serial_results]

    par8_wall, _, par8_client = _benchmark(8)
    assert par8_client.max_in_flight <= 8
    assert par8_wall <= par_wall * 1.5     # 8 并发不应明显变慢
