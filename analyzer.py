"""调用 TypeSafe Jev（System One）对 TA 的单条消息做结构化分析。

每个 TA 消息只发一次 API 请求，一次请求同时提出全部 9 个问题
（2 Choice + 5 Score + 2 Noul），与官方“fan-out / parallel questions”
模式一致。

官方文档：https://docs.typesafe.ai/
"""

from __future__ import annotations

import os

from parser import has_media_marker
from storage import make_cache_key

SCHEMA_VERSION = "chat-signal-v2.1"  # 问题 schema 变更时必须递增，使旧缓存自然失效
DEFAULT_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
API_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2  # SDK 默认即为 2，指数退避，这里显式声明

# 基础分析规则（与 v2.1 及更早版本一致：纯文本消息的 state 因此保持
# 与历史缓存条目完全相同的 cache key）。
ANALYSIS_RULE = (
    "Judge only observable signals in the conversation. "
    "Do not assume romantic interest from politeness or normal friendliness alone. "
    "Base every answer only on the target message and the conversation context "
    "that precedes it, as if you just saw this message at that moment in the chat."
)

# 媒体补充规则：**仅当 state 中实际存在媒体中性 marker 时**才追加
# （见 build_state → analysis_rule_for）。这样：
# - 不含媒体的纯文本消息：rule 与旧版一致 → cache key 不变，旧缓存可继续命中；
# - 含媒体 marker 的消息：rule 自然不同 → cache key 自然不同。
MEDIA_RULE_CLAUSE = (
    " Bracketed neutral markers such as [发送了一张图片，内容未知] mean the actual "
    "media content is unknown and was not provided; never guess what the media "
    "shows or what emotion it carries."
)


def analysis_rule_for(state: dict) -> str:
    """按 state 实际内容返回分析规则（媒体 marker 存在时才追加媒体条款）。"""
    texts = [state.get("target_message", {}).get("text", "")]
    texts += [c.get("text", "") for c in state.get("conversation_context", [])]
    if any(has_media_marker(t) for t in texts):
        return ANALYSIS_RULE + MEDIA_RULE_CLAUSE
    return ANALYSIS_RULE

# ---------------------------------------------------------------------------
# 问题定义（同时是缓存 key 的一部分，改动后请提升 SCHEMA_VERSION）
# ---------------------------------------------------------------------------

EMOTION_OPTIONS: dict[str, str] = {
    "calm": "平静、中性，没有明显强烈情绪",
    "happy": "高兴、开心、积极",
    "teasing": "调侃、玩笑、逗趣",
    "curious": "好奇、想进一步了解",
    "confused": "疑惑、不理解",
    "surprised": "惊讶、意外",
    "caring": "关心、体贴、担忧对方",
    "annoyed": "不满、烦躁、生气",
    "awkward": "尴尬、不自然",
    "sad": "低落、难过",
    "other": "无法明确归入以上类别",
}

EMOTION_LABELS: dict[str, str] = {
    "calm": "平静",
    "happy": "高兴",
    "teasing": "调侃",
    "curious": "好奇",
    "confused": "疑惑",
    "surprised": "惊讶",
    "caring": "关心",
    "annoyed": "不满",
    "awkward": "尴尬",
    "sad": "低落",
    "other": "其他",
}

INTENT_OPTIONS: dict[str, str] = {
    "ask_information": "询问信息",
    "confirm_understanding": "确认理解",
    "explain": "解释说明",
    "share_opinion": "表达观点或感受",
    "continue_topic": "主动延续话题",
    "show_care": "主动关心",
    "tease": "调侃或开玩笑",
    "invite": "邀约、一起做某事、见面等",
    "share_personal": "主动分享自己的生活或个人信息",
    "end_topic": "结束当前话题",
    "perfunctory": "礼貌但投入较低的回复",
    "distance": "回避、拒绝、主动拉开距离",
    "other": "难以判断",
}

WARMTH_LEVELS: list[str] = [
    "明显冷淡、疏离或拒绝",
    "基本中性或纯事务性",
    "友好、自然",
    "明显温暖并具有个人层面的投入",
    "非常亲近、亲密或异常温暖",
]

ENGAGEMENT_LEVELS: list[str] = [
    "明显不想继续交流",
    "最低限度、敷衍回应",
    "普通正常参与",
    "主动帮助对话继续",
    "高度主动、明显投入",
]

SPECIAL_ATTENTION_LEVELS: list[str] = [
    "没有特别关注，甚至略显疏离",
    "仅普通礼貌",
    "友好的个人关注",
    "明显超出普通社交的特别关注",
    "非常明显且高度个人化的特殊关注",
]

ROMANTIC_QUESTION = (
    "结合当前消息和前文，这条消息是否提供了超出普通友好或礼貌范围的"
    "具体暧昧、调情或浪漫兴趣信号？"
    "普通礼貌、正常朋友关心、正常聊天不能单独算作浪漫信号。"
)

DISTANCING_QUESTION = (
    "这条消息是否提供了对方正在结束交流、回避互动、降低投入"
    "或刻意拉开距离的信号？"
)

# relationship_evidence_strength：
# 评价“关系层面的信息量”，不是关系好坏本身。
# 高信息量 ≠ 亲近：明显疏离、明确拒绝同样属于高信息量。
RELATIONSHIP_EVIDENCE_LEVELS: list[str] = [
    "几乎没有关系判断价值。例如纯确认、简单应答、功能性回复",
    "只有很弱的关系信息，主要仍是普通对话内容",
    "包含一定关系信息，可以辅助判断互动方式",
    "包含明显的关系层面信号",
    "包含非常强的关系层面证据，例如明显特殊关注、亲密表达、暧昧、明确拒绝或明显疏离",
]

RELATIONSHIP_EVIDENCE_INSTRUCTIONS = (
    "这条消息包含多少可以用于判断双方关系亲近程度、个人关注、互动投入、"
    "暧昧或疏离程度的信息？这里评价的是“关系层面的信息量”，"
    "不是关系好坏本身。纯确认词、普通功能性回复、无关事实陈述通常信息量低；"
    "明显关心、特殊关注、主动邀约、关系表达、暧昧、拒绝、回避等消息信息量高。"
)

# relational_ease（v2.1 新增，仅解释层，不计入总分）：
# 衡量互动的自然 / 熟悉 / 轻松 / 默契程度。
# 不等于浪漫兴趣、特殊关注或暧昧——“哈哈你又来了”可能 ease 高但 romantic 低。
RELATIONAL_EASE_LEVELS: list[str] = [
    "明显陌生、拘谨、纯事务性或互动不自然",
    "较正式或普通礼貌，熟悉感较弱",
    "自然、正常、舒适的熟人互动",
    "明显熟悉、轻松、有默契或自然接话",
    "高度熟悉、非常自然、明显存在长期互动形成的舒适感或默契",
]

RELATIONAL_EASE_INSTRUCTIONS = (
    "这条消息在多大程度上体现双方互动中的自然、熟悉、无需过度客套、"
    "能够轻松接话或共享默认背景的关系舒适度？"
    "评价的是互动是否自然熟悉，不是浪漫兴趣，也不是特殊关注。"
    "普通朋友之间自然调侃、无需解释太多就能接话、轻松分享日常、自然接梗，"
    "都可以体现较高 relational_ease；"
    "正式、拘谨、纯事务性、明显陌生或尴尬互动通常较低。"
)


def build_questions() -> dict:
    """构建 SDK 问题对象（依赖 typesafe_sdk）。"""
    from typesafe_sdk import Choice, Noul, Score

    return {
        "emotion": Choice(
            instructions="这条 TA 的消息主要表现出哪种情绪？参考上下文判断。",
            criteria=EMOTION_OPTIONS,
        ),
        "intent": Choice(
            instructions="这条 TA 的消息主要意图是什么？参考上下文判断。",
            criteria=INTENT_OPTIONS,
        ),
        "warmth": Score(
            instructions="这条 TA 的消息在温暖/亲近程度上处于哪一级？",
            criteria=WARMTH_LEVELS,
        ),
        "engagement": Score(
            instructions="这条 TA 的消息在对话投入程度上处于哪一级？",
            criteria=ENGAGEMENT_LEVELS,
        ),
        "special_attention": Score(
            instructions=(
                "这条 TA 的消息对“我”表现出的特别关注程度处于哪一级？"
                "（相对普通社交而言）"
            ),
            criteria=SPECIAL_ATTENTION_LEVELS,
        ),
        "relationship_evidence_strength": Score(
            instructions=RELATIONSHIP_EVIDENCE_INSTRUCTIONS,
            criteria=RELATIONSHIP_EVIDENCE_LEVELS,
        ),
        "relational_ease": Score(
            instructions=RELATIONAL_EASE_INSTRUCTIONS,
            criteria=RELATIONAL_EASE_LEVELS,
        ),
        "romantic_signal": Noul(
            instructions=ROMANTIC_QUESTION,
            criteria={
                "true": "存在超出普通友好的暧昧、调情或浪漫兴趣信号",
                "false": "只有普通礼貌、正常朋友关心或正常聊天内容",
            },
        ),
        "distancing_signal": Noul(
            instructions=DISTANCING_QUESTION,
            criteria={
                "true": "存在结束交流、回避、降低投入或拉开距离的信号",
                "false": "没有明显的疏离或结束对话信号",
            },
        ),
    }


def build_questions_schema() -> dict:
    """问题的纯 JSON 镜像，用于缓存 key（不依赖 SDK 对象序列化行为）。"""
    return {
        "emotion": {"type": "choice", "criteria": EMOTION_OPTIONS},
        "intent": {"type": "choice", "criteria": INTENT_OPTIONS},
        "warmth": {"type": "score", "criteria": WARMTH_LEVELS},
        "engagement": {"type": "score", "criteria": ENGAGEMENT_LEVELS},
        "special_attention": {"type": "score", "criteria": SPECIAL_ATTENTION_LEVELS},
        "relationship_evidence_strength": {
            "type": "score",
            "criteria": RELATIONSHIP_EVIDENCE_LEVELS,
        },
        "relational_ease": {
            "type": "score",
            "criteria": RELATIONAL_EASE_LEVELS,
        },
        "romantic_signal": {"type": "noul"},
        "distancing_signal": {"type": "noul"},
    }


def build_state(
    context: list[dict], target: dict, max_context: int = 5
) -> dict:
    """构建 Jev state：目标消息 + 之前的最多 max_context 条上下文。

    不包含任何未来消息——模拟“当时看到这句话时能判断出什么”。

    ``analysis_rule`` 按 state 实际内容生成：仅当上下文 / 目标中存在媒体中性
    marker 时才追加媒体条款，因此纯文本消息的 state（及缓存 key）与
    引入媒体过滤之前完全一致。
    """
    state = {
        "conversation_context": context[-max_context:],
        "target_message": target,
    }
    state["analysis_rule"] = analysis_rule_for(state)
    return state


# ---------------------------------------------------------------------------
# 响应解析（只取字段，不打印任何请求头）
# ---------------------------------------------------------------------------


def extract_answers(response) -> dict:
    """把 SDK 响应转成可 JSON 序列化的纯 dict。"""
    answers = response.answers

    def choice_of(name: str) -> dict:
        a = answers[name]
        return {
            "choice": a.choice,
            "probabilities": dict(a.probabilities),
            "confidence": a.confidence,
        }

    def score_of(name: str) -> dict:
        a = answers[name]
        return {
            "score": a.score,
            "probabilities": {str(k): v for k, v in dict(a.probabilities).items()},
            "confidence": a.confidence,
        }

    return {
        "emotion": choice_of("emotion"),
        "intent": choice_of("intent"),
        "warmth": score_of("warmth"),
        "engagement": score_of("engagement"),
        "special_attention": score_of("special_attention"),
        "relationship_evidence_strength": score_of("relationship_evidence_strength"),
        "relational_ease": score_of("relational_ease"),
        "romantic_signal": answers["romantic_signal"].noul,
        "distancing_signal": answers["distancing_signal"].noul,
        "model": getattr(response, "model", None),
    }


# ---------------------------------------------------------------------------
# 客户端与错误处理
# ---------------------------------------------------------------------------


def create_client(api_key: str | None = None):
    """创建 TypeSafeClient（显式 max_retries=2，指数退避，与需求一致）。"""
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    return TypeSafeClient(
        api_key=api_key,
        model=DEFAULT_MODEL,
        timeout=API_TIMEOUT_SECONDS,
        retry=RetryPolicy(max_retries=MAX_RETRIES),
    )


def classify_error(exc: Exception) -> str:
    """把 SDK 异常转成面向用户的短消息（绝不包含 API Key / 请求头）。"""
    try:
        from typesafe_sdk import (
            TypeSafeAPIConnectionError,
            TypeSafeAPIResponseValidationError,
            TypeSafeAPITimeoutError,
            TypeSafeAuthenticationError,
            TypeSafeError,
            TypeSafeInternalServerError,
            TypeSafeRateLimitError,
        )
    except ImportError:  # 理论上不会发生：调用前已确认 SDK 可用
        return f"分析失败：{type(exc).__name__}"

    if isinstance(exc, TypeSafeAuthenticationError):
        return "API Key 无效或未设置（HTTP 401），请检查 .env 中的 TYPESAFE_API_KEY。"
    if isinstance(exc, TypeSafeRateLimitError):
        return "触发 API 速率限制（HTTP 429），请稍候再重新分析失败项。"
    if isinstance(exc, TypeSafeInternalServerError):
        return "TypeSafe 服务器暂时异常（5xx），请稍候再重新分析失败项。"
    if isinstance(exc, TypeSafeAPITimeoutError):
        return "请求 TypeSafe API 超时，请检查网络后重新分析失败项。"
    if isinstance(exc, TypeSafeAPIConnectionError):
        return "无法连接 TypeSafe API（网络中断或代理问题）。"
    if isinstance(exc, TypeSafeAPIResponseValidationError):
        return "API 返回了无法解析的响应（malformed response）。"
    if isinstance(exc, TypeSafeError):
        status = getattr(exc, "status", None)
        return f"TypeSafe API 错误（HTTP {status}）。" if status else f"TypeSafe 错误：{exc}"
    return f"未预期的错误：{type(exc).__name__}"


def analyze_messages(
    client,
    messages: list[dict],
    cache=None,
    only_indices: set[int] | None = None,
    progress_cb=None,
) -> list[dict]:
    """逐条分析 TA 的消息。

    参数:
        client: TypeSafeClient（或其测试替身）。
        messages: parser 解析并脱敏后的消息列表。
        cache: 可选的 storage.Cache 实例。
        only_indices: 只分析这些消息在 messages 中的下标（用于“重新分析失败项”）。
        progress_cb: 可选回调(done, total)。

    返回:
        与 messages 等长的结果列表；失败项带 "error" 字段，不会中断整体分析。
        每个元素: {"index", "speaker", "text", "time", "context", "result"|"error", "cached"}

    注意:
        ``content_type == "media"`` 的纯媒体占位符消息不作为 target：
        不发起 API 请求，也不出现在结果列表中（不计入任何统计）。
    """
    results: list[dict] = []
    questions = build_questions()
    questions_schema = build_questions_schema()

    targets = [
        (i, m)
        for i, m in enumerate(messages)
        if m["speaker"] == "them"
        and m["text"].strip()
        and m.get("content_type") != "media"
    ]
    if only_indices is not None:
        targets = [(i, m) for i, m in targets if i in only_indices]

    total = len(targets)
    for done, (i, m) in enumerate(targets, start=1):
        context = [
            {"speaker": c["speaker"], "text": c["text"], "time": c.get("time")}
            for c in messages[:i]
        ]
        # 只把 Jev 需要的字段放进 state：parser 新增的 content_type /
        # media_kinds 等本地元数据不进入 state，因此不会无谓改变缓存 key。
        target_state = {
            "speaker": "them",
            "text": m["text"],
            "time": m.get("time"),
            "raw_speaker": m.get("raw_speaker"),
        }
        state = build_state(context, target_state)

        base = {
            "index": i,
            "speaker": "them",
            "text": m["text"],
            "time": m.get("time"),
            "context": context,
        }

        key = None
        if cache is not None:
            key = make_cache_key(
                state, questions_schema, DEFAULT_MODEL, SCHEMA_VERSION
            )
            cached = cache.get(key)
            if cached is not None:
                results.append({**base, "result": cached, "cached": True})
                if progress_cb:
                    progress_cb(done, total)
                continue

        try:
            response = client.system_one(state=state, questions=questions)
            extracted = extract_answers(response)
        except Exception as exc:  # 单条失败不影响其余消息
            results.append({**base, "error": classify_error(exc), "cached": False})
        else:
            if cache is not None and key is not None:
                cache.set(key, extracted)
            results.append({**base, "result": extracted, "cached": False})

        if progress_cb:
            progress_cb(done, total)

    return results
