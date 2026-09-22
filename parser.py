"""聊天文本解析。

把从微信 / QQ 复制的纯文本解析为统一的消息列表::

    [
        {"speaker": "me"|"them"|"unknown", "text": "...", "time": "...",
         "raw_speaker": "...", "content_type": "text"|"media"|"mixed",
         "media_kinds": [...], "duration_seconds": 7（仅带时长的语音）},
    ]

``content_type="media"`` 表示纯媒体占位符消息（[图片] / [视频] / [动画表情] /
[语音] / [文件]）：不作为 Jev target、不产生 API 请求、不计入任何统计，
仅在预览与上下文中以中性 marker 出现。

支持的格式（可混合出现）：

1. 冒号格式（一行一条）::

    我: 你刚才怎么一直没回我
    TA: 可能比较沉浸

2. 时间 + 昵称同一行，内容在后续行::

    22:31 我
    你干嘛呢

3. 三行格式（微信 PC 端复制常见，消息之间通常有空行）::

    昵称A
    2026年09月08日 0:09
    消息内容

    昵称B
    2026年09月08日 0:10
    消息内容

微信三行块格式是**结构化消息块**，而不是“逐行猜昵称”：

    SENDER
    TIMESTAMP
    BODY...

一条新消息只允许在 **合法 sender candidate + 下一有效行整行 fullmatch 时间** 时
开始；否则当前文本一律继续属于上一条消息的 BODY。因此正文里出现的
``https://…``、``16:30``、英文、冒号、域名、文件名、多个空行都**不会**
单独开启新 speaker。

时间检测一律整行 fullmatch（``timestamp_of_line``）：只有独占一行的时间才算
时间行，正文中的时间子串（“行 16:30到悦城？”、“我大概16:30过去”、
“版本1:30应该能好”）永远不是时间行。

发言人识别：
- UI 自动检测本次聊天出现的参与者昵称，由用户指定“我 / TA”；
- 参与者只来自 parser 已确认处于 sender header 位置的 ``raw_speaker``
  （见 ``detect_participants``），绝不重新扫描正文寻找“像昵称的字符串”；
- 结构位置正确即可成为参与者，**不要求昵称重复出现**；
- 手工昵称优先；内置默认名（我/TA/他/她…）次之；
  只填一侧昵称时，其余具名发言人归入另一侧；
- **绝不根据消息内容猜测** speaker；
- 无法确定的单条消息标记为 "unknown"，不会导致整个文件解析失败。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 行级识别正则
# ---------------------------------------------------------------------------

_DATE_PART = r"(?:\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2})"
_TIME_PART = r"\d{1,2}:\d{2}(?::\d{2})?"

# 完整“日期 时间”独占一行
DATETIME_FULL = re.compile(rf"^\s*(?P<dp>{_DATE_PART})\s+(?P<tp>{_TIME_PART})\s*$")
# 仅时间独占一行
TIME_ONLY_LINE = re.compile(rf"^\s*(?P<tp>{_TIME_PART})\s*$")
# “日期 时间 昵称”或“时间 昵称”同一行
DATETIME_NAME = re.compile(
    rf"^\s*(?P<dp>{_DATE_PART})\s+(?P<tp>{_TIME_PART})\s+(?P<name>[^\s:：]+)\s*$"
)
TIME_NAME = re.compile(rf"^\s*(?P<tp>{_TIME_PART})\s+(?P<name>[^\s:：]+)\s*$")
# “昵称: 内容”
COLON_HEADER = re.compile(
    r"^\s*(?P<name>[^:：\s][^:：]{0,29}?)\s*[:：]\s*(?P<text>.*)$"
)

# URL 行（scheme://…）：永远不是“昵称: 内容”，也不是 sender candidate。
_URL_LINE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9+.\-]*://")
# 昵称候选里不允许出现的字符：冒号 / 括号 / 中英文句法与句末标点。
# 用途是把“像正文的文本”（URL、带标点的句子、媒体占位符）挡在 sender 之外。
_NAME_BAD_CHARS = re.compile(r"[:：\[\]{}<>，。！？；、…,;!?\"“”‘’]")
# 昵称至少包含一个中文或拉丁字母（纯数字 / 纯符号不是昵称）
_NAME_HAS_WORD = re.compile(r"[\u4e00-\u9fffA-Za-z]")

MAX_SENDER_NAME_LEN = 30

DEFAULT_ME_NAMES = {"我", "me", "Me", "ME", "自己", "本人"}
DEFAULT_THEM_NAMES = {"TA", "ta", "Ta", "tA", "他", "她", "对方"}

# ---------------------------------------------------------------------------
# 非文本媒体占位符（微信 PC 端复制文本常见）
#
# Jev 不具备图像 / 视频输入能力，复制文本里也只有占位符而没有实际媒体内容，
# 因此这些占位符**不能**当普通文本做情绪 / 意图 / 关系分析：
# - 纯媒体消息 → content_type="media"，不作为 Jev target，不产生 API 请求；
# - 处于其他消息上下文中的纯媒体消息 → 转换为中性 context marker，
#   并明确禁止 Jev 据此猜测媒体内容或情绪；
# - 普通 Unicode emoji（😂、😭、❤️）**不是**媒体占位符，原样保留。
# ---------------------------------------------------------------------------

MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_VIDEO = "video"
MEDIA_KIND_STICKER = "sticker"
MEDIA_KIND_VOICE = "voice"
MEDIA_KIND_FILE = "file"

# 中性 context marker（内容未知，禁止猜测）
MEDIA_MARKERS: dict[str, str] = {
    MEDIA_KIND_IMAGE: "[发送了一张图片，内容未知]",
    MEDIA_KIND_VIDEO: "[发送了一个视频，内容未知]",
    MEDIA_KIND_STICKER: "[发送了一个动画表情，内容未知]",
    MEDIA_KIND_VOICE: "[发送了一条语音，内容未知]",
    MEDIA_KIND_FILE: "[发送了一个文件，内容未知]",
}

MEDIA_KIND_LABELS: dict[str, str] = {
    MEDIA_KIND_IMAGE: "图片",
    MEDIA_KIND_VIDEO: "视频",
    MEDIA_KIND_STICKER: "动画表情",
    MEDIA_KIND_VOICE: "语音",
    MEDIA_KIND_FILE: "文件",
}

# 占位符 + 其后紧跟的本地媒体文件名（如 “[图片] 微信图片_20260908.dat”）。
# 文件名一律剥离：既不参与分析，也不进入报告。
_MEDIA_EXT = (
    r"(?:dat|mp4|mov|m4v|amr|silk|mp3|wav|jpg|jpeg|png|gif|webp|bmp"
    r"|doc|docx|xls|xlsx|ppt|pptx|pdf|zip|txt)"
)
# 语音时长写法：7" / 7'' / 7″ / 7” / 7秒（时长只是本地 metadata，
# 绝不据此推测内容、情绪或关系信号）。
_VOICE_DURATION = r"""\d+(?:\.\d+)?\s*(?:"|''|″|”|秒)"""
MEDIA_TOKEN = re.compile(
    rf"\[(?P<kind>动画表情|表情包|表情|图片|视频|语音|文件)\]"
    rf"(?:\s*(?P<dur>{_VOICE_DURATION}))?"
    rf"(?:\s*(?:微信(?:图片|视频|动画表情|语音|文件)\S*|\S+\.{_MEDIA_EXT}\b))?",
    re.IGNORECASE,
)

_STICKER_WORDS = {"动画表情", "表情包", "表情"}
_KIND_BY_WORD = {
    "图片": MEDIA_KIND_IMAGE,
    "视频": MEDIA_KIND_VIDEO,
    "语音": MEDIA_KIND_VOICE,
    "文件": MEDIA_KIND_FILE,
}


def _kind_of(word: str) -> str:
    if word in _STICKER_WORDS:
        return MEDIA_KIND_STICKER
    return _KIND_BY_WORD[word]


def media_label(kinds: list[str]) -> str:
    """媒体类型的展示名（如 “图片 / 视频”）。"""
    return " / ".join(MEDIA_KIND_LABELS.get(k, k) for k in kinds)


def media_marker(kind: str, duration_seconds=None) -> str:
    """中性 context marker（内容未知，禁止猜测）。

    语音知道录制时长时说明时长（“发送了一条 7 秒语音”），内容仍然未知；
    时长**不携带任何情绪 / 内容 / 关系信息**，仅描述录音长度。
    """
    if kind == MEDIA_KIND_VOICE and duration_seconds is not None:
        sec = f"{duration_seconds:g}" if isinstance(duration_seconds, float) \
            else str(duration_seconds)
        return f"[发送了一条 {sec} 秒语音，内容未知]"
    return MEDIA_MARKERS[kind]


# 所有中性 marker 共有的结尾：用于识别“任意时长/类型的媒体 marker”。
MEDIA_MARKER_TAIL = "，内容未知]"


def has_media_marker(text: str) -> bool:
    """文本中是否包含中性媒体 marker（含带时长的语音 marker）。"""
    if not text:
        return False
    return any(m in text for m in MEDIA_MARKERS.values()) \
        or MEDIA_MARKER_TAIL in text


def parse_duration_seconds(raw: str | None):
    """“7"” / “7''” / “7″” / “7秒” → 7（int）或 7.5（float）；无法解析返回 None。"""
    if not raw:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)", raw)
    if not m:
        return None
    value = float(m.group(1))
    return int(value) if value.is_integer() else value


def classify_media(text: str) -> dict:
    """识别消息中的非文本媒体占位符。

    返回 {"content_type", "text", "media_kinds"}：

    - ``media``：整条只有媒体占位符（+ 时长 / 本地文件名），没有可分析文字。
      ``text`` 为中性 marker 组合，仅供预览与上下文使用。
    - ``mixed``：文字 + 媒体占位符。保留文字，占位符替换为中性 marker，
      该条仍然可以分析。
    - ``text``：普通文本，Unicode emoji 原样保留。

    带时长的语音（``[语音] 7"`` 等）额外返回 ``duration_seconds``（本地
    metadata，绝不进入 Jev state / 缓存 key / 报告）。未出现时长时
    不返回该键，普通文本的返回形状与之前完全一致。
    """
    if not text or not text.strip():
        return {"content_type": "text", "text": text or "", "media_kinds": []}

    kinds: list[str] = []
    used_markers: list[str] = []
    duration = None

    def _replace(m: re.Match) -> str:
        nonlocal duration
        kind = _kind_of(m.group("kind"))
        if kind not in kinds:
            kinds.append(kind)
        seconds = None
        if kind == MEDIA_KIND_VOICE:
            seconds = parse_duration_seconds(m.group("dur"))
            if seconds is not None and duration is None:
                duration = seconds
        marker = media_marker(kind, seconds)
        used_markers.append(marker)
        return marker

    with_markers = MEDIA_TOKEN.sub(_replace, text)

    # 剥掉所有实际生成的 marker 后若不留任何文字 → 纯媒体消息
    residue = with_markers
    for marker in dict.fromkeys(used_markers):
        residue = residue.replace(marker, "")
    if not re.sub(r"\s+", "", residue):
        out = {
            "content_type": "media",
            "text": " ".join(used_markers),
            "media_kinds": kinds,
        }
        if duration is not None:
            out["duration_seconds"] = duration
        return out

    out = {
        "content_type": "mixed" if kinds else "text",
        "text": with_markers.strip(),
        "media_kinds": kinds,
    }
    if duration is not None:
        out["duration_seconds"] = duration
    return out


class ParseError(ValueError):
    """聊天文本完全无法解析时抛出，提示信息面向最终用户。

    注意：单条消息发言人无法识别不会抛出此错误，而是标记为 "unknown"。
    """


def resolve_speaker(
    name: str, my_name: str | None = None, them_name: str | None = None
) -> str | None:
    """把发言人名字映射为 'me' / 'them'，无法判断时返回 None（→ unknown）。

    只根据“名字”映射，绝不根据消息内容猜测。
    """
    n = name.strip()
    if not n:
        return None
    my = my_name.strip() if my_name else None
    them = them_name.strip() if them_name else None
    if my and n == my:
        return "me"
    if them and n == them:
        return "them"
    if n in DEFAULT_ME_NAMES:
        return "me"
    if n in DEFAULT_THEM_NAMES:
        return "them"
    # 只指定了一侧昵称时，其余具名发言人算另一侧
    if my and not them:
        return "them"
    if them and not my:
        return "me"
    return None


# ---------------------------------------------------------------------------
# 时间规范化
# ---------------------------------------------------------------------------


def _norm_date(dp: str) -> str:
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", dp)
    if m:
        y, mo, d = m.groups()
    else:
        y, mo, d = re.split(r"[-/]", dp)
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _norm_time(tp: str) -> str:
    parts = tp.split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def _norm_dt(m: re.Match) -> str:
    return f"{_norm_date(m.group('dp'))} {_norm_time(m.group('tp'))}"


def _header_time(m: re.Match) -> str:
    """DATETIME_FULL / TIME_ONLY_LINE / DATETIME_NAME / TIME_NAME 匹配结果 → 规范化时间。"""
    if "dp" in m.re.groupindex and m.groupdict().get("dp"):
        return _norm_dt(m)
    return _norm_time(m.group("tp"))


def timestamp_of_line(line: str) -> str | None:
    """整行 fullmatch 的时间 → 规范化时间串；不是独占一行的时间返回 None。

    允许::

        2026年08月21日 13:43    2026年08月21日  9:32
        2026-08-21 13:43        2026/8/21 13:43
        13:43                   13:43:07（规范化后丢弃秒）

    绝不允许把正文里的时间子串当时间行::

        行 16:30到大悦城？        行那就16:30吧
        我大概16:30过去          版本1:30应该能好
    """
    s = (line or "").strip()
    if not s:
        return None
    m = DATETIME_FULL.match(s)
    if m:
        return _norm_dt(m)
    m = TIME_ONLY_LINE.match(s)
    if m:
        return _norm_time(m.group("tp"))
    return None


def is_url_line(line: str) -> bool:
    """整行是 URL（scheme://…）——永远不能触发 speaker / speaker:content。"""
    return bool(_URL_LINE.match(line or ""))


def looks_like_sender_name(name: str) -> bool:
    """候选发送者昵称校验：结构上像昵称，而不像正文 / URL / 时间 / 媒体。

    只做保守判断（不硬编码任何昵称）：通过者**仍然**必须处于
    sender header 位置（下一有效行是整行时间，或“时间 昵称”同行），
    这里只负责把明显不是昵称的文本挡在参与者之外。
    """
    n = (name or "").strip()
    if not n or len(n) > MAX_SENDER_NAME_LEN:
        return False
    if is_url_line(n):
        return False
    if not _NAME_HAS_WORD.search(n):
        return False
    if _NAME_BAD_CHARS.search(n):
        return False
    if DATETIME_FULL.match(n) or TIME_ONLY_LINE.match(n):
        return False
    return True


def _colon_header(line: str) -> tuple[str, str] | None:
    """“昵称: 内容”——仅当昵称真正通过 sender candidate 校验时成立。

    与 URL 完全隔离：``https://example.com`` 不会被拆成
    ``speaker=https`` / ``content=//example.com``；同样，
    冒号属于时间子串（``行那就16:30吧``、``版本1:30应该能好``）时
    也不是发言人头。
    """
    if is_url_line(line):
        return None
    m = COLON_HEADER.match(line)
    if not m:
        return None
    name, text = m.group("name"), m.group("text")
    # 冒号是 HH:MM / 1:30 这类时间·比例子串的一部分 → 不是发言人头
    if name[-1:].isdigit() and text[:1].isdigit():
        return None
    if not looks_like_sender_name(name):
        return None
    return name, text


def _same_line_header(line: str) -> tuple[str, str] | None:
    """"日期 时间 昵称" / "时间 昵称" 同一行 → (昵称, 规范化时间)。"""
    if is_url_line(line):
        return None
    m = DATETIME_NAME.match(line) or TIME_NAME.match(line)
    if not m:
        return None
    name = m.group("name")
    if not looks_like_sender_name(name):
        return None
    return name, _header_time(m)


def _is_named_header(line: str) -> bool:
    """该行本身是否是“具名消息头”（冒号格式 / 同行时间昵称）。"""
    return _colon_header(line) is not None or _same_line_header(line) is not None


def _at_body_start(block: dict | None) -> bool:
    """是否正处于“微信消息块的第一行正文”位置（消息头刚建立、还没有正文）。

    微信块结构是 SENDER / TIMESTAMP / BODY：紧跟时间戳的那一行只可能是
    正文，绝不可能是下一条消息的发送者。这里据此把 ``16:30``、``OK: noted``
    这类“像消息头”的正文挡在 speaker 之外。
    """
    return block is not None and block["time"] is not None and not block["lines"]


def _detect_block_mode(nonblank: list[str]) -> bool:
    """是否启用微信三行块解析（普通行 + 下一有效行整行时间 = 消息头）。

    只在结构证据充分时启用，避免把“时间戳独占一行”的旧格式里的正文行
    误判成昵称。证据（满足其一即启用）：

    - 同一个候选昵称在多处“昵称 + 时间”位置重复出现；
    - 聊天以“昵称 + 时间”开头（微信复制总是这样开头）；
    - 候选昵称紧跟在另一个具名消息头之后（如冒号格式之后紧跟三行块）。
    """
    if len(nonblank) < 2:
        return False

    counts: dict[str, int] = {}
    for k in range(len(nonblank) - 1):
        line = nonblank[k]
        if timestamp_of_line(nonblank[k + 1]) is None:
            continue
        if not looks_like_sender_name(line):
            continue
        counts[line] = counts.get(line, 0) + 1
    if not counts:
        return False

    for k in range(len(nonblank) - 1):
        line = nonblank[k]
        if line not in counts:
            continue
        if counts[line] >= 2:
            return True
        if k == 0:
            return True
        if _is_named_header(nonblank[k - 1]):
            return True
    return False


def detect_participants(messages: list[dict]) -> list[str]:
    """从解析结果中按出现顺序提取参与者昵称（去重，忽略空名）。

    **只读 parser 已经确认处于 sender header 位置的 ``raw_speaker``**，
    绝不重新扫描正文寻找“像昵称的字符串”，因此正文中的 URL、域名、
    时间子串（``https`` / ``行16`` / ``16:30``）天然没有成为参与者的机会。

    不要求昵称重复出现：短聊天里只发一条消息的真实参与者也是参与者。
    """
    seen: list[str] = []
    for m in messages:
        name = (m.get("raw_speaker") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def parse_chat(
    text: str, my_name: str | None = None, them_name: str | None = None
) -> list[dict]:
    """解析聊天文本，返回统一消息列表（speaker ∈ me / them / unknown）。

    参数:
        text: 原始聊天文本。
        my_name: 手工指定的“我”的昵称（可选）。
        them_name: 手工指定的“TA”的昵称（可选）。

    抛出:
        ParseError: 完全无法识别出任何消息时（如空输入、首行即无法识别）。
    """
    if not text or not text.strip():
        raise ParseError("聊天内容为空，请先粘贴聊天文本。")

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # 空行只影响排版：预先折叠成非空行序列（同时保证解析是 O(n)）。
    nonblank = [ln.strip() for ln in lines if ln.strip()]
    if not nonblank:
        raise ParseError("聊天内容为空，请先粘贴聊天文本。")
    use_block = _detect_block_mode(nonblank)

    blocks: list[dict] = []  # {"raw": 名字|"", "time": str|None, "lines": [...]}
    current: dict | None = None

    pos = 0
    n = len(nonblank)
    while pos < n:
        line = nonblank[pos]

        # 1) 整行时间戳独占一行
        ts = timestamp_of_line(line)
        if ts is not None:
            if use_block and current is not None:
                # 微信块内部：正文里出现的 "16:30" 只是正文，不开启新 speaker
                current["lines"].append(line)
            else:
                current = {"raw": "", "time": ts, "lines": []}
                blocks.append(current)
            pos += 1
            continue

        # 微信块第一行正文：紧跟时间戳，只可能是正文
        body_start = use_block and _at_body_start(current)

        # 2) “日期 时间 昵称”或“时间 昵称”同一行 → 新块
        if not body_start:
            header = _same_line_header(line)
            if header is not None:
                current = {"raw": header[0], "time": header[1], "lines": []}
                blocks.append(current)
                pos += 1
                continue

        # 3) “昵称: 内容” → 新块（内容同行，后续行并入）
        if not body_start:
            colon = _colon_header(line)
            if colon is not None:
                current = {
                    "raw": colon[0],
                    "time": None,
                    "lines": [colon[1]],
                }
                blocks.append(current)
                pos += 1
                continue

        # 4) 微信三行块：合法 sender candidate + 下一有效行整行时间 → 新块。
        #    普通正文行（URL / 时间句子 / 多个空行之后的内容）不会命中，
        #    因此默认继续属于上一条消息的 BODY。
        if use_block and not body_start and pos + 1 < n:
            next_ts = timestamp_of_line(nonblank[pos + 1])
            if next_ts is not None and looks_like_sender_name(line):
                current = {"raw": line, "time": next_ts, "lines": []}
                blocks.append(current)
                pos += 2
                continue

        # 5) 其余 → 当前消息的内容行（支持多行消息）
        if current is None:
            raise ParseError(
                "开头第 1 行无法识别为消息（需要“名字: 内容”、“时间 名字”"
                "或“名字 + 日期时间”格式）。若格式特殊，请手工填写昵称后再试。"
            )
        current["lines"].append(line)
        pos += 1

    messages: list[dict] = []
    for b in blocks:
        content = "\n".join(b["lines"]).strip()
        if not content:
            continue
        speaker = resolve_speaker(b["raw"], my_name, them_name)
        media = classify_media(content)
        msg = {
            "speaker": speaker or "unknown",
            "text": media["text"],
            "time": b["time"],
            "raw_speaker": b["raw"],
            "content_type": media["content_type"],
            "media_kinds": media["media_kinds"],
        }
        # 语音时长只是本地 metadata：可选择性地存在，不进 Jev state / 报告默认正文
        if media.get("duration_seconds") is not None:
            msg["duration_seconds"] = media["duration_seconds"]
        messages.append(msg)

    if not messages:
        raise ParseError("未识别到任何消息，请检查格式或手工填写昵称。")
    return messages
