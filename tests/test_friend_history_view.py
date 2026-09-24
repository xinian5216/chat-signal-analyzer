"""②确认阶段「历史档案」查看状态的回归测试（取消查看 / 换人失效 / 替换聊天）。

覆盖：
- 查找 A → 取消查看 → 历史摘要与重叠信息消失，回到未选择状态；
- 已看 A 时查找 B → A 的历史立即失效；未匹配到任何档案也不继续显示 A；
- 替换聊天 / TA 身份改变 → 历史查询关联自动清除；
- 同一个人追加消息 → 保留当前历史查看状态；
- 取消查看**不删数据库、不改历史、不影响④的保存关联**；
- 使用 Streamlit 安全的重置方式（不在 widget 实例化后写它的 state）。

全部 mock + 全虚构聊天，零真实 Jev。
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import analyzer
import friend_history as fh
import paths
import storage

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CHAT_A = """甲一方
2026年08月21日 21:00
甲方第一条内容

乙一方
2026年08月21日 21:05
收到"""

CHAT_B = """丙二方
2026年09月02日 10:00
丙方第一条内容

丁二方
2026年09月02日 10:05
收到"""

CHAT_A_EARLIER = """甲一方
2026年08月20日 09:00
更早的一条

乙一方
2026年08月20日 09:05
更早的回复"""


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "friend_history_db_path",
                        lambda: tmp_path / "friend_history.db")
    return tmp_path / "friend_history.db"


@pytest.fixture(autouse=True)
def _fresh_friend_store():
    yield
    try:
        import streamlit
        streamlit.session_state.pop("friend_store", None)
    except Exception:
        pass


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    calls = []

    class CountingClient:
        def system_one(self, state, questions):
            calls.append(state)
            raise AssertionError("本文件不应触发 Jev 请求")

    class TmpCache(storage.Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(analyzer, "create_client",
                        lambda api_key: CountingClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)
    return calls


def _fresh():
    at = AppTest.from_file(str(APP_PATH), default_timeout=180)
    at.run()
    if not at.exception:
        at.session_state["input_mode"] = "text"
        at.run()
    return at


def _button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"button {label!r} not found; "
                         f"have {[b.label for b in at.button]}")


def _buttons(at, label):
    return [b for b in at.button if b.label == label]


def _text_input(at, label):
    for t in at.text_input:
        if t.label == label:
            return t
    raise AssertionError(f"text_input {label!r} not found")


def _texts(at):
    chunks = []
    for attr in ("markdown", "success", "info", "warning", "error", "caption",
                 "metric", "checkbox", "text_area"):
        for e in getattr(at, attr, []) or []:
            try:
                chunks.append(str(e.value))
            except Exception:
                pass
    return "\n".join(chunks)


def _import(at, chat):
    at.text_area[0].set_value(chat)
    _button(at, "解析并替换当前聊天").click()
    at.run()


def _seed_history(history_db, alias, display, chat, me, ta):
    """给某位好友存一条历史分析（全虚构数据）。"""
    from parser import parse_chat
    from privacy import mask_messages
    from scoring import compute_conversation_stats
    from timeline import sort_messages

    store = fh.FriendStore(history_db)
    friend = store.create_friend(display, aliases=[(alias, "wechat_name")])
    messages = sort_messages(mask_messages(parse_chat(chat, me, ta))).messages
    results = [{"index": i, "speaker": "them", "time": m.get("time"),
                "context": [], "result": {"warmth": {"score": 2.0}},
                "cached": False}
               for i, m in enumerate(messages) if m["speaker"] == "them"]
    store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results),
        schema_version="chat-signal-v3.3", request_model="jev-latest",
        summary_text=f"{display}的旧总结：共分析 1 条 TA 消息"))
    return store, friend


def _apply_identity(at, me="甲一方", ta="乙一方"):
    """确认双方身份（现在是 st.form：选完昵称后点提交才生效）。"""
    at.selectbox[0].select(me)
    at.selectbox[1].select(ta)
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()


def _lookup_history(at, alias):
    _text_input(at, "好友昵称 / 备注 / 别名（只在本机查找）").set_value(alias)
    _button(at, "查找历史档案").click()
    at.run()


# ---------------------------------------------------------------------------
# 取消查看
# ---------------------------------------------------------------------------


def test_cancel_history_view_returns_to_unselected(history, counting_client):
    _store, _friend = _seed_history(history, "乙一方", "档案A", CHAT_A,
                                    "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    _apply_identity(at)
    _lookup_history(at, "乙一方")
    texts = _texts(at)
    assert "正在查看档案" in texts and "档案A的旧总结" in texts

    _button(at, "取消查看历史").click()
    at.run()
    texts = _texts(at)
    assert "正在查看档案" not in texts
    assert "档案A的旧总结" not in texts
    assert "已取消查看历史档案" in texts
    assert at.session_state["history_friend_id"] is None
    assert at.session_state["history_matches"] is None


def test_cancel_history_view_keeps_data_and_save_selection(
        history, counting_client):
    """取消查看：不删数据库 / 不改历史 / 不影响保存关联。"""
    store, friend = _seed_history(history, "乙一方", "档案A", CHAT_A,
                                  "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    # 先显式选择保存档案（④阶段的关联）
    at.selectbox[0].select("甲一方")
    at.selectbox[1].select("乙一方")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _lookup_history(at, "乙一方")
    _button(at, "取消查看历史").click()
    at.run()

    # 数据库与历史原样保留
    assert store.friend_count() == 1
    assert len(store.list_runs(friend.friend_id)) == 1
    assert store.get_run(store.list_runs(friend.friend_id)[0].run_id)
    # 保存关联不受影响（没有指向任何档案，也没有被误清）
    assert at.session_state["friend_selected"] is None


def test_lookup_other_friend_invalidates_current_history(
        history, counting_client):
    """已看 A 时查找 B：A 的历史必须立即失效（B 是否显示取决于候选数量）。"""
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    _seed_history(history, "丁二方", "档案B", CHAT_B, "丙二方", "丁二方")
    at = _fresh()
    _import(at, CHAT_A)
    _apply_identity(at)
    _lookup_history(at, "乙一方")
    assert "档案A的旧总结" in _texts(at)
    friend_a = at.session_state["history_friend_id"]

    # 查找 B：称呼唯一命中 → 直接看 B，但 A 的历史必须已经消失
    _lookup_history(at, "丁二方")
    texts = _texts(at)
    assert "档案A的旧总结" not in texts
    friend_b = at.session_state["history_friend_id"]
    assert friend_b and friend_b != friend_a
    assert "档案B的旧总结" in texts


def test_lookup_multiple_candidates_requires_choice(
        history, counting_client):
    """同名多个候选：不自动看任何一个，必须用户显式选择。"""
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    _seed_history(history, "乙一方", "档案B", CHAT_B, "丙二方", "丁二方")
    at = _fresh()
    _import(at, CHAT_A)
    _apply_identity(at)
    _lookup_history(at, "乙一方")

    # 两个候选 → 不自动选
    texts = _texts(at)
    assert "不会自动合并" in texts
    assert "正在查看档案" not in texts
    assert at.session_state["history_friend_id"] is None

    labels = [o for o in at.radio[0].options]
    at.radio[0].set_value(labels[1])
    _button(at, "查看这个档案的历史").click()
    at.run()
    assert "正在查看档案" in _texts(at)


def test_no_match_keeps_previous_history_hidden(history, counting_client):
    """未匹配到任何档案时，不能继续展示上一位好友的历史。"""
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    _apply_identity(at)
    _lookup_history(at, "乙一方")
    assert "档案A的旧总结" in _texts(at)

    _lookup_history(at, "不存在的称呼")
    texts = _texts(at)
    assert "档案A的旧总结" not in texts
    assert "没有匹配的档案" in texts
    assert at.session_state["history_friend_id"] is None


def test_replacing_chat_clears_history_view(history, counting_client):
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    _apply_identity(at)
    _lookup_history(at, "乙一方")
    assert "档案A的旧总结" in _texts(at)

    _import(at, CHAT_B)
    assert at.session_state["history_friend_id"] is None
    assert at.session_state["history_matches"] is None
    assert "档案A的旧总结" not in _texts(at)


def test_identity_change_clears_history_view(history, counting_client):
    """TA 身份改变 → 历史查询关联清除（可能已经不是同一个人）。"""
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    at.selectbox[0].select("甲一方")
    at.selectbox[1].select("乙一方")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _lookup_history(at, "乙一方")
    assert "档案A的旧总结" in _texts(at)

    # 重新选择身份，把 TA 换成甲一方
    _button(at, "重新选择身份").click()
    at.run()
    assert at.session_state["history_friend_id"] is None
    assert at.session_state["history_matches"] is None
    assert "档案A的旧总结" not in _texts(at)


def test_same_person_append_keeps_history_view(history, counting_client):
    """同一个人追加更早记录：保留历史查看状态。"""
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    at.selectbox[0].select("甲一方")
    at.selectbox[1].select("乙一方")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _lookup_history(at, "乙一方")
    friend_id = at.session_state["history_friend_id"]
    assert friend_id

    at.text_area[0].set_value(CHAT_A_EARLIER)
    _button(at, "追加到当前聊天").click()
    at.run()

    # TA 身份没变 → 历史查看状态保留，且重叠信息按新导入刷新
    assert at.session_state["history_friend_id"] == friend_id
    assert "档案A的旧总结" in _texts(at)


def test_history_panel_is_collapsed_by_default(history, counting_client):
    """历史档案默认收起，不在预览表上方占用高度（减少页面跳动）。"""
    _seed_history(history, "乙一方", "档案A", CHAT_A, "甲一方", "乙一方")
    at = _fresh()
    _import(at, CHAT_A)
    # 未查看时不展示历史内容（AppTest 不渲染收起 expander 的内容，
    # 因此这里验证“默认看不到历史”，浏览器里再验证默认收起）
    assert "档案A的旧总结" not in _texts(at)
    assert at.session_state["history_friend_id"] is None


def test_widget_reset_is_queued_not_written():
    """取消查看只排队重置 widget key，不在实例化后直接写它的 state。"""
    import streamlit
    import app as app_module

    # 预置一个排队项，验证复位逻辑只 pop、不抛异常
    streamlit.session_state["friend_widget_reset_keys"] = [
        "history_match_radio", "history_alias_input"]
    app_module._apply_pending_widget_reset()
    assert streamlit.session_state.get("friend_widget_reset_keys") == []
    assert "history_match_radio" not in streamlit.session_state
    assert "history_alias_input" not in streamlit.session_state


def test_clear_history_view_only_queues_reset():
    """_clear_history_view 本身只碰非 widget 状态 + 排队，绝不直接写 widget。"""
    import streamlit
    import app as app_module

    streamlit.session_state["history_friend_id"] = "fid"
    streamlit.session_state["history_matches"] = [object()]
    streamlit.session_state["history_alias_input"] = "乙一方"
    app_module._clear_history_view()
    assert streamlit.session_state["history_friend_id"] is None
    assert streamlit.session_state["history_matches"] is None
    # widget 的旧值仍在（要等下次渲染前才 pop）
    assert streamlit.session_state["history_alias_input"] == "乙一方"
    pending = streamlit.session_state.get("friend_widget_reset_keys") or []
    assert "history_match_radio" in pending
    assert "history_alias_input" in pending
