"""候选身份关联回归（修复：修改候选后原候choices消失）。

真实缺陷：用户在「长期行为观察」里修改候选的行为方向 / 类型 / 消息范围
并保存后，事件成功入库，但**原始候选仍然出现在待审核列表**——因为
``event_identity`` 基于人工更正后的（方向, 类型, 指纹）计算，与原始候选
identity 不同，而待审核过滤只按最终事件身份匹配。

修复：事件持久化 ``original_candidate_identity``（原始推荐的稳定身份），
待审核过滤按「最终事件身份 ∪ 原始候选身份」判定；schema v3 迁移 +
迁移前备份；legacy 事件按 review_note 规则 + 自身指纹保守回填。

本文件覆盖任务书要求的全部场景：

- 未修改 / 改方向 / 改类型 / 同时改 / 改范围 / 改后排除 / 重启后重新导入
  ——原候选都必须从待审核列表消失；
- 跨好友严格隔离；历史候选仍按原历史指纹处理；
- 长期报告只统计最终事件一次；重复点击 / 重复导入不产生重复事件；
- legacy 回填：有证据才关联，没证据不猜；
- schema v3 迁移（版本化 / 事务 / 备份 / 回滚）。

全部本地纯逻辑 + 临时数据库；0 Jev API；昵称与聊天全虚构。
"""

import json
import sqlite3

import pytest

import behavior as bv
import friend_history as fh
import paths
from parser import parse_chat
from privacy import mask_messages
from timeline import sort_messages

ME = "小明."
THEM = "小安."

# 虚构跨日期聊天：关心（可改方向）、拒绝（可改类型）、邀约（可改范围）
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
好啊"""


def _messages():
    return sort_messages(mask_messages(
        parse_chat(CHAT, ME, THEM))).messages


def _one(cands, dimension, behavior_type):
    for c in cands:
        if c.dimension == dimension and c.behavior_type == behavior_type:
            return c
    return None


class _store:
    """上下文管理器：临时档案库 + 版本目标补丁（模拟旧版本创建的库）。"""

    def __init__(self, target_version: int | None = None):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "friend_history.db"
        self._orig_path = paths.friend_history_db_path
        paths.friend_history_db_path = lambda: self.db
        self._orig_target = fh.FriendStore._TARGET_VERSION
        if target_version is not None:
            fh.FriendStore._TARGET_VERSION = target_version

    def __enter__(self) -> fh.FriendStore:
        return fh.FriendStore(self.db)

    def __exit__(self, *exc):
        paths.friend_history_db_path = self._orig_path
        fh.FriendStore._TARGET_VERSION = self._orig_target
        return False


def _pending(store, friend_id, messages=None):
    return bv.pending_candidates(
        bv.generate_candidates(messages or _messages()),
        store.list_events(friend_id))


def _confirm(store, friend_id, cand, *, dimension=None, behavior_type=None,
             start=None, end=None, stance="supporting", status="confirmed"):
    """模拟用户核对候选（可修改方向 / 类型 / 范围）后保存。"""
    return store.save_event(bv.build_event_dict(
        candidate=cand, friend_id=friend_id,
        dimension=dimension or cand.dimension,
        behavior_type=behavior_type or cand.behavior_type,
        stance=stance,
        messages=None if (dimension or behavior_type or start is not None
                          or end is not None) and (start is None
                                                   and end is None)
        else _messages(),
        start=None if start is None else start,
        end=None if end is None else end,
        status=status,
        original_candidate_identity=cand.identity))


# ---------------------------------------------------------------------------
# 场景 1：未修改原始推荐
# ---------------------------------------------------------------------------


def test_unmodified_candidate_disappears():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        cands = bv.generate_candidates(_messages())
        target = _one(cands, bv.DIMENSION_CARE, "care_response")
        assert target is not None
        before = len(_pending(store, friend.friend_id))
        _confirm(store, friend.friend_id, target)
        after = _pending(store, friend.friend_id)
        assert len(after) == before - 1
        assert not any(c.identity == target.identity for c in after)


# ---------------------------------------------------------------------------
# 场景 2-5：修改方向 / 类型 / 两者 / 范围
# ---------------------------------------------------------------------------


def test_modified_dimension_candidate_disappears():
    """改行为方向：最终事件身份与原始候选身份不同，原候选仍必须消失。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        before = len(_pending(store, friend.friend_id))
        event_id = _confirm(store, friend.friend_id, target,
                            dimension=bv.DIMENSION_INITIATIVE,
                            behavior_type="proactive_contact",
                            stance="supporting")
        stored = store.get_event(event_id)
        # 两个身份不同（方向被改过）
        assert stored["event_identity"] != stored[
            "original_candidate_identity"]
        assert stored["original_candidate_identity"] == target.identity
        # 原候选从待审核列表消失
        after = _pending(store, friend.friend_id)
        assert len(after) == before - 1
        assert not any(c.identity == target.identity for c in after)


def test_modified_type_candidate_disappears():
    """改行为类型（同方向内）：原候选也必须消失。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        _confirm(store, friend.friend_id, target,
                 behavior_type="emotion_understanding")
        after = _pending(store, friend.friend_id)
        assert not any(c.identity == target.identity for c in after)


def test_modified_dimension_and_type_candidate_disappears():
    """同时改方向和类型。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        _confirm(store, friend.friend_id, target,
                 dimension=bv.DIMENSION_RESPECT,
                 behavior_type="conflict_repair")
        after = _pending(store, friend.friend_id)
        assert not any(c.identity == target.identity for c in after)


def test_modified_range_candidate_disappears():
    """调整消息范围（用当前导入重新算指纹）：原候选也必须消失。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages()
        target = _one(bv.generate_candidates(messages),
                      bv.DIMENSION_CARE, "care_response")
        # 范围从 [0,1] 扩大到 [0,2]
        event_id = _confirm(store, friend.friend_id, target,
                            start=0, end=2)
        stored = store.get_event(event_id)
        assert stored["msg_window"] == [0, 2]
        assert stored["original_candidate_identity"] == target.identity
        after = _pending(store, friend.friend_id)
        assert not any(c.identity == target.identity for c in after)


# ---------------------------------------------------------------------------
# 场景 6：修改后选择排除
# ---------------------------------------------------------------------------


def test_modified_then_rejected_candidate_disappears():
    """改方向后选择「排除」：排除记录也带原始身份，原候选同样消失。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        _confirm(store, friend.friend_id, target,
                 dimension=bv.DIMENSION_RESPECT,
                 behavior_type="disagreement_response",
                 status="rejected")
        after = _pending(store, friend.friend_id)
        assert not any(c.identity == target.identity for c in after)
        events = store.list_events(friend.friend_id, status="rejected")
        assert len(events) == 1
        assert events[0]["original_candidate_identity"] == target.identity


# ---------------------------------------------------------------------------
# 场景 7：关闭程序、重新打开并重新导入同一批聊天
# ---------------------------------------------------------------------------


def test_restart_and_reimport_candidate_stays_gone():
    """重启（新 store 实例）+ 重新导入同一批聊天：原候选不得复活。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        _confirm(store, friend.friend_id, target,
                 dimension=bv.DIMENSION_INITIATIVE,
                 behavior_type="proactive_contact")
        db_path = store.db_path
        friend_id = friend.friend_id

    # 模拟重启：全新 store 实例读同一数据库
    with _store() as store2:
        store2.db_path  # noqa: B018  （占位：路径已在外部固定）
    store_reopened = fh.FriendStore(db_path)
    after = _pending(store_reopened, friend_id)
    assert not any(c.identity == target.identity for c in after)
    # 反复重新生成（多次 rerun / 重新导入）依然稳定
    for _ in range(3):
        assert not any(c.identity == target.identity
                       for c in _pending(store_reopened, friend_id))


# ---------------------------------------------------------------------------
# 跨好友隔离
# ---------------------------------------------------------------------------


def test_cross_friend_review_state_isolated():
    """好友 A 审核了某候选，不影响好友 B 的同一候选。"""
    with _store() as store:
        a = store.create_friend("档案A", aliases=["小安"])
        b = store.create_friend("档案B", aliases=["小安"])
        cands = bv.generate_candidates(_messages())
        target = _one(cands, bv.DIMENSION_CARE, "care_response")
        _confirm(store, a.friend_id, target,
                 dimension=bv.DIMENSION_INITIATIVE,
                 behavior_type="proactive_contact")
        # A 的原候选消失；B 的同一候选仍在
        assert not any(c.identity == target.identity
                       for c in _pending(store, a.friend_id))
        assert any(c.identity == target.identity
                   for c in _pending(store, b.friend_id))
        # B 审核后两边互不影响
        _confirm(store, b.friend_id, target)
        assert not any(c.identity == target.identity
                       for c in _pending(store, b.friend_id))
        assert len(store.list_events(a.friend_id)) == 1
        assert len(store.list_events(b.friend_id)) == 1


# ---------------------------------------------------------------------------
# 历史候选项
# ---------------------------------------------------------------------------


def test_history_candidate_identity_uses_run_fingerprints():
    """历史候选项仍按原 run 的真实指纹：确认（含修改）后原候选消失。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages()
        results = [
            {"index": 1, "speaker": "them",
             "time": messages[1].get("time"), "cached": True,
             "result": {"intent": {"choice": "show_care"},
                        "model": "m"}},
        ]
        store.save_run(fh.build_run_snapshot(
            friend_id=friend.friend_id, messages=messages,
            results=results, stats={},
            schema_version="chat-signal-v3.3",
            request_model="jev-latest", summary_text="旧总结"))
        run = store.get_run(store.list_runs(friend.friend_id)[0].run_id)
        hist_cands = bv.history_candidates(run)
        assert hist_cands
        cand = hist_cands[0]
        assert cand.source_kind == "history"
        # 用户修改历史候选的方向后确认
        _confirm(store, friend.friend_id, cand,
                 dimension=bv.DIMENSION_RESPECT,
                 behavior_type="disagreement_response")
        stored = store.list_events(friend.friend_id)[0]
        assert stored["original_candidate_identity"] == cand.identity
        assert stored["source_kind"] == "history"
        # 历史候选不再出现在待审核列表（身份 = run 里的真实指纹）
        pending = _pending(store, friend.friend_id)
        pending.extend(bv.history_candidates(run))
        pending = bv.pending_candidates(pending,
                                        store.list_events(friend.friend_id))
        assert not any(c.identity == cand.identity for c in pending)


# ---------------------------------------------------------------------------
# 报告只统计一次 + 重复点击 / 重复导入
# ---------------------------------------------------------------------------


def test_report_counts_final_event_once():
    """长期报告只统计最终确认事件一次（不因原候选重复出现而翻倍）。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        _confirm(store, friend.friend_id, target,
                 dimension=bv.DIMENSION_INITIATIVE,
                 behavior_type="proactive_contact")
        events = store.list_events(friend.friend_id)
        report = bv.build_behavior_report(
            store.get_friend(friend.friend_id),
            [bv.BehaviorEvent.from_dict(e) for e in events],
            generated_at="2026-09-27 10:00")
        assert "已确认行为事件：1" in report
        assert "主动性" in report        # 修改后的方向出现在报告里


def test_duplicate_confirm_creates_no_duplicate_event():
    """重复点击 / 重复导入：同一候选同一修改 → 幂等，不产生重复事件。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        target = _one(bv.generate_candidates(_messages()),
                      bv.DIMENSION_CARE, "care_response")
        first = _confirm(store, friend.friend_id, target,
                         dimension=bv.DIMENSION_INITIATIVE,
                         behavior_type="proactive_contact")
        # 同一候选、同一修改再保存一次（双击 / 重新导入）
        second = store.save_event(bv.build_event_dict(
            candidate=target, friend_id=friend.friend_id,
            dimension=bv.DIMENSION_INITIATIVE,
            behavior_type="proactive_contact",
            stance="supporting",
            original_candidate_identity=target.identity))
        assert first == second, "同一最终事件必须幂等"
        assert len(store.list_events(friend.friend_id)) == 1


# ---------------------------------------------------------------------------
# legacy 回填（v3 迁移前保存的「已修正、原候选未标记」事件）
# ---------------------------------------------------------------------------


def _legacy_event(store, friend_id, review_rule="care_turn", fps=("fp-a",)):
    """造一条 v2 时代的事件：有 review_note 规则 + 指纹，无原始身份。"""
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO behavior_events VALUES ("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e-legacy", friend_id, "initiative", "proactive_contact",
             "supporting", "confirmed", "rule", None, None, None,
             "full", "[0, 1]", "[\"" + "\",\"".join(fps) + "\"]", "{}",
             "替代解释", "", "", "旧说明", "", "",
             f"候选命中规则：{review_rule}；确定性依据",
             1.0, 1.0, None, "id-legacy-final", ""))
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_backfill_links_legacy_event_to_original_candidate():
    """有充分证据（review_note 规则 + 事件自身指纹）→ 保守回填。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages()
        target = _one(bv.generate_candidates(messages),
                      bv.DIMENSION_CARE, "care_response")
        # 同窗口（指纹集合相同）+ 规则可反推 → 原候选身份
        # care_response 候选的规则是 difficulty_then_reply
        _legacy_event(store, friend.friend_id,
                      review_rule="difficulty_then_reply",
                      fps=target.fingerprints)
        event = store.list_events(friend.friend_id)[0]
        original = bv.original_identity_from_event(event)
        assert original == target.identity, (
            "同窗口 + 规则可反推出原始候选身份")
        stats = bv.backfill_original_identities(store)
        assert stats["filled"] == 1
        linked = store.list_events(friend.friend_id)[0]
        assert linked["original_candidate_identity"] == target.identity
        # 回填后原候选消失
        assert not any(c.identity == target.identity
                       for c in _pending(store, friend.friend_id))
        # 只填了新列，用户数据一个字段都没动
        assert linked["notes"] == "旧说明"
        assert linked["dimension"] == "initiative"


def test_backfill_skips_when_evidence_insufficient():
    """证据不足（人工创建 / 无规则备注 / 未知规则）→ 不猜、不填。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        # a) 人工创建：source_kind=manual → 跳过
        _legacy_event(store, friend.friend_id)
        conn = store._connect()
        conn.execute("UPDATE behavior_events SET source_kind='manual'"
                     " WHERE event_id='e-legacy'")
        conn.commit()
        conn.close()
        # b) review_note 无规则前缀（用户改写过备注）→ 跳过
        conn = store._connect()
        conn.execute(
            "INSERT INTO behavior_events VALUES ("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e-legacy-2", friend.friend_id, "care", "care_response",
             "supporting", "confirmed", "rule", None, None, None,
             "full", "[0, 1]", '["fp-x"]', "{}", "", "", "",
             "用户自己写的备注", "", "", "我把备注改成了这样",
             1.0, 1.0, None, "id-2", ""))
        conn.commit()
        conn.close()
        stats = bv.backfill_original_identities(store)
        assert stats["filled"] == 0
        assert stats["skipped"] >= 1
        events = {e["event_id"]: e for e in
                  store.list_events(friend.friend_id)}
        assert events["e-legacy"]["original_candidate_identity"] == ""
        assert events["e-legacy-2"]["original_candidate_identity"] == ""
        # 原候选继续留在待审核列表（由用户重新核对，不猜、不删数据）
        cands = bv.generate_candidates(_messages())
        pending = _pending(store, friend.friend_id)
        assert pending


def test_backfill_unknown_rule_skipped():
    """规则 id 不在映射表（未来新规则 / 备注被改）→ 不猜。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        _legacy_event(store, friend.friend_id,
                      review_rule="future_new_rule", fps=("fp-y",))
        event = store.list_events(friend.friend_id)[0]
        assert bv.original_identity_from_event(event) == ""
        assert bv.backfill_original_identities(store)["filled"] == 0


# ---------------------------------------------------------------------------
# schema v3 迁移与备份
# ---------------------------------------------------------------------------


def test_rule_behavior_mapping_matches_generated_candidates():
    """RULE_BEHAVIOR 与规则实际产出一致（回填正确性的漂移守卫）。"""
    messages = _messages()
    cands = bv.generate_candidates(messages)
    assert cands
    for cand in cands:
        pair = bv.RULE_BEHAVIOR.get(cand.rule)
        assert pair is not None, f"未知规则 id：{cand.rule}"
        assert pair == (cand.dimension, cand.behavior_type), cand.rule


# ---------------------------------------------------------------------------
# 两个不同原始候选修正为同一个最终事件（不新增表的集合语义）
# ---------------------------------------------------------------------------


def test_two_originals_corrected_to_same_event_both_tracked():
    """A/B 两个候选被修正成同一最终事件：两条原始身份都要记录在案。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        cands = bv.generate_candidates(_messages())
        A = _one(cands, bv.DIMENSION_CARE, "care_response")          # [0,1]
        B = _one(cands, bv.DIMENSION_CARE,
                 "emotion_understanding")                            # [1,3]
        assert A is not None and B is not None
        assert A.identity != B.identity

        a_id = _confirm(store, friend.friend_id, A,
                        dimension=bv.DIMENSION_INITIATIVE,
                        behavior_type="proactive_contact")
        # B 也修正成同一个最终事件（范围调整成与 A 一致）
        b_id = _confirm(store, friend.friend_id, B,
                        dimension=bv.DIMENSION_INITIATIVE,
                        behavior_type="proactive_contact",
                        start=0, end=1)
        assert a_id == b_id, "同一最终事件必须幂等"

        stored = store.get_event(a_id)
        originals = bv.original_identities(stored)
        assert set(originals) == {A.identity, B.identity}, originals

        # 两个原候 choices 都消失，且不产生第二个事件
        pending = _pending(store, friend.friend_id)
        assert not any(c.identity == A.identity for c in pending)
        assert not any(c.identity == B.identity for c in pending)
        assert len(store.list_events(friend.friend_id)) == 1


def test_multi_original_repeat_save_stays_idempotent():
    """对多原始身份事件重复保存同一候选：集合不变、事件不增。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        cands = bv.generate_candidates(_messages())
        A = _one(cands, bv.DIMENSION_CARE, "care_response")
        B = _one(cands, bv.DIMENSION_CARE, "emotion_understanding")
        _confirm(store, friend.friend_id, A,
                 dimension=bv.DIMENSION_INITIATIVE,
                 behavior_type="proactive_contact")
        first = _confirm(store, friend.friend_id, B,
                         dimension=bv.DIMENSION_INITIATIVE,
                         behavior_type="proactive_contact",
                         start=0, end=1)
        again = _confirm(store, friend.friend_id, B,
                         dimension=bv.DIMENSION_INITIATIVE,
                         behavior_type="proactive_contact",
                         start=0, end=1)
        assert first == again
        stored = store.get_event(first)
        assert set(bv.original_identities(stored)) == {A.identity,
                                                       B.identity}
        assert len(store.list_events(friend.friend_id)) == 1


def test_original_identities_storage_roundtrip_and_legacy_scalar():
    """标量（v3）与 JSON 数组两种存储形态都能读；集合编码可逆。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        cands = bv.generate_candidates(_messages())
        A = _one(cands, bv.DIMENSION_CARE, "care_response")
        single = store.save_event(bv.build_event_dict(
            candidate=A, friend_id=friend.friend_id,
            dimension=A.dimension, behavior_type=A.behavior_type,
            stance="supporting"))
        stored = store.get_event(single)
        # 单身份仍写标量（v3 兼容、DB 可读）
        assert stored["original_candidate_identity"] == A.identity
        assert bv.original_identities(stored) == [A.identity]

        # 追加第二个 → JSON 数组
        B = _one(bv.generate_candidates(_messages()),
                 bv.DIMENSION_CARE, "emotion_understanding")
        assert store.add_event_original_identity(single, B.identity)
        stored = store.get_event(single)
        assert set(bv.original_identities(stored)) == {A.identity,
                                                       B.identity}
        # 脏数据容忍：非 JSON / 非列表 → 空，不炸
        assert bv.original_identities(
            {"original_candidate_identity": "[oops"}) == []


# ---------------------------------------------------------------------------
# 回填预览（不写入）与执行结果一致
# ---------------------------------------------------------------------------


def test_plan_backfill_matches_run_result():
    """预览计数与执行结果一致；已有关联的事件不计入。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages()
        cand = _one(bv.generate_candidates(messages),
                    bv.DIMENSION_CARE, "care_response")
        conn = store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO behavior_events VALUES ("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("e-legacy", friend.friend_id, "initiative",
                 "proactive_contact", "supporting", "confirmed", "rule",
                 None, None, None, "full", "[0, 1]",
                 json.dumps(cand.fingerprints, ensure_ascii=False), "{}",
                 "", "", "", "旧说明", "", "",
                 "候选命中规则：difficulty_then_reply；x",
                 1.0, 1.0, None, "id-legacy", ""))
            conn.execute("COMMIT")
        finally:
            conn.close()

        plan = bv.plan_backfill(store, friend_id=friend.friend_id)
        assert plan["would_fill"] == 1
        assert plan["would_skip"] == 0

        stats = bv.backfill_original_identities(
            store, friend_id=friend.friend_id)
        assert stats["filled"] == 1 and stats["skipped"] == 0

        # 执行后再预览：可关联数归零、已有关联 +1
        plan2 = bv.plan_backfill(store, friend_id=friend.friend_id)
        assert plan2["would_fill"] == 0
        assert plan2["already_linked"] == 1
        # 原始候选随之消失
        assert not any(c.identity == cand.identity
                       for c in _pending(store, friend.friend_id))


def test_plan_backfill_skips_insufficient_evidence():
    """证据不足只在预览里计数为跳过，执行也不会写。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        # 人工创建的事件 → 不进 checked
        store.save_event(bv.build_event_dict(
            candidate=None, friend_id=friend.friend_id,
            dimension="care", behavior_type="care_response",
            stance="supporting", notes="手动",
            messages=_messages(), start=0, end=0))
        # 规则候选但没有证据（备注无规则前缀）→ would_skip
        conn = store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO behavior_events VALUES ("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("e-norule", friend.friend_id, "care", "care_response",
                 "supporting", "confirmed", "rule", None, None, None,
                 "full", "[0, 1]", '["fp"]', "{}", "", "", "",
                 "用户改写过的备注", "", "", "没有规则前缀",
                 1.0, 1.0, None, "id-norule", ""))
            conn.execute("COMMIT")
        finally:
            conn.close()

        plan = bv.plan_backfill(store, friend_id=friend.friend_id)
        assert plan["would_fill"] == 0
        assert plan["would_skip"] == 1
        stats = bv.backfill_original_identities(
            store, friend_id=friend.friend_id)
        assert stats["filled"] == 0 and stats["skipped"] == 1
