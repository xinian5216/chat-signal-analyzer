"""行为事件层（Longitudinal Phase 2A：长期行为证据与态度观察的本地基础）。

定位：在好友档案里建立**独立的「行为事件」层**，而不是把九项评分平均成
一个"尊重分"或"喜欢概率"。事件记录的是**可以指回具体聊天记录的观察**：
什么方向的行为、发生在什么时间、支持与相反证据、替代解释、人工审核状态。

四条不可妥协的规则：

1. **候选 ≠ 结论**：本模块只做确定性规则（连续同说话方 turn、明确的提问 /
   后续安排、明确的拒绝 / 关心 / 邀约措辞）与既有九问结果的**辅助筛选**，
   生成的全部是**待人工核对**的候选事件；人工确认前绝不进入报告结论；
2. **事件 ≠ 消息条数**：同一次互动里的连续多条消息（哪怕一分钟内五条关心）
   归并为**同一个候选事件**；事件边界由用户在核对时调整，不用固定时间
   间隔冒充互动边界；
3. **没有原文不许编**：历史分析快照只保存了指纹与九问结果（无正文），
   由历史来源生成的候选一律标记"上下文缺失"，报告不得凭指纹或旧总结
   重新生成聊天正文；
4. **时间归位用聊天时间**：事件时间取消息的完整时间（``YYYY-MM-DD HH:MM``，
   经 ``timeline.time_kind`` 真实日历校验）；缺时间的事件标记
   ``time_confidence`` 并在报告里作为限制展示，绝不猜测日期。

本模块是**纯本地、0 Jev API**：不调用任何模型，不改动九问 / 评分公式 /
Context Builder / 分析 schema；出站白名单（``build_state``）不受影响，
好友昵称 / 正文 / 指纹与 ``friend_id`` 绝不外发。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from merge import message_fingerprint
from parser import MEDIA_MARKERS, MEDIA_MARKER_TAIL
from timeline import TIME_FULL, TIME_MISSING, TIME_ONLY, time_kind

# ---------------------------------------------------------------------------
# 四方向与行为类型（观察方向，不是人格诊断，也不能换算成心理概率）
# ---------------------------------------------------------------------------

DIMENSION_RESPECT = "respect"      # 尊重与边界
DIMENSION_CARE = "care"            # 关心与回应性
DIMENSION_INITIATIVE = "initiative"  # 主动性与投入
DIMENSION_ROMANCE = "romance"      # 好感与关系性质

DIMENSIONS = (DIMENSION_RESPECT, DIMENSION_CARE,
              DIMENSION_INITIATIVE, DIMENSION_ROMANCE)

DIMENSION_LABELS = {
    DIMENSION_RESPECT: "尊重与边界",
    DIMENSION_CARE: "关心与回应性",
    DIMENSION_INITIATIVE: "主动性与投入",
    DIMENSION_ROMANCE: "好感与关系性质",
}

# 每个方向的行为类型：严格对应任务书对四个观察方向的定义，不发明新维度。
BEHAVIOR_TYPES: dict[str, tuple[tuple[str, str], ...]] = {
    DIMENSION_RESPECT: (
        ("disagreement_response", "不同意见时的回应"),
        ("refusal_reaction", "明确拒绝后的反应"),
        ("pressure_or_disdain", "施压或贬低"),
        ("conflict_repair", "冲突修复"),
    ),
    DIMENSION_CARE: (
        ("emotion_understanding", "理解情绪"),
        ("care_response", "认真回应困难"),
        ("concrete_support", "提供具体支持"),
        ("continued_attention", "持续关注"),
    ),
    DIMENSION_INITIATIVE: (
        ("proactive_contact", "主动发起交流"),
        ("topic_continuation", "延续话题"),
        ("invitation", "提出邀约"),
        ("concrete_arrangement", "具体安排与后续落实"),
    ),
    DIMENSION_ROMANCE: (
        ("ordinary_friendly", "普通友好"),
        ("close_friendship", "亲密友情"),
        ("special_attention", "特殊关注"),
        ("romantic_expression", "明确浪漫表达"),
    ),
}

BEHAVIOR_TYPE_LABELS = {
    f"{dim}.{key}": label
    for dim, items in BEHAVIOR_TYPES.items()
    for key, label in items
}

STANCE_LABELS = {
    "supporting": "支持性",
    "counter": "相反",
    "mixed": "混合",
    "unspecified": "待人工标注",
}

SOURCE_LABELS = {
    "rule": "确定性规则辅助筛选（当前导入）",
    "history": "历史分析的既有指标辅助筛选（无正文）",
    "manual": "人工创建",
}

STATUS_LABELS = {
    "candidate": "待人工核对",
    "confirmed": "已人工确认",
    "rejected": "已人工排除",
}

TIME_CONFIDENCE_LABELS = {
    "full": "时间可归位",
    "partial": "时间不完整",
    "unknown": "时间不明",
}

# 注意：候选**不在生成阶段截断**。历史上曾在这里设 40 条上限，导致用户
# 逐批处理完前 40 条后，后面的候选再也无法出现在分页里（先排除后分页的
# 顺序才是对的：先生成全部 → 按事件身份剔除已确认/已排除 → 调用方分页，
# 每批只渲染少量控件）。

# 防御性绝对上限：异常巨大的聊天（十万级消息）也不至于把内存拖爆。
# 正常聊天下远达不到；达到时面板会提示有候选未列出。
MAX_CANDIDATES_HARD_CAP = 2000

# 关心片段合并：两条同说话方的关心的候选之间，只夹着多短的我方应答才并入
# 同一次互动（"1 分钟内五条关心 = 一次互动"的推广）。
_ACK_MAX_CHARS = 8

# 持续关注：困难发言之后多少天内出现询问，才算候选
CONTINUED_ATTENTION_DAYS = 5

# 主动发起：TA 在我上一条之后间隔多少分钟才判定"主动发起交流"候选
INITIATIVE_GAP_MINUTES = 180

# 后续落实：邀约/安排之后多少天内出现同类内容的落实措辞
FOLLOW_UP_DAYS = 7


# ---------------------------------------------------------------------------
# 确定性措辞标记（只用于**定位候选**，不构成任何心理判断）
# ---------------------------------------------------------------------------
#
# 说明：这些都是正则命中的"措辞类别"，用于把可能的互动窗口找出来，交给
# 人工核对。命中不代表结论（例如"哈哈"之后的玩笑语气、口头上级的客套
# 关心），因此每条候选都附带替代解释，由用户最终标注支持/相反。


def _re(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns))


REFUSAL_RE = _re(
    r"还是少聊点", r"少联系", r"先这样吧", r"不用了", r"不太方便",
    r"下次吧", r"算了吧", r"不用麻烦", r"以后别", r"做朋友就好", r"保持距离",
)
PRESSURE_RE = _re(
    r"为什么不行", r"必须", r"一定要", r"我不管", r"你听我说", r"别想了",
    r"由不得你", r"少废话",
)
DISAGREEMENT_RE = _re(
    r"我觉得不是", r"不对", r"但是", r"可是", r"未必", r"不同意",
    r"搞错了吧", r"有问题",
)
CARE_RE = _re(
    r"辛苦", r"累不累", r"别难过", r"别难受", r"抱抱", r"心疼", r"你还好吗",
    r"怎么了", r"别勉强", r"注意身体", r"早点睡", r"别熬", r"吃了吗",
)
DIFFICULTY_RE = _re(
    r"好累", r"好难受", r"烦死", r"崩溃", r"不开心", r"失眠", r"压力好大",
    r"生病", r"感冒", r"好烦", r"委屈",
)
SUPPORT_RE = _re(
    r"我陪你", r"我帮", r"需要我", r"我给你", r"要不要我", r"我请",
    r"带你去", r"跟我说", r"我在呢",
)
QUESTION_RE = re.compile(r"[？?]")
INVITATION_RE = _re(
    r"一起", r"出来", r"见面", r"请你", r"找你", r"去玩", r"吃饭", r"看电影",
    r"走走", r"出去",
)
ARRANGEMENT_RE = _re(
    r"周六", r"周日", r"星期", r"明天", r"后天", r"下周", r"几点", r"地点",
    r"定位", r"订位", r"我过去", r"你过来", r"出发",
)
FOLLOW_UP_RE = _re(
    r"定了", r"说定了", r"到时见", r"到时候", r"别忘了", r"出发了", r"来了吗",
    r"到了吗", r"见到你",
)
REPAIR_RE = _re(
    r"对不起", r"抱歉", r"我错了", r"是我不好", r"别生气", r"原谅",
)
CONFLICT_RE = _re(
    r"生气", r"不高兴", r"算了", r"随你", r"别说了", r"吵架",
)
DISDAIN_RE = _re(
    r"你这个人", r"真是服了", r"幼稚", r"可笑", r"想太多", r"有病",
    r"神经", r"废物", r"矫情",
)
CLOSE_FRIEND_RE = _re(
    r"兄弟", r"哥们", r"闺蜜", r"老铁", r"自己人", r"咱们",
)
SPECIAL_ATTENTION_RE = _re(
    r"只告诉你", r"别人我都不", r"特意", r"专门", r"单独给你", r"只有你",
)
ROMANTIC_RE = _re(
    r"喜欢你", r"爱你", r"想你", r"宝贝", r"亲爱的", r"么么", r"亲亲",
    r"做我女朋友", r"做我男朋友", r"在一起", r"表白",
)
ACK_RE = re.compile(r"^(嗯+|哦+|好|好的|好吧|行|收到|呵+|哈+|呵|噢+|噢)+$")

# 延续话题用的通用词：太常见的词不构成"接话"证据
_TOKEN_STOPWORDS = frozenset(
    "你 我 他 她 的 了 是 不 在 有 就 都 也 和 要 会 说 很 还 吧 呢 啊 哦 "
    "嗯 好 对 把 被 给 让 跟 与 及 或 而 且 但 因为 所以 如果 这个 那个 "
    "什么 怎么 现在 一下 一个 我们 你们 他们 觉得 知道 没有 不能 可以".split()
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """连续同一说话方的消息区间（一次互动的自然边界）。"""

    speaker: str
    start: int      # 含
    end: int        # 含

    @property
    def is_them(self) -> bool:
        return self.speaker == "them"


def split_turns(messages: list[dict]) -> list[Turn]:
    """把消息列表切成 turn（连续同说话方）。纯函数，不修改输入。"""
    turns: list[Turn] = []
    for i, m in enumerate(messages):
        speaker = str(m.get("speaker") or "unknown")
        if turns and turns[-1].speaker == speaker:
            turns[-1].end = i
        else:
            turns.append(Turn(speaker=speaker, start=i, end=i))
    return turns


@dataclass
class EventCandidate:
    """一个待人工核对的行为候选（尚未写入档案，也未进入报告结论）。"""

    dimension: str
    behavior_type: str
    start: int
    end: int
    fingerprints: list[str]
    source_kind: str                  # rule / history
    rule: str                         # 命中的确定性规则 id（可复现）
    event_start_time: str | None
    event_end_time: str | None
    time_confidence: str              # full / partial / unknown
    stance: str = "unspecified"
    evidence_note: str = ""           # 确定性依据（只有类别，没有判断）
    alternative: str = ""             # 至少一种合理的替代解释
    support_hint: str = ""            # 确认"支持性"时该看到什么
    counter_hint: str = ""            # 确认"相反"时该看到什么
    flags: dict = field(default_factory=dict)
    source_run_id: str | None = None
    msg_texts: list[dict] = field(default_factory=list)  # 仅内存展示，绝不入库
    # #20 Interaction Dynamics：order-aware 原始候选身份（序列事件专用）。
    # legacy 候选不传该字段 -> 空字符串 -> 行为与身份完全不变。
    original_candidate_identity: str = ""

    @property
    def identity(self) -> str:
        return event_identity(self.fingerprints, self.dimension,
                              self.behavior_type)

    @property
    def window_size(self) -> int:
        return len(self.fingerprints)


@dataclass
class BehaviorEvent:
    """已写入档案的行为事件（候选被确认/排除后，或用户手动创建）。"""

    event_id: str
    friend_id: str
    dimension: str
    behavior_type: str
    stance: str = "unspecified"
    status: str = "candidate"         # candidate / confirmed / rejected
    source_kind: str = "rule"
    source_run_id: str | None = None
    event_start_time: str | None = None
    event_end_time: str | None = None
    time_confidence: str = "unknown"
    msg_window: list[int] = field(default_factory=list)  # [start, end]（来源排序，仅供展示）
    fingerprints: list[str] = field(default_factory=list)
    flags: dict = field(default_factory=dict)
    alternative: str = ""
    support_evidence: str = ""
    counter_evidence: str = ""
    notes: str = ""
    snippet: str = ""                 # 用户选择保留的脱敏片段（默认空）
    user_feeling: str = ""            # 用户主观感受（独立记录，非客观证据）
    review_note: str = ""             # 审核备注（规则依据 / 修正说明）
    created_at: float = 0.0
    updated_at: float = 0.0
    reviewed_at: float | None = None
    event_identity: str = ""

    def to_dict(self) -> dict:
        out = dict(self.__dict__)
        out["msg_window"] = list(self.msg_window or [])
        out["fingerprints"] = list(self.fingerprints or [])
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "BehaviorEvent":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def event_identity(fingerprints: list[str], dimension: str,
                   behavior_type: str) -> str:
    """事件身份 = 消息指纹集合 + 方向 + 行为类型。

    重复导入同一批聊天（不同批次覆盖、不同保存时间）→ 指纹集合不变 →
    身份不变 → 不会重复计算。同时间不同内容的消息指纹不同，也不会被
    误判为同一事件。
    """
    fps = sorted({str(f) for f in fingerprints or []})
    payload = "\n".join(["behavior-event-v1", dimension, behavior_type] + fps)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_event_id() -> str:
    import secrets

    return secrets.token_hex(12)


# 每个确定性规则产出的 (方向, 行为类型) 固定映射。
# 用途：legacy 事件（v3 迁移前保存、没有 original_candidate_identity）
# 的回填——用 review_note 里记录的 rule + 事件自己的指纹集合反推出
# **原始候选**身份。规则与类型的对应由
# ``test_rule_behavior_mapping_matches_generated_candidates`` 钉死。
RULE_BEHAVIOR: dict[str, tuple[str, str]] = {
    # 关心与回应性
    "care_turn": (DIMENSION_CARE, "emotion_understanding"),
    "difficulty_then_reply": (DIMENSION_CARE, "care_response"),
    "support_offer": (DIMENSION_CARE, "concrete_support"),
    "difficulty_then_later_question": (DIMENSION_CARE, "continued_attention"),
    # 尊重与边界
    "disagreement_then_reply": (DIMENSION_RESPECT, "disagreement_response"),
    "refusal_reaction": (DIMENSION_RESPECT, "refusal_reaction"),
    "refusal_then_pressure": (DIMENSION_RESPECT, "pressure_or_disdain"),
    "disdain": (DIMENSION_RESPECT, "pressure_or_disdain"),
    "conflict_then_repair": (DIMENSION_RESPECT, "conflict_repair"),
    # 主动性与投入
    "proactive_after_gap": (DIMENSION_INITIATIVE, "proactive_contact"),
    "shared_content_word": (DIMENSION_INITIATIVE, "topic_continuation"),
    "invitation": (DIMENSION_INITIATIVE, "invitation"),
    "invitation_with_arrangement": (DIMENSION_INITIATIVE,
                                    "concrete_arrangement"),
    "invitation_then_follow_up": (DIMENSION_INITIATIVE,
                                  "concrete_arrangement"),
    # 好感与关系性质
    "closest_friend_marker": (DIMENSION_ROMANCE, "close_friendship"),
    "special_attention_marker": (DIMENSION_ROMANCE, "special_attention"),
    "romantic_marker": (DIMENSION_ROMANCE, "romantic_expression"),
    # 历史快照辅助筛选
    "history_intent_care": (DIMENSION_CARE, "care_response"),
    "history_distancing_strong": (DIMENSION_RESPECT, "refusal_reaction"),
    "history_romantic_strong": (DIMENSION_ROMANCE, "romantic_expression"),
}

# review_note 里记录候选规则的固定前缀（build_event_dict 写入）
REVIEW_RULE_PREFIX = "候选命中规则："


def valid_pair(dimension: str, behavior_type: str) -> bool:
    """方向 / 行为类型组合是否合法（行为类型必须属于该方向）。"""
    return behavior_type in {key for key, _ in BEHAVIOR_TYPES.get(dimension, ())}


def reviewed_identities(events: list[dict]) -> set[str]:
    """全部"已审核"身份 = 最终事件身份 ∪ 原始候选身份。

    - ``event_identity``：最终事件身份（未修改推荐时与候选身份相同）；
    - ``original_candidate_identity``：**原始推荐**的稳定身份——用户改了
      方向 / 类型 / 范围之后，最终事件身份会变，但原始候选身份不变，
      原候选必须凭它判定为已审核（否则会反复出现——已修复的真实缺陷）。
    手动创建的事件没有原始候选身份（original 为空），不参与候选过滤。
    """
    known: set[str] = set()
    for event in events:
        if event.get("status") not in ("confirmed", "rejected"):
            continue
        identity = str(event.get("event_identity") or "")
        if identity:
            known.add(identity)
        known.update(original_identities(event))
    return known


def encode_original_identities(identities) -> str:
    """原始候选身份集合 → 存储形态（单个写标量，多个写 JSON 数组）。

    与 :func:`original_identities` 互为逆运算；保持 v3 标量行可读。
    """
    seen: list[str] = []
    for value in identities or []:
        s = str(value or "")
        if s and s not in seen:
            seen.append(s)
    if not seen:
        return ""
    if len(seen) == 1:
        return seen[0]
    return json.dumps(seen, ensure_ascii=False)


def original_identities(event: dict) -> list[str]:
    """事件的全部「原始候选身份」（一个最终事件可能来自多个原始候选）。

    存储兼容两种形态：

    - v3 标量：单个 identity 字符串；
    - 多值：JSON 数组字符串（与仓库已有的 ``fingerprints_json`` /
      ``msg_window_json`` 同款列内 JSON 模式，**不新增表结构**）。

    容忍脏数据：解析失败 / 非字符串元素一律忽略（宁可少关联，不猜）。
    """
    raw = str((event or {}).get("original_candidate_identity") or "")
    if not raw.strip():
        return []
    if raw.lstrip().startswith("["):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        out: list[str] = []
        for item in data:
            value = str(item or "")
            if value and value not in out:
                out.append(value)
        return out
    return [raw]


def pending_candidates(candidates: list[EventCandidate],
                       events: list[dict]) -> list[EventCandidate]:
    """先生成**全部**候选 → 按事件身份剔除已确认 / 已排除 → 再分页。

    顺序不可交换：在生成阶段截断会让"逐批处理完前 N 条"之后的候选永远
    消失（Phase 2A.1 修复的真实缺陷）。控件数量由调用方分页控制。
    """
    known = reviewed_identities(events)
    kept: list[EventCandidate] = []
    for candidate in candidates:
        # #20：序列型候选（interaction events）携带 order-aware 原始身份时，
        # **只**按该身份判定“已审核”——同一指纹集合但因果顺序不同的事件不会
        # 被误判为已处理；legacy 候选（无该字段）行为完全不变。
        original = str(getattr(candidate, "original_candidate_identity",
                               "") or "")
        if original:
            if original in known:
                continue
        elif candidate.identity in known:
            continue
        kept.append(candidate)
    return kept


def parse_review_rule(event: dict) -> str | None:
    """从事件里取回生成它的候选规则 id（review_note 由本模块写入，格式固定）。

    review_note 形如 ``候选命中规则：care_turn；<确定性依据>``；人工创建 /
    历史来源 / 备注被用户改写过导致解析失败时返回 None（宁可不知道，
    不猜测）。
    """
    note = str((event or {}).get("review_note") or "")
    if not note.startswith(REVIEW_RULE_PREFIX):
        return None
    rest = note[len(REVIEW_RULE_PREFIX):]
    rule = rest.split("；", 1)[0].strip()
    return rule or None


def original_identity_from_event(event: dict) -> str:
    """从一条**已保存**的事件反推它来源的原始候选 identity。

    证据链（全部来自事件自身已持久化的字段，不猜测用户意图）：

    1. ``review_note`` 里的候选规则 id（``parse_review_rule``）；
    2. ``RULE_BEHAVIOR[rule]`` → 该规则产出的 (方向, 行为类型)——
       每个规则的产出固定，因此 (窗口, 规则) 唯一确定一个候选；
    3. 事件自己的 ``fingerprints``（最终窗口的消息指纹集合）。

    → ``event_identity(fps, rule_dim, rule_type)`` 即**原始候选**身份。

    只覆盖"范围未改"的情形（原始窗口 == 最终窗口）：用户连范围一起改过
    时，反推身份不会匹配任何现存候选（窗口不同），因此不会产生错误关联。
    证据不足（无 rule / 无指纹 / 未知 rule）时返回空串。
    """
    rule = parse_review_rule(event)
    if not rule:
        return ""
    pair = RULE_BEHAVIOR.get(rule)
    if pair is None:
        return ""
    fps = [str(f) for f in (event or {}).get("fingerprints") or [] if f]
    if not fps:
        return ""
    return event_identity(fps, pair[0], pair[1])


def plan_backfill(store, *, friend_id: str | None = None) -> dict:
    """回填**预览**：只统计、不写入（应用入口先看数再执行）。

    返回 ``{checked, would_fill, would_skip, already_linked}``：

    - ``would_fill``：证据充分、执行回填会被关联的事件数；
    - ``would_skip``：source_kind 是规则候选但没有证据（无规则备注 /
      未知规则 / 无指纹）——执行时也会跳过，原候 choices 留给用户重新
      核对（不猜）；
    - ``already_linked``：已有原始候选身份的事件（不计入）。
    """
    would_fill = would_skip = already_linked = 0
    friends = ([friend_id] if friend_id
               else [f.friend_id for f in store.list_friends()])
    for fid in friends:
        for event in store.list_events(fid):
            if event.get("status") not in ("confirmed", "rejected"):
                continue
            if event.get("source_kind") != "rule":
                continue
            if original_identities(event):
                already_linked += 1
                continue
            if original_identity_from_event(event):
                would_fill += 1
            else:
                would_skip += 1
    return {"checked": would_fill + would_skip,
            "would_fill": would_fill, "would_skip": would_skip,
            "already_linked": already_linked}


def backfill_original_identities(store, *, friend_id: str | None = None
                                 ) -> dict:
    """为 v3 迁移前的旧事件补写 ``original_candidate_identity``。

    保守策略（对应"缺少充分证据不能自动猜测"的硬约束）：

    - 只**新增**原始候选身份这一列的值，绝不改动方向 / 类型 / 立场 /
      片段等任何用户数据，绝不删除事件；
    - 只处理 ``source_kind == "rule"``（确定性规则候选审核而来）且当前
      原始身份为空的事件；
    - 用 :func:`original_identity_from_event` 从事件自身证据反推；
      证据不足 → 跳过并计入 ``skipped``（原候选会继续出现在待审核列表，
      由用户重新核对——不猜、不丢数据）；
    - 同一原始身份被多个事件共享是允许的（用户修改后多次保存），这不
      构成重复计算：待审核过滤按身份去重，事件按 event_id 独立统计。

    返回 {"checked", "filled", "skipped"} 统计（供日志 / 测试断言）。
    """
    checked = filled = skipped = 0
    friends = ([friend_id] if friend_id
               else [f.friend_id for f in store.list_friends()])
    for fid in friends:
        for event in store.list_events(fid):
            if original_identities(event):
                continue
            if event.get("source_kind") != "rule":
                continue
            if event.get("status") not in ("confirmed", "rejected"):
                continue
            checked += 1
            original = original_identity_from_event(event)
            if not original:
                skipped += 1
                continue
            store.set_event_original_identity(event["event_id"], original)
            filled += 1
    return {"checked": checked, "filled": filled, "skipped": skipped}


def current_matches_by_fingerprint(fingerprint: str,
                                   messages: list[dict]) -> list[int]:
    """在当前导入里找与给定历史指纹**逐字节一致**的消息下标。

    这是为将来「历史候选 <-> 当前聊天」关联准备的原语：只有指纹一致才
    认为可能是同一条消息；是否关联仍必须由用户显式确认，任何自动合并
    都不允许。返回空列表 = 当前聊天里没有这条消息。同一指纹在导入里
    出现多次时全部返回（由人工选择，不猜）。
    """
    want = str(fingerprint or "")
    if not want:
        return []
    return [i for i, m in enumerate(messages)
            if message_fingerprint(m) == want]


# ---------------------------------------------------------------------------
# 人工核对 → 事件（构造存储用 dict；脱敏在调用方经 friend_history 完成）
# ---------------------------------------------------------------------------


def window_fingerprints(messages: list[dict], start: int,
                        end: int) -> list[str]:
    """窗口内消息的指纹集合（公开接口，供 UI 边界调整后重算身份）。"""
    return _fingerprints(messages, max(0, start),
                         min(len(messages) - 1, end))


def build_event_dict(*, candidate: EventCandidate | None, friend_id: str,
                     dimension: str, behavior_type: str, stance: str,
                     notes: str = "", snippet: str = "",
                     user_feeling: str = "", alternative: str = "",
                     messages: list[dict] | None = None,
                     start: int | None = None, end: int | None = None,
                     status: str = "confirmed",
                     original_candidate_identity: str = "") -> dict:

    if not valid_pair(dimension, behavior_type):
        raise ValueError(
            f"行为类型「{behavior_type}」不属于方向「{dimension}」")
    """把人工核对结果打包成可写入档案的事件 dict。

    - ``candidate`` 为 None → 人工手动创建的事件；
    - 用户调整了边界（``start`` / ``end`` 与候选不同）且有 ``messages`` →
      按新窗口重算指纹与身份（身份随边界改变而改变是预期行为：用户重新
      定义了事件范围）；
    - ``snippet`` 必须已由调用方脱敏（写入前 ``save_event`` 不再改内容，
      UI 层负责展示"正则脱敏不能保证完全匿名"的提醒）。
    """
    if candidate is None:
        if messages is None or start is None or end is None:
            raise ValueError("manual event requires messages + window")
        lo, hi = sorted((max(0, int(start)), min(len(messages) - 1,
                                                 int(end))))
        first, last = _window_times(messages, lo, hi)
        event = {
            "dimension": dimension, "behavior_type": behavior_type,
            "stance": stance, "status": status, "source_kind": "manual",
            "source_run_id": None,
            "event_start_time": first, "event_end_time": last,
            "time_confidence": _time_confidence_for(messages[lo:hi + 1]),
            "msg_window": [lo, hi],
            "fingerprints": _fingerprints(messages, lo, hi),
            "flags": {
                "media_unknown": _media_flag(messages, lo, hi),
                "time_uncertain": _time_confidence_for(
                    messages[lo:hi + 1]) != "full",
                "context_missing": False, "cross_run_duplicate": False,
            },
        }
    else:
        event = {
            "dimension": dimension, "behavior_type": behavior_type,
            "stance": stance, "status": status,
            "source_kind": candidate.source_kind,
            "source_run_id": candidate.source_run_id,
            "event_start_time": candidate.event_start_time,
            "event_end_time": candidate.event_end_time,
            "time_confidence": candidate.time_confidence,
            "msg_window": [candidate.start, candidate.end],
            "fingerprints": list(candidate.fingerprints),
            "flags": dict(candidate.flags or {}),
        }
        if (messages is not None and start is not None and end is not None
                and (int(start) != candidate.start
                     or int(end) != candidate.end)):
            lo, hi = sorted((max(0, int(start)),
                             min(len(messages) - 1, int(end))))
            event["msg_window"] = [lo, hi]
            event["fingerprints"] = _fingerprints(messages, lo, hi)
            first, last = _window_times(messages, lo, hi)
            event["event_start_time"] = first
            event["event_end_time"] = last
            event["time_confidence"] = _time_confidence_for(
                messages[lo:hi + 1])
            flags = dict(event["flags"])
            flags["time_uncertain"] = event["time_confidence"] != "full"
            flags["media_unknown"] = _media_flag(messages, lo, hi)
            event["flags"] = flags

    if candidate is not None and not alternative:
        alternative = candidate.alternative
    event.update({
        "friend_id": friend_id,
        # 原始候选身份：候选被人工修改（方向 / 类型 / 范围）后，最终事件
        # 身份会变，但原始候选必须凭这个身份判定为已审核。
        # 手动创建的事件没有来源候选 → 空串。
        "original_candidate_identity": str(
            original_candidate_identity
            or getattr(candidate, "original_candidate_identity", "")
            or (candidate.identity if candidate else "")),
        "stance": stance,
        "notes": str(notes or ""),
        "snippet": str(snippet or ""),
        "user_feeling": str(user_feeling or ""),
        "alternative": str(alternative or ""),
        "support_evidence": (candidate.support_hint if candidate else ""),
        "counter_evidence": (candidate.counter_hint if candidate else ""),
        "review_note": (f"候选命中规则：{candidate.rule}；"
                        f"{candidate.evidence_note}" if candidate
                        else "用户手动创建"),
        "created_at": 0.0, "updated_at": 0.0, "reviewed_at": None,
    })
    event["event_identity"] = event_identity(event["fingerprints"],
                                             event["dimension"],
                                             event["behavior_type"])
    event["event_id"] = ""            # 由 FriendStore.save_event 分配
    return event


def known_candidate_events(events: list[dict]) -> dict[str, dict]:
    """已处理的候选 → 按 event_identity 索引（确认/排除后不再重复呈现）。"""
    out: dict[str, dict] = {}
    for event in events:
        if event.get("status") in ("confirmed", "rejected") \
                and event.get("event_identity"):
            out.setdefault(event["event_identity"], event)
    return out


# ---------------------------------------------------------------------------
# 候选生成（确定性规则；只定位，不下结论）
# ---------------------------------------------------------------------------


def _time_confidence_for(messages: list[dict]) -> str:
    kinds = {time_kind(m.get("time")) for m in messages}
    if kinds == {TIME_FULL}:
        return "full"
    if TIME_FULL in kinds:
        return "partial"
    if kinds == {TIME_MISSING}:
        return "unknown"
    return "partial"                  # 仅 time_only 的日子能确定前后、不能归位


def _window_times(messages: list[dict], start: int, end: int) -> tuple:
    times = [str(messages[i].get("time")) for i in range(start, end + 1)
             if time_kind(messages[i].get("time")) == TIME_FULL]
    if not times:
        return None, None
    return min(times), max(times)


def _fingerprints(messages: list[dict], start: int, end: int) -> list[str]:
    return [message_fingerprint(messages[i]) for i in range(start, end + 1)]


def _media_flag(messages: list[dict], start: int, end: int) -> bool:
    return any(
        messages[i].get("content_type") == "media"
        or (messages[i].get("media_kinds") or [])
        for i in range(start, end + 1)
    )


def _has_time_gap(messages: list[dict], a: int, b: int,
                  minutes: int) -> bool | None:
    """两条消息间隔 >= minutes 分钟（任一缺完整时间 → None，不猜）。"""
    ta, tb = messages[a].get("time"), messages[b].get("time")
    if time_kind(ta) != TIME_FULL or time_kind(tb) != TIME_FULL:
        return None
    try:
        dt_a = datetime.strptime(str(ta), "%Y-%m-%d %H:%M")
        dt_b = datetime.strptime(str(tb), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (dt_b - dt_a).total_seconds() / 60.0 >= minutes


def _days_between(messages: list[dict], a: int, b: int) -> float | None:
    ta, tb = messages[a].get("time"), messages[b].get("time")
    if time_kind(ta) != TIME_FULL or time_kind(tb) != TIME_FULL:
        return None
    try:
        dt_a = datetime.strptime(str(ta), "%Y-%m-%d %H:%M")
        dt_b = datetime.strptime(str(tb), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (dt_b - dt_a).total_seconds() / 86400.0


# 完整媒体 marker（含**带时长的语音变体**「[发送了一条 7 秒语音，内容未知]」：
# 时长是动态插入的，逐字 replace 匹配不到，必须用模式匹配整段）。
_MEDIA_MARKER_RE = re.compile(r"\[发送了[^\[\]]*?内容未知\]")
# 裸尾巴兜底（异常拼接的半截 marker）
_MEDIA_MARKER_TAIL_RE = re.compile(r"[，,]\s*内容未知\s*\]")


def strip_media_markers(text) -> str:
    """剥掉中性媒体 marker（``[发送了一张图片，内容未知]`` 等合成占位符）。

    marker 是 parser 合成的提示，**不是用户写的文字**：绝不能参与关键词 /
    词语重合检测（否则「图片」「内容」「发送」这类占位符词会让纯媒体消息
    伪装成「延续话题」「特殊关注」）。混合消息里的真实文字原样保留。

    必须整段移除（含时长变体）：只删尾巴会残留
    「[发送了一条 7 秒语音」这类碎片，其词元（发送了一条 / 秒语音）
    照样会参与重合检测——已按此回归固定。
    """
    out = str(text or "")
    out = _MEDIA_MARKER_RE.sub(" ", out)
    out = _MEDIA_MARKER_TAIL_RE.sub(" ", out)
    return out


def message_has_text(message: dict) -> bool:
    """消息是否含有可理解的文字（纯媒体占位符不算文字）。"""
    return bool(strip_media_markers(message.get("text")).strip())


def _is_media_only_message(message: dict) -> bool:
    """纯媒体消息（内容未知）——不构成可核对的行为内容。"""
    if str(message.get("content_type") or "") == "media":
        return True
    return not message_has_text(message)


def _window_is_media_only(messages: list[dict], start: int, end: int) -> bool:
    """窗口内 **TA 侧**消息是否全是纯媒体。

    行为事件描述的是对方的行为：我方发了文字而对方只回了纯图片 / 语音 /
    动画表情时，「认真回应困难 / 延续话题 / 主动发起 / 好感表达」之类的
    候选没有可核对的内容，不应进入人工审核队列（真实缺陷：用户被要求
    审核一堆「内容未知」的消息）。混合消息（文字 + 媒体）照常生成候选；
    媒体消息本身仍保留在聊天里当上下文，绝不删除。
    """
    them = [messages[i] for i in range(max(0, start), min(end, len(messages) - 1) + 1)
            if str(messages[i].get("speaker") or "") == "them"]
    return bool(them) and all(_is_media_only_message(m) for m in them)


def _content_tokens(text: str) -> set[str]:
    words = re.findall(r"[一-龥A-Za-z0-9]{2,}", strip_media_markers(text))
    return {w for w in words if w not in _TOKEN_STOPWORDS}


def _matched(text: str, pattern: re.Pattern) -> str:
    m = pattern.search(str(text or ""))
    return m.group(0) if m else ""


def _short_text(messages: list[dict], i: int, limit: int = 24) -> str:
    """展示用短预览（只进 session 内存 / UI，绝不写入档案）。"""
    text = " ".join(str(messages[i].get("text") or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _texts_for(messages: list[dict], start: int, end: int,
               limit: int = 24) -> list[dict]:
    """候选的上下文预览（speaker/time/text）。调用方只用于 UI 展示。"""
    out = []
    for i in range(start, end + 1):
        out.append({
            "index": i,
            "speaker": messages[i].get("speaker"),
            "time": messages[i].get("time"),
            "text": _short_text(messages, i, limit),
        })
    return out


def _enrich_note(messages: list[dict], results: list[dict] | None,
                 start: int, end: int) -> str:
    """把既有九问结果作为**辅助筛选**说明（不改名、不当结论）。"""
    if not results:
        return ""
    by_index = {e["index"]: e for e in results if not e.get("error")}
    bits: list[str] = []
    for i in range(start, end + 1):
        entry = by_index.get(i)
        if not entry:
            continue
        result = entry.get("result") or {}
        intent = (result.get("intent") or {}).get("choice")
        warmth = (result.get("warmth") or {}).get("score")
        if intent:
            bits.append(f"#{i + 1} intent={intent}")
        if warmth is not None:
            bits.append(f"warmth={warmth}")
    if not bits:
        return ""
    return "该窗口内消息的既有分析指标（仅辅助筛选，不构成尊重/喜欢结论）：" \
        + "，".join(bits)


def _candidate(*, dimension: str, behavior_type: str, messages: list[dict],
               start: int, end: int, source_kind: str, rule: str,
               evidence_note: str, alternative: str, support_hint: str,
               counter_hint: str, results: list[dict] | None = None,
               source_run_id: str | None = None,
               extra_flags: dict | None = None,
               context_missing: bool = False) -> EventCandidate:
    start_i, end_i = max(0, start), min(len(messages) - 1, end)
    window = messages[start_i:end_i + 1]
    first, last = _window_times(messages, start_i, end_i)
    flags = {
        "media_unknown": _media_flag(messages, start_i, end_i),
        "time_uncertain": _time_confidence_for(window) != "full",
        "context_missing": bool(context_missing),
        "cross_run_duplicate": False,
    }
    if extra_flags:
        flags.update(extra_flags)
    note = evidence_note
    enriched = _enrich_note(messages, results, start_i, end_i)
    if enriched:
        note = (note + "；" if note else "") + enriched
    return EventCandidate(
        dimension=dimension, behavior_type=behavior_type,
        start=start_i, end=end_i,
        fingerprints=_fingerprints(messages, start_i, end_i),
        source_kind=source_kind, rule=rule,
        event_start_time=first, event_end_time=last,
        time_confidence=_time_confidence_for(window),
        evidence_note=note, alternative=alternative,
        support_hint=support_hint, counter_hint=counter_hint,
        flags=flags, source_run_id=source_run_id,
        msg_texts=_texts_for(messages, start_i, end_i),
    )


# ---- 规则：关心与回应性 -----------------------------------------------------


def _rule_care(messages: list[dict], results: list[dict] | None,
               out: list[EventCandidate]) -> None:
    turns = split_turns(messages)

    # R-CARE-1 理解情绪 / 关心片段：TA turn 命中关心措辞；相邻的 TA 关心
    # turn 之间只夹着我方短应答（嗯/哦/好…）的，并入同一次互动——
    # "一分钟内五条关心 = 一次互动"，不是五次独立关心。
    groups: list[list[Turn]] = []
    current: list[Turn] = []
    for turn in turns:
        if not turn.is_them:
            acks_only = all(
                len(str(messages[i].get("text") or "").strip())
                <= _ACK_MAX_CHARS and ACK_RE.match(
                    str(messages[i].get("text") or "").strip() or "x")
                for i in range(turn.start, turn.end + 1))
            if current and acks_only:
                continue                    # 短应答不打断同一次互动
            if current:
                groups.append(current)
            current = []
            continue
        hit = any(CARE_RE.search(str(messages[i].get("text") or ""))
                  for i in range(turn.start, turn.end + 1))
        if hit:
            current.append(turn)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    for group in groups:
        if not group:
            continue
        first, last = group[0].start, group[-1].end
        out.append(_candidate(
            dimension=DIMENSION_CARE,
            behavior_type="emotion_understanding",
            messages=messages, start=first, end=last,
            source_kind="rule", rule="care_turn",
            evidence_note="连续的关心措辞互动"
                           + ("（中间的我方短应答已并入同一次互动）"
                              if len(group) > 1 else ""),
            alternative="也可能是普通客套或礼貌回应，未必针对当时的情绪；"
                        "请结合前后文判断",
            support_hint="关心针对我当时的情绪或困难，且内容具体",
            counter_hint="只是客套话术，或与情绪无关的例行寒暄",
            results=results))

    # R-CARE-2 认真回应困难：我方困难发言 → 下一条 TA turn 有实质内容
    for i, turn in enumerate(turns):
        if turn.speaker != "me":
            continue
        if not any(DIFFICULTY_RE.search(str(messages[j].get("text") or ""))
                   for j in range(turn.start, turn.end + 1)):
            continue
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if nxt is None or not nxt.is_them:
            continue
        if not any(str(messages[j].get("text") or "").strip()
                   for j in range(nxt.start, nxt.end + 1)):
            continue
        out.append(_candidate(
            dimension=DIMENSION_CARE,
            behavior_type="care_response",
            messages=messages, start=turn.start, end=nxt.end,
            source_kind="rule", rule="difficulty_then_reply",
            evidence_note="我方困难发言之后的 TA 回应",
            alternative="对方可能只是普通一问一过，未必认真回应了困难本身",
            support_hint="回应针对困难内容本身（询问、安慰或实际建议）",
            counter_hint="只回复与困难无关的内容，或明显敷衍",
            results=results))

    # R-CARE-3 提供具体支持
    for turn in turns:
        if not turn.is_them:
            continue
        if any(SUPPORT_RE.search(str(messages[i].get("text") or ""))
               for i in range(turn.start, turn.end + 1)):
            out.append(_candidate(
                dimension=DIMENSION_CARE,
                behavior_type="concrete_support",
                messages=messages, start=turn.start, end=turn.end,
                source_kind="rule", rule="support_offer",
                evidence_note="出现具体支持性提议",
                alternative="可能是口头客气，后续并没有落实；"
                            "需要看之后的记录",
                support_hint="之后有对应的实际支持行为",
                counter_hint="说完就没有下文，或实际并未提供帮助",
                results=results))

    # R-CARE-4 持续关注：困难发言之后若干天内，TA 主动问起（且期间没有新的
    # 困难发言）——跨日期才算"持续"，同一天的不算。
    difficulties = [t for t in turns if t.speaker == "me" and any(
        DIFFICULTY_RE.search(str(messages[j].get("text") or ""))
        for j in range(t.start, t.end + 1))]
    for d in difficulties:
        for t in turns:
            if not t.is_them or t.start <= d.end:
                continue
            if not any(QUESTION_RE.search(str(messages[i].get("text") or ""))
                       for i in range(t.start, t.end + 1)):
                continue
            gap = _days_between(messages, d.end, t.start)
            if gap is None or gap > CONTINUED_ATTENTION_DAYS:
                continue
            if any(o is not d and o.start > d.start and o.end < t.start
                   for o in difficulties):
                continue
            out.append(_candidate(
                dimension=DIMENSION_CARE,
                behavior_type="continued_attention",
                messages=messages, start=d.start, end=t.end,
                source_kind="rule", rule="difficulty_then_later_question",
                evidence_note=f"困难发言之后 {gap:.1f} 天，TA 主动问起",
                alternative="也可能是随口开启新话题，未必是对之前困难的持续关注",
                support_hint="问题直接指向当时的困难",
                counter_hint="问题与之前的困难无关",
                results=results))
            break


# ---- 规则：尊重与边界 -------------------------------------------------------


def _rule_respect(messages: list[dict], results: list[dict] | None,
                  out: list[EventCandidate]) -> None:
    turns = split_turns(messages)

    # R-RESPECT-1 不同意见时的回应：我方分歧措辞 → 紧随的 TA turn
    for i, turn in enumerate(turns):
        if turn.speaker != "me":
            continue
        if not any(DISAGREEMENT_RE.search(str(messages[j].get("text") or ""))
                   for j in range(turn.start, turn.end + 1)):
            continue
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if nxt is None or not nxt.is_them:
            continue
        out.append(_candidate(
            dimension=DIMENSION_RESPECT,
            behavior_type="disagreement_response",
            messages=messages, start=turn.start, end=nxt.end,
            source_kind="rule", rule="disagreement_then_reply",
            evidence_note="意见不同之后的 TA 回应",
            alternative="也可能只是澄清事实或正常讨论，"
                        "并不存在分歧；正常争论不等于不尊重",
            support_hint="回应针对观点本身，语气平和",
            counter_hint="出现贬低、施压或拒绝讨论的姿态",
            results=results))

    # R-RESPECT-2 明确拒绝后的反应 / 施压：TA 拒绝措辞 → 之后最多两条 turn。
    # 施压措辞在**整个反应窗口**内检测（不分说话方）：对话模式
    # "被拒绝之后仍然有人施压"本身就是值得记录的互动，由人工标注立场。
    for i, turn in enumerate(turns):
        if not turn.is_them:
            continue
        if not any(REFUSAL_RE.search(str(messages[j].get("text") or ""))
                   for j in range(turn.start, turn.end + 1)):
            continue
        window_end = turn.end
        pressure_hits: list[str] = []
        covered: list[int] = []
        steps = 0
        for follow in turns[i + 1:]:
            if follow.speaker == "me":
                if steps > 0:
                    break            # 我方重新发言且 TA 已回应过 → 边界到此
                steps += 1
                window_end = follow.end
                covered.extend(range(follow.start, follow.end + 1))
                continue
            steps += 1
            window_end = follow.end
            covered.extend(range(follow.start, follow.end + 1))
            if steps >= 2:
                break
        for j in covered:
            hit = _matched(messages[j].get("text") or "", PRESSURE_RE)
            if hit:
                pressure_hits.append(hit)
        if pressure_hits:
            out.append(_candidate(
                dimension=DIMENSION_RESPECT,
                behavior_type="pressure_or_disdain",
                messages=messages, start=turn.start, end=window_end,
                source_kind="rule", rule="refusal_then_pressure",
                evidence_note=f"明确拒绝之后出现施压措辞（类别：{pressure_hits[0]}）",
                alternative="也可能是在争取沟通，动机需要更多上下文才能判断",
                support_hint="-", counter_hint="拒绝后持续施压、贬低或不顾边界",
                results=results))
        else:
            out.append(_candidate(
                dimension=DIMENSION_RESPECT,
                behavior_type="refusal_reaction",
                messages=messages, start=turn.start, end=window_end,
                source_kind="rule", rule="refusal_reaction",
                evidence_note="明确拒绝及之后的短暂互动",
                alternative="拒绝可能只是当时忙碌或时机不便，"
                            "未必是关系边界；请结合前后文",
                support_hint="接受拒绝、尊重边界",
                counter_hint="被拒后继续追问、施压或贬低",
                results=results))

    # R-RESPECT-3 施压或贬低（独立于拒绝场景；不分说话方，由人工标注）
    for turn in turns:
        if not any(DISDAIN_RE.search(str(messages[i].get("text") or ""))
                   for i in range(turn.start, turn.end + 1)):
            continue
        out.append(_candidate(
            dimension=DIMENSION_RESPECT,
            behavior_type="pressure_or_disdain",
            messages=messages, start=turn.start, end=turn.end,
            source_kind="rule", rule="disdain",
            evidence_note="出现施压或贬低类措辞",
            alternative="也可能是熟人之间的玩笑或口头禅；"
                        "语气无法从文本确定，请人工判断",
            support_hint="-", counter_hint="确实让对方感到被贬低或受压",
            results=results))

    # R-RESPECT-4 冲突修复：我方不满措辞 → TA 道歉/修复措辞
    for i, turn in enumerate(turns):
        if turn.speaker != "me":
            continue
        if not any(CONFLICT_RE.search(str(messages[j].get("text") or ""))
                   for j in range(turn.start, turn.end + 1)):
            continue
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if nxt is None or not nxt.is_them:
            continue
        if not any(REPAIR_RE.search(str(messages[j].get("text") or ""))
                   for j in range(nxt.start, nxt.end + 1)):
            continue
        out.append(_candidate(
            dimension=DIMENSION_RESPECT,
            behavior_type="conflict_repair",
            messages=messages, start=turn.start, end=nxt.end,
            source_kind="rule", rule="conflict_then_repair",
            evidence_note="冲突之后的道歉 / 修复",
            alternative="道歉也可能是敷衍或回避问题，未必真正修复",
            support_hint="道歉指向具体问题，之后行为有改变",
            counter_hint="只说对不起，之后照旧",
            results=results))


# ---- 规则：主动性与投入 -----------------------------------------------------


def _rule_initiative(messages: list[dict], results: list[dict] | None,
                     out: list[EventCandidate]) -> None:
    turns = split_turns(messages)

    # R-INIT-1 主动发起交流：TA turn 之前是我方发言且间隔超过阈值
    for i, turn in enumerate(turns):
        if not turn.is_them or i == 0:
            continue
        prev = turns[i - 1]
        if prev.speaker != "me":
            continue
        gap = _has_time_gap(messages, prev.end, turn.start,
                            INITIATIVE_GAP_MINUTES)
        if gap is None or not gap:
            continue
        out.append(_candidate(
            dimension=DIMENSION_INITIATIVE,
            behavior_type="proactive_contact",
            messages=messages, start=turn.start, end=turn.end,
            source_kind="rule", rule="proactive_after_gap",
            evidence_note=f"我方发言超过 {INITIATIVE_GAP_MINUTES // 60} 小时后"
                           "由 TA 主动开口",
            alternative="也可能只是当天第二次聊天的自然延续；"
                        "是否算'主动'可由你判断",
            support_hint="-", counter_hint="-",
            results=results))

    # R-INIT-2 延续话题：TA turn 与我上一条 turn 共享非通用实词
    for i, turn in enumerate(turns):
        if not turn.is_them or i == 0:
            continue
        prev = turns[i - 1]
        my_text = " ".join(str(messages[j].get("text") or "")
                           for j in range(prev.start, prev.end + 1))
        ta_text = " ".join(str(messages[j].get("text") or "")
                           for j in range(turn.start, turn.end + 1))
        shared = _content_tokens(my_text) & _content_tokens(ta_text)
        if shared:
            out.append(_candidate(
                dimension=DIMENSION_INITIATIVE,
                behavior_type="topic_continuation",
                messages=messages, start=prev.start, end=turn.end,
                source_kind="rule", rule="shared_content_word",
                evidence_note=f"TA 接续了我方内容词（类别：{sorted(shared)[0]}）",
                alternative="只是用了同一个常见词，不一定是接话",
                support_hint="-", counter_hint="-",
                results=results))

    # R-INIT-3 提出邀约；R-INIT-4 具体安排与后续落实
    for turn in turns:
        if not turn.is_them:
            continue
        hit = any(INVITATION_RE.search(str(messages[j].get("text") or ""))
                  for j in range(turn.start, turn.end + 1))
        if not hit:
            continue
        arrange_end = turn.end
        has_arrangement = any(
            ARRANGEMENT_RE.search(str(messages[j].get("text") or ""))
            for j in range(turn.start, turn.end + 1))
        if has_arrangement:
            out.append(_candidate(
                dimension=DIMENSION_INITIATIVE,
                behavior_type="concrete_arrangement",
                messages=messages, start=turn.start, end=arrange_end,
                source_kind="rule", rule="invitation_with_arrangement",
                evidence_note="邀约伴随具体安排措辞（时间 / 地点 / 吃什么）",
                alternative="也可能只是随口一提，未必真的执行",
                support_hint="-", counter_hint="-",
                results=results))
        else:
            out.append(_candidate(
                dimension=DIMENSION_INITIATIVE,
                behavior_type="invitation",
                messages=messages, start=turn.start, end=turn.end,
                source_kind="rule", rule="invitation",
                evidence_note="出现邀约措辞",
                alternative="可能只是客套邀请，没有具体时间地点",
                support_hint="-", counter_hint="-",
                results=results))
        # 后续落实：邀约之后若干天内出现同类落实措辞 → 安排事件的证据尾巴
        for follow in turns:
            if follow.start <= turn.end:
                continue
            gap = _days_between(messages, turn.end, follow.start)
            if gap is None or gap > FOLLOW_UP_DAYS:
                continue
            if not any(FOLLOW_UP_RE.search(str(messages[j].get("text") or ""))
                       or ARRANGEMENT_RE.search(
                           str(messages[j].get("text") or ""))
                       for j in range(follow.start, follow.end + 1)):
                continue
            out.append(_candidate(
                dimension=DIMENSION_INITIATIVE,
                behavior_type="concrete_arrangement",
                messages=messages, start=turn.start, end=follow.end,
                source_kind="rule", rule="invitation_then_follow_up",
                evidence_note=f"邀约之后 {gap:.1f} 天出现落实措辞（见面 /"
                               "出发 / 确认）",
                alternative="落实也可能被取消或搁置，需看更后面的记录",
                support_hint="-", counter_hint="-",
                results=results))
            break


# ---- 规则：好感与关系性质 -----------------------------------------------------


def _rule_romance(messages: list[dict], results: list[dict] | None,
                  out: list[EventCandidate]) -> None:
    turns = split_turns(messages)
    mapping = (
        (CLOSE_FRIEND_RE, "close_friendship", "closest_friend_marker",
         "亲密友情类措辞", "亲密友情、浪漫兴趣与特殊关注必须分别处理"),
        (SPECIAL_ATTENTION_RE, "special_attention", "special_attention_marker",
         "特殊关注类措辞", "特殊关注不等于浪漫兴趣"),
        (ROMANTIC_RE, "romantic_expression", "romantic_marker",
         "明确浪漫表达类措辞", "明确浪漫表达，需要结合上下文确认"),
    )
    for pattern, behavior_type, rule_id, note, alternative in mapping:
        for turn in turns:
            if not turn.is_them:
                continue
            if not any(pattern.search(str(messages[j].get("text") or ""))
                       for j in range(turn.start, turn.end + 1)):
                continue
            out.append(_candidate(
                dimension=DIMENSION_ROMANCE,
                behavior_type=behavior_type,
                messages=messages, start=turn.start, end=turn.end,
                source_kind="rule", rule=rule_id,
                evidence_note=note,
                alternative=alternative,
                support_hint="-", counter_hint="-",
                results=results))


_RULES = (
    ("care", _rule_care),
    ("respect", _rule_respect),
    ("initiative", _rule_initiative),
    ("romance", _rule_romance),
)


def generate_candidates_with_meta(
        messages: list[dict], results: list[dict] | None = None,
        *, limit: int | None = None) -> tuple[list[EventCandidate], dict]:
    """从当前导入的聊天生成**全部**待人工核对的行为候选（纯本地，0 Jev）。

    返回 ``(候选列表, 元信息)``；元信息含 ``generated``（去重后总数）、
    ``returned``（实际返回数）、``cap``、``truncated``——供主界面在候选
    被安全上限截断时明确提示，禁止静默截断。

    规则只做**定位**：连续同说话方 turn、明确提问、后续安排、明确的
    拒绝 / 关心 / 邀约 / 浪漫措辞。每个候选都带替代解释与人工标注指引；
    确认之前不构成任何关于尊重或喜欢的结论。

    去重：同一窗口同一方向同一行为类型只保留一个候选（指纹集合相同）。
    排序：按窗口起点、方向、行为类型——完全确定，重复运行结果一致。
    ``limit`` 默认 None（不截断）；分页由调用方负责，一次只渲染少量控件。

    无效候选过滤（在分页**之前**）：TA 侧窗口全是纯媒体消息的候选直接
    剔除并计入 ``media_filtered``——内容未知的消息不构成可核对的行为
    内容。过滤只丢候选，绝不修改 / 删除用户已确认的事件；已审核的原始
    候选身份仍按 ``pending_candidates`` 的双身份过滤，不会复活。
    """
    out: list[EventCandidate] = []
    for name, rule in _RULES:
        rule(messages, results, out)

    seen: set[str] = set()
    unique: list[EventCandidate] = []
    for candidate in out:
        if candidate.identity in seen:
            continue
        seen.add(candidate.identity)
        unique.append(candidate)
    unique.sort(key=lambda c: (c.start, c.end, c.dimension, c.behavior_type))

    usable: list[EventCandidate] = []
    media_filtered = 0
    for candidate in unique:
        if _window_is_media_only(messages, candidate.start, candidate.end):
            media_filtered += 1
            continue
        usable.append(candidate)

    cap = MAX_CANDIDATES_HARD_CAP if limit is None else max(0, limit)
    returned = usable[:cap]
    meta = {
        "generated": len(unique),      # 去重后的候选总数（低成本精确值）
        "returned": len(returned),      # 实际进入列表的数量
        "cap": cap,
        "truncated": len(usable) > cap,
        "media_filtered": media_filtered,   # 因纯媒体被剔除的候选数
    }
    return returned, meta


def generate_candidates(messages: list[dict],
                        results: list[dict] | None = None,
                        *, limit: int | None = None
                        ) -> list[EventCandidate]:
    """只要候选列表（兼容旧调用方）；需要截断元信息用
    :func:`generate_candidates_with_meta`。"""
    candidates, _meta = generate_candidates_with_meta(
        messages, results, limit=limit)
    return candidates


def truncation_notice(meta: dict | None) -> str | None:
    """候选被安全上限截断时的主界面提示（None = 没有截断，不显示）。

    禁止静默截断后仍声称已展示全部候选；正常数据规模不显示任何警告。
    """
    if not meta or not meta.get("truncated"):
        return None
    generated = int(meta.get("generated") or 0)
    cap = int(meta.get("cap") or 0)
    hidden = max(0, generated - cap)
    return (
        f"部分候选未显示：当前聊天里确定性规则共生成 **{generated}** 条候选，"
        f"受界面安全上限 {cap} 条限制，只列出前 {cap} 条，"
        f"其余 **{hidden}** 条未列出。确认或排除当前候选不会让它们出现——"
        "这是保护界面响应的上限，不代表互动只有这些。"
        "如需查看其余候选，请分批处理（确认 / 排除后重新进入）"
        "或缩小当前导入的聊天范围。")


# ---------------------------------------------------------------------------
# 历史快照辅助筛选（无正文：只既有指标 → 候选，上下文缺失必须标注）
# ---------------------------------------------------------------------------


def history_candidates(run: dict, *, limit: int = 5) -> list[EventCandidate]:
    """从一条历史分析快照的既有九问结果里筛选候选事件（无正文）。

    ``run`` 是 ``FriendStore.get_run()`` 的完整快照。注意：

    - 只使用既有指标做**筛选提示**（intent/distancing/romantic 的证据强度），
      绝不把分数改名成"尊重分"或"喜欢概率"；
    - 历史快照没有聊天正文，因此候选一律 ``context_missing=True``，
      ``time_confidence`` 按该消息行的 chat_time 判定；
    - 用户确认时可以补充自己的说明与脱敏片段，但**不得**凭指纹或旧总结
      重新生成正文。
    """
    out: list[EventCandidate] = []
    results = run.get("results") or []
    message_rows = {int(row["index"]): row
                    for row in (run.get("messages") or [])
                    if row.get("index") is not None}

    def _make(entry: dict, dimension: str, behavior_type: str,
              rule_id: str, note: str, alternative: str) -> None:
        index = int(entry["index"])
        row = message_rows.get(index) or {}
        time_value = row.get("chat_time")
        kind = time_kind(time_value)
        confidence = ("full" if kind == TIME_FULL
                      else "partial" if kind == TIME_ONLY else "unknown")
        # 历史候选没有正文，但**消息指纹是随快照真实保存过的**：候选身份
        # 直接用这条指纹——同一条历史消息被重复保存进多个 run 时身份一致，
        # 重叠可识别、不会重复计算。只有快照损坏（该 index 没有指纹行）
        # 才退回 run_id:index 兜底。
        row_fp = (message_rows.get(index) or {}).get("fingerprint")
        fps = [row_fp] if row_fp else [f"{run.get('run_id')}:{index}"]
        out.append(EventCandidate(
            dimension=dimension, behavior_type=behavior_type,
            start=index, end=index, fingerprints=fps,
            source_kind="history", rule=rule_id,
            event_start_time=str(time_value) if kind == TIME_FULL else None,
            event_end_time=str(time_value) if kind == TIME_FULL else None,
            time_confidence=confidence,
            stance="unspecified", evidence_note=note,
            alternative=alternative,
            support_hint="请结合你记得的上下文人工判断",
            counter_hint="请结合你记得的上下文人工判断",
            flags={"media_unknown": False, "time_uncertain": kind != TIME_FULL,
                   "context_missing": True, "cross_run_duplicate": False},
            source_run_id=run.get("run_id"),
            msg_texts=[],
        ))

    def _evidence_rank(entry: dict) -> float:
        result = entry.get("result") or {}
        value = (result.get("relationship_evidence_strength") or {}
                 ).get("score")
        return -(value or 0.0)

    ranked = sorted(
        (e for e in results if not e.get("error")),
        key=_evidence_rank)
    care_shown = refusal_shown = romantic_shown = 0
    for entry in ranked:
        result = entry.get("result") or {}
        intent = (result.get("intent") or {}).get("choice")
        romantic = result.get("romantic_signal") or 0.0
        distancing = result.get("distancing_signal") or 0.0
        if intent == "show_care" and care_shown < limit:
            _make(entry, DIMENSION_CARE, "care_response",
                  "history_intent_care",
                  "历史分析里这条消息的既有 intent=show_care"
                  "（仅辅助筛选，不是关心程度的结论）",
                  "意图标签只是模型当时的判断，可能有偏差")
            care_shown += 1
        elif distancing >= 0.70 and refusal_shown < limit:
            _make(entry, DIMENSION_RESPECT, "refusal_reaction",
                  "history_distancing_strong",
                  "历史分析里这条消息的既有疏离证据较强"
                  "（仅辅助筛选，不等于'明确拒绝'）",
                  "疏离证据也可能来自忙碌或话题转移，不是关系边界")
            refusal_shown += 1
        elif romantic >= 0.70 and romantic_shown < limit:
            _make(entry, DIMENSION_ROMANCE, "romantic_expression",
                  "history_romantic_strong",
                  "历史分析里这条消息的既有浪漫证据较强"
                  "（仅辅助筛选，不等于浪漫结论；普通关心尤其不能推断为浪漫）",
                  "浪漫标签由模型给出，未经你核对前不能当作事实")
            romantic_shown += 1
    return out


# ---------------------------------------------------------------------------
# 长期行为报告（现象 + 证据 + 限制；绝不生成数值评分）
# ---------------------------------------------------------------------------


def _fmt_time_span(event: BehaviorEvent) -> str:
    if event.event_start_time and event.event_end_time:
        if event.event_start_time == event.event_end_time:
            return event.event_start_time
        return f"{event.event_start_time} ~ {event.event_end_time}"
    if event.event_start_time:
        return event.event_start_time
    return "（时间不明）"


def _event_source_text(event: BehaviorEvent) -> str:
    if event.source_kind == "manual":
        return SOURCE_LABELS["manual"]
    if event.source_kind == "history":
        run = (event.source_run_id or "")[:8] or "未知"
        return f"{SOURCE_LABELS['history']}（run {run}…）"
    return SOURCE_LABELS["rule"]


def _event_limits(event: BehaviorEvent) -> list[str]:
    limits: list[str] = []
    if event.time_confidence != "full":
        limits.append(TIME_CONFIDENCE_LABELS.get(
            event.time_confidence, event.time_confidence))
    flags = event.flags or {}
    if flags.get("media_unknown"):
        limits.append("窗口含媒体消息（内容未知，未做推测）")
    if flags.get("context_missing") or event.source_kind == "history":
        limits.append("历史来源无聊天正文，上下文缺失")
    return limits


def _month_of(event: BehaviorEvent) -> str | None:
    value = event.event_start_time or event.event_end_time
    if not value or len(str(value)) < 7:
        return None
    return str(value)[:7]


def build_behavior_report(friend, events: list[BehaviorEvent],
                          *, pending_counts: dict[str, int] | None = None,
                          generated_at: str | None = None) -> str:
    """生成本地「长期行为观察」Markdown（0 Jev API，无任何新评分）。

    - 四个方向分别展示：已确认事件、代表性支持 / 相反证据、发生日期、
      所覆盖的聊天范围；
    - 相反或混合信号**单独列出**，绝不与支持证据抵消成一个平均分；
    - 不同时期只给事件分布表；数据量差异大、不连续或互动机会不明时
      明确**不得**据此宣称态度改善或恶化；
    - 缺失资料、重叠记录（重复导入已按事件身份去重）与其它限制单列一节；
    - 用户主观感受独立成节，明示不是对方意图的客观证据。
    """
    confirmed = [e for e in events if e.status == "confirmed"]
    rejected = [e for e in events if e.status == "rejected"]
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: list[str] = ["# 长期行为观察报告（本地）", ""]
    lines += [
        f"- 档案：{friend.display_name}",
        f"- 档案 ID：`{friend.friend_id}`（本地随机 ID，非昵称，不外发）",
        f"- 已确认行为事件：{len(confirmed)}"
        f"（另有 {len(rejected)} 条被人工排除）",
        f"- 生成时间：{stamp}",
        "",
        "> 本报告由**人工确认**的行为事件聚合而成：没有任何 TypeSafe API 调用，",
        "> 没有修改九问定义或评分公式，**不生成**任何“尊重分 / 喜欢概率 / 人格标签”。",
        "> 它只整理你逐条核对过的观察，不能当作对方真实心理状态的证明。",
        "",
    ]

    for dimension in DIMENSIONS:
        events_dim = [e for e in confirmed if e.dimension == dimension]
        lines += [f"## {DIMENSION_LABELS[dimension]}", ""]
        if not events_dim:
            lines += ["（该方向还没有已确认的事件。）", ""]
        else:
            lines += ["### 已确认的事件", ""]
            for event in sorted(events_dim, key=lambda e: (
                    e.event_start_time or "9999", e.created_at)):
                type_label = BEHAVIOR_TYPE_LABELS.get(
                    f"{event.dimension}.{event.behavior_type}",
                    event.behavior_type)
                lines.append(
                    f"- **{type_label}** · {_fmt_time_span(event)} · "
                    f"{STANCE_LABELS.get(event.stance, event.stance)}")
                if event.notes:
                    lines.append(f"  - 人工说明：{event.notes}")
                if event.snippet:
                    lines.append(f"  - 保留的脱敏片段：“{event.snippet}”")
                if event.alternative:
                    lines.append(f"  - 替代解释：{event.alternative}")
                limits = _event_limits(event)
                if limits:
                    lines.append(f"  - 限制：{'；'.join(limits)}")
                lines.append(f"  - 来源：{_event_source_text(event)}")
            lines.append("")

            counter = [e for e in events_dim
                       if e.stance in ("counter", "mixed")]
            if counter:
                lines += ["### 相反或混合信号（单独列出，"
                          "不与支持证据相互抵消）", ""]
                for event in counter:
                    type_label = BEHAVIOR_TYPE_LABELS.get(
                        f"{event.dimension}.{event.behavior_type}",
                        event.behavior_type)
                    lines.append(
                        f"- **{type_label}** · {_fmt_time_span(event)} · "
                        f"{STANCE_LABELS.get(event.stance, event.stance)}")
                    if event.counter_evidence:
                        lines.append(f"  - 相反证据说明：{event.counter_evidence}")
                    if event.notes:
                        lines.append(f"  - 人工说明：{event.notes}")
                lines.append("")

        pending = int((pending_counts or {}).get(dimension) or 0)
        if pending:
            lines += [f"（该方向还有 {pending} 条待人工核对的候选，"
                      "未经确认不计入上面的结论。）", ""]

    # ---- 分期分布（只给计数，不造趋势）----
    months: dict[str, dict[str, int]] = {}
    for event in confirmed:
        month = _month_of(event)
        if not month:
            continue
        bucket = months.setdefault(month, {d: 0 for d in DIMENSIONS})
        bucket[event.dimension] += 1
    if months:
        lines += ["## 不同时期的事件分布", ""]
        lines += ["| 月份 | " + " | ".join(DIMENSION_LABELS[d]
                                           for d in DIMENSIONS) + " |",
                  "|---|---|" + "---|" * len(DIMENSIONS)]
        for month in sorted(months):
            bucket = months[month]
            lines.append("| " + month + " | " + " | ".join(
                str(bucket[d]) for d in DIMENSIONS) + " |")
        lines += [
            "",
            "> 各时期数据量差异很大、互动机会不明或聊天记录不连续时，"
            "**不得**据此宣称对方态度改善或恶化；本表只说明"
            "“哪些月份有哪些被确认的观察”。",
            "",
        ]

    # ---- 限制与缺失 ----
    lines += ["## 限制与缺失资料", ""]
    unplaced = [e for e in confirmed if e.time_confidence != "full"]
    media_events = [e for e in confirmed
                    if (e.flags or {}).get("media_unknown")]
    context_missing = [e for e in confirmed
                       if (e.flags or {}).get("context_missing")
                       or e.source_kind == "history"]
    lines.append(f"- 时间无法完整归位的事件：{len(unplaced)} 条"
                 + ("（这些事件只描述现象，不能作为时间轴证据）"
                    if unplaced else ""))
    lines.append(f"- 窗口含媒体（内容未知）的事件：{len(media_events)} 条")
    lines.append(f"- 来自历史快照、无聊天正文的事件：{len(context_missing)} 条"
                 "（未凭指纹或旧总结重新生成任何正文）")
    if rejected:
        lines.append(f"- 人工排除的候选：{len(rejected)} 条"
                     "（排除记录同样保留，可追溯你的取舍）")
    lines += [
        "- 同一事件被重复导入时按事件身份（消息指纹集合 + 方向 + 行为类型）"
        "去重，不会重复计算；",
        "- 后来补充的更早聊天按**聊天发生日期**归位，不按软件分析日期。",
        "",
    ]

    # ---- 主观感受 ----
    feelings = [e for e in confirmed if e.user_feeling.strip()]
    if feelings:
        lines += ["## 用户主观感受（独立记录，"
                  "**不是**对方意图的客观证据）", ""]
        for event in feelings:
            type_label = BEHAVIOR_TYPE_LABELS.get(
                f"{event.dimension}.{event.behavior_type}",
                event.behavior_type)
            lines.append(f"- {type_label} · {_fmt_time_span(event)}："
                         f"{event.user_feeling}")
        lines.append("")

    lines += [
        "## 说明",
        "",
        "- 四个观察方向（尊重与边界 / 关心与回应性 / 主动性与投入 / 好感与关系性质）"
        "都只是**观察方向**，不是人格诊断，也不能换算成真实心理概率；",
        "- 正常争论不会被判为不尊重，普通关心不会被判为浪漫兴趣，"
        "回复快慢不会被当作关心程度；",
        "- 普通友好、亲密友情、特殊关注与明确浪漫表达在本报告中分别列出；",
        "- 聊天记录不连续或缺失时，本报告只呈现已有的观察，不推断未观察到的时期。",
        "",
    ]
    return "\n".join(lines)
