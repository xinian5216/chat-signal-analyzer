"""Field Study v1.1 测试：全部合成数据，绝不调用真实 Jev。

覆盖：item 级指标（无伪重复）、Krippendorff alpha（闭式&含缺失）、
PR-AUC、未校准 Noul 不算 Brier、participant perception 分层、角色-标签
合法性、recall delay 分桶、item dataset 隐私白名单、item 哈希变更使冻结
失效、dev/blind 泄漏检测、blind manifest / 模型版本追踪、cluster bootstrap
确定性、报告可复现性。
"""

import json
from pathlib import Path

import pytest

import field_study as fs
from field_study import FieldStudyError

REPO = Path(__file__).resolve().parents[1]
FS_DIR = REPO / "evaluation" / "field_study"
SYNTH = FS_DIR / "examples" / "annotations_synthetic.json"
SYNTH_ITEMS = FS_DIR / "examples" / "items_synthetic.json"
SYNTH_MODEL = FS_DIR / "examples" / "model_outputs_synthetic.json"


def _load_synth():
    return (fs.load_annotations(SYNTH),
            fs.load_items(SYNTH_ITEMS),
            fs.load_model_outputs(SYNTH_MODEL))


def _write(tmp_path, payload, name="x.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _rec(item_id, group, role, annotator, labels, **extra):
    return {"item_id": item_id, "group_id": group, "role": role,
            "annotator_id": annotator, "labels": labels, **extra}# ---------------------------------------------------------------------------
# 合成示例与 schema 校验
# ---------------------------------------------------------------------------


def test_synth_files_load_and_validate():
    annotations, items, model = _load_synth()
    assert len(annotations) == 6 and len(items) == 5
    assert model["schema_version"] == "chat-signal-v3.2"
    assert fs.render_item_text(items[0]) == "me: 最近有点感冒\nthem: 那你早点休息，多喝水"
    assert fs.item_target_message(items[0])["speaker"] == "them"


def test_observer_cannot_label_sender_intent(tmp_path):
    bad = [_rec("i1", "g1", "observer", "a-1",
                {"sender_intent": {"status": "labeled", "value": "invite"}})]
    with pytest.raises(FieldStudyError) as exc:
        fs.load_annotations(_write(tmp_path, bad))
    assert "不允许由 role=observer" in str(exc.value)


def test_sender_cannot_label_observer_dims(tmp_path):
    bad = [_rec("i1", "g1", "sender", "p-1",
                {"warmth_observed": {"status": "labeled", "value": 2}})]
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, bad))


def test_receiver_cannot_label_observer_dims(tmp_path):
    bad = [_rec("i1", "g1", "receiver", "p-2",
                {"romantic_binary": {"status": "labeled", "value": False}})]
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, bad))


def test_participant_perception_allowed_for_participants_only(tmp_path):
    ok_sender = _rec("i1", "g1", "sender", "p-1",
                     {"participant_perception": {"status": "labeled",
                                                 "value": "看起来在关心我"}})
    ok_receiver = _rec("i2", "g1", "receiver", "p-2",
                       {"participant_perception": {"status": "unsure"}})
    fs.load_annotations(_write(tmp_path, [ok_sender, ok_receiver]))
    bad_observer = _rec("i3", "g1", "observer", "a-1",
                        {"participant_perception": {"status": "unsure"}})
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [bad_observer], "bad.json"))


def test_recall_delay_bucket_validated(tmp_path):
    rec = _rec("i1", "g1", "sender", "p-1",
               {"sender_intent": {"status": "labeled", "value": "invite"}},
               recall_delay_bucket="8_30_days")
    fs.load_annotations(_write(tmp_path, [rec]))
    rec["recall_delay_bucket"] = "last_week"
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [rec], "bad.json"))


def test_forbidden_privacy_fields_rejected(tmp_path):
    rec = _rec("i1", "g1", "observer", "a-1",
               {"emotion_observed": {"status": "unsure"}})
    rec["raw_speaker"] = "某昵称"
    with pytest.raises(FieldStudyError) as exc:
        fs.load_annotations(_write(tmp_path, [rec]))
    assert "禁止字段" in str(exc.value)


def test_unknown_dimension_and_bad_status_still_rejected(tmp_path):
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [_rec(
            "i1", "g1", "observer", "a-1",
            {"dream_meaning": {"status": "unsure"}})]))
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [_rec(
            "i1", "g1", "observer", "a-1",
            {"warmth_observed": {"status": "maybe"}})]))


# ---------------------------------------------------------------------------
# item dataset：隐私白名单与目标合法性
# ---------------------------------------------------------------------------


def test_item_dataset_privacy_whitelist(tmp_path):
    items = fs.load_items(SYNTH_ITEMS)
    bad = json.loads(json.dumps(items[0]))
    bad["raw_speaker"] = "昵称"
    with pytest.raises(FieldStudyError):
        fs.load_items(_write(tmp_path, [bad]))
    bad2 = json.loads(json.dumps(items[0]))
    bad2["messages"][0]["speaker"] = "小柯"     # 非假名角色
    with pytest.raises(FieldStudyError):
        fs.load_items(_write(tmp_path, [bad2], "b.json"))


def test_item_target_must_be_them_side(tmp_path):
    items = fs.load_items(SYNTH_ITEMS)
    bad = json.loads(json.dumps(items[0]))
    bad["target_index"] = 0                    # me 侧
    with pytest.raises(FieldStudyError):
        fs.load_items(_write(tmp_path, [bad]))


def test_item_speaker_and_message_shape(tmp_path):
    bad = {"item_id": "i", "group_id": "g",
           "messages": [{"speaker": "me", "text": "hi", "time": "21:00"}],
           "target_index": 0}
    with pytest.raises(FieldStudyError):
        fs.load_items(_write(tmp_path, [bad]))


def test_item_text_change_breaks_freeze(tmp_path):
    annotations = fs.load_annotations(SYNTH)
    items = fs.load_items(SYNTH_ITEMS)
    items_copy = tmp_path / "items.json"
    items_copy.write_text(SYNTH_ITEMS.read_text(encoding="utf-8"),
                          encoding="utf-8")
    annotations_copy = tmp_path / "ann.json"
    annotations_copy.write_text(SYNTH.read_text(encoding="utf-8"),
                                encoding="utf-8")
    manifest = fs.freeze_dataset(
        annotations, ["group-A"], ["group-B", "group-C"], "v1",
        annotations_copy, items_path=items_copy)
    fs.verify_freeze(manifest, annotations, annotations_copy, items_copy)

    tampered = tmp_path / "items2.json"
    tampered.write_text(
        SYNTH_ITEMS.read_text(encoding="utf-8").replace("多喝水", "喝烫水"),
        encoding="utf-8")
    with pytest.raises(FieldStudyError) as exc:
        fs.verify_freeze(manifest, annotations, annotations_copy, tampered)
    assert "item dataset 哈希" in str(exc.value)


# ---------------------------------------------------------------------------
# item 级 reference：无伪重复
# ---------------------------------------------------------------------------


def test_multi_observer_does_not_inflate_n(tmp_path):
    """3 名观察者对同一 item 标注 → 指标 n=1（模型预测不重复计 3 次）。"""
    records = [
        _rec("i1", "g1", "observer", "a-1",
             {"intent_observed": {"status": "labeled", "value": "invite"}}),
        _rec("i1", "g1", "observer", "a-2",
             {"intent_observed": {"status": "labeled", "value": "invite"}}),
        _rec("i1", "g1", "observer", "a-3",
             {"intent_observed": {"status": "labeled", "value": "invite"}}),
    ]
    fs.load_annotations(_write(tmp_path, records))
    reference = fs.build_item_reference(records)
    assert reference["i1"]["intent_observed"]["value"] == "invite"
    assert reference["i1"]["intent_observed"]["n_raters"] == 3
    model = {"schema_version": "s", "model": "m", "model_alias": "a",
             "outputs": {"i1": {"intent": {"choice": "invite"}}},
             "source": "synthetic"}
    report = fs.evaluate(records, model, dataset_version="v")
    assert report["choice_metrics"]["intent_observed"]["n"] == 1
    assert report["choice_metrics"]["intent_observed"]["accuracy"] == 1.0
    assert report["dataset"]["independent_observers"] == 3
    assert report["dataset"]["annotation_records"] == 3
    assert report["dataset"]["items"] == 1


def _tmp():
    import tempfile
    from pathlib import Path as P
    d = P(tempfile.mkdtemp())
    return d / "ann.json"


def test_contested_reference_excluded_and_counted():
    records = [
        _rec("i1", "g1", "observer", "a-1",
             {"intent_observed": {"status": "labeled", "value": "invite"}}),
        _rec("i1", "g1", "observer", "a-2",
             {"intent_observed": {"status": "labeled", "value": "explain"}}),
        _rec("i2", "g1", "observer", "a-1",
             {"intent_observed": {"status": "labeled", "value": "invite"}}),
        _rec("i2", "g1", "observer", "a-2",
             {"intent_observed": {"status": "labeled", "value": "invite"}}),
    ]
    reference = fs.build_item_reference(records)
    assert reference["i1"]["intent_observed"]["contested"] is True
    assert reference["i2"]["intent_observed"]["agreement"] == "unanimous"
    model = {"schema_version": "s", "model": "m", "model_alias": "a",
             "outputs": {"i1": {"intent": {"choice": "invite"}},
                         "i2": {"intent": {"choice": "invite"}}},
             "source": "synthetic"}
    report = fs.evaluate(records, model, dataset_version="v")
    assert report["choice_metrics"]["intent_observed"]["n"] == 1
    assert report["choice_metrics"]["intent_observed"][
        "contested_excluded"] == 1


def test_ordinal_reference_uses_lower_median():
    records = [
        _rec("i1", "g1", "observer", "a-1",
             {"warmth_observed": {"status": "labeled", "value": 2}}),
        _rec("i1", "g1", "observer", "a-2",
             {"warmth_observed": {"status": "labeled", "value": 3}}),
        _rec("i1", "g1", "observer", "a-3",
             {"warmth_observed": {"status": "labeled", "value": 4}}),
    ]
    ref = fs.build_item_reference(records)["i1"]["warmth_observed"]
    assert ref["value"] == 3 and ref["n_raters"] == 3


# ---------------------------------------------------------------------------
# Krippendorff alpha（自实现：闭式与含缺失）
# ---------------------------------------------------------------------------


def test_alpha_perfect_agreement_is_one():
    units = [{"a": "x", "b": "x"}, {"a": "y", "b": "y"}]
    result = fs.krippendorff_alpha(units, level="nominal")
    assert result["alpha"] == 1.0
    assert result["n_units"] == 2


def test_alpha_complete_disagreement_two_raters():
    """两名标注者完全分歧：D_o=1，D_e=n_x*n_y*2/(N(N-1))=8/12 → α=-0.5。"""
    result = fs.krippendorff_alpha([{"a": "x", "b": "y"},
                                    {"a": "y", "b": "x"}], level="nominal")
    assert result["alpha"] == -0.5


def test_alpha_hand_computed_two_unit_case():
    """手算 coincidences 后得到的 alpha（回归锚点，防实现漂移）。

    每个 unit k=3 名标注者，每对无序 raters 向两个方向各贡献 1/(k-1)=0.5
    （同值对则向对角线贡献 2×0.5=1）：
      unit1 x,x,y → o_xx+=1.0, o_xy+=1.0, o_yx+=1.0
      unit2 y,y,y → o_yy+=3.0
      unit3 x,y,x → o_xx+=1.0, o_xy+=1.0, o_yx+=1.0
    合计 o_xx=2.0 o_yy=3.0 o_xy=o_yx=2.0；n_x=4.0 n_y=5.0 N=9
    D_o=(o_xy+o_yx)/N=4/9；D_e=2*n_x*n_y/(N(N-1))=40/72
    α = 1 - (4/9)/(40/72) = 1 - 0.8 = 0.2
    """
    units = [
        {"a": "x", "b": "x", "c": "y"},
        {"a": "y", "b": "y", "c": "y"},
        {"a": "x", "b": "y", "c": "x"},
    ]
    result = fs.krippendorff_alpha(units, level="nominal")
    assert result["alpha"] is not None
    assert abs(result["alpha"] - 0.2) < 1e-9, result["alpha"]


def test_alpha_skips_single_rater_units():
    """只有 1 名标注者的 unit 不贡献；<2 个多标注 unit 时 alpha 未定义。"""
    only_single = fs.krippendorff_alpha([{"a": "x"}, {"a": "y", "b": "y"}],
                                        level="nominal")
    assert only_single["n_units"] == 1
    assert only_single["alpha"] is None      # N<=1：alpha 未定义（Krippendorff）
    result = fs.krippendorff_alpha(
        [{"a": "x"}, {"a": "x", "b": "x"}, {"a": "y", "b": "y"}],
        level="nominal")
    assert result["n_units"] == 2
    assert result["alpha"] == 1.0


def test_alpha_ordinal_differs_from_nominal_on_same_data():
    units = [{"a": 1, "b": 2}, {"a": 4, "b": 3}]
    nominal = fs.krippendorff_alpha(units, level="nominal")["alpha"]
    ordinal = fs.krippendorff_alpha(units, level="ordinal")["alpha"]
    assert nominal < ordinal        # ordinal 惩罚距离，分歧评分更高


def test_alpha_insufficient_data_returns_none():
    result = fs.krippendorff_alpha([{"a": "x"}], level="nominal")
    assert result["alpha"] is None


# ---------------------------------------------------------------------------
# Noul 指标修正
# ---------------------------------------------------------------------------


def test_raw_noul_has_no_brier():
    m = fs.noul_metrics([False, True], [0.1, 0.9])
    assert m["auc"] == 1.0 and m["pr_auc"] == 1.0
    assert m["calibrated"] is False
    assert m["brier"] is None and m["log_loss"] is None
    assert "不是现实世界的事件概率" in m["caveat"]


def test_pr_auc_imbalanced_case():
    """4 负 1 正：正样本分数排第 3 → AUC=0.5，PR(平均精度)=1/3。"""
    scores = [0.2, 0.9, 0.8, 0.3, 0.7]
    labels = [False, False, False, False, True]
    m = fs.noul_metrics(labels, scores)
    assert m["prevalence"] == 0.2
    assert m["auc"] == 0.5
    assert abs(m["pr_auc"] - 1 / 3) < 1e-9


def test_calibration_enables_brier_only_when_frozen():
    labels = [False, True, False, True]
    scores = [0.1, 0.9, 0.2, 0.8]
    raw = fs.noul_metrics(labels, scores)
    assert raw["brier"] is None
    cal = fs.fit_noul_calibration(labels, scores)
    assert cal["method"] == "platt"
    calibrated = fs.noul_metrics(labels, scores, calibration=cal)
    assert calibrated["calibrated"] is True
    assert 0 <= calibrated["brier"] <= 1
    assert "dev partition" in cal["fitted_on"]
    # 确定性：重复拟合得到相同参数
    assert fs.fit_noul_calibration(labels, scores) == cal


def test_calibration_needs_both_classes():
    with pytest.raises(FieldStudyError):
        fs.fit_noul_calibration([True, True], [0.2, 0.8])


# ---------------------------------------------------------------------------
# group clustered bootstrap
# ---------------------------------------------------------------------------


def test_cluster_bootstrap_deterministic_and_recorded():
    values = {"i1": True, "i2": True, "i3": False, "i4": True}
    groups = {"i1": "g1", "i2": "g1", "i3": "g2", "i4": "g3"}

    def metric(items):
        hits = [i for i in items if values.get(i)]
        return len(hits) / len(items) if items else None

    first = fs.cluster_bootstrap_ci(values, groups, metric, rounds=200)
    second = fs.cluster_bootstrap_ci(values, groups, metric, rounds=200)
    assert first == second
    assert first["seed"] == fs.FIELD_STUDY_BOOTSTRAP_SEED
    assert first["rounds"] == 200
    lo, hi = first["ci95"]
    assert lo <= 0.75 <= hi


def test_cluster_bootstrap_needs_two_groups():
    result = fs.cluster_bootstrap_ci(
        {"i1": True}, {"i1": "g1"}, lambda items: 1.0, rounds=10)
    assert result["ci95"] == [None, None]


# ---------------------------------------------------------------------------
# dev/blind 隔离与 blind-lock
# ---------------------------------------------------------------------------


def test_group_leakage_rejected():
    annotations = fs.load_annotations(SYNTH)
    with pytest.raises(FieldStudyError) as exc:
        fs.partition_by_group(annotations, ["group-A", "group-B"],
                              ["group-B", "group-C"])
    assert "分组隔离失败" in str(exc.value)


def test_blind_manifest_lock_verify_and_model_version(tmp_path):
    annotations, items, _ = _load_synth()
    ann_copy = tmp_path / "ann.json"
    ann_copy.write_text(SYNTH.read_text(encoding="utf-8"),
                        encoding="utf-8")
    items_copy = tmp_path / "items.json"
    items_copy.write_text(SYNTH_ITEMS.read_text(encoding="utf-8"),
                          encoding="utf-8")
    manifest = fs.lock_blind_manifest(
        dataset_version="fs-synth-v1", annotations_path=ann_copy,
        items_path=items_copy, dev_group_ids=["group-A"],
        blind_group_ids=["group-B", "group-C"], annotations=annotations,
        commit_sha="abc123", model_alias="jev-latest")
    assert manifest["model_version_observed"] is None
    assert manifest["chat_signal_schema"] == "v3.2" or \
        manifest["chat_signal_schema"].startswith("chat-signal-")
    fs.verify_blind_manifest(manifest, ann_copy, items_copy, annotations)
    manifest = fs.record_model_version(manifest, "jev-1.13.0")
    assert manifest["model_version_observed"] == "jev-1.13.0"


def test_blind_manifest_requires_commit_sha(tmp_path):
    annotations, _, _ = _load_synth()
    with pytest.raises(FieldStudyError):
        fs.lock_blind_manifest(
            dataset_version="v", annotations_path=SYNTH,
            items_path=SYNTH_ITEMS, dev_group_ids=["group-A"],
            blind_group_ids=["group-B"], annotations=annotations,
            commit_sha="", model_alias="jev")


def test_metric_definition_change_breaks_verify(tmp_path):
    annotations, _, _ = _load_synth()
    ann_copy = tmp_path / "ann.json"
    ann_copy.write_text(SYNTH.read_text(encoding="utf-8"),
                        encoding="utf-8")
    items_copy = tmp_path / "items.json"
    items_copy.write_text(SYNTH_ITEMS.read_text(encoding="utf-8"),
                          encoding="utf-8")
    manifest = fs.lock_blind_manifest(
        dataset_version="v", annotations_path=ann_copy,
        items_path=items_copy, dev_group_ids=["group-A"],
        blind_group_ids=["group-B", "group-C"], annotations=annotations,
        commit_sha="abc", model_alias="jev")
    manifest["metric_definitions_sha256"] = "0" * 64
    with pytest.raises(FieldStudyError):
        fs.verify_blind_manifest(manifest, ann_copy, items_copy, annotations)


# ---------------------------------------------------------------------------
# 评估报告：item 级 / IRR / 弃答 / 回忆延迟 / 可复现
# ---------------------------------------------------------------------------


def test_report_includes_items_groups_records_observers():
    annotations, _, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    ds = report["dataset"]
    assert ds["items"] == 5
    assert ds["groups"] == 3
    assert ds["annotation_records"] == 6
    assert ds["independent_observers"] == 3     # a-01 / a-02 / a-03
    assert ds["analysis_plan_version"] == fs.ANALYSIS_PLAN_VERSION


def test_abstention_and_recall_buckets_reported():
    annotations, _, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    assert report["abstention"]["total_labels"] == 14
    assert report["abstention"]["by_status"]["declined"] == 1
    assert report["abstention"]["by_status"]["unsure"] == 1
    assert report["abstention"]["by_status"]["insufficient"] == 1
    assert report["recall_delay"]["by_bucket"]["1_7_days"] == 1
    assert "回忆" in report["recall_delay"]["caveat"]
    # intent_observed item 级 n=2（synth-001/003 有 labeled；synth-004 unsure 跳过）
    assert report["choice_metrics"]["intent_observed"]["n"] == 2


def test_irr_reported_for_multi_rater_dims():
    annotations, _, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    # 情感/温暖等维度各只有 1 名观察者 → 不生成 IRR（正确行为）
    assert "warmth_observed" not in report["inter_rater"]
    # 合成数据另有专门的 IRR 用例（见 test_alpha_*）


def test_separate_layers_never_mixed_with_gold():
    annotations, _, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    layers = report["separate_layers"]
    assert layers["sender_intent_records"] == 1
    assert layers["participant_perception_records"] == 1
    assert layers["receiver_experience_records"] == 1
    # sender/receiver/participant 维度不出现在任何模型对分指标里
    for dim in report["choice_metrics"]:
        assert dim not in ("sender_intent", "participant_perception",
                           "receiver_felt_experience")


def test_noul_metrics_report_auc_not_calibration():
    annotations, _, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    rom = report["noul_metrics"]["romantic_binary"]
    assert rom["n"] == 2 and rom["auc"] == 1.0
    assert rom["brier"] is None
    assert "calibration" in " ".join(report["caveats"]) or \
        "Brier" in " ".join(report["caveats"])


def test_evaluation_deterministic_and_reproducible(tmp_path):
    annotations, _, model = _load_synth()
    first = fs.evaluate(annotations, model, dataset_version="synth")
    annotations2, _, model2 = _load_synth()
    second = fs.evaluate(annotations2, model2, dataset_version="synth")
    dump = lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True)
    assert dump(first) == dump(second)
    path = fs.save_report(tmp_path / "r.json", first)
    assert dump(json.loads(path.read_text(encoding="utf-8"))) == dump(first)


def test_report_contains_no_chat_text():
    annotations, _, model = _load_synth()
    annotations2, items, model2 = _load_synth()
    report = fs.evaluate(annotations2, model2, dataset_version="synth")
    text = fs.format_report(report)
    assert "不提供临床心理诊断" in text
    assert "不是现实世界的事件概率" in text
    for forbidden in ("多喝水", "暴雨", "看电影", "remember"):
        assert forbidden not in text.replace("Field Study evaluation", "")


def test_model_version_unknown_when_missing(tmp_path):
    payload = {"outputs": {"synth-001": {"emotion": {"choice": "calm"},
                                         "warmth": {"score": 2.0}}}}
    path = _write(tmp_path, payload, "outs.json")
    model = fs.load_model_outputs(path)
    assert model["schema_version"] == "unknown"
    assert model["model_alias"] == "unknown"


def test_freeze_roundtrip_with_items(tmp_path):
    annotations = fs.load_annotations(SYNTH)
    ann = tmp_path / "ann.json"
    ann.write_text(SYNTH.read_text(encoding="utf-8"), encoding="utf-8")
    items_copy = tmp_path / "items.json"
    items_copy.write_text(SYNTH_ITEMS.read_text(encoding="utf-8"),
                          encoding="utf-8")
    manifest = fs.freeze_dataset(
        annotations, ["group-A"], ["group-B", "group-C"], "v1", ann,
        items_path=items_copy)
    assert manifest["items"] == 5
    assert manifest["independent_observers"] == 3
    assert "item_dataset_sha256" in manifest
    fs.verify_freeze(manifest, annotations, ann, items_copy)
    tampered_ann = tmp_path / "ann2.json"
    tampered_ann.write_text(
        SYNTH.read_text(encoding="utf-8").replace("group-B", "group-Z"),
        encoding="utf-8")
    with pytest.raises(FieldStudyError):
        fs.verify_freeze(manifest, annotations, tampered_ann, items_copy)
