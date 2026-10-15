from types import SimpleNamespace as NS
import pytest
from event_review import review_plans, review_event, OPTIONS
from storage import Cache


def messages():
    return [{"speaker": "me", "text": "今天不能去聚餐"},
            {"speaker": "them", "text": "那把文件发我吧，邮箱 demo@example.com", "raw_speaker": "原昵称"},
            {"speaker": "them", "text": "未来消息不应进入"}]


def event():
    return {"event_type": "boundary_continued_request", "review_status": "review_required",
            "identity": "private", "fingerprints": ["private"],
            "window": {"start_index": 0, "end_index": 1, "anchor_indices": [0, 1]}}


class Client:
    def __init__(self):
        self.calls = []
    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return NS(model=kwargs["model"], answers={name: NS(choice=next(iter(options)), confidence=.8)
            for name, options in OPTIONS.items()})


def test_plans_are_causal_redacted_and_deduplicated():
    plans = review_plans(messages(), {"events": [event(), event()]})
    assert len(plans) == 1
    assert plans[0]["source_indices"] == [0, 1]
    assert "未来消息" not in str(plans)
    assert "demo@example.com" not in str(plans)
    assert "原昵称" not in str(plans)
    assert review_plans(messages(), {"events": [{**event(), "review_status": "auto_supported"}]}) == []
    assert review_plans(messages(), {"events": [{**event(), "window": {"start_index": 0, "end_index": 100}}]}) == []


def test_review_is_only_assistance_and_cache_is_model_specific(tmp_path):
    client, cache = Client(), Cache(tmp_path / "cache.db")
    plan = review_plans(messages(), {"events": [event()]})[0]
    first = review_event(client, plan, model="jev-1.13.0", cache=cache)
    assert first["review_required"]
    assert review_event(client, plan, model="jev-1.13.0", cache=cache)["cached"]
    assert len(client.calls) == 1
    review_event(client, plan, model="jev-1.14.0", cache=cache)
    assert len(client.calls) == 2
    assert "private" not in str(client.calls)
    with pytest.raises(ValueError):
        review_event(client, plan, model="jev-latest")
