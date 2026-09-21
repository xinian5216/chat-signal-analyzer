"""聊天文本解析。

把从微信 / QQ 复制的纯文本解析为统一的消息列表::

    [
        {"speaker": "me"|"them"|"unknown", "text": "...", "time": "...", "raw_speaker": "..."},
    ]

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

   昵称可为任意中文 / 英文 / 数字组合，**不硬编码任何昵称**。
   规则：时间行之前的普通文本行视为发送者候选（消息内容行与
   “时间行”直接相邻的情况在本格式中不存在，因为每条消息都有昵称行）。

发言人识别：
- UI 自动检测本次聊天出现的参与者昵称，由用户指定“我 / TA”；
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

DEFAULT_ME_NAMES = {"我", "me", "Me", "ME", "自己", "本人"}
DEFAULT_THEM_NAMES = {"TA", "ta", "Ta", "tA", "他", "她", "对方"}


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


def _next_nonblank(lines: list[str], i: int) -> int | None:
    for j in range(i + 1, len(lines)):
        if lines[j].strip():
            return j
    return None


def detect_participants(messages: list[dict]) -> list[str]:
    """从解析结果中按出现顺序提取出现过的发言人名字（去重，忽略空名）。"""
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

    blocks: list[dict] = []  # {"raw": 名字|"", "time": str|None, "lines": [...]}
    current: dict | None = None

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 1) 完整“日期 时间”独占一行 → 新块（缺少昵称 → speaker 待定）
        m = DATETIME_FULL.match(line)
        if m:
            current = {"raw": "", "time": _norm_dt(m), "lines": []}
            blocks.append(current)
            i += 1
            continue

        # 2) “日期 时间 昵称”或“时间 昵称”同一行 → 新块
        m = DATETIME_NAME.match(line) or TIME_NAME.match(line)
        if m:
            current = {"raw": m.group("name"), "time": _header_time(m), "lines": []}
            blocks.append(current)
            i += 1
            continue

        # 3) “昵称: 内容” → 新块（内容同行，后续行并入）
        m = COLON_HEADER.match(line)
        if m:
            current = {
                "raw": m.group("name"),
                "time": None,
                "lines": [m.group("text")],
            }
            blocks.append(current)
            i += 1
            continue

        # 4) 昵称单独一行 + 下一非空行是日期时间 → 三行格式新块
        #    昵称可为任意中文/英文/数字组合，不硬编码、不猜内容。
        j = _next_nonblank(lines, i)
        if j is not None:
            dm = DATETIME_FULL.match(lines[j].strip()) or TIME_ONLY_LINE.match(
                lines[j].strip()
            )
            if dm:
                current = {"raw": line, "time": _header_time(dm), "lines": []}
                blocks.append(current)
                i = j + 1
                continue

        # 5) 其余 → 当前消息的内容行（支持多行消息）
        if current is None:
            raise ParseError(
                "开头第 1 行无法识别为消息（需要“名字: 内容”、“时间 名字”"
                "或“名字 + 日期时间”格式）。若格式特殊，请手工填写昵称后再试。"
            )
        current["lines"].append(line)
        i += 1

    messages: list[dict] = []
    for b in blocks:
        content = "\n".join(b["lines"]).strip()
        if not content:
            continue
        speaker = resolve_speaker(b["raw"], my_name, them_name)
        messages.append(
            {
                "speaker": speaker or "unknown",
                "text": content,
                "time": b["time"],
                "raw_speaker": b["raw"],
            }
        )

    if not messages:
        raise ParseError("未识别到任何消息，请检查格式或手工填写昵称。")
    return messages
