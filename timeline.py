"""消息时间线排序（本地、纯函数、0 Jev API）。

真实使用场景：用户从**最新**聊天开始复制，再逐批追加**更早**的聊天；
``merge.merge_messages`` 只做去重和追加，不改变顺序，因此合并后的列表
是按“复制顺序”而不是时间顺序排列的——Context Builder 会因此把更晚的
消息当成更早消息的上下文（未来泄漏）。

本模块在合并去重之后、Context Builder 使用消息列表之前，把消息排成
可靠的时间线：

1. **完整时间**（``YYYY-MM-DD HH:MM``）按时间**升序**排列（最早在前）；
   支持跨天 / 跨月 / 跨年，支持反向追加的片段；
2. 同一时间的多条消息：**稳定排序**保留已知的原始相对顺序；不同片段
   精确时间相同且相对顺序无法确定的，记为 ``ambiguous_exact_tie``——
   不虚构先后；
3. **仅时分（HH:mm）/ 无时间**的消息无法放入完整时间线：单独归组，
   保留各自可确定的原始相对顺序，绝不猜测日期后强行插入；涉及无法
   确认的前后关系时由 UI 要求用户确认或排除（``requires_order_confirm``）；
4. `migrate_bindings` 用 fingerprint 迁移媒体绑定：能唯一对应的才迁移，
   无法唯一确定的绑定直接失效（要求用户重新确认），避免排序后图片错配。

fingerprint 与 merge 模块一致（raw_speaker + time + text + 媒体类型），
纯本地 metadata，绝不进入 Jev state / 缓存 key。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

TIME_FULL = "full"        # YYYY-MM-DD HH:MM
TIME_ONLY = "time_only"   # HH:MM（缺日期）
TIME_MISSING = "missing"  # 完全没有时间

_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$")
_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_real_datetime(year: int, month: int, day: int,
                      hour: int, minute: int) -> bool:
    """真实日历校验：拒绝不存在的日期、非法月份、非法小时与分钟。

    含闰年（四年一闰、百年不闰、四百年再闰）与月底天数。
    """
    if not 1 <= month <= 12:
        return False
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return False
    dim = _DAYS_IN_MONTH[month - 1]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        dim = 29
    return 1 <= day <= dim


def _full_key(time_value: str):
    """完整时间 → 可排序 key；正则不匹配或不是真实时刻则返回 None。"""
    m = _FULL_RE.match(str(time_value).strip())
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    if not _is_real_datetime(y, mo, d, h, mi):
        return None
    return (y, mo, d, h, mi)


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "").strip())


def message_fingerprint(message: dict) -> str:
    """与 merge.message_fingerprint 一致的本地 fingerprint（用于绑定迁移）。"""
    parts = [
        _norm(message.get("raw_speaker")),
        _norm(message.get("time")),
        _norm(message.get("text")),
        _norm("+".join(message.get("media_kinds") or [])),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def time_kind(time_value) -> str:
    """时间字段分类：full / time_only / missing。

    ``full`` 需要**真实存在**的日期时间（经 _is_real_datetime 校验）：
    2026-02-30、2026-13-01、2026-09-23 25:61 这类会被判为 missing，
    绝不进入确定的时间线。``time_only`` 同样需要是合法时刻
    （25:00 / 12:61 判 missing）。
    """
    if not time_value or not str(time_value).strip():
        return TIME_MISSING
    s = str(time_value).strip()
    if _full_key(s) is not None:
        return TIME_FULL
    m = _ONLY_RE.match(s)
    if m and _is_real_datetime(2000, 1, 1, int(m.group(1)),
                               int(m.group(2))):
        return TIME_ONLY
    return TIME_MISSING          # 非法日期/时刻或无其它格式支持 → 不可解析


def _full_key(time_value: str):
    """完整时间 → 可排序 key；正则不匹配或不是真实时刻则返回 None。"""
    m = _FULL_RE.match(str(time_value).strip())
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    if not _is_real_datetime(y, mo, d, h, mi):
        return None
    return (y, mo, d, h, mi)


@dataclass
class TimelineResult:
    """排序结果 + 时间校验摘要（摘要只含计数，不含任何聊天文本）。"""

    messages: list[dict] = field(default_factory=list)
    full: int = 0
    time_only: int = 0
    missing: int = 0
    ambiguous_exact_tie: int = 0   # 跨片段精确同刻、相对顺序无法确定的对数
    order_changed: bool = False
    uncertain_order: bool = False   # 是否存在无法确认先后的消息

    @property
    def anchored(self) -> int:
        return self.full

    @property
    def unanchored(self) -> int:
        return self.time_only + self.missing

    @property
    def requires_order_confirm(self) -> bool:
        """分析前是否必须要求用户确认（仅当真无法确认顺序时为 True）。"""
        return self.uncertain_order

    def summary_text(self) -> str:
        parts = [f"完整时间 {self.full}"]
        if self.time_only:
            parts.append(f"仅时分 {self.time_only}")
        if self.missing:
            parts.append(f"无时间 {self.missing}")
        if self.ambiguous_exact_tie:
            parts.append(f"同刻顺序待确认 {self.ambiguous_exact_tie}")
        return " · ".join(parts)


def sort_messages(messages: list[dict],
                  multi_chunk: bool = False) -> TimelineResult:
    """把合并去重后的消息排成可靠时间线（纯函数，不修改传入列表）。

    返回 ``TimelineResult``：完整时间升序在前；仅时分 / 无时间的消息按
    原相对顺序附后（插到完整时间线之前会凭空制造先后关系，因此绝不那么做）。

    ``multi_chunk=True`` 表示消息来自多个复制片段：此时即使片段内全是
    “仅时分 / 无时间”的消息，也无法确定它们相对其它片段的位置，需要用户
    显式确认；单片段导入时顺序就是用户粘贴顺序，不视为不确定。

    同刻（同一分钟）多条消息：**同片段内**的相对顺序就是粘贴顺序（稳定
    排序保留）；消息若带本地 ``_chunk_idx`` 标签（app 在导入/追加时打上，
    绝不进入 Jev state），则跨片段同刻记为 ``ambiguous_exact_tie``——
    不虚构先后关系。
    """
    items = list(messages)
    anchored = [(i, m) for i, m in enumerate(items)
                if time_kind(m.get("time")) == TIME_FULL]
    rest = [(i, m) for i, m in enumerate(items)
            if time_kind(m.get("time")) != TIME_FULL]

    tie_groups: dict[tuple, list] = {}
    for i, m in anchored:
        chunk = m.get("_chunk_idx")
        tie_groups.setdefault(_full_key(m["time"].strip()), []).append(chunk)
    ambiguous = sum(
        1 for key, chunks in tie_groups.items()
        if len(set(chunks)) > 1        # 同一时刻跨片段 → 顺序不可确定
    )

    anchored_sorted = sorted(
        anchored, key=lambda pair: (_full_key(pair[1]["time"].strip()), pair[0])
    )
    ordered = [m for _, m in anchored_sorted] + [m for _, m in rest]
    order_changed = ordered != items

    full = len(anchored)
    time_only = sum(1 for _, m in rest if time_kind(m.get("time")) == TIME_ONLY)
    missing = len(rest) - time_only
    unanchored = time_only + missing
    # 无法确认先后的情形：(a) 完整时间与不完整时间混排（后者位置任意）；
    # (b) 多片段导入且存在不完整时间（跨片段位置不可确定）；
    # (c) 同刻跨片段顺序不可确定。
    uncertain = bool(unanchored) and (full > 0 or multi_chunk) \
        or ambiguous > 0
    return TimelineResult(
        messages=ordered, full=full, time_only=time_only, missing=missing,
        ambiguous_exact_tie=ambiguous, order_changed=order_changed,
        uncertain_order=uncertain,
    )


def order_signature(messages: list[dict]) -> str:
    """消息顺序指纹：顺序变了，所有依赖 index 的状态（分析结果/媒体绑定）即失效。"""
    blob = "\n".join(
        f"{m.get('speaker')}|{_norm(m.get('time'))}|{message_fingerprint(m)}"
        for m in messages
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def migrate_bindings(messages_before: list[dict], messages_after: list[dict],
                     bindings: dict) -> tuple[dict, list[int]]:
    """跨排序迁移媒体绑定（index → asset_id）。

    返回 (new_bindings, dropped_old_indices)：能按 fingerprint 唯一对应
    的绑定才迁移；对应不唯一或目标消失的绑定被丢弃（由 UI 要求用户重新
    确认），绝不让图片错配到另一条消息。
    """
    if not bindings:
        return {}, []

    def _fp_index_map(messages):
        out: dict[str, list[int]] = {}
        for i, m in enumerate(messages):
            out.setdefault(message_fingerprint(m), []).append(i)
        return out

    before = _fp_index_map(messages_before)
    after = _fp_index_map(messages_after)

    new_bindings: dict = {}
    dropped: list[int] = []
    for old_index, asset_id in bindings.items():
        if not (0 <= old_index < len(messages_before)):
            dropped.append(old_index)
            continue
        fp = message_fingerprint(messages_before[old_index])
        candidates = after.get(fp, [])
        before_hits = len(before.get(fp, []))
        if len(candidates) == 1 and before_hits == 1:
            new_bindings[candidates[0]] = asset_id
        else:
            # 对应不唯一（如完全相同的重复消息）→ 不猜，丢弃
            dropped.append(old_index)
    return new_bindings, dropped


def preview_page(messages: list[dict], *, mode: str = "recent",
                 limit: int = 15, page: int = 1, page_size: int = 40
                 ) -> tuple[list[tuple[int, dict]], int, int]:
    """预览取窗：返回 ([(全局 index, message)], 总条数, 总页数)。

    mode：
      - ``recent``：最近 limit 条（时间升序阅读，即列表末尾）；
      - ``earliest``：最早 limit 条；
      - ``all``：分页浏览全部，每页 page_size 条。
    只做展示取窗，绝不修改分析列表，也不调用 Jev。
    """
    total = len(messages)
    if mode == "all":
        size = max(1, page_size)
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, page), pages)
        start = (page - 1) * size
        window = list(enumerate(messages[start:start + size], start=start))
        return window, total, pages
    limit = max(1, limit)
    if mode == "earliest":
        window = list(enumerate(messages[:limit]))
    else:  # recent（默认）
        window = list(enumerate(messages[-limit:], start=max(0, total - limit)))
    return window, total, 1


def page_time_range(window: list[tuple[int, dict]]) -> str:
    """预览窗口的日期范围（只取完整时间的首尾；无完整时间时说明原因）。"""
    times = [m.get("time") for _, m in window if time_kind(m.get("time")) == TIME_FULL]
    if not times:
        return "本页无完整时间"
    return f"{times[0]} ~ {times[-1]}"
