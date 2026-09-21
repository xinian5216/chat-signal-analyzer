"""UI 辅助函数测试：过滤器只改变展示，不修改分析结果。

注意：导入 app 会以 bare 模式初始化 Streamlit（仅 warning，不影响断言）。
"""

from scoring import compute_conversation_stats

from app import filter_entries


def make_entry(index, evidence=2.0, conf=0.8):
    return {
        "index": index,
        "speaker": "them",
        "time": None,
        "text": f"消息{index}",
        "result": {
            "emotion": {"choice": "calm", "probabilities": {"calm": 1.0}, "confidence": 0.8},
            "intent": {"choice": "other", "probabilities": {"other": 1.0}, "confidence": 0.8},
            "warmth": {"score": 2.0, "probabilities": {}, "confidence": conf},
            "engagement": {"score": 2.0, "probabilities": {}, "confidence": conf},
            "special_attention": {"score": 1.0, "probabilities": {}, "confidence": conf},
            "relationship_evidence_strength": {"score": evidence,
                                               "probabilities": {}, "confidence": 0.8},
            "relational_ease": {"score": 2.0, "probabilities": {}, "confidence": 0.8},
            "romantic_signal": 0.1,
            "distancing_signal": 0.1,
        },
    }


def sample():
    return [make_entry(0, evidence=0.2), make_entry(1, evidence=3.0),
            make_entry(2, evidence=0.2), make_entry(3, evidence=1.5),
            make_entry(4, evidence=2.5)]


def test_filter_all_returns_everything_in_order():
    entries = sample()
    out = filter_entries(entries, only_effective=False, mode="全部消息")
    assert [e["index"] for e in out] == [0, 1, 2, 3, 4]


def test_filter_only_effective():
    entries = sample()
    out = filter_entries(entries, only_effective=True, mode="全部消息")
    assert [e["index"] for e in out] == [1, 3, 4]


def test_filter_mode_only_effective():
    entries = sample()
    out = filter_entries(entries, only_effective=False, mode="仅有效关系消息")
    assert [e["index"] for e in out] == [1, 3, 4]


def test_filter_top5():
    entries = sample()
    out = filter_entries(entries, only_effective=False, mode="关系信息量最高 Top 5")
    # 先按 evidence×conf 降序取 [1(3.0), 4(2.5), 3(1.5)]，再按原消息序号重排
    assert [e["index"] for e in out] == [1, 3, 4]


def test_filter_does_not_mutate_input():
    entries = sample()
    before = [e["index"] for e in entries]
    stats_before = compute_conversation_stats(entries)["overall"]
    filter_entries(entries, only_effective=True, mode="关系信息量最高 Top 5")
    filter_entries(entries, only_effective=True, mode="仅有效关系消息")
    assert [e["index"] for e in entries] == before
    assert compute_conversation_stats(entries)["overall"] == stats_before


def test_filter_empty_when_no_effective():
    entries = [make_entry(0, evidence=0.2), make_entry(1, evidence=0.3)]
    assert filter_entries(entries, only_effective=True, mode="全部消息") == []
