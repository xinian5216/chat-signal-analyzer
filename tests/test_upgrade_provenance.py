"""Regression coverage for alias drift, mixed caches and immutable snapshots."""
import copy
from types import SimpleNamespace

import analyzer
import friend_history as fh
import longitudinal as lg
import provenance as ap
from storage import Cache, make_cache_key
from tests.test_analyzer import FakeClient, make_fake_response, sample_messages
from tests.test_friend_history import msg, result
from scoring import compute_conversation_stats


class ModelClient(FakeClient):
    def __init__(self, model):
        super().__init__()
        self.model_id = model

    def system_one(self, state, questions, **kwargs):
        self.calls += 1
        response = make_fake_response()
        response.model = self.model_id
        return response


def test_default_is_pinned():
    assert analyzer.DEFAULT_MODEL == ap.PINNED_BASELINE_MODEL


def test_alias_never_reuses_or_writes_persistent_cache(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    old = ModelClient("jev-1.13.0")
    new = ModelClient("jev-1.14.0")
    analyzer.analyze_messages(old, sample_messages(), cache=cache, model="jev-latest")
    results = analyzer.analyze_messages(new, sample_messages(), cache=cache, model="jev-latest")
    assert old.calls == new.calls == 2
    assert not any(r["cached"] for r in results)
    assert {r["result"]["model"] for r in results} == {"jev-1.14.0"}


def test_pinned_versions_have_independent_caches(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    for model in ("jev-1.13.0", "jev-1.14.0"):
        first, repeat = ModelClient(model), ModelClient(model)
        analyzer.analyze_messages(first, sample_messages(), cache=cache, model=model)
        results = analyzer.analyze_messages(repeat, sample_messages(), cache=cache, model=model)
        assert first.calls == 2 and repeat.calls == 0
        assert all(e["result"]["model"] == model for e in results)


def test_instructions_criteria_and_choice_order_are_fingerprinted(monkeypatch):
    original = analyzer.build_questions_schema()
    monkeypatch.setattr(analyzer, "ROMANTIC_QUESTION", analyzer.ROMANTIC_QUESTION + " test")
    changed = analyzer.build_questions_schema()
    assert ap.digest(original) != ap.digest(changed)
    questions = analyzer.build_questions()
    questions["emotion"].criteria = dict(reversed(list(questions["emotion"].criteria.items())))
    assert ap.digest(changed) != ap.digest(analyzer.build_questions_schema(questions))


def test_unknown_models_not_cached_and_mismatches_fail(tmp_path):
    cache = Cache(tmp_path / "cache.db")
    for _ in range(2):
        missing = ModelClient(None)
        analyzer.analyze_messages(missing, sample_messages(), cache=cache)
        assert missing.calls == 2
    wrong = analyzer.analyze_messages(ModelClient("jev-1.14.0"), sample_messages(), cache=cache)
    assert all(e.get("error") for e in wrong)


def test_changed_question_cannot_reuse_old_cache(tmp_path, monkeypatch):
    cache = Cache(tmp_path / "cache.db")
    analyzer.analyze_messages(ModelClient("jev-1.13.0"), sample_messages(), cache=cache)
    monkeypatch.setattr(analyzer, "ROMANTIC_QUESTION", "Changed instructions only")
    client = ModelClient("jev-1.13.0")
    analyzer.analyze_messages(client, sample_messages(), cache=cache)
    assert client.calls == 2


def test_mixed_run_does_not_claim_first_model_and_is_not_comparable(tmp_path):
    store = fh.FriendStore(tmp_path / "history.db")
    friend = store.create_friend("测试档案")
    messages = [msg(0, "them", "2026-08-01 09:00", "第一条"),
                msg(1, "them", "2026-08-01 09:01", "第二条")]
    entries = [result(0, messages[0]["time"], model="jev-1.13.0"),
               result(1, messages[1]["time"], model="jev-1.14.0")]
    snapshot = fh.build_run_snapshot(friend_id=friend.friend_id, messages=messages,
        results=entries, stats=compute_conversation_stats(entries),
        schema_version=analyzer.SCHEMA_VERSION, request_model="jev-latest")
    assert snapshot["response_model"] == "mixed"
    run_id = store.save_run(snapshot)
    run = store.get_run(run_id)
    assert run["analysis_provenance"]["actual_models"] == {"jev-1.13.0": 1, "jev-1.14.0": 1}
    assert lg.eligibility(store.list_runs(friend.friend_id))[0].status == "reevaluate"
    # Simulate an old immutable snapshot that falsely recorded only its first model.
    with store._connect() as connection:
        connection.execute("UPDATE history_runs SET response_model=?, stats_json=? WHERE run_id=?",
                           ("jev-1.13.0", "{}", run_id))
    assert lg.eligibility(store.list_runs(friend.friend_id))[0].status == "reevaluate"
    with store._connect() as connection:
        assert connection.execute("SELECT stats_json FROM history_runs WHERE run_id=?", (run_id,)).fetchone()[0] == "{}"


def test_same_model_different_context_conditions_are_mixed():
    one = {"model": "jev-1.13.0", "analysis_provenance": {"context_policy": "recent"}}
    two = {"model": "jev-1.13.0", "analysis_provenance": {"context_policy": "related"}}
    info = ap.summarize([{"result": one}, {"result": two}])
    assert info["mixed"] and not info["comparable"]


def test_known_recipe_and_unrecorded_legacy_conditions_cannot_mix(tmp_path):
    from dataclasses import replace
    store = fh.FriendStore(tmp_path / "history.db")
    friend = store.create_friend("测试")
    messages = [msg(0, "them", "2026-08-01 09:00", "第一条")]
    entries = [result(0, messages[0]["time"])]
    snapshot = fh.build_run_snapshot(friend_id=friend.friend_id, messages=messages,
        results=entries, stats=compute_conversation_stats(entries),
        schema_version=analyzer.SCHEMA_VERSION, request_model="jev-1.13.0")
    store.save_run(snapshot)
    old = store.list_runs(friend.friend_id)[0]
    known = {**entries[0]["result"], "analysis_provenance": {
        "context_policy": "related-history-v1", "schema_version": analyzer.SCHEMA_VERSION}}
    new_info = ap.summarize([{"result": known}])
    modern = replace(old, run_id="new-run", chat_first_time="2026-08-02 09:00",
                     chat_last_time="2026-08-02 09:00", analysis_provenance=new_info)
    assert all(item.status == "reevaluate" for item in lg.eligibility([old, modern]))
    batch = ap.summarize([entries[0], {"result": known}])
    assert batch["mixed"] and not batch["comparable"]
