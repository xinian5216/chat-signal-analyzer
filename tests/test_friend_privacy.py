"""好友档案的隐私与隔离测试（P6 及其它约束）。

覆盖：
- 档案数据库文件里**不出现**昵称与聊天正文（字节级检查）；
- 保存/查找过程不往 stdout/stderr 打印昵称或聊天内容；
- 昵称、备注、friend_id 绝不进入 Jev state（出站白名单不变）；
- 保存档案不发起任何 Jev 请求；
- ``paths.describe()`` 只含路径，不含密钥；
- 开发模式的档案目录被 .gitignore 覆盖；
- 档案与导出的副本边界明确（导出快照只含白名单字段）。
"""

import json
import re

import pytest

import app  # noqa: F401  （_exportable_snapshot 需要）
import analyzer
import friend_history as fh
import paths
import storage
from parser import parse_chat
from privacy import mask_messages
from scoring import compute_conversation_stats
from timeline import sort_messages

NICKNAME = "测试甲."
SECRET_TEXT = "这是一句绝不能出现在档案里的私密内容"
SCHEMA = "chat-signal-v3.3"


def build_messages():
    chat = (
        f"{NICKNAME}\n2026年08月21日 21:00\n{SECRET_TEXT}\n\n"
        f"测试乙\n2026年08月21日 21:05\n收到，谢谢"
    )
    return sort_messages(mask_messages(
        parse_chat(chat, NICKNAME, "测试乙"))).messages


def make_answer():
    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": 0.9},
        "intent": {"choice": "other", "probabilities": {"other": 1.0},
                   "confidence": 0.9},
        "warmth": {"score": 2.0, "probabilities": {}, "confidence": 0.9},
        "engagement": {"score": 2.0, "probabilities": {},
                       "confidence": 0.9},
        "special_attention": {"score": 1.0, "probabilities": {},
                              "confidence": 0.9},
        "relationship_evidence_strength": {"score": 2.4, "probabilities": {},
                                           "confidence": 0.9},
        "relational_ease": {"score": 2.0, "probabilities": {},
                            "confidence": 0.9},
        "romantic_signal": 0.2, "distancing_signal": 0.2,
        "model": "jev-1.13.0",
    }


def seed(tmp_path):
    store = fh.FriendStore(tmp_path / "friend_history.db")
    friend = store.create_friend("本地档案标签", aliases=[(NICKNAME, "wechat_name")])
    messages = build_messages()
    results = [{"index": 1, "speaker": "them", "time": "2026-08-21 21:05",
                "context": [], "result": make_answer(), "cached": False}]
    run_id = store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results), schema_version=SCHEMA,
        request_model="jev-latest", summary_text="摘要",
        evidence=[{"index": 1, "stance": "supporting",
                   "note": "关系信息量 2.4/4、温暖 2.0/4"}]))
    return store, friend, run_id


# ---------------------------------------------------------------------------
# 磁盘上不留隐私
# ---------------------------------------------------------------------------


def test_default_snapshot_has_no_chat_text(tmp_path):
    """默认（不保留证据）时，档案里不出现任何聊天正文。"""
    store = fh.FriendStore(tmp_path / "friend_history.db")
    friend = store.create_friend("本地档案标签", aliases=[(NICKNAME, "wechat_name")])
    messages = build_messages()
    results = [{"index": 1, "speaker": "them", "time": "2026-08-21 21:05",
                "context": [], "result": make_answer(), "cached": False}]
    run_id = store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results), schema_version=SCHEMA,
        request_model="jev-latest", summary_text="摘要"))
    blob = (tmp_path / "friend_history.db").read_bytes()
    assert SECRET_TEXT.encode("utf-8") not in blob
    assert "收到，谢谢".encode("utf-8") not in blob
    assert store.get_run(run_id) is not None


def test_nickname_lives_only_in_lookup_tables(tmp_path):
    """昵称只允许出现在查找表（friends / friend_aliases），绝不进历史表。"""
    import sqlite3
    store, friend, run_id = seed(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "friend_history.db"))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            blob = json.dumps(rows, ensure_ascii=False, default=str)
            if table in ("friends", "friend_aliases"):
                continue                      # 这里本来就存称呼（本地查找用）
            assert NICKNAME not in blob, f"{table} 里出现了昵称"
            assert SECRET_TEXT not in blob, f"{table} 里出现了聊天正文"
    finally:
        conn.close()
    assert run_id


def test_evidence_is_opt_in_masked_and_truncated(tmp_path):
    """只有用户显式选择才保留证据片段，且脱敏 + 截断。"""
    store = fh.FriendStore(tmp_path / "friend_history.db")
    friend = store.create_friend("本地档案标签", aliases=[(NICKNAME, "wechat_name")])
    messages = build_messages()
    results = [{"index": 1, "speaker": "them", "time": "2026-08-21 21:05",
                "context": [], "result": make_answer(), "cached": False}]
    note = "我的手机号 13800138000，邮箱 leak@example.com，" + "细节" * 60
    run_id = store.save_run(fh.build_run_snapshot(
        friend_id=friend.friend_id, messages=messages, results=results,
        stats=compute_conversation_stats(results), schema_version=SCHEMA,
        request_model="jev-latest", summary_text="摘要",
        evidence=[{"index": 1, "stance": "supporting", "note": note}]))
    kept = store.get_run(run_id)["evidence"]
    assert len(kept) == 1
    assert "<PHONE>" in kept[0]["note"]
    assert "<EMAIL>" in kept[0]["note"]
    assert "13800138000" not in kept[0]["note"]
    assert len(kept[0]["note"]) <= fh.EVIDENCE_MAX_CHARS + 1


def test_no_stdout_or_stderr_leak(tmp_path, capsys):
    store, _friend, _run = seed(tmp_path)
    store.find_by_alias(NICKNAME)
    store.list_runs(store.list_friends()[0].friend_id)
    store.get_run(store.list_runs(store.list_friends()[0].friend_id)[0].run_id)
    captured = capsys.readouterr()
    assert NICKNAME not in captured.out
    assert NICKNAME not in captured.err
    assert SECRET_TEXT not in captured.out
    assert SECRET_TEXT not in captured.err


def test_paths_describe_has_no_secrets():
    described = paths.describe()
    assert "friend_history_db" in described
    blob = json.dumps(described, ensure_ascii=False)
    assert "TYPESAFE_API_KEY" not in blob
    assert str(described["friend_history_db"]).endswith("friend_history.db")
    assert described["friend_history_db"] != described["cache_db"]


def test_dev_history_dir_is_gitignored():
    """开发模式的档案目录必须被 .gitignore 覆盖（含本地缓存）。"""
    root = paths.app_dir()
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    patterns = {line.strip() for line in ignored}
    dev_path = paths.friend_history_db_path()
    if paths.portable_style():
        # portable：data/ 已忽略
        assert "data/" in patterns
    else:
        assert any(p in {".friend_history/", "*.db", "*.sqlite3"} for p in patterns)
        assert dev_path.parent.name == ".friend_history"


# ---------------------------------------------------------------------------
# 出站白名单 / 不调用 Jev
# ---------------------------------------------------------------------------


def test_friend_fields_never_enter_jev_state():
    """build_state 的出站白名单保持只有 speaker/text/time(+analysis_rule)。"""
    messages = build_messages()
    target = {"speaker": "them", "text": messages[1]["text"],
              "time": messages[1]["time"]}
    state = analyzer.build_state(messages[:1], target)
    assert set(state) == {"conversation_context", "target_message",
                          "analysis_rule"}
    for row in state["conversation_context"] + [state["target_message"]]:
        assert set(row) <= {"speaker", "text", "time"}
        assert row["speaker"] in ("me", "them")
    blob = json.dumps(state, ensure_ascii=False)
    assert NICKNAME not in blob                       # 昵称不出站
    # 档案相关字段名一个都不允许出现
    for banned in ("friend_id", "friend_selected", "display_name", "alias",
                   "nickname", "remark", "raw_speaker"):
        assert banned not in blob.lower()


def test_saving_history_calls_no_jev(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("保存档案不得调用 Jev")

    monkeypatch.setattr(analyzer, "create_client", boom)
    monkeypatch.setattr(storage.Cache, "set", lambda self, *a, **k: None)
    store, _friend, run_id = seed(tmp_path)
    assert run_id
    # 读取历史同样不得调用 Jev
    store.get_run(run_id)
    store.list_runs(store.list_friends()[0].friend_id)


def test_exported_snapshot_only_has_local_fields(tmp_path):
    store, _friend, run_id = seed(tmp_path)
    full = store.get_run(run_id)
    exported = app._exportable_snapshot(full)
    # 导出的指纹是哈希，不可反推昵称或正文；消息表只有角色/时间/指纹
    assert all(re.fullmatch(r"[0-9a-f]{64}", m["fingerprint"])
               for m in exported["messages"])
    assert all(set(m) == {"fingerprint", "index", "chat_time", "speaker",
                          "is_target"} for m in exported["messages"])
    assert all(m["speaker"] in ("me", "them") for m in exported["messages"])
    assert NICKNAME not in json.dumps(exported, ensure_ascii=False)
    assert "raw_speaker" not in json.dumps(exported, ensure_ascii=False)
    assert "case_signature" in exported and "results" in exported
