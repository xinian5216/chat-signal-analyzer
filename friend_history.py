"""本地好友档案与历史分析记忆（Longitudinal Phase 1：P1–P4）。

设计目标：让用户可以把**已经分析完成**的结果按“好友”归档，下次导入同一
位好友的聊天时看到历史总结、识别重复记录、比较不同时间段的互动——同时
不引入任何新的模型调用、不改动现有九问与评分。

硬约束（与 AGENTS.md 一致）：

- **全部本地**：独立 SQLite 文件（``paths.friend_history_db_path()``），
  与 ``storage.Cache`` 的 ``analysis_cache`` 完全分离——清 API 缓存不会
  碰档案，删档案也不会影响 API 缓存；
- **身份不是昵称**：``friend_id``（随机）才是身份；微信昵称 / 备注 / 别名
  只是本地查找键。同一个别名命中多个好友时**绝不合并且**，交由用户选择；
- **不外发**：好友昵称、别名、``friend_id``、备注绝不进入 Jev state /
  cache key / 出站请求（``build_state`` 的出站白名单保持不变）；
- **不留聊天正文**：快照只保存本地 fingerprint、时间、条数与九问结果
  （与默认匿名导出一致，不含 ``text``）；要保留证据片段必须由用户显式
  选择，且保存前再过一次 ``privacy.mask_text``；
- **无日志昵称**：本模块不做任何 logging，也不返回/打印昵称或聊天内容；
- **时间分离**：``analysis_*_at``（分析发生时间）与 ``chat_*_time``
  （聊天发生时间）是不同字段，纵向比较只按聊天时间归位。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import paths
from merge import message_fingerprint
from privacy import mask_text
from timeline import TIME_FULL, time_kind

# 证据片段保留的最大长度（匿名化之后）：足够定位，不当全文副本
EVIDENCE_MAX_CHARS = 120

# 一次快照里最多保留的证据片段数（防止把档案变成第二个全文副本）
EVIDENCE_MAX_ITEMS = 20

# 别名规范化：大小写无关、内部空白折叠
CONNECT_TIMEOUT = 5.0
BUSY_TIMEOUT_MS = 5000

ALIAS_KINDS = ("wechat_name", "remark", "alias")


# ---------------------------------------------------------------------------
# 数据结构（纯数据，不含任何昵称以外的隐私字段）
# ---------------------------------------------------------------------------


@dataclass
class Friend:
    friend_id: str
    display_name: str
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class AliasMatch:
    """一个别名命中的好友（用于“找到多个 → 让用户选”）。"""

    friend_id: str
    display_name: str
    alias: str            # 规范化查找键
    display: str          # 用户当时填写的写法
    kind: str
    run_count: int = 0    # 该好友已有多少条历史分析（帮助用户区分重名）


@dataclass
class RunSummary:
    """列表用：不含逐条结果，避免把整包数据读进内存。"""

    run_id: str
    friend_id: str
    saved_at: float
    analysis_started_at: float | None
    analysis_completed_at: float | None
    chat_first_time: str | None
    chat_last_time: str | None
    full_time_ratio: float | None
    message_count: int
    analyzed_count: int
    failed_count: int
    skipped_media_count: int
    case_signature: str
    schema_version: str
    request_model: str
    response_model: str | None
    summary_text: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 纯函数：身份 / 指纹 / 时间
# ---------------------------------------------------------------------------


def normalize_alias(value: str) -> str:
    """别名查找键：去首尾空白、内部连续空白折叠、小写。

    只用于**查找**，不覆盖用户原始写法（``display`` 仍保留原文）。
    """
    return " ".join(str(value if value is not None else "").split()).lower()


def new_friend_id() -> str:
    """随机 friend_id（16 hex）。不用昵称、不用自增数字。"""
    return secrets.token_hex(8)


def new_run_id() -> str:
    return secrets.token_hex(12)


def case_signature(messages: list[dict]) -> str:
    """“这次分析的是哪些消息”的稳定指纹（可去重的案例身份）。

    组成：消息条数 + 全部 message fingerprint 的排序集合。
    同一次重复分析 → 相同签名；同时间不同内容的消息 → fingerprint 不同 →
    签名不同，因此**不会**被误判为重复。
    """
    fps = sorted({message_fingerprint(m) for m in messages})
    payload = "\n".join(["case-v1", str(len(messages))] + fps)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chat_time_span(messages: list[dict]) -> tuple[str | None, str | None]:
    """完整时间消息的最早 / 最晚时间（无完整时间时为 (None, None)）。

    ``YYYY-MM-DD HH:MM`` 是定宽格式，字典序即时间序，因此 min/max 安全。
    """
    times = [str(m.get("time")) for m in messages
             if time_kind(m.get("time")) == TIME_FULL]
    if not times:
        return None, None
    return min(times), max(times)


def full_time_ratio(messages: list[dict]) -> float | None:
    """完整时间记录比例（1.0 = 全部可归位；None = 没有消息）。"""
    if not messages:
        return None
    full = sum(1 for m in messages if time_kind(m.get("time")) == TIME_FULL)
    return full / len(messages)


def anonymize_evidence_text(text: str) -> str:
    """证据片段匿名化：先过本地脱敏，再截断（不当全文副本）。"""
    cleaned = mask_text(str(text or ""))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > EVIDENCE_MAX_CHARS:
        cleaned = cleaned[:EVIDENCE_MAX_CHARS] + "…"
    return cleaned


def build_run_snapshot(
    *,
    friend_id: str,
    messages: list[dict],
    results: list[dict],
    stats: dict,
    skipped_media: int = 0,
    analysis_started_at: float | None = None,
    analysis_completed_at: float | None = None,
    schema_version: str,
    request_model: str,
    summary_text: str = "",
    evidence: list[dict] | None = None,
) -> dict:
    """把一次**已完成**的分析打包成不可变快照（纯函数，不碰数据库）。

    ``messages`` 是分析时使用的完整消息列表（排序后），``results`` 是
    ``analyzer.analyze_messages`` 的返回。快照**不含**消息正文：只有
    fingerprint / 时间 / 条数 / 九问结果。
    """
    first, last = chat_time_span(messages)
    target_indices = {e["index"] for e in results}
    msg_rows = [
        {
            "fingerprint": message_fingerprint(m),
            "index": i,
            "chat_time": m.get("time"),
            # 只存规范化角色（me / them）；raw_speaker 等 parser 内部字段
            # 一律不进档案
            "speaker": m.get("speaker"),
            "is_target": 1 if i in target_indices else 0,
        }
        for i, m in enumerate(messages)
    ]

    result_rows = []
    for entry in sorted(results, key=lambda e: e["index"]):
        row = {
            "index": entry["index"],
            "time": entry.get("time"),
            "speaker": entry.get("speaker", "them"),
            "cached": bool(entry.get("cached")),
        }
        if entry.get("error"):
            row["error"] = entry["error"]
        else:
            row["result"] = entry["result"]
        result_rows.append(row)

    # 实际返回的模型版本：Jev 响应里带的 model（可能与请求模型不同）
    response_model = None
    for row in result_rows:
        model = (row.get("result") or {}).get("model")
        if model:
            response_model = model
            break

    return {
        "friend_id": friend_id,
        "analysis_started_at": analysis_started_at,
        "analysis_completed_at": analysis_completed_at,
        "chat_first_time": first,
        "chat_last_time": last,
        "full_time_ratio": full_time_ratio(messages),
        "message_count": len(messages),
        "analyzed_count": int(stats.get("analyzed") or 0),
        "failed_count": int(stats.get("failed") or 0),
        "skipped_media_count": int(skipped_media or 0),
        "case_signature": case_signature(messages),
        "schema_version": schema_version,
        "request_model": request_model,
        "response_model": response_model,
        "stats": stats,
        "summary_text": summary_text,
        "warnings": list(stats.get("warnings") or []),
        "messages": msg_rows,
        "results": result_rows,
        "evidence": normalize_evidence(evidence),
    }


def normalize_evidence(evidence: list[dict] | None) -> list[dict]:
    """证据片段：只保留白名单字段 + 匿名化文本 + 去重 + 数量上限。

    - 按**消息身份（index）**去重：同一条消息只保留一份证据（写入数据库
      前的最后一道校验，不依赖 UI 已经去过重）；
    - ``snippet``（可选）是用户选择保留的匿名化证据片段：先过本地脱敏再
      截断，长度受限；没有显式选择时只保留确定性指标说明（不含聊天文本）；
    - ``stance`` 允许 ``supporting`` / ``counter`` / ``mixed``（同一条消息
      同时入选两类）/ ``context``，其它值归一到 support。
    """
    out: list[dict] = []
    seen: set[int] = set()
    for item in (evidence or []):
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index in seen:
            continue                          # 同一条消息：只保留第一份
        stance = str(item.get("stance") or "supporting")
        if stance not in ("supporting", "counter", "mixed", "context"):
            stance = "supporting"
        note = anonymize_evidence_text(item.get("note") or "")
        if not note:
            continue
        seen.add(index)
        if len(out) >= EVIDENCE_MAX_ITEMS:
            break
        out.append({"index": index, "stance": stance, "note": note,
                    "snippet": anonymize_evidence_text(item.get("snippet") or "")})
    return out


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class FriendStore:
    """好友档案 + 历史分析快照的 SQLite 存储。

    与 ``storage.Cache`` 相同的连接纪律：**不长期持有连接**，每个操作自建
    连接、用完即关（Streamlit rerun 跨线程下必须如此）。
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else paths.friend_history_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- 连接 ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=CONNECT_TIMEOUT)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS friends (
                    friend_id   TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    notes       TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS friend_aliases (
                    friend_id  TEXT NOT NULL,
                    alias      TEXT NOT NULL,
                    display    TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_friend_aliases_alias
                    ON friend_aliases(alias);
                CREATE TABLE IF NOT EXISTS history_runs (
                    run_id TEXT PRIMARY KEY,
                    friend_id TEXT NOT NULL,
                    saved_at REAL NOT NULL,
                    analysis_started_at REAL,
                    analysis_completed_at REAL,
                    chat_first_time TEXT,
                    chat_last_time TEXT,
                    full_time_ratio REAL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    analyzed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_media_count INTEGER NOT NULL DEFAULT 0,
                    case_signature TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    request_model TEXT NOT NULL,
                    response_model TEXT,
                    stats_json TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    warnings_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_history_runs_friend
                    ON history_runs(friend_id, saved_at);
                CREATE INDEX IF NOT EXISTS idx_history_runs_case
                    ON history_runs(friend_id, case_signature);
                CREATE TABLE IF NOT EXISTS history_messages (
                    run_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    msg_index INTEGER NOT NULL,
                    chat_time TEXT,
                    speaker TEXT,
                    is_target INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_history_messages_fp
                    ON history_messages(fingerprint);
                CREATE TABLE IF NOT EXISTS history_evidence (
                    run_id TEXT NOT NULL,
                    msg_index INTEGER NOT NULL,
                    stance TEXT NOT NULL,
                    note TEXT NOT NULL,
                    snippet TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history_results (
                    run_id TEXT PRIMARY KEY,
                    results_json TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ---- 好友 ----

    def create_friend(self, display_name: str, aliases=(),
                      notes: str = "") -> Friend:
        """新建好友档案。``aliases`` 是 [(alias, kind), ...] 或 [alias, ...]。"""
        now = time.time()
        friend = Friend(friend_id=new_friend_id(),
                        display_name=str(display_name or "").strip() or "未命名档案",
                        notes=str(notes or ""),
                        created_at=now, updated_at=now)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO friends VALUES (?, ?, ?, ?, ?)",
                (friend.friend_id, friend.display_name, friend.notes,
                 friend.created_at, friend.updated_at),
            )
            for item in aliases or ():
                if isinstance(item, (tuple, list)):
                    alias, kind = (list(item) + ["alias"])[:2]
                else:
                    alias, kind = item, "alias"
                self._insert_alias(conn, friend.friend_id, str(alias),
                                   str(kind or "alias"), now)
            conn.commit()
        finally:
            conn.close()
        return friend

    def _insert_alias(self, conn: sqlite3.Connection, friend_id: str,
                      display: str, kind: str, now: float) -> bool:
        alias = normalize_alias(display)
        if not alias:
            return False
        row = conn.execute(
            "SELECT 1 FROM friend_aliases WHERE friend_id = ? AND alias = ?",
            (friend_id, alias),
        ).fetchone()
        if row is not None:
            return False                       # 同一好友的同一别名：不重复
        conn.execute(
            "INSERT INTO friend_aliases VALUES (?, ?, ?, ?, ?)",
            (friend_id, alias, str(display), kind if kind in ALIAS_KINDS
             else "alias", now),
        )
        return True

    def add_alias(self, friend_id: str, display: str,
                  kind: str = "alias") -> bool:
        """给已有好友加一个查找键（同人多昵称就是这么累积的）。"""
        if not self.get_friend(friend_id):
            return False
        conn = self._connect()
        try:
            added = self._insert_alias(conn, friend_id, str(display),
                                       str(kind or "alias"), time.time())
            conn.commit()
        finally:
            conn.close()
        return added

    def remove_alias(self, friend_id: str, display: str) -> bool:
        alias = normalize_alias(display)
        if not alias:
            return False
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM friend_aliases WHERE friend_id = ? AND alias = ?",
                (friend_id, alias),
            )
            conn.commit()
        finally:
            conn.close()
        return bool(cur.rowcount)

    def list_friends(self) -> list[Friend]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT friend_id, display_name, notes, created_at, updated_at"
                " FROM friends ORDER BY updated_at DESC, friend_id"
            ).fetchall()
        finally:
            conn.close()
        return [Friend(friend_id=r[0], display_name=r[1], notes=r[2],
                       created_at=r[3], updated_at=r[4]) for r in rows]

    def get_friend(self, friend_id: str) -> Friend | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT friend_id, display_name, notes, created_at, updated_at"
                " FROM friends WHERE friend_id = ?", (friend_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Friend(friend_id=row[0], display_name=row[1], notes=row[2],
                      created_at=row[3], updated_at=row[4])

    def aliases_of(self, friend_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT alias, display, kind FROM friend_aliases"
                " WHERE friend_id = ? ORDER BY created_at, alias",
                (friend_id,),
            ).fetchall()
        finally:
            conn.close()
        return [{"alias": r[0], "display": r[1], "kind": r[2]} for r in rows]

    def find_by_alias(self, alias: str) -> list[AliasMatch]:
        """按昵称/备注/别名查找好友。

        命中多个好友时**全部返回**（调用方必须让用户选择，绝不自动合并）。
        """
        key = normalize_alias(alias)
        if not key:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT a.friend_id, f.display_name, a.alias, a.display, a.kind"
                " FROM friend_aliases a JOIN friends f"
                "   ON f.friend_id = a.friend_id"
                " WHERE a.alias = ? ORDER BY a.created_at, a.friend_id",
                (key,),
            ).fetchall()
        finally:
            conn.close()
        out: list[AliasMatch] = []
        for friend_id, display_name, norm, display, kind in rows:
            out.append(AliasMatch(
                friend_id=friend_id, display_name=display_name,
                alias=norm, display=display, kind=kind,
                run_count=self.run_count(friend_id),
            ))
        return out

    def update_friend(self, friend_id: str, display_name: str | None = None,
                      notes: str | None = None) -> Friend | None:
        friend = self.get_friend(friend_id)
        if friend is None:
            return None
        if display_name is not None:
            friend.display_name = (str(display_name).strip()
                                   or friend.display_name)
        if notes is not None:
            friend.notes = str(notes)
        friend.updated_at = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE friends SET display_name = ?, notes = ?, updated_at = ?"
                " WHERE friend_id = ?",
                (friend.display_name, friend.notes, friend.updated_at,
                 friend_id),
            )
            conn.commit()
        finally:
            conn.close()
        return friend

    def delete_friend(self, friend_id: str) -> bool:
        """删除好友及其全部历史（级联：runs / messages / evidence / aliases）。"""
        friend = self.get_friend(friend_id)
        if friend is None:
            return False
        conn = self._connect()
        try:
            run_ids = [r[0] for r in conn.execute(
                "SELECT run_id FROM history_runs WHERE friend_id = ?",
                (friend_id,)).fetchall()]
            for run_id in run_ids:
                conn.execute("DELETE FROM history_messages WHERE run_id = ?",
                             (run_id,))
                conn.execute("DELETE FROM history_evidence WHERE run_id = ?",
                             (run_id,))
            conn.execute("DELETE FROM history_runs WHERE friend_id = ?",
                         (friend_id,))
            conn.execute("DELETE FROM friend_aliases WHERE friend_id = ?",
                         (friend_id,))
            conn.execute("DELETE FROM friends WHERE friend_id = ?", (friend_id,))
            conn.commit()
        finally:
            conn.close()
        return True

    def friend_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM friends").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0

    # ---- 历史分析快照 ----

    def save_run(self, snapshot: dict) -> str:
        """写入一条**不可变**快照。

        每次都生成新的 ``run_id``：重复保存同一案例会产生新记录（历史不容
        篡改），调用方把返回值当作这条记录的 ID。
        """
        run_id = new_run_id()
        stats_json = json.dumps(snapshot.get("stats") or {},
                                ensure_ascii=False, sort_keys=True)
        warnings_json = json.dumps(snapshot.get("warnings") or [],
                                   ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO history_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    snapshot["friend_id"],
                    time.time(),
                    snapshot.get("analysis_started_at"),
                    snapshot.get("analysis_completed_at"),
                    snapshot.get("chat_first_time"),
                    snapshot.get("chat_last_time"),
                    snapshot.get("full_time_ratio"),
                    int(snapshot.get("message_count") or 0),
                    int(snapshot.get("analyzed_count") or 0),
                    int(snapshot.get("failed_count") or 0),
                    int(snapshot.get("skipped_media_count") or 0),
                    snapshot.get("case_signature") or "",
                    snapshot.get("schema_version") or "",
                    snapshot.get("request_model") or "",
                    snapshot.get("response_model"),
                    stats_json,
                    snapshot.get("summary_text") or "",
                    warnings_json,
                ),
            )
            for row in snapshot.get("messages") or []:
                conn.execute(
                    "INSERT INTO history_messages VALUES (?,?,?,?,?,?)",
                    (run_id, row["fingerprint"], int(row["index"]),
                     row.get("chat_time"), row.get("speaker"),
                     int(row.get("is_target") or 0)),
                )
            for row in snapshot.get("evidence") or []:
                conn.execute(
                    "INSERT INTO history_evidence VALUES (?,?,?,?,?,?)",
                    (run_id, int(row["index"]), row["stance"], row["note"],
                     row.get("snippet") or "", time.time()),
                )
            conn.execute(
                "INSERT INTO history_results VALUES (?, ?)",
                (run_id, json.dumps(snapshot.get("results") or [],
                                    ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def _row_to_summary(self, row) -> RunSummary:
        return RunSummary(
            run_id=row[0], friend_id=row[1], saved_at=row[2],
            analysis_started_at=row[3], analysis_completed_at=row[4],
            chat_first_time=row[5], chat_last_time=row[6],
            full_time_ratio=row[7], message_count=row[8],
            analyzed_count=row[9], failed_count=row[10],
            skipped_media_count=row[11], case_signature=row[12],
            schema_version=row[13], request_model=row[14],
            response_model=row[15], summary_text=row[17],
            warnings=json.loads(row[18] or "[]"),
        )

    _RUN_COLUMNS = (
        "SELECT run_id, friend_id, saved_at, analysis_started_at,"
        " analysis_completed_at, chat_first_time, chat_last_time,"
        " full_time_ratio, message_count, analyzed_count, failed_count,"
        " skipped_media_count, case_signature, schema_version,"
        " request_model, response_model, stats_json, summary_text,"
        " warnings_json FROM history_runs"
    )

    def list_runs(self, friend_id: str) -> list[RunSummary]:
        """该好友的全部历史快照，按**聊天发生时间**归位（最早在前）。

        后来分析的旧聊天会被排到它真实的聊天位置上，而不是保存时间位置上。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                self._RUN_COLUMNS + " WHERE friend_id = ?", (friend_id,)
            ).fetchall()
        finally:
            conn.close()
        summaries = [self._row_to_summary(r) for r in rows]
        summaries.sort(key=lambda s: (
            s.chat_first_time or "9999-99-99 99:99",
            s.chat_last_time or "9999-99-99 99:99",
            s.saved_at,
        ))
        return summaries

    def get_run(self, run_id: str) -> dict | None:
        """完整快照（含逐条九问结果、消息指纹与证据片段）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                self._RUN_COLUMNS + " WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            messages = conn.execute(
                "SELECT fingerprint, msg_index, chat_time, speaker, is_target"
                " FROM history_messages WHERE run_id = ?"
                " ORDER BY msg_index",
                (run_id,),
            ).fetchall()
            results = conn.execute(
                "SELECT msg_index, stance, note, snippet FROM history_evidence"
                " WHERE run_id = ? ORDER BY msg_index",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        summary = self._row_to_summary(row)
        stats = json.loads(row[16] or "{}")
        return {
            **summary.__dict__,
            "stats": stats,
            "messages": [
                {"fingerprint": m[0], "index": m[1], "chat_time": m[2],
                 "speaker": m[3], "is_target": bool(m[4])}
                for m in messages
            ],
            "results": self._load_results(run_id),
            "evidence": [
                {"index": e[0], "stance": e[1], "note": e[2], "snippet": e[3]}
                for e in results
            ],
        }

    def _load_results(self, run_id: str) -> list[dict]:
        """逐条九问结果（单独一列，保持 _RUN_COLUMNS 精简）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT results_json FROM history_results WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return []
        try:
            return json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            return []

    def delete_run(self, run_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM history_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM history_messages WHERE run_id = ?",
                         (run_id,))
            conn.execute("DELETE FROM history_evidence WHERE run_id = ?",
                         (run_id,))
            conn.execute("DELETE FROM history_results WHERE run_id = ?",
                         (run_id,))
            conn.execute("DELETE FROM history_runs WHERE run_id = ?",
                         (run_id,))
            conn.commit()
        finally:
            conn.close()
        return True

    def run_count(self, friend_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM history_runs WHERE friend_id = ?",
                (friend_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0

    def find_runs_by_case(self, friend_id: str,
                          signature: str) -> list[RunSummary]:
        """同一位好友下、案例指纹相同的快照（重复分析检测）。"""
        if not signature:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                self._RUN_COLUMNS
                + " WHERE friend_id = ? AND case_signature = ?",
                (friend_id, signature),
            ).fetchall()
        finally:
            conn.close()
        out = [self._row_to_summary(r) for r in rows]
        out.sort(key=lambda s: s.saved_at)
        return out

    def all_fingerprints(self, friend_id: str) -> set[str]:
        """该好友历史里出现过的全部消息指纹（用于导入去重）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT m.fingerprint FROM history_messages m"
                " JOIN history_runs r ON r.run_id = m.run_id"
                " WHERE r.friend_id = ?",
                (friend_id,),
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}
