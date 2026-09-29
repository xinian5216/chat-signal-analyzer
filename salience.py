"""Salience-aware evidence channel（Issue #19）——显著事件 / 相反事件的纯业务层。

职责：从现有 Jev structured results（`chat-signal-v3.3`）派生**维度内**的
显著事件（salient events）与相反事件（counter events），并给出不含事件消息的
普通互动基线（baseline）。目标结构（Issue #19）：

    Relationship Profile
    ├── baseline        普通互动模式（不含显著 / 反向事件消息）
    ├── salient events  各维度的明确显著证据（明确特殊关注 / 明确浪漫 / 明确后撤 / 明确关心）
    └── counter events  各维度的明确反向证据（明确低投入 / 明显冷淡 / 明显生疏）

硬边界（违者即 bug）：

- **不是“把高分消息权重调大”**：没有数值 boost、没有新权重、没有新综合分数；
  事件是**分类保留**（categorical retention），不是被平均成一个数字；
- **禁止全局正负 salience**：事件首先属于某个 dimension；`direction` 只相对该
  维度自身构念（如 distancing 高 = withdrawal 维度的 supporting evidence），
  不存在“全局正向 +20 / 负向 −20”；legacy overall / base_score **不参与**
  任何事件的方向判断；
- **evidence ≠ direction**：`relationship_evidence_strength` 只作 eligibility
  （≥ 1.0 才可产生事件）与信息量上下文，绝不决定方向；
- **message_weight 不决定方向**：本模块不使用 message_weight（如需参考只在
  diagnostics 中作为 reliability 上下文）；
- **absence ≠ counter**：counter 事件只在量表**明确负向档**（< 1.0，第 0 档：
  明确拒绝继续 / 明显冷淡拒绝 / 明显陌生拘谨）开启；special / romantic /
  withdrawal 无反证通道（低分只是未发现证据）；
- **只用当前 schema 真正支持的显著性**：话题开启 / 连续追问 / 跨日重提 /
  邀约推进 / reciprocity / 拒绝后反应 / personal recall 是 #20 的
  interaction events，本模块**永不生成**（仅预留类型名）；
- **边界压力恒为 unsupported**：当前九问无直接通道，绝不伪造 boundary
  pressure 事件；高信息量 ≠ 正向关系信号（D5），高信息消息无法定向时只进
  diagnostics（`unclassified_high_information`），绝不修补成正向结论；
- **确定性**：事件身份绑定 (source, message_index, dimension, event_class,
  metric)，同一条消息同一维度同类事件只产生一次；文案全部模板化；
- **pure**：无 Streamlit / 无网络 / 无文件副作用 / 不写 behavior DB
  （与 behavior.py 的人工确认长期事件层完全分离）。

阈值全部复用 `relationship_profile.py` / `scoring.py` 的现有锚点；本模块仅新增
4 个明确档分级常量（见下），依据见 `docs/issue19-salience-aware-aggregation.md`。
"""

from __future__ import annotations

import scoring
from analyzer import SCHEMA_VERSION
import relationship_profile as rp

SALIENCE_VERSION = "salience-v1"

# ---------------------------------------------------------------------------
# 阈值（集中配置）
# ---------------------------------------------------------------------------

# 事件资格 = #18 的“明确档”规则（单一事实来源，直接复用）：
# - Score 支持档：≥ 3.0（量表第 4 档起：明显 / 主动 / 超出普通社交）
# - Noul 明确档：≥ 0.70（NOUL_STRONG_MARK）
# - 严格负向档：< 1.0（量表第 0 档：明确拒绝继续 / 明显冷淡拒绝 / 明显陌生拘谨）
#   —— 第 1 档弱负向观察（如 engagement 1.8 敷衍档）只是弱观察，不产生事件，
#      避免大量短回复（“嗯”/“好”/“哈哈”）制造事件。
SUPPORT_MIN = rp.SUPPORT_MIN                      # 3.0
NOUL_CLEAR_MIN = rp.ROMANTIC_CLEAR_MIN            # 0.70
STRICT_NEG_BELOW = rp.EXPLICIT_NEG_BELOW           # 1.0
ELIGIBLE_MIN_EVIDENCE = scoring.EFFECTIVE_MESSAGE_MIN_EVIDENCE   # 1.0
HIGH_INFORMATION_EVIDENCE = 3.0   # D5：高关系信息量消息的诊断标记门槛

# salience_level 分级（只分档，不打连续分）：
# - Score 支持：≥ 3.5 明确（接近最高档）/ ≥ 3.25 强（“强”文字等级起点）/ ≥ 3.0 中等
# - Noul：≥ 0.85 明确 / ≥ 0.70 强
# - 严格负向：< 0.5 明确 / < 1.0 强
SALIENCE_EXPLICIT_MIN_SCORE = 3.5
SALIENCE_EXPLICIT_MIN_NOUL = 0.85
SALIENCE_EXPLICIT_NEG_BELOW = 0.5
SALIENCE_STRONG_MIN_SCORE = scoring.SCORE_LEVEL_STRONG   # 3.25

# 同类证据累积档（qualitative tiers；capped，禁止线性无限累加）：
# 1 条 = single / 2 条 = multiple / ≥3 条 = repeated（封顶，不再升级）
TIER_REPEATED_MIN = 3

TIER_NONE = "none"
TIER_SINGLE = "single"
TIER_MULTIPLE = "multiple"
TIER_REPEATED = "repeated"

SALIENCE_LEVEL_EXPLICIT = "explicit"
SALIENCE_LEVEL_STRONG = "strong"
SALIENCE_LEVEL_MODERATE = "moderate"

DIRECTION_SUPPORTING = "supporting"
DIRECTION_COUNTER = "counter"

# 事件来源（#20 的 interaction_event / behavior 层的 manual_confirmed_event 预留，
# 本模块永不产生）
SOURCE_JEV_METRIC = "jev_metric"

# #20 预留事件类型（只登记类型名，本模块永不生成）
RESERVED_EVENT_CLASSES = (
    "topic_initiation",          # 主动开启新话题
    "followup_structure",        # 连续追问
    "cross_day_reengagement",    # 几天后主动重新提起
    "invitation_progression",    # 邀约从模糊到具体
    "boundary_response",         # 尊重拒绝 / 继续施压（#20 boundary pressure）
    "personal_recall",           # 记得对方提过的细节
)

# ---------------------------------------------------------------------------
# 事件类（当前 schema 真正支持的全部显著性；每个事件类绑定唯一维度）
# ---------------------------------------------------------------------------

# (event_class, dimension, direction, metric, scale, noun)
EVENT_CLASSES = (
    ("explicit_special_attention", "special_attention", DIRECTION_SUPPORTING,
     "special_attention", "0~4", "特殊关注"),
    ("explicit_care_behavior", "care_responsiveness", DIRECTION_SUPPORTING,
     "warmth", "0~4", "关心"),
    ("explicit_romantic_signal", "romantic", DIRECTION_SUPPORTING,
     "romantic_signal", "0~1 raw", "浪漫"),
    ("explicit_relationship_withdrawal", "withdrawal", DIRECTION_SUPPORTING,
     "distancing_signal", "0~1 raw", "关系后撤"),
    ("low_investment_or_refusal", "initiative_engagement", DIRECTION_COUNTER,
     "engagement", "0~4", "低投入"),
    ("cold_or_rejecting_response", "care_responsiveness", DIRECTION_COUNTER,
     "warmth", "0~4", "冷淡"),
    ("stiff_or_unfamiliar_interaction", "familiarity", DIRECTION_COUNTER,
     "relational_ease", "0~4", "生疏"),
)

EVENT_CLASS_META = {
    cls: {"dimension": dim, "direction": direction, "metric": metric,
          "scale": scale, "noun": noun}
    for cls, dim, direction, metric, scale, noun in EVENT_CLASSES
}

# 每个事件类的确定性解释模板（why salient / 还可能是什么 / 限制）
_ALTERNATIVE_EXPLANATIONS = {
    "explicit_special_attention":
        "特别关注也可能来自普通朋友的热心或话题高度相关；特殊关注 ≠ 浪漫。",
    "explicit_care_behavior":
        "温暖回应也可能只是普通朋友之间的关心；关心 ≠ 特殊关注 ≠ 浪漫。",
    "explicit_romantic_signal":
        "调侃 / 玩笑措辞也可能产生高 raw 概率，需结合语境人工核对；"
        "raw 是 Jev decision probability，不是对方真实感情概率。",
    "explicit_relationship_withdrawal":
        "需排除自然结束话题 / 暂时忙碌 / 话题拒绝 / 仅划定浪漫边界"
        "（v3.3 疏离语义）；请结合上下文人工核对。",
    "low_investment_or_refusal":
        "低投入也可能来自忙碌或话题不相关；低投入 ≠ 关系疏离。",
    "cold_or_rejecting_response":
        "冷淡回应也可能只是事务性语境；纯事务 / 中性只是缺证据，不是冷漠。",
    "stiff_or_unfamiliar_interaction":
        "生疏 / 拘谨也可能来自场合正式或话题陌生。",
}

_COMMON_EVENT_LIMITS = [
    "事件来自单条消息的可观察信号，不代表对方真实心理，也不代表长期关系模式。",
]
_HIGH_INFORMATION_LIMIT = (
    "该消息关系信息量高（relationship_evidence_strength {ev:.2f} ≥ "
    f"{HIGH_INFORMATION_EVIDENCE:g}）；高信息量 ≠ 正向关系信号（D5）："
    "边界施压 / 无视拒绝的语境当前 schema 无法识别。"
)

# 语义守护（S14 / 禁止全局正负 salience）：事件永远没有全局方向 / 效价字段
_FORBIDDEN_EVENT_FIELDS = ("valence", "global_direction", "positive", "negative",
                           "boost", "score_impact", "weight")


# ---------------------------------------------------------------------------
# 确定性小工具
# ---------------------------------------------------------------------------


def _round(value, digits: int = 4):
    return None if value is None else round(float(value), digits)


def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def collect_items(results: list[dict]) -> list[dict]:
    """可分析消息（只读生产 message_metrics；失败 / 缺字段跳过，不 crash）。"""
    items: list[dict] = []
    for entry in sorted(results, key=lambda e: e["index"]):
        if entry.get("error"):
            continue
        metrics = scoring.message_metrics(entry)
        if metrics is None:
            continue
        items.append({"index": entry["index"], "result": entry["result"],
                      "metrics": metrics})
    return items


def _dim_confidence(result: dict, dim: str) -> float | None:
    block = result.get(dim)
    if isinstance(block, dict):
        conf = block.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            return float(conf)
    return None


# ---------------------------------------------------------------------------
# 事件生成（阈值档 → 事件；每个消息 × 事件类至多一条）
# ---------------------------------------------------------------------------


def _tier_supporting_score(value: float) -> str:
    if value >= SALIENCE_EXPLICIT_MIN_SCORE:
        return SALIENCE_LEVEL_EXPLICIT
    if value >= SALIENCE_STRONG_MIN_SCORE:
        return SALIENCE_LEVEL_STRONG
    return SALIENCE_LEVEL_MODERATE


def _tier_supporting_noul(raw: float) -> str:
    return SALIENCE_LEVEL_EXPLICIT if raw >= SALIENCE_EXPLICIT_MIN_NOUL \
        else SALIENCE_LEVEL_STRONG


def _tier_counter(value: float) -> str:
    return SALIENCE_LEVEL_EXPLICIT if value < SALIENCE_EXPLICIT_NEG_BELOW \
        else SALIENCE_LEVEL_STRONG


def _event(event_class: str, item: dict, value: float, confidence: float | None,
           salience_level: str, reason: str) -> dict:
    meta = EVENT_CLASS_META[event_class]
    ev = item["metrics"]["evidence"]
    limitations = list(_COMMON_EVENT_LIMITS)
    if ev >= HIGH_INFORMATION_EVIDENCE:
        limitations.append(_HIGH_INFORMATION_LIMIT.format(ev=ev))
    return {
        "event_id": (f"{SOURCE_JEV_METRIC}:{item['index']}:{meta['dimension']}:"
                     f"{event_class}:{meta['metric']}"),
        "dimension": meta["dimension"],
        "event_class": event_class,
        "direction": meta["direction"],
        "source": SOURCE_JEV_METRIC,
        "message_index": item["index"],
        "metric": meta["metric"],
        "value": _round(value),
        "scale": meta["scale"],
        "confidence": _round(confidence),
        # 信息量上下文（eligibility / 诊断用），绝不决定方向
        "relationship_evidence_strength": _round(ev),
        "salience_level": salience_level,
        "reason": reason,
        "alternative_explanation": _ALTERNATIVE_EXPLANATIONS[event_class],
        "limitations": limitations,
    }


def _detect_events(item: dict) -> list[dict]:
    """单条消息 → 0..n 个事件（每事件类至多一个；不同维度可各有一个）。"""
    events: list[dict] = []
    m = item["metrics"]
    r = item["result"]

    special = m["special_attention"]
    if special >= SUPPORT_MIN:
        events.append(_event(
            "explicit_special_attention", item, special,
            _dim_confidence(r, "special_attention"),
            _tier_supporting_score(special),
            f"special_attention {special:.2f} ≥ {SUPPORT_MIN:g}：明显超出普通社交"
            f"的特别关注（量表第 4 档起；{ _tier_word(_tier_supporting_score(special)) }）"))

    warmth = m["warmth"]
    if warmth >= SUPPORT_MIN:
        events.append(_event(
            "explicit_care_behavior", item, warmth, _dim_confidence(r, "warmth"),
            _tier_supporting_score(warmth),
            f"warmth {warmth:.2f} ≥ {SUPPORT_MIN:g}：明显温暖并具有个人层面投入"
            f"（量表第 4 档起；{ _tier_word(_tier_supporting_score(warmth)) }）"))

    romantic_raw = m["romantic_raw"]
    if romantic_raw >= NOUL_CLEAR_MIN:
        events.append(_event(
            "explicit_romantic_signal", item, romantic_raw, None,
            _tier_supporting_noul(romantic_raw),
            f"romantic_signal raw {romantic_raw:.2f} ≥ {NOUL_CLEAR_MIN:g}："
            f"明确浪漫信号档（transformed evidence {m['romantic_ev']:.2f}；"
            f"{ _tier_word(_tier_supporting_noul(romantic_raw)) }）"))

    distancing_raw = m["distancing_raw"]
    if distancing_raw >= NOUL_CLEAR_MIN:
        events.append(_event(
            "explicit_relationship_withdrawal", item, distancing_raw, None,
            _tier_supporting_noul(distancing_raw),
            f"distancing_signal raw {distancing_raw:.2f} ≥ {NOUL_CLEAR_MIN:g}："
            f"明确关系层疏离信号档（v3.3 语义；transformed evidence "
            f"{m['distancing_ev']:.2f}；"
            f"{ _tier_word(_tier_supporting_noul(distancing_raw)) }）"))

    engagement = m["engagement"]
    if engagement < STRICT_NEG_BELOW:
        events.append(_event(
            "low_investment_or_refusal", item, engagement,
            _dim_confidence(r, "engagement"), _tier_counter(engagement),
            f"engagement {engagement:.2f} < {STRICT_NEG_BELOW:g}：明确拒绝继续"
            f"（量表第 0 档；{ _tier_word(_tier_counter(engagement)) }）"))

    if warmth < STRICT_NEG_BELOW:
        events.append(_event(
            "cold_or_rejecting_response", item, warmth, _dim_confidence(r, "warmth"),
            _tier_counter(warmth),
            f"warmth {warmth:.2f} < {STRICT_NEG_BELOW:g}：明显冷淡、疏离或拒绝"
            f"（量表第 0 档；{ _tier_word(_tier_counter(warmth)) }）"))

    ease = m["relational_ease"]
    if ease < STRICT_NEG_BELOW:
        events.append(_event(
            "stiff_or_unfamiliar_interaction", item, ease,
            _dim_confidence(r, "relational_ease"), _tier_counter(ease),
            f"relational_ease {ease:.2f} < {STRICT_NEG_BELOW:g}：明显陌生、"
            f"拘谨或不自然互动（量表第 0 档；{ _tier_word(_tier_counter(ease)) }）"))

    return events


def _tier_word(level: str) -> str:
    return {"explicit": "接近最高档，明确", "strong": "明确",
            "moderate": "明显档起点"}[level]


# ---------------------------------------------------------------------------
# 同类证据累积（qualitative tiers，capped）
# ---------------------------------------------------------------------------


def tier_for_count(count: int) -> str:
    """1 条 = single；2 条 = multiple；≥3 条 = repeated（封顶，不线性升级）。"""
    if count <= 0:
        return TIER_NONE
    if count == 1:
        return TIER_SINGLE
    if count < TIER_REPEATED_MIN:
        return TIER_MULTIPLE
    return TIER_REPEATED


def describe_events(events: list[dict], direction: str) -> list[str]:
    """事件列表 → 确定性用户可见短句（无百分比、无 boost、无伪精度）。"""
    if not events:
        return []
    noun = EVENT_CLASS_META[events[0]["event_class"]]["noun"]
    count = len({e["message_index"] for e in events})
    tier = tier_for_count(count)
    if tier == TIER_SINGLE:
        head = f"出现 1 条明确{noun}信号"
    elif tier == TIER_MULTIPLE:
        head = f"出现明确{noun}信号（2 条不同消息）"
    else:
        head = (f"多次出现明确{noun}信号（{count} 条不同消息）；"
                "多次出现不代表统计独立性")
    lines = [head]
    for e in sorted(events, key=lambda e: e["message_index"]):
        lines.append(f"消息 #{e['message_index'] + 1}：{e['reason']}")
    return lines


# ---------------------------------------------------------------------------
# baseline（普通互动模式：不含显著 / 反向事件消息）
# ---------------------------------------------------------------------------

_BASELINE_METRICS = (
    ("initiative_engagement", "engagement", "0~4", scoring.score_level_label),
    ("care_responsiveness", "warmth", "0~4", scoring.score_level_label),
    ("familiarity", "relational_ease", "0~4", scoring.relational_ease_label),
    ("special_attention", "special_attention", "0~4", scoring.score_level_label),
)


def _baseline_for(metric: str, scale: str, label_fn, ordinary: list[dict],
                  excluded: int) -> dict:
    values = [item["metrics"][metric] for item in ordinary]
    value = _mean(values)
    return {
        "ordinary_messages": len(ordinary),
        "excluded_event_messages": excluded,
        "value": _round(value),
        "scale": scale,
        "level": label_fn(value) if value is not None else "样本不足",
        "note": "普通互动基线（不含显著 / 反向事件消息；含低信息量短回复）",
    }


def _baseline_noul(raw_key: str, ordinary: list[dict], excluded: int) -> dict:
    raws = [item["metrics"][raw_key] for item in ordinary]
    clear = sum(1 for v in raws if v >= NOUL_CLEAR_MIN)
    weak = sum(1 for v in raws if rp.ROMANTIC_WEAK_MIN < v < NOUL_CLEAR_MIN)
    return {
        "ordinary_messages": len(ordinary),
        "excluded_event_messages": excluded,
        "raw_avg": _round(_mean(raws)),
        "scale": "0~1 raw",
        "clear_signal_count": clear,
        "weak_signal_count": weak,
        "level": "未发现明确证据" if not clear else "仍有明确信号",
        "note": "普通互动基线（不含显著 / 反向事件消息）",
    }


def _baseline_summary(dimensions: dict, ordinary_count: int,
                      excluded_count: int, total_count: int) -> str:
    if total_count == 0:
        return "没有可分析消息，无法给出普通互动基线。"
    if ordinary_count == 0:
        return ("没有普通消息（全部消息均为显著 / 反向事件消息），"
                "无法给出普通互动基线。")
    care = dimensions["care_responsiveness"]
    init = dimensions["initiative_engagement"]
    fam = dimensions["familiarity"]
    text = (f"基线互动：温暖「{care['level']}」· 投入「{init['level']}」· "
            f"熟悉度「{fam['level']}」（普通消息 {ordinary_count} 条"
            f"{'，另有 ' + str(excluded_count) + ' 条显著 / 反向事件消息不计入基线' if excluded_count else ''}）。")
    return text


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_salience(results: list[dict], *, stats: dict | None = None) -> dict:
    """派生 salience-v1 证据通道（纯函数、确定性、0 Jev）。

    参数:
        results: ``analyze_messages`` 的结果列表（只读）。
        stats: 可选的 ``scoring.compute_conversation_stats`` 输出（本模块只用
            其中的信息量上下文，不用 overall / recent / trend 做任何方向判断）。

    返回:
        ``salience-v1`` dict：events（扁平列表）+ dimensions（每维度
        salient_events / counter_events / tiers / baseline）+ baseline_summary +
        diagnostics（unclassified_high_information 等，仅供诊断，不是结论）。
    """
    items = collect_items(results)
    effective = [item for item in items
                 if item["metrics"]["evidence"] >= ELIGIBLE_MIN_EVIDENCE]

    events: list[dict] = []
    seen_ids: set[str] = set()
    duplicate_suppressed = 0
    for item in effective:
        for event in _detect_events(item):
            if event["event_id"] in seen_ids:
                duplicate_suppressed += 1
                continue
            seen_ids.add(event["event_id"])
            events.append(event)
    events.sort(key=lambda e: (e["message_index"], e["dimension"],
                               e["event_class"]))

    event_message_indices = {e["message_index"] for e in events}
    ordinary = [item for item in items
                if item["index"] not in event_message_indices]

    dimensions: dict = {}
    for key in rp.DIMENSION_ORDER:
        salient = [e for e in events
                   if e["dimension"] == key and e["direction"] == DIRECTION_SUPPORTING]
        counter = [e for e in events
                   if e["dimension"] == key and e["direction"] == DIRECTION_COUNTER]
        salient_phrases = describe_events(salient, DIRECTION_SUPPORTING)
        counter_phrases = describe_events(counter, DIRECTION_COUNTER)
        dimensions[key] = {
            "salient_events": salient,
            "counter_events": counter,
            "salient_tier": tier_for_count(len({e["message_index"] for e in salient})),
            "counter_tier": tier_for_count(len({e["message_index"] for e in counter})),
            "salient_count": len(salient),
            "counter_count": len(counter),
            "salient_phrase": salient_phrases[0] if salient_phrases else None,
            "counter_phrase": counter_phrases[0] if counter_phrases else None,
        }

    excluded = len(event_message_indices)
    for key, metric, scale, label_fn in _BASELINE_METRICS:
        dimensions[key]["baseline"] = _baseline_for(
            metric, scale, label_fn, ordinary, excluded)
    dimensions["romantic"]["baseline"] = _baseline_noul("romantic_raw", ordinary,
                                                        excluded)
    dimensions["withdrawal"]["baseline"] = _baseline_noul("distancing_raw", ordinary,
                                                          excluded)
    dimensions["boundary_pressure"]["baseline"] = {
        "ordinary_messages": len(ordinary),
        "excluded_event_messages": excluded,
        "note": "无直接指标，不给基线（#20 boundary response 事件后补）",
    }

    unclassified = []
    for item in effective:
        if item["index"] in event_message_indices:
            continue
        if item["metrics"]["evidence"] >= HIGH_INFORMATION_EVIDENCE:
            unclassified.append({
                "message_index": item["index"],
                "relationship_evidence_strength": _round(
                    item["metrics"]["evidence"]),
                "note": ("高关系信息量但当前 schema 无受支持的显著事件映射；"
                         "不得解释为正向或负向结论（D5 directionality 限制），"
                         "也不得修补成假的 boundary pressure 事件。"),
            })

    baseline_dims = {key: dimensions[key]["baseline"]
                     for key in ("care_responsiveness", "initiative_engagement",
                                 "familiarity")}
    return {
        "version": SALIENCE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "events": events,
        "dimensions": dimensions,
        "baseline_summary": _baseline_summary(
            baseline_dims, len(ordinary), len(event_message_indices), len(items)),
        "diagnostics": {
            "event_class_counts": {
                cls: sum(1 for e in events if e["event_class"] == cls)
                for cls in sorted(EVENT_CLASS_META)
            },
            "event_message_count": len(event_message_indices),
            "ordinary_message_count": len(ordinary),
            "duplicate_suppressed": duplicate_suppressed,
            "unclassified_high_information": unclassified,
            "notes": [
                "diagnostics 仅供研究 / 排查，不是用户结论。",
                "message_weight 与 legacy overall 不参与任何事件方向判断。",
                "本层不写 behavior DB；behavior 事件仍须人工确认后长期保存。",
            ],
        },
    }
