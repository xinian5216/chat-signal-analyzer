"""统一滚动锚点模块测试（nonce 单调递增 / 只由翻页触发 / 区域隔离）。"""

import scroll_anchor as sa


class _FakeStore(dict):
    """模拟 st.session_state 之上的滚动请求存储。"""


def test_request_scroll_increments_nonce(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(sa, "_scroll_requests", lambda: store, raising=False)
    sa.request_scroll("area", 1)
    sa.request_scroll("area", 2)
    sa.request_scroll("area", 3)
    assert store["area"]["nonce"] == 3
    assert store["area"]["page"] == 3


def test_consume_scroll_is_one_shot(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(sa, "_scroll_requests", lambda: store, raising=False)
    sa.request_scroll("area", 4)
    first = sa.consume_scroll("area")
    assert first == {"page": 4, "nonce": 1}
    assert sa.consume_scroll("area") is None      # 一次性：普通 rerun 不滚动


def test_areas_are_isolated(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(sa, "_scroll_requests", lambda: store, raising=False)
    sa.request_scroll("preview", 2)
    assert sa.consume_scroll("messages") is None  # 结果视图不受预览翻页影响
    assert sa.consume_scroll("preview")["page"] == 2


def test_clear_scroll_request(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(sa, "_scroll_requests", lambda: store, raising=False)
    sa.request_scroll("preview", 2)
    sa.clear_scroll_request("preview")
    assert sa.consume_scroll("preview") is None


def test_zero_nonce_never_scrolls(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(sa, "_scroll_requests", lambda: store, raising=False)
    store["area"] = {"page": 3, "nonce": 0}
    assert sa.consume_scroll("area") is None      # nonce 缺失 → 不滚动
