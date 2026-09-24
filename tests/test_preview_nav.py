"""导入预览区分页导航测试（上/下统一渲染函数）。

覆盖：
- 顶部与底部导航由**同一个渲染函数**产出：左右对称、页码居中、日期范围
  单独一行，页码状态共享（点顶部翻页，底部指示同步变化）；
- 首页禁用“上一页”、末页禁用“下一页”（两处都正确）；
- 只有页码**真正变化**时才登记滚动请求 / rerun（nonce 递增一次），
  页码没变时什么都不做；
- 翻页仍然只动本地展示状态：不请求 Jev、不动消息顺序/全局 index。

全部 mock，绝不调用真实 Jev API。
"""

from pathlib import Path

import pytest
import scroll_anchor as sa
import storage
import streamlit as st
from streamlit.testing.v1 import AppTest

import app
import analyzer

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

PREV = "◀ 上一页"
NEXT = "下一页 ▶"


def _chat(n=95):
    """虚构微信三行块聊天：n 条（默认 95 → 每页 40 条共 3 页）。"""
    return "\n\n".join(
        f"{'我' if i % 2 == 0 else 'TA'}\n"
        f"2026年09月{1 + i // 60:02d}日 09:{i % 60:02d}\n"
        f"消息 {i}"
        for i in range(n)
    )


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    class CountingClient:
        def system_one(self, state, questions):
            raise AssertionError("预览分页不得调用 Jev API")

    class TmpCache(storage.Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(analyzer, "create_client",
                        lambda api_key: CountingClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)
    return None


def _fresh(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=180)
    at.run()
    at.session_state["input_mode"] = "text"
    at.run()
    return at


def _parse(at, chat):
    at.text_area[0].set_value(chat)
    for b in at.button:
        if b.label == "解析并替换当前聊天":
            b.click()
            break
    at.run()
    assert not at.exception


def _all_mode(at):
    """切到“分页浏览全部”。"""
    at.radio[0].set_value("all")
    at.run()
    assert not at.exception


def _by_label(at, label):
    return [b for b in at.button if b.label == label]


def _page_badges(at, page_text):
    return [m for m in at.markdown if page_text in str(m.value)]


def _ranges(at):
    return [c for c in at.caption if str(c.value).startswith("本页 ")]


def _nonce(at):
    return int((at.session_state.get("signalens_scroll_nonce") or {})
               .get(app.PREVIEW_SCROLL_AREA, 0))


def _pending(at):
    return (at.session_state.get("signalens_scroll_pending") or {})


# ---------------------------------------------------------------------------
# 统一渲染：上下两处结构一致、状态共享
# ---------------------------------------------------------------------------


def test_preview_nav_top_and_bottom_share_one_layout(counting_client):
    at = _fresh(counting_client)
    _parse(at, _chat(95))
    _all_mode(at)

    prevs = _by_label(at, PREV)
    nexts = _by_label(at, NEXT)
    # 顶部 + 底部各一组，键不冲突（同一渲染函数的 position 参数）
    assert len(prevs) == 2 and len(nexts) == 2
    assert {b.proto.id for b in prevs}.isdisjoint({b.proto.id for b in nexts})

    # 第 1 / 3 页：上一页两处都禁用，下一页两处都可用
    assert all(b.proto.disabled for b in prevs)
    assert not any(b.proto.disabled for b in nexts)

    # 页码居中渲染两份（顶部 + 底部）→ 同一个渲染函数、同一个状态
    assert len(_page_badges(at, "第 1 / 3 页")) == 2
    # 日期范围单独一行 caption（不挤在翻页行里）
    ranges = _ranges(at)
    assert len(ranges) == 2
    assert all(r.value.startswith("本页 2026-09-01") for r in ranges)


def test_top_and_bottom_share_the_same_page_state(counting_client):
    at = _fresh(counting_client)
    _parse(at, _chat(95))
    _all_mode(at)

    # 点**顶部**下一页 → 底部指示同步变成第 2 页
    _by_label(at, NEXT)[0].click()
    at.run()
    assert len(_page_badges(at, "第 2 / 3 页")) == 2

    # 点**底部**上一页 → 顶部指示同步回到第 1 页
    _by_label(at, PREV)[-1].click()
    at.run()
    assert len(_page_badges(at, "第 1 / 3 页")) == 2


def test_last_page_disables_next_on_both_navs(counting_client):
    at = _fresh(counting_client)
    _parse(at, _chat(95))
    _all_mode(at)

    for _ in range(5):
        nexts = [b for b in _by_label(at, NEXT) if not b.proto.disabled]
        if not nexts:
            break
        nexts[-1].click()
        at.run()

    assert len(_page_badges(at, "第 3 / 3 页")) == 2
    assert all(b.proto.disabled for b in _by_label(at, NEXT))
    assert not any(b.proto.disabled for b in _by_label(at, PREV))


# ---------------------------------------------------------------------------
# 只在页码真正变化时滚动
# ---------------------------------------------------------------------------


def test_real_page_turn_registers_exactly_one_scroll_request(counting_client):
    at = _fresh(counting_client)
    _parse(at, _chat(95))
    _all_mode(at)

    before = _nonce(at)
    _by_label(at, NEXT)[-1].click()
    at.run()

    # nonce 前进 1 → 每次翻页的锚点 HTML 都不同（连续翻页每页都触发）
    assert _nonce(at) == before + 1
    # 渲染时已被一次性消费：普通 rerun 不会残留待滚动请求
    assert app.PREVIEW_SCROLL_AREA not in _pending(at)

    before2 = _nonce(at)
    _by_label(at, NEXT)[-1].click()
    at.run()
    assert _nonce(at) == before2 + 1
    assert at.session_state["preview_page"] == 3


def test_goto_preview_page_ignores_unchanged_page(monkeypatch):
    """页码没变 → 不改状态、不登记滚动、不 rerun（边界守卫，防多余重渲染）。"""
    sa._PROCESS_PENDING.clear()
    sa._PROCESS_NONCE.clear()
    monkeypatch.setattr(sa, "_use_process_store", lambda: True)
    reruns = []
    monkeypatch.setattr(st, "rerun", lambda *a, **k: reruns.append(1))
    monkeypatch.setattr(st, "session_state", {"preview_page": 3},
                        raising=False)

    app._goto_preview_page(3)                 # 页码没变
    assert reruns == []
    assert sa._PROCESS_PENDING == {}          # 没有登记任何滚动请求
    assert sa.current_nonce(app.PREVIEW_SCROLL_AREA) == 0

    app._goto_preview_page(7)                 # 真的翻页
    assert reruns == [1]
    assert sa._PROCESS_PENDING[app.PREVIEW_SCROLL_AREA] == {
        "page": 7, "nonce": 1}
    assert st.session_state["preview_page"] == 7


def test_goto_preview_page_clamps_below_first_page(monkeypatch):
    sa._PROCESS_PENDING.clear()
    sa._PROCESS_NONCE.clear()
    monkeypatch.setattr(sa, "_use_process_store", lambda: True)
    reruns = []
    monkeypatch.setattr(st, "rerun", lambda *a, **k: reruns.append(1))
    monkeypatch.setattr(st, "session_state", {"preview_page": 1},
                        raising=False)

    app._goto_preview_page(0)                 # 试图越过首页
    assert reruns == []                       # 已在第 1 页 → 不动
    app._goto_preview_page(-3)                 # 负页码夹紧到 1 → 仍不动
    assert reruns == []


# ---------------------------------------------------------------------------
# 分页只读本地数据
# ---------------------------------------------------------------------------


def test_paging_never_calls_jev_and_keeps_messages(counting_client):
    at = _fresh(counting_client)
    _parse(at, _chat(95))
    _all_mode(at)
    msgs_before = at.session_state["messages"]
    order_before = [m.get("time") for m in msgs_before]

    _by_label(at, NEXT)[-1].click()
    at.run()

    # 消息列表 / 全局 index 顺序不变（分页只改展示取窗）
    assert at.session_state["messages"] is msgs_before
    assert [m.get("time") for m in at.session_state["messages"]] == order_before
    assert at.session_state["preview_page"] == 2
    assert not at.exception
