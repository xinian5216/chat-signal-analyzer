"""score_diagnostics 测试：三层捕获、paired delta、压力测试、严格失败语义。"""

import json
import socket
from pathlib import Path

import pytest

import scoring
import score_diagnostics as sd

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "evaluation" / "cases_relationship_v0.4.json"
PAIRS_PATH = REPO_ROOT / "evaluation" / "relationship_pairs_v0.4.json"
CONV_PATH = (REPO_ROOT / "evaluation" / "fixtures"
             / "relationship_conversations_v0.4_synthetic.json")

_FROZEN_WEIGHTS = {
    "WEIGHT_WARMTH": 0.30,
    "WEIGHT_ENGAGEMENT": 0.25,
    "WEIGHT_SPECIAL_ATTENTION": 0.25,
    "WEIGHT_ROMANTIC": 0.20,
    "WEIGHT_DISTANCING_PENALTY": 0.15,
    "NOUL_NOISE_FLOOR": 0.30,
    "NOUL_STRONG_MARK": 0.70,
    "NOUL_MID_GAIN": 0.35,
}


def _dist(score: float) -> dict:
    lo, hi = int(score // 1), int(-(-score // 1))
    out = {str(i): 0.0 for i in range(5)}
    if lo == hi:
        out[str(lo)] = 1.0
    else:
        frac = score - lo
        out[str(lo)] = 1.0 - frac
        out[str(hi)] = frac
    return out


def _result(warmth=2.0, engagement=2.0, special=1.0, evidence=2.0, ease=2.0,
            romantic=0.2, distancing=0.1, conf=0.8, model="synthetic-test"):
    return {
        "emotion": {"choice": "calm", "probabilities": {"calm": 1.0},
                    "confidence": conf},
        "intent": {"choice": "continue_topic",
                   "probabilities": {"continue_topic": 1.0}, "confidence": conf},
        "warmth": {"score": warmth, "probabilities": _dist(warmth),
                   "confidence": conf},
        "engagement": {"score": engagement, "probabilities": _dist(engagement),
                       "confidence": conf},
        "special_attention": {"score": special, "probabilities": _dist(special),
                              "confidence": conf},
        "relationship_evidence_strength": {
            "score": evidence, "probabilities": _dist(evidence), "confidence": conf},
        "relational_ease": {"score": ease, "probabilities": _dist(ease),
                            "confidence": conf},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
        "model": model,
    }


def _write_conv(path: Path, cases: dict) -> None:
    payload = {
        "meta": {"provenance": "synthetic", "disclaimer": "test", "model_label": "x"},
        "cases": cases,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 三层捕获完整性
# ---------------------------------------------------------------------------


def test_layer_a_captures_probabilities_and_confidence():
    result = _result()
    a = sd.layer_a(result)
    for dim in sd.CHOICE_DIMS:
        assert set(a["choice"][dim]) == {"choice", "probabilities", "confidence"}
    for dim in sd.SCORE_DIMS:
        assert set(a["scores"][dim]) == {"score", "probabilities", "confidence"}
        assert a["scores"][dim]["probabilities"]
    assert a["noul"] == {"romantic_signal": 0.2, "distancing_signal": 0.1}


def test_layer_b_matches_production_message_metrics():
    result = _result(warmth=3.0, engagement=3.4, special=2.2, evidence=3.0,
                     ease=2.6, romantic=0.5, distancing=0.05, conf=0.75)
    b = sd.layer_b(result)
    m = scoring.message_metrics({"index": 0, "result": result})
    assert b["warmth"] == m["warmth"]
    assert b["engagement"] == m["engagement"]
    assert b["special_attention"] == m["special_attention"]
    assert b["relationship_evidence_strength"] == m["evidence"]
    assert b["relational_ease"] == m["relational_ease"]
    assert b["relation_confidence"] == m["relation_confidence"]
    assert b["evidence_norm"] == m["evidence_norm"]
    assert b["romantic_raw"] == m["romantic_raw"]
    assert b["romantic_ev"] == m["romantic_ev"]
    assert b["distancing_raw"] == m["distancing_raw"]
    assert b["distancing_ev"] == m["distancing_ev"]
    assert b["message_weight"] == m["weight"]
    assert b["base_score"] == m["base_score"]
    for key in ("warmth", "engagement", "special_attention",
                "relationship_evidence_strength", "relational_ease",
                "relation_confidence", "evidence_norm", "romantic_raw",
                "romantic_ev", "distancing_raw", "distancing_ev",
                "message_weight", "base_score"):
        assert key in b


def test_layer_c_matches_production_compute_conversation_stats():
    results = [_result(warmth=2.0 + i * 0.3, evidence=2.0 + i * 0.2)
               for i in range(3)]
    c = sd.layer_c(results)
    stats = scoring.compute_conversation_stats(
        [{"index": i, "result": r} for i, r in enumerate(results)])
    for key in ("overall", "recent", "first_half", "second_half", "trend",
                "effective_messages", "total_weight", "warmth_avg",
                "engagement_avg", "special_attention_avg", "relational_ease_avg",
                "romantic_evidence", "distancing_evidence"):
        assert c[key] == stats[key], key
    assert c["intent_profiles"] == stats["intent_profiles"]


# ---------------------------------------------------------------------------
# paired delta 计算正确性（手算对照）
# ---------------------------------------------------------------------------


def test_pair_delta_matches_hand_computed_values(tmp_path):
    # 手算（生产公式）：
    #   A: evidence_norm=2/4=0.5, conf=0.8 → weight=0.4
    #      romantic_ev(0.2)=0, base=0.3*0.5+0.25*0.5+0.25*0.25=0.3375
    #   B: evidence_norm=3/4=0.75 → weight=0.6
    #      romantic_ev(0.5)=0.175, base=0.3*0.75+0.25*0.75+0.25*0.5+0.2*0.175=0.5725
    a_result = _result(warmth=2.0, engagement=2.0, special=1.0, evidence=2.0,
                       ease=2.0, romantic=0.2, distancing=0.1)
    b_result = _result(warmth=3.0, engagement=3.0, special=2.0, evidence=3.0,
                       ease=2.5, romantic=0.5, distancing=0.1)
    conv_path = tmp_path / "conv.json"
    _write_conv(conv_path, {
        "pair_a": {"target_index": 0, "results": [a_result, a_result]},
        "pair_b": {"target_index": 0, "results": [b_result, b_result]},
    })
    conv = sd.load_conversation_fixture(conv_path)
    ra = sd.analyze_case("pair_a", conv["cases"]["pair_a"])
    rb = sd.analyze_case("pair_b", conv["cases"]["pair_b"])
    meta = {"pair_id": "P", "family": "care", "issue_bullet": "x", "factor": "y",
            "baseline": "pair_a", "contrast": "pair_b"}
    d = sd.pair_delta(ra, rb, meta)

    assert d["raw_score_delta"] == {
        "warmth": pytest.approx(1.0), "engagement": pytest.approx(1.0),
        "special_attention": pytest.approx(1.0),
        "relationship_evidence_strength": pytest.approx(1.0),
        "relational_ease": pytest.approx(0.5)}
    assert d["raw_noul_delta"]["romantic_signal"] == pytest.approx(0.3)
    assert d["raw_noul_delta"]["distancing_signal"] == pytest.approx(0.0)
    assert d["transformed_noul_delta"]["romantic_ev"] == pytest.approx(0.175)
    assert d["transformed_noul_delta"]["distancing_ev"] == pytest.approx(0.0)
    assert d["weight_delta"] == pytest.approx(0.2)
    assert d["base_score_delta"] == pytest.approx(0.235)
    assert d["base_score_delta_100"] == pytest.approx(23.5)
    # 单条消息会话：overall = base*100；recent == overall（消息数 <= RECENT_WINDOW）
    assert d["overall_delta"] == pytest.approx(23.5)
    assert d["recent_delta"] == pytest.approx(23.5)
    assert d["total_weight_delta"] == pytest.approx(0.4)
    assert d["delta_trace"]["raw_score_abs_max_100"] == pytest.approx(25.0)
    assert d["delta_trace"]["base_score_abs_100"] == pytest.approx(23.5)
    assert d["delta_trace"]["overall_abs_100"] == pytest.approx(23.5)


def test_pair_delta_rejects_mismatched_pair_meta(tmp_path):
    conv_path = tmp_path / "conv.json"
    r = _result()
    _write_conv(conv_path, {
        "x": {"target_index": 0, "results": [r]},
        "y": {"target_index": 0, "results": [r]},
    })
    conv = sd.load_conversation_fixture(conv_path)
    ra = sd.analyze_case("x", conv["cases"]["x"])
    rb = sd.analyze_case("y", conv["cases"]["y"])
    with pytest.raises(sd.DiagnosticError, match="baseline"):
        sd.pair_delta(ra, rb, {"pair_id": "P", "baseline": "not_x",
                               "contrast": "y"})


def test_report_pairs_match_pair_file_end_to_end():
    report = sd.build_report(CASES_PATH, CONV_PATH, PAIRS_PATH)
    pairs_doc = sd.load_pairs(
        PAIRS_PATH, set(json.loads(CONV_PATH.read_text(encoding="utf-8"))["cases"]))
    assert [p["pair_id"] for p in report["pairs"]] == \
        [p["pair_id"] for p in pairs_doc["pairs"]]
    for d in report["pairs"]:
        for key in ("raw_score_delta", "raw_noul_delta",
                    "transformed_noul_delta", "weight_delta", "base_score_delta",
                    "overall_delta", "recent_delta", "total_weight_delta"):
            assert key in d, d["pair_id"]
        assert d["overall_delta"] is not None, d["pair_id"]


# ---------------------------------------------------------------------------
# Noul transform 曲线与生产函数一致
# ---------------------------------------------------------------------------


def test_noul_curve_matches_production_transform():
    curve = sd.stress_noul_transform_curve(step=0.01)
    for row in curve["curve"]:
        assert row["evidence"] == pytest.approx(
            scoring.transform_noul_evidence(row["p"]))
    # 边界点抽样
    by_p = {row["p"]: row["evidence"] for row in curve["curve"]}
    assert by_p[0.0] == 0.0
    assert by_p[0.3] == 0.0
    assert by_p[0.7] == pytest.approx(0.35)
    assert by_p[1.0] == pytest.approx(1.0)


def test_noul_curve_reports_floor_and_gains():
    curve = sd.stress_noul_transform_curve()
    segs = curve["segments"]
    assert segs["noise_floor"]["raw_interval"] == [0.0, scoring.NOUL_NOISE_FLOOR]
    assert segs["noise_floor"]["slope"] == 0.0
    assert segs["mid_gain"]["slope"] == pytest.approx(0.875)
    assert segs["strong_mark"]["slope"] == pytest.approx(0.65 / 0.3)
    facts = curve["compression_facts"]
    assert facts["zero_zone_raw_width"] == pytest.approx(0.3)
    assert facts["strong_vs_mid_slope_ratio"] == pytest.approx(2.476190476, abs=1e-6)


# ---------------------------------------------------------------------------
# 压力测试可重复 / 确定性
# ---------------------------------------------------------------------------


def test_dilution_stress_is_reproducible_and_monotonic():
    a = sd.stress_weighted_mean_dilution()
    b = sd.stress_weighted_mean_dilution()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    for kind in ("low", "mid"):
        rows = [r for r in a["rows"] if r["filler_kind"] == kind]
        overalls = [r["mixed_overall"] for r in rows]
        # 混入的普通消息越多，overall 越靠近填充消息的水平（单调稀释）
        assert overalls == sorted(overalls, reverse=True)
        assert rows[-1]["mixed_overall"] < rows[0]["mixed_overall"]
        assert all(r["salient_only_overall"] == a["salient_only"]["overall"]
                   for r in rows)


def test_weight_sensitivity_grid_matches_formula():
    stress = sd.stress_message_weight_sensitivity()
    for row in stress["rows"]:
        expected = min(1.0, row["evidence_norm"] * row["relation_confidence"])
        assert row["message_weight"] == pytest.approx(expected)
        assert row["evidence_norm"] == pytest.approx(
            row["relationship_evidence_strength"] / scoring.SCORE_MAX)


def test_same_score_distributions_hide_variance():
    stress = sd.stress_same_score_distributions()
    rows = stress["rows"]
    assert len({r["score"] for r in rows}) == 1
    assert len({r["base_score_x100"] for r in rows}) == 1
    assert len({r["message_weight"] for r in rows}) == 1
    variances = [r["variance"] for r in rows]
    assert variances[0] == 0.0 and variances[-1] > variances[1] > variances[0]


# ---------------------------------------------------------------------------
# 严格失败语义（malformed fixture / 缺字段）
# ---------------------------------------------------------------------------


def _minimal_conv(result):
    return {"cases": {"c": {"target_index": 0, "results": [result]}},
            "meta": {"provenance": "synthetic"}}


def test_fixture_with_wrong_provenance_is_rejected(tmp_path):
    payload = _minimal_conv(_result())
    payload["meta"]["provenance"] = "real"
    path = tmp_path / "f.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match="provenance"):
        sd.load_conversation_fixture(path)


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.pop("romantic_signal"), "romantic_signal"),
    (lambda r: r["warmth"].pop("probabilities"), "probabilities"),
    (lambda r: r["warmth"].pop("confidence"), "confidence"),
    (lambda r: r["emotion"].pop("choice"), "emotion"),
    (lambda r: r.__setitem__("intent", {"choice": "nope",
                                        "probabilities": {"nope": 1.0},
                                        "confidence": 0.5}), "choice"),
    (lambda r: r["special_attention"].__setitem__("confidence", 1.5), "confidence"),
    (lambda r: r["relationship_evidence_strength"].__setitem__("score", 9.0),
     "score"),
])
def test_fixture_missing_or_bad_field_fails_explicitly(tmp_path, mutate, match):
    result = _result()
    mutate(result)
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_minimal_conv(result)), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match=match):
        sd.load_conversation_fixture(path)


def test_fixture_score_must_equal_distribution_expectation(tmp_path):
    result = _result()
    result["warmth"]["score"] = 3.5  # 分布期望仍是 2.0
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_minimal_conv(result)), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match="期望值"):
        sd.load_conversation_fixture(path)


def test_fixture_non_synthetic_model_is_rejected(tmp_path):
    result = _result(model="jev-1.13.0")
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_minimal_conv(result)), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match="synthetic"):
        sd.load_conversation_fixture(path)


def test_fixture_target_index_out_of_range_fails(tmp_path):
    payload = _minimal_conv(_result())
    payload["cases"]["c"]["target_index"] = 3
    path = tmp_path / "f.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match="target_index"):
        sd.load_conversation_fixture(path)


def test_pairs_file_missing_reference_fails(tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({
        "meta": {}, "families": {"care": {}},
        "pairs": [{"pair_id": "P", "family": "care", "baseline": "missing_a",
                   "contrast": "missing_b", "factor": "f", "issue_bullet": "i",
                   "expected_distinguishers": ["x"]}],
        "singletons": [],
        "coverage_map": {},
    }), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match="不存在的 case id"):
        sd.load_pairs(path, {"other"})


# ---------------------------------------------------------------------------
# 确定性 / 不修改生产常量 / 不触网
# ---------------------------------------------------------------------------


def test_build_report_is_deterministic():
    a = sd.build_report(CASES_PATH, CONV_PATH, PAIRS_PATH)
    b = sd.build_report(CASES_PATH, CONV_PATH, PAIRS_PATH)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_diagnostics_do_not_modify_production_constants():
    before = {name: getattr(scoring, name) for name in _FROZEN_WEIGHTS}
    frozen = sd.build_report(CASES_PATH, CONV_PATH, PAIRS_PATH)
    sd.render_markdown(frozen)
    sd.stress_weighted_mean_dilution()
    sd.stress_message_weight_sensitivity()
    sd.stress_noul_transform_curve()
    sd.stress_same_score_distributions()
    after = {name: getattr(scoring, name) for name in _FROZEN_WEIGHTS}
    assert before == after
    # 冻结锚点：与 tests/test_scoring.py 的权重锚一致
    assert after == _FROZEN_WEIGHTS
    assert frozen["meta"]["production_constants"] == sd.PRODUCTION_CONSTANTS


def test_diagnostics_never_touch_network_or_client(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("诊断不允许网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    import analyzer
    monkeypatch.setattr(analyzer, "create_client",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("诊断不允许创建客户端")))
    report = sd.build_report(CASES_PATH, CONV_PATH, PAIRS_PATH)
    sd.render_markdown(report)


# ---------------------------------------------------------------------------
# 真实 raw 加载（既存冻结数据；合成数据不得混入）
# ---------------------------------------------------------------------------


def _real_result():
    result = _result(model="jev-1.13.0")
    # 真实 raw 的概率只序列化到 2 位小数：构造一个合法的舍入偏差
    result["warmth"] = {"score": 2.02,
                        "probabilities": {"1": 0.0, "2": 0.99, "3": 0.01},
                        "confidence": 0.7}
    return result


def test_real_raw_loader_accepts_frozen_real_output(tmp_path):
    path = tmp_path / "raw.json"
    path.write_text(json.dumps({
        "meta": {"schema_version": "chat-signal-v3.2", "model": "jev-latest"},
        "cases": [],
        "results": {"case_a": _real_result()},
    }), encoding="utf-8")
    loaded = sd.load_real_raw(path)
    assert loaded["sha256"]
    section = sd.real_raw_section([loaded], [
        {"pair_id": "P", "issue_bullet": "b", "factor": "f",
         "baseline": "case_a", "contrast": "case_missing"}])
    assert section["available"] is True
    assert section["frozen_pairs"][0]["available"] is False
    dev = section["score_expectation_serialization_deviation"]
    assert dev["warmth"]["max"] == pytest.approx(0.01, abs=1e-6)


def test_real_raw_loader_rejects_synthetic_payload(tmp_path):
    payload = {"results": {"case_a": _result(model="synthetic-issue17-fixture")}}
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(sd.DiagnosticError, match="synthetic"):
        sd.load_real_raw(path)


def test_real_raw_loader_fails_clearly_on_missing_file(tmp_path):
    with pytest.raises(sd.DiagnosticError, match="不存在"):
        sd.load_real_raw(tmp_path / "nope.json")


def test_real_raw_section_without_data_states_no_live_api_limitation():
    section = sd.real_raw_section([], [])
    assert section["available"] is False
    assert "no-live-API" in section["note"]


# ---------------------------------------------------------------------------
# 报告内容卫生：不含聊天正文
# ---------------------------------------------------------------------------


def test_markdown_report_contains_no_chat_text():
    import evaluation as ev

    report = sd.build_report(CASES_PATH, CONV_PATH, PAIRS_PATH)
    md = sd.render_markdown(report)
    for case in ev.load_cases(CASES_PATH):
        assert case["target"] not in md, case["id"]
        assert case["chat"] not in md, case["id"]
    # 只应出现 case id
    assert "rb_init_reply" in md
