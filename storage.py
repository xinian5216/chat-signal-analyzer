"""本地结果缓存（SQLite）。

同一条“消息 + 上下文 + 问题 schema + 模型版本”生成 SHA256 key，
命中缓存时直接复用结果，避免重复消耗 API 额度。

缓存文件位于项目目录 .jev_cache/cache.db，已被 .gitignore 排除。

线程安全设计（修复 Streamlit 跨线程 ProgrammingError）：
- Cache 实例只保存 db_path 与配置，**不长期持有 sqlite3.Connection**；
- 每次 get/set/clear/初始化 schema 都通过 _connect() 获取一个
  短生命周期连接，操作结束立即关闭；
- 因此同一个 Cache 实例可以被多个线程安全复用，
  也不会在 Streamlit rerun（新线程）中触发
  “SQLite objects created in a thread can only be used in that same thread”。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / ".jev_cache" / "cache.db"

CONNECT_TIMEOUT = 5.0      # sqlite3.connect(timeout=...)：等待锁的最长秒数
BUSY_TIMEOUT_MS = 5000     # PRAGMA busy_timeout：毫秒


def make_cache_key(
    state: dict, questions_schema: dict, model: str, schema_version: str
) -> str:
    """由 state、问题 schema、模型和 schema 版本生成稳定的 SHA256 key。"""
    payload = json.dumps(
        {
            "state": state,
            "questions": questions_schema,
            "model": model,
            "schema_version": schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Cache:
    """极简 SQLite 缓存：key -> JSON 结果。

    不持有长期连接；每个操作自建连接、用完即关。接口与旧版一致。
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """获取一个短生命周期连接（每次数据库操作调用一次）。"""
        conn = sqlite3.connect(str(self.db_path), timeout=CONNECT_TIMEOUT)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            # WAL：读写不互斥，降低并发访问时的锁竞争（文件级持久设置）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS analysis_cache ("
                "key TEXT PRIMARY KEY, "
                "result TEXT NOT NULL, "
                "created_at REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT result FROM analysis_cache WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return json.loads(row[0])

    def set(self, key: str, result: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO analysis_cache VALUES (?, ?, ?)",
                (key, json.dumps(result, ensure_ascii=False), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM analysis_cache")
            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        """保留接口兼容：无长期连接可关闭，空操作。"""
        return None
