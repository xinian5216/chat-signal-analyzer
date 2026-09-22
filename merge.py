"""多片段聊天的本地合并与去重（微信一次复制条数有限的补偿）。

微信一次复制能带出的消息数量有限，用户需要分几次复制。本模块把多次
复制得到的“聊天片段”在**本机**合并成一个消息列表：

- 合并、fingerprint、去重全部本地完成，**0 次 Jev API 请求**；
- ``fingerprint`` / 片段信息只是本地 metadata：绝不进入 Jev state、
  缓存 key 或报告默认正文，因此不会让已有分析缓存无谓失效；
- 去重保守：宁可保留重复，也不要误删两条真实相同的回复（“嗯” “嗯”）。

去重规则：

1. **有时间戳的消息**：``normalized raw_speaker + normalized time +
   normalized text + 媒体类型`` 的 SHA256 完全一致 → 视为同一条消息，
   直接丢弃（片段 A 1~100、片段 B 90~190 只会得到 189 条）。
2. **没有时间戳的消息**：不做跨片段去重；只有发送者、文本、相邻位置
   （紧邻上一条已接受消息）全部一致时才当作相邻重复丢弃。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


def _norm(value) -> str:
    """规范化：去掉首尾空白，内部连续空白折叠为单个空格。"""
    return re.sub(r"\s+", " ", str(value if value is not None else "").strip())


def message_fingerprint(message: dict) -> str:
    """为一条已解析消息生成本地 fingerprint（SHA256 hex）。

    组成：规范化 raw_speaker + 规范化 time + 规范化 text + 媒体类型标记。
    纯本地用途：跨片段识别“同一条消息”，与 Jev / 缓存 / 报告无关。
    """
    parts = [
        _norm(message.get("raw_speaker")),
        _norm(message.get("time")),
        _norm(message.get("text")),
        _norm("+".join(message.get("media_kinds") or [])),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


@dataclass
class MergeResult:
    """一次片段追加的结果（统计数字全部来自本地处理）。"""

    messages: list[dict] = field(default_factory=list)  # 合并后的完整列表
    chunk_size: int = 0          # 本次片段解析出的消息数
    duplicates: int = 0          # 判定为重复而丢弃的数量
    added: int = 0               # 实际新增数量
    total: int = 0               # 追加后当前总消息数
    adjacent_duplicates: int = 0  # 其中“无时间戳、仅相邻重复”的数量


def _is_adjacent_duplicate(previous: dict | None, message: dict) -> bool:
    """无时间戳消息的保守去重：仅当紧邻、同发送者、同文本时才判重。"""
    if previous is None:
        return False
    if previous.get("time") or message.get("time"):
        return False
    if _norm(previous.get("raw_speaker")) != _norm(message.get("raw_speaker")):
        return False
    return _norm(previous.get("text")) == _norm(message.get("text"))


def merge_messages(existing: list[dict], incoming: list[dict]) -> MergeResult:
    """把新片段 ``incoming`` 合并进 ``existing``，返回新列表与统计。

    ``existing`` / ``incoming`` 都是 parser 解析（并已脱敏）后的消息列表。
    纯本地函数：不调用任何 API，不修改传入列表。
    """
    merged: list[dict] = list(existing)
    known = {message_fingerprint(m) for m in existing}
    duplicates = 0
    adjacent = 0
    added = 0

    for message in incoming:
        if message.get("time"):
            fingerprint = message_fingerprint(message)
            if fingerprint in known:
                duplicates += 1
                continue
            known.add(fingerprint)
        elif _is_adjacent_duplicate(merged[-1] if merged else None, message):
            duplicates += 1
            adjacent += 1
            continue
        merged.append(message)
        added += 1

    return MergeResult(
        messages=merged,
        chunk_size=len(incoming),
        duplicates=duplicates,
        added=added,
        total=len(merged),
        adjacent_duplicates=adjacent,
    )
