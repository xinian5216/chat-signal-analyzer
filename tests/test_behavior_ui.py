"""「长期行为观察」UI（AppTest）集成测试：候选确认 / 排除 / 手动添加 / 分页 / 报告。

全部使用**全虚构**聊天 + 临时数据库；客户端为 CountingClient，绝不调用真实
Jev（每个测试都断言分析调用次数不变）。
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import analyzer
import behavior as bv
import friend_history as fh
import paths
import storage

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CHAT = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
辛苦啦，别熬太晚，早点睡

小明.
2026年08月21日 21:06
嗯嗯

小安.
2026年08月21日 21:10
抱抱，别难过

小明.
2026年08月22日 09:00
周末有空吗

小安.
2026年08月22日 09:05
周末一起吃饭吧，我请你，地点你定

小明.
2026年08月22日 09:06
好啊

小安.
2026年08月25日 10:00
定位我订好了，到时见

小明.
2026年08月27日 11:00
出发了"""


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
                          confidence=0.9,
                          score=score if score is not None else 2.0,
                          noul=noul if noul is not None else 0.2)

            return NS(answers={
                "emotion": fake(choice="calm"),
                "intent": fake(choice="show_care"),
                "warmth": fake(score=3.0),
                "engagement": fake(score=2.5),
                "special_attention": fake(score=1.0),
                "relationship_evidence_strength": fake(score=2.4),
                "relational_ease": fake(score=2.0),
                "romantic_signal": fake(noul=0.1),
                "distancing_signal": fake(noul=0.1),
            }, model="jev-fixture")

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


def _widgets(at, kind, key_prefix):
    return [e for e in getattr(at, kind)
            if str(getattr(e, "key", "")).startswith(key_prefix)]


def _parse_and_analyze(at):
    at.text_area[0].set_value(CHAT)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    at.selectbox[0].select("小明.")
    at.selectbox[1].select("小安.")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()          # pending → 真正执行
    assert not at.exception
    assert at.session_state["analysis_state"] == "complete"


def _open_behavior(at):
    at.segmented_control[0].set_value("长期观察")
    at.run()
    assert not at.exception
    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    for t in at.text_input:
        if t.label == label:
            t.set_value("小安")
            break
    _button(at, "用这个名字新建档案").click()
    at.run()
    assert not at.exception
    assert "长期行为观察" in _texts(at)


def _events(history):
    store = fh.FriendStore(history)
    friend = store.list_friends()[0]
    return store, store.list_events(friend.friend_id)


# ---------------------------------------------------------------------------
# 候选确认
# ---------------------------------------------------------------------------


def test_confirm_candidate_creates_event_and_report(history,
                                                    counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    before = len(counting_client)

    # 确认前候选数（用于验证确认后同身份的候选不再重复呈现）
    def _pending_count(app) -> int:
        for line in _texts(app).splitlines():
            if "条待核对" in line and "候选 " in line:
                return int(line.split("候选 ")[1].split(" 条待核对")[0])
        return -1

    pending_before = _pending_count(at)
    assert pending_before >= 1

    # 把第一条候选的立场改成"支持性"，并写人工说明
    stance_radios = _widgets(at, "radio", "behavior_stance_")
    assert stance_radios, "候选表单未渲染"
    stance_radios[0].set_value("supporting")
    note = _widgets(at, "text_area", "behavior_note_")[0]
    note.set_value("TA 认真回应了我说的困难")
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception

    store, events = _events(history)
    assert len(events) == 1
    event = events[0]
    assert event["status"] == "confirmed"
    assert event["stance"] == "supporting"
    assert event["notes"] == "TA 认真回应了我说的困难"
    assert event["dimension"] == bv.DIMENSION_CARE
    assert event["fingerprints"]
    # 第一条候选是 care_response，窗口 [0, 1]（我的困难发言 + TA 回应）：
    # 边界不能被 1-based 显示编号错位（历史上出现过窗口偏移一位导致
    # 事件身份与候选不匹配、确认后候选不消失的缺陷）
    assert event["behavior_type"] == "care_response"
    assert event["msg_window"] == [0, 1]
    # 确认后：候选数减一（同身份不重复呈现）、报告区出现
    assert _pending_count(at) == pending_before - 1
    assert "长期行为事件报告" in _texts(at)
    # 整个流程 0 额外 Jev 请求
    assert len(counting_client) == before


def test_exclude_candidate_keeps_rejected_trace(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    before = len(counting_client)

    _button(at, "排除这条").click()
    at.run()
    assert not at.exception

    store, events = _events(history)
    assert len(events) == 1
    assert events[0]["status"] == "rejected"
    # 排除记录在面板里可查（不静默丢弃）：以可展开列表呈现
    labels = " ".join(str(e.label) for e in at.expander)
    assert "已排除的候选" in labels
    assert len(counting_client) == before


def test_candidate_pagination_shows_batches(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    texts = _texts(at)
    assert "待人工核对的候选" in texts
    assert "第 1 /" in texts                       # 第一批
    pending_total = None
    for line in texts.splitlines():
        if "条待核对" in line:
            pending_total = int(line.split("候选 ")[1].split(" 条")[0])
            break
    assert pending_total >= 1
    _button(at, "下一批 ▶").click()
    at.run()
    assert not at.exception
    assert "第 2 /" in _texts(at)
    # 往回翻
    _button(at, "◀ 上一批").click()
    at.run()
    assert "第 1 /" in _texts(at)


# ---------------------------------------------------------------------------
# 手动添加 + 编辑 + 删除
# ---------------------------------------------------------------------------


def test_manual_add_event(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)

    for n in at.number_input:
        if n.label == "起始消息编号（从 1 开始）":
            n.set_value(1)
        elif n.label == "结束消息编号（含）":
            n.set_value(2)
    dim = [s for s in at.selectbox
           if str(s.key).startswith("behavior_manual_dim_")][0]
    dim.set_value("care")
    at.run()
    type_select = [s for s in at.selectbox
                   if str(s.key).startswith("behavior_manual_type_")][0]
    type_select.set_value("care_response")
    stance = [r for r in at.radio
              if str(r.key).startswith("behavior_manual_stance_")][0]
    stance.set_value("counter")
    for t in at.text_area:
        if str(t.key).startswith("behavior_manual_notes_"):
            t.set_value("我自己复盘时注意到的一次互动")
    _button(at, "添加事件").click()
    at.run()
    assert not at.exception

    store, events = _events(history)
    assert len(events) == 1
    event = events[0]
    assert event["source_kind"] == "manual"
    assert event["dimension"] == "care"
    assert event["behavior_type"] == "care_response"
    assert event["stance"] == "counter"
    assert event["msg_window"] == [0, 1]
    assert len(event["fingerprints"]) == 2
    assert event["notes"] == "我自己复盘时注意到的一次互动"


def test_edit_and_delete_event(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception

    # 修改人工说明 + 主观感受
    for t in at.text_area:
        if t.key.startswith("behavior_event_notes_"):
            t.set_value("修改后的说明")
            break
    for t in at.text_area:
        if t.key.startswith("behavior_event_feeling_"):
            t.set_value("这段关系让我觉得安心")
            break
    _button(at, "保存修改").click()
    at.run()
    assert not at.exception
    store, events = _events(history)
    assert events[0]["notes"] == "修改后的说明"
    assert events[0]["user_feeling"] == "这段关系让我觉得安心"
    audit = [a["action"] for a in store.get_event(events[0]["event_id"])
             ["audit"]]
    assert "edited" in audit

    # 删除（两步确认）
    for b in at.button:
        if b.label == "删除这个事件":
            b.click()
            break
    at.run()
    assert not at.exception
    _button(at, "确认删除").click()
    at.run()
    assert not at.exception
    store, events = _events(history)
    assert events == []


def test_behavior_panel_has_no_score_output(history, counting_client):
    """面板与报告里不得出现任何"尊重分 / 喜欢概率"式的数值输出。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception
    texts = _texts(at)
    # 说明性文字允许提到这些词（明确声明不生成），但不允许出现"评分输出"
    assert "尊重分：" not in texts
    assert "喜欢概率：" not in texts
    assert "好感度" not in texts
    # 反对过度推断的措辞必须在场
    assert "不生成" in texts


# ---------------------------------------------------------------------------
# Phase 2A.1：P0-1 手动路径脱敏 / P0-3 跨好友隔离 / P1 分页与校验
# ---------------------------------------------------------------------------

PII_TEXT = ("我叫林小满，电话 13812345678，邮箱 lin@example.com，"
            "主页 https://example.com/lin")


def _switch_friend(at, alias: str, label: str):
    """换档案：走「用这个名字新建档案」（同别名不同档案 = 重名场景）。"""
    lookup = ("用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）")
    for t in at.text_input:
        if t.label == lookup:
            t.set_value(alias)
            break
    _button(at, label).click()
    at.run()
    assert not at.exception


def test_manual_snippet_masked_before_write(history, counting_client):
    """手动添加：片段里的虚构 PII 落库前必须被脱敏（store 层兜底）。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)

    keep = [c for c in at.checkbox
            if str(c.key).startswith("behavior_manual_keep_")][0]
    keep.check()
    at.run()
    snippet = [t for t in at.text_area
               if str(t.key).startswith("behavior_manual_snippet_")][0]
    snippet.set_value(PII_TEXT)
    _button(at, "添加事件").click()
    at.run()
    assert not at.exception

    store, events = _events(history)
    assert len(events) == 1
    stored = events[0]["snippet"]
    for secret in ("13812345678", "lin@example.com",
                   "https://example.com/lin"):
        assert secret not in stored, secret
    assert "<PHONE>" in stored and "<EMAIL>" in stored
    # PII 提示在场（正则脱敏不保证匿名 + 说明/主观感受也可能含 PII）
    assert "不能保证完全匿名" in _texts(at)
    assert "主观感受" in _texts(at)


def test_cross_friend_candidate_state_isolated(history, counting_client):
    """好友 A / B 同指纹同类型：切换好友不得继承备注、感受、片段。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)                      # 建的是好友 A（别名 小安）

    # A：写一条明显的备注后确认
    note = _widgets(at, "text_area", "behavior_note_")[0]
    note.set_value("A的备注-不得串到B")
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception
    store = fh.FriendStore(history)
    friends = store.list_friends()
    assert len(friends) == 1
    friend_a = friends[0]
    events_a = store.list_events(friend_a.friend_id)
    assert len(events_a) == 1
    assert events_a[0]["notes"] == "A的备注-不得串到B"

    # 切到好友 B（同一份聊天、同一别名：重名档案）
    _button(at, "换一个档案").click()
    at.run()
    _switch_friend(at, "小安", "用这个名字新建档案")
    assert "长期行为观察" in _texts(at)

    # B 的候选表单必须是全新作用域：备注为空（不是 A 的备注）
    note_b = _widgets(at, "text_area", "behavior_note_")[0]
    assert note_b.value == "", f"跨好友串味：{note_b.value!r}"
    stance_b = _widgets(at, "radio", "behavior_stance_")[0]
    assert stance_b.value == "unspecified"
    assert _widgets(at, "checkbox", "behavior_keep_")
    assert not _widgets(at, "checkbox", "behavior_keep_")[0].value

    # B 确认同一条候选（同身份）→ 两条事件，互不影响
    note_b.set_value("B的备注")
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception
    store = fh.FriendStore(history)
    friends = {f.friend_id: f for f in store.list_friends()}
    assert len(friends) == 2
    events_a2 = store.list_events(friend_a.friend_id)
    assert len(events_a2) == 1
    assert events_a2[0]["notes"] == "A的备注-不得串到B"
    friend_b = [f for fid, f in friends.items() if fid != friend_a.friend_id][0]
    events_b = store.list_events(friend_b.friend_id)
    assert len(events_b) == 1
    assert events_b[0]["notes"] == "B的备注"
    assert events_b[0]["event_identity"] == events_a2[0]["event_identity"]


# >60 条候选的虚构聊天（邀约 + 关心循环，候选密度高）
LONG_CHAT = "".join(
    f"""小明.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
在忙吗 {i}

小安.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
周末一起吃饭吧，我请你，地点你定

小明.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
今天好累，压力好大

小安.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
辛苦啦，别熬太晚，我陪你

"""
    for i in range(24)
)


def _parse_and_analyze_chat(at, chat: str):
    at.text_area[0].set_value(chat)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    at.selectbox[0].select("小明.")
    at.selectbox[1].select("小安.")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()
    assert not at.exception
    assert at.session_state["analysis_state"] == "complete"


def test_candidate_pagination_past_forty(history, counting_client):
    """>60 条候选：逐批翻到第 9 批（第 41 条之后）仍能确认。"""
    at = _fresh()
    _parse_and_analyze_chat(at, LONG_CHAT)
    _open_behavior(at)

    texts = _texts(at)
    m = [line for line in texts.splitlines() if "条待核对" in line]
    total = int(m[0].split("候选 ")[1].split(" 条待核对")[0])
    assert total >= 60, f"candidates={total}"
    for _ in range(8):
        _button(at, "下一批 ▶").click()
        at.run()
        assert not at.exception
    assert "第 9 /" in _texts(at)
    # 第 9 批的第 1 条 = 全局第 41 条候选（与被 40 条上限截断的旧实现对比）
    from parser import parse_chat
    from privacy import mask_messages
    from timeline import sort_messages
    messages = sort_messages(mask_messages(
        parse_chat(LONG_CHAT, "小明.", "小安."))).messages
    expected = bv.generate_candidates(messages)
    assert len(expected) >= 60
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception
    store, events = _events(history)
    assert len(events) == 1
    assert events[0]["event_identity"] == expected[40].identity


def test_history_candidate_confirm_keeps_run_window(history,
                                                   counting_client):
    """UI 里确认历史候选：窗口与指纹保持原 run，不与当前聊天混。"""
    import friend_history as _fh
    from parser import parse_chat
    from privacy import mask_messages
    from scoring import compute_conversation_stats
    from timeline import sort_messages

    store0 = _fh.FriendStore(history)
    friend = store0.create_friend("档案", aliases=["小安"])
    messages = sort_messages(mask_messages(parse_chat(CHAT, "小明.",
                                                      "小安."))).messages
    # 造一条历史快照：第 3、5 条 TA 消息带强浪漫 / show_care 证据
    results = [
        {"index": 3, "speaker": "them", "time": messages[3].get("time"),
         "cached": True,
         "result": {"intent": {"choice": "show_care"}, "model": "m"}},
        {"index": 5, "speaker": "them", "time": messages[5].get("time"),
         "cached": True,
         "result": {"romantic_signal": 0.9, "model": "m"}},
    ]
    run_id = store0.save_run(_fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results),
        schema_version="chat-signal-v3.3",
        request_model="jev-latest", summary_text="旧总结"))
    run = store0.get_run(run_id)
    history_fps = {row["index"]: row["fingerprint"] for row in run["messages"]}

    at = _fresh()
    _parse_and_analyze(at)
    # 查找已存在的档案并选择
    at.segmented_control[0].set_value("长期观察")
    at.run()
    for t in at.text_input:
        if t.label == ("用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"):
            t.set_value("小安")
            break
    _button(at, "查找档案").click()
    at.run()
    assert not at.exception
    at.radio[0].set_value(at.radio[0].options[0])
    _button(at, "确认使用这个档案").click()
    at.run()
    assert not at.exception
    assert "长期行为观察" in _texts(at)

    # 翻到含历史候选的那一批（历史候选排在当前候选之后）。
    # 信号必须用只属于候选的「（run …」后缀——面板总说明里也含
    # 「历史分析的既有指标辅助筛选」，会造成第一页假阳性。
    found = False
    for _ in range(12):
        if "（run " in _texts(at):
            found = True
            break
        _button(at, "下一批 ▶").click()
        at.run()
        assert not at.exception
    assert found, "history candidate not reachable"
    assert "原分析快照" in _texts(at)          # 编号归属提示在场

    # 确认第一条（当前批里最后一个 = 历史候选在最后）
    confirm = [b for b in at.button if b.label == "确认这条事件"]
    confirm[-1].click()
    at.run()
    assert not at.exception

    store, events = _events(history)
    history_events = [e for e in events if e["source_kind"] == "history"]
    assert len(history_events) == 1
    event = history_events[0]
    assert event["status"] == "confirmed"
    assert event["source_run_id"] == run_id
    # 窗口是 run 内的下标，指纹是 run 里真实保存的指纹
    assert event["msg_window"] == [event["msg_window"][0],
                                   event["msg_window"][0]]
    assert event["fingerprints"][0] in set(history_fps.values())
    assert event["flags"]["context_missing"] is True


def test_candidate_dimension_change_updates_type_and_rejects_bad_pair(
        history, counting_client):
    """form 内改方向：类型选项对应新方向；非法组合绝不落库。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)

    # 把第一条候选的方向改成「好感与关系性质」再提交
    dim = _widgets(at, "selectbox", "behavior_dim_")[0]
    dim.set_value("romance")
    _button(at, "确认这条事件").click()
    at.run()
    assert not at.exception

    # 无论 Streamlit 是否重置了类型值：数据库里绝不允许出现
    # （方向, 行为类型）非法组合
    store, events = _events(history)
    for event in events:
        assert bv.valid_pair(event["dimension"], event["behavior_type"]), event
    # 类型下拉的选项必须属于新方向
    type_select = _widgets(at, "selectbox", "behavior_type_")[0]
    expected_labels = {bv.BEHAVIOR_TYPE_LABELS[f"romance.{key}"]
                      for key, _ in bv.BEHAVIOR_TYPES["romance"]}
    assert set(type_select.options) == expected_labels
