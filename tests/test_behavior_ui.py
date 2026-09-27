"""「长期行为观察」UI（AppTest）集成测试：候选确认 / 排除 / 手动添加 / 分页 / 报告。

全部使用**全虚构**聊天 + 临时数据库；客户端为 CountingClient，绝不调用真实
Jev（每个测试都断言分析调用次数不变）。
"""

import re
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

# 确认候选的边界回归：两条连续 TA 消息同一分钟，同一 turn，只有第一条含
# 「哥们」。整个候选应是 #2–#3，默认分类确认后不得继续留在待审核列表。
CHAT_SAME_SPEAKER_SAME_MINUTE = """小明.
2026年08月21日 21:00
今天准备休息

小安.
2026年08月21日 21:01
哥们

小安.
2026年08月21日 21:01
收到"""

CHAT_MULTI_CANDIDATE_SAME_WINDOW = """小明.
2026年08月21日 17:00
收工啦

小安.
2026年08月21日 20:41
哥们，有空一起吃饭吧

小安.
2026年08月21日 20:41
改天聊聊"""


@pytest.fixture
def history(tmp_path, monkeypatch):
    """把好友档案数据库重定向到临时目录（绝不碰仓库里的真实档案）。"""
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "runtime-data"))
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


def _scope_from_widgets(at, key_prefix: str, last: bool = False) -> str:
    """从指定前缀的控件 key 提取表单作用域（DOM 顺序第一个 / 最后一个）。"""
    scopes = []
    for group in (at.text_area, at.checkbox, at.radio, at.selectbox,
                  at.number_input):
        for e in group:
            key = str(getattr(e, "key", ""))
            if key.startswith(key_prefix):
                scopes.append(key[len(key_prefix):])
    assert scopes, f"no widget with key prefix {key_prefix!r}"
    return scopes[-1] if last else scopes[0]


def _preview_then_click(at, name: str, widget_prefix: str,
                        target_prefix: str, last: bool = False):
    """两阶段保存：先点与目标表单**同作用域**的「查看最终预览」，
    再点目标保存按钮。

    作用域从控件 key 精确提取（不靠按钮顺序：候选 → 手动添加 → 事件
    编辑都会渲染「查看最终预览」，顺序会随页面内容变化）。
    ``last=True`` 取页面最后一个候选（历史候选排在候选列表末尾）。
    """
    scope = _scope_from_widgets(at, widget_prefix, last=last)
    preview = next((b for b in at.button
                    if b.key == f"behavior_preview_btn_{scope}"), None)
    assert preview is not None, (
        f"「查看最终预览」按钮未出现（scope={scope}，两阶段门控回归）")
    preview.click()
    at.run()
    assert not at.exception
    target = next((b for b in at.button
                   if b.key == f"{target_prefix}{scope}"), None)
    assert target is not None and target.label == name, (
        f"按钮 {name!r} 在预览后未出现（scope={scope}）")
    target.click()
    at.run()
    assert not at.exception


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


def _parse_and_analyze(at, chat=CHAT):
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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")

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
    event_panel = next(e for e in at.expander
                       if e.key == f"behavior_event_panel_{event['event_id']}")
    assert "2026-08-21 21:00 ~ 2026-08-21 21:05" in event_panel.label
    # 确认后：候选数减一（同身份不重复呈现）、报告区出现
    assert _pending_count(at) == pending_before - 1
    assert "长期行为事件报告" in _texts(at)
    # 整个流程 0 额外 Jev 请求
    assert len(counting_client) == before


@pytest.mark.parametrize(("operation", "expected_status"), [
    ("default_confirm", "confirmed"),
    ("changed_classification", "confirmed"),
    ("changed_range", "confirmed"),
    ("different_stance", "confirmed"),
    ("exclude", "rejected"),
])
def test_same_speaker_same_minute_candidate_review_does_not_reappear(
        history, counting_client, monkeypatch, operation, expected_status):
    """真实 Streamlit UI 链路：同说话方同分钟双消息审核后不再入待审核。

    覆盖默认分类确认、改分类、改立场、排除；同时核对 UI 按钮、事件构造、
    save_event 返回值、SQLite 行、list_events/reviewed_identities、分页前
    pending_candidates，以及同会话刷新和新会话重启后的候选状态。所有数据
    都是虚构的，friend_history 和分析缓存由测试临时目录 fixture 隔离。
    """
    import sqlite3

    at = _fresh()
    _parse_and_analyze(at, CHAT_SAME_SPEAKER_SAME_MINUTE)
    _open_behavior(at)

    messages = (at.session_state.get("analysis_messages")
                or at.session_state["messages"])
    generated = bv.generate_candidates(messages)
    matches = [c for c in generated
               if c.dimension == bv.DIMENSION_ROMANCE
               and c.behavior_type == "close_friendship"]
    assert len(matches) == 1
    target = matches[0]
    assert (target.start, target.end) == (1, 2)
    assert [m["speaker"] for m in messages[1:3]] == ["them", "them"]
    assert messages[1]["time"] == messages[2]["time"]

    pending_before = next(
        int(m.group(1)) for line in _texts(at).splitlines()
        if (m := re.search(r"候选 (\d+) 条待核对", line)))
    assert pending_before == 1
    analysis_calls_before_review = len(counting_client)
    friend_id = at.session_state["friend_selected"]
    assert friend_id

    # 只捕获身份/状态字段，避免把聊天正文写入诊断数据。
    built, saved = [], []
    original_build = bv.build_event_dict
    original_save = fh.FriendStore.save_event

    def traced_build(*args, **kwargs):
        event = original_build(*args, **kwargs)
        built.append({k: event.get(k) for k in (
            "friend_id", "event_identity", "original_candidate_identity",
            "dimension", "behavior_type", "stance", "status")})
        return event

    def traced_save(store, event):
        event_id = original_save(store, event)
        saved.append({"event_id": event_id,
                      "friend_id": event.get("friend_id"),
                      "event_identity": event.get("event_identity"),
                      "original_candidate_identity": event.get(
                          "original_candidate_identity"),
                      "status": event.get("status")})
        return event_id

    monkeypatch.setattr(bv, "build_event_dict", traced_build)
    monkeypatch.setattr(fh.FriendStore, "save_event", traced_save)

    scope_widget = next(
        e for e in at.selectbox
        if str(getattr(e, "key", "")).startswith("behavior_dim_")
        and str(getattr(e, "key", "")).endswith(target.identity[:12]))
    scope = scope_widget.key[len("behavior_dim_"):]

    if operation == "changed_classification":
        dim = next(e for e in at.selectbox
                   if e.key == f"behavior_dim_{scope}")
        dim.set_value(bv.DIMENSION_CARE)
        at.run()
        typ = next(e for e in at.selectbox
                   if e.key == f"behavior_type_{scope}")
        typ.set_value("care_response")
        at.run()
    elif operation == "different_stance":
        stance = next(e for e in at.radio
                      if e.key == f"behavior_stance_{scope}")
        stance.set_value("counter")
        at.run()
    elif operation == "changed_range":
        start = next(e for e in at.number_input
                     if e.key == f"behavior_start_{scope}")
        end = next(e for e in at.number_input
                   if e.key == f"behavior_end_{scope}")
        start.set_value(1)
        end.set_value(3)
        at.run()

    preview = next(b for b in at.button
                   if b.key == f"behavior_preview_btn_{scope}")
    assert preview.label == "查看最终预览"
    _preview_then_click(
        at, "排除这条" if operation == "exclude" else "确认这条事件",
        "behavior_note_",
        "behavior_exclude_" if operation == "exclude"
        else "behavior_confirm_")
    assert not at.exception
    assert len(built) == len(saved) == 1
    assert len(counting_client) == analysis_calls_before_review
    assert saved[0]["event_id"]
    assert built[0]["friend_id"] == saved[0]["friend_id"] == friend_id
    assert saved[0]["status"] == expected_status

    store = fh.FriendStore(history)
    rows = store.list_events(friend_id)
    assert len(rows) == 1
    event = rows[0]
    assert event["event_id"] == saved[0]["event_id"]
    assert event["friend_id"] == friend_id
    assert event["status"] == expected_status
    assert event["original_candidate_identity"] == target.identity
    assert built[0]["original_candidate_identity"] == target.identity
    if operation in ("changed_classification", "changed_range"):
        assert event["event_identity"] != target.identity
    else:
        assert event["event_identity"] == target.identity
    if operation == "changed_range":
        assert event["msg_window"] == [0, 2]
    if operation == "different_stance":
        assert event["stance"] == "counter"

    # SQLite 原始行、Store 读取、已审核身份与重生成候选逐层一致。
    with sqlite3.connect(f"file:{history}?mode=ro", uri=True) as db:
        db_row = db.execute(
            "SELECT event_id, friend_id, status, event_identity, "
            "original_candidate_identity FROM behavior_events "
            "WHERE friend_id = ?", (friend_id,)).fetchone()
        db_count = db.execute(
            "SELECT COUNT(*) FROM behavior_events WHERE friend_id = ?",
            (friend_id,)).fetchone()[0]
    assert db_count == 1
    assert db_row == (event["event_id"], friend_id, expected_status,
                      event["event_identity"], target.identity)
    reviewed = bv.reviewed_identities(rows)
    assert target.identity in reviewed
    if operation == "changed_classification":
        assert event["event_identity"] in reviewed
    pending = bv.pending_candidates(bv.generate_candidates(messages), rows)
    assert target.identity not in {c.identity for c in pending}
    assert not any(c.dimension == bv.DIMENSION_ROMANCE
                   and c.behavior_type == "close_friendship" for c in pending)
    assert pending_before - 1 == next(
        int(m.group(1)) for line in _texts(at).splitlines()
        if (m := re.search(r"候选 (\d+) 条待核对", line)))
    assert not any(e.key == f"behavior_dim_{scope}" for e in at.selectbox)
    if expected_status == "confirmed":
        assert any(e.key == f"behavior_event_panel_{event['event_id']}"
                   for e in at.expander)
    else:
        assert any("已排除的候选" in e.label for e in at.expander)

    # 页面切换触发刷新后仍不复活。
    at.segmented_control[0].set_value("概览")
    at.run()
    at.segmented_control[0].set_value("长期观察")
    at.run()
    assert not at.exception
    assert pending_before - 1 == next(
        int(m.group(1)) for line in _texts(at).splitlines()
        if (m := re.search(r"候选 (\d+) 条待核对", line)))
    assert not any(e.key == f"behavior_dim_{scope}" for e in at.selectbox)

    # 新 AppTest session 模拟重启：经 UI 查找并再次选择同一 friend_id。
    at2 = _fresh()
    _parse_and_analyze(at2, CHAT_SAME_SPEAKER_SAME_MINUTE)
    at2.segmented_control[0].set_value("长期观察")
    at2.run()
    search = next(e for e in at2.text_input
                  if e.label == "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）")
    search.set_value("小安")
    _button(at2, "查找档案").click()
    at2.run()
    at2.radio[0].set_value(at2.radio[0].options[0])
    _button(at2, "确认使用这个档案").click()
    at2.run()
    assert not at2.exception
    assert len(counting_client) == analysis_calls_before_review
    assert at2.session_state["friend_selected"] == friend_id
    messages2 = (at2.session_state.get("analysis_messages")
                 or at2.session_state["messages"])
    target2 = next(c for c in bv.generate_candidates(messages2)
                   if c.dimension == bv.DIMENSION_ROMANCE
                   and c.behavior_type == "close_friendship")
    assert target2.identity == target.identity
    after_restart = bv.pending_candidates(
        bv.generate_candidates(messages2), store.list_events(friend_id))
    assert target2.identity not in {c.identity for c in after_restart}
    assert pending_before - 1 == next(
        int(m.group(1)) for line in _texts(at2).splitlines()
        if (m := re.search(r"候选 (\d+) 条待核对", line)))
    assert not any(str(getattr(e, "key", "")).endswith(target.identity[:12])
                   and str(getattr(e, "key", "")).startswith("behavior_dim_")
                   for e in at2.selectbox)


def test_confirm_notice_keeps_other_same_window_types_pending(
        history, counting_client):
    """共享指纹不应把邀约 / 主动联系等其它类型误标成已审核。"""
    at = _fresh()
    _parse_and_analyze(at, CHAT_MULTI_CANDIDATE_SAME_WINDOW)
    _open_behavior(at)
    messages = (at.session_state.get("analysis_messages")
                or at.session_state["messages"])
    candidates = bv.generate_candidates(messages)
    target = next(c for c in candidates
                  if c.dimension == bv.DIMENSION_ROMANCE
                  and c.behavior_type == "close_friendship")
    siblings = {c.identity for c in candidates if c.identity != target.identity
                and set(c.fingerprints) == set(target.fingerprints)}
    assert {(c.dimension, c.behavior_type) for c in candidates} == {
        (bv.DIMENSION_INITIATIVE, "proactive_contact"),
        (bv.DIMENSION_INITIATIVE, "invitation"),
        (bv.DIMENSION_ROMANCE, "close_friendship"),
    }
    assert len(siblings) == 2
    before_calls = len(counting_client)
    assert any("候选 3 条待核对" in line for line in _texts(at).splitlines())

    scope_widget = next(e for e in at.selectbox
                        if str(getattr(e, "key", "")).startswith(
                            "behavior_dim_")
                        and str(getattr(e, "key", "")).endswith(
                            target.identity[:12]))
    scope = scope_widget.key[len("behavior_dim_"):]
    preview = next(b for b in at.button
                   if b.key == f"behavior_preview_btn_{scope}")
    preview.click()
    at.run()
    save = next(b for b in at.button
                if b.key == f"behavior_confirm_{scope}")
    save.click()
    at.run()
    assert not at.exception
    text = _texts(at)
    assert "已确认事件：好感与关系性质 · 亲密友情" in text
    assert "同一段聊天可能还有其他类型的候选，仍需分别审核" in text
    assert "2 条" in text
    assert len(counting_client) == before_calls

    store, events = _events(history)
    assert len(events) == 1
    reviewed = bv.reviewed_identities(events)
    assert target.identity in reviewed
    assert not (siblings & reviewed)
    pending = bv.pending_candidates(bv.generate_candidates(messages), events)
    assert siblings <= {c.identity for c in pending}
    assert any("候选 2 条待核对" in line for line in _texts(at).splitlines())


@pytest.mark.parametrize(("failure", "category", "stored_rows"), [
    ("readback_missing", "event_readback_missing", 1),
    ("readback_identity_mismatch", "event_readback_mismatch", 1),
    ("original_identity_missing", "original_identity_missing", 1),
    ("reviewed_missing", "candidate_not_in_reviewed_set", 1),
    ("write_exception_no_record", "write_exception_no_record", 0),
    ("write_exception_record_found", "write_exception_record_found", 1),
])
def test_candidate_save_verification_failure_never_claims_success(
        history, counting_client, monkeypatch, failure, category, stored_rows):
    """保存或回读异常必须明确提示，绝不自动重试或虚报成功。"""
    import sqlite3

    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    save_calls = []
    original_save = fh.FriendStore.save_event
    original_get = fh.FriendStore.get_event

    if failure in ("readback_missing", "readback_identity_mismatch"):
        def fake_get(store, event_id):
            row = original_get(store, event_id)
            if failure == "readback_missing":
                return None
            if row is not None:
                row = dict(row)
                row["event_identity"] = "fictional-readback-mismatch"
            return row

        monkeypatch.setattr(fh.FriendStore, "get_event", fake_get)

    if failure == "original_identity_missing":
        def save_without_original(store, event):
            save_calls.append(1)
            stored = dict(event)
            stored["original_candidate_identity"] = ""
            return original_save(store, stored)

        monkeypatch.setattr(fh.FriendStore, "save_event", save_without_original)
    elif failure == "write_exception_no_record":
        def write_error(_store, _event):
            save_calls.append(1)
            raise sqlite3.OperationalError("fictional write failure")

        monkeypatch.setattr(fh.FriendStore, "save_event", write_error)
    elif failure == "write_exception_record_found":
        def save_then_error(store, event):
            save_calls.append(1)
            original_save(store, event)
            raise sqlite3.OperationalError("fictional post-commit error")

        monkeypatch.setattr(fh.FriendStore, "save_event", save_then_error)
    elif failure == "reviewed_missing":
        saved_ids = set()
        original_list = fh.FriendStore.list_events

        def save_then_hide_review(store, event):
            save_calls.append(1)
            event_id = original_save(store, event)
            saved_ids.add(event_id)
            return event_id

        def list_without_reviewed_status(store, friend_id, *, status=None,
                                         dimension=None):
            rows = original_list(store, friend_id, status=status,
                                 dimension=dimension)
            if status is None and saved_ids:
                return [dict(row, status="candidate")
                        if row["event_id"] in saved_ids else row
                        for row in rows]
            return rows

        monkeypatch.setattr(fh.FriendStore, "save_event", save_then_hide_review)
        monkeypatch.setattr(fh.FriendStore, "list_events",
                            list_without_reviewed_status)
    else:
        def count_save(store, event):
            save_calls.append(1)
            return original_save(store, event)

        monkeypatch.setattr(fh.FriendStore, "save_event", count_save)

    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")
    assert not at.exception
    text = _texts(at)
    assert "保存结果未通过核验" in text
    assert f"核验代码：{category}" in text
    assert "未自动重试" in text
    assert "请检查下方" in text
    assert "已确认事件：" not in text
    assert len(save_calls) == 1
    friend_id = at.session_state["friend_selected"]
    assert len(fh.FriendStore(history).list_events(friend_id)) == stored_rows


def test_verified_save_helper_classifies_idempotent_original_union(
        history, counting_client):
    """已有最终事件时只幂等调用一次，并验证第二个原始候选已并入。"""
    import app as app_module
    from parser import parse_chat
    from privacy import mask_messages
    from timeline import sort_messages

    messages = sort_messages(mask_messages(parse_chat(
        CHAT_MULTI_CANDIDATE_SAME_WINDOW, "小明.", "小安."))).messages
    candidates = bv.generate_candidates(messages)
    first = next(c for c in candidates
                 if c.behavior_type == "invitation")
    second = next(c for c in candidates
                  if c.behavior_type == "proactive_contact")
    assert first.start == second.start and first.end == second.end
    assert set(first.fingerprints) == set(second.fingerprints)

    store = fh.FriendStore(history)
    friend = store.create_friend("虚构档案", aliases=["小安"])
    event_a = bv.build_event_dict(
        candidate=first, friend_id=friend.friend_id,
        dimension=first.dimension, behavior_type=first.behavior_type,
        stance="supporting", messages=messages,
        start=first.start, end=first.end)
    saved_a = app_module._save_behavior_event_verified(
        store, event_a, first.identity)
    assert saved_a["ok"] is True
    assert saved_a["idempotent"] is False

    event_b = bv.build_event_dict(
        candidate=second, friend_id=friend.friend_id,
        dimension=first.dimension, behavior_type=first.behavior_type,
        stance="supporting", messages=messages,
        start=second.start, end=second.end)
    assert event_a["event_identity"] == event_b["event_identity"]
    saved_b = app_module._save_behavior_event_verified(
        store, event_b, second.identity)
    assert saved_b["ok"] is True
    assert saved_b["idempotent"] is True
    assert saved_b["event_id"] == saved_a["event_id"]
    rows = store.list_events(friend.friend_id)
    assert len(rows) == 1
    assert set(bv.original_identities(rows[0])) == {
        first.identity, second.identity}
    assert {first.identity, second.identity} <= bv.reviewed_identities(rows)


def test_behavior_render_helpers_accept_event_dict_and_objects(
        monkeypatch, tmp_path):
    """数据库字典与候选对象都正确显示时间和限制标记。"""
    from types import SimpleNamespace

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "runtime-data"))
    import app as app_module

    start, end = "2026-04-16 20:41", "2026-04-16 20:42"
    flags = {"time_uncertain": True, "media_unknown": True,
             "context_missing": True}
    expected_time = f"{start} ~ {end}"
    expected_flags = "时间不能完整归位；窗口含媒体（内容未知，未推测）；历史来源，无聊天正文"
    for value in (
        {"event_start_time": start, "event_end_time": end, "flags": flags},
        SimpleNamespace(event_start_time=start, event_end_time=end,
                        flags=flags),
    ):
        assert app_module._behavior_time_text(value) == expected_time
        assert app_module._behavior_flags_text(value) == expected_flags
    assert app_module._behavior_time_text({
        "event_start_time": None, "event_end_time": None}) == "（时间不明）"


def test_exclude_candidate_keeps_rejected_trace(history, counting_client):
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    before = len(counting_client)

    _preview_then_click(at, "排除这条", "behavior_note_",
                        "behavior_exclude_")

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
    _preview_then_click(at, "添加事件", "behavior_manual_notes_",
                        "behavior_manual_submit_")

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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")

    # 修改人工说明 + 主观感受
    for t in at.text_area:
        if t.key.startswith("behavior_event_notes_"):
            t.set_value("修改后的说明")
            break
    for t in at.text_area:
        if t.key.startswith("behavior_event_feeling_"):
            t.set_value("这段关系让我觉得安心")
            break
    _preview_then_click(at, "保存修改", "behavior_event_notes_",
                        "behavior_event_save_")
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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")
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
    _preview_then_click(at, "添加事件", "behavior_manual_notes_",
                        "behavior_manual_submit_")

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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")
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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")
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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")
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
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_", last=True)

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
    """改方向：类型选项对应新方向；预览后改内容立即失效；原候选消失。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)

    # 把第一条候选的方向改成「好感与关系性质」
    dim = _widgets(at, "selectbox", "behavior_dim_")[0]
    dim.set_value("romance")
    at.run()
    assert not at.exception
    # 类型下拉的选项必须立即属于新方向（不提交也生效）
    type_select = _widgets(at, "selectbox", "behavior_type_")[0]
    expected_labels = {bv.BEHAVIOR_TYPE_LABELS["romance." + key]
                       for key, _ in bv.BEHAVIOR_TYPES["romance"]}
    assert set(type_select.options) == expected_labels, (
        f"类型选项未跟随新方向：{type_select.options}")

    # 预览后再次修改方向 → 旧预览立即失效（保存按钮消失）
    _button(at, "查看最终预览").click()
    at.run()
    assert not at.exception
    _widgets(at, "selectbox", "behavior_dim_")[0].set_value("care")
    at.run()
    assert not at.exception
    assert "确认这条事件" not in [b.label for b in at.button]

    # 重新预览后保存（方向最终改回关心：类型组合合法）
    _preview_then_click(at, "确认这条事件", "behavior_note_",
                        "behavior_confirm_")
    # 数据库里绝不允许出现（方向, 行为类型）非法组合
    store, events = _events(history)
    for event in events:
        assert bv.valid_pair(event["dimension"], event["behavior_type"]), event
    # 关键回归（本 bug 修复）：修改方向后保存，原候选必须消失
    assert len(events) == 1
    assert events[0]["original_candidate_identity"]
    assert events[0]["original_candidate_identity"] != \
        events[0]["event_identity"], "方向被改过，两个身份应不同"


def test_candidate_editor_immediate_linkage(history, counting_client):
    """候选编辑器即时联动（三个交互缺陷的 AppTest 回归）：

    1. 勾选「保留一段脱敏片段」→ 编辑框**立即**出现（不提交表单）；
    2. 改行为方向 → 行为类型选项**立即**换成新方向；
    3. 「查看最终预览」是显式第二步：预览给出**最终将写入档案**的
       脱敏 + 截断内容（该 Streamlit 版本 textarea 击键不 rerun、失焦
       才提交，因此预览必须是显式一步而不能只靠 caption 自动更新）；
    4. 预览后改方向 → 旧预览立即失效，必须重新预览；
    5. 确认保存 → 落库内容 = 预览内容（脱敏后），原候选消失。
    """
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)

    # 1) 复选框即时生效
    keep = _widgets(at, "checkbox", "behavior_keep_")[0]
    assert keep.value is False
    keep.check()
    at.run()
    assert not at.exception
    snippet_tas = _widgets(at, "text_area", "behavior_snippet_")
    assert len(snippet_tas) >= 1, (
        "勾选后脱敏片段编辑框必须立即出现（st.form 内不会——已移出表单）")
    # 两阶段门控：预览未打开前不渲染「最终预览」块与保存按钮
    assert "最终预览——下面就是保存后将写入档案的内容" not in _texts(at)
    assert "确认这条事件" not in [b.label for b in at.button]

    # 2) 输入虚构 PII
    snippet_tas[0].set_value("电话 13812345678 邮箱 lin@example.com")
    at.run()
    assert not at.exception

    # 3) 显式「查看最终预览」→ 最终内容 + 脱敏预览
    scope = _scope_from_widgets(at, "behavior_snippet_")
    preview = next((b for b in at.button
                    if b.key == f"behavior_preview_btn_{scope}"), None)
    assert preview is not None, "查看最终预览按钮未出现"
    preview.click()
    at.run()
    assert not at.exception
    texts = _texts(at)
    assert "最终预览——下面就是保存后将写入档案的内容" in texts
    assert "<PHONE>" in texts and "<EMAIL>" in texts
    assert "13812345678" not in texts.replace("<PHONE>", "")

    # 4) 改方向 → 类型选项立即对应新方向（不提交）
    dim = _widgets(at, "selectbox", "behavior_dim_")[0]
    dim.set_value("respect")
    at.run()
    assert not at.exception
    type_select = _widgets(at, "selectbox", "behavior_type_")[0]
    expected = {bv.BEHAVIOR_TYPE_LABELS["respect." + key]
                for key, _ in bv.BEHAVIOR_TYPES["respect"]}
    assert set(type_select.options) == expected, (
        f"类型选项未跟随新方向：{type_select.options}")

    # 5) 改方向后旧预览过期 → 保存按钮消失 → 重新预览后才可保存
    assert "确认这条事件" not in [b.label for b in at.button], (
        "改方向后旧预览必须立即失效")
    preview2 = next((b for b in at.button
                     if b.key == f"behavior_preview_btn_{scope}"), None)
    assert preview2 is not None
    preview2.click()
    at.run()
    assert not at.exception
    target = next((b for b in at.button
                   if b.key == f"behavior_confirm_{scope}"), None)
    assert target is not None, "重新预览后保存按钮应出现"
    target.click()
    at.run()
    assert not at.exception

    store, events = _events(history)
    assert len(events) == 1
    event = events[0]
    assert bv.valid_pair(event["dimension"], event["behavior_type"])
    assert event["dimension"] == "respect"
    assert "13812345678" not in event["snippet"]
    assert "<PHONE>" in event["snippet"]
    assert "lin@example.com" not in event["snippet"]
    # 原候选身份与最终事件身份都记录在案
    assert event["original_candidate_identity"]
    assert event["original_candidate_identity"] != event["event_identity"]


def test_behavior_panel_no_false_truncation_warning(history,
                                                    counting_client):
    """正常规模不得出现「部分候选未显示」警告。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    assert "部分候选未显示" not in _texts(at)


def test_candidate_stale_preview_blocks_save(history, counting_client):
    """预览过期守卫：预览之后再改片段 → 保存按钮消失、不得写入。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)
    keep = _widgets(at, "checkbox", "behavior_keep_")[0]
    keep.check()
    at.run()
    assert not at.exception
    snippet = _widgets(at, "text_area", "behavior_snippet_")[0]
    snippet.set_value("第一版 13812345678")
    _button(at, "查看最终预览").click()
    at.run()
    assert not at.exception
    assert "最终预览" in _texts(at)
    assert "<PHONE>" in _texts(at)

    # 预览之后又修改片段（AppTest set_value + run 直接提交）
    snippet2 = _widgets(at, "text_area", "behavior_snippet_")[0]
    snippet2.set_value("第二版 13900001111")
    at.run()
    assert not at.exception
    # 过期守卫：保存按钮隐藏 + 提示重新预览
    assert "确认这条事件" not in [b.label for b in at.button]
    assert "重新点击" in _texts(at)
    store, events = _events(history)
    assert events == []


def _analysis_messages_like_app(chat: str, me: str, them: str):
    """复刻应用里 analysis_messages 的构造（parse → mask → sort）。"""
    from parser import parse_chat
    from privacy import mask_messages
    from timeline import sort_messages
    return sort_messages(mask_messages(
        parse_chat(chat, me, them))).messages


def test_behavior_backfill_panel_previews_then_applies(history,
                                                       counting_client):
    """回填入面板：先预览计数、确认后执行；只补原始身份，不动其它数据。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)                       # 建档 + 打开行为面板

    # 造一条 v2 时代的 legacy 事件：改过方向、无原始身份
    import friend_history as _fh
    store0 = _fh.FriendStore(history)
    friend = store0.list_friends()[0]
    messages = _analysis_messages_like_app(CHAT, "小明.", "小安.")
    cands = bv.generate_candidates(messages)
    cand = [c for c in cands if c.dimension == "care"
            and c.behavior_type == "care_response"][0]
    import json as _json
    conn = store0._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO behavior_events VALUES ("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e-legacy-ui", friend.friend_id, "initiative",
             "proactive_contact", "supporting", "confirmed", "rule",
             None, None, None, "full", "[0, 1]",
             _json.dumps(cand.fingerprints, ensure_ascii=False), "{}",
             "替代解释", "", "", "legacy 说明", "", "",
             "候选命中规则：difficulty_then_replay；x".replace(
                 "difficulty_then_replay", "difficulty_then_reply"),
             1.0, 1.0, None, "id-legacy-ui", ""))
        conn.execute("COMMIT")
    finally:
        conn.close()

    # 预览：可关联 1 条
    _button(at, "检查可关联的旧事件").click()
    at.run()
    assert not at.exception
    texts = _texts(at)
    assert "可关联" in texts and "1" in texts
    assert "已有关联" in texts

    # 执行：提示 + 落库
    _button(at, "执行关联（只补充原始候选身份）").click()
    at.run()
    assert not at.exception
    assert "已补充 1 条" in _texts(at)

    store2, evs = _events(history)
    stored = store2.get_event("e-legacy-ui")
    assert stored is not None
    assert stored["original_candidate_identity"] == cand.identity
    # 只补了新列：其它用户数据一个字段都没动
    assert stored["notes"] == "legacy 说明"
    assert stored["dimension"] == "initiative"
    # 原候选随之从待审核列表消失
    pending = bv.pending_candidates(bv.generate_candidates(messages),
                                    store2.list_events(friend.friend_id))
    assert not any(c.identity == cand.identity for c in pending)


# ---------------------------------------------------------------------------
# Phase 2A.1 追加：纯媒体候选过滤 + 历史线索分区
# ---------------------------------------------------------------------------

CHAT_MEDIA_UI = """小明.
2026年08月21日 21:00
今天好累，压力好大

小安.
2026年08月21日 21:05
[图片]

小明.
2026年08月21日 21:06
周末有空吗

小安.
2026年08月21日 21:07
辛苦啦，那周末再约 [动画表情]"""


def test_pure_media_candidates_are_filtered_in_ui(history,
                                                  counting_client):
    """纯媒体回应：不进入待审核队列，界面给出过滤计数。"""
    at = _fresh()
    at.text_area[0].set_value(CHAT_MEDIA_UI)
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

    _open_behavior(at)
    texts = _texts(at)
    # 过滤计数提示（不再让用户审核「内容未知」的消息）
    assert "已过滤" in texts
    assert "无效候选" in texts
    assert "纯图片" in texts
    filtered = 0
    for line in texts.splitlines():
        m = re.search(r"已过滤 (\d+) 条无效候选", line)
        if m:
            filtered = int(m.group(1))
    assert filtered >= 1, "纯媒体回应必须被计入过滤"
    # 媒体消息仍完整保留在聊天里（确认阶段的媒体计数）
    assert "媒体" in texts


def test_history_clues_shown_separately(history, counting_client):
    """历史线索单独分区展示，不进普通待审核队列。"""
    at = _fresh()
    _parse_and_analyze(at)
    _open_behavior(at)

    # 预置一条历史快照（含 show_care 的既有结果 → 产出历史线索）
    import friend_history as _fh
    store0 = _fh.FriendStore(history)
    friend = store0.list_friends()[0]
    messages = _analysis_messages_like_app(CHAT, "小明.", "小安.")
    them = [i for i, m in enumerate(messages) if m["speaker"] == "them"]
    results = [
        {"index": i, "speaker": "them", "time": messages[i].get("time"),
         "cached": True,
         "result": {"intent": {"choice": "show_care"}, "model": "m"}}
        for i in them[:2]
    ]
    store0.save_run(_fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats={}, schema_version="chat-signal-v3.3",
        request_model="jev-latest", summary_text="旧总结"))

    # 重新进入面板（模拟 rerun 后读取）
    at.segmented_control[0].set_value("概览")
    at.run()
    at.segmented_control[0].set_value("长期观察")
    at.run()
    assert not at.exception

    texts = _texts(at)
    # 历史线索分区存在，且明示「不是已核实的行为事件」
    assert "历史线索" in texts
    assert "不是" in texts and "已核实" in texts
    assert "指纹" in texts                     # 关联必须先过指纹验证
    # 普通待审核计数只包含当前导入候选（历史线索不计入）
    for line in texts.splitlines():
        if "条待核对" in line and "候选 " in line:
            count = int(line.split("候选 ")[1].split(" 条待核对")[0])
            assert count < 10, count
            break
    # 历史快照的来源 run 展示出来
    labels = " ".join(str(e.label) for e in at.expander)
    assert "历史快照" in labels
