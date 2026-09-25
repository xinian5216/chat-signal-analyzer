"""本地好友档案与历史分析记忆（Longitudinal Phase 1：P1–P4）。

Phase 2A 起同时承载**行为事件层**（``behavior_events`` /
``behavior_event_audit``，schema v2）：档案库带版本号增量迁移
（``schema_meta``），v1 老库打开时自动补建新表，迁移整体事务化、
失败即回滚，已有的不可变分析快照不受影响。

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

# 档案数据库 schema 版本（Phase 2A 引入行为事件表 → v2）。
# 迁移必须是**增量且可失败回滚**的：用户的数据库完全可能是 Phase 1 时代的
# v1（没有 schema_meta、没有行为事件表）。不能假设它永远是新创建的。
SCHEMA_VERSION_FRIEND_HISTORY = 2


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

    # v1 基础表（Phase 1 时代的内容；老库里已存在 → IF NOT EXISTS 幂等）
    _V1_DDL: tuple[str, ...] = (
        """
        CREATE TABLE IF NOT EXISTS friends (
            friend_id   TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            notes       TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS friend_aliases (
            friend_id  TEXT NOT NULL,
            alias      TEXT NOT NULL,
            display    TEXT NOT NULL,
            kind       TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_friend_aliases_alias
            ON friend_aliases(alias)
        """,
        """
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
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_history_runs_friend
            ON history_runs(friend_id, saved_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_history_runs_case
            ON history_runs(friend_id, case_signature)
        """,
        """
        CREATE TABLE IF NOT EXISTS history_messages (
            run_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            msg_index INTEGER NOT NULL,
            chat_time TEXT,
            speaker TEXT,
            is_target INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_history_messages_fp
            ON history_messages(fingerprint)
        """,
        """
        CREATE TABLE IF NOT EXISTS history_evidence (
            run_id TEXT NOT NULL,
            msg_index INTEGER NOT NULL,
            stance TEXT NOT NULL,
            note TEXT NOT NULL,
            snippet TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS history_results (
            run_id TEXT PRIMARY KEY,
            results_json TEXT NOT NULL
        )
        """,
    )

    # v2 行为事件表（Phase 2A）。作为类属性暴露，便于测试注入失败语句验证
    # 事务回滚与失败恢复。
    _V2_DDL: tuple[str, ...] = (
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS behavior_events (
            event_id TEXT PRIMARY KEY,
            friend_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            behavior_type TEXT NOT NULL,
            stance TEXT NOT NULL DEFAULT 'unspecified',
            status TEXT NOT NULL DEFAULT 'candidate',
            source_kind TEXT NOT NULL DEFAULT 'rule',
            source_run_id TEXT,
            event_start_time TEXT,
            event_end_time TEXT,
            time_confidence TEXT NOT NULL DEFAULT 'unknown',
            msg_window_json TEXT NOT NULL DEFAULT '[]',
            fingerprints_json TEXT NOT NULL DEFAULT '[]',
            flags_json TEXT NOT NULL DEFAULT '{}',
            alternative TEXT NOT NULL DEFAULT '',
            support_evidence TEXT NOT NULL DEFAULT '',
            counter_evidence TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            snippet TEXT NOT NULL DEFAULT '',
            user_feeling TEXT NOT NULL DEFAULT '',
            review_note TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            reviewed_at REAL,
            event_identity TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS behavior_event_audit (
            audit_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_behavior_events_friend
            ON behavior_events(friend_id, status)
        """,
        # 同一事件重复导入不得重复计算：确认 / 排除状态下
        # (friend_id, event_identity) 唯一（部分索引）。
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_behavior_events_identity
            ON behavior_events(friend_id, event_identity)
            WHERE status IN ('confirmed', 'rejected')
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_behavior_event_audit_event
            ON behavior_event_audit(event_id)
        """,
    )

    # 目标 schema 版本（测试可降低它来模拟“旧版本创建的库”）
    _TARGET_VERSION: int = SCHEMA_VERSION_FRIEND_HISTORY

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=CONNECT_TIMEOUT)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # 显式事务：DDL 也必须可回滚——迁移失败时不能留下半套表。
            # （sqlite3 默认只在 DML 前隐式 BEGIN，DDL 会在 autocommit 下
            #   逐条提交，因此这里手工管理隔离级别。）
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._upgrade_schema(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _upgrade_schema(self, conn: sqlite3.Connection) -> None:
        """按版本号增量迁移（幂等；失败由调用方整体回滚）。"""
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'schema_meta'").fetchone()
        version = 0
        if row is not None:
            value = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if value and str(value[0]).isdigit():
                version = int(value[0])
        if version < 1:
            for stmt in self._V1_DDL:
                conn.execute(stmt)
            if row is None:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta ("
                    " key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES"
                " ('schema_version', '1')")
            version = 1
        if version < 2 and self._TARGET_VERSION >= 2:
            for stmt in self._V2_DDL:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES"
                " ('schema_version', '2')")
            version = 2

    def schema_version(self) -> int:
        """档案数据库当前 schema 版本（v1 老库读出来是 1 或 0）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()
        if row and str(row[0]).isdigit():
            return int(row[0])
        return 0

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
        """删除好友及其全部历史（级联：runs / messages / evidence / aliases /
        behavior events / audit）。"""
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
            # 行为事件（Phase 2A）随档案一起删除，审计记录一并清除
            conn.execute("DELETE FROM friends WHERE friend_id = ?", (friend_id,))
            event_ids = [r[0] for r in conn.execute(
                "SELECT event_id FROM behavior_events WHERE friend_id = ?",
                (friend_id,)).fetchall()]
            for event_id in event_ids:
                conn.execute(
                    "DELETE FROM behavior_event_audit WHERE event_id = ?",
                    (event_id,))
            conn.execute("DELETE FROM behavior_events WHERE friend_id = ?",
                         (friend_id,))
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

    # ---- 行为事件（Phase 2A，schema v2）----

    def save_event(self, event: dict) -> str | None:
        """写入一个行为事件（候选被人工确认 / 手动创建 / 人工排除）。

        去重是**结构性**的：同一好友下，确认或排除状态的
        (friend_id, event_identity) 唯一。重复导入同一事件时直接返回既有
        event_id（幂等），不重复计算。候选（未确认）不落库——它们每次从
        当前聊天重新生成，避免档案里堆积垃圾。
        """
        identity = str(event.get("event_identity") or "")
        friend_id = str(event.get("friend_id") or "")
        status = str(event.get("status") or "candidate")
        if identity and friend_id and status in ("confirmed", "rejected"):
            existing = self.events_by_identity(friend_id, identity)
            if existing:
                return existing[0]["event_id"]

        event_id = str(event.get("event_id") or new_run_id())
        now = time.time()
        # store 层兜底脱敏：所有写入路径（手动 / 确认 / 编辑）一致处理，
        # 不依赖调用方已经脱敏（与 normalize_evidence 同风格）
        event = dict(event)
        event["snippet"] = anonymize_evidence_text(event.get("snippet") or "")
        created_at = float(event.get("created_at") or now)
        updated_at = float(event.get("updated_at") or now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO behavior_events VALUES ("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, friend_id,
                    str(event.get("dimension") or ""),
                    str(event.get("behavior_type") or ""),
                    str(event.get("stance") or "unspecified"),
                    status,
                    str(event.get("source_kind") or "rule"),
                    event.get("source_run_id"),
                    event.get("event_start_time"),
                    event.get("event_end_time"),
                    str(event.get("time_confidence") or "unknown"),
                    json.dumps(list(event.get("msg_window") or []),
                               ensure_ascii=False),
                    json.dumps(list(event.get("fingerprints") or []),
                               ensure_ascii=False),
                    json.dumps(dict(event.get("flags") or {}),
                               ensure_ascii=False, sort_keys=True),
                    str(event.get("alternative") or ""),
                    str(event.get("support_evidence") or ""),
                    str(event.get("counter_evidence") or ""),
                    str(event.get("notes") or ""),
                    event["snippet"],
                    str(event.get("user_feeling") or ""),
                    str(event.get("review_note") or ""),
                    created_at, updated_at,
                    event.get("reviewed_at") if event.get("reviewed_at")
                    is not None else now,
                    identity,
                ),
            )
            self._insert_audit(
                conn, event_id, "created",
                f"来源 {event.get('source_kind') or 'rule'}，"
                f"方向 {event.get('dimension')}，"
                f"行为 {event.get('behavior_type')}，状态 {status}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return event_id

    @staticmethod
    def _insert_audit(conn: sqlite3.Connection, event_id: str, action: str,
                      detail: str = "") -> None:
        conn.execute(
            "INSERT INTO behavior_event_audit VALUES (?, ?, ?, ?, ?)",
            (new_run_id(), event_id, action, detail, time.time()),
        )

    _EVENT_COLUMNS = (
        "SELECT event_id, friend_id, dimension, behavior_type, stance,"
        " status, source_kind, source_run_id, event_start_time,"
        " event_end_time, time_confidence, msg_window_json,"
        " fingerprints_json, flags_json, alternative, support_evidence,"
        " counter_evidence, notes, snippet, user_feeling, review_note,"
        " created_at, updated_at, reviewed_at, event_identity"
        " FROM behavior_events"
    )

    def _row_to_event(self, row) -> dict:
        return {
            "event_id": row[0], "friend_id": row[1], "dimension": row[2],
            "behavior_type": row[3], "stance": row[4], "status": row[5],
            "source_kind": row[6], "source_run_id": row[7],
            "event_start_time": row[8], "event_end_time": row[9],
            "time_confidence": row[10],
            "msg_window": json.loads(row[11] or "[]"),
            "fingerprints": json.loads(row[12] or "[]"),
            "flags": json.loads(row[13] or "{}"),
            "alternative": row[14], "support_evidence": row[15],
            "counter_evidence": row[16], "notes": row[17],
            "snippet": row[18], "user_feeling": row[19],
            "review_note": row[20], "created_at": row[21],
            "updated_at": row[22], "reviewed_at": row[23],
            "event_identity": row[24],
        }

    def get_event(self, event_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                self._EVENT_COLUMNS + " WHERE event_id = ?",
                (event_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        event = self._row_to_event(row)
        event["audit"] = self.audit_of(event_id)
        return event

    def list_events(self, friend_id: str, *, status: str | None = None,
                    dimension: str | None = None) -> list[dict]:
        """该好友的行为事件，按聊天发生时间归位（最早在前；无时间的排最后）。"""
        sql = self._EVENT_COLUMNS + " WHERE friend_id = ?"
        params: list = [friend_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if dimension:
            sql += " AND dimension = ?"
            params.append(dimension)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        events = [self._row_to_event(r) for r in rows]
        events.sort(key=lambda e: (
            e["event_start_time"] or "9999-99-99 99:99",
            e["event_end_time"] or "9999-99-99 99:99",
            e["created_at"],
        ))
        return events

    def events_by_identity(self, friend_id: str, identity: str) -> list[dict]:
        if not identity:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                self._EVENT_COLUMNS
                + " WHERE friend_id = ? AND event_identity = ?"
                " ORDER BY created_at",
                (friend_id, identity)).fetchall()
        finally:
            conn.close()
        return [self._row_to_event(r) for r in rows]

    def event_counts(self, friend_id: str) -> dict[str, int]:
        """按状态统计（用于"未审核候选数"与报告统计）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM behavior_events"
                " WHERE friend_id = ? GROUP BY status",
                (friend_id,)).fetchall()
        finally:
            conn.close()
        return {row[0]: int(row[1]) for row in rows}

    _EVENT_EDITABLE = (
        "stance", "dimension", "behavior_type", "notes", "snippet",
        "alternative", "support_evidence", "counter_evidence",
        "user_feeling", "review_note", "time_confidence",
        "event_start_time", "event_end_time",
    )

    def update_event(self, event_id: str, changes: dict) -> bool:
        """更新可编辑字段（白名单；任何修改写审计）。"""
        safe = {k: v for k, v in (changes or {}).items()
                if k in self._EVENT_EDITABLE}
        if not safe:
            return False
        if "snippet" in safe:
            safe["snippet"] = anonymize_evidence_text(safe.get("snippet") or "")
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM behavior_events WHERE event_id = ?",
                (event_id,)).fetchone()
            if exists is None:
                return False
            conn.execute("BEGIN IMMEDIATE")
            assignments = ", ".join(f"{k} = ?" for k in safe)
            conn.execute(
                f"UPDATE behavior_events SET {assignments}, updated_at = ?"
                " WHERE event_id = ?",
                (*safe.values(), time.time(), event_id))
            self._insert_audit(conn, event_id, "edited",
                               "修改字段：" + "、".join(sorted(safe)))
            conn.execute("COMMIT")
        finally:
            conn.close()
        return True

    def set_event_status(self, event_id: str, status: str,
                         note: str = "") -> bool:
        """人工确认 / 排除（写审计； reviewed_at 记录核对时间）。"""
        if status not in ("confirmed", "rejected", "candidate"):
            return False
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM behavior_events WHERE event_id = ?",
                (event_id,)).fetchone()
            if exists is None:
                return False
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE behavior_events SET status = ?, reviewed_at = ?,"
                " updated_at = ? WHERE event_id = ?",
                (status, time.time(), time.time(), event_id))
            self._insert_audit(conn, event_id, status, note)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return True

    def delete_event(self, event_id: str) -> bool:
        """删除一个事件及其审计记录（用户的人工修正）。"""
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM behavior_events WHERE event_id = ?",
                (event_id,)).fetchone()
            if exists is None:
                return False
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM behavior_event_audit WHERE event_id = ?",
                (event_id,))
            conn.execute("DELETE FROM behavior_events WHERE event_id = ?",
                         (event_id,))
            conn.execute("COMMIT")
        finally:
            conn.close()
        return True

    def audit_of(self, event_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT audit_id, action, detail, created_at"
                " FROM behavior_event_audit WHERE event_id = ?"
                " ORDER BY created_at, audit_id",
                (event_id,)).fetchall()
        finally:
            conn.close()
        return [{"audit_id": r[0], "action": r[1], "detail": r[2],
                 "created_at": r[3]} for r in rows]
