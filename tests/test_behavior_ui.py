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
    dim = [s for s in at.selectbox if s.key == "behavior_manual_dim"][0]
    dim.set_value("care")
    at.run()
    type_select = [s for s in at.selectbox
                   if s.key == "behavior_manual_type"][0]
    type_select.set_value("care_response")
    stance = [r for r in at.radio if r.key == "behavior_manual_stance"][0]
    stance.set_value("counter")
    for t in at.text_area:
        if t.key == "behavior_manual_notes":
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
