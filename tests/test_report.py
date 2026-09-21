"""报告导出测试：Markdown / JSON / 摘要，全部基于 mock 数据，不调用 Jev API。"""

import json

from report import (
    build_json_report,
    build_markdown_report,
    build_summary_text,
    report_filename,
)
from scoring import compute_conversation_stats


def make_result(warmth=2.0, engagement=2.0, special=1.5,
                romantic=0.2, distancing=0.2, evidence=2.4, ease=2.3,
                emotion="teasing", intent="tease"):
    return {
        "emotion": {"choice": emotion, "probabilities": {emotion: 0.93, "calm": 0.07},
                    "confidence": 0.85},
        "intent": {"choice": intent, "probabilities": {intent: 0.91, "other": 0.09},
                   "confidence": 0.88},
        "warmth": {"score": warmth, "probabilities": {}, "confidence": 0.8},
        "engagement": {"score": engagement, "probabilities": {}, "confidence": 0.8},
        "special_attention": {"score": special, "probabilities": {}, "confidence": 0.8},
        "relationship_evidence_strength": {"score": evidence, "probabilities": {},
                                           "confidence": 0.8},
        "relational_ease": {"score": ease, "probabilities": {}, "confidence": 0.8},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
    }


def make_entries(n=6):
    entries = []
    for i in range(n):
        if i % 3 == 0:  # 低信息量短回复
            entries.append({"index": i, "speaker": "them", "time": f"22:3{i}",
                            "text": "哦哦",
                            "result": make_result(warmth=0.5, engagement=0.5, special=0.2,
                                                  evidence=0.2)})
        else:
            entries.append({"index": i, "speaker": "them", "time": f"22:3{i}",
                            "text": f"在干嘛呢{i}",
                            "result": make_result()})
    return entries


def sample_stats():
    results = make_entries()
    return results, compute_conversation_stats(results)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def test_markdown_structure():
    results, stats = sample_stats()
    md = build_markdown_report(results, stats, include_text=True)
    for section in ("# 聊天信号分析报告", "## 基本信息", "## 总体结果",
                    "## 整段互动行为", "## 主要关系信号", "## 低信息量消息",
                    "## 趋势", "## 分析摘要", "## 方法说明"):
        assert section in md
    assert "v2" in md  # schema 版本
    assert "不代表对方真实心理状态" in md


def test_markdown_includes_text_when_enabled():
    results, stats = sample_stats()
    md = build_markdown_report(results, stats, include_text=True)
    assert "在干嘛呢1" in md
    assert "TA 消息 #" not in md  # 包含原文时不用编号替代


def test_markdown_hides_text_by_default():
    results, stats = sample_stats()
    md = build_markdown_report(results, stats, include_text=False)
    assert "在干嘛呢1" not in md
    assert "哦哦" not in md
    assert "TA 消息 #2" in md


def test_markdown_insufficient_samples_trend():
    results, stats = sample_stats()
    stats["trend"] = "insufficient_samples"
    md = build_markdown_report(results, stats)
    assert "样本不足，暂不判断趋势。" in md


def test_markdown_no_llm_flavor_words():
    results, stats = sample_stats()
    md = build_markdown_report(results, stats)
    # 不得出现超出数据证据的结论
    for banned in ("不喜欢你", "好感", "只是普通朋友", "喜欢你的概率"):
        assert banned not in md


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def test_json_schema_and_validity():
    results, stats = sample_stats()
    data = build_json_report(results, stats, include_text=False)
    text = json.dumps(data, ensure_ascii=False)  # 必须可序列化
    assert json.loads(text) == data
    assert set(data) == {"metadata", "summary", "aggregate", "behavior_stats", "messages"}
    m = data["messages"][0]
    for field in ("time", "speaker", "text", "emotion", "intent", "warmth",
                  "engagement", "special_attention", "relationship_evidence_strength",
                  "romantic_signal", "distancing_signal", "base_score",
                  "relation_confidence", "message_weight"):
        assert field in m
    assert set(m["romantic_signal"]) == {"raw", "evidence"}
    assert "v2" in data["metadata"]["schema_version"]


def test_json_respects_privacy_option():
    results, stats = sample_stats()
    with_text = build_json_report(results, stats, include_text=True)
    without = build_json_report(results, stats, include_text=False)
    assert with_text["messages"][0]["text"] == results[0]["text"]
    assert all(m["text"] is None for m in without["messages"])


def test_json_has_no_secrets():
    results, stats = sample_stats()
    text = json.dumps(build_json_report(results, stats), ensure_ascii=False)
    for banned in ("api_key", "Authorization", "Bearer", "cache.db", ".jev_cache"):
        assert banned not in text


# ---------------------------------------------------------------------------
# 摘要文本
# ---------------------------------------------------------------------------

def test_summary_text_is_dynamic_and_safe():
    results, stats = sample_stats()
    text = build_summary_text(results, stats)
    assert "本次共分析 6 条 TA 消息" in text
    assert "互动亲近信号指数" in text
    for banned in ("不喜欢你", "只是普通朋友", "好感"):
        assert banned not in text


def test_summary_text_insufficient_evidence():
    results = [{"index": 0, "speaker": "them", "time": None, "text": "哦",
                "result": make_result(evidence=0.2, warmth=0.2, engagement=0.2,
                                      special=0.1)}]
    stats = compute_conversation_stats(results)
    text = build_summary_text(results, stats)
    assert "暂不生成可靠的互动亲近信号指数" in text


# ---------------------------------------------------------------------------
# 文件名
# ---------------------------------------------------------------------------

def test_filename_has_no_nickname():
    name = report_filename("md")
    assert name.startswith("chat-analysis-") and name.endswith(".md")
    assert len(name.split("-")) >= 4  # 日期时间戳


# ---------------------------------------------------------------------------
# v2.1：relational_ease 与低信息量免责声明
# ---------------------------------------------------------------------------


def test_json_contains_relational_ease():
    results, stats = sample_stats()
    data = build_json_report(results, stats, include_text=True)
    assert "relational_ease_avg" in data["aggregate"]
    assert "relational_ease_label" in data["aggregate"]
    assert "information_coverage" in data["aggregate"]
    assert "low_evidence_display" in data["aggregate"]
    m = data["messages"][0]
    assert set(m["relational_ease"]) == {"score", "probabilities", "confidence"}


def test_json_aggregate_labels_consistent():
    results, stats = sample_stats()
    data = build_json_report(results, stats)
    ease = data["aggregate"]["relational_ease_avg"]
    if ease is not None:
        assert data["aggregate"]["relational_ease_label"] in (
            "较生疏", "偏正式 / 熟悉度较低", "自然熟悉",
            "较熟悉、互动轻松", "高度熟悉 / 明显默契",
        )


def test_markdown_low_evidence_disclaimer():
    # 1 条有效 + 7 条低信息量 → 低信息量展示模式
    results = [{"index": 0, "speaker": "them", "time": None, "text": "你比较重要",
                "result": make_result(evidence=3.0)}]
    results += [
        {"index": i, "speaker": "them", "time": None, "text": "哦哦",
         "result": make_result(evidence=0.2, warmth=0.5, engagement=0.5, special=0.2)}
        for i in range(1, 8)
    ]
    stats = compute_conversation_stats(results)
    md = build_markdown_report(results, stats, include_text=True)
    assert "当前样本关系信息量较低，互动亲近信号指数仅作参考" in md
    assert "不建议据此判断整体关系亲近程度" in md
    assert "（参考）" in md
    assert "互动熟悉度" in md
    assert "信息覆盖率" in md


def test_markdown_normal_mode_has_no_low_evidence_disclaimer():
    results = [{"index": i, "speaker": "them", "time": None, "text": f"m{i}",
                "result": make_result(evidence=3.0)} for i in range(6)]
    stats = compute_conversation_stats(results)
    md = build_markdown_report(results, stats)
    assert "当前样本关系信息量较低" not in md
    assert "（参考）" not in md


def test_summary_uses_neutral_behavior_wording():
    results = make_entries(6)
    stats = compute_conversation_stats(results)
    stats["intent_profiles"] = {"share_personal": 0.20, "continue_topic": 0.17,
                                "tease": 0.12}
    text = build_summary_text(results, stats)
    assert "相对更常见的互动信号包括" in text
    assert "为主" not in text


def test_summary_uses_score_level_labels():
    results = make_entries(6)
    stats = compute_conversation_stats(results)
    stats["warmth_avg"] = 1.7
    stats["engagement_avg"] = 2.1
    stats["special_attention_avg"] = 1.0
    text = build_summary_text(results, stats)
    assert "温暖程度一般" in text
    assert "投入程度一般" in text
    assert "特殊关注偏弱" in text


def test_summary_mentions_ease_without_overreach():
    results = make_entries(6)
    stats = compute_conversation_stats(results)
    stats["relational_ease_avg"] = 2.3
    text = build_summary_text(results, stats)
    assert "互动熟悉度自然熟悉" in text
    assert "不等于浪漫兴趣或特殊关注" in text
