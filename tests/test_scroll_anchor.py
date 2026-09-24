"""滚动锚点模块测试。

覆盖：
- nonce 与待执行请求分开保存：request → consume → request → consume 时
  nonce 持续递增（Streamlit 去重相同 HTML，nonce 必须单调）；
- 只由翻页触发：consume 是一次性的，普通 rerun 不消费；
- 区域隔离：预览与结果视图互不抢占；
- **会话隔离**：正常 Streamlit 运行时状态存 st.session_state，
  模块级兜底存储不被使用（不同浏览器会话不共享滚动状态）；
- nonce 计数器不被 consume / clear 重置。
"""

import scroll_anchor as sa


class _FakeSessionState(dict):
    """模拟 st.session_state（dict 即可赋值/读取）。"""


def _process_store():
    sa._PROCESS_PENDING.clear()
    sa._PROCESS_NONCE.clear()
    return sa._PROCESS_PENDING, sa._PROCESS_NONCE


def _use_process(monkeypatch):
    monkeypatch.setattr(sa, "_use_process_store", lambda: True)
    return _process_store()


def _use_session(monkeypatch):
    """模拟正常 Streamlit：走 session_state 分支，进程级兜底必须不被使用。"""
    session = _FakeSessionState()
    monkeypatch.setattr(sa, "_use_process_store", lambda: False)
    monkeypatch.setattr(sa.st, "session_state", session, raising=False)
    return session


# ---------------------------------------------------------------------------
# nonce 与待执行请求分开：request → consume → request → consume
# ---------------------------------------------------------------------------


def test_request_consume_request_consume_keeps_incrementing_nonce(monkeypatch):
    _use_process(monkeypatch)
    sa.request_scroll("area", 1, "bottom")
    first = sa.consume_scroll("area")
    assert first == {"page": 1, "nonce": 1, "position": "bottom"}

    sa.request_scroll("area", 2, "top")
    second = sa.consume_scroll("area")
    assert second == {"page": 2, "nonce": 2, "position": "top"}

    sa.request_scroll("area", 3)
    third = sa.consume_scroll("area")
    assert third == {"page": 3, "nonce": 3, "position": "auto"}

    # nonce 单调递增 → 连续翻页的 HTML 永不重复（Streamlit 不去重）
    assert [first["nonce"], second["nonce"], third["nonce"]] == [1, 2, 3]


def test_consume_does_not_reset_nonce(monkeypatch):
    _use_process(monkeypatch)
    for i in range(5):
        sa.request_scroll("area", i + 1)
        assert sa.consume_scroll("area")["nonce"] == i + 1
        # consume 之后 nonce 不回退
        assert sa.current_nonce("area") == i + 1


def test_clear_keeps_nonce_counter(monkeypatch):
    _use_process(monkeypatch)
    sa.request_scroll("area", 7)
    sa.clear_scroll_request("area")
    assert sa.consume_scroll("area") is None      # 待执行请求已清
    assert sa.current_nonce("area") == 1          # 计数器保留

    sa.request_scroll("area", 8)
    entry = sa.consume_scroll("area")
    assert entry["nonce"] == 2                    # 从 1 继续，不回退


def test_multi_turn_html_unique(monkeypatch):
    """连续 4 次翻页渲染的 HTML 必须互不相同（nonce 进入 HTML）。"""
    _use_process(monkeypatch)
    nonces = []
    for page in (2, 3, 4, 5):
        sa.request_scroll("area", page)
        entry = sa.consume_scroll("area")
        nonces.append(entry["nonce"])
    assert len(set(nonces)) == 4


# ---------------------------------------------------------------------------
# 只由翻页触发 / 区域隔离
# ---------------------------------------------------------------------------


def test_consume_is_one_shot(monkeypatch):
    _use_process(monkeypatch)
    sa.request_scroll("area", 4)
    assert sa.consume_scroll("area") == {"page": 4, "nonce": 1,
                                     "position": "auto"}
    assert sa.consume_scroll("area") is None      # 普通 rerun 不会二次滚动


def test_areas_are_isolated(monkeypatch):
    _use_process(monkeypatch)
    sa.request_scroll("preview", 2)
    sa.request_scroll("messages", 3)
    assert sa.consume_scroll("messages")["page"] == 3
    assert sa.consume_scroll("preview")["page"] == 2


def test_zero_nonce_never_scrolls(monkeypatch):
    _use_process(monkeypatch)
    sa._PROCESS_PENDING["area"] = {"page": 3, "nonce": 0}
    assert sa.consume_scroll("area") is None


# ---------------------------------------------------------------------------
# 会话隔离（session_state，非进程级共享）
# ---------------------------------------------------------------------------


def test_normal_streamlit_uses_session_state(monkeypatch):
    session = _use_session(monkeypatch)
    sa.request_scroll("preview", 2)
    # 状态进入 session_state（每个浏览器会话独立）
    assert sa._SESSION_PENDING_KEY in session
    assert sa._SESSION_NONCE_KEY in session
    assert session[sa._SESSION_PENDING_KEY]["preview"]["page"] == 2
    # 模块级兜底存储未被使用
    assert sa._PROCESS_PENDING == {} and sa._PROCESS_NONCE == {}


def test_two_sessions_do_not_share_scroll_state(monkeypatch):
    """两个浏览器会话：各自的 request/consume/nonce 互不影响。"""
    monkeypatch.setattr(sa, "_use_process_store", lambda: False)
    session_a = _FakeSessionState()
    session_b = _FakeSessionState()

    # ---- 会话 A：连续翻两页 ----
    monkeypatch.setattr(sa.st, "session_state", session_a, raising=False)
    sa.request_scroll("preview", 1)
    sa.request_scroll("preview", 2)
    assert session_a[sa._SESSION_NONCE_KEY]["preview"] == 2

    # ---- 会话 B：全新 session_state，看不到 A 的任何状态 ----
    monkeypatch.setattr(sa.st, "session_state", session_b, raising=False)
    assert sa.consume_scroll("preview") is None      # B 没有 A 的待执行请求
    assert sa.current_nonce("preview") == 0          # B 的 nonce 独立

    # B 自己翻页，nonce 从 1 开始
    sa.request_scroll("preview", 9)
    assert sa.consume_scroll("preview") == {"page": 9, "nonce": 1,
                                     "position": "auto"}

    # ---- 切回会话 A：nonce 仍是 2（B 的操作不影响 A） ----
    monkeypatch.setattr(sa.st, "session_state", session_a, raising=False)
    assert sa.current_nonce("preview") == 2
    sa.request_scroll("preview", 3)
    assert sa.consume_scroll("preview") == {"page": 3, "nonce": 3,
                                     "position": "auto"}
    # 进程级兜底仍保持干净（没有跨会话泄漏）
    assert sa._PROCESS_PENDING == {} and sa._PROCESS_NONCE == {}


def test_session_nonce_survives_within_session(monkeypatch):
    session = _use_session(monkeypatch)
    for page in (2, 3, 4):
        sa.request_scroll("messages", page)
        entry = sa.consume_scroll("messages")
        assert entry["nonce"] == page - 1            # 同一会话内单调递增
    assert session[sa._SESSION_NONCE_KEY]["messages"] == 3
