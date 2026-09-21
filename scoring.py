"""互动亲近信号指数计算 —— 聚合逻辑 v2。

核心思想：
- 每条 TA 消息先算 base_score（“若这条消息有关系判断价值，它偏向什么方向”）；
- 再算 message_weight = 关系信息量 × Jev 置信度；
- 总体指数 = base_score 的加权平均（权重 = message_weight），
  而不是让所有消息（包括“哦”“嗯”这类低信息量短回复）等权稀释结论。

重要：该指数只表示聊天文本中**可观察到的**互动信号组合，
不代表对方真实心理状态，更不是“TA 喜欢我的概率”。

所有阈值集中在下面的配置区，不要在业务代码里散落 magic number。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# base_score 公式权重（与 v1 相同）
# ---------------------------------------------------------------------------
WEIGHT_WARMTH = 0.30
WEIGHT_ENGAGEMENT = 0.25
WEIGHT_SPECIAL_ATTENTION = 0.25
WEIGHT_ROMANTIC = 0.20
WEIGHT_DISTANCING_PENALTY = 0.15

SCORE_MAX = 4.0  # Score 为 5 级（0~4）

# ---------------------------------------------------------------------------
# Noul → evidence 转换参数（原始概率不直接当作现实程度）
# ---------------------------------------------------------------------------
NOUL_NOISE_FLOOR = 0.30   # <= 0.30：视为没有明确证据
NOUL_STRONG_MARK = 0.70   # >= 0.70：逐渐视为明确证据
NOUL_MID_GAIN = 0.35      # p=0.70 时的 evidence 值

# ---------------------------------------------------------------------------
# 聚合阈值
# ---------------------------------------------------------------------------
MIN_TOTAL_WEIGHT = 0.5        # Σ(message_weight) 低于此值 → 有效证据不足，不出分
MISSING_CONFIDENCE_FALLBACK = 0.5  # confidence 缺失时的替补值（并记录 warning）

TREND_MIN_SAMPLES = 6         # 可分析 TA 消息少于该值 → 趋势“样本不足”
TREND_DELTA_THRESHOLD = 8.0   # 后半段 − 前半段 >= ±8 记为上升 / 下降
TREND_HALF_MIN_WEIGHT = 0.5   # 任一半段 Σweight 低于此值 → 趋势“有效信息不足”
RECENT_WINDOW = 10            # “最近消息”窗口大小

EFFECTIVE_MESSAGE_MIN_EVIDENCE = 1.0  # relationship_evidence_strength >= 1 记为“有效消息”

# 总体 evidence 等级标签阈值
EVIDENCE_LEVEL_WEAK = 0.15
EVIDENCE_LEVEL_SOME = 0.35
EVIDENCE_LEVEL_CLEAR = 0.60
EVIDENCE_LEVEL_STRONG = 0.80

# 顶部“有效关系证据”总量标签阈值
EVIDENCE_TOTAL_HIGH = 3.0
EVIDENCE_TOTAL_MEDIUM = 1.0

# ---------------------------------------------------------------------------
# 低信息量展示模式（LOW_EVIDENCE_DISPLAY_MODE）
# 满足任一条件即进入：避免用大号总分制造“关系只有 XX 分”的错觉
# ---------------------------------------------------------------------------
LOW_EVIDENCE_MIN_EFFECTIVE = 2      # effective_messages < 2 → 低信息量展示
LOW_EVIDENCE_TOTAL_WEIGHT = 0.75    # total_weight < 0.75 → 低信息量展示

# ---------------------------------------------------------------------------
# 通用 Score 文字等级（warmth / engagement / special_attention 用）
# ---------------------------------------------------------------------------
SCORE_LEVEL_WEAK = 1.0        # < 1.0  → 弱
SCORE_LEVEL_BELOW = 1.5       # < 1.5  → 偏弱
SCORE_LEVEL_MID = 2.5         # < 2.5  → 一般
SCORE_LEVEL_STRONG = 3.25     # < 3.25 → 较强；否则 强

# ---------------------------------------------------------------------------
# relational_ease（互动熟悉度）文字等级 —— 专用标签，不复用上面的体系
# 仅解释层，不计入 base_score / message_weight / overall / recent / trend
# ---------------------------------------------------------------------------
EASE_LEVEL_DISTANT = 0.9      # < 0.9   → 较生疏
EASE_LEVEL_FORMAL = 1.7       # < 1.7   → 偏正式 / 熟悉度较低
EASE_LEVEL_NATURAL = 2.5      # < 2.5   → 自然熟悉
EASE_LEVEL_CLOSE = 3.3        # < 3.3   → 较熟悉、互动轻松；否则 高度熟悉 / 明显默契

# 行为摘要措辞阈值：>= 40% 才允许说“以 X 为主”
BEHAVIOR_DOMINANT_THRESHOLD = 0.40
BEHAVIOR_NOTABLE_THRESHOLD = 0.10  # 全部 < 10% → “没有特别突出的单一互动行为类型”

# ---------------------------------------------------------------------------
# 置信度 / 信号阈值（MVP 默认值，集中配置）
# ---------------------------------------------------------------------------
CHOICE_SCORE_CONFIDENT = 0.70   # >= 0.70：显示正常结论
CHOICE_SCORE_AMBIGUOUS = 0.45   # 0.45 ~ 0.70：存在一定歧义；< 0.45：难以判断

NOUL_STRONG = 0.70              # >= 0.70：明显存在该信号
NOUL_WEAK = 0.30                # 0.30 ~ 0.70：不确定；< 0.30：缺乏明显该信号

# conversation-level 行为统计展示用的 intent 键（解释层，不计入总分）
INTENT_PROFILE_LABELS: dict[str, str] = {
    "ask_information": "主动询问",
    "continue_topic": "延续话题",
    "show_care": "主动关心",
    "share_personal": "个人分享",
    "invite": "邀约",
    "tease": "调侃互动",
    "perfunctory": "低投入回应",
    "end_topic": "结束话题",
    "distance": "疏离意图",
}

TREND_LABELS: dict[str, str] = {
    "up": "↑ 上升",
    "flat": "→ 基本稳定",
    "down": "↓ 下降",
    "insufficient_samples": "样本不足",
    "insufficient_evidence": "有效信息不足",
}


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Noul 概率 → evidence 转换
# ---------------------------------------------------------------------------


def transform_noul_evidence(p: float | None) -> float:
    """把 Noul 原始概率转换为参与聚合的“证据强度”。

    - <= 0.30：视为没有明确证据（0）
    - 0.30 ~ 0.70：弱 / 不确定证据，线性升到 0.35
    - >= 0.70：逐渐视为明确证据，线性升到 1.0

    原始概率仍保留在 UI / debug 中展示，不进聚合公式。
    """
    if p is None:
        return 0.0
    p = clamp(float(p))
    if p <= NOUL_NOISE_FLOOR:
        return 0.0
    if p < NOUL_STRONG_MARK:
        span = NOUL_STRONG_MARK - NOUL_NOISE_FLOOR
        return ((p - NOUL_NOISE_FLOOR) / span) * NOUL_MID_GAIN
    span = 1.0 - NOUL_STRONG_MARK
    return NOUL_MID_GAIN + ((p - NOUL_STRONG_MARK) / span) * (1.0 - NOUL_MID_GAIN)


# ---------------------------------------------------------------------------
# 单条消息指标
# ---------------------------------------------------------------------------


def message_metrics(entry: dict) -> dict | None:
    """计算单条 TA 消息的 v2 指标。

    返回:
        {
            "base_score": 0~1,      # 若本条有判断价值时的方向分（聚合用）
            "weight": 0~1,          # message_weight = evidence_norm × relation_confidence
            "evidence": 0~4,        # relationship_evidence_strength 原始分
            "evidence_norm": 0~1,
            "relation_confidence": 0~1,  # warmth/engagement/special 三者 confidence 均值
            "romantic_ev": / "distancing_ev": 转换后证据,
            "romantic_raw": / "distancing_raw": Jev 原始概率（仅展示/debug）,
            "warmth"/"engagement"/"special_attention": 原始 Score,
            "relational_ease": 0~4,        # v2.1 解释层指标（不进任何总分公式）
            "relational_ease_probabilities": dict,
            "relational_ease_confidence": float | None,
            "intent_probabilities": dict,
            "warnings": [str],
        }
    分析失败、字段缺失（例如旧缓存结果没有 relational_ease 字段）时返回 None。
    """
    r = entry.get("result")
    if not r:
        return None
    try:
        warmth = float(r["warmth"]["score"])
        engagement = float(r["engagement"]["score"])
        special = float(r["special_attention"]["score"])
        evidence = float(r["relationship_evidence_strength"]["score"])
        relational_ease = float(r["relational_ease"]["score"])
        romantic_p = float(r["romantic_signal"])
        distancing_p = float(r["distancing_signal"])
        intent_probs = dict(r["intent"]["probabilities"])
    except (KeyError, TypeError, ValueError):
        return None

    warnings: list[str] = []
    confs: list[float] = []
    for name in ("warmth", "engagement", "special_attention"):
        c = r[name].get("confidence")
        if c is None:
            warnings.append(f"{name} 缺少 confidence，按 {MISSING_CONFIDENCE_FALLBACK} 处理")
            c = MISSING_CONFIDENCE_FALLBACK
        confs.append(float(c))
    relation_confidence = sum(confs) / len(confs)

    evidence_norm = clamp(evidence / SCORE_MAX)
    weight = clamp(evidence_norm * relation_confidence)

    romantic_ev = transform_noul_evidence(romantic_p)
    distancing_ev = transform_noul_evidence(distancing_p)

    base_score = clamp(
        WEIGHT_WARMTH * (warmth / SCORE_MAX)
        + WEIGHT_ENGAGEMENT * (engagement / SCORE_MAX)
        + WEIGHT_SPECIAL_ATTENTION * (special / SCORE_MAX)
        + WEIGHT_ROMANTIC * romantic_ev
        - WEIGHT_DISTANCING_PENALTY * distancing_ev
    )

    return {
        "base_score": base_score,
        "weight": weight,
        "evidence": evidence,
        "evidence_norm": evidence_norm,
        "relation_confidence": relation_confidence,
        "romantic_ev": romantic_ev,
        "distancing_ev": distancing_ev,
        "romantic_raw": romantic_p,
        "distancing_raw": distancing_p,
        "warmth": warmth,
        "engagement": engagement,
        "special_attention": special,
        "relational_ease": relational_ease,
        "relational_ease_probabilities": {
            str(k): v for k, v in dict(r["relational_ease"]["probabilities"]).items()
        },
        "relational_ease_confidence": r["relational_ease"].get("confidence"),
        "intent_probabilities": intent_probs,
        "warnings": warnings,
    }


def _weighted_mean(pairs: list[tuple[float, float]]) -> tuple[float | None, float]:
    """加权平均；总权重低于 MIN_TOTAL_WEIGHT 时返回 (None, total_weight)。"""
    total_weight = sum(w for _, w in pairs)
    if total_weight < MIN_TOTAL_WEIGHT:
        return None, total_weight
    return sum(v * w for v, w in pairs) / total_weight, total_weight


# ---------------------------------------------------------------------------
# 整段聚合
# ---------------------------------------------------------------------------


def compute_conversation_stats(results: list[dict]) -> dict:
    """整段聚合统计（v2：message_weight 加权）。

    返回字段见函数内 base 字典。overall / recent / 三项 Score 均值 /
    romantic / distancing evidence 全部使用 message_weight 加权；
    conversation-level intent 行为统计仅为解释层，**不计入总分**。
    """
    metrics = [
        (e["index"], m)
        for e in results
        if (m := message_metrics(e)) is not None
    ]
    analyzed = len(metrics)
    failed = sum(1 for e in results if e.get("error"))

    base: dict = {
        "overall": None,
        "overall_sufficient": False,
        "recent": None,
        "recent_sufficient": False,
        "first_half": None,
        "second_half": None,
        "trend": "insufficient_samples",
        "analyzed": analyzed,
        "failed": failed,
        "effective_messages": 0,
        "total_weight": 0.0,
        "warmth_avg": None,
        "engagement_avg": None,
        "special_attention_avg": None,
        "relational_ease_avg": None,   # v2.1 解释层：互动熟悉度（message_weight 加权）
        "romantic_evidence": None,
        "distancing_evidence": None,
        "romantic_raw_avg": None,
        "distancing_raw_avg": None,
        "intent_profiles": {},
        "warnings": sorted({w for _, m in metrics for w in m["warnings"]}),
    }
    if not metrics:
        return base

    base["effective_messages"] = sum(
        1 for _, m in metrics if m["evidence"] >= EFFECTIVE_MESSAGE_MIN_EVIDENCE
    )
    base["total_weight"] = sum(m["weight"] for _, m in metrics)
    base["romantic_raw_avg"] = sum(m["romantic_raw"] for _, m in metrics) / analyzed
    base["distancing_raw_avg"] = sum(m["distancing_raw"] for _, m in metrics) / analyzed

    # 总体 / 最近窗口（base_score 加权）
    base["overall"], _ = _weighted_mean(
        [(m["base_score"] * 100, m["weight"]) for _, m in metrics]
    )
    base["overall_sufficient"] = base["overall"] is not None
    recent_items = metrics[-RECENT_WINDOW:]
    base["recent"], recent_w = _weighted_mean(
        [(m["base_score"] * 100, m["weight"]) for _, m in recent_items]
    )
    base["recent_sufficient"] = recent_w >= MIN_TOTAL_WEIGHT

    # 三项 Score 加权均值（0~4），避免被大量低信息量短回复拉低
    for field, key in (
        ("warmth_avg", "warmth"),
        ("engagement_avg", "engagement"),
        ("special_attention_avg", "special_attention"),
    ):
        base[field], _ = _weighted_mean([(m[key], m["weight"]) for _, m in metrics])

    # romantic / distancing：聚合使用转换后的 evidence，message_weight 加权
    base["romantic_evidence"], _ = _weighted_mean(
        [(m["romantic_ev"], m["weight"]) for _, m in metrics]
    )
    base["distancing_evidence"], _ = _weighted_mean(
        [(m["distancing_ev"], m["weight"]) for _, m in metrics]
    )

    # 互动熟悉度（relational_ease，v2.1 解释层）：同样按 message_weight 加权，
    # 但**绝不进入** base_score / message_weight / overall / recent / trend。
    base["relational_ease_avg"], _ = _weighted_mean(
        [(m["relational_ease"], m["weight"]) for _, m in metrics]
    )

    # conversation-level intent 行为统计（完整概率分布加权，非 top-1 计数）
    if base["total_weight"] >= MIN_TOTAL_WEIGHT:
        for key in INTENT_PROFILE_LABELS:
            value, _ = _weighted_mean(
                [
                    (m["intent_probabilities"].get(key, 0.0), m["weight"])
                    for _, m in metrics
                ]
            )
            base["intent_profiles"][key] = value if value is not None else 0.0

    # 趋势：前半段 vs 后半段（均使用加权聚合）
    half = analyzed // 2
    if analyzed >= TREND_MIN_SAMPLES and half >= 1:
        first = metrics[:half]
        second = metrics[half:]
        first_w = sum(m["weight"] for _, m in first)
        second_w = sum(m["weight"] for _, m in second)
        if first_w < TREND_HALF_MIN_WEIGHT or second_w < TREND_HALF_MIN_WEIGHT:
            base["trend"] = "insufficient_evidence"
        else:
            first_score = sum(m["base_score"] * 100 * m["weight"] for _, m in first) / first_w
            second_score = sum(m["base_score"] * 100 * m["weight"] for _, m in second) / second_w
            base["first_half"] = first_score
            base["second_half"] = second_score
            delta = second_score - first_score
            if delta >= TREND_DELTA_THRESHOLD:
                base["trend"] = "up"
            elif delta <= -TREND_DELTA_THRESHOLD:
                base["trend"] = "down"
            else:
                base["trend"] = "flat"

    return base


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------


def confidence_label(confidence: float | None) -> str:
    """Choice / Score 置信度标签。"""
    if confidence is None:
        return "难以判断"
    if confidence >= CHOICE_SCORE_CONFIDENT:
        return "结论较明确"
    if confidence >= CHOICE_SCORE_AMBIGUOUS:
        return "存在一定歧义"
    return "难以判断"


def noul_label(value: float | None) -> str:
    """Noul 原始概率的信号强度标签（展示用，非聚合）。"""
    if value is None:
        return "不确定"
    if value >= NOUL_STRONG:
        return "明显存在该信号"
    if value >= NOUL_WEAK:
        return "不确定"
    return "缺乏明显该信号"


def evidence_level_label(value: float | None) -> str:
    """转换后 evidence 的整段文字等级（暧昧 / 疏离主展示）。"""
    if value is None:
        return "有效信息不足"
    if value < EVIDENCE_LEVEL_WEAK:
        return "未发现明显信号"
    if value < EVIDENCE_LEVEL_SOME:
        return "存在少量弱信号"
    if value < EVIDENCE_LEVEL_CLEAR:
        return "存在一定信号"
    if value < EVIDENCE_LEVEL_STRONG:
        return "存在较明显信号"
    return "存在强信号"


def total_evidence_label(total_weight: float) -> str:
    """顶部“有效关系证据”总量标签。"""
    if total_weight >= EVIDENCE_TOTAL_HIGH:
        return "较高"
    if total_weight >= EVIDENCE_TOTAL_MEDIUM:
        return "中等"
    return "较低"


# ---------------------------------------------------------------------------
# v2.1 解释层：文字等级 / 低信息量展示 / 排序 / 行为摘要措辞
# ---------------------------------------------------------------------------


def score_level_label(score: float | None) -> str:
    """通用 Score 文字等级（warmth / engagement / special_attention）。"""
    if score is None:
        return "数据不足"
    if score < SCORE_LEVEL_WEAK:
        return "弱"
    if score < SCORE_LEVEL_BELOW:
        return "偏弱"
    if score < SCORE_LEVEL_MID:
        return "一般"
    if score < SCORE_LEVEL_STRONG:
        return "较强"
    return "强"


def relational_ease_label(value: float | None) -> str:
    """互动熟悉度（relational_ease）专用文字等级。

    衡量自然 / 熟悉 / 轻松 / 默契，不等于喜欢、暧昧或特殊关注。
    """
    if value is None:
        return "有效关系信息不足"
    if value < EASE_LEVEL_DISTANT:
        return "较生疏"
    if value < EASE_LEVEL_FORMAL:
        return "偏正式 / 熟悉度较低"
    if value < EASE_LEVEL_NATURAL:
        return "自然熟悉"
    if value < EASE_LEVEL_CLOSE:
        return "较熟悉、互动轻松"
    return "高度熟悉 / 明显默契"


def is_low_evidence_display(stats: dict) -> bool:
    """是否进入 LOW_EVIDENCE_DISPLAY_MODE（弱化 overall 的视觉优先级）。

    满足任一条件即触发：
    - effective_messages < LOW_EVIDENCE_MIN_EFFECTIVE
    - total_weight < LOW_EVIDENCE_TOTAL_WEIGHT
    - 关系信息量标签为“较低”
    """
    return (
        stats["effective_messages"] < LOW_EVIDENCE_MIN_EFFECTIVE
        or stats["total_weight"] < LOW_EVIDENCE_TOTAL_WEIGHT
        or total_evidence_label(stats["total_weight"]) == "较低"
    )


def information_coverage(stats: dict) -> float | None:
    """信息覆盖率 = effective_messages / analyzed_messages（0~1）。"""
    if not stats["analyzed"]:
        return None
    return stats["effective_messages"] / stats["analyzed"]


def rank_relationship_signals(results: list[dict], max_n: int = 5) -> list[dict]:
    """按 relationship_evidence_strength × relation_confidence 降序取前 max_n 条。

    并列时保持原消息顺序（稳定排序）。只返回可分析且 evidence >= 1 的消息。
    """
    candidates = []
    for order, e in enumerate(results):
        if e.get("error"):
            continue
        m = message_metrics(e)
        if m is None or m["evidence"] < EFFECTIVE_MESSAGE_MIN_EVIDENCE:
            continue
        candidates.append((m["evidence"] * m["relation_confidence"], order, e, m))
    candidates.sort(key=lambda t: (-t[0], t[1]))
    return [
        {"entry": e, "metrics": m, "score": s} for s, _, e, m in candidates[:max_n]
    ]


def behavior_summary_text(profiles: dict) -> str:
    """行为统计的中性摘要措辞。

    - 某行为 >= BEHAVIOR_DOMINANT_THRESHOLD：允许“以 X 为主”；
    - 否则：“当前样本中相对更常见的互动信号包括：X、Y、Z”；
    - 全部 < BEHAVIOR_NOTABLE_THRESHOLD：“没有特别突出的单一互动行为类型”。
    """
    if not profiles:
        return "当前样本行为统计不足。"
    ranked = sorted(profiles.items(), key=lambda kv: kv[1], reverse=True)
    dominant = [(k, v) for k, v in ranked if v >= BEHAVIOR_DOMINANT_THRESHOLD]
    if dominant:
        names = "、".join(INTENT_PROFILE_LABELS.get(k, k) for k, _ in dominant[:3])
        return f"聊天以{names}为主。"
    notable = [(k, v) for k, v in ranked if v >= BEHAVIOR_NOTABLE_THRESHOLD]
    if not notable:
        return "当前样本中没有特别突出的单一互动行为类型。"
    names = "、".join(INTENT_PROFILE_LABELS.get(k, k) for k, _ in notable[:3])
    return f"当前样本中相对更常见的互动信号包括：{names}。"
