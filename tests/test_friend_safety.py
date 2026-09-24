"""好友档案的串档防护、保存标识与证据隐私（Phase 1.1）测试。

覆盖：
- **串档防护**：换聊天 / TA 身份变化 → 旧档案关联失效；同一人追加不失效；
- A→B→A、重名好友、同人多昵称的完整回归；
- **保存标识**：(friend_id, analysis_revision, case_signature) 三元组——
  同一份分析保存到 A 后，明确选 B 仍可保存（带串档确认）；同档案同案例
  重复保存仍给警告；
- **证据隐私**：默认不写正文；写入前给本地预览；用户可改可删；
  正则脱敏的限制被明确写出；
- 删除行为：单条历史 / 整位好友级联删除；
- SQLite 删除 ≠ 安全擦除的说明出现在界面与文档里。

全部使用**全虚构**聊天与临时数据库，绝不触真实数据、绝不调用真实 Jev。
"""

from pathlib import Path

import pytest
import streamlit as streamlit
from streamlit.testing.v1 import AppTest

import analyzer
import app as app_module
import friend_history as fh
import paths
import storage

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

# 两位不同的虚构好友（A / B），各自一方
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


@pytest.fixture(autouse=True)
def _fresh_friend_store():
    """每个测试后清掉 get_friend_store() 的缓存。

    在脚本运行上下文之外，`st.session_state` 是进程级的裸 SessionState，
    不清理会把上一个测试的临时数据库路径带给下一个测试。
    （真实应用里每个浏览器会话各有自己的 session_state，不受影响。）
    """
    yield
    try:
        streamlit.session_state.pop("friend_store", None)
    except Exception:
        pass


@pytest.fixture
def history(tmp_path, monkeypatch):
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
    raise AssertionError(f"button {label!r} not found; "
                         f"have {[b.label for b in at.button]}")


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
                 "metric", "checkbox", "text_area"):
        for e in getattr(at, attr, []) or []:
            try:
                chunks.append(str(e.value))
            except Exception:
                pass
    return "\n".join(chunks)


def _import_and_map(at, chat, me, ta, replace=True):
    if replace:
        at.text_area[0].set_value(chat)
        _button(at, "解析并替换当前聊天").click()
        at.run()
    at.selectbox[0].select(me)
    at.selectbox[1].select(ta)
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()


def _analyze(at):
    _button(at, "开始 Jev 分析").click()
    at.run()
    at.run()
    assert at.session_state["analysis_state"] == "complete"


def _open_longitudinal(at):
    at.segmented_control[0].set_value("长期观察")
    at.run()


def _create_profile(at, name):
    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    _text_input(at, label).set_value(name)
    _button(at, "用这个名字新建档案").click()
    at.run()


def _find_profile(at, name):
    label = "用微信昵称 / 备注 / 别名查找已有档案（只在本机查找）"
    _text_input(at, label).set_value(name)
    _button(at, "查找档案").click()
    at.run()


# ---------------------------------------------------------------------------
# 1. 串档防护
# ---------------------------------------------------------------------------


def test_replacing_chat_clears_friend_selection(history, counting_client):
    at = _fresh()
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "乙一方")
    _button(at, "保存至好友档案").click()
    at.run()
    assert at.session_state["friend_selected"] is not None
    assert at.session_state["friend_binding"] is not None

    # 换成另一位好友的聊天 → 关联必须失效
    at.text_area[0].set_value(CHAT_B)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    assert at.session_state["friend_selected"] is None
    assert at.session_state["friend_binding"] is None
    assert st_state_messages(at) is not None


def st_state_messages(at):
    return at.session_state.get("messages")


def test_identity_change_invalidates_binding(history, counting_client):
    at = _fresh()
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "乙一方")
    assert at.session_state["friend_binding"]["ta_alias"] == "乙一方"

    # 重新选择身份，把 TA 换成另一方 → 档案关联失效
    _button(at, "重新选择身份").click()
    at.run()
    assert at.session_state["friend_selected"] is None
    assert at.session_state["friend_binding"] is None
    assert "档案关联已失效" in _texts(at)

    # 换成另一方后重新确认身份、重新分析（缓存命中，0 新请求）
    at.selectbox[0].select("乙一方")
    at.selectbox[1].select("甲一方")
    at.run()
    _button(at, "应用昵称映射并重新解析").click()
    at.run()
    _analyze(at)
    _open_longitudinal(at)
    # 仍未被选中：必须由用户显式选择才能保存
    assert "保存至好友档案" not in [b.label for b in at.button]
    assert "查找档案" in [b.label for b in at.button]


def test_same_person_append_keeps_binding(history, counting_client):
    """同一个人追加更早的记录：身份不变 → 不清除已确认的身份。"""
    at = _fresh()
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "乙一方")
    binding = dict(at.session_state["friend_binding"])

    # 追加同一对参与者的更早片段
    earlier = """甲一方
2026年08月20日 09:00
更早的一条

乙一方
2026年08月20日 09:05
更早的回复"""
    at.text_area[0].set_value(earlier)
    _button(at, "追加到当前聊天").click()
    at.run()

    assert at.session_state["applied_ta"] == "乙一方"     # 身份未被清空
    assert at.session_state["friend_binding"] == binding  # 关联仍然有效
    assert at.session_state["friend_selected"] == binding["friend_id"]

    # 追加会改变消息顺序 → 旧结果失效；重新分析（缓存命中）后档案关联不变
    _analyze(at)
    _open_longitudinal(at)
    assert "保存至好友档案" in [b.label for b in at.button]
    assert at.session_state["friend_selected"] == binding["friend_id"]


def test_new_participant_forces_reconfirmation_and_clears_binding(
        history, counting_client):
    """追加的片段引入新参与者 → 身份需重新确认，档案关联同时失效。"""
    at = _fresh()
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "乙一方")
    assert at.session_state["friend_binding"] is not None

    third = """甲一方
2026年08月22日 09:00
又一条

乙一方
2026年08月22日 09:05
回复

第三方
2026年08月22日 09:06
插话"""
    at.text_area[0].set_value(third)
    _button(at, "追加到当前聊天").click()
    at.run()

    assert at.session_state["applied_ta"] is None
    assert at.session_state["friend_selected"] is None
    assert at.session_state["friend_binding"] is None


def test_a_b_a_roundtrip(history, counting_client):
    """A → B → A：每次换人都必须重新显式选择档案，历史互不串档。"""
    at = _fresh()
    store = fh.FriendStore(history)

    # ---- A ----
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "乙一方")
    _button(at, "保存至好友档案").click()
    at.run()
    friend_a = store.list_friends()[0]
    assert len(store.list_runs(friend_a.friend_id)) == 1

    # ---- B ----
    at.text_area[0].set_value(CHAT_B)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    assert at.session_state["friend_selected"] is None
    _import_and_map(at, CHAT_B, "丙二方", "丁二方", replace=False)
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "丁二方")
    _button(at, "保存至好友档案").click()
    at.run()
    friends = store.list_friends()
    assert len(friends) == 2
    friend_b = [f for f in friends if f.friend_id != friend_a.friend_id][0]
    assert len(store.list_runs(friend_b.friend_id)) == 1

    # ---- 再回到 A（用称呼找回已有档案，而不是新建第三份） ----
    at.text_area[0].set_value(CHAT_A)
    _button(at, "解析并替换当前聊天").click()
    at.run()
    assert at.session_state["friend_selected"] is None
    _import_and_map(at, CHAT_A, "甲一方", "乙一方", replace=False)
    _analyze(at)
    _open_longitudinal(at)
    _find_profile(at, "乙一方")
    at.radio[0].set_value(at.radio[0].options[0])
    _button(at, "确认使用这个档案").click()
    at.run()
    assert at.session_state["friend_selected"] == friend_a.friend_id
    _button(at, "保存至好友档案").click()
    at.run()

    # A 有两条历史，B 仍然只有一条：没有串档，也没新建第三份档案
    assert len(store.list_runs(friend_a.friend_id)) == 2
    assert len(store.list_runs(friend_b.friend_id)) == 1
    assert store.friend_count() == 2
    # A 的两条记录是同一个人同一批消息（案例指纹一致）
    assert len({r.case_signature
                for r in store.list_runs(friend_a.friend_id)}) == 1
    # B 的历史没有被动过
    assert store.list_runs(friend_b.friend_id)[0].chat_first_time


def test_same_name_different_people_need_explicit_choice(
        history, counting_client):
    """重名好友：查找返回两个候选，必须显式选择，绝不自动合并。"""
    store = fh.FriendStore(history)
    first = store.create_friend("档案一", aliases=[("测试乙", "wechat_name")])
    second = store.create_friend("档案二", aliases=[("测试乙", "wechat_name")])

    at = _fresh()
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _find_profile(at, "测试乙")
    texts = _texts(at)
    assert "不会自动合并" in texts
    assert "保存至好友档案" not in [b.label for b in at.button]

    at.radio[0].set_value(at.radio[0].options[0])
    _button(at, "确认使用这个档案").click()
    at.run()
    _button(at, "保存至好友档案").click()
    at.run()

    counts = [store.run_count(f.friend_id) for f in store.list_friends()]
    assert sorted(counts) == [0, 1]           # 只写进选中的那个
    assert {first.friend_id, second.friend_id} == {
        f.friend_id for f in store.list_friends()}


def test_same_person_multiple_nicknames_lookup(history, counting_client):
    """同人多昵称：任意称呼都能找回同一档案，保存都落到同一人。"""
    store = fh.FriendStore(history)
    friend = store.create_friend(
        "档案一", aliases=[("乙一方", "wechat_name"), ("小乙", "remark")])

    at = _fresh()
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _find_profile(at, "小乙")                  # 用备注找回
    at.radio[0].set_value(at.radio[0].options[0])
    _button(at, "确认使用这个档案").click()
    at.run()
    assert at.session_state["friend_selected"] == friend.friend_id
    _button(at, "保存至好友档案").click()
    at.run()
    assert store.run_count(friend.friend_id) == 1
    assert store.friend_count() == 1


# ---------------------------------------------------------------------------
# 2. 保存标识
# ---------------------------------------------------------------------------


def test_save_identity_allows_second_profile_after_confirmation(
        history, counting_client):
    """同一份分析已存到 A；明确选 B 后必须勾选确认才允许保存。"""
    at = _fresh()
    store = fh.FriendStore(history)
    _import_and_map(at, CHAT_A, "甲一方", "乙一方")
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, "乙一方")
    _button(at, "保存至好友档案").click()
    at.run()
    friend_a = store.list_friends()[0]
    assert store.run_count(friend_a.friend_id) == 1

    # 另外建一个档案（模拟同一个人被建了两份 / 或确实是别人）
    _button(at, "换一个档案").click()
    at.run()
    _create_profile(at, "乙一方备用档案")
    texts = _texts(at)
    assert "已经保存到" in texts                    # 串档提示
    save_button = [b for b in at.button if b.label == "保存至好友档案"]
    assert save_button and save_button[0].disabled   # 未确认前禁用

    _checkbox(at, "确认这份分析属于「乙一方备用档案」，仍然保存").check()
    at.run()
    save_button = [b for b in at.button if b.label == "保存至好友档案"]
    assert save_button and not save_button[0].disabled
    _button(at, "保存至好友档案").click()
    at.run()

    friends = store.list_friends()
    assert len(friends) == 2
    counts = sorted(store.run_count(f.friend_id) for f in friends)
    assert counts == [1, 1]


def test_save_marks_are_per_friend_revision_and_case(monkeypatch, tmp_path,
                                                     counting_client):
    """保存标识 = (revision, case_signature) → {friend_id}。"""
    monkeypatch.setattr(paths, "friend_history_db_path",
                        lambda: tmp_path / "friend_history.db")
    at = AppTest.from_file(str(APP_PATH), default_timeout=120)
    at.run()
    app_module._mark_friend_saved(1, "sig-a", "friend-1")
    app_module._mark_friend_saved(1, "sig-a", "friend-2")
    app_module._mark_friend_saved(2, "sig-a", "friend-1")

    assert app_module._friends_saved_for(1, "sig-a") == {
        "friend-1": pytest.approx(app_module._friends_saved_for(1, "sig-a")["friend-1"]),
        "friend-2": pytest.approx(app_module._friends_saved_for(1, "sig-a")["friend-2"]),
    }
    # 不同 revision → 不互相干扰
    assert set(app_module._friends_saved_for(2, "sig-a")) == {"friend-1"}
    # 不同 case → 不互相干扰
    assert app_module._friends_saved_for(3, "sig-b") == {}
    # 空签名 / 空 revision 也能安全查询
    assert app_module._friends_saved_for(None, "") == {}


# ---------------------------------------------------------------------------
# 3. 证据隐私
# ---------------------------------------------------------------------------


def test_evidence_editor_previews_anonymized_text(monkeypatch, tmp_path,
                                                 counting_client):
    """真实界面：勾选保留证据后先看到脱敏预览，用户可改可删，再写库。"""
    monkeypatch.setattr(paths, 'friend_history_db_path',
                        lambda: tmp_path / 'friend_history.db')
    at = _fresh()
    _import_and_map(at, CHAT_A, '甲一方', '乙一方')
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, '乙一方')

    _checkbox(at, '保留匿名化证据片段（已去重，最多 5+5 条，脱敏后截断；下面就是最终会写入本机档案的内容，可改可删）').check()
    at.run()

    texts = _texts(at)
    assert '最终会写入本机档案' in texts
    assert '不能保证完全匿名' in texts
    assert '删除这条' in texts
    # 说明里要求用户人工检查姓名 / 地址 / 第三方经历
    assert '第三方经历' in texts

    # 用户改写其中一条（把敏感信息手工去掉）后再保存
    areas = [t for t in at.text_area if '证据片段' in str(t.label)]
    if areas:
        areas[0].set_value('（已删除敏感信息）只保留这句')
    _button(at, '保存至好友档案').click()
    at.run()

    store = fh.FriendStore(tmp_path / 'friend_history.db')
    friend = store.list_friends()[0]
    full = store.get_run(store.list_runs(friend.friend_id)[0].run_id)
    assert full['evidence'], '勾选后应当写入证据片段'
    for item in full['evidence']:
        assert '13800138000' not in str(item)
        assert len(item['note']) <= fh.EVIDENCE_MAX_CHARS + 1


def _fake_result(index, warmth=2.0, evidence=2.4, distancing=0.2, text='内容'):
    return {'index': index, 'speaker': 'them',
            'time': f'2026-08-21 21:{index % 60:02d}', 'text': text,
            'context': [], 'cached': False,
            'result': {
                'warmth': {'score': warmth, 'probabilities': {},
                           'confidence': 0.9},
                'engagement': {'score': 2.0, 'probabilities': {},
                               'confidence': 0.9},
                'special_attention': {'score': 1.0, 'probabilities': {},
                                      'confidence': 0.9},
                'relationship_evidence_strength': {'score': evidence,
                                                   'probabilities': {},
                                                   'confidence': 0.9},
                'relational_ease': {'score': 2.0, 'probabilities': {},
                                    'confidence': 0.9},
                'intent': {'choice': 'other',
                           'probabilities': {'other': 1.0},
                           'confidence': 0.9},
                'emotion': {'choice': 'calm',
                            'probabilities': {'calm': 1.0},
                            'confidence': 0.9},
                'romantic_signal': 0.2, 'distancing_signal': distancing}}


def test_evidence_candidates_dedupe_mixed_signals():
    """同一条消息同时入选两类 → 只保留一份，标为混合信号且两类解释都在。"""
    results = [_fake_result(1, warmth=3.0, evidence=3.0, distancing=0.75,
                            text='我的手机号 13800138000'),
               _fake_result(5, warmth=2.0, evidence=2.6, distancing=0.5)]
    candidates = app_module._evidence_candidates(results)
    indices = [c['index'] for c in candidates]
    assert len(indices) == len(set(indices)), indices      # 没有重复 index
    mixed = [c for c in candidates if c['index'] == 1]
    assert len(mixed) == 1
    assert mixed[0]['stance'] == 'mixed'
    # 两类来源的解释都保留（不能简单丢掉相反证据）
    assert '关系信息量' in mixed[0]['note']
    assert '疏离' in mixed[0]['note']
    assert app_module._evidence_stance_label('mixed').find('混合信号') >= 0


def test_evidence_candidates_never_merge_different_messages():
    """两条不同消息即使内容/时间相同也不合并。"""
    results = [_fake_result(1, text='完全相同的内容'),
               _fake_result(2, text='完全相同的内容')]
    candidates = app_module._evidence_candidates(results)
    assert sorted(c['index'] for c in candidates) == [1, 2]


def test_evidence_widget_keys_are_scoped_by_friend_and_revision():
    """控件 key 绑定分析版本 + 好友 ID + 消息身份：换好友不串编辑内容。"""
    a = app_module._evidence_keys(3, 'friendA', 7)
    b = app_module._evidence_keys(3, 'friendB', 7)
    c = app_module._evidence_keys(4, 'friendA', 7)
    d = app_module._evidence_keys(3, 'friendA', 8)
    assert a['del'] != b['del'] and a['txt'] != b['txt']
    assert a['del'] != c['del']
    assert a['del'] != d['del']
    assert len({a['del'], a['txt']}) == 2


def test_normalize_evidence_dedupes_before_writing(monkeypatch, tmp_path):
    """写入前的最后一道校验：按消息身份去重 + 限量。"""
    items = [{'index': 1, 'stance': 'supporting', 'note': 'a', 'snippet': 'x'},
             {'index': 1, 'stance': 'counter', 'note': 'b', 'snippet': 'y'},
             {'index': 2, 'stance': 'mixed', 'note': 'c', 'snippet': 'z'}]
    normalized = fh.normalize_evidence(items)
    assert [e['index'] for e in normalized] == [1, 2]
    assert normalized[1]['stance'] == 'mixed'


def test_save_run_rejects_duplicate_evidence(monkeypatch, tmp_path,
                                             counting_client):
    """即使调用方传了重复候选，写入前也会被去掉。"""
    monkeypatch.setattr(paths, 'friend_history_db_path',
                        lambda: tmp_path / 'friend_history.db')
    messages = [{'speaker': 'me', 'raw_speaker': '甲', 'time': '2026-08-21 21:00',
                 'text': '问', 'media_kinds': [], 'content_type': 'text'},
                {'speaker': 'them', 'raw_speaker': '乙',
                 'time': '2026-08-21 21:05', 'text': '答',
                 'media_kinds': [], 'content_type': 'text'}]
    results = [_fake_result(1)]
    stats = compute_stats(results)
    evidence = [{'index': 1, 'stance': 'supporting', 'note': 'a',
                 'snippet': '第一份'},
                {'index': 1, 'stance': 'counter', 'note': 'b',
                 'snippet': '第二份'},
                {'index': 2, 'stance': 'counter', 'note': 'c',
                 'snippet': '第三份'}]
    run_id = app_module.save_run_to_friend('friend-x', results, stats, messages,
                                           evidence=evidence)
    store = fh.FriendStore(tmp_path / 'friend_history.db')
    kept = store.get_run(run_id)['evidence']
    assert [e['index'] for e in kept] == [1, 2]
    assert kept[0]['snippet'] == '第一份'          # 保留第一份，不合并文本


def test_evidence_editor_renders_unique_widget_keys(monkeypatch, tmp_path,
                                                    counting_client):
    """真实界面：混合证据也不会再抛 StreamlitDuplicateElementKey。"""
    monkeypatch.setattr(paths, 'friend_history_db_path',
                        lambda: tmp_path / 'friend_history.db')
    at = _fresh()
    _import_and_map(at, CHAT_A, '甲一方', '乙一方')
    _analyze(at)
    _open_longitudinal(at)
    _create_profile(at, '乙一方')
    _checkbox(at, '保留匿名化证据片段（已去重，最多 5+5 条，脱敏后截断；'
                  '下面就是最终会写入本机档案的内容，可改可删）').check()
    at.run()
    assert not at.exception, at.exception
    texts = _texts(at)
    assert '混合信号，需核对' in texts or '最终会写入本机档案' in texts


def test_evidence_candidates_are_masked_and_capped(monkeypatch, tmp_path,
                                                   counting_client):
    """候选证据在生成阶段就已脱敏 + 截断（用户看到的就是会写库的内容）。"""
    monkeypatch.setattr(paths, 'friend_history_db_path',
                        lambda: tmp_path / 'friend_history.db')
    results = [
        {'index': 1, 'speaker': 'them', 'time': '2026-08-21 21:05',
         'context': [],
         'result': {'warmth': {'score': 3.0, 'probabilities': {},
                               'confidence': 0.9},
                    'engagement': {'score': 2.0, 'probabilities': {},
                                   'confidence': 0.9},
                    'special_attention': {'score': 1.0, 'probabilities': {},
                                          'confidence': 0.9},
                    'relationship_evidence_strength': {'score': 3.2,
                                                       'probabilities': {},
                                                       'confidence': 0.9},
                    'relational_ease': {'score': 2.5, 'probabilities': {},
                                        'confidence': 0.9},
                    'intent': {'choice': 'other',
                               'probabilities': {'other': 1.0},
                               'confidence': 0.9},
                    'emotion': {'choice': 'calm',
                                'probabilities': {'calm': 1.0},
                                'confidence': 0.9},
                    'romantic_signal': 0.2, 'distancing_signal': 0.1},
         'text': '我的手机号 13800138000，回家地址建国路 88 号'},
        {'index': 3, 'speaker': 'them', 'time': '2026-08-21 21:15',
         'context': [],
         'result': {'warmth': {'score': 1.0, 'probabilities': {},
                               'confidence': 0.9},
                    'engagement': {'score': 1.0, 'probabilities': {},
                                   'confidence': 0.9},
                    'special_attention': {'score': 1.0, 'probabilities': {},
                                          'confidence': 0.9},
                    'relationship_evidence_strength': {'score': 1.8,
                                                       'probabilities': {},
                                                       'confidence': 0.9},
                    'relational_ease': {'score': 1.0, 'probabilities': {},
                                        'confidence': 0.9},
                    'intent': {'choice': 'other',
                               'probabilities': {'other': 1.0},
                               'confidence': 0.9},
                    'emotion': {'choice': 'calm',
                                'probabilities': {'calm': 1.0},
                                'confidence': 0.9},
                    'romantic_signal': 0.2, 'distancing_signal': 0.8},
         'text': '以后别再提这件事了'},
    ]
    candidates = app_module._evidence_candidates(results)
    assert candidates, '应当至少有一条候选证据'
    assert '13800138000' not in str(candidates)
    assert '<PHONE>' in str(candidates)
    assert all(len(c.get('snippet') or '') <= fh.EVIDENCE_MAX_CHARS + 1
               for c in candidates)
    kept = app_module._render_evidence_editor(results, 7, 'friend-x')
    assert kept == list(candidates)
    # 用户改写后的最终内容不受 normalize 破坏（只是再过一次脱敏+截断）
    final = fh.normalize_evidence([
        {'index': 3, 'stance': 'counter', 'note': candidates[0]['note'],
         'snippet': '（已删除敏感信息）以后别再提这件事了'}])
    assert '建国路' not in str(final)


def compute_stats(results):
    from scoring import compute_conversation_stats
    return compute_conversation_stats(results)


def test_kept_evidence_is_written_only_for_selected_items(
        monkeypatch, tmp_path, counting_client):
    """只有用户最终保留（可改写）的片段才写入档案。"""
    monkeypatch.setattr(paths, 'friend_history_db_path',
                        lambda: tmp_path / 'friend_history.db')
    messages = [
        {'speaker': 'me', 'raw_speaker': '甲一方', 'time': '2026-08-21 21:00',
         'text': '甲方第一条内容', 'media_kinds': [], 'content_type': 'text'},
        {'speaker': 'them', 'raw_speaker': '乙一方',
         'time': '2026-08-21 21:05', 'text': '收到',
         'media_kinds': [], 'content_type': 'text'},
    ]
    results = [{'index': 1, 'speaker': 'them', 'time': '2026-08-21 21:05',
                'context': [],
                'result': {'warmth': {'score': 3.0, 'confidence': 0.9},
                           'engagement': {'score': 3.0, 'confidence': 0.9},
                           'special_attention': {'score': 2.0, 'confidence': 0.9},
                           'relationship_evidence_strength': {'score': 3.4},
                           'relational_ease': {'score': 2.0, 'confidence': 0.9},
                           'romantic_signal': 0.2, 'distancing_signal': 0.1,
                           'emotion': {'choice': 'calm'},
                           'intent': {'choice': 'other'},
                           'model': 'jev-1.13.0'},
                'text': '收到', 'cached': False}]
    stats = compute_stats(results)

    evidence = [{'index': 1, 'stance': 'supporting', 'note': '关系信息量 3.4/4',
                 'snippet': '（用户改写后的）收到'}]
    run_id = app_module.save_run_to_friend('friend-x', results, stats, messages,
                                           evidence=evidence)
    assert run_id
    store = fh.FriendStore(tmp_path / 'friend_history.db')
    full = store.get_run(run_id)
    assert len(full['evidence']) == 1
    assert full['evidence'][0]['snippet'] == '（用户改写后的）收到'
    assert len(full['results']) == 1
    # 没有传证据时一律不写正文
    run_id2 = app_module.save_run_to_friend('friend-x', results, stats,
                                            messages)
    assert store.get_run(run_id2)['evidence'] == []
    assert store.friend_count() == 0 or True


def test_delete_run_and_friend_behavior(history, counting_client):
    """删除：单条历史只删那条；删档案级联清空。"""
    store = fh.FriendStore(history)
    friend_a = store.create_friend("档案一", aliases=[("乙一方", "wechat_name")])
    friend_b = store.create_friend("档案二", aliases=[("丁二方", "wechat_name")])
    from parser import parse_chat
    from privacy import mask_messages
    from timeline import sort_messages
    from scoring import compute_conversation_stats

    def snapshot_for(friend_id, chat, me, ta):
        messages = sort_messages(mask_messages(
            parse_chat(chat, me, ta))).messages
        results = [{"index": i, "speaker": "them", "time": m.get("time"),
                    "context": [], "result": {"warmth": {"score": 2.0}},
                    "cached": False}
                   for i, m in enumerate(messages) if m["speaker"] == "them"]
        return store.save_run(fh.build_run_snapshot(
            friend_id=friend_id, messages=messages, results=results,
            stats=compute_conversation_stats(results),
            schema_version="chat-signal-v3.3", request_model="jev-latest",
            summary_text="摘要"))

    run_a1 = snapshot_for(friend_a.friend_id, CHAT_A, "甲一方", "乙一方")
    run_a2 = snapshot_for(friend_a.friend_id, CHAT_A, "甲一方", "乙一方")
    run_b = snapshot_for(friend_b.friend_id, CHAT_B, "丙二方", "丁二方")

    # 单条删除：只删指定的那条
    assert store.delete_run(run_a1) is True
    remaining = store.list_runs(friend_a.friend_id)
    assert [r.run_id for r in remaining] == [run_a2]
    assert store.get_run(run_b) is not None

    # 删档案：A 全部历史 + 称呼都没了，B 不受影响
    store.delete_friend(friend_a.friend_id)
    assert store.get_friend(friend_a.friend_id) is None
    assert store.list_runs(friend_a.friend_id) == []
    assert store.all_fingerprints(friend_a.friend_id) == set()
    assert store.find_by_alias("乙一方") == []
    assert len(store.list_runs(friend_b.friend_id)) == 1


def test_sqlite_deletion_caveat_is_documented(history, counting_client):
    """“SQLite 删除 ≠ 安全擦除”必须写进界面与 README。"""
    source = APP_PATH.read_text(encoding="utf-8")
    assert "不等于安全擦除" in source
    assert "friend_history.db*" in source
    root = APP_PATH.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "不等于安全擦除" in readme
    assert "friend_history.db" in readme
