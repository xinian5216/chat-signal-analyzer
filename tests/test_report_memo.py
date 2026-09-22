"""报告 memo 身份回归测试（report memo identity）。

旧 memo key 用统计量（条数 / analyzed / skipped_media / failed / overall）
拼接，两份不同聊天只要这些数字相同就会错误复用上一份 Markdown / JSON 报告。

现在改用 ``analysis_revision``（每次产生 / 替换 / 清除一整份分析结果时
递增）+ 是否包含原文。这里用 A / B 两份**结构完全相同、统计完全相同、
正文完全不同**的分析结果来验证。

全部 mock，绝不调用真实 Jev API。
"""

import pytest
from streamlit.testing.v1 import AppTest

import storage
from app import report_memo_key   # 纯函数，不触碰 Streamlit 状态

APP_PATH = __import__("pathlib").Path(__file__).resolve().parents[1] / "app.py"

# A / B：同样的消息条数、同样的 TA 文本条数（→ analyzed / overall / failed /
# skipped_media 全部相同），只有正文不同。
CHAT_A = """我
2026年08月21日 21:00
阿尔法第一条

TA
2026年08月21日 21:05
收到，谢谢"""  # noqa

CHAT_B = """我
2026年08月21日 21:00
贝塔第二条

TA
2026年08月21日 21:05
好的，明白"""  # noqa


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_response():
    """固定响应 → 两份聊天的 overall / analyzed / failed 必然完全相同。"""
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
        "relationship_evidence_strength": FakeAnswer(score=2.4, probabilities={},
                                                     confidence=0.9),
        "relational_ease": FakeAnswer(score=2.0, probabilities={},
                                      confidence=0.9),
        "romantic_signal": FakeAnswer(noul=0.2),
        "distancing_signal": FakeAnswer(noul=0.2),
    }, model="fake")


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    calls = []

    class CountingClient:
        def system_one(self, state, questions):
            calls.append(state)
            return fake_response()

    class TmpCache(storage.Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    import analyzer
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(analyzer, "create_client", lambda api_key: CountingClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)
    return calls


def _button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"button {label!r} not found; have {[b.label for b in at.button]}")


def _texts(at) -> str:
    chunks = []
    for attr in ("markdown", "success", "info", "warning", "error", "caption",
                 "metric", "subheader", "title", "text", "dataframe", "badge"):
        for e in getattr(at, attr, []) or []:
            try:
                chunks.append(str(e.value))
            except Exception:
                pass
    return "\n".join(chunks)


def _fresh():
    at = AppTest.from_file(str(APP_PATH), default_timeout=120)
    at.run()
    at.session_state["input_mode"] = "text"
    at.run()
    return at


def _run_chat(at, chat):
    at.text_area[0].set_value(chat)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    at.selectbox[0].select("我")
    at.selectbox[1].select("TA")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()


def _open_report(at):
    at.segmented_control[0].set_value("报告")
    at.run()
    at.checkbox[0].check()      # 报告中包含本地脱敏后的聊天文本
    at.run()


def _stats_of(at):
    """当前结果的统计指纹（用于证明两份结果统计完全相同）。"""
    stats = at.session_state["stats"]
    return (len(at.session_state["results"]), stats["analyzed"],
            stats["failed"], stats["overall"], stats["total_weight"],
            stats["effective_messages"])


# ---------------------------------------------------------------------------
# 纯函数：memo key 的身份
# ---------------------------------------------------------------------------


def test_memo_key_uses_revision_not_stats():
    assert report_memo_key(3, True) == (3, True)
    assert report_memo_key(3, False) == (3, False)
    # 相同 revision + 相同 include_text → 命中
    assert report_memo_key(7, False) == report_memo_key(7, False)
    # revision 不同 → 一定不命中（与统计量无关）
    assert report_memo_key(7, False) != report_memo_key(8, False)
    # include_text 不同 → 不命中
    assert report_memo_key(7, True) != report_memo_key(7, False)


def test_memo_key_ignores_result_contents():
    """两份统计完全不同的结果，只要 revision 相同就复用同一 key。"""
    assert report_memo_key(1, True) == report_memo_key(1, True)


# ---------------------------------------------------------------------------
# 端到端：A / B 统计相同但报告不得互相复用
# ---------------------------------------------------------------------------


def test_identical_stats_do_not_share_report(counting_client):
    at = _fresh()

    # ---- A ----
    _run_chat(at, CHAT_A)
    stats_a = _stats_of(at)
    _open_report(at)
    body_a = _texts(at)
    assert "收到，谢谢" in at.session_state["report_cache"]["json"]
    assert "好的，明白" not in at.session_state["report_cache"]["json"]
    memo_a = at.session_state["report_cache"]
    assert memo_a is not None
    n_api = len(counting_client)

    # ---- B：结构相同 → 统计必须完全相同 ----
    _run_chat(at, CHAT_B)
    stats_b = _stats_of(at)
    assert stats_b == stats_a, (stats_a, stats_b)

    _open_report(at)
    # B 不得复用 A 的报告（断言在 JSON 输出上，它含每条 TA 消息原文）
    assert "好的，明白" in at.session_state["report_cache"]["json"]
    assert "收到，谢谢" not in at.session_state["report_cache"]["json"]
    # memo 确实被重新生成（revision 变了）
    assert at.session_state["report_cache"]["key"] != memo_a["key"]
    assert at.session_state["report_cache"]["md"] != memo_a["md"]
    assert "好的，明白" in at.session_state["report_cache"]["json"]
    assert "收到，谢谢" not in at.session_state["report_cache"]["json"]
    # 切换 / 重新生成报告都不请求 API
    assert len(counting_client) == n_api + 1     # 只多了 B 的 1 条分析


def test_same_result_reentering_report_hits_memo(counting_client):
    at = _fresh()
    _run_chat(at, CHAT_A)
    at.segmented_control[0].set_value("报告")
    at.run()
    memo = at.session_state["report_cache"]
    assert memo is not None
    assert memo["key"] == (at.session_state["analysis_revision"], False)
    n_api = len(counting_client)

    # 离开再进入：同一份结果 → 命中 memo（key 不变、内容不变、0 API）
    for _ in range(3):
        at.segmented_control[0].set_value("概览")
        at.run()
        at.segmented_control[0].set_value("报告")
        at.run()
        assert at.session_state["report_cache"]["key"] == memo["key"]
        assert at.session_state["report_cache"]["md"] == memo["md"]
    assert len(counting_client) == n_api


def test_include_text_toggle_rebuilds_report(counting_client):
    at = _fresh()
    _run_chat(at, CHAT_A)
    at.segmented_control[0].set_value("报告")
    at.run()
    assert at.session_state["report_cache"]["key"] == (
        at.session_state["analysis_revision"], False
    )
    anon = at.session_state["report_cache"]["md"]
    assert "阿尔法第一条" not in anon           # 默认匿名报告不含原文

    at.checkbox[0].check()
    at.run()
    assert at.session_state["report_cache"]["key"] == (
        at.session_state["analysis_revision"], True
    )
    assert at.session_state["report_cache"]["md"] != anon
    # JSON 报呋含毋条结果的原文
    assert "收到，谢谢" in at.session_state["report_cache"]["json"]
    anon_json = at.session_state["report_cache"]["json"]

    at.checkbox[0].uncheck()
    at.run()
    assert at.session_state["report_cache"]["key"] == (
        at.session_state["analysis_revision"], False
    )
    assert at.session_state["report_cache"]["json"] != anon_json
    assert "收到，谢谢" not in at.session_state["report_cache"]["json"]


def test_new_chat_with_identical_stats_regenerates_report(counting_client):
    """即使 analyzed/overall/条数全部一样，新聊天也必须重新生成报告。"""
    at = _fresh()
    _run_chat(at, CHAT_A)
    before_rev = at.session_state["analysis_revision"]
    _open_report(at)
    rev_a = at.session_state["analysis_revision"]

    _run_chat(at, CHAT_B)
    assert at.session_state["analysis_revision"] > before_rev
    rev_before_view = at.session_state["analysis_revision"]
    _open_report(at)
    assert (at.session_state["analysis_revision"]
            == rev_before_view)          # 看报告不改变 revision
    assert rev_before_view > rev_a
    assert "好的，明白" in at.session_state["report_cache"]["json"]
    assert "收到，谢谢" not in at.session_state["report_cache"]["json"]
