import analyzer
from context_builder import select_context, select_related_context
from tests.test_upgrade_provenance import ModelClient
from storage import Cache


def history():
    return [{"speaker": "me", "text": "下周要去参加面试，好紧张"}] + [
        {"speaker": "me" if i % 2 else "them", "text": f"普通近况第{i}条"}
        for i in range(20)]


def test_related_context_keeps_recent_and_recovers_explicit_anchor():
    prefix = history()
    selected = select_related_context(prefix, {"text": "你上次那个面试怎么样了"})
    assert selected[0] is prefix[0]
    assert all(any(m is old for m in selected) for old in select_context(prefix))
    assert [prefix.index(m) for m in selected] == sorted(prefix.index(m) for m in selected)


def test_no_reference_or_distinctive_anchor_means_no_retrieval():
    prefix = history()
    for text in ("面试", "那个后来怎么样了", "今天天气不错"):
        assert select_related_context(prefix, {"text": text}) == select_context(prefix)


def test_budget_and_causality():
    prefix = [{"speaker": "me", "text": "面试" + "内容" * 1000}] + history() * 3
    target = {"text": "上次的面试怎么样"}
    selected = select_related_context(prefix, target)
    recent = select_context(prefix)
    assert all(m in prefix for m in selected)
    assert len(selected) - len(recent) <= 3
    assert sum(len(m["text"]) for m in selected if not any(m is r for r in recent)) <= 1200
    assert select_related_context(prefix, target) == select_related_context(prefix, target)


def test_default_unchanged_and_mode_has_separate_cache(tmp_path):
    messages = history() + [{"speaker": "them", "text": "你上次的面试怎么样了"}]
    index = len(messages) - 1
    cache = Cache(tmp_path / "cache.db")
    first = ModelClient("jev-1.13.0")
    default = analyzer.analyze_messages(first, messages, cache=cache, only_indices={index})
    assert messages[0]["text"] not in str(default[0]["context"])
    other = ModelClient("jev-1.13.0")
    related = analyzer.analyze_messages(other, messages, cache=cache, only_indices={index}, context_mode="related")
    assert other.calls == 1
    assert messages[0]["text"] in str(related[0]["context"])
    assert related[0]["result"]["analysis_provenance"]["context_policy"] == "related-history-v1"
