"""本地隐私脱敏。

任何聊天内容发往 TypeSafe Jev 之前，先在这里做本地正则脱敏。
真实姓名不做自动猜测，避免误替换。

注意：这是 MVP 级别的启发式脱敏，不能替代人工检查。
"""

from __future__ import annotations

import re

# 顺序敏感：URL 先整体替换（内含的 token 一并消失），
# 身份证先于银行卡（18 位数字否则会被银行卡规则吃掉）。
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("URL", re.compile(r"https?://[^\s\"'<>)]+")),
    ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("CARD", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("SECRET", re.compile(r"(?i)[\"']?\b(?:sk|pk|ak|api)-[A-Za-z0-9]{16,}\b[\"']?")),
    (
        "SECRET",
        re.compile(
            r"(?i)(api[_-]?key|access[_-]?token|secret|authorization|\bkey|\btoken)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{8,}[\"']?"
        ),
    ),
]


def mask_text(text: str) -> str:
    """把文本中的敏感信息替换为 <PHONE> 等占位符。"""
    for tag, pattern in _PATTERNS:
        text = pattern.sub(f"<{tag}>", text)
    return text


def mask_messages(messages: list[dict]) -> list[dict]:
    """对解析后的消息列表逐条脱敏（返回新列表，不修改原数据）。"""
    return [
        {**m, "text": mask_text(m["text"]), "raw_speaker": mask_text(m.get("raw_speaker", ""))}
        for m in messages
    ]
