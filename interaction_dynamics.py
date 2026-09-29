"""Interaction Dynamics（Issue #20）——turn / sequence 级互动结构事件（纯业务层）。

职责：从**有序本地消息 + 现有 Jev structured results** 派生互动结构事件：
连续追问、明显间隔后的重启、邀约推进、个人回忆候选、互惠结构观察、
边界回应（拒绝后 TA 的实际行为）。这是 #17 D5 方向缺陷的正式修复路径
（不再依赖 legacy base_score 判断边界压力）。

硬边界（违者即 bug）：

- **causal-only（prefix invariance）**：事件只使用 ``<= event_end_index`` 的
  消息；第 80 条消息不能改变“第 30 条时已知的事件”（测试强制）；
- **observable ≠ psychological**：只描述互动结构，绝不输出人格 / 关心程度 /
  “TA 很在乎你”类结论；
- **关键词相似 ≠ 记住**：personal recall 只做严格 distinctive anchor 候选，
  默认 ``review_required``（人工确认后才可升级）；
- **没施压 ≠ 尊重**：只有明确 boundary opportunity + 明确接受 / 调整才构成
  boundary_pressure 的相反证据；沉默不构成证据（absence ≠ counter）；
- **不修改 Jev questions / schema / Context Builder / scoring**；0 Jev API、
  0 网络、0 DB、无 Streamlit、无文件副作用；
- **复用既有锚点**：gap / 问题 / 邀约 / 施压 / 拒绝等规则常量直接引用
  ``behavior.py``（不复制第二份 regex）；identity 与 behavior 事件层共用
  fingerprint 稳定身份（不新增 DB schema、不改旧 candidate identity）；
- **保守分级**：``auto_supported``（结构证据明确）/
  ``review_required``（候选，禁入正式结论）/ ``deferred``（不实现）。
- **媒体无语义**：纯媒体 turn 只证明“一个 turn 存在”，不得赋予任何语义；
  mixed 消息只分析可见文本部分。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

import behavior as bv
from merge import message_fingerprint
from timeline import time_kind as _time_kind

INTERACTION_VERSION = "interaction-dynamics-v1"
SOURCE = "interaction_event"

# review status（§34 信任分级）
REVIEW_AUTO = "auto_supported"        # 结构证据明确，可进正式 Profile
REVIEW_CANDIDATE = "review_required"  # 候选，只能进“待核对线索”
REVIEW_DEFERRED = "deferred"          # 当前确定性信息不足，不实现

DIRECTION_SUPPORTING = "supporting"
DIRECTION_COUNTER = "counter"
DIRECTION_OBSERVATION = "observation"

# ---------------------------------------------------------------------------
# 操作阈值（集中定义；都是 operational rule，不是科学阈值）
# ---------------------------------------------------------------------------

# “明显间隔后重启”的门槛：直接复用 behavior.py 的集中锚点（180 分钟），
# 诊断工具另测 2h/6h/12h/24h 候选取值（见 docs/issue20-interaction-dynamics.md §5）。
REENGAGEMENT_GAP_MINUTES = bv.INITIATIVE_GAP_MINUTES      # 180
# personal recall 要求跨日（full timestamps 且间隔 ≥ 24h）：同日内的关键词
# 复现更可能是普通话题延续，不作 recall 处理（operational rule）。
RECALL_MIN_GAP_MINUTES = 1440
RECALL_MAX_ANCHOR_CHARS = 8
FOLLOWUP_MAX_TURNS = 4          # follow-up 的有界本地窗口（turn 数）
BOUNDARY_RESPONSE_TURNS = 2     # boundary 后观察 TA 反应的 turn 窗口
RECIPROCITY_MIN_TURNS = 4       # reciprocity 小样本保护（可计数 turn 下限）
CARE_SIGNAL_MIN_MASS = 0.5      # follow-up 携带 care 结构化信号的门槛

# ---------------------------------------------------------------------------
# 既有正则的复用引用（§51：不复制第二份词典）
# ---------------------------------------------------------------------------

QUESTION_RE = bv.QUESTION_RE            # [？?]（复用 behavior 既有常量）
# 中文口语问句常无问号（“…吗 / …呢”）：在既有常量之上组合，不复制第二份词典。
QUESTION_LIKE_RE = re.compile(QUESTION_RE.pattern + "|吗|呢")
INVITATION_RE = bv.INVITATION_RE        # 一起|出来|见面|请你|…
ARRANGEMENT_RE = bv.ARRANGEMENT_RE      # 周六|周日|明天|后天|几点|地点|…
PRESSURE_RE = bv.PRESSURE_RE            # 为什么不行|必须|一定要|…

# 新增模式（behavior.py 无对应项，故在此定义；命名与语义见设计文档）
PAUSE_RE = re.compile("我现在不想聊这个|先不聊这个|别聊这个|不想聊这个|暂停一下|"
                      "能不能别说这个|换个话题吧|先别提这个")
RESCHEDULE_RE = re.compile("今天不行|今天不方便|这几天不行|最近不行|没空|改天|"
                           "下次吧|另约时间|这两天不方便|今天算了")
ROMANTIC_BOUNDARY_RE = re.compile("只做朋友|只想做朋友|只把你当|只是朋友|保持距离|"
                                  "别有那种想法|没有那种感觉|界限")
REFUSAL_MARKERS_RE = re.compile("不想|不去|别去|不用|算了|别再|不要再|别问|"
                                "别勉强|请别")
ACCEPT_RE = re.compile(r"(?<![不没])好|(?<![不没])行|嗯+|哦+|好吧|好的|知道了|"
                       r"听你的|理解了?|尊重|不勉强|那算了|不去就算|都行|可以")
CONTINUE_REQUEST_RE = re.compile(
    PRESSURE_RE.pattern + "|就来|来嘛|去嘛|去吧|走吧|真的不行吗|再想想|再考虑|"
    "别拒绝|试一下|试试看|给个机会|别不给|商量一下|就一次|考虑考虑|"
    "你现在过来|出来见一面")
TIME_PROPOSE_RE = re.compile(ARRANGEMENT_RE.pattern + "|晚上|下午|中午|明早|周末|"
                             "改天|另约|重新约|改时间|这周|[0-9]{1,2}点")
# 邀约“具体化”用严格时间（星期 / 処理时间）；“周末 / 晚上”等宽敚时间词不算
# 具体安排（否则“周末大家一起吃饭”会被误判为已具体）。
TIME_CONCRETE_RE = re.compile(ARRANGEMENT_RE.pattern + "|[0-9]{1,2}点")
GROUP_INVITE_RE = re.compile("大家|我们几个|几个朋友|一起聚餐|同事们|群里|"
                             "都出来|聚会|大伙")
DYADIC_INVITE_RE = re.compile("就咱俩|只有我们俩|就我们两|单独|两个人|"
                              "咱俩|我俩|就你和我")
PLACE_RE = re.compile("定位|地址|楼下|门口|公司|学校|咖啡|餐厅|饭店|酒店|"
                      "电影院|商场|车站|机场|我家|你家|在.{1,10}(?:见|碰|聚|等|吃|玩)")

# personal recall 的 generic anchor 名单（精确率 / 召回率的手闸；
# 日常高频词不足以支撑“记得某事”，命中即拒绝）。冻结于 benchmark，见文档 §9。
GENERIC_ANCHORS = frozenset({
    "工作", "上班", "加班", "开会", "会议", "下班", "吃饭", "外卖", "睡觉",
    "早起", "天气", "下雨", "学校", "上课", "下课", "医院", "身体",
    "家里", "回家", "周末", "明天", "今天", "昨天", "晚上", "下午", "中午",
    "项目", "报告", "文件", "考试", "学习", "地铁", "公交", "打车", "机票",
    "高铁", "快递", "电影", "音乐", "游戏", "朋友", "家人", "妈妈", "爸爸",
    "对象", "结婚", "生日", "假期", "春节", "国庆", "早上", "凌晨",
})

# interaction dimension → relationship_profile dimension key 映射
DIM_TO_PROFILE = {
    "initiative": "initiative_engagement",
    "care": "care_responsiveness",
    "boundary_pressure": "boundary_pressure",
}

CAPABILITIES = {
    "conversation_reengagement": {"auto": "yes", "review": "no", "deferred": "no",
                                  "time_required": "full timestamp"},
    "followup_sequence": {"auto": "yes", "review": "no", "deferred": "no",
                          "time_required": "ordered sequence only"},
    "invitation_progression": {"auto": "yes", "review": "no", "deferred": "no",
                               "time_required": "ordered sequence only"},
    "personal_recall": {"auto": "candidate-only", "review": "yes",
                        "deferred": "paraphrase / semantic matching",
                        "time_required": "full timestamp"},
    "reciprocity": {"auto": "observation-only", "review": "no",
                    "deferred": "score / percentage",
                    "time_required": "ordered sequence only"},
    "boundary_response": {"auto": "yes (explicit refusal + explicit response)",
                          "review": "ambiguous paths", "deferred": "no",
                          "time_required": "ordered sequence only"},
}

_ALTERNATIVE = {
    "conversation_reengagement":
        "可能只是继续处理未完成事项，不代表特殊关注或想念。",
    "followup_sequence":
        "连续提问可能来自任务需要或信息处理习惯，不一定代表关心。",
    "invitation_progression":
        "具体邀约只说明安排被推进，不代表浪漫；群体 / 双人结构文本不明确时按 unknown 处理。",
    "personal_recall_candidate":
        "可能是同一关键词再次出现，需人工核对是否真关联此前具体事项。",
    "boundary_accepted":
        "接受边界是当前互动中的可观察行为，不推断人格（“尊重”是人格判断）。",
    "boundary_adjusted":
        "改时间可能是正常协调，不代表更广泛的关系态度。",
    "boundary_continued_request":
        "继续请求可能出于安排需要，需结合语境人工核对。",
    "boundary_pressure":
        "描述的是“明确拒绝后仍持续推进同一请求”这一可观察结构，不评价对方人格。",
    "boundary_ambiguous":
        "回应含义不明确（可能正常调整、也可能继续推进），必须人工核对。",
}

_COMMON_LIMITS = [
    "互动结构事件只描述可观察的序列行为，不推断对方意图、感情或人格。",
    "事件窗口以本地消息序号表示；序号可能因导入 / 排序变化，身份以消息指纹为准（内部使用，不进报告）。",
]

_BOUNDARY_KIND_LABELS = {
    "explicit_refusal": "明确拒绝",
    "pause": "要求暂停该话题",
    "reschedule": "改期",
    "romantic_boundary": "划定浪漫边界",
}


# ---------------------------------------------------------------------------
# 确定性小工具
# ---------------------------------------------------------------------------


def _parse_full(time_value):
    """full_timestamp → datetime；否则 None（不猜日期）。"""
    if not time_value:
        return None
    try:
        return datetime.strptime(str(time_value).strip(), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def _is_text_message(message: dict) -> bool:
    """可见文本（纯媒体除外；mixed 只算实际文本部分）。"""
    return (message.get("content_type") != "media"
            and str(message.get("text") or "").strip() != "")


def _text_of(message: dict) -> str:
    return str(message.get("text") or "").strip()


def _minutes_between(t1, t2):
    """两个 full timestamp 的间隔分钟；任一时间不可靠则 None。"""
    a, b = _parse_full(t1), _parse_full(t2)
    if a is None or b is None:
        return None
    return abs((b - a).total_seconds()) / 60.0


def _turns(messages: list[dict]) -> list[dict]:
    """连续同说话方消息 → turn（含 has_text / media_only 标记）。"""
    turns: list[dict] = []
    for i, m in enumerate(messages):
        speaker = str(m.get("speaker") or "unknown")
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["end"] = i
            turns[-1]["messages"].append(m)
        else:
            turns.append({"speaker": speaker, "start": i, "end": i,
                          "messages": [m]})
    for turn in turns:
        turn["has_text"] = any(_is_text_message(m) for m in turn["messages"])
        turn["media_only"] = (not turn["has_text"]
                              and any(m.get("content_type") == "media"
                                      or not _text_of(m)
                                      for m in turn["messages"]))
    return turns


def _results_by_index(results: list[dict] | None) -> dict:
    return {e["index"]: e for e in (results or [])
            if not e.get("error") and e.get("result")}


def _intent_mass(result: dict | None, key: str) -> float:
    if not result:
        return 0.0
    return float(result.get("intent", {}).get("probabilities", {}).get(key, 0.0))


def _intent_choice(result: dict | None) -> str:
    if not result:
        return ""
    return str(result.get("intent", {}).get("choice") or "")


def _emotion_mass(result: dict | None, key: str) -> float:
    if not result:
        return 0.0
    return float(result.get("emotion", {}).get("probabilities", {}).get(key, 0.0))


def _entry_result(results: dict, index: int) -> dict | None:
    entry = results.get(index)
    return entry.get("result") if isinstance(entry, dict) else None


def _message_asks(results: dict, index: int) -> bool:
    r = _entry_result(results, index)
    return (_intent_mass(r, "ask_information") >= CARE_SIGNAL_MIN_MASS
            or _intent_choice(r) == "ask_information")


def _message_care_signal(results: dict, index: int) -> bool:
    r = _entry_result(results, index)
    return (_intent_mass(r, "show_care") >= CARE_SIGNAL_MIN_MASS
            or _emotion_mass(r, "caring") >= CARE_SIGNAL_MIN_MASS)


def _message_invites(results: dict, index: int) -> bool:
    r = _entry_result(results, index)
    return _intent_mass(r, "invite") >= CARE_SIGNAL_MIN_MASS \
        or _intent_choice(r) == "invite"


def _evidence_mean(results: dict, indices: list[int]) -> float | None:
    values = []
    for i in indices:
        r = _entry_result(results, i)
        if r:
            score = r.get("relationship_evidence_strength", {}).get("score")
            if isinstance(score, (int, float)):
                values.append(float(score))
    return round(sum(values) / len(values), 4) if values else None


def _identity(event_type: str, actor: str, fingerprints: list[str]) -> str:
    fps = sorted({str(f) for f in fingerprints or []})
    payload = "\n".join(["interaction-event-v1", event_type, actor] + fps)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _time_basis(messages: list[dict], start: int, end: int) -> str:
    kinds = {_time_kind(messages[i].get("time")) for i in range(start, end + 1)}
    if kinds == {"full"}:
        return "full_timestamp"
    if kinds and kinds <= {"time_only", "missing"}:
        return "ordered_only"
    return "mixed"


def _event(event_type: str, dimension: str, direction: str, actor: str,
           messages: list[dict], start: int, end: int, rule: str,
           observations: list[str], strength: str, review_status: str,
           reason: str, extra: dict | None = None,
           anchors: list[int] | None = None) -> dict:
    window = list(range(start, end + 1))
    anchor_indices = sorted(set(anchors if anchors is not None else window))
    fingerprints = [message_fingerprint(messages[i]) for i in anchor_indices]
    event = {
        "event_type": event_type,
        "source": SOURCE,
        "dimension": dimension,
        "direction": direction,
        "actor": actor,
        "window": {"start_index": start, "end_index": end,
                   "anchor_indices": anchor_indices},
        "time_basis": _time_basis(messages, start, end),
        "trigger": {"rule": rule, "observations": observations},
        "strength": strength,
        "review_status": review_status,
        # 以下两个字段仅本地用于去重 / 人工审核，绝不进入默认报告（§9 / §58）
        "identity": _identity(event_type, actor, fingerprints),
        "fingerprints": fingerprints,
        "reason": reason,
        "alternative_explanation": _ALTERNATIVE.get(event_type, ""),
        "limitations": list(_COMMON_LIMITS),
        "capability": dict(CAPABILITIES.get(
            {"personal_recall_candidate": "personal_recall"}.get(
                event_type, event_type), {})),
    }
    if extra:
        event.update(extra)
    return event


def _distinctive_anchors(user_text: str, ta_text: str) -> list[str]:
    """user/TA 文本间最长共同子串中的 distinctive anchor。

    拒绝规则（冻结于 benchmark）：
    - 命中 generic 名单；
    - 长度不足 2 个字符；
    - **仅由 generic 词拼接而成**（如“今天工作”
      ≈“今天”+“工作”）——避免长公共子串绕过泛化闸门。
    """
    anchors: list[str] = []
    a, b = user_text, ta_text
    for length in range(min(len(a), len(b), RECALL_MAX_ANCHOR_CHARS), 1, -1):
        for start in range(0, len(b) - length + 1):
            piece = b[start:start + length]
            if piece not in a:
                continue
            if not re.search(r"[\u4e00-\u9fff]", piece):
                continue
            if piece in GENERIC_ANCHORS:
                continue
            if _generic_covered(a, piece) or _generic_covered(b, piece):
                continue
            if not any(piece in x for x in anchors):
                anchors.append(piece)
        if anchors:
            return anchors[:2]
    return []


# generic 名单中长度 ≥2 的词，用于“拼接泛化”检查
_GENERIC_LONG = tuple(sorted((w for w in GENERIC_ANCHORS if len(w) >= 2),
                             key=len, reverse=True))


def _generic_covered(text: str, piece: str) -> bool:
    """piece 的每一次出现是否**完全落在 generic 词覆盖区**内。

    “今天工作”的交界子串“天工”本身不是 generic 词，但它的
    位置被“今天”与“工作”覆盖——属于泛化区域，不构成
    distinctive anchor。
    """
    if not piece:
        return False
    covered = [False] * len(text)
    for word in _GENERIC_LONG:
        start = text.find(word)
        while start != -1:
            for k in range(start, start + len(word)):
                covered[k] = True
            start = text.find(word, start + 1)
    idx = text.find(piece)
    while idx != -1:
        if all(covered[idx:idx + len(piece)]):
            return True
        idx = text.find(piece, idx + 1)
    return False


# ---------------------------------------------------------------------------
# 检测器（全部 causal-only：只消费 <= 当前扫描位置的消息）
# ---------------------------------------------------------------------------


def _detect_reengagement(messages: list[dict],
                         gap_minutes: int | None = None) -> list[dict]:
    """明显时间间隔后，谁先发送了消息（含媒体——重启本身是内容无关的行为）。

    ``gap_minutes`` 可覆盖生产门槛（研究 / diagnostics 用；生产固定用
    REENGAGEMENT_GAP_MINUTES）。
    """
    threshold = REENGAGEMENT_GAP_MINUTES if gap_minutes is None else gap_minutes
    events: list[dict] = []
    prev_text = None
    for i, m in enumerate(messages):
        if not (_is_text_message(m) or m.get("content_type") == "media"):
            continue
        if prev_text is not None:
            gap = _minutes_between(messages[prev_text].get("time"), m.get("time"))
            if gap is not None and gap >= threshold:
                actor = str(m.get("speaker") or "unknown")
                events.append(_event(
                    "conversation_reengagement",
                    "initiative" if actor == "them" else "observation",
                    DIRECTION_SUPPORTING if actor == "them"
                    else DIRECTION_OBSERVATION,
                    "TA" if actor == "them" else ("用户" if actor == "me"
                                                  else actor),
                    messages, prev_text, i, "gap_then_first_message",
                    [f"距上一条消息 {gap:.0f} 分钟（≥ "
                     f"{threshold} 分钟操作门槛）",
                     f"间隔后首条消息由{'TA' if actor == 'them' else actor}发出"],
                    "observable", REVIEW_AUTO,
                    f"{'TA' if actor == 'them' else actor}在明显间隔后首先恢复互动"
                    "（结构观察；不代表想念或特殊关注）",
                    extra={"media_content_unknown":
                           m.get("content_type") == "media"},
                ))
        prev_text = i if _is_text_message(m) else prev_text
    return events


def _detect_followups(messages: list[dict],
                      results: dict) -> list[dict]:
    """me 陈述 → TA 提问 → me 回应 → TA 再提问（bounded 4-turn 窗口，不重叠）。"""
    turns = _turns(messages)
    events: list[dict] = []
    k = 0
    while k + FOLLOWUP_MAX_TURNS <= len(turns):
        t0, t1, t2, t3 = turns[k:k + 4]
        matched = (
            t0["speaker"] == "me" and t0["has_text"]
            and t1["speaker"] == "them" and t1["has_text"]
            and t2["speaker"] == "me" and t2["has_text"]
            and t3["speaker"] == "them" and t3["has_text"]
            and not any(QUESTION_LIKE_RE.search(_text_of(m)) for m in t0["messages"])
            and not any(QUESTION_LIKE_RE.search(_text_of(m)) for m in t2["messages"])
            and any(_message_asks(results, j) for j in range(t1["start"], t1["end"] + 1))
            and any(_message_asks(results, j) for j in range(t3["start"], t3["end"] + 1))
        )
        if matched:
            care = any(_message_care_signal(results, j)
                       for turn in (t1, t3)
                       for j in range(turn["start"], turn["end"] + 1))
            start, end = t0["end"], t3["end"]
            anchors = sorted({t0["end"]}
                             | set(range(t1["start"], t1["end"] + 1))
                             | {t2["end"]}
                             | set(range(t3["start"], t3["end"] + 1)))
            # 链条延伸：后续（me 陈述 → TA 提问）继续时并入同一事件，
            # 保持“一条追问链一个事件”，避免重叠窗口重复计数。
            m = k + 4
            while m + 1 < len(turns):
                a, b = turns[m], turns[m + 1]
                if (a["speaker"] == "me" and a["has_text"]
                        and b["speaker"] == "them" and b["has_text"]
                        and not any(QUESTION_LIKE_RE.search(_text_of(x))
                                    for x in a["messages"])
                        and any(_message_asks(results, j)
                                for j in range(b["start"], b["end"] + 1))):
                    end = b["end"]
                    anchors = sorted(set(anchors) | {a["end"]}
                                     | set(range(b["start"], b["end"] + 1)))
                    m += 2
                else:
                    break
            events.append(_event(
                "followup_sequence", "initiative", DIRECTION_SUPPORTING, "TA",
                messages, start, end, "statement_ask_answer_ask",
                ["用户陈述（消息 #{s}）".format(s=t0["end"] + 1),
                 "TA 提问（消息 #{a}~#{b}，Jev ask_information 明确）".format(
                     a=t1["start"] + 1, b=t1["end"] + 1),
                 "用户回应（消息 #{c}）".format(c=t2["end"] + 1),
                 "TA 再次提问（消息 #{d}~#{e}）".format(
                     d=t3["start"] + 1, e=t3["end"] + 1)],
                "observable", REVIEW_AUTO,
                "TA 在用户回应后针对同一轮内容继续追问（结构观察）",
                extra={"care_signal": care,
                       "care_dimension_evidence": care,
                       "message_window": [start, end]},
                anchors=anchors,
            ))
            k = m                        # 不重叠：一条链一个事件
            continue
        k += 1
    return events


def _detect_invitation_progression(messages: list[dict],
                                   results: dict) -> list[dict]:
    """同一行为人（TA）的邀约从缺少时间 / 地点走向包含时间 / 地点。"""
    turns = _turns(messages)
    plays = []
    for turn in turns:
        if turn["speaker"] != "them" or not turn["has_text"]:
            continue
        if any(_message_invites(results, j)
               for j in range(turn["start"], turn["end"] + 1)):
            plays.append(turn)
    events: list[dict] = []
    for earlier, later in zip(plays, plays[1:]):
        earlier_concrete = _has_time_or_place(earlier)
        later_time = _has_time(later)
        later_place = _has_place(later)
        if earlier_concrete or not (later_time or later_place):
            continue
        party = _party_structure(earlier, later)
        events.append(_event(
            "invitation_progression", "initiative", DIRECTION_SUPPORTING, "TA",
            messages, earlier["start"], later["end"], "invite_then_concrete",
            ["较早邀约（消息 #{a}~#{b}）缺少时间 / 地点".format(
                a=earlier["start"] + 1, b=earlier["end"] + 1),
             "后续邀约（消息 #{c}~#{e}）包含{d}{p}".format(
                 c=later["start"] + 1, e=later["end"] + 1,
                 d="时间" if later_time else "", p="地点" if later_place else "")],
            "explicit" if (later_time and later_place) else "observable",
            REVIEW_AUTO,
            "同一行为人的邀约从模糊走向包含时间 / 地点（结构观察）",
            extra={"party_structure": party,
                   "message_window": [earlier["start"], later["end"]]},
        ))
    return events


def _has_time_or_place(turn: dict) -> bool:
    return _has_time(turn) or _has_place(turn)


def _has_time(turn: dict) -> bool:
    return any(TIME_CONCRETE_RE.search(_text_of(m))
               for m in turn["messages"] if _is_text_message(m))


def _has_place(turn: dict) -> bool:
    return any(PLACE_RE.search(_text_of(m))
               for m in turn["messages"] if _is_text_message(m))


def _party_structure(*turns: dict) -> str:
    text = "".join(_text_of(m) for turn in turns for m in turn["messages"])
    if GROUP_INVITE_RE.search(text):
        return "group"
    if DYADIC_INVITE_RE.search(text):
        return "dyadic"
    return "unknown"


def _detect_personal_recall(messages: list[dict],
                            results: dict) -> list[dict]:
    """严格 distinctive anchor 候选：user 提过 → 隔天后 TA 重新问起（候选级）。"""
    events: list[dict] = []
    text_messages = [(i, m) for i, m in enumerate(messages)
                     if _is_text_message(m)]
    for ui, (i, um) in enumerate(text_messages):
        if str(um.get("speaker")) != "me" or _parse_full(um.get("time")) is None:
            continue
        for j, tm in text_messages[ui + 1:]:
            if str(tm.get("speaker")) != "them":
                continue
            gap = _minutes_between(um.get("time"), tm.get("time"))
            if gap is None or gap < RECALL_MIN_GAP_MINUTES:
                continue
            if not (QUESTION_LIKE_RE.search(_text_of(tm))
                    or _message_asks(results, j)):
                continue
            anchors = _distinctive_anchors(_text_of(um), _text_of(tm))
            if not anchors:
                continue
            reopened = any(a in _text_of(m) for a in anchors
                           for k, m in text_messages
                           if i < k < j and str(m.get("speaker")) == "me")
            if reopened:
                continue
            events.append(_event(
                "personal_recall_candidate", "care", DIRECTION_SUPPORTING, "TA",
                messages, i, j, "distinctive_anchor_after_gap",
                [f"用户此前提到的具体内容（消息 #{i + 1}）",
                 f"TA 间隔 {gap / 1440:.1f} 天后再次提及（消息 #{j + 1}）",
                 f"共同 distinctive anchor：{anchors[0]}",
                 "用户在该时间间隔内未重新提起同一内容"],
                "explicit" if len(anchors[0]) >= 3 else "observable",
                REVIEW_CANDIDATE,
                "TA 后续消息可能明确关联到此前用户提到的具体事项（待人工核对）",
            ))
            break     # 同一 user 消息只取第一条严格候选
    return events


def _classify_boundary(text: str) -> str | None:
    if ROMANTIC_BOUNDARY_RE.search(text):
        return "romantic_boundary"
    if PAUSE_RE.search(text):
        return "pause"
    if REFUSAL_MARKERS_RE.search(text) or bv.REFUSAL_RE.search(text):
        return "explicit_refusal"
    if RESCHEDULE_RE.search(text):
        return "reschedule"
    return None


def _classify_response(text: str) -> str:
    if CONTINUE_REQUEST_RE.search(text):
        return "continued_request"
    has_accept = ACCEPT_RE.search(text) is not None
    has_schedule = bool(ARRANGEMENT_RE.search(text)
                        or TIME_PROPOSE_RE.search(text))
    if has_accept and has_schedule:
        return "adjusted"
    if has_accept:
        return "accepted"
    if has_schedule:
        return "ambiguous"
    return "unclassified"


def _detect_boundary_response(messages: list[dict],
                              results: dict) -> tuple[list[dict], int]:
    """用户明确边界 → 后续 TA 反应的结构分类（D5 正式通道）。"""
    turns = _turns(messages)
    events: list[dict] = []
    opportunities = 0
    for k, turn in enumerate(turns):
        if turn["speaker"] != "me" or not turn["has_text"]:
            continue
        boundary_text = "".join(_text_of(m) for m in turn["messages"])
        kind = _classify_boundary(boundary_text)
        if kind is None:
            continue
        opportunities += 1
        response = None
        ta_turns_seen = 0
        for nxt in turns[k + 1:]:
            if nxt["speaker"] != "them":
                continue
            ta_turns_seen += 1
            if ta_turns_seen > BOUNDARY_RESPONSE_TURNS:
                break
            if nxt["has_text"]:
                response = nxt
                break
        if response is None:
            continue        # 沉默不构成任何证据（absence ≠ counter）
        ta_text = "".join(_text_of(m) for m in response["messages"])
        cls = _classify_response(ta_text)
        observations = [
            f"用户边界（消息 #{turn['start'] + 1}，"
            f"{_BOUNDARY_KIND_LABELS.get(kind, kind)}）",
            f"TA 后续回应（消息 #{response['start'] + 1}）：{ta_text[:40]}",
            f"规则分类：{cls}",
        ]
        if cls == "unclassified":
            continue     # 未分类：只进 observations，不产生事件
        if cls == "ambiguous":
            events.append(_event(
                "boundary_ambiguous", "boundary_pressure", DIRECTION_OBSERVATION,
                "TA", messages, turn["start"], response["end"],
                "boundary_then_unclear_response", observations, "observable",
                REVIEW_CANDIDATE,
                "用户表达边界后，TA 的回应含义不明确（需人工核对）",
                extra={"boundary_kind": kind,
                       "message_window": [turn["start"], response["end"]]},
            ))
            continue
        if cls == "continued_request":
            explicit_pressure = PRESSURE_RE.search(ta_text) is not None
            pressure = (kind == "explicit_refusal")
            if pressure:
                events.append(_event(
                    "boundary_pressure", "boundary_pressure",
                    DIRECTION_SUPPORTING, "TA", messages, turn["start"],
                    response["end"], "explicit_refusal_then_continued_request",
                    observations, "explicit" if explicit_pressure else "observable",
                    REVIEW_AUTO,
                    "用户明确拒绝后，TA 仍持续推进同一请求（结构观察）",
                    extra={"boundary_kind": kind, "explicit_pressure_language":
                           explicit_pressure,
                           "message_window": [turn["start"], response["end"]]},
                ))
            else:
                events.append(_event(
                    "boundary_continued_request", "boundary_pressure",
                    DIRECTION_SUPPORTING, "TA", messages, turn["start"],
                    response["end"], "non_refusal_boundary_then_continued_request",
                    observations, "observable", REVIEW_CANDIDATE,
                    "用户表达边界后 TA 仍有请求 / 推进表述（需人工核对）",
                    extra={"boundary_kind": kind,
                           "message_window": [turn["start"], response["end"]]},
                ))
            continue
        event_type = "boundary_adjusted" if cls == "adjusted" else "boundary_accepted"
        events.append(_event(
            event_type, "boundary_pressure", DIRECTION_COUNTER, "TA", messages,
            turn["start"], response["end"],
            "boundary_then_acceptance", observations, "observable", REVIEW_AUTO,
            f"用户明确边界后，TA 的后续行为为{cls}"
            "（边界压力的相反证据；不推断人格）",
            extra={"boundary_kind": kind,
                   "message_window": [turn["start"], response["end"]]},
        ))
    return events, opportunities


def _reciprocity(messages: list[dict], results: dict,
                 reengagement: list[dict],
                 invitations: list[dict]) -> dict:
    """互惠结构观察：只数可观察 turn，不给百分比、不评价关系。"""
    turns = _turns(messages)
    ta_text_turns = [t for t in turns if t["speaker"] == "them" and t["has_text"]]
    me_text_turns = [t for t in turns if t["speaker"] == "me" and t["has_text"]]

    def _entry_result(j):
        entry = results.get(j)
        return entry.get("result") if entry else None

    ta_questions = sum(
        1 for t in ta_text_turns
        if any(_message_asks(results, j) for j in range(t["start"], t["end"] + 1))
        or any(QUESTION_LIKE_RE.search(_text_of(m)) for m in t["messages"]))
    me_questions = sum(
        1 for t in me_text_turns
        if any(QUESTION_LIKE_RE.search(_text_of(m)) for m in t["messages"]))
    ta_share = sum(
        1 for t in ta_text_turns
        if any(_intent_mass(_entry_result(j), "share_personal")
               >= CARE_SIGNAL_MIN_MASS
               for j in range(t["start"], t["end"] + 1)))
    me_share = sum(
        1 for t in me_text_turns
        if any(len(_text_of(m)) >= 6 for m in t["messages"]))
    restart_ta = sum(1 for e in reengagement if e["actor"] == "TA")
    restart_me = sum(1 for e in reengagement if e["actor"] != "TA")
    total_turns = len(ta_text_turns) + len(me_text_turns)
    lines: list[str] = []
    if total_turns >= RECIPROCITY_MIN_TURNS:
        if ta_questions and me_questions:
            lines.append("本样本中双方都有提问。")
        if me_questions and not ta_questions:
            lines.append("本样本中的提问基本由用户侧发起。")
        if ta_questions and not me_questions:
            lines.append("本样本中的提问基本由 TA 侧发起。")
        if ta_share and me_share:
            lines.append("本样本中双方都有个人分享。")
        elif me_share and not ta_share:
            lines.append("本样本中的个人分享基本由用户侧发起。")
        if restart_ta and not restart_me:
            lines.append("本样本中间隔后的重启由 TA 侧发起的次数更多。")
        if restart_me and not restart_ta:
            lines.append("本样本中间隔后的重启由用户侧发起的次数更多。")
        if not lines:
            lines.append("本样本互动结构较简短，不足以描述互惠模式。")
    else:
        lines.append("可计数的互动轮次不足，互惠结构暂不描述。")
    lines.append("以上仅描述当前导入样本，不代表长期关系模式。")
    return {
        "sufficient": total_turns >= RECIPROCITY_MIN_TURNS,
        "counted_turns": total_turns,
        "counts": {
            "ta_question_turns": ta_questions,
            "me_question_turns": me_questions,
            "ta_share_turns": ta_share,
            "me_share_turns": me_share,
            "ta_reengagements": restart_ta,
            "user_reengagements": restart_me,
            "invitation_progressions": len(invitations),
        },
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_interaction_events(messages: list[dict],
                             results: list[dict] | None = None) -> dict:
    """派生 interaction-dynamics-v1（纯函数、确定性、causal-only、0 Jev）。

    参数:
        messages: 有序本地消息（time-sorted；与 results 的 index 同空间）。
        results: ``analyze_messages`` 的结果列表（只读；可缺省——此时只用消息
            文本层规则，不含 Jev 结构化信号）。

    返回:
        version / events / observations / capabilities / summary_lines /
        engine_available / time_basis / media_text_unknown_turns。
        事件只使用 ``<= window.end_index`` 的消息（prefix invariance）。
    """
    messages = list(messages or [])
    results_by_index = _results_by_index(results)
    events: list[dict] = []
    events += _detect_reengagement(messages)
    events += _detect_followups(messages, results_by_index)
    invitation_events = _detect_invitation_progression(messages,
                                                       results_by_index)
    events += invitation_events
    events += _detect_personal_recall(messages, results_by_index)
    boundary_events, opportunities = _detect_boundary_response(
        messages, results_by_index)
    events += boundary_events
    events.sort(key=lambda e: (e["window"]["start_index"],
                               e["window"]["end_index"], e["event_type"]))

    reciprocity = _reciprocity(messages, results_by_index, events,
                               invitation_events)

    media_turns = sum(1 for t in _turns(messages) if t["media_only"])
    kinds = {_time_kind(m.get("time")) for m in messages}
    if not messages:
        time_basis = "missing"
    elif kinds == {"full"}:
        time_basis = "full_timestamp"
    elif kinds and kinds <= {"time_only", "missing"}:
        time_basis = "ordered_only"
    else:
        time_basis = "mixed"

    boundary_auto = [e for e in boundary_events
                     if e["review_status"] == REVIEW_AUTO]
    summary_lines = list(reciprocity["lines"])

    return {
        "version": INTERACTION_VERSION,
        "engine_available": bool(messages),
        "time_basis": time_basis,
        "events": events,
        "observations": {
            "reciprocity": reciprocity,
            "boundary_opportunities": opportunities,
            "media_text_unknown_turns": media_turns,
        },
        "capabilities": {key: dict(value) for key, value in CAPABILITIES.items()},
        "summary_lines": summary_lines,
        "diagnostics": {
            "auto_supported": sum(1 for e in events
                                  if e["review_status"] == REVIEW_AUTO),
            "review_required": sum(1 for e in events
                                   if e["review_status"] == REVIEW_CANDIDATE),
            "boundary_pressure_events": sum(
                1 for e in events if e["event_type"] == "boundary_pressure"),
            "boundary_counter_events": sum(
                1 for e in boundary_auto
                if e["direction"] == DIRECTION_COUNTER),
        },
    }


# ---------------------------------------------------------------------------
# behavior 人工审核适配器（§35：复用现有候选 / 确认 / 排除 / audit 机制）
# ---------------------------------------------------------------------------

# interaction event_type → (behavior dimension, behavior behavior_type)
_BEHAVIOR_MAP = {
    "conversation_reengagement": ("initiative", "proactive_contact"),
    "followup_sequence": ("initiative", "topic_continuation"),
    "invitation_progression": ("initiative", "concrete_arrangement"),
    "personal_recall_candidate": ("care", "continued_attention"),
    "boundary_accepted": ("respect", "refusal_reaction"),
    "boundary_adjusted": ("respect", "refusal_reaction"),
    "boundary_continued_request": ("respect", "pressure_or_disdain"),
    "boundary_pressure": ("respect", "pressure_or_disdain"),
    "boundary_ambiguous": ("respect", "refusal_reaction"),
}


def interaction_behavior_candidates(interaction: dict,
                                    messages: list[dict] | None = None
                                    ) -> list["bv.EventCandidate"]:
    """interaction events → behavior.EventCandidate（供现有审核队列使用）。

    - identity 走 behavior 既有 event_identity（指纹 + dimension + type），
      与规则候选 / 已审核事件天然去重（§37）；
    - 不含个人主观文本；notes / alternative 为确定性模板；
    - 默认 **不持久化**：候选进入 pending 队列后由用户确认 / 排除。
    """
    candidates: list["bv.EventCandidate"] = []
    for event in interaction.get("events", []):
        mapping = _BEHAVIOR_MAP.get(event["event_type"])
        if mapping is None:
            continue
        dimension, behavior_type = mapping
        window = event["window"]
        texts = (messages or [])[window["start_index"]:window["end_index"] + 1]
        candidates.append(bv.EventCandidate(
            dimension=dimension,
            behavior_type=behavior_type,
            start=window["start_index"],
            end=window["end_index"],
            fingerprints=list(event["fingerprints"]),
            source_kind="rule",
            rule=f"interaction:{event['event_type']}",
            event_start_time=(texts[0].get("time") if texts else None),
            event_end_time=(texts[-1].get("time") if texts else None),
            time_confidence={"full_timestamp": "full",
                             "mixed": "partial"}.get(event["time_basis"], "unknown"),
            stance="supporting" if event["direction"] == DIRECTION_SUPPORTING
            else ("counter" if event["direction"] == DIRECTION_COUNTER
                  else "unspecified"),
            evidence_note=event["reason"],
            alternative=event["alternative_explanation"],
            flags={"media_unknown": bool(event.get("media_content_unknown")),
                   "time_uncertain": event["time_basis"] != "full_timestamp",
                   "context_missing": False,
                   "cross_run_duplicate": False},
            source_run_id=None,
            msg_texts=[{"index": window["start_index"] + offset,
                        "speaker": str(t.get("speaker") or ""),
                        "time": t.get("time"),
                        "text": _text_of(t)[:24]}
                       for offset, t in enumerate(texts)],
        ))
    return candidates


# ---------------------------------------------------------------------------
# 报告视图（剥离本地身份字段；默认不含聊天文本 / 指纹 /昵称）
# ---------------------------------------------------------------------------


def report_view(interaction: dict) -> dict:
    """可导出视图：去掉 fingerprints / identity 等本地字段（§58）。"""
    events = []
    for e in interaction.get("events", []):
        events.append({k: v for k, v in e.items()
                       if k not in ("fingerprints", "identity")})
    return {
        "version": interaction.get("version", INTERACTION_VERSION),
        "engine_available": interaction.get("engine_available", False),
        "time_basis": interaction.get("time_basis"),
        "events": events,
        "observations": interaction.get("observations", {}),
        "capabilities": interaction.get("capabilities", {}),
        "summary_lines": interaction.get("summary_lines", []),
        "diagnostics": interaction.get("diagnostics", {}),
    }
