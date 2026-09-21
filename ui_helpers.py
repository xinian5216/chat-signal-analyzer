"""纯展示层辅助（UI-only）。

本模块只做“展示层转换”：短标签、徽章、媒体中性展示、消息过滤、
概览排版数据。**不包含任何评分公式，不修改分析结果**——
所有数值都来自 scoring / analyzer 已经算好的数据。

禁止把 business logic 迁进这里。
"""

from __future__ import annotations

from parser import MEDIA_KIND_LABELS, MEDIA_MARKERS
from scoring import (
    EFFECTIVE_MESSAGE_MIN_EVIDENCE,
    INTENT_PROFILE_LABELS,
    is_low_evidence_display,
    message_metrics,
    rank_relationship_signals,
)

# ---------------------------------------------------------------------------
# 短标签（避免窄列截断；完整说明由调用方放在 caption）
# ---------------------------------------------------------------------------

TREND_SHORT = {
    "up": "↑ 上升",
    "flat": "→ 稳定",
    "down": "↓ 下降",
    "insufficient_samples": "样本不足",
    "insufficient_evidence": "信息不足",
}

EVIDENCE_SHORT = {
    "未发现明显信号": "未发现",
    "存在少量弱信号": "弱信号",
    "存在一定信号": "一定信号",
    "存在较明显信号": "较明显",
    "存在强信号": "强信号",
    "有效信息不足": "信息不足",
}

MEDIA_ICONS = {
    "image": "🖼",
    "video": "🎬",
    "sticker": "💬",
    "voice": "🎙",
    "file": "📎",
}

BEHAVIOR_MIN_DISPLAY = 0.05   # 概览“互动方式”默认只显示占比 >= 5% 的项目


def trend_short(code: str) -> str:
    return TREND_SHORT.get(code, code)


def evidence_short(label: str) -> str:
    """完整 evidence 等级 → metric 用短文本（关键含义不丢失）。"""
    return EVIDENCE_SHORT.get(label, label)


def media_badge(kinds: list[str]) -> str:
    """媒体类型徽章，如 “🖼 图片”。"""
    if not kinds:
        return "媒体"
    icons = "".join(MEDIA_ICONS.get(k, "📎") for k in kinds)
    names = " / ".join(MEDIA_KIND_LABELS.get(k, k) for k in kinds)
    return f"{icons} {names}"


def media_placeholder_label(kinds: list[str]) -> str:
    """预览表 / 消息卡中媒体内容列的脱敏展示（绝不出现本地文件名）。"""
    names = " / ".join(MEDIA_KIND_LABELS.get(k, k) for k in kinds) or "媒体"
    return f"[{names}，内容未分析]"


def media_counts(messages: list[dict]) -> dict[str, int]:
    """按类型统计纯媒体消息数量（只含 content_type == "media"）。"""
    counts: dict[str, int] = {}
    for m in messages:
        if m.get("content_type") != "media":
            continue
        for k in m.get("media_kinds") or []:
            counts[k] = counts.get(k, 0) + 1
    return counts


def media_summary_text(messages: list[dict]) -> str:
    """“图片 3 · 视频 1 · 动画表情 2”；无媒体时返回空串。"""
    counts = media_counts(messages)
    if not counts:
        return ""
    return " · ".join(
        f"{MEDIA_KIND_LABELS.get(k, k)} {n}" for k, n in counts.items()
    )


def skipped_media_count(messages: list[dict]) -> int:
    """TA 侧被跳过分析的纯媒体消息数（我方 / unknown 媒体本就不是 target）。"""
    return sum(
        1 for m in messages
        if m.get("speaker") == "them" and m.get("content_type") == "media"
    )


def content_type_label(m: dict) -> str:
    """预览表“类型”列。"""
    if m.get("content_type") == "media":
        return " / ".join(MEDIA_KIND_LABELS.get(k, k)
                          for k in m.get("media_kinds") or []) or "媒体"
    if m.get("content_type") == "mixed":
        return "文字+媒体"
    return "文本"


def preview_rows(messages: list[dict], limit: int = 15) -> list[dict]:
    """解析预览表行。媒体内容列使用中性脱敏展示，不暴露本地文件名。"""
    rows = []
    for idx, m in enumerate(messages[:limit]):
        if m.get("content_type") == "media":
            content = media_placeholder_label(m.get("media_kinds") or [])
        else:
            content = m["text"].replace("\n", " ⏎ ")
            if len(content) > 50:
                content = content[:50] + "…"
        rows.append({
            "#": idx + 1,
            "发言人": m["speaker"],
            "时间": m.get("time") or "-",
            "类型": content_type_label(m),
            "内容": content,
        })
    return rows


def media_event_entry(m: dict) -> dict:
    """把纯媒体消息转成“媒体事件”展示项（无任何分析指标）。"""
    return {
        "index": m.get("index"),
        "speaker": m.get("speaker", "them"),
        "text": MEDIA_MARKERS.get(
            (m.get("media_kinds") or ["image"])[0], "[媒体，内容未分析]"
        ),
        "time": m.get("time"),
        "media_kinds": m.get("media_kinds") or [],
        "media_event": True,
    }


def filter_entries(
    results: list[dict],
    only_effective: bool = False,
    mode: str = "全部消息",
    include_media: bool = False,
    messages: list[dict] | None = None,
) -> list[dict]:
    """纯 UI 过滤：只改变展示，绝不修改分析结果。

    - mode="仅有效关系消息" 或 only_effective=True → 只留 evidence >= 1 的消息；
    - mode="关系信息量最高 Top 5" → 按 evidence × confidence 取前 5（恢复原序）；
    - include_media=True → 追加媒体事件展示项（不带任何分析指标）。
    """
    entries = sorted(results, key=lambda e: e["index"])
    if only_effective or mode == "仅有效关系消息":
        entries = [
            e for e in entries
            if (m := message_metrics(e)) is not None
            and m["evidence"] >= EFFECTIVE_MESSAGE_MIN_EVIDENCE
        ]
    elif mode == "关系信息量最高 Top 5":
        entries = [item["entry"] for item in rank_relationship_signals(results, max_n=5)]
        entries.sort(key=lambda e: e["index"])

    if include_media and messages:
        known = {e["index"] for e in entries}
        events = [
            media_event_entry({**m, "index": i})
            for i, m in enumerate(messages)
            if m.get("speaker") == "them"
            and m.get("content_type") == "media"
            and i not in known
        ]
        entries = sorted(entries + events, key=lambda e: e["index"] or 0)
    return entries


def visible_behaviors(
    profiles: dict, min_pct: float = BEHAVIOR_MIN_DISPLAY
) -> list[tuple[str, float]]:
    """概览“互动方式”默认展示项：占比 >= min_pct，按占比降序。"""
    items = [(k, v) for k, v in profiles.items() if v >= min_pct]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return [(INTENT_PROFILE_LABELS.get(k, k), v) for k, v in items]


def hidden_behavior_count(
    profiles: dict, min_pct: float = BEHAVIOR_MIN_DISPLAY
) -> int:
    return sum(1 for v in profiles.values() if v < min_pct)


def overview_mode(stats: dict) -> str:
    """"reference"（低信息量）或 "normal"。"""
    return "reference" if is_low_evidence_display(stats) else "normal"
