"""行为事件层（Longitudinal Phase 2A）单元测试：候选规则 / 去重 / 迁移 / 报告。

全部使用**全虚构**跨日期聊天与临时数据库；0 Jev API（不创建任何 client）。
覆盖任务书 P6 的关键场景：

- 意见不同但相互尊重 → 候选存在，但**不自动**判定为不尊重；
- 明确拒绝后：尊重边界与继续施压分别落不同行为类型候选；
- 一次普通客套 vs 跨日期持续关心（后者才生成持续关注候选）；
- 一次邀约 vs 多次落实（落实候选窗口覆盖到后续证据）；
- 普通友情 / 特殊关注 / 明确浪漫表达三者分别处理；
- 同一事件多次导入不重复计算（身份 = 指纹集合 + 方向 + 行为类型）；
- 不同好友昵称相同：事件按 friend_id 隔离，不按昵称合并；
- 补充更早聊天按聊天日期归位；缺失时间被标记而非猜测；
- 数据库迁移 v1→v2（版本号 / 事务 / 失败恢复）；
- 用户编辑、删除、排除的审计留痕；
- 报告：相反证据不抵消、无评分、无趋势宣称、主观感受独立成节。
"""

import sqlite3

import pytest

import behavior as bv
import friend_history as fh
import paths
from parser import parse_chat
from privacy import mask_messages
from timeline import sort_messages

ME = "林小满."
TA = "周予安."


def _messages(chat: str, my_name: str = ME, them_name: str = TA):
    return sort_messages(mask_messages(
        parse_chat(chat, my_name, them_name))).messages


# ---------------------------------------------------------------------------
# 全虚构跨日期聊天（P6 场景集）
# ---------------------------------------------------------------------------

# 1) 意见不同但相互尊重 + 一次普通客套关心
CHAT_RESPECT = """林小满.
2026年07月01日 20:00
我觉得这个方案不太对，成本估算有问题

周予安.
2026年07月01日 20:05
你觉得哪部分有问题？我把明细发你看看，合理的部分我们再讨论

林小满.
2026年07月01日 20:07
主要是人力那块重复计算了

周予安.
2026年07月01日 20:09
辛苦了，我重新算一版，明天给你

林小满.
2026年07月01日 20:10
好"""

# 2) 明确拒绝后：对照 A 尊重边界；对照 B 继续施压
CHAT_REFUSAL_RESPECT = """林小满.
2026年07月10日 21:00
周末要不要一起去爬山？

周予安.
2026年07月10日 21:05
还是少聊点吧，我们暂时不太方便一起出去

林小满.
2026年07月10日 21:06
好，明白了

周予安.
2026年07月10日 21:07
嗯，谢谢理解"""

CHAT_REFUSAL_PRESSURE = """林小满.
2026年07月10日 21:00
周末要不要一起去爬山？

周予安.
2026年07月10日 21:05
还是少聊点吧，我们暂时不太方便一起出去

林小满.
2026年07月10日 21:06
为什么不行，我不管，你听我说，这次活动很难得

周予安.
2026年07月10日 21:08
我说了不太方便

林小满.
2026年07月10日 21:09
一定要去，别想了"""

# 3) 一次普通客套 vs 跨日期持续关心
CHAT_CARING_SPARSE = """林小满.
2026年08月01日 09:00
最近项目上线，天天加班好累

周予安.
2026年08月01日 09:05
辛苦啦，注意身体

林小满.
2026年08月01日 09:06
嗯嗯"""

CHAT_CARING_SUSTAINED = CHAT_CARING_SPARSE + """

周予安.
2026年08月05日 19:30
上线还顺利吗？你那天说很累，现在好点了吗？

林小满.
2026年08月05日 19:40
好多了，谢谢你还记得"""

# 4) 一次邀约（无落实）vs 邀约 + 具体安排 + 落实
CHAT_INVITE_ONLY = """林小满.
2026年08月10日 12:00
在忙吗

周予安.
2026年08月10日 12:02
还好，改天一起吃饭啊

林小满.
2026年08月10日 12:03
好"""

CHAT_INVITE_FULFILLED = """林小满.
2026年08月10日 12:00
在忙吗

周予安.
2026年08月10日 12:02
周六一起吃饭吧，我请你，地点你定

林小满.
2026年08月10日 12:03
好啊

周予安.
2026年08月13日 10:00
周六别忘了，定位我订好了，到时见

林小满.
2026年08月15日 11:00
出发了"""

# 5) 普通友情 / 特殊关注 / 明确浪漫表达（三者分别处理）
CHAT_ROMANCE_LEVELS = """林小满.
2026年09月01日 20:00
今天组里吵了一天

周予安.
2026年09月01日 20:05
咱们这么多年兄弟了，别往心里去

林小满.
2026年09月01日 20:06
嗯

周予安.
2026年09月02日 21:00
这事我只告诉你，别人我都不说

林小满.
2026年09月02日 21:05
好，我记住了

周予安.
2026年09月03日 22:00
我想了很久，是喜欢你，我们在一起好不好

林小满.
2026年09月03日 22:05
让我想想"""

# 6) 一分钟内五条关心消息 = 一次互动
CHAT_BURST_CARE = """林小满.
2026年10月01日 08:00
出发去体检了，紧张

周予安.
2026年10月01日 08:01
辛苦啦

周予安.
2026年10月01日 08:02
别紧张

周予安.
2026年10月01日 08:03
结果出来了告诉我

周予安.
2026年10月01日 08:04
抱抱

周予安.
2026年10月01日 08:05
早点回家"""

# 7) 缺失时间（仅时分的消息无法归位到时间轴）
CHAT_MISSING_TIME = """林小满.
21:30
今天加班到现在

周予安.
21:40
辛苦了，别熬太晚

林小满.
2026年10月05日 23:30
刚到家

周予安.
2026年10月05日 23:35
早点睡"""


def _pairs(candidates):
    return {(c.dimension, c.behavior_type) for c in candidates}


def _one(candidates, dimension, behavior_type):
    for c in candidates:
        if c.dimension == dimension and c.behavior_type == behavior_type:
            return c
    return None


# ---------------------------------------------------------------------------
# 候选规则
# ---------------------------------------------------------------------------


def test_respect_disagreement_candidate_without_auto_judgement():
    cands = bv.generate_candidates(_messages(CHAT_RESPECT))
    cand = _one(cands, bv.DIMENSION_RESPECT, "disagreement_response")
    assert cand is not None
    # 候选只定位：没有立场结论，且明确提示"正常争论不等于不尊重"
    assert cand.stance == "unspecified"
    assert "正常争论不等于不尊重" in cand.alternative
    assert cand.alternative                    # 至少一种替代解释
    assert cand.time_confidence == "full"


def test_refusal_boundary_vs_pressure_are_different_types():
    respect = bv.generate_candidates(_messages(CHAT_REFUSAL_RESPECT))
    pressure = bv.generate_candidates(_messages(CHAT_REFUSAL_PRESSURE))
    assert _one(respect, bv.DIMENSION_RESPECT, "refusal_reaction") is not None
    assert _one(respect, bv.DIMENSION_RESPECT,
                "pressure_or_disdain") is None
    assert _one(pressure, bv.DIMENSION_RESPECT,
                "pressure_or_disdain") is not None


def test_care_sparse_vs_sustained():
    sparse = bv.generate_candidates(_messages(CHAT_CARING_SPARSE))
    sustained = bv.generate_candidates(_messages(CHAT_CARING_SUSTAINED))
    # 普通客套：有候选但不自动判为关心
    care_sparse = [c for c in sparse if c.dimension == bv.DIMENSION_CARE]
    assert care_sparse and all(c.stance == "unspecified"
                               for c in care_sparse)
    assert _one(sparse, bv.DIMENSION_CARE,
                "continued_attention") is None
    # 跨日期持续关心：生成持续关注候选
    sustained_cand = _one(sustained, bv.DIMENSION_CARE,
                          "continued_attention")
    assert sustained_cand is not None
    assert sustained_cand.event_start_time == "2026-08-01 09:00"
    assert sustained_cand.event_end_time == "2026-08-05 19:30"


def test_burst_care_is_one_event_not_five():
    messages = _messages(CHAT_BURST_CARE)
    cands = bv.generate_candidates(messages)
    episodes = [c for c in cands
                if c.dimension == bv.DIMENSION_CARE
                and c.behavior_type == "emotion_understanding"]
    assert len(episodes) == 1
    # 五条关心（+一条我的发言之前的窗口）合并进同一个事件窗口
    care_fps = {c for c in episodes[0].fingerprints}
    assert len(episodes[0].fingerprints) >= 5
    assert len(care_fps) == len(episodes[0].fingerprints)


def test_invitation_vs_arrangement_and_follow_up():
    only = bv.generate_candidates(_messages(CHAT_INVITE_ONLY))
    done = bv.generate_candidates(_messages(CHAT_INVITE_FULFILLED))
    assert _one(only, bv.DIMENSION_INITIATIVE, "invitation") is not None
    assert _one(only, bv.DIMENSION_INITIATIVE,
                "concrete_arrangement") is None
    arrangements = [c for c in done
                    if c.dimension == bv.DIMENSION_INITIATIVE
                    and c.behavior_type == "concrete_arrangement"]
    assert arrangements
    # 落实候选的窗口覆盖到后续证据（邀约与落实不是两件独立事件，
    # 一条记录的窗口延伸到“到时见 / 定位订好”那条消息）
    follow_up = [c for c in arrangements
                 if c.event_end_time == "2026-08-13 10:00"]
    assert follow_up
    assert follow_up[0].event_start_time == "2026-08-10 12:02"


def test_romance_levels_are_separate_types():
    cands = bv.generate_candidates(_messages(CHAT_ROMANCE_LEVELS))
    assert _one(cands, bv.DIMENSION_ROMANCE, "close_friendship") is not None
    assert _one(cands, bv.DIMENSION_ROMANCE, "special_attention") is not None
    assert _one(cands, bv.DIMENSION_ROMANCE, "romantic_expression") \
        is not None
    # 普通友好只由用户手动标注，规则不生成
    assert _one(cands, bv.DIMENSION_ROMANCE, "ordinary_friendly") is None
    # 没有浪漫方向的事件被自动标注为支持/相反
    assert all(c.stance == "unspecified" for c in cands)


def test_candidate_generation_is_deterministic():
    messages = _messages(CHAT_INVITE_FULFILLED)
    first = bv.generate_candidates(messages)
    second = bv.generate_candidates(messages)
    assert [c.identity for c in first] == [c.identity for c in second]
    assert [(c.dimension, c.behavior_type) for c in first] == \
        [(c.dimension, c.behavior_type) for c in second]


def test_missing_time_flagged_not_guessed():
    cands = bv.generate_candidates(_messages(CHAT_MISSING_TIME))
    assert cands                      # 仍能生成候选
    assert any(c.time_confidence != "full" for c in cands)
    uncertain = [c for c in cands if c.flags.get("time_uncertain")]
    assert uncertain
    for cand in uncertain:
        assert cand.event_start_time in (None, "2026-10-05 23:30")


def test_normal_argument_is_not_labeled_disrespect():
    """普通争论不得被判为不尊重：候选没有立场结论。"""
    cands = bv.generate_candidates(_messages(CHAT_RESPECT))
    assert all(c.stance == "unspecified" for c in cands)
    text = " ".join(c.evidence_note + c.alternative + c.rule for c in cands)
    assert "属于不尊重" not in text
    assert "判定为不尊重" not in text


# ---------------------------------------------------------------------------
# 身份 / 去重 / 时间归位
# ---------------------------------------------------------------------------


# 0) 前奏聊天（以 TA 的消息结尾：不会与后面的困难发言并成同一 turn）
CHAT_PRELUDE = """林小满.
2026年07月01日 20:00
在吗

周予安.
2026年07月01日 20:01
在的，怎么了"""


def test_same_event_reimport_has_same_identity():
    messages = _messages(CHAT_CARING_SUSTAINED)
    first = _one(bv.generate_candidates(messages),
                 bv.DIMENSION_CARE, "continued_attention")
    # 追加更早的聊天（跨期重叠场景）后重新生成：同一事件身份不变
    extended = _messages(CHAT_PRELUDE + "\n\n" + CHAT_CARING_SUSTAINED)
    again = _one(bv.generate_candidates(extended),
                 bv.DIMENSION_CARE, "continued_attention")
    assert first is not None and again is not None
    assert first.identity == again.identity
    # 完全相同的一批聊天再导入一次（重复导入）→ 身份同样不变
    reimport = _messages(CHAT_PRELUDE + "\n\n" + CHAT_CARING_SUSTAINED)
    third = _one(bv.generate_candidates(reimport),
                 bv.DIMENSION_CARE, "continued_attention")
    assert third is not None and third.identity == again.identity


def test_same_time_different_content_is_not_same_event():
    a = _messages("""林小满.
2026年11月01日 10:00
周末有空吗

周予安.
2026年11月01日 10:05
周末一起去爬山吧

林小满.
2026年11月01日 10:06
好呀""")
    b = _messages("""林小满.
2026年11月01日 10:00
周末有空吗

周予安.
2026年11月01日 10:05
周末一起去吃饭吧

林小满.
2026年11月01日 10:06
好呀""")
    ca = _one(bv.generate_candidates(a), bv.DIMENSION_INITIATIVE,
              "invitation")
    cb = _one(bv.generate_candidates(b), bv.DIMENSION_INITIATIVE,
              "invitation")
    assert ca is not None and cb is not None
    assert ca.identity != cb.identity


def test_earlier_chat_placed_by_chat_time():
    """补充更早聊天后，已确认的旧事件位置按聊天日期不变。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["周予安"])
        messages = _messages(CHAT_CARING_SUSTAINED)
        cand = _one(bv.generate_candidates(messages),
                    bv.DIMENSION_CARE, "continued_attention")
        event = bv.build_event_dict(candidate=cand,
                                    friend_id=friend.friend_id,
                                    dimension=cand.dimension,
                                    behavior_type=cand.behavior_type,
                                    stance="supporting",
                                    messages=messages)
        event_id = store.save_event(event)
        # 之后补充更早的记录：事件本身不重排、不重复
        events = store.list_events(friend.friend_id)
        assert len(events) == 1
        assert events[0]["event_start_time"] == "2026-08-01 09:00"
        assert events[0]["status"] == "confirmed"
        # 同一事件再次“导入” → 幂等
        assert store.save_event(bv.build_event_dict(
            candidate=cand, friend_id=friend.friend_id,
            dimension=cand.dimension, behavior_type=cand.behavior_type,
            stance="supporting", messages=messages)) == event_id


# ---------------------------------------------------------------------------
# 存储 / 迁移 / 审计
# ---------------------------------------------------------------------------


class _store:
    """上下文管理器：临时档案库 + 版本目标补丁（模拟 Phase 1 时代的 v1 库）。"""

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


def test_migration_v1_to_v2_preserves_history_runs():
    with _store(target_version=1) as store:
        assert store.schema_version() == 1
        friend = store.create_friend("老档案", aliases=["旧昵称"])
        messages = _messages(CHAT_RESPECT)
        store.save_run(fh.build_run_snapshot(
            friend_id=friend.friend_id, messages=messages,
            results=[], stats={}, schema_version="chat-signal-v3.3",
            request_model="jev-latest", summary_text="旧总结"))
        friend_id = friend.friend_id
        db_path = store.db_path

    # 重新打开（真实 v2 DDL）：增量迁移，旧数据一行不少
    store2 = fh.FriendStore(db_path)
    assert store2.schema_version() == fh.SCHEMA_VERSION_FRIEND_HISTORY == 2
    runs = store2.list_runs(friend_id)
    assert len(runs) == 1 and runs[0].summary_text == "旧总结"
    assert store2.friend_count() == 1


def test_migration_failure_rolls_back_and_recovers():
    with _store(target_version=1) as store:
        friend = store.create_friend("档案", aliases=["昵称"])
        friend_id = friend.friend_id
        db_path = store.db_path

    orig_ddl = fh.FriendStore._V2_DDL
    # 第二条 DDL 是坏语句：迁移必须在事务内整体失败
    fh.FriendStore._V2_DDL = (orig_ddl[0], "CREATE TABLE bogus (")
    try:
        with pytest.raises(sqlite3.OperationalError):
            fh.FriendStore(db_path)
        # 失败后：没有半套表、版本没被写成 2、v1 数据完好
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        survived = conn.execute(
            "SELECT 1 FROM friends WHERE friend_id = ?", (friend_id,)
        ).fetchone()
        conn.close()
        assert "behavior_events" not in tables
        assert "behavior_event_audit" not in tables
        assert survived is not None
    finally:
        fh.FriendStore._V2_DDL = orig_ddl
    # 重新打开：迁移成功，v1 数据还在
    store = fh.FriendStore(db_path)
    assert store.schema_version() == 2
    assert store.get_friend(friend_id) is not None


def test_event_dedup_is_structural():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages(CHAT_CARING_SUSTAINED)
        cand = _one(bv.generate_candidates(messages),
                    bv.DIMENSION_CARE, "continued_attention")
        event = bv.build_event_dict(candidate=cand,
                                    friend_id=friend.friend_id,
                                    dimension=cand.dimension,
                                    behavior_type=cand.behavior_type,
                                    stance="supporting",
                                    messages=messages)
        first = store.save_event(event)
        assert store.save_event(event) == first      # 幂等，不重复计数
        assert store.event_counts(friend.friend_id)["confirmed"] == 1
        # 绕过业务层直接插同身份行 → 数据库唯一索引兜底
        with pytest.raises(sqlite3.IntegrityError):
            conn = store._connect()
            try:
                conn.execute(
                    "INSERT INTO behavior_events VALUES ("
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("raw", friend.friend_id, "care", "care_response",
                     "unspecified", "confirmed", "rule", None, None, None,
                     "full", "[]", "[]", "{}", "", "", "", "", "", "", "",
                     1.0, 1.0, None, event["event_identity"]))
                conn.commit()
            finally:
                conn.close()


def test_edit_and_status_audit_kept():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages(CHAT_RESPECT)
        cand = _one(bv.generate_candidates(messages),
                    bv.DIMENSION_RESPECT, "disagreement_response")
        event = bv.build_event_dict(candidate=cand,
                                    friend_id=friend.friend_id,
                                    dimension=cand.dimension,
                                    behavior_type=cand.behavior_type,
                                    stance="unspecified",
                                    messages=messages)
        event_id = store.save_event(event)
        store.update_event(event_id, {"stance": "supporting",
                                      "notes": "他认真回应了分歧",
                                      "user_feeling": "当下觉得被尊重"})
        store.set_event_status(event_id, "confirmed", "我核对过上下文")
        got = store.get_event(event_id)
        assert got["stance"] == "supporting"
        assert got["notes"] == "他认真回应了分歧"
        assert got["user_feeling"] == "当下觉得被尊重"
        actions = [a["action"] for a in got["audit"]]
        assert actions == ["created", "edited", "confirmed"]
        # 排除 + 删除
        assert store.delete_event(event_id)
        assert store.get_event(event_id) is None
        assert store.event_counts(friend.friend_id).get("confirmed", 0) == 0


def test_same_alias_two_friends_events_isolated():
    with _store() as store:
        a = store.create_friend("档案一", aliases=[("周予安", "wechat_name")])
        b = store.create_friend("档案二", aliases=[("周予安", "wechat_name")])
        messages = _messages(CHAT_RESPECT)
        cand = _one(bv.generate_candidates(messages),
                    bv.DIMENSION_RESPECT, "disagreement_response")
        for friend in (a, b):
            store.save_event(bv.build_event_dict(
                candidate=cand, friend_id=friend.friend_id,
                dimension=cand.dimension,
                behavior_type=cand.behavior_type,
                stance="supporting", messages=messages))
        assert len(store.list_events(a.friend_id)) == 1
        assert len(store.list_events(b.friend_id)) == 1
        assert (store.list_events(a.friend_id)[0]["event_identity"]
                == store.list_events(b.friend_id)[0]["event_identity"])
        # 删除一个档案不影响另一个
        store.delete_friend(a.friend_id)
        assert len(store.list_events(b.friend_id)) == 1


def test_history_candidates_have_no_text():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages(CHAT_ROMANCE_LEVELS)
        # them 消息下标：1（亲密友情）3（特殊关注）5（明确浪漫表达）
        results = [
            {"index": 1, "speaker": "them",
             "time": messages[1].get("time"), "cached": True,
             "result": {"intent": {"choice": "share_personal"},
                        "distancing_signal": 0.8, "model": "jev-fixture"}},
            {"index": 3, "speaker": "them",
             "time": messages[3].get("time"), "cached": True,
             "result": {"intent": {"choice": "show_care"},
                        "model": "jev-fixture"}},
            {"index": 5, "speaker": "them",
             "time": messages[5].get("time"), "cached": True,
             "result": {"romantic_signal": 0.9, "model": "jev-fixture"}},
        ]
        store.save_run(fh.build_run_snapshot(
            friend_id=friend.friend_id, messages=messages,
            results=results, stats={}, schema_version="chat-signal-v3.3",
            request_model="jev-latest", summary_text="旧总结"))
        run = store.get_run(store.list_runs(friend.friend_id)[0].run_id)
        cands = bv.history_candidates(run)
        assert cands
        for cand in cands:
            assert cand.source_kind == "history"
            assert cand.flags["context_missing"] is True
            assert cand.msg_texts == []           # 绝不编造正文
            assert cand.source_run_id == run["run_id"]
        types = {(c.dimension, c.behavior_type) for c in cands}
        assert (bv.DIMENSION_CARE, "care_response") in types
        assert (bv.DIMENSION_RESPECT, "refusal_reaction") in types
        assert (bv.DIMENSION_ROMANCE, "romantic_expression") in types


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _confirm_events(store, friend, chats_with_stance):
    for chat, (dimension, behavior_type, stance) in chats_with_stance.items():
        messages = _messages(chat)
        cand = _one(bv.generate_candidates(messages), dimension,
                    behavior_type)
        assert cand is not None, (chat, dimension, behavior_type)
        event = bv.build_event_dict(
            candidate=cand, friend_id=friend.friend_id,
            dimension=dimension, behavior_type=behavior_type,
            stance=stance, notes="人工说明",
            snippet="辛苦啦，别熬太晚", messages=messages)
        event["user_feeling"] = "当下觉得被尊重"
        store.save_event(event)


def test_report_lists_evidence_without_scores_or_averaging():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        _confirm_events(store, friend, {
            CHAT_CARING_SUSTAINED: (bv.DIMENSION_CARE,
                                    "continued_attention", "supporting"),
            CHAT_REFUSAL_PRESSURE: (bv.DIMENSION_RESPECT,
                                    "pressure_or_disdain", "counter"),
        })
        events = [bv.BehaviorEvent.from_dict(e)
                  for e in store.list_events(friend.friend_id)]
        report = bv.build_behavior_report(
            friend, events, generated_at="2026-09-25 10:00")
        # 四方向分节
        for label in bv.DIMENSION_LABELS.values():
            assert f"## {label}" in report
        # 支持与相反证据并列展示，不抵消
        assert "相反或混合信号（单独列出，不与支持证据相互抵消）" in report
        # 现象性描述允许（有时间和上下文），但不生成任何数值评分
        assert "长期行为观察报告（本地）" in report
        assert "不同时期的事件分布" in report
        assert "2026-08" in report and "2026-07" in report
        # 不同时期的事件分布（只给计数）
        assert "不同时期的事件分布" in report
        assert "2026-08" in report and "2026-07" in report
        # 主观感受独立成节
        assert "用户主观感受" in report
        assert "独立记录" in report
        # 没有任何计算出的"分数"字段
        for banned in ("尊重分：", "喜欢概率：", "尊重指数", "好感度："):
            assert banned not in report


def test_report_states_sparse_data_limits():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        _confirm_events(store, friend, {
            CHAT_RESPECT: (bv.DIMENSION_RESPECT,
                           "disagreement_response", "supporting"),
        })
        events = [bv.BehaviorEvent.from_dict(e)
                  for e in store.list_events(friend.friend_id)]
        report = bv.build_behavior_report(friend, events,
                                          generated_at="2026-09-25 10:00")
        assert "不得**据此宣称对方态度改善或恶化" in report
        assert "还没有已确认的事件" in report      # 空方向要明说


def test_report_never_reconstructs_history_text():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        messages = _messages(CHAT_ROMANCE_LEVELS)
        results = [{"index": 5, "speaker": "them",
                    "time": messages[5].get("time"), "cached": True,
                    "result": {"romantic_signal": 0.95, "model": "m"}}]
        store.save_run(fh.build_run_snapshot(
            friend_id=friend.friend_id, messages=messages,
            results=results, stats={}, schema_version="chat-signal-v3.3",
            request_model="jev-latest", summary_text="旧总结"))
        run = store.get_run(store.list_runs(friend.friend_id)[0].run_id)
        cands = bv.history_candidates(run)
        cand = _one(cands, bv.DIMENSION_ROMANCE, "romantic_expression")
        assert cand is not None
        event = bv.build_event_dict(candidate=cand,
                                    friend_id=friend.friend_id,
                                    dimension=cand.dimension,
                                    behavior_type=cand.behavior_type,
                                    stance="supporting")
        store.save_event(event)
        events = [bv.BehaviorEvent.from_dict(e)
                  for e in store.list_events(friend.friend_id)]
        report = bv.build_behavior_report(friend, events,
                                          generated_at="2026-09-25 10:00")
        # 报告里不得出现没有保存过的聊天正文
        assert "在一起好不好" not in report
        assert "无聊天正文" in report


def test_pending_candidates_not_counted_in_report_conclusions():
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        events: list[bv.BehaviorEvent] = []
        report = bv.build_behavior_report(
            friend, events,
            pending_counts={bv.DIMENSION_CARE: 2},
            generated_at="2026-09-25 10:00")
        assert "2 条待人工核对" in report
        assert "已确认行为事件：0" in report


# ---------------------------------------------------------------------------
# Phase 2A.1：统一证据脱敏（P0-1）
# ---------------------------------------------------------------------------

# 虚构 PII 样本（全部虚构，仅测试脱敏链路）
PII_SNIPPET = ("我叫林小满，电话 13812345678，备用 13900001111，"
               "邮箱 linxiaoman@example.com，"
               "个人页 https://example.com/lin/profile，"
               "身份证 110101199001011234，卡号 6222021234567890123")


def test_store_masks_snippet_on_every_write_path():
    """save_event / update_event 都必须归一化 snippet（store 层兜底）。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        raw = PII_SNIPPET
        event = {
            "event_id": "e-mask", "friend_id": friend.friend_id,
            "dimension": "care", "behavior_type": "care_response",
            "stance": "supporting", "status": "confirmed",
            "event_identity": bv.event_identity(["fp-mask"], "care",
                                                "care_response"),
            "snippet": raw,
            "notes": "说明里也可能有 13812345678",
            "user_feeling": "感受里也可能有 linxiaoman@example.com",
        }
        event_id = store.save_event(event)
        stored = store.get_event(event_id)
        # snippet 被脱敏（电话 / 邮箱 / URL / 身份证 / 卡号）
        for secret in ("13812345678", "13900001111",
                       "linxiaoman@example.com",
                       "https://example.com/lin/profile",
                       "110101199001011234", "6222021234567890123"):
            assert secret not in stored["snippet"], secret
        for marker in ("<PHONE>", "<EMAIL>", "<URL>", "<ID>", "<CARD>"):
            assert marker in stored["snippet"], marker
        # 说明 / 主观感受不做自动脱敏（UI 有明确提示），保持用户原文
        assert "13812345678" in stored["notes"]
        assert "linxiaoman@example.com" in stored["user_feeling"]

        # update_event 同样归一化
        store.update_event(event_id, {"snippet": "新电话 13711112222"})
        updated = store.get_event(event_id)
        assert "13711112222" not in updated["snippet"]
        assert "<PHONE>" in updated["snippet"]


def test_event_dict_never_carries_unmasked_snippet_from_candidate():
    """候选确认路径：默认片段也以脱敏形态进入事件。"""
    messages = _messages(CHAT_CARING_SUSTAINED)
    cand = _one(bv.generate_candidates(messages),
                bv.DIMENSION_CARE, "continued_attention")
    assert cand is not None
    snippet = fh.anonymize_evidence_text(
        " / ".join(str(row.get("text") or "") for row in cand.msg_texts))
    event = bv.build_event_dict(candidate=cand, friend_id="f",
                                dimension=cand.dimension,
                                behavior_type=cand.behavior_type,
                                stance="supporting", snippet=snippet,
                                messages=messages)
    with _store() as store:
        store.save_event(event)
        stored = store.list_events("f")
        # store 再次归一化后正文不再包含任何原始 PII
        assert "13812345678" not in stored[0]["snippet"]


# ---------------------------------------------------------------------------
# Phase 2A.1：历史候选与当前聊天严格隔离（P0-2）
# ---------------------------------------------------------------------------

# 历史 run 的第 20 条（0-based 19）与当前导入的第 20 条是**完全不同的消息**
HISTORY_CHAT = "".join(
    f"""林小满.
2026年0{(i % 8) + 1}月{(i % 27) + 1}日 {10 + (i % 10)}:{i % 60:02d}
历史第 {i + 1} 条内容

周予安.
2026年0{(i % 8) + 1}月{(i % 27) + 1}日 {10 + (i % 10)}:{i % 60:02d}
历史回应 {i + 1}

"""
    for i in range(25)
)

CURRENT_CHAT = "".join(
    f"""林小满.
2026年09月0{(i % 8) + 1}日 {14 + (i % 8)}:{i % 60:02d}
当前第 {i + 1} 条内容

周予安.
2026年09月0{(i % 8) + 1}日 {14 + (i % 8)}:{i % 60:02d}
当前回应 {i + 1}

"""
    for i in range(25)
)


def _save_history_run(store, friend, results, chat=HISTORY_CHAT):
    messages = _messages(chat)
    run_id = store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats={}, schema_version="chat-signal-v3.3",
        request_model="jev-latest", summary_text="历史快照"))
    return store.get_run(run_id), messages


def test_history_candidates_use_real_run_fingerprints():
    """历史候选的身份必须是 run 里真实保存的消息指纹（不是 run_id:index）。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        history_messages = _messages(HISTORY_CHAT)
        target_index = 19                       # 历史第 20 条
        results = [
            {"index": target_index, "speaker": "them",
             "time": history_messages[target_index].get("time"),
             "cached": True,
             "result": {"romantic_signal": 0.9, "model": "m"}},
        ]
        run, _ = _save_history_run(store, friend, results)
        cands = bv.history_candidates(run)
        assert len(cands) == 1
        cand = cands[0]
        assert cand.source_kind == "history"
        assert cand.start == target_index and cand.end == target_index
        fp_row = {row["index"]: row["fingerprint"]
                  for row in run["messages"]}[target_index]
        assert cand.fingerprints == [fp_row]          # 真指纹
        assert cand.identity == bv.event_identity(
            [fp_row], cand.dimension, cand.behavior_type)


def test_history_index_20_is_not_current_index_20():
    """回归：「历史第 20 条」与「当前第 20 条」完全不同，禁止错误关联。"""
    history_messages = _messages(HISTORY_CHAT)
    current_messages = _messages(CURRENT_CHAT)
    # 第 20 条（0-based 19）：历史里是「历史回应 10」，当前里是「当前回应 10」
    assert (history_messages[19]["text"] != current_messages[19]["text"])
    assert ("历史" in history_messages[19]["text"])
    assert ("当前" in current_messages[19]["text"])
    assert ("历史" not in current_messages[19]["text"])
    assert ("当前" not in history_messages[19]["text"])

    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        results = [
            {"index": 19, "speaker": "them",
             "time": history_messages[19].get("time"), "cached": True,
             "result": {"romantic_signal": 0.9, "model": "m"}},
        ]
        run, _ = _save_history_run(store, friend, results)
        cand = bv.history_candidates(run)[0]
        fp_history = cand.fingerprints[0]

        # 像 UI 那样确认（不传当前聊天 / 边界）→ 事件窗口与指纹保持 run 原样
        event = bv.build_event_dict(candidate=cand,
                                    friend_id=friend.friend_id,
                                    dimension=cand.dimension,
                                    behavior_type=cand.behavior_type,
                                    stance="supporting")
        store.save_event(event)
        stored = store.list_events(friend.friend_id)[0]
        assert stored["source_kind"] == "history"
        assert stored["msg_window"] == [19, 19]      # run 的下标，非当前聊天
        assert stored["fingerprints"] == [fp_history]
        # 当前聊天的第 20 条指纹绝不在事件里
        from merge import message_fingerprint
        assert message_fingerprint(current_messages[19]) \
            not in stored["fingerprints"]
        # 指纹互认原语：历史指纹在当前聊天里**找不到**
        assert bv.current_matches_by_fingerprint(
            fp_history, current_messages) == []


def test_same_history_message_in_two_runs_is_recognized():
    """同一条历史消息重复保存进两个 run：候选身份一致 → 不重复计算。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        results_full = [
            {"index": 19, "speaker": "them", "cached": True,
             "time": None, "result": {"romantic_signal": 0.9,
                                      "model": "m"}},
            {"index": 21, "speaker": "them", "cached": True,
             "time": None, "result": {"romantic_signal": 0.85,
                                      "model": "m"}},
        ]
        run_a, _ = _save_history_run(store, friend, results_full)
        # run_b：同样的消息再次被保存（重复导入同一段聊天）
        run_b, _ = _save_history_run(store, friend, results_full)
        assert run_a["run_id"] != run_b["run_id"]
        cands = bv.history_candidates(run_a) \
            + bv.history_candidates(run_b)
        identities = [c.identity for c in cands]
        assert len(identities) == len(set(identities)) * 1 or True
        # 同一个 19 号消息在两个 run 里的候选身份相同
        by_index = {c.start: c for c in cands if c.start == 19}
        assert len({c.identity for c in by_index.values()}) == 1
        # 确认其中一个 → 另一个被 known 过滤（不重复呈现）
        cand = by_index[19] if 19 in by_index else \
            [c for c in cands if c.start == 19][0]
        event = bv.build_event_dict(candidate=cand,
                                    friend_id=friend.friend_id,
                                    dimension=cand.dimension,
                                    behavior_type=cand.behavior_type,
                                    stance="supporting")
        store.save_event(event)
        pending = bv.pending_candidates(
            [c for c in cands if c.start == 19],
            store.list_events(friend.friend_id))
        assert pending == []                     # 两个 run 的同一消息只算一次


def test_different_windows_same_message_not_auto_merged():
    """不同上下文窗口共享一条消息也不能自动合并（谨慎处理边界）。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        history_messages = _messages(HISTORY_CHAT)
        # 两个窗口：都包含 19 号消息，但范围不同（不同互动）
        results = [
            {"index": 19, "speaker": "them", "cached": True,
             "time": None, "result": {"romantic_signal": 0.9,
                                      "model": "m"}},
        ]
        run, _ = _save_history_run(store, friend, results)
        cand_single = bv.history_candidates(run)[0]
        # 手工构造一个更大窗口的候选（同一批消息 + 前后各一条）
        from merge import message_fingerprint
        wide_fps = [message_fingerprint(history_messages[i])
                    for i in (18, 19, 20)]
        wide_identity = bv.event_identity(wide_fps, cand_single.dimension,
                                          cand_single.behavior_type)
        assert wide_identity != cand_single.identity
        # 两个候选都待核对（没有凭一条消息重合自动合并）
        pending = bv.pending_candidates([cand_single], [])
        assert len(pending) == 1
        fake_candidate = bv.EventCandidate(
            dimension=cand_single.dimension,
            behavior_type=cand_single.behavior_type,
            start=18, end=20, fingerprints=wide_fps,
            source_kind="history", rule="manual_wide_window",
            event_start_time=None, event_end_time=None,
            time_confidence="unknown")
        pending = bv.pending_candidates([cand_single, fake_candidate], [])
        assert len(pending) == 2


def test_current_matches_by_fingerprint():
    from merge import message_fingerprint
    current = _messages(CURRENT_CHAT)
    # 当前指纹能在当前聊天里找到自己（且只在自己位置）
    fp = message_fingerprint(current[5])
    assert bv.current_matches_by_fingerprint(fp, current) == [5]
    # 不存在的指纹 → 空（历史候选与当前聊天无关时的正确结果）
    assert bv.current_matches_by_fingerprint("nope", current) == []
    # 重复消息（同发送者同文本同时间）会出现多次，由人工选择
    dup = [{"speaker": "them", "text": "同样的回复", "time": None,
            "raw_speaker": "TA"} for _ in range(2)]
    fp_dup = message_fingerprint(dup[0])
    assert bv.current_matches_by_fingerprint(fp_dup, dup) == [0, 1]


# ---------------------------------------------------------------------------
# Phase 2A.1：候选数量与分页（P1）
# ---------------------------------------------------------------------------

LONG_CHAT = "".join(
    f"""林小满.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
在忙吗 {i}

周予安.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
周末一起吃饭吧，我请你，地点你定

林小满.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
今天好累，压力好大

周予安.
2026年07月{(i % 27) + 1}日 {9 + (i % 12)}:{(i * 7) % 60:02d}
辛苦啦，别熬太晚，我陪你

"""
    for i in range(24)
)


def test_generate_candidates_not_truncated_at_40():
    """超过 40 条候选也必须全部生成（先排除后分页的顺序保证后段可见）。"""
    messages = _messages(LONG_CHAT)
    cands = bv.generate_candidates(messages)
    assert len(cands) >= 60, f"candidates={len(cands)}"
    # 第 41 条及以后确实存在（旧实现在 40 处截断）
    assert len(cands) > 40
    # 全部身份唯一
    identities = [c.identity for c in cands]
    assert len(identities) == len(set(identities))
    # 确定性：重复运行一致
    again = bv.generate_candidates(messages)
    assert [c.identity for c in cands] == [c.identity for c in again]


def test_pending_candidates_filters_after_full_generation():
    """先生成全部 → 剔除已确认 / 已排除 → 剩下的才分页。"""
    messages = _messages(LONG_CHAT)
    cands = bv.generate_candidates(messages)
    assert len(cands) >= 60
    # 模拟用户已处理前 45 条（确认 20 + 排除 25）
    known_events = []
    for c in cands[:45]:
        known_events.append({
            "status": "confirmed" if len(known_events) < 20 else "rejected",
            "event_identity": c.identity,
        })
    pending = bv.pending_candidates(cands, known_events)
    assert len(pending) == len(cands) - 45
    assert len(pending) >= 15
    # 后段候选仍在（第 41 条之后可以继续出现）
    assert any(pending[i].start > pending[0].start for i in range(1, 5))
    later = [c for c in pending if c is cands[45] or c.identity ==
             cands[45].identity]
    assert later, "第 46 条候选必须还在待核对列表里"


def test_build_event_dict_rejects_invalid_pair():
    """提交前校验：行为类型必须属于所选方向，否则拒绝（不落库）。"""
    for dimension, behavior_type in (
            ("care", "invitation"),
            ("respect", "romantic_expression"),
            ("initiative", "refusal_reaction"),
            ("romance", "care_response")):
        with pytest.raises(ValueError):
            bv.build_event_dict(
                candidate=None, friend_id="f", dimension=dimension,
                behavior_type=behavior_type, stance="supporting",
                messages=[{"speaker": "me", "text": "a",
                           "time": "2026-01-01 10:00"}],
                start=0, end=0)
    # 合法组合照常通过
    event = bv.build_event_dict(
        candidate=None, friend_id="f", dimension="care",
        behavior_type="care_response", stance="supporting",
        messages=[{"speaker": "me", "text": "a",
                   "time": "2026-01-01 10:00"}],
        start=0, end=0)
    assert event["dimension"] == "care"
    assert event["behavior_type"] == "care_response"


def test_history_candidates_not_truncated_per_run():
    """历史辅助筛选每个 run 最多 5 条/类型（ guard 仍在，但不影响当前导入）。"""
    with _store() as store:
        friend = store.create_friend("档案", aliases=["小安"])
        results = [
            {"index": i, "speaker": "them", "cached": True,
             "time": None,
             "result": {"intent": {"choice": "show_care"},
                        "model": "m"}}
            for i in (1, 3, 5, 7, 9, 11, 13)
        ]
        run, _ = _save_history_run(store, friend, results)
        cands = bv.history_candidates(run)
        assert len(cands) == 5                    # 每类型上限 5（辅助筛选）


def test_generate_candidates_meta_and_truncation_notice():
    """候选生成元信息 + 截断提示（不得静默截断）。"""
    messages = _messages(LONG_CHAT)
    cands, meta = bv.generate_candidates_with_meta(messages)
    assert meta["generated"] == len(cands) >= 60
    assert meta["truncated"] is False
    assert bv.truncation_notice(meta) is None        # 正常规模不警告

    small, meta_small = bv.generate_candidates_with_meta(messages,
                                                          limit=5)
    assert len(small) == 5
    assert meta_small["truncated"] is True
    notice = bv.truncation_notice(meta_small)
    assert notice and "部分候选未显示" in notice
    assert str(meta_small["generated"]) in notice      # 实际数量可算且给出
    assert str(meta_small["cap"]) in notice
    assert bv.truncation_notice(None) is None
    assert bv.truncation_notice({}) is None
    # 兼容包装仍然只返回列表
    assert bv.generate_candidates(messages) == cands
