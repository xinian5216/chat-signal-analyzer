"""Field Study：独立的心理学相关行为验证工具（完全离线、确定性）。

定位与免责声明（写入每份报告）：

- 本项目是**聊天行为分析工具**：它分析“写下来的文字”，不提供临床心理
  诊断，不声称能直接读取他人的真实心理；
- 人工标注明确区分三层：发送者当时的交流意图、接收者实际感受到的互动、
  观察者仅根据文字能够识别的行为；
- 允许「不确定 / 不愿回答 / 信息不足」——不强迫所有案例拥有确定标签；
- 模型的 Noul 输出**只是模型内部的序数分数**，不是现实世界的事件概率，
  不得当作概率解释；本工具只报告排序质量（AUC）与 Brier 等统计量；
- 本模块不调用 Jev、不联网，只读取脱敏标注与已有模型输出。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

FIELD_STUDY_DIR = Path(__file__).resolve().parent / "evaluation" / "field_study"

FIELD_STUDY_SCHEMA = "field-study-annotation-v1"

# 标注层（三层分离）
INTENT_DIM = "sender_intent"
EXPERIENCE_DIM = "receiver_felt_experience"
OBSERVER_DIMS = (
    "emotion_observed",
    "warmth_observed",
    "engagement_observed",
    "special_attention_observed",
    "relational_ease_observed",
    "romantic_binary",
    "distancing_binary",
)

# 每个标签允许的状态（不确定/不愿回答/信息不足 与 labeled 同级）
LABEL_STATUSES = ("labeled", "unsure", "declined", "insufficient")
ABSTAIN_STATUSES = ("unsure", "declined", "insufficient")

# Choice / Score / Noul 维度与模型输出的映射（观察层 + 发送者意图层）
CHOICE_DIMS = {
    INTENT_DIM: "intent",
    "emotion_observed": "emotion",
}
SCORE_DIMS = {
    "warmth_observed": "warmth",
    "engagement_observed": "engagement",
    "special_attention_observed": "special_attention",
    "relational_ease_observed": "relational_ease",
}
NOUL_DIMS = {
    "romantic_binary": "romantic_signal",
    "distancing_binary": "distancing_signal",
}
# 无模型对应项的体验层维度：只做描述统计，不与模型对分
EXPERIENCE_ONLY_DIMS = (EXPERIENCE_DIM,)

ALLOWED_LABEL_KEYS = set(CHOICE_DIMS) | set(SCORE_DIMS) | set(NOUL_DIMS) \
    | set(EXPERIENCE_ONLY_DIMS)

# 标注记录允许的顶层字段（严格白名单，防隐私字段混入）
ANNOTATION_FIELDS = ("item_id", "group_id", "role", "labels", "annotator_id",
                     "notes")
ANNOTATION_ROLES = ("sender", "receiver", "observer")

# 明确禁止出现在标注中的字段（隐私）
FORBIDDEN_ANNOTATION_FIELDS = (
    "text", "chat", "raw_speaker", "speaker_name", "email", "phone",
    "exact_time", "timestamp", "user_id", "contact",
)

NOUL_CAVEAT = ("Noul 数值是模型输出的序数分数，不是现实世界的事件概率；"
               "此处仅报告排序质量（AUC）与 Brier 分数等统计量，"
               "不得解释为“该事件有 X% 概率发生”。")
NON_CLINICAL_CAVEAT = ("本工具是聊天行为分析设施：不提供临床心理诊断，"
                       "不声称能直接读取他人的真实心理。")
ABSTENTION_CAVEAT = ("弃答覆盖率是数据质量指标：unsure/declined/insufficient "
                     "不计为错误，也不计入指标分母。")


class FieldStudyError(ValueError):
    """field study 数据 / 使用错误。"""


# ---------------------------------------------------------------------------
# 标注加载与校验
# ---------------------------------------------------------------------------


def load_annotations(path: str | Path) -> list[dict]:
    """加载并校验脱敏标注文件（返回记录列表，保持文件顺序）。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FieldStudyError(f"标注文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise FieldStudyError(f"标注文件不是合法 JSON：{path}: {exc}") from exc
    if isinstance(raw, dict):
        raw = raw.get("annotations", [])
    if not isinstance(raw, list) or not raw:
        raise FieldStudyError(f"标注文件必须是非空数组：{path}")
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(raw):
        _validate_annotation(record, index, path)
        key = (record["item_id"], record["annotator_id"])
        if key in seen:
            raise FieldStudyError(
                f"同一 item_id + annotator_id 重复标注：{key}")
        seen.add(key)
    return raw


def _validate_annotation(record, index: int, path: Path) -> None:
    where = f"{path} 第 {index} 条"
    if not isinstance(record, dict):
        raise FieldStudyError(f"{where}: 必须是 object")
    for field in ("item_id", "group_id", "role", "labels", "annotator_id"):
        if field not in record:
            raise FieldStudyError(f"{where}: 缺少必填字段 {field!r}")
    unknown = set(record) - set(ANNOTATION_FIELDS)
    forbidden = set(record) & set(FORBIDDEN_ANNOTATION_FIELDS)
    if forbidden:
        raise FieldStudyError(
            f"{where}: 禁止字段 {sorted(forbidden)}"
            "（脱敏规范不允许携带隐私内容）")
    if unknown:
        raise FieldStudyError(f"{where}: 未知字段 {sorted(unknown)}")
    for field in ("item_id", "group_id", "role", "annotator_id"):
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise FieldStudyError(f"{where}: {field!r} 必须是非空字符串")
    if "notes" in record and not isinstance(record["notes"], str):
        raise FieldStudyError(f"{where}: notes 必须是字符串")
    if record["role"] not in ANNOTATION_ROLES:
        raise FieldStudyError(
            f"{where}: role 必须是 {ANNOTATION_ROLES} 之一")
    labels = record["labels"]
    if not isinstance(labels, dict) or not labels:
        raise FieldStudyError(f"{where}: labels 必须是非空 object")
    for dim, entry in labels.items():
        if dim not in ALLOWED_LABEL_KEYS:
            raise FieldStudyError(f"{where}: 未知标注维度 {dim!r}")
        if not isinstance(entry, dict) or "status" not in entry:
            raise FieldStudyError(f"{where}: {dim} 必须是含 status 的 object")
        if entry["status"] not in LABEL_STATUSES:
            raise FieldStudyError(
                f"{where}: {dim}.status 必须是 {LABEL_STATUSES} 之一")
        if entry["status"] == "labeled":
            if "value" not in entry:
                raise FieldStudyError(f"{where}: {dim} 标记 labeled 必须有 value")
            if dim in SCORE_DIMS and not isinstance(entry["value"], (int, float)):
                raise FieldStudyError(f"{where}: {dim} 的 value 必须是 0~4 数字")
            if dim in SCORE_DIMS and not 0 <= float(entry["value"]) <= 4:
                raise FieldStudyError(f"{where}: {dim} 的 value 必须在 0~4")
            if dim in NOUL_DIMS and entry["value"] not in (True, False):
                raise FieldStudyError(f"{where}: {dim} 的 value 必须是布尔")


# ---------------------------------------------------------------------------
# 模型输出加载
# ---------------------------------------------------------------------------


def load_model_outputs(path: str | Path) -> dict:
    """加载模型输出（含 schema_version / model 版本；缺失记 unknown）。"""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FieldStudyError(f"模型输出文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise FieldStudyError(f"模型输出不是合法 JSON：{path}: {exc}") from exc
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise FieldStudyError(f"模型输出缺少 outputs 映射：{path}")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "schema_version": payload.get("schema_version") or "unknown",
        "model": payload.get("model") or "unknown",
        "outputs": outputs,
        "source": str(path).replace("\\", "/"),
    }


# ---------------------------------------------------------------------------
# 分组隔离与数据集冻结
# ---------------------------------------------------------------------------


def check_group_isolation(dev_group_ids, blind_group_ids) -> None:
    """同一参与者/聊天双方的 group 不得同时出现在开发集与盲测集。"""
    dev, blind = set(dev_group_ids), set(blind_group_ids)
    overlap = sorted(dev & blind)
    if overlap:
        raise FieldStudyError(
            f"分组隔离失败：以下 group 同时出现在开发集与盲测集：{overlap}")


def partition_by_group(annotations, dev_group_ids, blind_group_ids):
    """按 group 划分开发/盲测集；未覆盖的 group 直接报错（不允许悬空）。"""
    groups = {a["group_id"] for a in annotations}
    assigned = set(dev_group_ids) | set(blind_group_ids)
    if groups != assigned:
        raise FieldStudyError(
            "分组划分未覆盖全部 group：未分配="
            f"{sorted(groups - assigned)}，多余={sorted(assigned - groups)}")
    check_group_isolation(dev_group_ids, blind_group_ids)
    dev = [a for a in annotations if a["group_id"] in set(dev_group_ids)]
    blind = [a for a in annotations if a["group_id"] in set(blind_group_ids)]
    return {"dev": dev, "blind": blind}


def freeze_dataset(annotations, dev_group_ids, blind_group_ids,
                   dataset_version: str, annotations_path: str | Path) -> dict:
    """冻结数据集：记录清单哈希 + 划分配置（供事后复验）。"""
    partition_by_group(annotations, dev_group_ids, blind_group_ids)
    items_path = Path(annotations_path)
    digest = hashlib.sha256(items_path.read_bytes()).hexdigest()
    return {
        "dataset_version": dataset_version,
        "field_study_schema": FIELD_STUDY_SCHEMA,
        "annotations_file": str(items_path).replace("\\", "/"),
        "annotations_sha256": digest,
        "items": len({a["item_id"] for a in annotations}),
        "annotation_records": len(annotations),
        "groups": sorted({a["group_id"] for a in annotations}),
        "partition": {"dev": sorted(set(dev_group_ids)),
                      "blind": sorted(set(blind_group_ids))},
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def verify_freeze(manifest: dict, annotations, annotations_path: str | Path) -> None:
    """复验冻结清单：哈希 / 数量 / 分组覆盖 / 隔离。任何不符即报错。"""
    items_path = Path(annotations_path)
    digest = hashlib.sha256(items_path.read_bytes()).hexdigest()
    if digest != manifest.get("annotations_sha256"):
        raise FieldStudyError("冻结校验失败：标注文件哈希与清单不符")
    if len({a["item_id"] for a in annotations}) != manifest.get("items"):
        raise FieldStudyError("冻结校验失败：案例数量与清单不符")
    partition_by_group(annotations,
                       manifest["partition"]["dev"],
                       manifest["partition"]["blind"])
    if sorted({a["group_id"] for a in annotations}) != sorted(manifest["groups"]):
        raise FieldStudyError("冻结校验失败：group 清单不符")


# ---------------------------------------------------------------------------
# 指标计算（确定性、纯函数）
# ---------------------------------------------------------------------------


def _wilson_ci(successes: int, total: int, z: float = 1.96):
    """比例的 Wilson 置信区间；样本为 0 时返回 (None, None)。"""
    if total == 0:
        return None, None
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = z * ((phat * (1 - phat) / total
                   + z * z / (4 * total * total)) ** 0.5) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def choice_metrics(gold: list, pred: list, classes) -> dict:
    """Choice 指标：accuracy（含 Wilson CI）、混淆矩阵、逐类 P/R/F1。"""
    classes = sorted(classes)
    if len(gold) != len(pred):
        raise FieldStudyError("choice_metrics: gold/pred 长度不一致")
    n = len(gold)
    confusion = {g: {p: 0 for p in classes} for g in classes}
    for g, p in zip(gold, pred):
        if g not in confusion:
            raise FieldStudyError(f"gold 标签 {g!r} 不在类别集合内")
        if p not in confusion[g]:
            raise FieldStudyError(f"预测标签 {p!r} 不在类别集合内")
        confusion[g][p] += 1
    correct = sum(confusion[c][c] for c in classes)
    per_class = {}
    for c in classes:
        tp = confusion[c][c]
        fp = sum(confusion[g][c] for g in classes) - tp
        fn = sum(confusion[c].values()) - tp
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        per_class[c] = {"precision": precision, "recall": recall,
                        "f1": f1, "support": tp + fn}
    lo, hi = _wilson_ci(correct, n)
    return {
        "n": n,
        "accuracy": correct / n if n else None,
        "accuracy_ci95": [lo, hi],
        "confusion": confusion,
        "per_class": per_class,
    }


def score_metrics(gold: list[float], pred: list[float]) -> dict:
    """Score 指标：n / MAE / RMSE / 有符号偏差。"""
    if len(gold) != len(pred):
        raise FieldStudyError("score_metrics: gold/pred 长度不一致")
    n = len(gold)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    errors = [p - g for g, p in zip(gold, pred)]
    return {
        "n": n,
        "mae": sum(abs(e) for e in errors) / n,
        "rmse": (sum(e * e for e in errors) / n) ** 0.5,
        "bias": sum(errors) / n,
    }


def noul_metrics(gold_binary: list[bool], scores: list[float]) -> dict:
    """Noul 排序指标：n / AUC / Brier。附带“非概率”告示。"""
    if len(gold_binary) != len(scores):
        raise FieldStudyError("noul_metrics: gold/scores 长度不一致")
    n = len(gold_binary)
    if n == 0:
        return {"n": 0, "auc": None, "brier": None, "caveat": NOUL_CAVEAT}
    positives = sum(1 for g in gold_binary if g)
    negatives = n - positives
    if positives == 0 or negatives == 0:
        return {"n": n, "auc": None,
                "brier": None,
                "caveat": NOUL_CAVEAT + "（样本缺少正类或负类，AUC 未计算）"}
    ordered = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[ordered[j + 1]] == scores[ordered[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[ordered[k]] = avg_rank
        i = j + 1
    rank_sum_pos = sum(r for r, g in zip(ranks, gold_binary) if g)
    auc = (rank_sum_pos - positives * (positives + 1) / 2) / (positives * negatives)
    brier = sum((s - (1.0 if g else 0.0)) ** 2
                for s, g in zip(scores, gold_binary)) / n
    return {"n": n, "auc": auc, "brier": brier, "caveat": NOUL_CAVEAT}


def abstention_summary(annotations) -> dict:
    """弃答覆盖率：按状态统计所有标签条目。"""
    counts = {status: 0 for status in LABEL_STATUSES}
    total = 0
    for record in annotations:
        for entry in record["labels"].values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            total += 1
    abstain = sum(counts[s] for s in ABSTAIN_STATUSES)
    return {
        "total_labels": total,
        "by_status": counts,
        "abstain_coverage": abstain / total if total else None,
        "caveat": ABSTENTION_CAVEAT,
    }


# ---------------------------------------------------------------------------
# 评估与报告
# ---------------------------------------------------------------------------


def _collect_pairs(annotations, outputs):
    """按维度收集 (gold, pred) 配对；跳过弃答/缺模型输出/类型不符。"""
    pairs = {dim: {"gold": [], "pred": []} for dim in
             list(CHOICE_DIMS) + list(SCORE_DIMS) + list(NOUL_DIMS)}
    skipped = {"abstained": 0, "missing_output": 0, "type_mismatch": 0}
    for record in annotations:
        item_id = record["item_id"]
        model = outputs.get(item_id)
        for dim, entry in record["labels"].items():
            if entry["status"] != "labeled":
                skipped["abstained"] += 1
                continue
            if model is None:
                skipped["missing_output"] += 1
                continue
            value = entry["value"]
            if dim in CHOICE_DIMS:
                model_value = model.get(CHOICE_DIMS[dim], {}).get("choice")
                if model_value is None:
                    skipped["missing_output"] += 1
                    continue
                pairs[dim]["gold"].append(value)
                pairs[dim]["pred"].append(model_value)
            elif dim in SCORE_DIMS:
                model_value = model.get(SCORE_DIMS[dim], {}).get("score")
                if model_value is None:
                    skipped["missing_output"] += 1
                    continue
                pairs[dim]["gold"].append(float(value))
                pairs[dim]["pred"].append(float(model_value))
            elif dim in NOUL_DIMS:
                model_value = model.get(NOUL_DIMS[dim])
                if model_value is None:
                    skipped["missing_output"] += 1
                    continue
                if not isinstance(model_value, (int, float)):
                    skipped["type_mismatch"] += 1
                    continue
                pairs[dim]["gold"].append(bool(value))
                pairs[dim]["pred"].append(float(model_value))
    return pairs, skipped


def evaluate(annotations, model: dict, *, dataset_version: str,
             partition: dict | None = None) -> dict:
    """离线评估：标注 × 已有模型输出 → 指标报告（确定性，无时间戳）。"""
    outputs = model["outputs"]
    pairs, skipped = _collect_pairs(annotations, outputs)

    choice = {}
    for dim in CHOICE_DIMS:
        gold, pred = pairs[dim]["gold"], pairs[dim]["pred"]
        classes = {g for g in gold} | {p for p in pred}
        if classes:
            choice[dim] = choice_metrics(gold, pred, classes)

    scores = {}
    for dim in SCORE_DIMS:
        gold, pred = pairs[dim]["gold"], pairs[dim]["pred"]
        if gold:
            scores[dim] = score_metrics(gold, pred)

    nouls = {}
    for dim in NOUL_DIMS:
        gold, pred = pairs[dim]["gold"], pairs[dim]["pred"]
        if gold:
            nouls[dim] = noul_metrics(gold, pred)

    annotated_ids = {a["item_id"] for a in annotations}
    report = {
        "dataset": {
            "field_study_schema": FIELD_STUDY_SCHEMA,
            "dataset_version": dataset_version,
            "items": len(annotated_ids),
            "groups": len({a["group_id"] for a in annotations}),
            "annotation_records": len(annotations),
            "partition": (None if partition is None else
                          {name: len(items) for name, items in
                           partition.items()}),
        },
        "model": {
            "schema_version": model["schema_version"],
            "model": model["model"],
            "source": model["source"],
        },
        "abstention": abstention_summary(annotations),
        "choice_metrics": choice,
        "score_metrics": scores,
        "noul_metrics": nouls,
        "skipped_pairs": skipped,
        "unmatched": {
            "annotations_without_output": sorted(
                annotated_ids - set(outputs)),
            "outputs_without_annotation": sorted(
                set(outputs) - annotated_ids),
        },
        "caveats": [NON_CLINICAL_CAVEAT, NOUL_CAVEAT, ABSTENTION_CAVEAT],
    }
    return report


def format_report(report: dict) -> str:
    """人类可读报告（不含任何聊天文本）。"""
    lines = [
        "=== Field Study evaluation（offline, deterministic） ===",
        f"dataset: {report['dataset']['dataset_version']} "
        f"(schema {report['dataset']['field_study_schema']}) "
        f"items={report['dataset']['items']} "
        f"groups={report['dataset']['groups']}",
        f"model: {report['model']['model']} "
        f"(schema {report['model']['schema_version']}, "
        f"source {report['model']['source']})",
        f"abstain coverage: {report['abstention']['abstain_coverage']} "
        f"({report['abstention']['by_status']})",
    ]
    if report["choice_metrics"]:
        lines.append("choice metrics:")
        for dim, m in sorted(report["choice_metrics"].items()):
            ci = m["accuracy_ci95"]
            lines.append(f"  {dim}: n={m['n']} acc={m['accuracy']:.3f} "
                         f"ci95=[{ci[0]:.3f},{ci[1]:.3f}]")
    if report["score_metrics"]:
        lines.append("score metrics:")
        for dim, m in sorted(report["score_metrics"].items()):
            lines.append(f"  {dim}: n={m['n']} mae={m['mae']:.3f} "
                         f"rmse={m['rmse']:.3f} bias={m['bias']:+.3f}")
    if report["noul_metrics"]:
        lines.append("noul metrics（非概率）:")
        for dim, m in sorted(report["noul_metrics"].items()):
            lines.append(f"  {dim}: n={m['n']} auc={m['auc']} "
                         f"brier={m['brier']}")
    lines.append("caveats:")
    for caveat in report["caveats"]:
        lines.append(f"  - {caveat}")
    return "\n".join(lines)


def save_report(path: str | Path, report: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                 sort_keys=True), encoding="utf-8")
    return target
