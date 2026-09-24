"""纵向比较：重叠检测、重复去重、时间顺序门禁与长期报告（P3–P5）。

全部是**纯本地函数**：读好友档案里已有的快照，做集合/时间运算，不调用
Jev，不改动九问与评分，也不新增任何“尊重分 / 喜欢概率 / 人格标签”。

三条不可妥协的规则：

1. **聊天时间与分析时间分开**（P4）：历史记录按 ``chat_*_time`` 归位，
   后来分析的旧聊天要排到它真实的聊天位置上；
2. **未来信息隔离**（P4）：给某个目标消息找历史证据时，只允许聊天时间
   **早于**该目标的消息参与，绝不把更晚的总结塞进更早目标的上下文；
3. **不生成虚假连续趋势**（P4）：聊天时间缺失、范围重叠、片段不连续、
   模型版本或分析 schema 不同时，只输出限制说明或要求重新评估，
   绝不把不同条件的记录连成一条趋势线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from analyzer import DEFAULT_MODEL, SCHEMA_VERSION
from friend_history import RunSummary, case_signature, chat_time_span
from merge import message_fingerprint
from scoring import (
    evidence_level_label,
    message_metrics,
    rank_relationship_signals,
    score_level_label,
    total_evidence_label,
)
from timeline import TIME_FULL, time_kind

# 相邻两次历史记录之间，超过这个天数就在报告里标注“片段不连续”
CONTINUITY_GAP_DAYS = 30


# ---------------------------------------------------------------------------
# 重叠 / 重复检测（P3）
# ---------------------------------------------------------------------------


@dataclass
class OverlapReport:
    """当前导入与某一位好友历史之间的消息重叠情况。"""

    fingerprint_total: int = 0        # 当前导入的消息条数
    duplicate_count: int = 0          # 指纹已在历史中出现过（重复记录）
    new_count: int = 0                # 历史中没有的新消息
    covered_runs: list[str] = field(default_factory=list)  # 命中过哪些 run
    time_overlap: bool = False        # 时间范围是否与历史重叠
    overlap_first: str | None = None
    overlap_last: str | None = None
    identical_cases: list[str] = field(default_factory=list)

    @property
    def duplicate_ratio(self) -> float | None:
        if not self.fingerprint_total:
            return None
        return self.duplicate_count / self.fingerprint_total

    @property
    def is_full_duplicate(self) -> bool:
        """整包重复：导入的每条消息都已在历史里，且没有新消息。"""
        return (self.fingerprint_total > 0
                and self.duplicate_count == self.fingerprint_total)


def overlap_with_history(messages: list[dict],
                         runs: list[dict]) -> OverlapReport:
    """比较当前导入与历史快照的消息重叠（基于稳定消息指纹）。

    ``runs`` 是 ``FriendStore.get_run()`` 的完整快照（带 ``messages``）。
    同时间不同内容的消息指纹不同，因此不会被误判为重复。
    """
    current = {message_fingerprint(m) for m in messages}
    seen: set[str] = set()
    covered: list[str] = []
    identical: list[str] = []
    signature = case_signature(messages)
    for run in runs:
        fingerprints = {row["fingerprint"] for row in run.get("messages") or []}
        if fingerprints & current:
            covered.append(run["run_id"])
        seen |= fingerprints
        if run.get("case_signature") == signature:
            identical.append(run["run_id"])
    covered.sort()
    identical.sort()

    duplicates = current & seen
    first, last = chat_time_span(messages)
    overlap_first = overlap_last = None
    time_overlap = False
    for run in runs:
        rf, rl = run.get("chat_first_time"), run.get("chat_last_time")
        if not (first and last and rf and rl):
            continue
        if first <= rl and rf <= last:
            time_overlap = True
            lo, hi = max(first, rf), min(last, rl)
            overlap_first = lo if overlap_first is None else min(overlap_first, lo)
            overlap_last = hi if overlap_last is None else max(overlap_last, hi)

    return OverlapReport(
        fingerprint_total=len(current),
        duplicate_count=len(duplicates),
        new_count=len(current - seen),
        covered_runs=covered,
        time_overlap=time_overlap,
        overlap_first=overlap_first,
        overlap_last=overlap_last,
        identical_cases=identical,
    )


def stale_targets(run: dict, messages: list[dict]) -> list[int]:
    """历史快照中**上下文可能已变化**的目标消息下标。

    规则：当前导入里存在一条聊天时间**早于**该目标、且不在该次快照消息
    集合中的消息 → 该目标的完整上下文已经改变，不能盲目复用当时的逐条
    分析缓存（Context Builder 的窗口可能因此不同）。
    """
    run_messages = run.get("messages") or []
    if not run_messages:
        return []
    known = {row["fingerprint"] for row in run_messages}
    earlier = [
        str(m.get("time")) for m in messages
        if time_kind(m.get("time")) == TIME_FULL
        and message_fingerprint(m) not in known
    ]
    if not earlier:
        return []
    earliest_new = min(earlier)
    stale: list[int] = []
    for row in run_messages:
        if not row.get("is_target"):
            continue
        target_time = row.get("chat_time")
        if target_time and str(target_time) > earliest_new:
            stale.append(int(row["index"]))
    return sorted(stale)


# ---------------------------------------------------------------------------
# 纵向资格门禁（P4）
# ---------------------------------------------------------------------------


@dataclass
class RunEligibility:
    """一条历史记录能否参与纵向比较。"""

    run_id: str
    status: str                       # usable / reevaluate / excluded
    reasons: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status == "usable"


def _gap_days(first: str | None, last: str | None) -> float | None:
    if not first or not last:
        return None
    try:
        a = datetime.strptime(first, "%Y-%m-%d %H:%M")
        b = datetime.strptime(last, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (b - a).total_seconds() / 86400.0


def eligibility(runs: list[RunSummary], *,
                schema_version: str = SCHEMA_VERSION,
                model: str = DEFAULT_MODEL) -> list[RunEligibility]:
    """逐条判断历史记录参与纵向比较的资格与限制原因。

    - ``schema`` / ``model`` 不同 → ``reevaluate``（结论不可直接比较）；
    - 聊天时间缺失（无完整时间范围，或完整时间比例 < 1）→ 限制说明，
      记录仍按“参考”保留，但不作为趋势点；
    - 与另一条记录聊天范围重叠 → ``reevaluate``（无法确定归属，除非是
      同一案例的重复保存）；
    - 片段之间间隔过大 → 限制说明（不构成连续观察）。
    """
    ordered = list(runs)
    out: list[RunEligibility] = []
    # 各次真实返回的模型版本集合：只在**同一档案内部**互相比较。
    # （请求用的 DEFAULT_MODEL 是移动别名，永远不必要求相等。）
    returned = {r.response_model for r in ordered if r.response_model}
    for run in ordered:
        reasons: list[str] = []
        status = "usable"

        if run.schema_version and run.schema_version != schema_version:
            status = "reevaluate"
            reasons.append(f"分析 schema 不同（{run.schema_version} ≠ {schema_version}）")
        if run.request_model and run.request_model != model:
            reasons.append(f"请求模型不同（{run.request_model}）")
        if len(returned) > 1 and run.response_model:
            others = sorted(returned - {run.response_model})
            reasons.append(
                f"各次真实返回模型版本不一致（本次 {run.response_model}，"
                f"其它 {'、'.join(others)}），结论不可直接横向比较")
            status = "reevaluate"

        if not run.chat_first_time or not run.chat_last_time:
            status = "reevaluate" if status == "usable" else status
            reasons.append("缺少完整聊天时间范围，无法归位到时间轴")
        elif run.full_time_ratio is not None and run.full_time_ratio < 1.0:
            reasons.append(
                f"完整时间记录比例 {run.full_time_ratio * 100:.0f}%"
                "（部分消息无法归位）")

        # 与其它记录的范围重叠
        for other in ordered:
            if other is run:
                continue
            if not (run.chat_first_time and run.chat_last_time
                    and other.chat_first_time and other.chat_last_time):
                continue
            if (run.chat_first_time <= other.chat_last_time
                    and other.chat_first_time <= run.chat_last_time):
                if run.case_signature and run.case_signature == other.case_signature:
                    reasons.append("与另一条记录是同一批消息（重复保存）")
                else:
                    status = "reevaluate"
                    reasons.append("聊天时间范围与另一条记录重叠，消息归属不确定")

        if run.failed_count:
            reasons.append(f"其中 {run.failed_count} 条分析失败")
        if run.skipped_media_count:
            reasons.append(f"其中 {run.skipped_media_count} 条媒体被跳过（内容未知）")

        out.append(RunEligibility(run_id=run.run_id, status=status,
                                  reasons=reasons))
    return out


def continuity_gap_days(runs: list[RunSummary]) -> float | None:
    """相邻历史记录之间的最长间隔天数（None = 无法计算）。

    超过 ``CONTINUITY_GAP_DAYS`` 时不构成连续观察，报告只作展示，
    绝不据此连成趋势线。
    """
    gaps: list[float] = []
    for prev, cur in zip(runs, runs[1:]):
        if prev.chat_last_time and cur.chat_first_time:
            gap = _gap_days(prev.chat_last_time, cur.chat_first_time)
            if gap is not None:
                gaps.append(gap)
    return max(gaps) if gaps else None


def future_leakage_allowed(target_time: str | None,
                           history_times: list[str]) -> list[str]:
    """只返回聊天时间**早于**目标的历史时间点（未来信息隔离）。

    ``history_times`` 里的无时间/晚于目标的时间一律剔除——不能把更晚
    的总结放进更早目标消息的上下文。
    """
    if not target_time:
        return []
    target = str(target_time)
    return [t for t in history_times
            if t and str(t) < target]


# ---------------------------------------------------------------------------
# 证据抽取（确定性，来自既有指标，不新增评分）
# ---------------------------------------------------------------------------


def supporting_evidence(results: list[dict], limit: int = 5) -> list[dict]:
    """代表性证据：这次快照里关系信息量最高的目标消息。

    排序完全沿用 ``scoring.rank_relationship_signals``（evidence ×
    relation_confidence），不新增任何评分。
    """
    out: list[dict] = []
    for item in rank_relationship_signals(results, max_n=limit):
        entry, metrics = item["entry"], item["metrics"]
        out.append({
            "index": entry["index"],
            "time": entry.get("time"),
            "stance": "supporting",
            "note": _signal_note(entry, metrics),
        })
    return out


def counter_evidence(results: list[dict], limit: int = 5) -> list[dict]:
    """相反证据：与整体方向不一致的**既有**信号。

    判定只用已有指标，不发明新分数：带弱/明确疏离证据，或温暖程度 ≤1/4
    的消息（即“不是一路走高”的那类）。按疏离证据降序、温暖升序排。
    """
    rows: list[tuple[float, float, dict, dict]] = []
    for entry in results:
        if entry.get("error"):
            continue
        metrics = message_metrics(entry)
        if metrics is None:
            continue
        distancing = metrics.get("distancing_ev") or 0.0
        warmth = (entry.get("result") or {}).get("warmth", {}).get("score") or 0.0
        if distancing >= 0.30 or warmth <= 1.0:
            rows.append((distancing, -warmth, entry, metrics))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]["index"]))
    return [
        {"index": entry["index"], "time": entry.get("time"),
         "stance": "counter", "note": _signal_note(entry, metrics)}
        for _, _, entry, metrics in rows[:limit]
    ]


def _signal_note(entry: dict, metrics: dict) -> str:
    """证据片段的确定性说明（只有指标，没有判断措辞）。"""
    result = entry.get("result") or {}
    parts: list[str] = []
    if metrics.get("evidence") is not None:
        parts.append(f"关系信息量 {metrics['evidence']:.1f}/4")
    for key, label in (("warmth", "温暖"), ("engagement", "投入")):
        value = (result.get(key) or {}).get("score")
        if value is not None:
            parts.append(f"{label} {value:.1f}/4")
    if metrics.get("distancing_ev") is not None:
        parts.append(f"疏离证据 {evidence_level_label(metrics['distancing_ev'])}")
    if metrics.get("romantic_ev") is not None:
        parts.append(f"暧昧证据 {evidence_level_label(metrics['romantic_ev'])}")
    if not parts:
        parts.append("（无可展示指标）")
    return "、".join(parts)


def missing_data_notes(run: RunSummary) -> list[str]:
    """缺失数据提示（不猜、不补、只用已有事实）。"""
    notes: list[str] = []
    if not run.chat_first_time or not run.chat_last_time:
        notes.append("该次分析没有可归位的完整聊天时间")
    elif run.full_time_ratio is not None and run.full_time_ratio < 1.0:
        notes.append(f"完整时间记录比例 {run.full_time_ratio * 100:.0f}%")
    if run.failed_count:
        notes.append(f"{run.failed_count} 条消息分析失败，未纳入统计")
    if run.skipped_media_count:
        notes.append(
            f"{run.skipped_media_count} 条非文本媒体被跳过"
            "（复制文本只有占位符，内容未知，未做任何推测）")
    for warning in run.warnings:
        if warning and warning not in notes:
            notes.append(warning)
    return notes


# ---------------------------------------------------------------------------
# 长期报告（P5）
# ---------------------------------------------------------------------------


def build_longitudinal_report(friend, runs: list[RunSummary],
                              full_runs: list[dict] | None = None,
                              *, current_case: str | None = None) -> str:
    """生成本地「长期观察」Markdown。

    ``friend`` 是 Friend（只用 display_name / friend_id 做本地标注）；
    ``runs`` 是 RunSummary 列表（已按聊天时间归位）；``full_runs`` 是同一
    批 run 的完整快照（可选，用于证据片段）。
    """
    full_by_id = {r["run_id"]: r for r in (full_runs or [])}
    elig = {e.run_id: e for e in eligibility(runs)}

    lines: list[str] = ["# 长期观察（本地档案）", ""]
    lines += [
        f"- 档案：{friend.display_name}",
        f"- 档案 ID：`{friend.friend_id}`（本地随机 ID，非昵称，不外发）",
        f"- 历史分析次数：{len(runs)}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "> 本报告由**本地**档案聚合生成，没有调用任何 TypeSafe API，",
        "> 也没有修改九问定义或评分公式；不构成科学验证结论，",
        "> 不提供人格标签，也不根据回复快慢推断态度。",
        "",
    ]

    if not runs:
        lines += ["（该档案还没有保存过分析快照。）", ""]
        return "\n".join(lines)

    # ---- 各次分析的聊天覆盖范围 ----
    lines += ["## 各次分析的聊天覆盖范围", ""]
    lines += [
        "| # | 保存于 | 聊天时间范围 | 完整时间比例 | 消息 | 已分析 | 失败 |"
        " 媒体跳过 | schema | 模型 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, run in enumerate(runs, start=1):
        span = _span_text(run)
        ratio = ("-" if run.full_time_ratio is None
                 else f"{run.full_time_ratio * 100:.0f}%")
        model = run.response_model or run.request_model
        lines.append(
            f"| {i} | {_fmt_ts(run.saved_at)} | {span} | {ratio} "
            f"| {run.message_count} | {run.analyzed_count} "
            f"| {run.failed_count} | {run.skipped_media_count} "
            f"| {run.schema_version} | {model} |"
        )
    lines.append("")

    # ---- 各次互动行为统计 ----
    lines += ["## 各次互动行为统计", ""]
    for i, run in enumerate(runs, start=1):
        stats = (full_by_id.get(run.run_id) or {}).get("stats") or {}
        lines.append(f"### {i}. 保存于 {_fmt_ts(run.saved_at)}（{_span_text(run)}）")
        lines.append(f"- 摘要：{run.summary_text or '（无）'}")
        for label, value in _behavior_lines(stats):
            lines.append(f"- {label}：{value}")
        notes = missing_data_notes(run)
        if notes:
            lines.append("- 缺失/限制：" + "；".join(notes))
        e = elig.get(run.run_id)
        if e and e.reasons:
            lines.append("- 纵向比较限制：" + "；".join(e.reasons))
        lines.append("")

    # ---- 可核查证据 / 相反证据 ----
    lines += ["## 可核查的代表性证据与相反证据", ""]
    any_evidence = False
    for i, run in enumerate(runs, start=1):
        full = full_by_id.get(run.run_id) or {}
        kept = full.get("evidence") or []
        if not kept:
            continue
        any_evidence = True
        lines.append(f"### {i}. 保存于 {_fmt_ts(run.saved_at)}")
        for item in kept:
            stance = {"supporting": "支持性", "counter": "相反/其它"}.get(
                item["stance"], "其它")
            line = f"- [{stance}] 消息 #{item['index'] + 1}：{item['note']}"
            if item.get("snippet"):
                line += f" · 片段：“{item['snippet']}”"
            lines.append(line)
        lines.append("")
    if not any_evidence:
        lines += [
            "（没有保留任何证据片段——保存快照时未勾选“保留匿名化证据片段”，",
            "因此本报告只含统计与消息编号，不含任何聊天文本。）",
            "",
        ]

    # ---- 连续性（不构成连续观察时明确说明）----
    gap = continuity_gap_days(runs)
    if gap is not None and gap > CONTINUITY_GAP_DAYS:
        lines += [
            "## 连续性限制",
            "",
            f"- 相邻记录之间最长间隔 **{gap:.0f} 天**，不构成连续观察；",
            "  只能分别描述各时间段，不能据此推断一条跨越整段时间的趋势。",
            "",
        ]

    # ---- 重复 / 新消息 ----
    dupes = [r for r in runs
             if current_case and r.case_signature == current_case]
    if dupes:
        lines += [
            "## 与当前导入的关系",
            "",
            f"- 当前导入与档案中 {len(dupes)} 次历史分析是**同一批消息**"
            "（案例指纹一致），属于重复分析，不构成本次新数据。",
            "",
        ]

    lines += [
        "## 说明",
        "",
        "- 纵向比较只按**聊天发生时间**归位；分析（保存）时间只用于排序展示。",
        "- 只要存在缺失时间、范围重叠、片段不连续、模型或 schema 不同，",
        "  对应记录就只作参考，不能连成趋势线。",
        "- 本工具分析聊天文本中可观察的互动信号，不代表对方真实心理状态。",
        "",
    ]
    return "\n".join(lines)


def _span_text(run: RunSummary) -> str:
    if run.chat_first_time and run.chat_last_time:
        return f"{run.chat_first_time} ~ {run.chat_last_time}"
    return "（无完整时间）"


def _fmt_ts(value: float | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _behavior_lines(stats: dict) -> list[tuple[str, str]]:
    """从既有统计量取解释层指标（绝不新增评分）。"""
    out: list[tuple[str, str]] = []
    overall = stats.get("overall")
    out.append(("互动亲近信号指数",
                "样本有效关系信息不足，暂不生成可靠指数"
                if overall is None else f"{overall:.1f} / 100"))
    out.append(("关系信息量", total_evidence_label(stats.get("total_weight"))))
    for key, label in (("warmth_avg", "温暖程度"),
                       ("engagement_avg", "投入程度"),
                       ("special_attention_avg", "特殊关注"),
                       ("relational_ease_avg", "互动熟悉度")):
        value = stats.get(key)
        out.append((label, score_level_label(value)))
    out.append(("暧昧信号", evidence_level_label(stats.get("romantic_evidence"))))
    out.append(("疏离信号", evidence_level_label(stats.get("distancing_evidence"))))
    return out
