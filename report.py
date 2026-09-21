"""分析结果报告导出：Markdown / JSON / 摘要文本。

硬约束：
- 报告完全基于**已分析完成**的 results / stats 生成，不调用任何 Jev API；
- 不使用生成式 LLM，所有文字由确定性模板从现有指标生成；
- 不输出 API Key、SQLite 路径、异常堆栈等敏感信息；
- 隐私选项 include_text=False（默认）时，报告不含聊天原文。
"""

from __future__ import annotations

from datetime import datetime

from analyzer import DEFAULT_MODEL, EMOTION_LABELS, INTENT_OPTIONS, SCHEMA_VERSION
from scoring import (
    EFFECTIVE_MESSAGE_MIN_EVIDENCE,
    INTENT_PROFILE_LABELS,
    TREND_LABELS,
    evidence_level_label,
    message_metrics,
    total_evidence_label,
)

TOP_SIGNALS_MAX = 5          # “主要关系信号”最多列几条
BEHAVIOR_MIN_PERCENT = 10    # 摘要中只提占比 >= 10% 的行为
TOP_BEHAVIORS_IN_SUMMARY = 3


def report_filename(ext: str) -> str:
    """默认文件名，不含昵称（降低隐私风险）。"""
    return f"chat-analysis-{datetime.now().strftime('%Y-%m-%d-%H%M')}.{ext}"


# ---------------------------------------------------------------------------
# 摘要文本（确定性模板，适合直接复制）
# ---------------------------------------------------------------------------

def _score_level(value: float | None) -> str:
    if value is None:
        return "数据不足"
    if value >= 3.0:
        return "较高水平"
    if value >= 2.0:
        return "中等水平"
    if value >= 1.0:
        return "偏弱水平"
    return "很弱水平"


def build_summary_text(results: list[dict], stats: dict) -> str:
    analyzed = stats["analyzed"]
    effective = stats["effective_messages"]
    parts: list[str] = [
        f"本次共分析 {analyzed} 条 TA 消息，其中 {effective} 条包含较明确的关系信息。"
    ]

    if stats["overall"] is not None:
        trend_text = {"up": "前后互动呈上升态势", "flat": "前后互动基本稳定",
                      "down": "前后互动呈下降态势"}.get(stats["trend"])
        trend_sentence = f"{trend_text}。" if trend_text else ""
        parts.append(
            f"整体互动亲近信号指数为 {stats['overall']:.1f}/100，"
            f"关系信息量{total_evidence_label(stats['total_weight'])}。{trend_sentence}"
        )
    else:
        parts.append("当前样本缺少足够的关系层面信息，暂不生成可靠的互动亲近信号指数。")

    # 互动行为：按占比取前几位（仅解释层）
    profiles = stats.get("intent_profiles") or {}
    ranked = sorted(
        ((k, v) for k, v in profiles.items() if v * 100 >= BEHAVIOR_MIN_PERCENT),
        key=lambda kv: kv[1],
        reverse=True,
    )[:TOP_BEHAVIORS_IN_SUMMARY]
    if ranked:
        names = "、".join(INTENT_PROFILE_LABELS.get(k, k) for k, _ in ranked)
        parts.append(f"聊天以{names}为主。")
    else:
        parts.append("聊天中各类型互动行为分布较为分散。")

    parts.append(f"暧昧信号：{evidence_level_label(stats['romantic_evidence'])}。")
    parts.append(f"疏离信号：{evidence_level_label(stats['distancing_evidence'])}。")
    parts.append(
        f"温暖程度与投入程度处于{_score_level(stats['warmth_avg'])}"
        f"（温暖 {_fmt_opt(stats['warmth_avg'])}/4，投入 {_fmt_opt(stats['engagement_avg'])}/4），"
        f"特殊关注信号{_score_level(stats['special_attention_avg'])}"
        f"（{_fmt_opt(stats['special_attention_avg'])}/4）。"
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# JSON 报告
# ---------------------------------------------------------------------------

def _data_range(results: list[dict]) -> dict:
    times = [e.get("time") for e in results if e.get("time")]
    return {"first": times[0] if times else None,
            "last": times[-1] if times else None}


def build_json_report(
    results: list[dict], stats: dict, include_text: bool = False
) -> dict:
    messages = []
    for e in sorted(results, key=lambda e: e["index"]):
        if e.get("error"):
            continue
        m = message_metrics(e)
        if m is None:
            continue
        r = e["result"]
        messages.append({
            "index": e["index"],
            "time": e.get("time"),
            "speaker": e.get("speaker", "them"),
            "text": e.get("text") if include_text else None,
            "emotion": r["emotion"]["choice"],
            "intent": r["intent"]["choice"],
            "warmth": r["warmth"]["score"],
            "engagement": r["engagement"]["score"],
            "special_attention": r["special_attention"]["score"],
            "relationship_evidence_strength": r["relationship_evidence_strength"]["score"],
            "romantic_signal": {"raw": r["romantic_signal"], "evidence": m["romantic_ev"]},
            "distancing_signal": {"raw": r["distancing_signal"], "evidence": m["distancing_ev"]},
            "base_score": m["base_score"],
            "relation_confidence": m["relation_confidence"],
            "message_weight": m["weight"],
        })

    return {
        "metadata": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "model": DEFAULT_MODEL,
            "schema_version": SCHEMA_VERSION,
            "ta_messages": stats["analyzed"],
            "effective_messages": stats["effective_messages"],
            "failed_messages": stats["failed"],
            "data_range": _data_range(results),
            "include_text": include_text,
        },
        "summary": {"text": build_summary_text(results, stats)},
        "aggregate": {
            "overall": stats["overall"],
            "overall_sufficient": stats["overall_sufficient"],
            "recent": stats["recent"],
            "recent_sufficient": stats["recent_sufficient"],
            "first_half": stats["first_half"],
            "second_half": stats["second_half"],
            "trend": stats["trend"],
            "trend_label": TREND_LABELS.get(stats["trend"], stats["trend"]),
            "total_weight": stats["total_weight"],
            "evidence_level_total": total_evidence_label(stats["total_weight"]),
            "warmth_avg": stats["warmth_avg"],
            "engagement_avg": stats["engagement_avg"],
            "special_attention_avg": stats["special_attention_avg"],
            "romantic_evidence": stats["romantic_evidence"],
            "romantic_evidence_label": evidence_level_label(stats["romantic_evidence"]),
            "distancing_evidence": stats["distancing_evidence"],
            "distancing_evidence_label": evidence_level_label(stats["distancing_evidence"]),
            "romantic_raw_avg": stats["romantic_raw_avg"],
            "distancing_raw_avg": stats["distancing_raw_avg"],
            "warnings": stats["warnings"],
        },
        "behavior_stats": {
            INTENT_PROFILE_LABELS.get(k, k): v
            for k, v in (stats.get("intent_profiles") or {}).items()
        },
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Markdown 报告（适合保存到 Obsidian）
# ---------------------------------------------------------------------------

def _fmt(value, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _fmt_opt(value: float | None, digits: int = 1) -> str:
    return "数据不足" if value is None else f"{value:.{digits}f}"


def _message_label(e: dict, include_text: bool) -> str:
    if include_text:
        return f"“{e.get('text', '')}”"
    return f"TA 消息 #{e['index'] + 1}"


def _trend_section(stats: dict) -> str:
    lines = []
    if stats["trend"] == "insufficient_samples":
        lines.append("- 样本不足，暂不判断趋势。")
    elif stats["trend"] == "insufficient_evidence":
        lines.append("- 有效信息不足，暂不判断趋势。")
    else:
        lines.append(f"- 前半段：{_fmt(stats['first_half'])} / 100")
        lines.append(f"- 后半段：{_fmt(stats['second_half'])} / 100")
        lines.append(
            f"- 最近 10 条："
            f"{_fmt(stats['recent']) + ' / 100' if stats['recent_sufficient'] and stats['recent'] is not None else '近期有效关系信息不足'}"
        )
        lines.append(f"- 趋势：{TREND_LABELS.get(stats['trend'], stats['trend'])}")
    return "\n".join(lines)


def build_markdown_report(
    results: list[dict], stats: dict, include_text: bool = False
) -> str:
    analyzed = stats["analyzed"]
    effective = stats["effective_messages"]
    low_n = analyzed - effective

    lines: list[str] = ["# 聊天信号分析报告", ""]

    # ---- 基本信息 ----
    lines += ["## 基本信息", ""]
    lines.append(f"- 分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- Jev 模型：{DEFAULT_MODEL}")
    lines.append(f"- TA 消息数：{analyzed}")
    lines.append(f"- 有效关系消息：{effective}")
    dr = _data_range(results)
    lines.append(f"- 数据范围：{dr['first'] or '-'} ~ {dr['last'] or '-'}")
    lines.append(f"- 分析 schema：{SCHEMA_VERSION}")
    if stats["failed"]:
        lines.append(f"- 分析失败：{stats['failed']} 条")
    lines += [
        "",
        "> 本报告分析的是聊天文本中可观察到的互动信号，",
        "> 不代表对方真实心理状态，也不是“喜欢概率”。",
        "",
    ]

    # ---- 总体结果 ----
    lines += ["## 总体结果", ""]
    if stats["overall"] is not None:
        lines.append(f"- 互动亲近信号指数：{stats['overall']:.1f} / 100")
    else:
        lines.append("- 互动亲近信号指数：样本有效关系信息不足，暂不生成可靠指数")
    lines.append(f"- 关系信息量：{total_evidence_label(stats['total_weight'])}")
    lines.append(f"- 最近互动趋势：{TREND_LABELS.get(stats['trend'], stats['trend'])}")
    lines.append(f"- 温暖程度：{_fmt(stats['warmth_avg'])} / 4")
    lines.append(f"- 投入程度：{_fmt(stats['engagement_avg'])} / 4")
    lines.append(f"- 特殊关注：{_fmt(stats['special_attention_avg'])} / 4")
    lines.append(f"- 暧昧信号：{evidence_level_label(stats['romantic_evidence'])}")
    lines.append(f"- 疏离信号：{evidence_level_label(stats['distancing_evidence'])}")
    lines.append("")

    # ---- 整段互动行为 ----
    profiles = stats.get("intent_profiles") or {}
    if profiles:
        lines += ["## 整段互动行为", ""]
        for key, value in profiles.items():
            lines.append(f"- {INTENT_PROFILE_LABELS.get(key, key)}：{value * 100:.0f}%")
        lines += [
            "",
            "注：这些行为统计来自 intent 完整概率分布的聚合，",
            "当前仅用于解释，不参与互动亲近信号指数计算。",
            "",
        ]

    # ---- 主要关系信号（evidence 最高的若干条，只收有效消息）----
    ok_entries = [
        e for e in results
        if not e.get("error") and (m := message_metrics(e)) is not None
        and m["evidence"] >= EFFECTIVE_MESSAGE_MIN_EVIDENCE
    ]
    ok_entries.sort(key=lambda e: message_metrics(e)["evidence"], reverse=True)
    lines += ["## 主要关系信号", ""]
    if ok_entries:
        for rank, e in enumerate(ok_entries[:TOP_SIGNALS_MAX], start=1):
            m = message_metrics(e)
            r = e["result"]
            lines.append(f"### {rank}. {_message_label(e, include_text)}")
            lines.append(f"- 关系信息量：{m['evidence']:.1f} / 4")
            lines.append(f"- 聚合权重：{m['weight']:.2f}")
            top_emotion = max(r["emotion"]["probabilities"].items(), key=lambda kv: kv[1])
            top_intent = max(r["intent"]["probabilities"].items(), key=lambda kv: kv[1])
            lines.append(
                f"- 主要情绪：{EMOTION_LABELS.get(top_emotion[0], top_emotion[0])} "
                f"{top_emotion[1] * 100:.0f}%"
            )
            lines.append(
                f"- 主要意图：{INTENT_OPTIONS.get(top_intent[0], top_intent[0])} "
                f"{top_intent[1] * 100:.0f}%"
            )
            lines.append(f"- 温暖程度：{r['warmth']['score']:.1f} / 4")
            lines.append(f"- 投入程度：{r['engagement']['score']:.1f} / 4")
            lines.append(f"- 特殊关注：{r['special_attention']['score']:.1f} / 4")
            lines.append(f"- 暧昧信号：{evidence_level_label(m['romantic_ev'])}")
            lines.append(f"- 疏离信号：{evidence_level_label(m['distancing_ev'])}")
            lines.append("")
    else:
        lines += ["（没有可分析的成功消息）", ""]

    # ---- 低信息量消息 ----
    if low_n > 0:
        lines += [
            "## 低信息量消息",
            "",
            f"- 共 {low_n} 条消息关系信息量较低，因此对最终指数影响很小。",
            "",
        ]

    # ---- 趋势 ----
    lines += ["## 趋势", "", _trend_section(stats), ""]

    # ---- 摘要 ----
    lines += ["## 分析摘要", "", build_summary_text(results, stats), ""]

    # ---- 方法说明 ----
    lines += [
        "## 方法说明",
        "",
        "- **relationship_evidence_strength**：Jev Score（0~4），衡量单条消息的关系层面信息量；",
        "- **message_weight** = (evidence/4) × mean(warmth/engagement/special 的 confidence)，"
        "决定该条在聚合中的发言权；",
        "- **Noul evidence 转换**：原始概率 ≤0.30 视为无证据，0.30~0.70 为弱证据，"
        "≥0.70 为明确证据，romantic / distancing 共用；",
        "- **weighted aggregation**：总体指数与三项 Score 均值均按 message_weight 加权，"
        "低信息量短回复不会稀释结论；行为统计仅解释层，不计入总分。",
        "",
    ]
    return "\n".join(lines)
