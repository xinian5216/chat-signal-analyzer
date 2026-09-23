"""调用 TypeSafe Jev（System One）对 TA 的单条消息做结构化分析。

每个 TA 消息只发一次 API 请求，一次请求同时提出全部 9 个问题
（2 Choice + 5 Score + 2 Noul），与官方“fan-out / parallel questions”
模式一致。

官方文档：https://docs.typesafe.ai/
"""

from __future__ import annotations

import os
import sys
import threading
import time

from context_builder import select_context
from parser import has_media_marker
from storage import make_cache_key

# 缓存 schema 版本。v3.2：**只改 engagement 的问题描述与等级说明**——把投入度
# 从“是否同意话题/是否亲密”重新锚定为“当前消息对互动的实际参与和贡献”：
# 拒绝话题但主动追问、暂时忙碌但给出具体安排、礼貌收尾但内容具体，都不再
# 机械判为低投入；只有反复无实质回应或明确拒绝继续交流才计入低投入；
# 且 engagement 与关系疏离（distancing_signal）显式解耦。其余 8 问、scoring、
# Noul 转换、Context Builder 均不变；仍 9 问一次 system_one。bump 使旧缓存
# 自然失效（不删除缓存）。
SCHEMA_VERSION = "chat-signal-v3.2"
DEFAULT_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
API_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2  # SDK 默认即为 2，指数退避，这里显式声明

# 基础分析规则（各版本一致；媒体条款见下）。v2.2 起上下文的**选择方式**改变
# （Context Builder v2）且出站字段由白名单剥离，所有消息的 cache key 随
# 之变化（SCHEMA_VERSION 已 bump）。
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
    # v3.1：显式承认三类子情形共享本标签——**输出没有独立分类**，不要把
    # intent=distance 当作“关系疏离已判定”（那是 distancing_signal 的职责）。
    "distance": "回避、拒绝、主动拉开距离。话题拒绝、浪漫边界（不做恋人但"
                "朋友往来可继续）与关系疏离（减少或结束持续联系）都可能落在"
                "本项；本项不区分范围，范围由上下文判断",
    "other": "难以判断",
}

# 歧义条款（v3.1）：附加到 emotion / intent 的 instructions。没有语气证据
# 时（孤立的“哈哈”“行吧”），调侃与冒犯等解读都可能成立——必须选择能覆盖
# 合理解读的选项，**不得**假设不存在的表情、声音或其他媒体内容来消除歧义。
AMBIGUITY_RULE_CLAUSE = (
    " 当缺少语气证据（如孤立的“哈哈”“行吧”）、调侃与冒犯等解读都可能成立时，"
    "选择能覆盖合理解读的选项，避免无证据的极端判断；"
    "不得假设不存在的表情、声音或其他媒体内容来消除歧义。"
)

WARMTH_LEVELS: list[str] = [
    "明显冷淡、疏离或拒绝",
    "基本中性或纯事务性",
    "友好、自然",
    "明显温暖并具有个人层面的投入",
    "非常亲近、亲密或异常温暖",
]

ENGAGEMENT_LEVELS: list[str] = [
    "明显不想继续交流：明确拒绝继续对话，或反复无实质回应的敷衍",
    "最低限度、敷衍回应：无实质内容，或连续简短应付",
    "普通正常参与",
    "主动帮助对话继续：追问、开启新话题、给出具体后续安排",
    "高度主动、明显投入：持续主动贡献内容或明显维持交流",
]

ENGAGEMENT_INSTRUCTIONS = (
    "这条 TA 的消息在对话投入程度上处于哪一级？"
    "engagement 衡量 TA 当前这条消息对互动的实际参与和贡献，"
    "而不是 TA 是否同意当前话题，更不是双方关系的亲密程度。区分："
    "拒绝当前话题但主动追问、开启新话题——存在对话投入；"
    "暂时忙碌但同时给出具体后续安排——有继续互动的意愿，不机械判为低投入；"
    "礼貌结束当前交流——投入度取决于这条回复的具体内容（是否安排后续、"
    "是否带有关心内容），不能自动判成敷衍；"
    "单次简短回复——没有更多证据时，不能直接判为持续性低投入；"
    "多次缺乏实质参与的敷衍回复——可构成低投入证据；"
    "明确拒绝继续交流——低投入；是否关系疏离由 distancing_signal 单独判断，"
    "与本项无关。注意：提出后续计划不自动获得高分；只有实际体现主动贡献、"
    "具体安排或明显维持交流的行为，才支持较高投入度。"
)

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

# distancing_signal（v3.0 语义修正）：只判断**关系层**疏离，显式区分五类
# 情况，避免把会话层行为当成关系疏离：
#   1. 暂时结束话题（conversation closing）——不是疏离；
#   2. 当前疲劳 / 忙碌 / 暂时没意愿聊——不是疏离；
#   3. 对当前话题的拒绝——不是疏离；
#   4. 对浪漫关系的明确边界（只想做朋友）——不是疏离；
#   5. 对持续互动 / 双方关系的明确疏离——这才是 true
#（v2.2 真实基线的误报全部来自 1~4 被旧定义归入 true。）
DISTANCING_QUESTION = (
    "这条消息是否提供了**关系层面**的疏离信号：对方正在明确减少或结束"
    "持续的互动、回避这段关系本身，或明确拒绝继续保持联系？"
    "只在“对持续互动 / 双方关系的明确疏离”上判 true；"
    "礼貌收尾、计划稍后再聊、一次短回复、当前疲劳或忙碌、"
    "对单个话题的拒绝，以及仅划定浪漫边界（如只想做朋友）都**不是**"
    "关系疏离，除非同时带有明确的减少联系或结束关系的表述"
    "（如“别再找我了”“我们还是别联系了”）。"
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
    "本项只衡量信息量，**不衡量方向**（积极或消极）：浪漫拒绝（如表明只做"
    "朋友、已有喜欢的人）同样具有很高的关系信息量；普通话题拒绝（只是不想"
    "谈当前话题）不自动代表高关系信息量。"
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
            instructions=(
                "这条 TA 的消息主要表现出哪种情绪？参考上下文判断。"
                + AMBIGUITY_RULE_CLAUSE
            ),
            criteria=EMOTION_OPTIONS,
        ),
        "intent": Choice(
            instructions=(
                "这条 TA 的消息主要意图是什么？参考上下文判断。"
                + AMBIGUITY_RULE_CLAUSE
            ),
            criteria=INTENT_OPTIONS,
        ),
        "warmth": Score(
            instructions=(
                "这条 TA 的消息在温暖/亲近程度上处于哪一级？"
                "区分普通礼貌/友好与具体的情绪支持：礼貌、友好、正常社交通常"
                "只在中位；针对对方困境或自我披露的回应性关心才进入较高等级。"
                "关心可以很温暖，但不自动证明亲密或浪漫兴趣，"
                "也不要仅因为温暖就把分数推至最高级。"
            ),
            criteria=WARMTH_LEVELS,
        ),
        "engagement": Score(
            instructions=ENGAGEMENT_INSTRUCTIONS,
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
                "true": "存在明确的持续性疏离证据：明确拒绝继续保持联系、反复回避"
                        "互动、宣布结束关系或长期后撤（如“别再找我了”"
                        "“我们还是别联系了”）",
                "false": "只是暂时结束话题、礼貌收尾、计划稍后再聊、一次短回复、"
                         "当前疲劳或忙碌、对单个话题的拒绝，或仅划定浪漫边界"
                         "（如只想做朋友）而不拒绝普通往来",
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


def build_state(context: list[dict], target: dict) -> dict:
    """构建 Jev state：目标消息 + turn-aware 有界上下文（Context Builder v2）。

    ``context`` 是 target 之前**全部**历史消息（chronological，双方混合）。
    实际进入 state 的窗口由 ``context_builder.select_context`` 按
    “turn 回溯 + turn/message/char 预算”选择（不再机械取最近 5 条），
    详见 context_builder 模块。

    不包含任何未来消息——模拟“当时看到这句话时能判断出什么”。
    target 自身不受任何上下文预算约束，永远完整保留。

    这里也是发送到 Jev 前的最后一道字段白名单：只保留规范化角色、
    已脱敏文本与可选时间。解析器内部使用的 ``raw_speaker``（原始昵称）
    以及其它本地 metadata 一律不得进入远端请求或缓存 key。

    ``analysis_rule`` 按 state 实际内容生成：仅当上下文 / 目标中存在媒体中性
    marker 时才追加媒体条款。
    """
    state = {
        "conversation_context": [
            {
                "speaker": item.get("speaker"),
                "text": item.get("text", ""),
                "time": item.get("time"),
            }
            for item in select_context(context)
        ],
        "target_message": {
            "speaker": target.get("speaker"),
            "text": target.get("text", ""),
            "time": target.get("time"),
        },
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


# ---------------------------------------------------------------------------
# 并发与诊断配置
#
# 每条 TA 消息互相独立，但**一个 message 仍然只发一次 system_one**（9 个问题
# 一起在同一请求里），这里只并发这些互相独立的调用，不拆问题、不做 batch 语义。
# ---------------------------------------------------------------------------

JEV_CONCURRENCY_ENV = "SIGNALLENS_JEV_CONCURRENCY"
JEV_MAX_WORKERS = 4            # 默认并发（1 = 串行，便于调试）
JEV_CONCURRENCY_LIMIT = 8      # 上限：避免压垮 API / 触发 429

DEBUG_TIMING = os.environ.get("SIGNALLENS_DEBUG_TIMING", "").strip().lower() \
    not in ("", "0", "false", "no", "off")

# 最近一次 analyze_messages 的统计（只含计数与延迟，绝不含聊天内容）
LAST_RUN_STATS: dict = {}


def effective_workers(requested: int | str | None = None) -> int:
    """实际并发度：显式参数 > 环境变量 > 默认 4，夹在 1~8 之间。"""
    raw = requested if requested is not None else os.environ.get(
        JEV_CONCURRENCY_ENV, JEV_MAX_WORKERS
    )
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = JEV_MAX_WORKERS
    return max(1, min(value, JEV_CONCURRENCY_LIMIT))


def error_kind(exc: BaseException) -> str:
    """把 SDK 异常归类成计数用的短标签（429 / timeout / 5xx / ...）。

    只用于统计，不含异常文本，绝不记录请求内容。
    """
    try:
        from typesafe_sdk import (
            TypeSafeAPIError,
            TypeSafeAPIConnectionError,
            TypeSafeAPIResponseValidationError,
            TypeSafeAPITimeoutError,
            TypeSafeAuthenticationError,
            TypeSafeInternalServerError,
            TypeSafeRateLimitError,
        )
    except ImportError:  # SDK 未安装时安全降级
        return "other"

    if isinstance(exc, TypeSafeRateLimitError):
        return "rate_limit"
    if isinstance(exc, TypeSafeAPITimeoutError):
        return "timeout"
    if isinstance(exc, TypeSafeInternalServerError):
        return "server"
    if isinstance(exc, TypeSafeAuthenticationError):
        return "auth"
    if isinstance(exc, TypeSafeAPIConnectionError):
        return "connection"
    if isinstance(exc, TypeSafeAPIResponseValidationError):
        return "validation"
    if isinstance(exc, TypeSafeAPIError):
        return "api"
    return "other"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    frac = k - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _dump_run_stats(stats: dict) -> None:
    """DEBUG 时把本次运行的统计打到 stderr（只有计数与延迟，无任何内容）。"""
    if not DEBUG_TIMING:
        return
    print(
        "[jev] targets=%d cache_hits=%d api_calls=%d workers=%d "
        "api_wall=%.2fs avg=%.0fms p50=%.0fms p95=%.0fms max=%.0fms "
        "errors=%s rate_limit=%d" % (
            stats.get("targets", 0), stats.get("cache_hits", 0),
            stats.get("api_calls", 0), stats.get("workers", 1),
            stats.get("api_wall_seconds", 0.0) or 0.0,
            (stats.get("avg_latency_ms") or 0.0),
            (stats.get("p50_latency_ms") or 0.0),
            (stats.get("p95_latency_ms") or 0.0),
            (stats.get("max_latency_ms") or 0.0),
            stats.get("error_kinds", {}), stats.get("rate_limit", 0),
        ),
        file=sys.stderr, flush=True,
    )


def _is_sdk_client(obj) -> bool:
    """是否为官方 TypeSafeClient 实例（用于线程安全判断）。"""
    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError:  # SDK 未安装（测试环境）
        return False
    return isinstance(obj, TypeSafeClient)


def debug_target_view(entry: dict) -> dict:
    """测试 / 调试辅助：查看某个 target 最终构造的 target_text 与上下文。

    只在测试或显式调试时调用；**绝不自动打印、不写日志**，避免泄露私人聊天
    内容。返回纯结构，不含任何模型指标。
    """
    context = entry.get("context") or []
    return {
        "index": entry.get("index"),
        "target_text": entry.get("text"),
        "conversation_context": [c.get("text") for c in context],
        "context_speakers": [c.get("speaker") for c in context],
    }


def analyze_messages(
    client,
    messages: list[dict],
    cache=None,
    only_indices: set[int] | None = None,
    progress_cb=None,
    max_workers: int | None = None,
    client_factory=None,
) -> list[dict]:
    """分析 TA 的消息（先查缓存，再对未命中的消息做**有限并发**请求）。

    参数:
        client: TypeSafeClient（或其测试替身）。
        messages: parser 解析并脱敏后的消息列表。
        cache: 可选的 storage.Cache 实例。
        only_indices: 只分析这些消息在 messages 中的下标（用于“重新分析失败项”）。
        progress_cb: 可选回调(done, total, info=None)；只在主线程调用。
        max_workers: 并发度（默认取 ``SIGNALLENS_JEV_CONCURRENCY``，否则 4）。
        client_factory: 可选，worker 线程里创建独立 client 的工厂。
            官方 SDK 未承诺线程安全，因此并发时**不共享**同一个 client。

    返回:
        与 targets 对应的结果列表（顺序与原 TA targets 完全一致，不受并发
        完成顺序影响）；失败项带 "error" 字段，不会中断整体分析。
        每个元素: {"index", "speaker", "text", "time", "context", "result"|"error", "cached"}

    注意:
        - ``content_type == "media"`` 的纯媒体占位符消息不作为 target：
          不发起 API 请求，也不出现在结果列表中（不计入任何统计）。
        - 一个 message = 一次 system_one（9 个问题一起），并发只发生在
          “不同 message”之间，不拆分问题、不改变分析语义。
        - parser 新增的 content_type / media_kinds / duration_seconds 等本地
          元数据不进入 state，因此不会无谓改变缓存 key。
    """
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
    if total == 0:
        LAST_RUN_STATS.clear()
        LAST_RUN_STATS.update({"targets": 0, "cache_hits": 0, "api_calls": 0,
                               "workers": effective_workers(max_workers),
                               "failed": 0, "error_kinds": {}, "rate_limit": 0,
                               "api_wall_seconds": 0.0, "latencies_ms": [],
                               "avg_latency_ms": None, "p50_latency_ms": None,
                               "p95_latency_ms": None, "max_latency_ms": None})
        return []

    target_indices = {i for i, _ in targets}

    # ---- 阶段 1：为每条 target 构造 Jev state / cache key（纯本地，串行）----
    plans: list[dict] = []
    prefix: list[dict] = []
    for i, m in enumerate(messages):
        if i in target_indices:
            # 全量前缀交给 Context Builder v2 选择窗口；state / cache key 只含
            # 选中的上下文。结果条目里的 "context" 就是实际发给 Jev 的窗口，
            # 因此 UI“判断上下文”展示的与 Jev 所见完全一致。
            prefix_view = [
                {"speaker": c["speaker"], "text": c["text"], "time": c.get("time")}
                for c in prefix
            ]
            # 本地元数据绝不进入 state：build_state 内部按出站白名单剥离
            # （raw_speaker 等 parser 内部字段不属于远端请求 / 缓存 key）
            target_state = {
                "speaker": "them",
                "text": m["text"],
                "time": m.get("time"),
            }
            state = build_state(prefix_view, target_state)
            window = state["conversation_context"]
            plans.append({
                "index": i,
                "message": m,
                "state": state,
                "context": window,
                "key": None if cache is None else make_cache_key(
                    state, questions_schema, DEFAULT_MODEL, SCHEMA_VERSION
                ),
                "base": {
                    "index": i,
                    "speaker": "them",
                    "text": m["text"],
                    "time": m.get("time"),
                    "context": window,
                },
            })
        prefix.append({
            "speaker": m["speaker"], "text": m["text"], "time": m.get("time")
        })

    # ---- 阶段 2：先查本地缓存；命中的**不**进入并发 API 队列 ----
    results_by_index: dict[int, dict] = {}
    misses: list[dict] = []
    cache_hits = 0
    for plan in plans:
        cached = None
        if cache is not None and plan["key"] is not None:
            cached = cache.get(plan["key"])
        if cached is not None:
            cache_hits += 1
            results_by_index[plan["index"]] = {
                **plan["base"], "result": cached, "cached": True
            }
            if progress_cb:
                progress_cb(len(results_by_index), total,
                            {"cached": cache_hits, "misses": len(misses),
                             "done_misses": 0, "phase": "cache"})
        else:
            misses.append(plan)

    workers = effective_workers(max_workers)
    shared_client = False
    if workers > 1 and client_factory is None and _is_sdk_client(client):
        # 没有 client_factory 就无法为每个 worker 建独立 client；
        # 官方 SDK 未承诺线程安全，因此刽而串行，而不把同一个
        # transport 给多个线程共享。app.py 一直传 client_factory，所以不会走这条降级路径。
        workers = 1
        shared_client = True
    latencies: list[float] = []
    error_kinds: dict[str, int] = {}
    api_calls = 0
    rate_limited = 0
    api_start = time.perf_counter()

    # ---- worker-local client：SDK 未承诺线程安全，不跨线程共享同一个实例 ----
    factory = client_factory or (lambda: client)
    local = threading.local()
    created_clients: list = []
    clients_lock = threading.Lock()

    def _worker_client():
        instance = getattr(local, "client", None)
        if instance is None:
            instance = factory()
            local.client = instance
            with clients_lock:
                created_clients.append(instance)
        return instance

    def _call(plan: dict):
        started = time.perf_counter()
        try:
            response = _worker_client().system_one(
                state=plan["state"], questions=questions
            )
        finally:
            plan["latency"] = (time.perf_counter() - started) * 1000
        return extract_answers(response)

    def _record_error(plan: dict, exc: BaseException) -> None:
        kind = error_kind(exc)
        error_kinds[kind] = error_kinds.get(kind, 0) + 1
        results_by_index[plan["index"]] = {
            **plan["base"], "error": classify_error(exc), "cached": False
        }

    def _record_success(plan: dict, extracted: dict) -> None:
        if cache is not None and plan["key"] is not None:
            cache.set(plan["key"], extracted)
        results_by_index[plan["index"]] = {
            **plan["base"], "result": extracted, "cached": False
        }

    try:
        if workers <= 1 or len(misses) <= 1:
            # 串行路径（与历史行为完全一致，便于调试 / 单条消息时复用）
            for plan in misses:
                try:
                    extracted = _call(plan)
                except Exception as exc:  # 单条失败不影响其余消息
                    _record_error(plan, exc)
                else:
                    _record_success(plan, extracted)
                finally:
                    latencies.append(plan.get("latency") or 0.0)
                    api_calls += 1
                    if progress_cb:
                        progress_cb(
                            len(results_by_index), total,
                            {"cached": cache_hits, "misses": len(misses),
                             "done_misses": api_calls, "phase": "api",
                             "workers": 1},
                        )
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_call, plan): plan for plan in misses}
                for future in as_completed(futures):
                    plan = futures[future]
                    api_calls += 1
                    try:
                        extracted = future.result()
                    except Exception as exc:  # 单条失败不影响其余消息
                        _record_error(plan, exc)
                    else:
                        _record_success(plan, extracted)
                    finally:
                        latencies.append(plan.get("latency") or 0.0)
                        # 进度回调只在主线程调用（Streamlit widget 非线程安全）
                        if progress_cb:
                            progress_cb(
                                len(results_by_index), total,
                                {"cached": cache_hits, "misses": len(misses),
                                 "done_misses": api_calls, "phase": "api",
                                 "workers": workers},
                            )
    finally:
        api_wall = time.perf_counter() - api_start
        for instance in created_clients:
            if instance is client:
                continue
            close = getattr(instance, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    # ---- 阶段 4：按原始 target 顺序组装（并发完成顺序不影响结果）----
    results = [results_by_index[plan["index"]] for plan in plans]

    failed = sum(1 for e in results if e.get("error"))
    rate_limited = error_kinds.get("rate_limit", 0)
    LAST_RUN_STATS.clear()
    LAST_RUN_STATS.update({
        "targets": total,
        "cache_hits": cache_hits,
        "api_calls": api_calls,
        "workers": workers if (len(misses) > 1 and workers > 1) else 1,
        "shared_client": shared_client,
        "failed": failed,
        "error_kinds": error_kinds,
        "rate_limit": rate_limited,
        "api_wall_seconds": api_wall,
        "latencies_ms": latencies,
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "max_latency_ms": max(latencies) if latencies else None,
    })
    _dump_run_stats(LAST_RUN_STATS)
    return results
