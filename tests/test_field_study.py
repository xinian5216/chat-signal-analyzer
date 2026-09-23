"""Field Study 离线工具测试：全部使用合成数据，绝不调用真实 Jev。

覆盖：标注 schema 校验、指标计算（accuracy/混淆矩阵/P-R-F1/MAE/
AUC/Brier/Wilson CI）、弃答处理、缺失标签、参与者分组隔离、数据集
冻结与复验（含篡改检测）、版本追踪与报告可复现性。
"""

import json
from pathlib import Path

import pytest

import field_study as fs
from field_study import FieldStudyError

REPO = Path(__file__).resolve().parents[1]
FS_DIR = REPO / "evaluation" / "field_study"
SYNTH = FS_DIR / "examples" / "annotations_synthetic.json"
SYNTH_MODEL = FS_DIR / "examples" / "model_outputs_synthetic.json"


def _load_synth():
    return fs.load_annotations(SYNTH), fs.load_model_outputs(SYNTH_MODEL)


# ---------------------------------------------------------------------------
# 标注 schema 校验
# ---------------------------------------------------------------------------


def test_synth_annotations_load_and_validate():
    annotations, model = _load_synth()
    assert len(annotations) == 6
    assert len({a["item_id"] for a in annotations}) == 5
    assert model["schema_version"] == "chat-signal-v3.2"
    assert model["model"] == "jev-1.13.0"


def _write(tmp_path, payload, name="bad.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")
    return path


def test_forbidden_privacy_field_rejected(tmp_path):
    record = {"item_id": "x", "group_id": "g", "role": "observer",
              "annotator_id": "a", "raw_speaker": "某昵称",
              "labels": {"emotion_observed": {"status": "unsure"}}}
    with pytest.raises(FieldStudyError) as exc:
        fs.load_annotations(_write(tmp_path, [record]))
    assert "禁止字段" in str(exc.value)


def test_unknown_dimension_rejected(tmp_path):
    record = {"item_id": "x", "group_id": "g", "role": "observer",
              "annotator_id": "a",
              "labels": {"dream_meaning": {"status": "unsure"}}}
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [record]))


def test_invalid_status_rejected(tmp_path):
    record = {"item_id": "x", "group_id": "g", "role": "observer",
              "annotator_id": "a",
              "labels": {"warmth_observed": {"status": "maybe"}}}
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [record]))


def test_labeled_without_value_rejected(tmp_path):
    record = {"item_id": "x", "group_id": "g", "role": "observer",
              "annotator_id": "a",
              "labels": {"warmth_observed": {"status": "labeled"}}}
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [record]))


def test_score_out_of_range_rejected(tmp_path):
    record = {"item_id": "x", "group_id": "g", "role": "observer",
              "annotator_id": "a",
              "labels": {"warmth_observed": {"status": "labeled",
                                             "value": 9}}}
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [record]))


def test_duplicate_item_annotator_rejected(tmp_path):
    base = {"group_id": "g", "role": "observer", "annotator_id": "a",
            "labels": {"warmth_observed": {"status": "unsure"}}}
    records = [dict(base, item_id="x"), dict(base, item_id="x")]
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, records))


def test_binary_must_be_boolean(tmp_path):
    record = {"item_id": "x", "group_id": "g", "role": "observer",
              "annotator_id": "a",
              "labels": {"romantic_binary": {"status": "labeled",
                                             "value": "maybe"}}}
    with pytest.raises(FieldStudyError):
        fs.load_annotations(_write(tmp_path, [record]))


# ---------------------------------------------------------------------------
# 指标计算（手工核对）
# ---------------------------------------------------------------------------


def test_choice_metrics_hand_computed():
    gold = ["a", "a", "b", "c"]
    pred = ["a", "b", "b", "c"]
    m = fs.choice_metrics(gold, pred, {"a", "b", "c"})
    assert m["n"] == 4
    assert m["accuracy"] == 3 / 4
    assert m["confusion"]["a"]["a"] == 1 and m["confusion"]["a"]["b"] == 1
    assert m["per_class"]["a"]["precision"] == 1.0
    assert m["per_class"]["a"]["recall"] == 0.5
    assert m["per_class"]["b"]["precision"] == 0.5
    assert m["per_class"]["b"]["recall"] == 1.0
    assert m["per_class"]["c"]["f1"] == 1.0
    lo, hi = m["accuracy_ci95"]
    assert 0 < lo <= m["accuracy"] <= hi < 1


def test_wilson_ci_edges():
    assert fs._wilson_ci(0, 0) == (None, None)
    lo, hi = fs._wilson_ci(5, 5)
    assert hi == 1.0 and lo > 0.5


def test_score_metrics_hand_computed():
    m = fs.score_metrics([1.0, 3.0], [2.0, 1.0])
    assert m["n"] == 2
    assert m["mae"] == 1.5
    assert m["rmse"] == pytest.approx((1 + 4) ** 0.5 / 2 ** 0.5)
    assert m["bias"] == pytest.approx(-0.5)


def test_noul_metrics_ordering_and_brier():
    m = fs.noul_metrics([False, True], [0.1, 0.9])
    assert m["n"] == 2 and m["auc"] == 1.0
    assert m["brier"] == pytest.approx((0.01 + 0.01) / 2)
    assert "不是现实世界的事件概率" in m["caveat"]


def test_noul_single_class_no_auc():
    m = fs.noul_metrics([True, True], [0.2, 0.8])
    assert m["auc"] is None and m["n"] == 2


# ---------------------------------------------------------------------------
# 弃答与缺失标签
# ---------------------------------------------------------------------------


def test_abstention_not_counted_as_error():
    annotations, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    ab = report["abstention"]
    assert ab["total_labels"] == 13
    assert ab["by_status"]["declined"] == 1
    assert ab["by_status"]["unsure"] == 1
    assert ab["by_status"]["insufficient"] == 1
    assert ab["abstain_coverage"] == pytest.approx(3 / 13)
    # synth-005（只有体验层标签、无模型输出）进 unmatched 而非错误
    assert report["unmatched"]["annotations_without_output"] == ["synth-005"]
    # sender_intent 只用 3 条 labeled 样本（synth-004 unsure 被跳过）
    assert report["choice_metrics"]["sender_intent"]["n"] == 3


def test_scores_and_noul_on_synth():
    annotations, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth")
    assert report["score_metrics"]["warmth_observed"]["n"] == 1
    assert report["score_metrics"]["warmth_observed"]["mae"] == 0.0
    rom = report["noul_metrics"]["romantic_binary"]
    assert rom["n"] == 2 and rom["auc"] == 1.0


# ---------------------------------------------------------------------------
# 分组隔离与冻结
# ---------------------------------------------------------------------------


def test_group_isolation_detects_overlap():
    with pytest.raises(FieldStudyError) as exc:
        fs.check_group_isolation(["g1", "g2"], ["g2", "g3"])
    assert "分组隔离失败" in str(exc.value)


def test_partition_must_cover_all_groups():
    annotations, _ = _load_synth()
    with pytest.raises(FieldStudyError):
        fs.partition_by_group(annotations, ["group-A"], [])


def test_partition_disjoint_groups():
    annotations, _ = _load_synth()
    parts = fs.partition_by_group(annotations, ["group-A"],
                                  ["group-B", "group-C"])
    assert len(parts["dev"]) == 3 and len(parts["blind"]) == 3


def test_freeze_and_verify_roundtrip(tmp_path):
    annotations, _ = _load_synth()
    frozen_copy = tmp_path / "annotations.json"
    frozen_copy.write_text(SYNTH.read_text(encoding="utf-8"),
                           encoding="utf-8")
    manifest = fs.freeze_dataset(annotations, ["group-A"],
                                 ["group-B", "group-C"], "synth-v1",
                                 frozen_copy)
    assert manifest["items"] == 5
    fs.verify_freeze(manifest, annotations, frozen_copy)  # 不抛错

    # 篡改标注内容 → 哈希不符必须被发现
    tampered = tmp_path / "tampered.json"
    tampered.write_text(frozen_copy.read_text(encoding="utf-8")
                        .replace("group-B", "group-Z"), encoding="utf-8")
    with pytest.raises(FieldStudyError):
        fs.verify_freeze(manifest, annotations, tampered)


def test_freeze_partition_tamper_detected(tmp_path):
    annotations, _ = _load_synth()
    frozen_copy = tmp_path / "a.json"
    frozen_copy.write_text(SYNTH.read_text(encoding="utf-8"),
                           encoding="utf-8")
    manifest = fs.freeze_dataset(annotations, ["group-A"],
                                 ["group-B", "group-C"], "v1", frozen_copy)
    manifest["partition"]["blind"].append("group-A")   # 破坏隔离
    with pytest.raises(FieldStudyError):
        fs.verify_freeze(manifest, annotations, frozen_copy)


# ---------------------------------------------------------------------------
# 版本追踪与可复现性
# ---------------------------------------------------------------------------


def test_report_records_model_and_schema_versions():
    annotations, model = _load_synth()
    report = fs.evaluate(annotations, model, dataset_version="synth-v1")
    assert report["model"]["schema_version"] == "chat-signal-v3.2"
    assert report["model"]["model"] == "jev-1.13.0"
    assert report["dataset"]["field_study_schema"] == \
        fs.FIELD_STUDY_SCHEMA
    assert "不是现实世界的事件概率" in "\n".join(report["caveats"])
    assert "不提供临床心理诊断" in report["caveats"][0]


def test_model_outputs_without_version_recorded_unknown(tmp_path):
    payload = {"outputs": {"synth-001": {"emotion": {"choice": "calm"}}}}
    path = _write(tmp_path, payload, "outs.json")
    model = fs.load_model_outputs(path)
    assert model["schema_version"] == "unknown"
    assert model["model"] == "unknown"


def test_evaluation_is_deterministic_and_reproducible(tmp_path):
    annotations, model = _load_synth()
    first = fs.evaluate(annotations, model, dataset_version="synth")
    second = fs.evaluate(fs.load_annotations(SYNTH),
                         fs.load_model_outputs(SYNTH_MODEL),
                         dataset_version="synth")
    dump = lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True)
    assert dump(first) == dump(second)
    path = fs.save_report(tmp_path / "r.json", first)
    assert dump(json.loads(path.read_text(encoding="utf-8"))) == dump(first)


def test_report_contains_no_chat_text():
    annotations, model = _load_synth()
    text = fs.format_report(fs.evaluate(annotations, model,
                                        dataset_version="synth"))
    assert "caring" not in text.split("choice metrics:")[0].replace(
        "abstain coverage", "")
    for forbidden in ("装修队", "多喝水", "晚安"):
        assert forbidden not in text
