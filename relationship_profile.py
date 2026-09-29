"""Relationship Signal Profile v2（Issue #18）——多维关系画像，纯业务层。

职责：把现有 Jev structured results（`chat-signal-v3.3`）派生为**可解释、多维、
证据优先**的关系画像：每个维度分别给出 strength / coverage / direction /
reliability / supporting evidence / counter evidence，并把 insufficient /
unsupported 作为一等状态。

硬边界（与 Issue #18 一致，违者即 bug）：

- **没有新的综合分数**：不产生任何跨维度加权总分；不把维度折算成 0~100 互比；
- **不修改生产算法**：只读调用 `scoring.py` 现有函数与常量；
  不改 Noul transform / message_weight / weighted mean / legacy overall；
- **evidence ≠ direction**：`relationship_evidence_strength` 只作覆盖 / 信息量，
  绝不决定 supporting / counter 方向；
- **absence ≠ counter**：低分 / 未发现 / 不确定都不是反证；counter 只在量表
  的明确负向行为档开启（详见 `docs/issue18-relationship-profile-v2.md`）；
- **不偷做数值聚合**：#19 的 salience 通道（`salience.py`）只以**分类事件**
  （salient_events / counter_events）注入本模块，绝不产生 top-k / percentile /
  boost / 新权重 / 新综合分数；本模块自身不计算 salience；
- **不偷做 #20**：不产生话题开启 / 追问 / 跨日重提 / 邀约推进 / reciprocity /
  拒绝后反应等 turn-level 事件，`interaction_events` 仅预留接口；
- **确定性文案**：所有用户可见文字由模板生成，无生成式 LLM、无心理脑补；
- **pure**：无 Streamlit 依赖、无网络、无文件副作用。

D5 directionality 限制（#17 发现，本模块只如实表达，不修）：高信息量 ≠ 正向
关系信号；`base_score` 缺少边界施压 / 无视拒绝的负方向通道，因此：
- 不得用高 engagement 推断“尊重边界”；
- 不得用 distancing 低推断“没有边界压力”；
- 不得用 overall 高推断“关系健康”；
- 边界压力维度恒为 `unsupported`，等 #20 的 boundary response 事件。
"""

from __future__ import annotations

import math

import scoring
from analyzer import SCHEMA_VERSION

PROFILE_VERSION = "relationship-profile-v2"

# 统一 evidence 来源类型（#18 只使用前两种；其余为 #19 / #20 预留）
EVIDENCE_SOURCE_TYPES = (
    "jev_metric",
    "intent_probability",
    "interaction_event",        # #20 预留
    "salient_event",            # #19 预留
    "manual_confirmed_event",   # behavior.py 人工确认事件，预留
)

# ---------------------------------------------------------------------------
# 阈值（集中配置；复用生产锚点 + 2 个新增，依据见设计文档 §4）
# ---------------------------------------------------------------------------

SUPPORT_MIN = 3.0                 # Score 量表第 4 档起点（明显 / 主动 / 超出普通社交）
# 反向档（counter）按量表的**负向行为档**逐维对齐（严格小于）：
# - engagement 第 0~1 档（明确拒绝继续 / 敷衍）= 可观察的低投入行为 → < 2.0
# - warmth 第 0 档（明显冷淡、疏离或拒绝）= 可观察的冷淡；第 1 档（中性/纯事务）
#   只是缺证据 → < 1.0
# - relational_ease 第 0 档（明显陌生、拘谨）= 可观察的生疏；第 1 档同上 → < 1.0
INITIATIVE_COUNTER_BELOW = 2.0
CARE_COUNTER_BELOW = 1.0
FAMILIARITY_COUNTER_BELOW = 1.0
ROMANTIC_CLEAR_MIN = scoring.NOUL_STRONG_MARK      # 0.70 明确 Noul 证据
ROMANTIC_WEAK_MIN = scoring.NOUL_NOISE_FLOOR       # 0.30 弱 Noul 痕迹下限
RELIABILITY_HIGH_MIN_CONF = scoring.CHOICE_SCORE_CONFIDENT     # 0.70
RELIABILITY_MID_MIN_CONF = scoring.CHOICE_SCORE_AMBIGUOUS      # 0.45
RELIABILITY_MIN_EFFECTIVE = scoring.LOW_EVIDENCE_MIN_EFFECTIVE  # 2
RELIABILITY_HIGH_MIN_MESSAGES = 3   # 新增：≥3 条独立有效消息才允许“较高”
RELIABILITY_MIN_COVERAGE = 0.5      # 新增：覆盖率 < 0.5 时可靠性封顶“中等”
FACET_DOMINANT = scoring.BEHAVIOR_DOMINANT_THRESHOLD   # 0.40
FACET_NOTABLE = scoring.BEHAVIOR_NOTABLE_THRESHOLD     # 0.10

DIMENSION_ORDER = (
    "initiative_engagement",
    "care_responsiveness",
    "familiarity",
    "special_attention",
    "romantic",
    "withdrawal",
    "boundary_pressure",
)

DIMENSION_LABELS = {
    "initiative_engagement": "主动性与投入",
    "care_responsiveness": "关心与回应性",
    "familiarity": "互动熟悉度",
    "special_attention": "特殊关注",
    "romantic": "浪漫 / 暧昧信号",
    "withdrawal": "关系疏离 / 后撤",
    "boundary_pressure": "边界压力",
}

# status / direction / reliability 枚举（字符串常量，便于测试锚定）
STATUS_SUFFICIENT = "sufficient"
STATUS_INSUFFICIENT = "insufficient"
# 审计修复（#18 语义审计）：明确证据存在但覆盖有限 ≠ 数据不足
STATUS_EVIDENCE_LIMITED = "evidence_limited"
STATUS_UNSUPPORTED = "unsupported"

# 明确档门槛（复用量表第 0 档边界）：用于区分“单条强证据”与“单条普通证据”。
# 只有量表明确档（Score 明显档 ≥ 3.0 / Noul 明确档 ≥ 0.70 / 严格负向档 < 1.0）
# 才能解锁 evidence_limited；第 1 档弱观察（如 engagement 1.8）不算明确证据。
EXPLICIT_NEG_BELOW = 1.0

DIR_SUPPORTING = "supporting"
DIR_COUNTER = "counter"
DIR_MIXED = "mixed"
DIR_NONE = "none"
DIR_UNKNOWN = "unknown"

ROLE_SUPPORTING = "supporting"
ROLE_COUNTER = "counter"
ROLE_WEAK_TRACE = "weak_trace"

CONF_LEVEL_HIGH = "较高"
CONF_LEVEL_MID = "中等"
CONF_LEVEL_LOW = "较低"
CONF_LEVEL_NONE = "不足"
CONF_LEVEL_NA = "不适用"


# ---------------------------------------------------------------------------
# 确定性小工具
# ---------------------------------------------------------------------------


def distribution_variance(probabilities: dict) -> float | None:
    """Score 分布方差 Var[i]（确定性数学函数；缺失 / 非法返回 None）。"""
    try:
        items = [(int(k), float(v)) for k, v in probabilities.items()]
    except (TypeError, ValueError):
        return None
    if not items or any(v < 0 for _, v in items):
        return None
    total = sum(v for _, v in items)
    if total <= 0:
        return None
    mean = sum(k * v for k, v in items) / total
    return sum(v * (k - mean) ** 2 for k, v in items) / total


def distribution_entropy_bits(probabilities: dict) -> float | None:
    """Score / Choice 分布熵 H = −Σ p·log2 p（只表达模糊程度，不是准确率）。"""
    try:
        values = [float(v) for v in probabilities.values()]
    except (TypeError, ValueError):
        return None
    if not values or any(v < 0 for v in values):
        return None
    total = sum(values)
    if total <= 0:
        return None
    entropy = 0.0
    for v in values:
        p = v / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def _round(value, digits: int = 4):
    return None if value is None else round(float(value), digits)


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------------------
# 消息迭代（只读生产 message_metrics）
# ---------------------------------------------------------------------------


def _collect_items(results: list[dict]) -> tuple[list[dict], int]:
    """可分析消息列表 + 因字段缺失被排除的数量（旧缓存降级，不 crash）。"""
    items: list[dict] = []
    unavailable = 0
    for entry in sorted(results, key=lambda e: e["index"]):
        if entry.get("error"):
            continue
        metrics = scoring.message_metrics(entry)
        if metrics is None:
            unavailable += 1
            continue
        items.append({
            "index": entry["index"],
            "result": entry["result"],
            "metrics": metrics,
        })
    return items, unavailable


def _dim_confidence(result: dict, dim: str) -> float | None:
    block = result.get(dim)
    if isinstance(block, dict):
        conf = block.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            return float(conf)
    return None


def _dim_probabilities(result: dict, dim: str) -> dict:
    block = result.get(dim)
    if isinstance(block, dict) and isinstance(block.get("probabilities"), dict):
        return dict(block["probabilities"])
    return {}


def _evidence_item(item: dict, metric: str, value, confidence, role: str,
                   source: str, reason: str) -> dict:
    return {
        "message_index": item["index"],
        "metric": metric,
        "value": _round(value),
        "confidence": _round(confidence),
        "role": role,
        "source": source,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 维度骨架（strength / coverage / direction / reliability 的统一装配）
# ---------------------------------------------------------------------------


def _coverage(effective: list[dict], supporting_count: int,
              analyzed_total: int) -> dict:
    eligible = len(effective)
    return {
        "eligible_messages": eligible,
        "supporting_messages": supporting_count,
        "analyzed_messages": analyzed_total,
        "coverage_ratio": _round(eligible / analyzed_total) if analyzed_total else None,
    }


def _direction(supporting_count: int, counter_count: int,
               counter_available: bool, status: str) -> str:
    if status not in (STATUS_SUFFICIENT, STATUS_EVIDENCE_LIMITED):
        return DIR_UNKNOWN
    if supporting_count and counter_count:
        return DIR_MIXED
    if supporting_count:
        return DIR_SUPPORTING
    if counter_count:
        return DIR_COUNTER
    return DIR_NONE


def _explicit_count(supporting: list[dict], counter: list[dict]) -> int:
    """明确档证据条数：supporting（明显档 / Noul 明确档）+ 严格负向档 counter。

    只用于区分“单条非常明确的 observable signal”与“单条普通 / 弱观察”：
    第 1 档弱观察（如 engagement 1.8 敷衍档）不解锁 evidence_limited，
    minimum sample protection 不被取消，只是不再抹掉明确证据。
    """
    strict_counter = sum(1 for item in counter
                         if item.get("value") is not None
                         and item["value"] < EXPLICIT_NEG_BELOW)
    return len(supporting) + strict_counter


def _mark_limited_reliability(reliability: dict) -> dict:
    if reliability["level"] == CONF_LEVEL_HIGH:
        reliability["level"] = CONF_LEVEL_MID
    reliability["basis"].append("覆盖有限，需更多样本确认整体模式")
    return reliability


def _reliability_from_confidence(eligible: int, coverage: dict,
                                 confidences: list[float],
                                 missing_conf: int,
                                 status: str | None = None) -> dict:
    """置信度 + 覆盖封顶的可靠性（绝不简单相乘隐藏两者）。"""
    basis: list[str] = []
    confidence = _mean(confidences)
    if confidence is None:
        level = CONF_LEVEL_LOW
        basis.append("判断置信度缺失，可靠性按较低处理")
    else:
        if confidence >= RELIABILITY_HIGH_MIN_CONF:
            level = CONF_LEVEL_HIGH
        elif confidence >= RELIABILITY_MID_MIN_CONF:
            level = CONF_LEVEL_MID
        else:
            level = CONF_LEVEL_LOW
        basis.append(f"消息级判断置信度均值 {confidence:.2f}")
    basis.append(f"有效证据 {eligible} 条"
                 f"（覆盖 {coverage['eligible_messages']}/"
                 f"{coverage['analyzed_messages']}）")
    if missing_conf:
        basis.append(f"{missing_conf} 条消息缺少置信度字段")
    if eligible < RELIABILITY_MIN_EFFECTIVE:
        level = CONF_LEVEL_LOW
        basis.append("有效证据不足 2 条，可靠性封顶“较低”")
    else:
        if eligible < RELIABILITY_HIGH_MIN_MESSAGES and level == CONF_LEVEL_HIGH:
            level = CONF_LEVEL_MID
            basis.append("独立有效消息少于 3 条，可靠性不高于“中等”")
        ratio = coverage["coverage_ratio"]
        if ratio is not None and ratio < RELIABILITY_MIN_COVERAGE \
                and level == CONF_LEVEL_HIGH:
            level = CONF_LEVEL_MID
            basis.append("证据覆盖率低于 50%，可靠性不高于“中等”")
    reliability = {"level": level, "confidence": _round(confidence),
                   "proxy": False, "basis": basis}
    if status == STATUS_EVIDENCE_LIMITED:
        _mark_limited_reliability(reliability)
    return reliability


def _reliability_proxy(eligible: int, coverage: dict, note: str,
                       status: str | None = None) -> dict:
    """Noul 维度可靠性代理：官方无 confidence 字段，只按覆盖，最高“中等”。"""
    basis = [note,
             f"有效证据 {eligible} 条"
             f"（覆盖 {coverage['eligible_messages']}/"
             f"{coverage['analyzed_messages']}）"]
    if eligible < RELIABILITY_MIN_EFFECTIVE:
        level = CONF_LEVEL_NONE if eligible == 0 else CONF_LEVEL_LOW
    else:
        level = CONF_LEVEL_MID if eligible >= RELIABILITY_HIGH_MIN_MESSAGES \
            else CONF_LEVEL_LOW
    reliability = {"level": level, "confidence": None, "proxy": True,
                   "basis": basis}
    if status == STATUS_EVIDENCE_LIMITED:
        _mark_limited_reliability(reliability)
    return reliability


def _uncertainty_from_scores(items: list[dict], dim: str) -> dict:
    variances: list[float] = []
    entropies: list[float] = []
    missing = 0
    for item in items:
        probs = _dim_probabilities(item["result"], dim)
        if not probs:
            missing += 1
            continue
        var = distribution_variance(probs)
        ent = distribution_entropy_bits(probs)
        if var is not None:
            variances.append(var)
        if ent is not None:
            entropies.append(ent)
    if not variances and not entropies:
        return {"available": False,
                "note": "结果未保留概率分布（旧缓存），无法给出分布不确定性"}
    return {
        "available": True,
        "mean_variance": _round(_mean(variances)),
        "max_variance": _round(max(variances) if variances else None),
        "mean_entropy_bits": _round(_mean(entropies)),
        "max_entropy_bits": _round(max(entropies) if entropies else None),
        "missing_distributions": missing,
        "note": "方差 / 熵只表达分布集中或模糊程度，不是判断准确率",
    }


def _uncertainty_from_noul(items: list[dict], raw_key: str) -> dict:
    raws = [float(item["metrics"][raw_metrics_key(raw_key)]) for item in items]
    if not raws:
        return {"available": False,
                "note": "没有可用于该维度的消息，无法给出信号区间"}
    return {
        "available": True,
        "raw_min": _round(min(raws)),
        "raw_max": _round(max(raws)),
        "raw_avg": _round(_mean(raws)),
        "note": "Noul 为 0~1 概率且无置信度字段（官方设计）；"
                "保留 raw 区间表达不确定性，raw 漂移不等于结论变化",
    }


def _status(eligible: int, explicit_count: int = 0) -> str:
    """三态：证据充分 / 明确证据但覆盖有限 / 真正信息不足。

    coverage 低 ≠ evidence 不存在：单条明确档证据（如 romantic raw ≥ 0.90、
    “以后别联系我了”的 distancing 高信号）必须表达为
    ``evidence_limited``（当前样本存在明确信号，覆盖有限），而不是抹成
    “数据不足，无法判断”；普通 / 弱观察单条仍为 ``insufficient``。
    """
    if eligible >= RELIABILITY_MIN_EFFECTIVE:
        return STATUS_SUFFICIENT
    if explicit_count >= 1:
        return STATUS_EVIDENCE_LIMITED
    return STATUS_INSUFFICIENT


def _strength_score_dim(items: list[dict], stats: dict, avg_key: str,
                        label_fn) -> dict:
    value = stats.get(avg_key)
    level = label_fn(value)
    basis = [f"{avg_key} = {_round(value)} / 4（message_weight 加权，生产聚合）"
             if value is not None else f"{avg_key} 无有效样本"]
    return {"level": level, "value": _round(value), "scale": "0~4", "basis": basis}


# ---------------------------------------------------------------------------
# 各维度构建（每个维度只读现有指标，规则见设计文档 §1~§4）
# ---------------------------------------------------------------------------


def _facet_level(prob_mass: float | None) -> str:
    if prob_mass is None:
        return "数据不足"
    if prob_mass >= FACET_DOMINANT:
        return "明显"
    if prob_mass >= FACET_NOTABLE:
        return "存在"
    return "不明显"


def _intent_mass(items: list[dict], key: str) -> float | None:
    values = [float(item["result"].get("intent", {}).get("probabilities", {})
                    .get(key, 0.0)) for item in items]
    return _mean(values)


def _emotion_mass(items: list[dict], key: str) -> float | None:
    values = [float(item["result"].get("emotion", {}).get("probabilities", {})
                    .get(key, 0.0)) for item in items]
    return _mean(values)


def _build_initiative(effective: list[dict], items: list[dict], stats: dict) -> dict:
    supporting, counter = [], []
    for item in effective:
        value = item["metrics"]["engagement"]
        conf = _dim_confidence(item["result"], "engagement")
        if value >= SUPPORT_MIN:
            supporting.append(_evidence_item(
                item, "engagement", value, conf, ROLE_SUPPORTING, "jev_metric",
                f"engagement {value:.2f} ≥ {SUPPORT_MIN:g}：主动帮助对话继续"
                "或更高投入（量表第 4 档起）"))
        elif value < INITIATIVE_COUNTER_BELOW:
            counter.append(_evidence_item(
                item, "engagement", value, conf, ROLE_COUNTER, "jev_metric",
                f"engagement {value:.2f} < {INITIATIVE_COUNTER_BELOW:g}：观察到"
                "最低限度 / 敷衍式回应或明确拒绝继续（低投入的直接观察，非推测）"))
    status = _status(len(effective), _explicit_count(supporting, counter))
    coverage = _coverage(effective, len(supporting), len(items))
    confidences = [_dim_confidence(item["result"], "engagement")
                   for item in effective]
    missing = sum(1 for c in confidences if c is None)
    proactive = _intent_mass(effective, "ask_information")
    continue_topic = _intent_mass(effective, "continue_topic")
    invite = _intent_mass(effective, "invite")
    share_personal = _intent_mass(effective, "share_personal")
    proactive_mass = _mean([v for v in (proactive, continue_topic, invite,
                                        share_personal) if v is not None])
    if status == STATUS_INSUFFICIENT:
        conclusion = "数据不足，无法判断（有效证据不足 2 条）"
    elif status == STATUS_EVIDENCE_LIMITED:
        role_word = ("发现明确证据" if supporting else "存在明确反向证据")
        conclusion = (f"当前样本{role_word}"
                      f"（{len(supporting) + len(counter)} 条）；"
                      "覆盖有限，需更多样本确认整体模式")
    elif stats.get("engagement_avg") is None:
        conclusion = "投入程度：有效关系信息不足，暂不评级"
    else:
        conclusion = f"投入程度：{scoring.score_level_label(stats.get('engagement_avg'))}"
        if (proactive_mass or 0) >= FACET_NOTABLE:
            conclusion += "，存在主动互动信号"
        conclusion += "；是否经常主动开启新话题：当前版本数据不足"
    return {
        "key": "initiative_engagement",
        "label": DIMENSION_LABELS["initiative_engagement"],
        "status": status,
        "conclusion": conclusion,
        "strength": _strength_score_dim(effective, stats, "engagement_avg",
                                        scoring.score_level_label),
        "coverage": coverage,
        "direction": _direction(len(supporting), len(counter), True, status),
        "reliability": _reliability_from_confidence(
            len(effective), coverage, [c for c in confidences if c is not None],
            missing, status),
        "supporting_count": len(supporting),
        "counter_count": len(counter),
        "counter_evidence_available": True,
        "evidence": supporting + counter,
        "signal": {
            "engagement_avg": _round(stats.get("engagement_avg")),
            "intent_mass": {
                "ask_information": _round(proactive),
                "continue_topic": _round(continue_topic),
                "invite": _round(invite),
                "share_personal": _round(share_personal),
            },
        },
        "uncertainty": _uncertainty_from_scores(effective, "engagement"),
        "facets": {
            "互动投入": scoring.score_level_label(stats.get("engagement_avg")),
            "主动互动信号": _facet_level(proactive_mass),
            "主动开启新话题": "数据不足（需 #20 互动序列分析）",
            "连续追问 / 邀约推进": "数据不足（需 #20 互动序列分析）",
        },
        "capability": {
            "interaction_contribution": "analyzed",
            "proactive_signals": "analyzed",
            "topic_initiation": "unsupported",
            "followup_structure": "unsupported",
            "reciprocity": "unsupported",
        },
        "limitations": [
            "主动 ≠ 浪漫；intent=ask_information 只表示询问，不等于主动开启新话题",
            "谁开启话题 / 连续追问 / 邀约推进属于 #20 互动序列分析，本版本不计算",
        ],
        "baseline": {}, "salient_events": [], "counter_events": [],
        "interaction_events": [],
    }


def _build_care(effective: list[dict], items: list[dict], stats: dict) -> dict:
    supporting, counter = [], []
    for item in effective:
        warmth = item["metrics"]["warmth"]
        conf = _dim_confidence(item["result"], "warmth")
        show_care = float(item["result"].get("intent", {})
                          .get("probabilities", {}).get("show_care", 0.0))
        if warmth >= SUPPORT_MIN or (show_care >= 0.5 and warmth >= 2.0):
            reason = (f"warmth {warmth:.2f} ≥ {SUPPORT_MIN:g}：明显温暖并具有"
                      "个人层面的投入") if warmth >= SUPPORT_MIN else \
                (f"intent.show_care {show_care:.2f} ≥ 0.5 且 warmth {warmth:.2f}"
                 "：可观察的回应性关心")
            supporting.append(_evidence_item(
                item, "warmth", warmth, conf, ROLE_SUPPORTING, "jev_metric",
                reason))
        elif warmth < CARE_COUNTER_BELOW:
            counter.append(_evidence_item(
                item, "warmth", warmth, conf, ROLE_COUNTER, "jev_metric",
                f"warmth {warmth:.2f} < {CARE_COUNTER_BELOW:g}：观察到明显冷淡、"
                "疏离或拒绝（注意：纯事务性 / 中性回应只是缺证据，不计反证）"))
    status = _status(len(effective), _explicit_count(supporting, counter))
    coverage = _coverage(effective, len(supporting), len(items))
    confidences = [_dim_confidence(item["result"], "warmth")
                   for item in effective]
    missing = sum(1 for c in confidences if c is None)
    show_care_mass = _intent_mass(effective, "show_care")
    caring_mass = _emotion_mass(effective, "caring")
    if status == STATUS_INSUFFICIENT:
        conclusion = "数据不足，无法判断（有效证据不足 2 条）"
    elif status == STATUS_EVIDENCE_LIMITED:
        role_word = "发现明确证据" if supporting else "存在明确反向证据"
        conclusion = (f"当前样本{role_word}"
                      f"（{len(supporting) + len(counter)} 条）；"
                      "覆盖有限，需更多样本确认整体模式")
    elif stats.get("warmth_avg") is None:
        conclusion = "温暖程度：有效关系信息不足，暂不评级"
    else:
        conclusion = f"温暖程度：{scoring.score_level_label(stats.get('warmth_avg'))}"
        if (show_care_mass or 0) >= FACET_NOTABLE:
            conclusion += "，存在关心意图信号"
        conclusion += "；关心不等于特殊关注或浪漫"
    return {
        "key": "care_responsiveness",
        "label": DIMENSION_LABELS["care_responsiveness"],
        "status": status,
        "conclusion": conclusion,
        "strength": _strength_score_dim(effective, stats, "warmth_avg",
                                        scoring.score_level_label),
        "coverage": coverage,
        "direction": _direction(len(supporting), len(counter), True, status),
        "reliability": _reliability_from_confidence(
            len(effective), coverage, [c for c in confidences if c is not None],
            missing, status),
        "supporting_count": len(supporting),
        "counter_count": len(counter),
        "counter_evidence_available": True,
        "evidence": supporting + counter,
        "signal": {
            "warmth_avg": _round(stats.get("warmth_avg")),
            "show_care_mass": _round(show_care_mass),
            "caring_emotion_mass": _round(caring_mass),
        },
        "uncertainty": _uncertainty_from_scores(effective, "warmth"),
        "facets": {
            "温暖程度": scoring.score_level_label(stats.get("warmth_avg")),
            "关心意图信号": _facet_level(show_care_mass),
            "关心情绪信号": _facet_level(caring_mass),
        },
        "capability": {"observable_care": "analyzed",
                       "care_vs_special_distinction": "analyzed"},
        "limitations": [
            "温暖 ≠ 特殊关注；关心 ≠ 浪漫（普通朋友关心不能升级 special / romantic）",
            "低 warmth 不自动等于“不关心”：纯事务性回应是缺证据，不是反面证据",
        ],
        "baseline": {}, "salient_events": [], "counter_events": [],
        "interaction_events": [],
    }


def _build_familiarity(effective: list[dict], items: list[dict], stats: dict) -> dict:
    supporting, counter = [], []
    for item in effective:
        ease = item["metrics"]["relational_ease"]
        conf = _dim_confidence(item["result"], "relational_ease")
        if ease >= SUPPORT_MIN:
            supporting.append(_evidence_item(
                item, "relational_ease", ease, conf, ROLE_SUPPORTING,
                "jev_metric",
                f"relational_ease {ease:.2f} ≥ {SUPPORT_MIN:g}：明显熟悉、"
                "轻松、有默契（量表第 4 档起）"))
        elif ease < FAMILIARITY_COUNTER_BELOW:
            counter.append(_evidence_item(
                item, "relational_ease", ease, conf, ROLE_COUNTER, "jev_metric",
                f"relational_ease {ease:.2f} < {FAMILIARITY_COUNTER_BELOW:g}："
                "观察到明显陌生、拘谨或不自然互动（正式 / 礼貌只是缺证据，不计反证）"))
    status = _status(len(effective), _explicit_count(supporting, counter))
    coverage = _coverage(effective, len(supporting), len(items))
    confidences = [_dim_confidence(item["result"], "relational_ease")
                   for item in effective]
    missing = sum(1 for c in confidences if c is None)
    if status == STATUS_INSUFFICIENT:
        conclusion = "数据不足，无法判断（有效证据不足 2 条）"
    elif status == STATUS_EVIDENCE_LIMITED:
        role_word = "发现明确证据" if supporting else "存在明确反向证据"
        conclusion = (f"当前样本{role_word}"
                      f"（{len(supporting) + len(counter)} 条）；"
                      "覆盖有限，需更多样本确认整体模式")
    elif stats.get("relational_ease_avg") is None:
        conclusion = "互动熟悉度：有效关系信息不足，暂不评级"
    else:
        conclusion = (f"互动熟悉度："
                      f"{scoring.relational_ease_label(stats.get('relational_ease_avg'))}"
                      "；熟悉不代表特殊关注或浪漫")
    return {
        "key": "familiarity",
        "label": DIMENSION_LABELS["familiarity"],
        "status": status,
        "conclusion": conclusion,
        "strength": _strength_score_dim(effective, stats, "relational_ease_avg",
                                        scoring.relational_ease_label),
        "coverage": coverage,
        "direction": _direction(len(supporting), len(counter), True, status),
        "reliability": _reliability_from_confidence(
            len(effective), coverage, [c for c in confidences if c is not None],
            missing, status),
        "supporting_count": len(supporting),
        "counter_count": len(counter),
        "counter_evidence_available": True,
        "evidence": supporting + counter,
        "signal": {"relational_ease_avg": _round(stats.get("relational_ease_avg"))},
        "uncertainty": _uncertainty_from_scores(effective, "relational_ease"),
        "facets": {"互动熟悉度": scoring.relational_ease_label(
            stats.get("relational_ease_avg"))},
        "capability": {"natural_familiarity": "analyzed"},
        "limitations": [
            "互动熟悉度只衡量自然 / 熟悉 / 轻松 / 默契，绝不解释为喜欢、"
            "特殊关注或浪漫",
        ],
        "baseline": {}, "salient_events": [], "counter_events": [],
        "interaction_events": [],
    }


def _build_special(effective: list[dict], items: list[dict], stats: dict) -> dict:
    # counter 通道关闭：special 低分只是“没有发现特殊关注”，不是反证。
    supporting = []
    for item in effective:
        value = item["metrics"]["special_attention"]
        if value >= SUPPORT_MIN:
            supporting.append(_evidence_item(
                item, "special_attention", value,
                _dim_confidence(item["result"], "special_attention"),
                ROLE_SUPPORTING, "jev_metric",
                f"special_attention {value:.2f} ≥ {SUPPORT_MIN:g}：明显超出"
                "普通社交的特别关注（量表第 4 档起）"))
    status = _status(len(effective), _explicit_count(supporting, []))
    coverage = _coverage(effective, len(supporting), len(items))
    confidences = [_dim_confidence(item["result"], "special_attention")
                   for item in effective]
    missing = sum(1 for c in confidences if c is None)
    if status == STATUS_INSUFFICIENT:
        conclusion = "数据不足，无法判断（有效证据不足 2 条）"
    elif status == STATUS_EVIDENCE_LIMITED:
        conclusion = (f"当前样本发现明确的特别关注证据"
                      f"（{len(supporting)} 条消息超出普通社交水平）；"
                      "覆盖有限，需更多样本确认整体模式")
    elif supporting:
        conclusion = ("发现明确的特别关注证据"
                      f"（{len(supporting)} 条消息超出普通社交水平）；"
                      "特殊关注不自动等于浪漫")
    else:
        conclusion = ("未发现明确的特别关注证据"
                      "（缺少证据 ≠ 证明没有特殊关注）")
    return {
        "key": "special_attention",
        "label": DIMENSION_LABELS["special_attention"],
        "status": status,
        "conclusion": conclusion,
        "strength": _strength_score_dim(effective, stats, "special_attention_avg",
                                        scoring.score_level_label),
        "coverage": coverage,
        "direction": _direction(len(supporting), 0, False, status),
        "reliability": _reliability_from_confidence(
            len(effective), coverage, [c for c in confidences if c is not None],
            missing, status),
        "supporting_count": len(supporting),
        "counter_count": 0,
        "counter_evidence_available": False,
        "evidence": supporting,
        "signal": {"special_attention_avg": _round(
            stats.get("special_attention_avg"))},
        "uncertainty": _uncertainty_from_scores(effective, "special_attention"),
        "facets": {"特别关注": scoring.score_level_label(
            stats.get("special_attention_avg"))},
        "capability": {"selective_attention": "analyzed"},
        "limitations": [
            "特殊关注与温暖 / 熟悉度 / 浪漫严格分开：很熟或很自然不代表特殊关注",
            "“只告诉你”类选择性披露可以构成特殊关注证据，但不自动升级为浪漫",
            "本维度没有可靠反证通道：低 special 只是未发现证据（absence ≠ counter）",
        ],
        "baseline": {}, "salient_events": [], "counter_events": [],
        "interaction_events": [],
    }


def ev_key_name(raw_key: str) -> str:
    return "romantic_ev" if raw_key == "romantic_signal" else "distancing_ev"


def raw_metrics_key(raw_key: str) -> str:
    """结果字段名（romantic_signal）→ message_metrics 输出键（romantic_raw）。"""
    return "romantic_raw" if raw_key == "romantic_signal" else "distancing_raw"


def raw_avg_key_name(raw_key: str) -> str:
    return "romantic_raw_avg" if raw_key == "romantic_signal" \
        else "distancing_raw_avg"


def _noul_dimension(key: str, raw_key: str, ev_avg_key: str,
                    items_all: list[dict], effective: list[dict], stats: dict,
                    notes: list[str], signal_noun: str) -> dict:
    clear, weak = [], []
    for item in effective:
        raw = float(item["metrics"][raw_metrics_key(raw_key)])
        ev = float(item["metrics"][ev_key_name(raw_key)])
        if raw >= ROMANTIC_CLEAR_MIN:
            clear.append(_evidence_item(
                item, raw_key, raw, None, ROLE_SUPPORTING, "jev_metric",
                f"{raw_key} {raw:.2f} ≥ {ROMANTIC_CLEAR_MIN:g}：明确信号档"
                f"（transformed evidence {ev:.2f}）"))
        elif raw > ROMANTIC_WEAK_MIN:
            weak.append(_evidence_item(
                item, raw_key, raw, None, ROLE_WEAK_TRACE, "jev_metric",
                f"{raw_key} {raw:.2f} 属弱信号区间（{ROMANTIC_WEAK_MIN:g}~"
                f"{ROMANTIC_CLEAR_MIN:g}）：只保留痕迹，不构成结论依据"))
    status = _status(len(effective), _explicit_count(clear, []))
    coverage = _coverage(effective, len(clear), len(items_all))
    avg_ev = stats.get(ev_avg_key)
    strength = {"level": scoring.evidence_level_label(avg_ev),
                "value": _round(avg_ev), "scale": "transformed 0~1",
                "basis": [f"{ev_avg_key} = {_round(avg_ev)}（transform_noul_evidence "
                          "后的加权证据，生产聚合）",
                          f"raw 明确档（≥{ROMANTIC_CLEAR_MIN:g}）消息 {len(clear)} 条；"
                          f"弱信号痕迹（>{ROMANTIC_WEAK_MIN:g}）{len(weak)} 条"]}
    if status == STATUS_INSUFFICIENT:
        conclusion = "数据不足，无法判断（有效证据不足 2 条）"
    elif status == STATUS_EVIDENCE_LIMITED:
        conclusion = (f"当前样本发现明确{signal_noun}证据"
                      f"（{len(clear)} 条消息，raw ≥ {ROMANTIC_CLEAR_MIN:g}）；"
                      "覆盖有限，需更多样本确认整体模式")
    elif clear:
        conclusion = (f"发现明确{signal_noun}证据（{len(clear)} 条消息达到明确档）；"
                      f"整体证据：{scoring.evidence_level_label(avg_ev)}")
    elif weak:
        conclusion = ("未发现明确证据；存在弱信号痕迹（不构成结论，"
                      "仅保留 raw 差异供诊断）")
    else:
        conclusion = "未发现明确证据"
    return {
        "key": key,
        "label": DIMENSION_LABELS[key],
        "status": status,
        "conclusion": conclusion,
        "strength": strength,
        "coverage": coverage,
        "direction": _direction(len(clear), 0, False, status),
        "reliability": _reliability_proxy(
            len(effective), coverage,
            "Noul 无 confidence 字段（官方设计），可靠性为覆盖代理", status),
        "supporting_count": len(clear),
        "counter_count": 0,
        "counter_evidence_available": False,
        "evidence": clear + weak,
        "signal": {
            "raw_avg": _round(stats.get(raw_avg_key_name(raw_key))),
            "evidence_avg": _round(avg_ev),
            "raw_max": _round(max((float(i["metrics"][raw_metrics_key(raw_key)])
                                   for i in effective), default=None)),
            "clear_signal_count": len(clear),
            "weak_signal_count": len(weak),
        },
        "uncertainty": _uncertainty_from_noul(effective, raw_key),
        "facets": {"信号结论": scoring.evidence_level_label(avg_ev)},
        "capability": {"noul_signal": "analyzed"},
        "limitations": notes,
        "baseline": {}, "salient_events": [], "counter_events": [],
        "interaction_events": [],
    }


def _build_romantic(effective: list[dict], items: list[dict], stats: dict) -> dict:
    return _noul_dimension(
        "romantic", "romantic_signal", "romantic_evidence", items, effective,
        stats, signal_noun="浪漫",
        notes=[
            "raw 概率与 transformed evidence 双轨保留：weak floor（≤0.30）会把"
            "弱 raw 差异清零，raw 漂移 ≠ 结论变化",
            "保留弱信息 ≠ 把弱信息解释成结论；未发现明确证据不等于“不喜欢”",
            "本维度没有可靠反证通道（absence ≠ counter）",
            "不代表对方真实感情；禁止解读为“喜欢概率”",
        ])


def _build_withdrawal(effective: list[dict], items: list[dict], stats: dict) -> dict:
    return _noul_dimension(
        "withdrawal", "distancing_signal", "distancing_evidence", items,
        effective, stats, signal_noun="疏离",
        notes=[
            "仅指关系层疏离（v3.0 语义）：自然结束话题、暂时忙碌、话题拒绝、"
            "仅划定浪漫边界都不是疏离",
            "低 engagement ≠ 疏离；distancing 低也不代表没有边界压力",
            "本维度没有可靠反证通道（absence ≠ counter）",
        ])


def _build_boundary_pressure(effective: list[dict], items: list[dict],
                             stats: dict) -> dict:
    """边界压力：当前九问无直接通道 → 恒为 unsupported（正确表达能力边界）。

    D5 directionality 限制的落点：施压消息可能同时具有高 engagement /
    高 relationship_evidence，legacy base_score 因此偏高；本维度**绝不**用这些
    间接指标拼凑假的 boundary_pressure 分数，也绝不因 legacy 分高给出正向结论。
    """
    return {
        "key": "boundary_pressure",
        "label": DIMENSION_LABELS["boundary_pressure"],
        "status": STATUS_UNSUPPORTED,
        "conclusion": "当前 schema 不支持可靠判断（无直接边界施压指标）",
        "strength": {"level": "无法判断", "value": None, "scale": None,
                     "basis": ["九问没有边界施压 / 无视拒绝 / coercive interaction"
                               "的直接指标；间接指标（engagement / evidence）"
                               "不能替代方向判断"]},
        "coverage": {"eligible_messages": 0, "supporting_messages": 0,
                     "analyzed_messages": len(items), "coverage_ratio": None},
        "direction": DIR_UNKNOWN,
        "reliability": {"level": CONF_LEVEL_NA, "confidence": None,
                        "proxy": False,
                        "basis": ["没有直接指标，不评估可靠性"]},
        "supporting_count": 0,
        "counter_count": 0,
        "counter_evidence_available": False,
        "evidence": [],
        "signal": {},
        "uncertainty": {"available": False,
                        "note": "没有直接指标，无法给出信号或不确定性"},
        "facets": {"边界压力": "当前分析模型没有足够直接指标可靠判断"},
        "capability": {"boundary_pressure": "unsupported",
                       "boundary_response": "unsupported"},
        "limitations": [
            "当前分析模型没有足够直接指标可靠判断边界压力（#20 将引入 "
            "boundary response 事件后补充）",
            "高信息量 ≠ 正向关系信号：边界施压消息可能同时具有高 engagement / "
            "高 relationship_evidence，不得据此推断“尊重边界”或“关系健康”",
            "不得用 distancing 低推断“没有边界压力”，不得用 overall 高推断"
            "“关系健康”",
        ],
        "baseline": {}, "salient_events": [], "counter_events": [],
        "interaction_events": [],
    }


# ---------------------------------------------------------------------------
# 汇总文案（确定性模板；无生成式 LLM、无心理解读）
# ---------------------------------------------------------------------------


def _summary_lines(dimensions: dict) -> list[str]:
    def _line(dim: dict) -> str:
        if dim["conclusion"].startswith(dim["label"]):
            return dim["conclusion"]
        return f"{dim['label']}：{dim['conclusion']}"

    lines = [_line(dimensions[key]) for key in DIMENSION_ORDER]
    fam = dimensions["familiarity"]
    romantic = dimensions["romantic"]
    special = dimensions["special_attention"]
    care = dimensions["care_responsiveness"]
    initiative = dimensions["initiative_engagement"]
    if fam["status"] == STATUS_SUFFICIENT and fam["strength"]["value"] is not None \
            and fam["strength"]["value"] >= SUPPORT_MIN \
            and romantic["conclusion"].startswith("未发现明确证据"):
        lines.append("互动熟悉度高不代表特殊关注或浪漫信号。")
    if care["supporting_count"] and not special["supporting_count"]:
        lines.append("关心可以只是普通朋友之间的关心，不构成特殊关注证据。")
    if special["supporting_count"] and romantic["conclusion"].startswith(
            "未发现明确证据"):
        lines.append("特殊关注不自动等于浪漫信号。")
    if initiative["supporting_count"] and romantic["conclusion"].startswith(
            "未发现明确证据"):
        lines.append("主动投入不自动等于浪漫信号。")
    if any(dimensions[key]["direction"] == DIR_MIXED for key in DIMENSION_ORDER):
        lines.append("存在支持与相反两方向的证据并存，请以逐维证据为准，不要平均成单一结论。")
    if any(dimensions[key]["status"] == STATUS_EVIDENCE_LIMITED
           for key in DIMENSION_ORDER):
        lines.append("单条明确证据只代表当前样本，不能外推为长期关系模式。")
    return lines


def _salience_summary_lines(salience: dict) -> list[str]:
    """#19 显著 / 相反事件的确定性摘要（分类保留，无任何数值聚合）。"""
    lines: list[str] = []
    sal_dims = salience.get("dimensions") or {}
    if salience.get("baseline_summary"):
        lines.append(salience["baseline_summary"])
    has_salient = has_counter = False
    for key in DIMENSION_ORDER:
        entry = sal_dims.get(key) or {}
        if entry.get("salient_phrase"):
            has_salient = True
            lines.append(f"{DIMENSION_LABELS[key]}：{entry['salient_phrase']}"
                         "（显著证据，单独保留，不并入基线）")
        if entry.get("counter_phrase"):
            has_counter = True
            lines.append(f"{DIMENSION_LABELS[key]}：{entry['counter_phrase']}"
                         "（相反证据，独立存在，不与显著证据抵消）")
    if has_salient and has_counter:
        lines.append("显著证据与相反证据并存，未做加权抵消或平均。")
    if has_salient:
        lines.append("显著事件不因普通消息数量多而消失；"
                     "单条明确证据也不外推为长期关系模式。")
    return lines


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_profile(results: list[dict], *, stats: dict | None = None,
                  salience: dict | None = None,
                  interaction_events: list | None = None) -> dict:
    """由现有分析结果派生 Relationship Profile v2（纯函数、确定性）。

    参数:
        results: ``analyze_messages`` 的结果列表（只读；不修改、不持久化）。
        stats: 可选的 ``scoring.compute_conversation_stats(results)`` 输出；
            缺省时内部调用生产函数计算（不复制公式）。
        salience: 可选的 ``salience.build_salience(results)`` 输出（#19）；
            提供时按维度填充 ``baseline`` / ``salient_events`` / ``counter_events``
            （#18 预留槽位，schema 不变）。缺省时槽位为空（#18 行为）。
        interaction_events: **#20 预留**注入接口；本版本不产生 turn-level 事件。

    返回:
        ``relationship-profile-v2`` dict（schema 见设计文档 §3）。
        旧缓存缺少字段时降级为 insufficient / 排除，绝不 crash。
    """
    if stats is None:
        stats = scoring.compute_conversation_stats(results)
    items, unavailable = _collect_items(results)
    effective = [item for item in items
                 if item["metrics"]["evidence"]
                 >= scoring.EFFECTIVE_MESSAGE_MIN_EVIDENCE]

    dimensions = {
        "initiative_engagement": _build_initiative(effective, items, stats),
        "care_responsiveness": _build_care(effective, items, stats),
        "familiarity": _build_familiarity(effective, items, stats),
        "special_attention": _build_special(effective, items, stats),
        "romantic": _build_romantic(effective, items, stats),
        "withdrawal": _build_withdrawal(effective, items, stats),
        "boundary_pressure": _build_boundary_pressure(effective, items, stats),
    }
    sal_dims = (salience or {}).get("dimensions") or {}
    for key in DIMENSION_ORDER:
        entry = sal_dims.get(key) or {}
        dimensions[key].update({
            "salient_events": list(entry.get("salient_events") or []),
            "counter_events": list(entry.get("counter_events") or []),
            "interaction_events": list(interaction_events or []),
        })
        if entry.get("baseline"):
            dimensions[key]["baseline"] = entry["baseline"]
        if entry.get("salient_tier") is not None:
            dimensions[key]["salient_tier"] = entry["salient_tier"]
        if entry.get("counter_tier") is not None:
            dimensions[key]["counter_tier"] = entry["counter_tier"]
        if entry.get("salient_phrase") is not None:
            dimensions[key]["salient_phrase"] = entry["salient_phrase"]
        if entry.get("counter_phrase") is not None:
            dimensions[key]["counter_phrase"] = entry["counter_phrase"]

    limitations = [
        "本画像只描述聊天文本中可观察到的信号，不代表对方真实心理状态。",
        "低分 / 未发现证据 ≠ 反面证据；不确定 ≠ 否定。",
        "relationship_evidence_strength 衡量关系信息量，不代表关系方向或好坏。",
    ]
    if unavailable:
        limitations.append(
            f"{unavailable} 条结果缺少画像所需字段（旧缓存格式），已从画像中排除。")
    if not items:
        limitations.append("没有可分析的成功结果，画像各维度均为数据不足。")
    unclassified = ((salience or {}).get("diagnostics") or {}) \
        .get("unclassified_high_information") or []
    if unclassified:
        limitations.append(
            f"{len(unclassified)} 条高信息量消息当前 schema 无法定向"
            "（仅记录于 diagnostics），不得据此推断正向或负向关系结论。")

    summary_lines = _summary_lines(dimensions)
    if salience:
        summary_lines = _salience_summary_lines(salience) + summary_lines

    return {
        "version": PROFILE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "dimensions": dimensions,
        "overall_legacy": {
            "overall": stats.get("overall"),
            "recent": stats.get("recent"),
            "trend": stats.get("trend"),
            "role": "辅助参考（Legacy overall）",
            "conflict_note": "画像与 overall 表面冲突时，以维度结构与证据为准，"
                             "不得把所有维度重新总结成一个数字",
        },
        "summary": {
            "lines": summary_lines,
            "text": "\n".join(summary_lines),
        },
        "limitations": limitations,
        "capability_gaps": [
            "话题开启 / 连续追问 / 跨日重提 / 邀约推进 / reciprocity / "
            "拒绝后反应属于 #20 Interaction dynamics，当前版本不计算。",
            "显著 / 相反证据以分类事件保留（salient_events / counter_events），"
            "不做数值聚合、不产生综合分；legacy overall 不参与事件方向判断。",
        ],
        "reserved": {
            "evidence_source_types": list(EVIDENCE_SOURCE_TYPES),
            "salient_events": [e for key in DIMENSION_ORDER
                               for e in dimensions[key]["salient_events"]],
            "counter_events": [e for key in DIMENSION_ORDER
                               for e in dimensions[key]["counter_events"]],
            "interaction_events": list(interaction_events or []),
        },
    }
