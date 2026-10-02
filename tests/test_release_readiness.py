"""Issue #26 Release Hardening：v0.4 集成 / 隐私 / 兼容性 / 打包门禁测试。

全部离线、确定性、虚构数据；不触碰真实 Jev、真实聊天、真实 data。
Windows EXE 构建由 build-windows-portable workflow 负责，本文件只做
可移植的纯逻辑门禁（CI 友好）。
"""

import json
import re
import zipfile
from pathlib import Path

import pytest

import analyzer
import behavior as bv
import interaction_dynamics as idyn
import relationship_profile as rp
import salience as sal
import scoring
from interaction_diagnostics import materialize_case
from interaction_dynamics import build_interaction_events
from relationship_profile import build_profile
from report import build_json_report, build_markdown_report

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# ---------------------------------------------------------------------------
# 虚构 E2E 聊天：#6 要求的 12 要素全覆盖
# 普通 baseline / special attention / romantic / TA after-gap re-engagement /
# follow-up / 邀约推进 / boundary+接受 / 第二 boundary + 继续同一请求 /
# personal recall candidate / 明确负例（自然收尾）
# ---------------------------------------------------------------------------

E2E_MESSAGES = [
    # 0-9 普通 baseline（含低信息量短回复）
    {"speaker": "me", "time": "2026-03-01 09:00", "text": "早啊"},
    {"speaker": "them", "time": "2026-03-01 09:01", "text": "早"},
    {"speaker": "me", "time": "2026-03-01 09:02", "text": "今天天气不错"},
    {"speaker": "them", "time": "2026-03-01 09:03", "text": "嗯"},
    {"speaker": "me", "time": "2026-03-01 12:00", "text": "吃饭了吗"},
    {"speaker": "them", "time": "2026-03-01 12:01", "text": "吃了"},
    {"speaker": "me", "time": "2026-03-01 18:00", "text": "下班了"},
    {"speaker": "them", "time": "2026-03-01 18:01", "text": "好"},
    {"speaker": "me", "time": "2026-03-01 20:00", "text": "早点睡"},
    {"speaker": "them", "time": "2026-03-01 20:01", "text": "晚安"},
    # 10-13 明确 special attention（“这个我只告诉你”）
    {"speaker": "me", "time": "2026-03-02 10:00", "text": "最近有点迷茫"},
    {"speaker": "them", "time": "2026-03-02 10:01",
     "text": "这个我只告诉你，我觉得你一直很棒"},
    # 14-17 明确 romantic signal
    {"speaker": "me", "time": "2026-03-02 21:00", "text": "早点休息吧"},
    {"speaker": "them", "time": "2026-03-02 21:01",
     "text": "喜欢你，晚安"},
    # 18-21 follow-up sequence
    {"speaker": "me", "time": "2026-03-03 09:00", "text": "胃有点不舒服"},
    {"speaker": "them", "time": "2026-03-03 09:01", "text": "怎么了？"},
    {"speaker": "me", "time": "2026-03-03 09:02", "text": "可能吃坏了"},
    {"speaker": "them", "time": "2026-03-03 09:03", "text": "现在还疼吗？"},
    # 22-25 邀约推进（模糊 → 具体）
    {"speaker": "me", "time": "2026-03-04 12:00", "text": "周末有空"},
    {"speaker": "them", "time": "2026-03-04 12:01", "text": "有空一起吃饭"},
    {"speaker": "me", "time": "2026-03-04 12:05", "text": "好啊"},
    {"speaker": "them", "time": "2026-03-04 12:06",
     "text": "那周六六点，公司楼下见"},
    # 26-33 第一个 boundary → TA 接受；第二 boundary → 继续同一请求
    {"speaker": "me", "time": "2026-03-05 19:00", "text": "今晚聚餐我不去了"},
    {"speaker": "them", "time": "2026-03-05 19:01", "text": "好，那不去了"},
    {"speaker": "me", "time": "2026-03-05 19:05", "text": "我不想去唱歌"},
    {"speaker": "them", "time": "2026-03-05 19:06", "text": "就来嘛"},
    # 34-36 个人回忆候选（三天后 TA 重提智齿）
    {"speaker": "me", "time": "2026-03-01 15:00", "text": "周五要去拔智齿"},
    {"speaker": "them", "time": "2026-03-06 10:00", "text": "你智齿拔了吗？"},
    # 37-39 after-gap TA re-engagement（三天后 TA 先发消息）
    {"speaker": "them", "time": "2026-03-09 09:00", "text": "那个文件发你了"},
    # 40-41 明确负例：自然收尾（不得 withdrawal salient）
    {"speaker": "me", "time": "2026-03-09 09:30", "text": "今天先聊到这"},
    {"speaker": "them", "time": "2026-03-09 09:31", "text": "好，回头聊"},
]

# TA 消息 → 虚构九问结果（按顺序对应）
_SPECIAL = {"warmth": 2.8, "engagement": 2.6, "special": 3.6, "evidence": 3.2,
            "ease": 2.5, "romantic": 0.2, "distancing": 0.03}
_ROMANTIC = {"warmth": 2.9, "engagement": 2.5, "special": 2.2, "evidence": 3.0,
             "ease": 2.5, "romantic": 0.92, "distancing": 0.04}
_ASK = {"warmth": 2.5, "engagement": 2.6, "special": 1.8, "evidence": 2.2,
        "ease": 2.4, "romantic": 0.15, "distancing": 0.03,
        "intent": "ask_information",
        "intent_probs": {"ask_information": 0.88, "other": 0.12}}
_ASK_CARE = dict(_ASK, warmth=3.0, intent_probs={"ask_information": 0.8,
                                                 "show_care": 0.15})
_INVITE = {"warmth": 2.4, "engagement": 2.8, "special": 1.6, "evidence": 2.0,
           "ease": 2.3, "romantic": 0.12, "distancing": 0.03,
           "intent": "invite", "intent_probs": {"invite": 0.85, "other": 0.15}}
_DEFAULT = {"warmth": 2.0, "engagement": 2.0, "special": 1.2, "evidence": 1.5,
            "ease": 2.2, "romantic": 0.08, "distancing": 0.05}


def _dist(score):
    lo, hi = int(score // 1), int(-(-score // 1))
    out = {str(i): 0.0 for i in range(5)}
    if lo == hi:
        out[str(lo)] = 1.0
    else:
        out[str(lo)] = 1.0 - (score - lo)
        out[str(hi)] = score - lo
    return out


def _result(spec):
    conf = spec.get("conf", 0.75)
    intent = spec.get("intent", "continue_topic")
    probs = spec.get("intent_probs") or {intent: 1.0}

    def block(key, default):
        score = float(spec.get(key, default))
        return {"score": score, "probabilities": _dist(score),
                "confidence": conf}

    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": conf},
        "intent": {"choice": intent, "probabilities": probs,
                   "confidence": conf},
        "warmth": block("warmth", 2.0),
        "engagement": block("engagement", 2.0),
        "special_attention": block("special", 1.5),
        "relationship_evidence_strength": block("evidence", 2.4),
        "relational_ease": block("ease", 2.3),
        "romantic_signal": float(spec.get("romantic", 0.1)),
        "distancing_signal": float(spec.get("distancing", 0.05)),
        "model": "fake-e2e",
    }


def _e2e_pipeline():
    messages = [dict(m) for m in E2E_MESSAGES]
    ta_specs = {
        11: _SPECIAL, 13: _ROMANTIC, 15: _ASK, 17: _ASK_CARE,
        19: _INVITE, 21: _INVITE,
    }
    results = []
    for i, m in enumerate(messages):
        if m["speaker"] != "them":
            continue
        results.append({"index": i, "speaker": "them", "text": m["text"],
                        "time": m["time"], "context": [],
                        "result": _result(ta_specs.get(i, _DEFAULT))})
    interaction = build_interaction_events(messages, results)
    salience_out = sal.build_salience(results, interaction=interaction)
    profile = build_profile(results, salience=salience_out,
                            interaction=interaction)
    return messages, results, interaction, salience_out, profile


# ---------------------------------------------------------------------------
# §6/§7 E2E synthetic acceptance flow
# ---------------------------------------------------------------------------


class TestEndToEndSyntheticFlow:
    def test_full_pipeline_structure(self):
        messages, results, interaction, salience_out, profile = _e2e_pipeline()
        # 普通 baseline 由普通消息决定
        assert profile["dimensions"]["special_attention"]["baseline"][
            "ordinary_messages"] >= 8
        # Jev 明确档 salience 事件
        specials = profile["dimensions"]["special_attention"]["salient_events"]
        assert any(e["source"] == "jev_metric"
                   and e["event_class"] == "explicit_special_attention"
                   for e in specials)
        romantics = profile["dimensions"]["romantic"]["salient_events"]
        assert any(e["event_class"] == "explicit_romantic_signal"
                   for e in romantics)
        # interaction 结构事件
        types = {e["event_type"] for e in interaction["events"]}
        assert "followup_sequence" in types
        assert "invitation_progression" in types
        assert "conversation_reengagement" in types
        assert "boundary_accepted" in types
        assert "boundary_pressure" in types
        assert "personal_recall_candidate" in types
        # 明确负例：自然收尾不是 withdrawal salient
        assert not any(e["event_class"] == "explicit_relationship_withdrawal"
                       for e in profile["dimensions"]["withdrawal"]
                       ["salient_events"])
        assert not any(e["event_type"] == "boundary_continued_request"
                       and e["review_status"] == idyn.REVIEW_AUTO
                       for e in interaction["events"]
                       if e["event_type"] != "boundary_pressure")

    def test_contradictory_evidence_coexist_without_net_verdict(self):
        _messages, _results, interaction, _sal, profile = _e2e_pipeline()
        text = profile["summary"]["text"]
        dims = profile["dimensions"]
        # special + romantic 明确，同时存在一次拒绝后继续推进
        assert dims["special_attention"]["supporting_count"] >= 1
        assert dims["romantic"]["supporting_count"] >= 1
        assert any(e["event_type"] == "boundary_pressure"
                   for e in interaction["events"])
        # 不得出现任何“综合”裁决
        for banned in ("综合关系", "整体关系", "关系很好", "关系不好",
                       "关系健康", "更健康", "更喜欢你"):
            assert banned not in text, banned
        # 多维结构分别陈述
        assert "特殊关注" in text and "浪漫" in text

    def test_no_composite_score_added_by_pipeline(self):
        _m, _r, interaction, salience_out, profile = _e2e_pipeline()
        dumped = json.dumps([interaction, salience_out, profile],
                            ensure_ascii=False)
        for banned in ("composite", "overall_salience", "salience_score",
                       "relationship_health", "liking_probability"):
            assert banned not in dumped, banned


# ---------------------------------------------------------------------------
# §8 三态契约（端到端）
# ---------------------------------------------------------------------------


class TestThreeStateContract:
    def test_single_explicit_is_evidence_limited(self):
        messages, results, interaction, _s, profile = _e2e_pipeline()
        dim = profile["dimensions"]["special_attention"]
        assert dim["status"] in (rp.STATUS_SUFFICIENT,
                                 rp.STATUS_EVIDENCE_LIMITED)
        for e in dim["salient_events"]:
            assert e.get("review_status") != idyn.REVIEW_CANDIDATE

    def test_boundary_no_opportunity_is_insufficient_not_low(self):
        messages = [
            {"speaker": "me", "time": "2026-03-01 09:00", "text": "早上好"},
            {"speaker": "them", "time": "2026-03-01 09:01", "text": "早"},
        ]
        interaction = build_interaction_events(messages, [])
        profile = build_profile([], interaction=interaction)
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["status"] == rp.STATUS_INSUFFICIENT
        assert "缺少可观察机会" in dim["conclusion"] or "没有可用于观察" in dim["conclusion"]
        assert "边界压力" not in dim["conclusion"] or "低" not in dim["conclusion"]

    def test_engine_unavailable_stays_unsupported(self):
        profile = build_profile([])
        dim = profile["dimensions"]["boundary_pressure"]
        assert dim["status"] == rp.STATUS_UNSUPPORTED

    def test_weak_ordinary_is_insufficient(self):
        profile = build_profile([])
        dim = profile["dimensions"]["romantic"]
        assert dim["status"] in (rp.STATUS_INSUFFICIENT, rp.STATUS_UNSUPPORTED)


# ---------------------------------------------------------------------------
# §9 review-required 隔离（端到端）
# ---------------------------------------------------------------------------


class TestReviewRequiredIsolation:
    def test_recall_and_ambiguous_boundary_never_formal(self):
        messages, results, interaction, salience_out, profile = _e2e_pipeline()
        # personal recall 与模糊 boundary 都是 review_required
        candidates = [e for e in interaction["events"]
                      if e["review_status"] == idyn.REVIEW_CANDIDATE]
        assert any(e["event_type"] == "personal_recall_candidate"
                   for e in candidates)
        # 不进正式 salient / counter evidence
        for dim in profile["dimensions"].values():
            for e in dim["salient_events"] + dim["counter_events"]:
                assert e.get("review_status") != idyn.REVIEW_CANDIDATE, e
        # 不进 salience 正式事件
        assert all(e.get("review_status") != idyn.REVIEW_CANDIDATE
                   for e in salience_out["events"])
        # 不进默认 Markdown 结论（事件表只作“待核对”展示，不进摘要行）
        md = build_markdown_report(results, scoring.compute_conversation_stats(
            results), include_text=False, interaction=interaction)
        summary_block = md.split("## 互动结构证据")[0]
        assert "智齿" not in summary_block
        # 不改变 overall / baseline
        stats = scoring.compute_conversation_stats(results)
        assert profile["overall_legacy"]["overall"] == stats["overall"]
        care = profile["dimensions"]["care_responsiveness"]["baseline"]
        assert care["ordinary_messages"] >= 1

    def test_review_candidates_are_offered_to_behavior_queue(self):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        candidates = idyn.interaction_behavior_candidates(interaction,
                                                          messages)
        assert any(c.rule == "interaction:personal_recall_candidate"
                   for c in candidates)


# ---------------------------------------------------------------------------
# §10/§11/§12 behavior review / 重复导入 / 双通道去重
# ---------------------------------------------------------------------------


class TestBehaviorReviewChain:
    def test_confirm_then_reimport_not_pending(self, tmp_path):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        candidates = idyn.interaction_behavior_candidates(interaction, messages)
        target = next(c for c in candidates
                      if c.rule == "interaction:personal_recall_candidate")
        event = bv.build_event_dict(candidate=target, friend_id="f1",
                                    dimension=target.dimension,
                                    behavior_type=target.behavior_type,
                                    stance="supporting", status="confirmed")
        again = build_interaction_events(messages, results)
        pending = bv.pending_candidates(
            idyn.interaction_behavior_candidates(again, messages), [event])
        assert all(c.identity != target.identity
                   or c.original_candidate_identity != target.original_candidate_identity
                   for c in pending)

    def test_reject_then_reimport_not_pending(self):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        candidates = idyn.interaction_behavior_candidates(interaction, messages)
        target = candidates[0]
        event = bv.build_event_dict(candidate=target, friend_id="f1",
                                    dimension=target.dimension,
                                    behavior_type=target.behavior_type,
                                    stance="unspecified", status="rejected")
        again = build_interaction_events(messages, results)
        pending = bv.pending_candidates(
            idyn.interaction_behavior_candidates(again, messages), [event])
        assert target.identity not in {c.identity for c in pending} or \
            target.original_candidate_identity not in {
                c.original_candidate_identity for c in pending}

    def test_prepend_append_keep_identity(self):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        base_ids = {e["identity"] for e in interaction["events"]}
        older = [{"speaker": "me", "time": "2026-02-20 08:00",
                  "text": "上次的方案改完了"},
                 {"speaker": "them", "time": "2026-02-20 08:01", "text": "收到"}]
        shifted = [{**e, "index": e["index"] + len(older)} for e in results]
        prepended = build_interaction_events(older + messages, shifted)
        assert base_ids <= {e["identity"] for e in prepended["events"]}
        later = [{"speaker": "me", "time": "2026-03-20 08:00", "text": "早"},
                 {"speaker": "them", "time": "2026-03-20 08:01", "text": "早"}]
        appended = build_interaction_events(messages + later, results)
        assert base_ids <= {e["identity"] for e in appended["events"]}

    def test_duplicate_import_no_double_events(self):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        again = build_interaction_events(messages, results)
        assert len(interaction["events"]) == len(again["events"])
        assert [e["identity"] for e in interaction["events"]] == \
            [e["identity"] for e in again["events"]]
        # append 只新增真正的新事件（返聘与 recall 候选之外不应暴涨）
        later = [{"speaker": "them", "time": "2026-03-20 08:01",
                  "text": "周末大家一起吃饭？",
                  # 该消息无 result：普通文本消息
                  }]
        appended = build_interaction_events(messages + later, results)
        assert len(appended["events"]) >= len(interaction["events"])

    def test_same_observation_not_inflated_across_channels(self):
        """同一底层观察穿越两个内部通道不得膨胀证据计数（§12）。"""
        messages, results, interaction, salience_out, profile = _e2e_pipeline()
        special_dim = profile["dimensions"]["special_attention"]
        jev_events = [e for e in special_dim["salient_events"]
                      if e.get("source") == "jev_metric"]
        interaction_events = special_dim["interaction_events"]
        # salient 列表里 interaction 事件只允许来自互动通道（不会与 Jev 事件
        # 重复同一 observable fact：special/care/romantic 不因互动结构自动产生）
        for e in special_dim["salient_events"]:
            if e.get("source") == idyn.SOURCE:
                assert e["event_class"] in (
                    "conversation_reengagement", "followup_sequence",
                    "invitation_progression")
        assert not any(e["event_class"] == "explicit_special_attention"
                       and e.get("source") == idyn.SOURCE
                       for e in special_dim["salient_events"])
        # 特殊关注的用户可见结论条数 = Jev 明确档消息数（不被互动事件放大）
        assert "出现 1 条明确特殊关注信号" in profile["summary"]["text"] or \
            special_dim["supporting_count"] == len(jev_events)


# ---------------------------------------------------------------------------
# §13/§14 报告内容 / 隐私 / 确定性
# ---------------------------------------------------------------------------


class TestReportChain:
    def test_report_sections_and_privacy(self):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        stats = scoring.compute_conversation_stats(results)
        payload = build_json_report(results, stats, include_text=False,
                                    interaction=interaction)
        dumped = json.dumps(payload, ensure_ascii=False)
        assert "relationship_profile" in payload
        assert "salience" in payload
        assert "interaction_dynamics" in payload
        profile = payload["relationship_profile"]
        assert profile["dimensions"]["special_attention"]["baseline"]
        assert payload["salience"]["salient_event_count"] >= 1
        assert payload["interaction_dynamics"]["events"]
        md = build_markdown_report(results, stats, include_text=False,
                                   interaction=interaction)
        for section in ("关系画像", "互动结构证据", "总体结果（辅助参考）"):
            assert section in md
        # 隐私：无昵称 / 指纹 / parser 元数据 / API key / 内部路径 / 完整正文
        for banned in ("raw_speaker", "fingerprint", "TYPESAFE_API_KEY",
                       "D:\\Documents\\", "我喜欢你", "就来嘛", "拔智齿"):
            assert banned not in dumped, banned
        assert "就来嘛" not in md and "拔智齿" not in md

    def test_report_determinism(self):
        messages, results, interaction, _s, _p = _e2e_pipeline()
        stats = scoring.compute_conversation_stats(results)
        first = build_json_report(results, stats, include_text=False,
                                  interaction=interaction)
        second = build_json_report(results, stats, include_text=False,
                                   interaction=interaction)
        first["metadata"].pop("generated_at")
        second["metadata"].pop("generated_at")
        assert first == second
        strip = lambda s: re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d", "", s)
        assert strip(build_markdown_report(results, stats, include_text=False,
                                           interaction=interaction)) == \
            strip(build_markdown_report(results, stats, include_text=False,
                                        interaction=interaction))

    def test_new_modules_do_not_expand_outbound_surface(self):
        """#17–#20 不得把新字段送出进程：build_state 仍是唯一出站允许清单。"""
        target = {"index": 0, "speaker": "them",
                  "text": "你好", "time": "2026-03-01 09:00",
                  "raw_speaker": "小安", "content_type": "text",
                  "media_kinds": []}
        entries = [{"index": 0, "speaker": "them",
                    "text": "你好", "time": "2026-03-01 09:00",
                    "raw_speaker": "小安", "content_type": "text",
                    "media_kinds": [],
                    "result": _result(_SPECIAL)}]
        messages = [dict(target)]
        interaction = build_interaction_events(messages, entries)
        state = analyzer.build_state([], target)
        dumped = json.dumps(state, ensure_ascii=False)
        for banned in ("raw_speaker", "fingerprint", "小安", "_chunk_idx"):
            assert banned not in dumped, banned
        # 新模块输出也不得出站（纯本地派生，无发送调用）
        dumped_new = json.dumps([interaction,
                                 build_profile(entries,
                                               interaction=interaction)],
                                ensure_ascii=False)
        assert "raw_speaker" not in dumped_new
        assert "小安" not in dumped_new


# ---------------------------------------------------------------------------
# §16 Jev request-count invariant
# ---------------------------------------------------------------------------


class TestJevRequestInvariant:
    def test_new_layers_add_zero_requests(self, monkeypatch):
        calls = []

        class CountingClient:
            def system_one(self, state, questions):
                calls.append(state)
                from types import SimpleNamespace as NS
                return NS(answers=_result(_DEFAULT), model="fake")

        import analyzer as an
        monkeypatch.setattr(an, "create_client", lambda key: CountingClient())
        messages = [{"speaker": "them", "text": f"消息{i}",
                     "time": f"2026-03-01 09:0{i}", "content_type": "text"}
                    for i in range(3)]
        entries = [{"index": i, "speaker": "them", "text": m["text"],
                    "time": m["time"], "content_type": "text",
                    "result": _result(_DEFAULT)} for i, m in enumerate(messages)]
        before = len(calls)
        results = an.analyze_messages(CountingClient(), [
            {"index": i, "speaker": "them", "text": m["text"],
             "time": m["time"], "content_type": "text", "context": []}
            for i, m in enumerate(messages)])
        assert len(calls) - before == 3      # 3 个 TA target = 3 次请求
        interaction = build_interaction_events(messages, results)
        salience_out = sal.build_salience(results, interaction=interaction)
        profile = build_profile(results, salience=salience_out,
                                interaction=interaction)
        # 三个新层之后请求数不变
        assert len(calls) - before == 3


# ---------------------------------------------------------------------------
# §17/§18/§19 cache / friend-history / 旧结果兼容
# ---------------------------------------------------------------------------


class TestDataCompatibility:
    def test_cached_result_reused_and_derivable(self, tmp_path, monkeypatch):
        import storage
        from storage import Cache

        class TmpCache(Cache):
            def __init__(self, *a, **k):
                super().__init__(tmp_path / "cache.db")

        monkeypatch.setattr(storage, "Cache", TmpCache)
        entry = {"index": 0, "speaker": "them", "text": "你好",
                 "time": "2026-03-01 09:00", "content_type": "text",
                 "context": []}
        store = TmpCache()
        result = _result(_SPECIAL)
        target = dict(entry)
        state = analyzer.build_state([], target)
        key = analyzer.make_cache_key(state, {}, "fake-model",
                                      analyzer.SCHEMA_VERSION)
        store.set(key, {"result": result, "cached": True})
        got = store.get(key)
        assert got is not None and got["result"] == result
        # v0.4 三层可从 cache 的 structured result 派生
        messages = [{"speaker": "them", "text": "你好",
                     "time": "2026-03-01 09:00", "content_type": "text"}]
        results = [{"index": 0, "speaker": "them", "text": "你好",
                    "time": "2026-03-01 09:00", "content_type": "text",
                    "context": [], "result": got["result"], "cached": True}]
        interaction = build_interaction_events(messages, results)
        profile = build_profile(results, interaction=interaction)
        assert profile["version"] == "relationship-profile-v2"
        assert interaction["version"] == idyn.INTERACTION_VERSION

    def test_old_results_missing_fields_degrade(self):
        # 旧缓存：缺 relational_ease / Noul 字段 / intent 概率 → 不 crash
        legacy_result = {
            "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                        "confidence": 0.7},
            "intent": {"choice": "other", "probabilities": {"other": 1.0},
                       "confidence": 0.7},
            "warmth": {"score": 2.0, "probabilities": {"2": 1.0},
                       "confidence": 0.7},
            "engagement": {"score": 2.0, "probabilities": {"2": 1.0},
                           "confidence": 0.7},
            "special_attention": {"score": 1.5,
                                  "probabilities": {"1": 0.5, "2": 0.5},
                                  "confidence": 0.7},
            "relationship_evidence_strength": {"score": 1.2,
                                               "probabilities": {"1": 1.0},
                                               "confidence": 0.7},
            "relational_ease": {"score": None, "probabilities": {},
                                "confidence": None},
            "romantic_signal": None,
            "distancing_signal": None,
        }
        results = [{"index": 0, "speaker": "them", "text": "旧消息",
                    "time": "2026-03-01 09:00", "context": [],
                    "result": legacy_result}]
        interaction = build_interaction_events(
            [{"speaker": "them", "text": "旧消息",
              "time": "2026-03-01 09:00", "content_type": "text"}], results)
        salience_out = sal.build_salience(results, interaction=interaction)
        profile = build_profile(results, salience=salience_out,
                                interaction=interaction)
        assert profile["dimensions"]["romantic"]["status"] in (
            rp.STATUS_INSUFFICIENT, rp.STATUS_EVIDENCE_LIMITED,
            rp.STATUS_UNSUPPORTED)
        stats = scoring.compute_conversation_stats(results)
        assert stats["analyzed"] == 0   # 旧缓存缺字段 → 不进入统计（不 crash）
        report = build_json_report(results, stats, include_text=False,
                                   interaction=interaction)
        assert report["relationship_profile"]["summary"]["lines"] is not None

    def test_error_entries_do_not_break_pipeline(self):
        results = [{"index": 0, "speaker": "them", "text": "x",
                    "time": "2026-03-01 09:00", "context": [],
                    "error": "boom"}]
        interaction = build_interaction_events(
            [{"speaker": "them", "text": "x",
              "time": "2026-03-01 09:00", "content_type": "text"}], results)
        salience_out = sal.build_salience(results, interaction=interaction)
        profile = build_profile(results, salience=salience_out,
                                interaction=interaction)
        assert profile["summary"]["text"]


# ---------------------------------------------------------------------------
# §33/§37 版本一致性与 ZIP 门禁（纯逻辑；Windows 包由 workflow 构建）
# ---------------------------------------------------------------------------


class TestReleaseGates:
    def test_version_is_semver(self):
        text = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        assert re.match(r"^\d+\.\d+\.\d+$", text), text

    def test_expected_zip_name_matches_version(self):
        import validate_release

        version = validate_release._repo_version()
        assert validate_release._expected_zip_name(version) == \
            f"SignalLens-v{version}-Windows-x64-portable.zip"

    def test_zip_content_guard_rejects_user_data(self, tmp_path):
        import validate_release

        version = validate_release._repo_version()
        zip_path = tmp_path / validate_release._expected_zip_name(version)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("SignalLens/SignalLens.exe", b"MZ")
            zf.writestr("SignalLens/_internal/base_library.zip", b"")
            for required in validate_release.REQUIRED_MEMBERS:
                if not required.endswith(".exe") \
                        and required != "SignalLens/_internal":
                    zf.writestr(required, "hi")
            zf.writestr("SignalLens/LICENSE", "lic")
            zf.writestr("SignalLens/VERSION", version)
            zf.writestr("SignalLens/data/cache.sqlite3", b"")   # \u7981\u6b62
        errors = validate_release.validate(zip_path, None)
        assert errors
        assert any("cache.sqlite3" in e for e in errors)

    def test_zip_content_guard_accepts_clean_package(self, tmp_path):
        import validate_release

        version = validate_release._repo_version()
        zip_path = tmp_path / validate_release._expected_zip_name(version)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("SignalLens/SignalLens.exe", b"MZ")
            zf.writestr("SignalLens/_internal/base_library.zip", b"")
            for required in validate_release.REQUIRED_MEMBERS:
                if not required.endswith(".exe") \
                        and required != "SignalLens/_internal":
                    zf.writestr(required, "hi")
            zf.writestr("SignalLens/LICENSE", "lic")
            zf.writestr("SignalLens/VERSION", version)
        errors = validate_release.validate(zip_path, None)
        # \u7f3a sidecar \u4f1a\u62a5\u9519\uff1b\u8865\u4e0a\u540e\u5e94\u5b8c\u5168\u901a\u8fc7
        digest = validate_release.hashlib.sha256(
            zip_path.read_bytes()).hexdigest()
        zip_path.with_name(zip_path.name + ".sha256").write_text(
            f"{digest}  {zip_path.name}\n", encoding="utf-8")
        assert validate_release.validate(zip_path, None) == []

    def test_spec_lists_all_runtime_modules(self):
        """release blocker\uff1aSignalLens.spec \u5fc5\u987b\u5217\u5168\u8fd0\u884c\u65f6\u6a21\u5757\u3002"""
        spec = (REPO_ROOT / "SignalLens.spec").read_text(encoding="utf-8")
        for module in ("relationship_profile.py", "salience.py",
                       "interaction_dynamics.py", "observer_advice.py", "timeline.py",
                       "scroll_anchor.py", "context_builder.py",
                       "friend_history.py", "behavior.py",
                       "longitudinal.py", "app.py"):
            assert f'"{module}"' in spec, module
        # \u7814\u7a76\u5de5\u5177\u4e0d\u5f97\u6253\u8fdb Portable
        for research in ("score_diagnostics.py", "salience_diagnostics.py",
                         "interaction_diagnostics.py", "evaluation.py"):
            assert f'"{research}"' not in spec, research

    def test_frozen_smoke_covers_module_imports(self):
        text = (REPO_ROOT / "scripts" / "frozen_smoke.py").read_text(
            encoding="utf-8")
        assert "--module-smoke" in text
        assert "_module_smoke" in text

    def test_launcher_exposes_module_smoke(self):
        text = (REPO_ROOT / "portable_launcher.py").read_text(
            encoding="utf-8")
        assert "MODULE_SMOKE_FLAG" in text
        assert "interaction_dynamics" in text

    def test_workflow_guards_tag_version(self):
        text = (REPO_ROOT / ".github" / "workflows"
                / "build-windows-portable.yml").read_text(encoding="utf-8")
        assert "tag version" in text and "VERSION" in text
        # workflow_dispatch \u4e0d\u5f97\u53d1\u5e03\u6b63\u5f0f Release
        assert "startsWith(github.ref, 'refs/tags/v')" in text


# ---------------------------------------------------------------------------
# gitignore / \u4e0d\u63d0\u4ea4\u6784\u5efa\u4ea7\u7269
# ---------------------------------------------------------------------------


class TestGitignore:
    def test_build_artifacts_ignored(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("dist/", "build/", "*.zip", "*.sha256",
                        ".release-test/"):
            assert pattern in text, pattern
