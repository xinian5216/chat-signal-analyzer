"""好友档案 UI（AppTest）集成测试：保存 / 查找 / 歧义 / 历史面板。

覆盖：
- 分析完成后**默认不自动保存**（只有点了按钮才写档案）；
- 用昵称新建档案 → 保存本次分析 → 档案里出现一条 run；
- 同昵称多个档案 → 提示不自动合并，未确认前不能保存；
- 再次保存同一批消息 → 报告/提示为重复分析；
- ② 确认阶段能按昵称看到历史总结与重叠区间；
- “长期观察”视图本地聚合，0 Jev API。

全部使用**全虚构**聊天 + 临时数据库，绝不触真实数据、绝不调用真实 Jev。
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import analyzer
import friend_history as fh
import paths
import storage

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CHAT = """测试甲.
2026年08月21日 21:00
第一条消息内容

测试乙.
2026年08月21日 21:05
收到，谢谢

测试甲.
2026年08月21日 21:10
第二条消息内容

测试乙.
2026年08月21日 21:15
好的，知道了

测试甲.
2026年08月21日 21:20
第三条消息内容

测试乙.
2026年08月21日 21:25
晚安"""


@pytest.fixture
def history(tmp_path, monkeypatch):
    """把好友档案数据库重定向到临时目录（绝不碰仓库里的真实档案）。"""
    monkeypatch.setattr(paths, "friend_history_db_path",
                        lambda: tmp_path / "friend_history.db")
    return tmp_path / "friend_history.db"


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    calls = []

    class CountingClient:
        def system_one(self, state, questions):
            calls.append(state)
            from types import SimpleNamespace as NS

            def fake(choice=None, score=None, noul=None):
                return NS(choice=choice or "other",
                          probabilities={"other": 1.0} if choice else {},
                          confidence=0.9, score=score if score is not None else 2.0,
                          noul=noul if noul is not None else 0.2)

            return NS(answers={
                "emotion": fake(choice="calm"),
                "intent": fake(choice="other"),
                "warmth": fake(score=2.0),
                "engagement": fake(score=2.0),
                "special_attention": fake(score=1.0),
                "relationship_evidence_strength": fake(score=2.4),
                "relational_ease": fake(score=2.0),
                "romantic_signal": fake(noul=0.2),
                "distancing_signal": fake(noul=0.2),
            }, model="jev-1.13.0")

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
    raise AssertionError(f"button {label!r} not found; have {[b.label for b in at.button]}")


def _text_input(at, label):
    for t in at.text_input:
        if t.label == label:
            return t
    raise AssertionError(f"text_input {label!r} not found")


def _checkbox(at, label):
    for c in at.checkbox:
        if c.label == label:
            return c
    raise AssertionError(f"checkbox {label!r} not found")


def _texts(at):
    chunks = []
    for attr in ("markdown", "success", "info", "warning", "error", "caption",
                 "metric", "subheader", "text", "checkbox"):
        for e in getattr(at, attr, []) or []:
            try:
                chunks.append(str(e.value))
            except Exception:
                pass
    return "\n".join(chunks)


def _parse_and_analyze(at):
    at.text_area[0].set_value(CHAT)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    at.selectbox[0].select("测试甲.")
    at.selectbox[1].select("测试乙.")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()          # pending → 真正执行
    assert not at.exception
    assert at.session_state["analysis_state"] == "complete"


def _open_longitudinal(at):
    at.segmented_control[0].set_value("长期观察")
    at.run()
    assert not at.exception


# ---------------------------------------------------------------------------
# 默认不自动保存
# ---------------------------------------------------------------------------


def test_no_auto_save_without_explicit_click(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_longitudinal(at)
    # 只浏览“长期观察”视图，完全没有点击保存
    assert fh.FriendStore(history).friend_count() == 0
    assert "默认不自动保存" in _texts(at)


def test_save_to_new_friend_writes_one_run(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_longitudinal(at)

    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    _text_input(at, label).set_value("测试乙")
    _button(at, "用这个名字新建档案").click()
    at.run()

    store = fh.FriendStore(history)
    assert store.friend_count() == 1
    friend = store.list_friends()[0]
    assert store.run_count(friend.friend_id) == 0        # 还没保存分析

    _button(at, "保存至好友档案").click()
    at.run()
    runs = store.list_runs(friend.friend_id)
    assert len(runs) == 1
    run = runs[0]
    assert run.message_count == 6
    assert run.analyzed_count == 3
    assert run.chat_first_time == "2026-08-21 21:00"
    assert run.chat_last_time == "2026-08-21 21:25"
    assert run.schema_version
    assert run.summary_text
    # 档案里没有聊天正文
    full = store.get_run(run.run_id)
    blob = str(full)
    assert "第一条消息内容" not in blob
    # 保存不产生新的 Jev 请求
    assert len(counting_client) == 3


def test_saving_twice_flags_duplicate(history, counting_client):
    """同一份分析重复保存：按钮不再提供；换一次分析版本则提示重复案例。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_longitudinal(at)
    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    _text_input(at, label).set_value("测试乙")
    _button(at, "用这个名字新建档案").click()
    at.run()
    _button(at, "保存至好友档案").click()
    at.run()

    store = fh.FriendStore(history)
    friend = store.list_friends()[0]
    assert len(store.list_runs(friend.friend_id)) == 1

    # 同一份分析（同版本 + 同案例）再次进入 → 不再提供保存按钮
    _open_longitudinal(at)
    texts = _texts(at)
    assert "已经保存到这个档案" in texts
    assert "保存至好友档案" not in [b.label for b in at.button]

    # 模拟“重新分析了一次同一批消息”（新 revision）→ 提示重复案例，
    # 但仍允许保存（历史不可改，会产生新记录）
    at.session_state["analysis_revision"] = 99
    at.run()
    texts = _texts(at)
    assert "同一批消息" in texts
    _button(at, "保存至好友档案").click()
    at.run()

    runs = store.list_runs(friend.friend_id)
    assert len(runs) == 2
    assert len({r.case_signature for r in runs}) == 1   # 同一批消息


# ---------------------------------------------------------------------------
# 重名不自动合并
# ---------------------------------------------------------------------------


def test_same_alias_two_friends_requires_user_choice(history, counting_client):
    store = fh.FriendStore(history)
    store.create_friend("档案一", aliases=[("测试乙", "wechat_name")])
    store.create_friend("档案二", aliases=[("测试乙", "wechat_name")])

    at = _fresh()
    _parse_and_analyze(at)
    _open_longitudinal(at)
    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    _text_input(at, label).set_value("测试乙")
    _button(at, "查找档案").click()
    at.run()

    texts = _texts(at)
    assert "不会自动合并" in texts
    # 还没有确认 → 不能保存
    assert store.run_count(store.list_friends()[0].friend_id) == 0
    assert "保存至好友档案" not in [b.label for b in at.button]

    # 显式选择其中一个
    labels = [r for r in at.radio[0].options]
    at.radio[0].set_value(labels[0])
    _button(at, "确认使用这个档案").click()
    at.run()
    _button(at, "保存至好友档案").click()
    at.run()

    chosen = int(at.session_state["friend_selected"] is not None)
    assert chosen == 1
    counts = [store.run_count(f.friend_id) for f in store.list_friends()]
    assert counts.count(1) == 1 and counts.count(0) == 1   # 只写进选中的那个


def test_lookup_find_existing_profile_for_same_person(history, counting_client):
    """同人多昵称：用任意一个称呼都能找到同一档案。"""
    store = fh.FriendStore(history)
    friend = store.create_friend("档案一", aliases=[("测试乙", "wechat_name"),
                                                   ("小乙", "remark")])

    at = _fresh()
    _parse_and_analyze(at)
    _open_longitudinal(at)
    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    _text_input(at, label).set_value("小乙")
    _button(at, "查找档案").click()
    at.run()
    at.radio[0].set_value(at.radio[0].options[0])
    _button(at, "确认使用这个档案").click()
    at.run()
    assert at.session_state["friend_selected"] == friend.friend_id

    # 追加第三个称呼（同人多昵称）
    _checkbox(at, "给这个档案追加一个称呼（同人多昵称）").check()
    at.run()
    _text_input(at, "新的昵称 / 备注 / 别名").set_value("乙老板")
    _button(at, "追加称呼").click()
    at.run()
    assert len(store.aliases_of(friend.friend_id)) == 3
    assert len(store.find_by_alias("乙老板")) == 1


# ---------------------------------------------------------------------------
# ② 确认阶段的历史面板
# ---------------------------------------------------------------------------


def test_confirm_stage_shows_history_and_overlap(history, counting_client):
    store = fh.FriendStore(history)
    friend = store.create_friend("档案一", aliases=[("测试乙", "wechat_name")])
    from parser import parse_chat
    from privacy import mask_messages
    from timeline import sort_messages
    from scoring import compute_conversation_stats

    messages = sort_messages(mask_messages(
        parse_chat(CHAT, "测试甲.", "测试乙."))).messages
    results = [{"index": i, "speaker": "them",
                "time": m.get("time"), "context": [],
                "result": {"warmth": {"score": 2.0}, "engagement": {"score": 2.0},
                           "special_attention": {"score": 1.0},
                           "relationship_evidence_strength": {"score": 2.4},
                           "romantic_signal": 0.2, "distancing_signal": 0.2,
                           "model": "jev-1.13.0"},
                "cached": False}
               for i, m in enumerate(messages) if m["speaker"] == "them"]
    store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results), schema_version="chat-signal-v3.3",
        request_model="jev-latest", summary_text="旧总结：本次共分析 3 条 TA 消息"))

    at = _fresh()
    at.text_area[0].set_value(CHAT)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    at.selectbox[0].select("测试甲.")
    at.selectbox[1].select("测试乙.")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()

    _text_input(at, "好友昵称 / 备注 / 别名（只在本机查找）").set_value("测试乙")
    _button(at, "查找历史档案").click()
    at.run()

    texts = _texts(at)
    assert "旧总结" in texts                       # 历史总结可见
    assert "重复导入" in texts or "同一批消息" in texts   # 重叠被标出
    assert "加载档案不会重新调用 Jev" in texts
    assert len(counting_client) == 0               # 查看历史 0 请求


def test_longitudinal_view_is_local_only(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    before = len(counting_client)
    _open_longitudinal(at)
    at.segmented_control[0].set_value("全部消息")
    at.run()
    at.segmented_control[0].set_value("长期观察")
    at.run()
    at.segmented_control[0].set_value("概览")
    at.run()
    assert len(counting_client) == before          # 视图切换 0 请求
    assert not at.exception
