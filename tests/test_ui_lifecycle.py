"""Streamlit  rerun 生命周期与输入 UX 测试。

覆盖：
- 输入框只在**成功**处理（替换 / 追加）后清空；失败时保留原输入；
- ``analysis_state`` 状态机（idle / pending / running / complete / error /
  interrupted）：新 rerun 看到遗留的 running → 自动恢复为 interrupted；
- 任何状态下普通控件都不得永久 disabled（含“恢复界面状态”兜底按钮）；
- Stop / 中断不丢聊天、身份映射、结果与缓存，重新分析命中缓存不重复请求；
- 结果视图**懒渲染**：概览 rerun 不渲染“全部消息”/“报告”，切视图与分页 0 Jev API。

全部 mock，绝不调用真实 Jev API。
"""

import re

import analyzer
import pytest
import storage
from streamlit.testing.v1 import AppTest

APP_PATH = __import__("pathlib").Path(__file__).resolve().parents[1] / "app.py"

CHAT_A = """我
2026年08月21日 21:00
第一条

TA
2026年08月21日 21:05
收到，谢谢"""

CHAT_B = """TA
2026年08月21日 21:05
收到，谢谢

我
2026年08月21日 21:10
第二条"""

BAD_TEXT = "这一行完全没有任何结构"


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_response(evidence=2.4):
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
        "relationship_evidence_strength": FakeAnswer(score=evidence,
                                                     probabilities={},
                                                     confidence=0.9),
        "relational_ease": FakeAnswer(score=2.0, probabilities={},
                                      confidence=0.9),
        "romantic_signal": FakeAnswer(noul=0.2),
        "distancing_signal": FakeAnswer(noul=0.2),
    }, model="fake")


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    """AppTest 用的 FakeClient + 临时缓存（绝不触真实 API / .jev_cache）。"""
    calls = []

    class CountingClient:
        def system_one(self, state, questions):
            calls.append(state)
            return fake_response()

    class TmpCache(storage.Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

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


def _fresh(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=120)
    at.run()
    at.session_state["input_mode"] = "text"
    at.run()
    return at


def _parse(at, chat):
    at.text_area[0].set_value(chat)
    _button(at, "解析并替换当前聊天").click()
    at.run()


def _append(at, chat):
    at.text_area[0].set_value(chat)
    _button(at, "追加到当前聊天").click()
    at.run()


def _analyze(at):
    at.selectbox[0].select("我")
    at.selectbox[1].select("TA")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()          # pending → 真正执行分析的那一轮


# ---------------------------------------------------------------------------
# 输入框清空
# ---------------------------------------------------------------------------


def test_replace_success_clears_textarea(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    assert not at.exception
    assert at.session_state["messages"] is not None
    # 成功后才清空：下一次 rerun 里 text_area 已被置空
    assert at.session_state["chat_input"] == ""
    assert at.session_state["pending_clear_input"] is False
    at.run()
    assert at.text_area[0].value == ""
    assert len(counting_client) == 0


def test_replace_failure_keeps_textarea(counting_client):
    at = _fresh(counting_client)
    at.text_area[0].set_value(BAD_TEXT)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    assert at.session_state["messages"] is None
    # 失败不清空：用户原样保留可修正
    assert at.session_state["chat_input"] == BAD_TEXT
    assert at.session_state["pending_clear_input"] is False


def test_append_success_clears_textarea(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    at.text_area[0].set_value(CHAT_B)
    _button(at, "追加到当前聊天").click()
    at.run()
    assert not at.exception
    assert len(at.session_state["messages"]) == 3
    assert at.session_state["chat_input"] == ""
    at.run()
    assert at.text_area[0].value == ""


def test_append_failure_keeps_textarea(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    at.text_area[0].set_value(BAD_TEXT)
    _button(at, "追加到当前聊天").click()
    at.run()
    assert len(at.session_state["messages"]) == 2     # 原聊天不受影响
    assert at.session_state["chat_input"] == BAD_TEXT
    assert at.session_state["pending_clear_input"] is False


def test_empty_append_keeps_textarea(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    at.text_area[0].set_value("")
    _button(at, "追加到当前聊天").click()
    at.run()
    assert at.session_state["pending_clear_input"] is False
    assert len(at.session_state["messages"]) == 2


# ---------------------------------------------------------------------------
# analysis_state 状态机
# ---------------------------------------------------------------------------


def test_state_complete_after_success(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    assert at.session_state["analysis_state"] == "complete"
    assert len(counting_client) == 1
    assert not at.exception


def test_stale_running_is_recovered_as_interrupted(counting_client):
    """新 rerun 已开始却仍看到 running → 上一轮必被 Stop / 中断 / 热重载。"""
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    at.session_state["analysis_state"] = "running"      # 模拟上一轮被打断
    at.run()
    assert at.session_state["analysis_state"] == "interrupted"
    # 结果仍然在，且提示可以重新开始
    assert at.session_state["results"]
    assert "被中断" in _texts(at)


def test_controls_enabled_in_every_recovered_state(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    for state in ("complete", "error", "interrupted", "idle"):
        at.session_state["analysis_state"] = state
        at.run()
        assert not at.exception
        labels = [b.label for b in at.button]
        assert "开始 Jev 分析" in labels
        for b in at.button:
            assert not b.disabled, f"{b.label} disabled in state={state}"


def test_state_error_when_analysis_raises(counting_client, monkeypatch):
    """分析抛异常：状态机必须落到 error，而不是永久 running。"""

    def boom(*a, **k):
        raise RuntimeError("boom")

    # 必须在 AppTest 导入 app.py 之前 patch（app 用 from-import 绑定函数）
    monkeypatch.setattr(analyzer, "analyze_messages", boom)
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    assert at.session_state["analysis_state"] == "error"
    # 下一轮 UI 恢复正常（不再有 running / 兜底按钮）
    at.run()
    assert at.session_state["analysis_state"] == "error"
    labels = [b.label for b in at.button]
    assert "恢复界面状态" not in labels
    assert not any(b.disabled for b in at.button)


def test_reset_ui_state_button_only_when_stuck(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    labels = [b.label for b in at.button]
    assert "恢复界面状态" not in labels          # 正常完成时不需要兜底

    at.session_state["analysis_state"] = "running"
    at.run()
    assert at.session_state["analysis_state"] == "interrupted"
    _button(at, "恢复界面状态").click()
    at.run()
    assert at.session_state["analysis_state"] == "idle"
    assert "恢复界面状态" not in [b.label for b in at.button]


def test_reset_ui_state_keeps_everything(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    keep = {k: at.session_state[k] for k in
            ("messages", "results", "stats", "applied_me", "applied_ta",
             "raw_chunks")}

    at.session_state["analysis_state"] = "interrupted"
    at.run()
    _button(at, "恢复界面状态").click()
    at.run()

    for key, value in keep.items():
        assert at.session_state[key] == value, key
    assert at.session_state["analysis_state"] == "idle"
    # 缓存没有被清空：仍然命中，不重复请求
    n = len(counting_client)
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()
    assert len(counting_client) == n


def test_reanalysis_after_interrupt_hits_cache(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    n = len(counting_client)
    at.session_state["analysis_state"] = "running"     # 模拟被 Stop
    at.run()
    assert at.session_state["analysis_state"] == "interrupted"
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()
    assert at.session_state["analysis_state"] == "complete"
    assert len(counting_client) == n                   # 全部缓存命中，0 新请求


# ---------------------------------------------------------------------------
# 结果视图懒渲染
# ---------------------------------------------------------------------------


def test_overview_render_does_not_render_hidden_views(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    at.run()
    body = _texts(at)
    assert "互动亲近信号指数" in body
    # 隐藏视图的内容一次都没有渲染（用只在那些视图里出现的文案判断）
    assert "#### 报告导出" not in body
    assert "下载 Markdown" not in body
    assert "每页" not in body               # 全部消息视图的分页说明
    # 报告 memo 也为空：只有进入报告视图才会生成
    assert at.session_state["report_cache"] is None


def test_report_built_only_when_report_view_open(counting_client):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    at.run()
    assert at.session_state["report_cache"] is None
    at.segmented_control[0].set_value("报告")
    at.run()
    assert at.session_state["report_cache"] is not None
    body = _texts(at)
    assert "报告导出" in body
    # memo：同一结果重复进入不重复构建（key 稳定）
    key = at.session_state["report_cache"]["key"]
    at.segmented_control[0].set_value("概览")
    at.run()
    at.segmented_control[0].set_value("报告")
    at.run()
    assert at.session_state["report_cache"]["key"] == key
    assert len(counting_client) == 1


@pytest.mark.parametrize("view", ["关键消息", "全部消息", "报告", "概览"])
def test_switching_views_is_zero_api(counting_client, view):
    at = _fresh(counting_client)
    _parse(at, CHAT_A)
    _analyze(at)
    n = len(counting_client)
    at.segmented_control[0].set_value(view)
    at.run()
    assert len(counting_client) == n
    assert not at.exception


def test_all_messages_is_paginated(counting_client):
    at = _fresh(counting_client)
    # 构造 > 25 条可分析消息
    chat = "\n\n".join(
        f"我\n2026年08月21日 {9 + i % 8:02d}:{i % 60:02d}\n问 {i}\n\n"
        f"TA\n2026年08月21日 {9 + i % 8:02d}:{i % 60:02d}\n答 {i}"
        for i in range(30)
    )
    _parse(at, chat)
    _analyze(at)
    at.segmented_control[0].set_value("全部消息")
    at.run()
    body = _texts(at)
    assert "第 1 /" in body and "每页 25 条" in body
    n = len(counting_client)

    _button(at, "下一页 ▶").click()
    at.run()
    body = _texts(at)
    assert "第 2 /" in body
    assert len(counting_client) == n              # 切页 0 API

    # 到末页后“下一页”必须禁用（不越界，也不得请求 API）
    while not _button(at, "下一页 ▶").disabled:
        _button(at, "下一页 ▶").click()
        at.run()
    assert _button(at, "下一页 ▶").disabled
    body = _texts(at)
    m = re.search(r"第 (\d+) / (\d+) 页", body)
    assert m and m.group(1) == m.group(2), body[-200:]   # 已在末页
    assert len(counting_client) == n
    assert not at.exception


def test_clipboard_probe_not_loaded_by_default(counting_client):
    """Probe 默认不创建组件：不影响主流程任何一次 rerun。"""
    at = _fresh(counting_client)
    assert at.session_state["probe_open"] is False
    body = _texts(at)
    assert "尚未粘贴" not in body            # 组件未渲染
    # 显式打开后才出现内容
    _button(at, "打开 Clipboard Probe").click()
    at.run()
    assert at.session_state["probe_open"] is True
    assert "尚未粘贴" in _texts(at)
    _button(at, "收起 Clipboard Probe").click()
    at.run()
    assert at.session_state["probe_open"] is False
