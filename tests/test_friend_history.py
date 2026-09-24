"""好友档案与历史分析记忆（Longitudinal Phase 1：P1–P4）测试。

覆盖验收项：
- 同人多昵称：多个称呼都指向同一个 friend_id；
- 不同人同昵称：命中多个档案时**绝不自动合并**，必须用户选择；
- 倒序追加历史：按真实聊天时间归位，而不是按保存时间；
- 重复分析：同一批消息 → 案例指纹一致 → 识别为重复；
- 跨期记录：两次不同时间段的分析都能列出并各自归位；
- 未来信息隔离：晚于目标的历史证据不得进入更早目标的上下文；
- schema / 模型版本不同：不得直接横向比较；
- 档案删除：级联清空，且不影响其它档案；
- 存储隔离：friend_history 与 analysis_cache 是两个独立文件；
- 隐私：档案里不存聊天正文、不存昵称，证据片段必须脱敏截断。

全部使用**全虚构**聊天与临时数据库，绝不触真实数据、绝不调用 Jev。
"""

import json
import time

import pytest

import friend_history as fh
import longitudinal as lg
import storage
from scoring import compute_conversation_stats

SCHEMA = "chat-signal-v3.3"


@pytest.fixture
def store(tmp_path):
    return fh.FriendStore(tmp_path / "friend_history.db")


def msg(index, speaker, when, text):
    return {"index": index, "speaker": speaker,
            "raw_speaker": f"说话人{index}", "time": when,
            "text": text, "content_type": "text", "media_kinds": []}


def answer(warmth=2.0, engagement=2.0, evidence=2.4, distancing=0.2,
           romantic=0.2, model="jev-1.13.0", confidence=0.9):
    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": confidence},
        "intent": {"choice": "other", "probabilities": {"other": 1.0},
                   "confidence": confidence},
        "warmth": {"score": warmth, "probabilities": {}, "confidence": confidence},
        "engagement": {"score": engagement, "probabilities": {},
                       "confidence": confidence},
        "special_attention": {"score": 1.0, "probabilities": {},
                              "confidence": confidence},
        "relationship_evidence_strength": {"score": evidence, "probabilities": {},
                                           "confidence": confidence},
        "relational_ease": {"score": 2.0, "probabilities": {},
                            "confidence": confidence},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
        "model": model,
    }


def result(index, when, **kw):
    return {"index": index, "speaker": "them", "time": when,
            "context": [], "result": answer(**kw), "cached": False}


def snapshot(store, friend_id, messages, results, *, schema=SCHEMA,
             model="jev-latest", summary="摘要", skipped=0, started=None):
    return store.save_run(fh.build_run_snapshot(
        friend_id=friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results), skipped_media=skipped,
        analysis_started_at=started if started is not None else time.time(),
        analysis_completed_at=time.time(), schema_version=schema,
        request_model=model, summary_text=summary))


# ---------------------------------------------------------------------------
# P1：好友档案
# ---------------------------------------------------------------------------


def test_create_friend_and_alias_lookup(store):
    friend = store.create_friend("档案一", aliases=[("测试甲.", "wechat_name")])
    assert friend.friend_id and len(friend.friend_id) >= 12
    hits = store.find_by_alias("测试甲.")
    assert len(hits) == 1 and hits[0].friend_id == friend.friend_id
    assert hits[0].display == "测试甲."
    assert hits[0].kind == "wechat_name"


def test_alias_normalization_is_for_lookup_only(store):
    """大小写/多余空白不影响查找，但用户原始写法被保留。"""
    friend = store.create_friend("档案一", aliases=[("  测试  甲. ", "wechat_name")])
    for probe in ("测试 甲.", "  测试  甲. ", "测试\t甲."):
        hits = store.find_by_alias(probe)
        assert len(hits) == 1, probe
        assert hits[0].friend_id == friend.friend_id
        assert hits[0].display == "  测试  甲. "     # 原文保留
    # 内部空白的**有无**仍然区分不同昵称：不去猜谁是谁
    assert store.find_by_alias("测试甲.") == []


def test_same_person_multiple_aliases(store):
    """同人多昵称：微信昵称 + 备注 + 自定义别名都指向同一档案。"""
    friend = store.create_friend("档案一", aliases=[("测试甲.", "wechat_name")])
    assert store.add_alias(friend.friend_id, "甲老板", "remark")
    assert store.add_alias(friend.friend_id, "老甲", "alias")
    for probe in ("测试甲.", "甲老板", "老甲"):
        hits = store.find_by_alias(probe)
        assert len(hits) == 1, probe
        assert hits[0].friend_id == friend.friend_id
    # 重复追加同一个称呼不会产生重复行
    assert store.add_alias(friend.friend_id, "甲老板", "remark") is False
    assert len(store.aliases_of(friend.friend_id)) == 3


def test_different_people_same_alias_never_auto_merge(store):
    """不同人同昵称：查找返回多个候选，绝不合并且必须用户选择。"""
    a = store.create_friend("档案一", aliases=[("测试甲.", "wechat_name")])
    b = store.create_friend("档案二", aliases=[("测试甲.", "wechat_name")])
    hits = store.find_by_alias("测试甲.")
    assert len(hits) == 2
    assert {h.friend_id for h in hits} == {a.friend_id, b.friend_id}
    # 两个档案互为独立：各自的 run_count 不共享
    msgs = [msg(0, "me", "2026-08-01 09:00", "在吗"),
            msg(1, "them", "2026-08-01 09:05", "在的")]
    snapshot(store, a.friend_id, msgs, [result(1, "2026-08-01 09:05")])
    hits = store.find_by_alias("测试甲.")
    counts = sorted(h.run_count for h in hits)
    assert counts == [0, 1]


def test_empty_alias_is_ignored(store):
    assert store.find_by_alias("") == []
    assert store.find_by_alias("   ") == []
    assert store.create_friend("x", aliases=["", "  "]).friend_id


def test_friend_crud(store):
    a = store.create_friend("档案一")
    b = store.create_friend("档案二")
    assert store.friend_count() == 2
    renamed = store.update_friend(a.friend_id, display_name="新名字")
    assert renamed.display_name == "新名字"
    assert store.update_friend("不存在", display_name="x") is None
    assert store.get_friend("不存在") is None
    assert store.delete_friend(b.friend_id) is True
    assert store.delete_friend(b.friend_id) is False
    assert store.friend_count() == 1


# ---------------------------------------------------------------------------
# P2：完整快照
# ---------------------------------------------------------------------------


def test_snapshot_has_full_provenance(store):
    friend = store.create_friend("档案", aliases=[("测试甲.", "wechat_name")])
    messages = [
        msg(0, "me", "2026-08-01 09:00", "在吗"),
        msg(1, "them", "2026-08-01 09:05", "在的"),
        msg(2, "me", "2026-08-02 10:00", "晚安"),
        msg(3, "them", "2026-08-02 10:05", "晚安啦"),
    ]
    results = [result(1, "2026-08-01 09:05"), result(3, "2026-08-02 10:05")]
    started = time.time() - 120
    run_id = snapshot(store, friend.friend_id, messages, results,
                      skipped=2, started=started)

    full = store.get_run(run_id)
    assert full is not None
    # 分析时间与聊天时间是分开的字段
    assert full["analysis_started_at"] == started
    assert full["analysis_completed_at"] >= started
    assert full["chat_first_time"] == "2026-08-01 09:00"
    assert full["chat_last_time"] == "2026-08-02 10:05"
    assert full["full_time_ratio"] == 1.0
    assert full["message_count"] == 4
    assert full["analyzed_count"] == 2
    assert full["failed_count"] == 0
    assert full["skipped_media_count"] == 2
    assert full["schema_version"] == SCHEMA
    assert full["request_model"] == "jev-latest"
    assert full["response_model"] == "jev-1.13.0"   # 实际返回版本
    assert len(full["case_signature"]) == 64
    # 九项结果与统计都在（不能只存总分）
    assert len(full["results"]) == 2
    assert set(full["results"][0]["result"]) == {
        "emotion", "intent", "warmth", "engagement", "special_attention",
        "relationship_evidence_strength", "relational_ease",
        "romantic_signal", "distancing_signal", "model"}
    assert full["stats"]["analyzed"] == 2
    assert "overall" in full["stats"]
    assert full["summary_text"] == "摘要"
    assert len(full["messages"]) == 4
    assert sum(1 for m in full["messages"] if m["is_target"]) == 2


def test_snapshot_stores_no_chat_text_or_nickname(store):
    """档案里不能出现聊天正文或原始昵称（只留指纹/时间/角色）。"""
    friend = store.create_friend("档案", aliases=[("测试甲.", "wechat_name")])
    messages = [msg(0, "me", "2026-08-01 09:00", "一句很私密的话"),
                msg(1, "them", "2026-08-01 09:05", "另一句私密回复")]
    results = [result(1, "2026-08-01 09:05")]
    run_id = snapshot(store, friend.friend_id, messages, results)
    blob = "".join(str(v) for v in store.get_run(run_id).values())
    assert "一句很私密的话" not in blob
    assert "另一句私密回复" not in blob
    assert "说话人1" not in blob and "说话人0" not in blob
    # 角色只有规范化身份
    assert {m["speaker"] for m in store.get_run(run_id)["messages"]} <= {"me", "them"}


def test_evidence_is_opt_in_anonymized_and_capped(store):
    friend = store.create_friend("档案")
    messages = [msg(0, "me", "2026-08-01 09:00", "在吗"),
                msg(1, "them", "2026-08-01 09:05",
                    "我的手机号是 13800138000，邮箱 a@b.com，"
                    + "很长的一段话" * 40)]
    results = [result(1, "2026-08-01 09:05")]
    evidence = fh.normalize_evidence([
        {"index": 1, "stance": "supporting", "note": messages[1]["text"]},
        {"index": 1, "stance": "bogus_stance", "note": "另一条"},
        {"index": "坏下标", "stance": "supporting", "note": "无效"},
        {"index": 0, "stance": "supporting", "note": "   "},
    ])
    assert len(evidence) == 2
    assert "<PHONE>" in evidence[0]["note"] and "<EMAIL>" in evidence[0]["note"]
    assert "13800138000" not in evidence[0]["note"]
    assert len(evidence[0]["note"]) <= fh.EVIDENCE_MAX_CHARS + 1
    assert evidence[1]["stance"] == "supporting"      # 非法 stance 归一化

    run_id = store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results), schema_version=SCHEMA,
        request_model="jev-latest", summary_text="s", evidence=evidence))
    kept = store.get_run(run_id)["evidence"]
    assert len(kept) == 2
    assert "13800138000" not in json.dumps(kept, ensure_ascii=False)


def test_history_is_immutable_and_duplicates_get_new_ids(store):
    friend = store.create_friend("档案")
    messages = [msg(0, "me", "2026-08-01 09:00", "在吗"),
                msg(1, "them", "2026-08-01 09:05", "在的")]
    results = [result(1, "2026-08-01 09:05")]
    run_a = snapshot(store, friend.friend_id, messages, results)
    run_b = snapshot(store, friend.friend_id, messages, results)
    assert run_a != run_b                       # 每次保存都是新记录
    assert len(store.list_runs(friend.friend_id)) == 2
    # 历史不可改：再次保存不会覆盖旧记录内容
    assert store.get_run(run_a)["summary_text"] == "摘要"


def test_delete_run_keeps_other_runs(store):
    friend = store.create_friend("档案")
    m1 = [msg(0, "me", "2026-08-01 09:00", "a"), msg(1, "them", "2026-08-01 09:05", "b")]
    m2 = [msg(0, "me", "2026-09-01 09:00", "c"), msg(1, "them", "2026-09-01 09:05", "d")]
    r1 = snapshot(store, friend.friend_id, m1, [result(1, "2026-08-01 09:05")])
    r2 = snapshot(store, friend.friend_id, m2, [result(1, "2026-09-01 09:05")])
    assert store.delete_run(r1) is True
    assert store.delete_run(r1) is False
    remaining = store.list_runs(friend.friend_id)
    assert [r.run_id for r in remaining] == [r2]
    assert store.all_fingerprints(friend.friend_id) == {
        fh.message_fingerprint(m) for m in m2}


def test_delete_friend_cascades(store):
    a = store.create_friend("档案一", aliases=[("测试甲.", "wechat_name")])
    b = store.create_friend("档案二")
    messages = [msg(0, "me", "2026-08-01 09:00", "a"),
                msg(1, "them", "2026-08-01 09:05", "b")]
    snapshot(store, a.friend_id, messages, [result(1, "2026-08-01 09:05")])
    other = snapshot(store, b.friend_id, messages, [result(1, "2026-08-01 09:05")])
    store.delete_friend(a.friend_id)
    assert store.get_friend(a.friend_id) is None
    assert store.list_runs(a.friend_id) == []
    assert store.find_by_alias("测试甲.") == []
    assert store.all_fingerprints(a.friend_id) == set()
    # 别的档案完全不受影响
    assert len(store.list_runs(b.friend_id)) == 1
    assert store.get_run(other) is not None


# ---------------------------------------------------------------------------
# P3：重复检测 / 重叠
# ---------------------------------------------------------------------------


def _two_run_fixture(store):
    friend = store.create_friend("档案", aliases=[("测试甲.", "wechat_name")])
    early = [msg(0, "me", "2026-08-01 09:00", "在吗"),
             msg(1, "them", "2026-08-01 09:05", "在的"),
             msg(2, "me", "2026-08-02 10:00", "周末有空吗"),
             msg(3, "them", "2026-08-02 10:05", "有啊")]
    late = [msg(0, "me", "2026-09-01 09:00", "早"),
            msg(1, "them", "2026-09-01 09:05", "早呀")]
    snapshot(store, friend.friend_id, early, [result(1, "2026-08-01 09:05"),
                                              result(3, "2026-08-02 10:05")])
    snapshot(store, friend.friend_id, late, [result(1, "2026-09-01 09:05")])
    return friend, early, late


def test_overlap_detects_duplicates_and_new_messages(store):
    friend, early, _late = _two_run_fixture(store)
    fulls = [store.get_run(r.run_id) for r in store.list_runs(friend.friend_id)]

    report = lg.overlap_with_history(early, fulls)
    assert report.fingerprint_total == 4
    assert report.duplicate_count == 4
    assert report.new_count == 0
    assert report.is_full_duplicate
    assert len(report.identical_cases) == 1       # 与某次历史是同一批消息
    assert report.time_overlap

    mixed = early + [msg(4, "them", "2026-08-03 08:00", "新的一句")]
    report2 = lg.overlap_with_history(mixed, fulls)
    assert report2.fingerprint_total == 5
    assert report2.duplicate_count == 4
    assert report2.new_count == 1
    assert not report2.is_full_duplicate


def test_same_time_different_content_is_not_duplicate(store):
    """同时间不同内容 → 指纹不同 → 不得误判为重复。"""
    friend, early, _late = _two_run_fixture(store)
    same_time_other_text = [
        msg(0, "me", "2026-08-01 09:00", "在吗"),
        msg(1, "them", "2026-08-01 09:05", "不在"),      # 时间相同、内容不同
    ]
    report = lg.overlap_with_history(
        same_time_other_text,
        [store.get_run(r.run_id) for r in store.list_runs(friend.friend_id)])
    assert report.duplicate_count == 1                 # 只有真正相同的那条
    assert report.new_count == 1
    assert not report.is_full_duplicate


def test_repeated_analysis_is_flagged(store):
    """重复分析同一批消息 → 案例指纹一致，UI 可提示。"""
    friend, early, _late = _two_run_fixture(store)
    signature = fh.case_signature(early)
    dupes = store.find_runs_by_case(friend.friend_id, signature)
    assert len(dupes) == 1
    # 完全不同的消息 → 找不到
    assert store.find_runs_by_case(
        friend.friend_id, fh.case_signature(
            [msg(0, "me", "2030-01-01 00:00", "别的")])) == []


# ---------------------------------------------------------------------------
# P4：时间顺序 / 未来信息隔离 / 资格门禁
# ---------------------------------------------------------------------------


def test_runs_are_ordered_by_chat_time_not_save_time(store):
    """倒序追加/晚保存的旧聊天，必须按真实聊天时间归位。"""
    friend = store.create_friend("档案", aliases=[("测试甲.", "wechat_name")])
    late = [msg(0, "me", "2026-09-01 09:00", "c"),
            msg(1, "them", "2026-09-01 09:05", "d")]
    early = [msg(0, "me", "2026-08-01 09:00", "a"),
             msg(1, "them", "2026-08-01 09:05", "b")]
    # 先保存**较晚**的聊天，再保存更早的（倒序追加）
    snapshot(store, friend.friend_id, late, [result(1, "2026-09-01 09:05")])
    snapshot(store, friend.friend_id, early, [result(1, "2026-08-01 09:05")])
    runs = store.list_runs(friend.friend_id)
    assert [r.chat_first_time for r in runs] == [
        "2026-08-01 09:00", "2026-09-01 09:00"]


def test_cross_period_records_are_both_listed(store):
    friend, early, late = _two_run_fixture(store)
    runs = store.list_runs(friend.friend_id)
    assert [r.chat_first_time for r in runs] == [
        "2026-08-01 09:00", "2026-09-01 09:00"]
    assert runs[0].analyzed_count == 2
    assert runs[1].analyzed_count == 1
    gap = lg.continuity_gap_days(runs)
    assert gap is not None and 29 < gap < 31


def test_future_information_never_enters_earlier_context(store):
    """晚于目标的历史时间点不得参与该目标的历史上下文。"""
    friend, early, _late = _two_run_fixture(store)
    fulls = [store.get_run(r.run_id) for r in store.list_runs(friend.friend_id)]
    history_times = [row["chat_time"] for run in fulls
                     for row in run["messages"] if row["chat_time"]]
    target = "2026-08-01 09:05"
    allowed = lg.future_leakage_allowed(target, history_times)
    assert allowed == ["2026-08-01 09:00"]        # 只有更早的那条
    assert all(t < target for t in allowed)
    # 目标没有时间 → 一律不参与（不能猜）
    assert lg.future_leakage_allowed(None, history_times) == []


def test_context_change_detects_stale_cache_reuse(store):
    """导入了更早的新消息 → 旧目标的上下文已变，不能盲目复用逐条缓存。"""
    friend, early, _late = _two_run_fixture(store)
    run = store.get_run(store.list_runs(friend.friend_id)[0].run_id)
    # 没有任何更早的新消息 → 没有失效目标
    assert lg.stale_targets(run, early) == []
    # 加入一条 8-01 之前的新消息 → 两个目标都受影响
    with_earlier = early + [msg(9, "me", "2026-07-31 08:00", "前一天")]
    stale = lg.stale_targets(run, with_earlier)
    assert stale == [1, 3]
    # 加入一条更晚的新消息 → 不影响旧目标
    with_later = early + [msg(9, "them", "2026-08-05 08:00", "之后")]
    assert lg.stale_targets(run, with_later) == []


def test_schema_mismatch_requires_reevaluation(store):
    friend = store.create_friend("档案")
    early = [msg(0, "me", "2026-08-01 09:00", "a"),
             msg(1, "them", "2026-08-01 09:05", "b")]
    late = [msg(0, "me", "2026-09-01 09:00", "c"),
            msg(1, "them", "2026-09-01 09:05", "d")]
    snapshot(store, friend.friend_id, early, [result(1, "2026-08-01 09:05")])
    snapshot(store, friend.friend_id, late, [result(1, "2026-09-01 09:05")],
             schema="chat-signal-v3.2")
    runs = store.list_runs(friend.friend_id)
    elig = {e.run_id: e for e in lg.eligibility(runs, schema_version=SCHEMA)}
    statuses = [e.status for e in elig.values()]
    assert statuses.count("reevaluate") == 1
    reasons = [r for e in elig.values() for r in e.reasons]
    assert any("schema" in r for r in reasons)
    # 报告里必须出现限制，而不是把两段连成趋势
    markdown = lg.build_longitudinal_report(
        friend, runs, [store.get_run(r.run_id) for r in runs])
    assert "纵向比较限制" in markdown


def test_model_version_mismatch_requires_reevaluation(store):
    friend = store.create_friend("档案")
    early = [msg(0, "me", "2026-08-01 09:00", "a"),
             msg(1, "them", "2026-08-01 09:05", "b")]
    late = [msg(0, "me", "2026-09-01 09:00", "c"),
            msg(1, "them", "2026-09-01 09:05", "d")]
    snapshot(store, friend.friend_id, early, [result(1, "2026-08-01 09:05")])
    snapshot(store, friend.friend_id, late,
             [result(1, "2026-09-01 09:05", model="jev-1.99.0")])
    runs = store.list_runs(friend.friend_id)
    elig = lg.eligibility(runs, schema_version=SCHEMA)
    assert all(e.status == "reevaluate" for e in elig)
    assert any("模型版本" in r for e in elig for r in e.reasons)


def test_missing_chat_time_is_a_limitation(store):
    """聊天时间缺失 → 只给限制，不生成连续趋势。"""
    friend = store.create_friend("档案")
    partial = [msg(0, "me", None, "a"), msg(1, "them", None, "b")]
    snapshot(store, friend.friend_id, partial, [result(1, None)])
    run = store.list_runs(friend.friend_id)[0]
    assert run.chat_first_time is None
    assert run.full_time_ratio == 0.0
    elig = lg.eligibility([run])
    assert elig[0].status == "reevaluate"
    assert any("缺少完整聊天时间" in r for r in elig[0].reasons)
    notes = lg.missing_data_notes(run)
    assert any("没有可归位的完整聊天时间" in n for n in notes)


def test_overlapping_ranges_require_reevaluation(store):
    """范围重叠且内容不同 → 归属不确定，必须重新评估。"""
    friend = store.create_friend("档案")
    a = [msg(0, "me", "2026-08-01 09:00", "a"),
         msg(1, "them", "2026-08-01 09:40", "b")]
    b = [msg(0, "me", "2026-08-01 09:20", "c"),
         msg(1, "them", "2026-08-01 09:50", "d")]
    snapshot(store, friend.friend_id, a, [result(1, "2026-08-01 09:40")])
    snapshot(store, friend.friend_id, b, [result(1, "2026-08-01 09:50")])
    runs = store.list_runs(friend.friend_id)
    assert runs[0].chat_last_time == "2026-08-01 09:40"
    assert runs[1].chat_first_time == "2026-08-01 09:20"   # 确实重叠
    elig = lg.eligibility(runs)
    assert all(e.status == "reevaluate" for e in elig)
    assert any("重叠" in r for e in elig for r in e.reasons)


def test_large_gap_is_reported_as_discontinuous(store):
    friend = store.create_friend("档案")
    early = [msg(0, "me", "2026-01-01 09:00", "a"),
             msg(1, "them", "2026-01-01 09:05", "b")]
    late = [msg(0, "me", "2026-08-01 09:00", "c"),
            msg(1, "them", "2026-08-01 09:05", "d")]
    snapshot(store, friend.friend_id, early, [result(1, "2026-01-01 09:05")])
    snapshot(store, friend.friend_id, late, [result(1, "2026-08-01 09:05")])
    runs = store.list_runs(friend.friend_id)
    assert lg.continuity_gap_days(runs) > lg.CONTINUITY_GAP_DAYS
    markdown = lg.build_longitudinal_report(
        friend, runs, [store.get_run(r.run_id) for r in runs])
    assert "连续性限制" in markdown
    assert "不构成连续观察" in markdown


def test_report_mentions_missing_media_and_failures(store):
    friend = store.create_friend("档案")
    messages = [msg(0, "me", "2026-08-01 09:00", "a"),
                msg(1, "them", "2026-08-01 09:05", "b")]
    failed = [{"index": 1, "speaker": "them", "time": "2026-08-01 09:05",
               "error": "分析失败：模拟", "cached": False}]
    snapshot(store, friend.friend_id, messages, failed, skipped=3)
    run = store.list_runs(friend.friend_id)[0]
    notes = lg.missing_data_notes(run)
    assert any("分析失败" in n for n in notes)
    assert any("媒体" in n for n in notes)


def test_longitudinal_report_shape(store):
    """报告结构：覆盖范围 / 行为统计 / 证据 / 说明，且没有新增评分。"""
    friend, early, late = _two_run_fixture(store)
    runs = store.list_runs(friend.friend_id)
    fulls = [store.get_run(r.run_id) for r in runs]
    markdown = lg.build_longitudinal_report(friend, runs, fulls,
                                            current_case=fh.case_signature(early))
    for section in ("各次分析的聊天覆盖范围", "各次互动行为统计",
                    "可核查的代表性证据与相反证据", "说明"):
        assert section in markdown
    assert "2026-08-01 09:00" in markdown and "2026-09-01 09:00" in markdown
    assert "同一批消息" in markdown                  # 当前导入是重复分析
    # 没有证据片段时必须明确说“只含统计与编号”
    assert "没有保留任何证据片段" in markdown
    # 不出现未经验证的新指标（报告里只允许在免责声明中“否定性”提及）
    assert "不提供人格标签" in markdown
    assert "不代表对方真实心理状态" in markdown
    for banned in ("尊重分：", "喜欢概率：", "人格类型：", "依恋风格："):
        assert banned not in markdown


def test_empty_history_report(store):
    friend = store.create_friend("档案")
    markdown = lg.build_longitudinal_report(friend, [])
    assert "还没有保存过分析快照" in markdown


def test_evidence_notes_are_deterministic(store):
    """支持性 / 相反证据都来自既有指标，排序确定。"""
    results = [
        result(1, "2026-08-01 09:05", warmth=3.0, evidence=3.2),
        result(3, "2026-08-02 10:05", warmth=1.0, evidence=2.0,
               distancing=0.8),
    ]
    support = lg.supporting_evidence(results, limit=5)
    counter = lg.counter_evidence(results, limit=5)
    assert [e["index"] for e in support] == [1, 3]      # evidence 降序
    assert 3 in [e["index"] for e in counter]           # 强疏离信号被列为相反证据
    assert all("关系信息量" in e["note"] for e in support)
    again = lg.supporting_evidence(list(reversed(results)), limit=5)
    assert [e["index"] for e in again] == [1, 3]        # 与输入顺序无关


# ---------------------------------------------------------------------------
# 存储隔离
# ---------------------------------------------------------------------------


def test_friend_history_db_is_separate_from_analysis_cache(tmp_path):
    from paths import cache_db_path, friend_history_db_path
    assert friend_history_db_path() != cache_db_path()
    assert friend_history_db_path().name == "friend_history.db"
    assert "cache" not in friend_history_db_path().name


def test_clearing_analysis_cache_does_not_touch_history(tmp_path):
    cache = storage.Cache(tmp_path / "cache.sqlite3")
    cache.set("k", {"x": 1})
    history = fh.FriendStore(tmp_path / "friend_history.db")
    friend = history.create_friend("档案")
    messages = [msg(0, "me", "2026-08-01 09:00", "a"),
                msg(1, "them", "2026-08-01 09:05", "b")]
    snapshot(history, friend.friend_id, messages, [result(1, "2026-08-01 09:05")])

    cache.clear()                                   # 清 API 缓存
    assert cache.get("k") is None
    assert len(history.list_runs(friend.friend_id)) == 1   # 档案不动

    history.delete_friend(friend.friend_id)         # 删档案
    assert cache.get("k") is None
    assert history.friend_count() == 0
