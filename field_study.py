"""Field Study v1.1：独立的人工验证设施（完全离线、确定性）。

相对 v1 的科学加固（详见 evaluation/field_study/PROTOCOL.md）：

1. **item 级指标**：同一 item 的多个观察者标注不再各自计为一个模型样本；
   观察者标签先汇总为 item 级 reference（多数决 / 单标注 / contested），
   模型预测每 item 只计一次。
2. **group clustered CI**：同一聊天双方（group）的多条 item 不假设独立；
   主不确定性指标为 group-level bootstrap（seed 与次数固定并记录），
   Wilson CI 仅作描述性参考。
3. **inter-rater reliability**：Krippendorff's alpha（nominal / ordinal，
   自实现、允许缺失与多标注者）；人类一致性低时，模型与“人类真值”的
   一致率解释力有限（报告明示）。
4. **Noul 修正**：原始 Noul 主指标 ROC-AUC + PR-AUC；**不**再把 raw Noul
   的 Brier 当作概率校准指标。Brier / log loss 只能在 dev partition 上
   拟合并冻结 calibration mapping 之后、只在 blind partition 上计算。
5. **item dataset**：脱敏 item（含 messages / target_index）独立 schema，
   模型输入与观察者文本由同一份冻结 item dataset 的同一渲染函数派生；
   freeze manifest 同时绑定 item / annotations 哈希。
6. **角色合法性**：sender_intent 仅 sender role；receiver_felt_experience
   仅 receiver role；observer 维度仅 observer role；participant_perception
   是单独层，绝不与 observer gold 混合。
7. **blind lock**：揭晓前冻结 dataset 版本、双哈希、划分、SignalLens
   commit SHA、chat-signal schema、模型 alias、指标定义哈希与（未来的）
   校准映射；揭晓后该 blind set 退休为 development evidence。
8. **recall delay**：sender 事后回忆意图的粗粒度延迟分桶（禁止精确时间），
   报告按桶给出样本数并声明 retrospective self-report 非无误差真值。

免责声明（写入每份报告）：本项目是聊天行为分析工具，不提供临床心理
诊断，不声称能直接读取他人的真实心理；本模块不调用 Jev、不联网。
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

FIELD_STUDY_DIR = Path(__file__).resolve().parent / "evaluation" / "field_study"

ANNOTATION_SCHEMA_VERSION = "field-study-annotation-v1"
ITEM_SCHEMA_VERSION = "field-study-item-v1"
ANALYSIS_PLAN_VERSION = "field-study-plan-v1.1"
FIELD_STUDY_BOOTSTRAP_SEED = 20260923   # 固定 seed：可复现
FIELD_STUDY_BOOTSTRAP_ROUNDS = 2000     # bootstrap 次数：固定并记录

# ---------------------------------------------------------------------------
# 标注层与合法性
# ---------------------------------------------------------------------------

# 发送者本人回顾意图（sender ground truth，仅 sender role）
INTENT_DIM = "sender_intent"
# 接收者实际感受（仅 receiver role；与模型 gold 分开，不参与对分）
EXPERIENCE_DIM = "receiver_felt_experience"
# 参与者主观认为文本看起来如何（sender/receiver role；单独层，绝不与
# observer gold 混合）
PERCEPTION_DIM = "participant_perception"
# 独立观察者维度（仅 observer role；模型对分的 human reference）
OBSERVER_CHOICE_DIMS = {"intent_observed": "intent",
                        "emotion_observed": "emotion"}
OBSERVER_SCORE_DIMS = {"warmth_observed": "warmth",
                       "engagement_observed": "engagement",
                       "special_attention_observed": "special_attention",
                       "relational_ease_observed": "relational_ease"}
OBSERVER_NOUL_DIMS = {"romantic_binary": "romantic_signal",
                      "distancing_binary": "distancing_signal"}

LABEL_STATUSES = ("labeled", "unsure", "declined", "insufficient")
ABSTAIN_STATUSES = ("unsure", "declined", "insufficient")
ANNOTATION_ROLES = ("sender", "receiver", "observer")
RECALL_DELAY_BUCKETS = ("same_day", "1_7_days", "8_30_days", "31_plus",
                        "unknown")

ALLOWED_LABEL_KEYS = ({INTENT_DIM, EXPERIENCE_DIM, PERCEPTION_DIM}
                      | set(OBSERVER_CHOICE_DIMS) | set(OBSERVER_SCORE_DIMS)
                      | set(OBSERVER_NOUL_DIMS))
ROLE_ALLOWED_DIMS = {
    "sender": {INTENT_DIM, PERCEPTION_DIM},
    "receiver": {EXPERIENCE_DIM, PERCEPTION_DIM},
    "observer": set(OBSERVER_CHOICE_DIMS) | set(OBSERVER_SCORE_DIMS)
    | set(OBSERVER_NOUL_DIMS),
}

ANNOTATION_FIELDS = ("item_id", "group_id", "role", "labels", "annotator_id",
                     "notes", "recall_delay_bucket")
FORBIDDEN_ANNOTATION_FIELDS = (
    "text", "chat", "raw_speaker", "speaker_name", "email", "phone",
    "exact_time", "timestamp", "user_id", "contact", "account", "handle",
)

NOUL_CAVEAT = ("Noul 数值是模型输出的序数分数，不是现实世界的事件概率；"
               "此处报告 ROC-AUC / PR-AUC 等排序指标。Brier / log loss 等"
               "概率校准指标只能在 dev partition 上拟合并冻结 calibration "
               "mapping 之后、只在 blind partition 上计算。")
NON_CLINICAL_CAVEAT = ("本工具是聊天行为分析设施：不提供临床心理诊断，"
                       "不声称能直接读取他人的真实心理。")
ABSTENTION_CAVEAT = ("弃答覆盖率是数据质量指标：unsure/declined/insufficient "
                     "不计为错误，也不计入指标分母。")
RETROSPECTIVE_CAVEAT = ("sender_intent 是发送者事后自我报告，可能存在回忆"
                        "偏差，不是无误差的心理真值；请结合 recall_delay_bucket "
                        "分布解释。")
IRR_CAVEAT = ("Krippendorff's alpha 低表示人类标注者本身一致性不足，"
              "此时模型与“人类参考”的一致率解释力有限。")
BLIND_POLICY = ("一旦查看 blind performance，该 blind set 即退休为 "
                "development evidence；基于盲测结果修改模型后，必须使用新的 "
                "blind groups 才能再次声称独立盲测。")


class FieldStudyError(ValueError):
    """field study 数据 / 使用错误。"""


# ---------------------------------------------------------------------------
# 脱敏 item dataset
# ---------------------------------------------------------------------------

ITEM_FIELDS = ("item_id", "group_id", "messages", "target_index")
ITEM_OPTIONAL_FIELDS = ("time_bucket", "source_note")
ITEM_FORBIDDEN_FIELDS = ("text", "chat", "raw_speaker", "speaker_name",
                         "contact", "email", "phone", "exact_time",
                         "timestamp", "user_id", "account", "handle",
                         "media_content", "real_names", "nickname")
ITEM_SPEAKERS = ("me", "them")
TIME_BUCKETS = ("early_morning", "morning", "afternoon", "evening",
                "late_night")


def load_items(path: str | Path) -> list[dict]:
    """加载并校验脱敏 item dataset（严格白名单，禁隐私字段）。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FieldStudyError(f"item 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise FieldStudyError(f"item 文件不是合法 JSON：{path}: {exc}") from exc
    if isinstance(raw, dict):
        raw = raw.get("items", [])
    if not isinstance(raw, list) or not raw:
        raise FieldStudyError(f"item 文件必须是非空数组：{path}")
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{path} 第 {index} 条"
        if not isinstance(item, dict):
            raise FieldStudyError(f"{where}: 必须是 object")
        forbidden = set(item) & set(ITEM_FORBIDDEN_FIELDS)
        if forbidden:
            raise FieldStudyError(
                f"{where}: 禁止字段 {sorted(forbidden)}（隐私内容）")
        unknown = set(item) - set(ITEM_FIELDS) - set(ITEM_OPTIONAL_FIELDS)
        if unknown:
            raise FieldStudyError(f"{where}: 未知字段 {sorted(unknown)}")
        for field in ITEM_FIELDS:
            if field not in item:
                raise FieldStudyError(f"{where}: 缺少必填字段 {field!r}")
        if not (isinstance(item["item_id"], str) and item["item_id"].strip()):
            raise FieldStudyError(f"{where}: item_id 必须是非空字符串")
        if not (isinstance(item["group_id"], str) and item["group_id"].strip()):
            raise FieldStudyError(f"{where}: group_id 必须是非空字符串")
        if item["item_id"] in seen:
            raise FieldStudyError(f"{where}: item_id 重复 {item['item_id']!r}")
        seen.add(item["item_id"])
        messages = item["messages"]
        if not isinstance(messages, list) or not messages:
            raise FieldStudyError(f"{where}: messages 必须是非空数组")
        for m in messages:
            if not isinstance(m, dict) or set(m) != {"speaker", "text"}:
                raise FieldStudyError(
                    f"{where}: message 必须只含 speaker/text")
            if m["speaker"] not in ITEM_SPEAKERS:
                raise FieldStudyError(
                    f"{where}: speaker 必须是 {ITEM_SPEAKERS}（假名角色）")
            if not isinstance(m["text"], str) or not m["text"].strip():
                raise FieldStudyError(f"{where}: message.text 不能为空")
        target_index = item["target_index"]
        if not isinstance(target_index, int) or isinstance(target_index, bool) \
                or not 0 <= target_index < len(messages):
            raise FieldStudyError(
                f"{where}: target_index 必须是 0..{len(messages) - 1} 的整数")
        if messages[target_index]["speaker"] != "them":
            raise FieldStudyError(f"{where}: target 必须是 them 侧消息")
        if "time_bucket" in item and item["time_bucket"] not in TIME_BUCKETS:
            raise FieldStudyError(
                f"{where}: time_bucket 必须是 {TIME_BUCKETS} 之一")
    return raw


def render_item_text(item: dict) -> str:
    """item 的唯一文本渲染：模型输入与观察者视图都由它派生。"""
    return "\n".join(f"{m['speaker']}: {m['text']}" for m in item["messages"])


def item_target_message(item: dict) -> dict:
    return item["messages"][item["target_index"]]


# ---------------------------------------------------------------------------
# 标注加载与校验
# ---------------------------------------------------------------------------


def load_annotations(path: str | Path) -> list[dict]:
    """加载并校验脱敏标注（角色-维度合法性 + 隐私字段白名单）。"""
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
    if record["role"] not in ANNOTATION_ROLES:
        raise FieldStudyError(f"{where}: role 必须是 {ANNOTATION_ROLES} 之一")
    if "notes" in record and not isinstance(record["notes"], str):
        raise FieldStudyError(f"{where}: notes 必须是字符串")
    if "recall_delay_bucket" in record:
        if record["recall_delay_bucket"] not in RECALL_DELAY_BUCKETS:
            raise FieldStudyError(
                f"{where}: recall_delay_bucket 必须是 "
                f"{RECALL_DELAY_BUCKETS} 之一")
    labels = record["labels"]
    if not isinstance(labels, dict) or not labels:
        raise FieldStudyError(f"{where}: labels 必须是非空 object")
    allowed = ROLE_ALLOWED_DIMS[record["role"]]
    for dim, entry in labels.items():
        if dim not in ALLOWED_LABEL_KEYS:
            raise FieldStudyError(f"{where}: 未知标注维度 {dim!r}")
        if dim not in allowed:
            raise FieldStudyError(
                f"{where}: 维度 {dim!r} 不允许由 role={record['role']} 填写"
                f"（该角色只能填 {sorted(allowed)}）")
        if not isinstance(entry, dict) or "status" not in entry:
            raise FieldStudyError(f"{where}: {dim} 必须是含 status 的 object")
        if entry["status"] not in LABEL_STATUSES:
            raise FieldStudyError(
                f"{where}: {dim}.status 必须是 {LABEL_STATUSES} 之一")
        if entry["status"] == "labeled":
            if "value" not in entry:
                raise FieldStudyError(f"{where}: {dim} 标记 labeled 必须有 value")
            value = entry["value"]
            if dim in OBSERVER_SCORE_DIMS:
                if not isinstance(value, (int, float)) \
                        or isinstance(value, bool) or not 0 <= value <= 4:
                    raise FieldStudyError(
                        f"{where}: {dim} 的 value 必须是 0~4 数字")
            if dim in OBSERVER_NOUL_DIMS and not isinstance(value, bool):
                raise FieldStudyError(f"{where}: {dim} 的 value 必须是布尔")


# ---------------------------------------------------------------------------
# 模型输出
# ---------------------------------------------------------------------------


def load_model_outputs(path: str | Path) -> dict:
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
    return {
        "schema_version": payload.get("schema_version") or "unknown",
        "model": payload.get("model") or "unknown",
        "model_alias": payload.get("model_alias") or "unknown",
        "outputs": outputs,
        "source": str(path).replace("\\", "/"),
    }


# ---------------------------------------------------------------------------
# item 级 reference 汇总（避免伪重复）
# ---------------------------------------------------------------------------


def build_item_reference(annotations) -> dict:
    """按 item×维度汇总观察者标签 → item 级 reference。

    返回 {item_id: {dim: {"value", "n_raters", "agreement", "contested"}}}。
    - categorical/binary：多数决；无多数（平票）→ contested（不计入指标）；
    - ordinal score：labeled 值的中位数（偶数取较低中位数，确定性）；
    - sender_intent / receiver_felt_experience / participant_perception 原样
      按记录收集（不进入模型对分指标，仅描述统计）。
    """
    per_item: dict[str, dict[str, list]] = {}
    for record in annotations:
        item = per_item.setdefault(record["item_id"], {})
        for dim, entry in record["labels"].items():
            if entry["status"] != "labeled":
                continue
            item.setdefault(dim, []).append(entry["value"])

    reference: dict[str, dict] = {}
    for item_id, dims in per_item.items():
        ref: dict[str, dict] = {}
        for dim, values in dims.items():
            if dim == INTENT_DIM or dim == EXPERIENCE_DIM \
                    or dim == PERCEPTION_DIM:
                ref[dim] = {"value": values[-1], "n_raters": len(values),
                            "agreement": "single", "contested": False}
                continue
            if dim in OBSERVER_SCORE_DIMS or dim == EXPERIENCE_DIM:
                ordered = sorted(float(v) for v in values)
                mid = (len(ordered) - 1) // 2       # 较低中位数
                ref[dim] = {"value": ordered[mid], "n_raters": len(values),
                            "agreement": "single" if len(values) == 1
                            else "median", "contested": False}
                continue
            counts: dict = {}
            for value in values:
                counts[value] = counts.get(value, 0) + 1
            top = max(counts.values())
            winners = sorted(v for v, c in counts.items() if c == top)
            if len(winners) == 1:
                ref[dim] = {"value": winners[0], "n_raters": len(values),
                            "agreement": "unanimous" if len(values) > 1
                            and len(counts) == 1 else "majority",
                            "contested": False}
            else:
                ref[dim] = {"value": None, "n_raters": len(values),
                            "agreement": "contested", "contested": True}
        reference[item_id] = ref
    return reference


# ---------------------------------------------------------------------------
# Krippendorff's alpha（自实现：nominal / ordinal，允许多标注者与缺失）
# ---------------------------------------------------------------------------


def _coincidence_matrix(units):
    values = sorted({v for unit in units for v in unit.values()},
                    key=lambda x: (str(type(x)), x))
    index = {v: i for i, v in enumerate(values)}
    size = len(values)
    o = [[0.0] * size for _ in range(size)]
    for unit in units:
        rated = list(unit.values())
        k = len(rated)
        if k < 2:
            continue                        # 少于 2 个标注者的 unit 无贡献
        for a in range(k):
            for b in range(a + 1, k):
                i, j = index[rated[a]], index[rated[b]]
                o[i][j] += 1.0 / (k - 1)
                o[j][i] += 1.0 / (k - 1)
    return values, o


def krippendorff_alpha(units, level: str = "nominal"):
    """Krippendorff's alpha。units: [{rater_id: value}]，允许缺失。

    nominal: delta(c,k) = 0 if c==k else 1
    ordinal: delta^2(c,k) = (sum_{g=c}^{k} n_g - (n_c+n_k)/2)^2
    """
    if level not in ("nominal", "ordinal"):
        raise FieldStudyError("level 必须是 nominal 或 ordinal")
    units = [u for u in units if len(u) >= 2]
    if not units:
        return {"alpha": None, "n_units": 0, "level": level,
                "note": "没有 ≥2 名标注者的 unit"}
    values, o = _coincidence_matrix(units)
    size = len(values)
    n = [sum(row) for row in o]
    total = sum(n)
    if total <= 1:
        return {"alpha": None, "n_units": len(units), "level": level,
                "note": "巧合矩阵总量不足"}

    def delta(c, k):
        if level == "nominal":
            return 0.0 if c == k else 1.0
        lo, hi = min(c, k), max(c, k)
        inner = sum(n[g] for g in range(lo, hi + 1))
        return (inner - (n[c] + n[k]) / 2) ** 2

    d_o = 0.0
    d_e = 0.0
    for c in range(size):
        for k in range(size):
            d_o += o[c][k] * delta(c, k)
            d_e += n[c] * n[k] * delta(c, k) / (total * (total - 1))
    d_o /= total
    if d_e == 0:
        return {"alpha": None, "n_units": len(units), "level": level,
                "note": "期望分歧为 0（单一取值），alpha 未定义"}
    return {"alpha": 1 - d_o / d_e, "n_units": len(units), "level": level}


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def _wilson_ci(successes: int, total: int, z: float = 1.96):
    if total == 0:
        return None, None
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = z * ((phat * (1 - phat) / total
                   + z * z / (4 * total * total)) ** 0.5) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def choice_metrics(gold: list, pred: list, classes) -> dict:
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
    return {"n": n, "accuracy": correct / n if n else None,
            "accuracy_ci95": [lo, hi],
            "accuracy_ci95_note": "Wilson CI 仅描述性；存在 group clustering "
                                  "时以 cluster bootstrap CI 为主",
            "confusion": confusion, "per_class": per_class}


def score_metrics(gold: list[float], pred: list[float]) -> dict:
    if len(gold) != len(pred):
        raise FieldStudyError("score_metrics: gold/pred 长度不一致")
    n = len(gold)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    errors = [p - g for g, p in zip(gold, pred)]
    return {"n": n, "mae": sum(abs(e) for e in errors) / n,
            "rmse": (sum(e * e for e in errors) / n) ** 0.5,
            "bias": sum(errors) / n}


def roc_auc(scores: list[float], labels: list[bool]) -> float | None:
    if len({bool(x) for x in labels}) < 2:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    pos = sum(1 for x in labels if x)
    neg = len(labels) - pos
    rank_sum = sum(r for r, x in zip(ranks, labels) if x)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def pr_auc(scores: list[float], labels: list[bool]) -> float | None:
    """平均精度（PR-AUC / Average Precision）。确定性处理并列分数。"""
    if len({bool(x) for x in labels}) < 2:
        return None
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    tp = 0
    fp = 0
    prev_recall = 0.0
    ap = 0.0
    positives = sum(1 for x in labels if x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) \
                and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            if labels[order[k]]:
                tp += 1
            else:
                fp += 1
        precision = tp / (tp + fp)
        recall = tp / positives
        ap += (recall - prev_recall) * precision
        prev_recall = recall
        i = j + 1
    return ap


def noul_metrics(labels: list[bool], scores: list[float],
                 calibration: dict | None = None) -> dict:
    """Noul 指标：ROC-AUC + PR-AUC；raw Noul 不算 Brier。

    仅当提供（在 dev partition 拟合并冻结的）calibration mapping 时，
    才计算 Brier / log loss，且调用方必须确保只在 blind partition 上使用。
    """
    if len(labels) != len(scores):
        raise FieldStudyError("noul_metrics: labels/scores 长度不一致")
    n = len(labels)
    result = {"n": n, "auc": None, "pr_auc": None, "prevalence": None,
              "calibrated": calibration is not None, "brier": None,
              "log_loss": None, "caveat": NOUL_CAVEAT}
    if n == 0:
        return result
    result["prevalence"] = sum(1 for x in labels if x) / n
    result["auc"] = roc_auc(scores, labels)
    result["pr_auc"] = pr_auc(scores, labels)
    if calibration is not None:
        a = float(calibration["a"])
        b = float(calibration["b"])
        probs = [1 / (1 + pow(2.718281828459045, -(a * s + b)))
                 for s in scores]
        result["brier"] = sum((p - (1.0 if y else 0.0)) ** 2
                              for p, y in zip(probs, labels)) / n
        result["log_loss"] = -sum(
            (1.0 if y else 0.0) * _log(max(p, 1e-12))
            + (0.0 if y else 1.0) * _log(max(1 - p, 1e-12))
            for p, y in zip(probs, labels)) / n
    return result


def _log(x: float) -> float:
    import math
    return math.log(x)


def fit_noul_calibration(labels: list[bool], scores: list[float]) -> dict:
    """Platt scaling：在 dev partition 上拟合 Noul→probability 映射。

    确定性梯度下降（固定学习率与迭代数，无随机性）。调用方必须把返回值
    写入 freeze manifest，并且只在 blind partition 上用它计算 Brier。
    """
    if len(labels) != len(scores):
        raise FieldStudyError("fit_noul_calibration: 长度不一致")
    if len(set(bool(x) for x in labels)) < 2:
        raise FieldStudyError("校准拟合需要正负两类样本")
    a, b = 0.0, 0.0
    lr, iterations = 0.5, 4000
    n = len(scores)
    for _ in range(iterations):
        grad_a = grad_b = 0.0
        for s, y in zip(scores, labels):
            z = a * s + b
            p = 1 / (1 + pow(2.718281828459045, -z))
            error = p - (1.0 if y else 0.0)
            grad_a += error * s
            grad_b += error
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return {"method": "platt", "a": a, "b": b, "n": n,
            "fitted_on": "dev partition（必须冻结并记录）"}


# ---------------------------------------------------------------------------
# group-level cluster bootstrap CI
# ---------------------------------------------------------------------------


def cluster_bootstrap_ci(item_values: dict, item_groups: dict,
                          metric, *, seed: int = FIELD_STUDY_BOOTSTRAP_SEED,
                          rounds: int = FIELD_STUDY_BOOTSTRAP_ROUNDS
                          ) -> dict:
    """group-level bootstrap：按 group 重采样（同 group 的 item 一起进出）。

    item_values: {item_id: 该指标所需的中间值}
    item_groups: {item_id: group_id}
    metric: (item_ids: list) -> float | None
    """
    groups: dict[str, list[str]] = {}
    for item_id, group_id in item_groups.items():
        groups.setdefault(group_id, []).append(item_id)
    unique = sorted(groups)
    if len(unique) < 2:
        return {"ci95": [None, None], "seed": seed, "rounds": rounds,
                "note": "group 数 < 2，无法做 cluster bootstrap"}
    rng = random.Random(seed)
    stats = []
    for _ in range(rounds):
        sampled = rng.choices(unique, k=len(unique))
        items = [i for g in sampled for i in groups[g]]
        value = metric(items)
        if value is not None:
            stats.append(value)
    if not stats:
        return {"ci95": [None, None], "seed": seed, "rounds": rounds,
                "note": "bootstrap 未产生有效统计量"}
    stats.sort()
    lo = stats[int(0.025 * (len(stats) - 1))]
    hi = stats[int(0.975 * (len(stats) - 1))]
    return {"ci95": [lo, hi], "seed": seed, "rounds": rounds}


# ---------------------------------------------------------------------------
# 分组隔离与冻结
# ---------------------------------------------------------------------------


def check_group_isolation(dev_group_ids, blind_group_ids) -> None:
    dev, blind = set(dev_group_ids), set(blind_group_ids)
    overlap = sorted(dev & blind)
    if overlap:
        raise FieldStudyError(
            f"分组隔离失败：以下 group 同时出现在开发集与盲测集：{overlap}")


def partition_by_group(annotations, dev_group_ids, blind_group_ids):
    groups = {a["group_id"] for a in annotations}
    assigned = set(dev_group_ids) | set(blind_group_ids)
    if groups != assigned:
        raise FieldStudyError(
            "分组划分未覆盖全部 group：未分配="
            f"{sorted(groups - assigned)}，多余={sorted(assigned - groups)}")
    check_group_isolation(dev_group_ids, blind_group_ids)
    return {"dev": [a for a in annotations
                    if a["group_id"] in set(dev_group_ids)],
            "blind": [a for a in annotations
                      if a["group_id"] in set(blind_group_ids)]}


def freeze_dataset(annotations, dev_group_ids, blind_group_ids,
                   dataset_version: str, annotations_path: str | Path,
                   items_path: str | Path | None = None,
                   analysis_plan_version: str = ANALYSIS_PLAN_VERSION
                   ) -> dict:
    """冻结数据集：annotations SHA256 + （可选）item dataset SHA256 + 划分配置。"""
    partition_by_group(annotations, dev_group_ids, blind_group_ids)
    annotations_path = Path(annotations_path)
    manifest = {
        "dataset_version": dataset_version,
        "field_study_schema": ANNOTATION_SCHEMA_VERSION,
        "analysis_plan_version": analysis_plan_version,
        "annotations_file": str(annotations_path).replace("\\", "/"),
        "annotations_sha256": hashlib.sha256(
            annotations_path.read_bytes()).hexdigest(),
        "items": len({a["item_id"] for a in annotations}),
        "annotation_records": len(annotations),
        "independent_observers": len({
            a["annotator_id"] for a in annotations
            if a["role"] == "observer"}),
        "groups": sorted({a["group_id"] for a in annotations}),
        "partition": {"dev": sorted(set(dev_group_ids)),
                      "blind": sorted(set(blind_group_ids))},
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if items_path is not None:
        items_path = Path(items_path)
        manifest["item_dataset_file"] = str(items_path).replace("\\", "/")
        manifest["item_dataset_sha256"] = hashlib.sha256(
            items_path.read_bytes()).hexdigest()
        manifest["item_schema_version"] = ITEM_SCHEMA_VERSION
    return manifest


def verify_freeze(manifest: dict, annotations, annotations_path: str | Path,
                  items_path: str | Path | None = None) -> None:
    """复验冻结清单：任何哈希 / 数量 / 划分 / item 数据集不符即报错。"""
    annotations_path = Path(annotations_path)
    digest = hashlib.sha256(annotations_path.read_bytes()).hexdigest()
    if digest != manifest.get("annotations_sha256"):
        raise FieldStudyError("冻结校验失败：标注文件哈希与清单不符")
    if len({a["item_id"] for a in annotations}) != manifest.get("items"):
        raise FieldStudyError("冻结校验失败：案例数量与清单不符")
    partition_by_group(annotations,
                       manifest["partition"]["dev"],
                       manifest["partition"]["blind"])
    if sorted({a["group_id"] for a in annotations}) != sorted(manifest["groups"]):
        raise FieldStudyError("冻结校验失败：group 清单不符")
    if "item_dataset_sha256" in manifest:
        if items_path is None:
            raise FieldStudyError("冻结清单绑定了 item dataset，但未提供该文件")
        items_digest = hashlib.sha256(Path(items_path).read_bytes()).hexdigest()
        if items_digest != manifest["item_dataset_sha256"]:
            raise FieldStudyError(
                "冻结校验失败：item dataset 哈希与清单不符"
                "（item 文本变化必须导致校验失败）")


# ---------------------------------------------------------------------------
# blind-lock 清单
# ---------------------------------------------------------------------------


def metric_definitions_digest() -> str:
    """主/次指标定义的确定性摘要（改写定义必须改变该值）。"""
    definitions = {
        "choice": "accuracy + Wilson(descriptive) + cluster bootstrap(primary)"
                  " + per-class P/R/F1 + confusion",
        "score": "MAE / RMSE / bias at item level",
        "noul": "ROC-AUC + PR-AUC; Brier/log loss only with frozen "
                "dev-fitted calibration mapping, only on blind partition",
        "irr": "Krippendorff alpha (nominal/ordinal) per observer dimension",
        "abstention": "coverage of unsure/declined/insufficient",
        "reference": "item-level majority (categorical) / lower median "
                     "(ordinal); contested items excluded",
    }
    blob = json.dumps(definitions, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lock_blind_manifest(*, dataset_version: str, annotations_path,
                        items_path, dev_group_ids, blind_group_ids,
                        annotations, commit_sha: str, model_alias: str,
                        calibration: dict | None = None) -> dict:
    """盲测锁定：揭晓 blind 结果之前必须完成并保存该清单。"""
    if not commit_sha:
        raise FieldStudyError("lock_blind_manifest 需要 commit_sha")
    from analyzer import SCHEMA_VERSION as CHAT_SCHEMA

    return {
        "analysis_plan_version": ANALYSIS_PLAN_VERSION,
        "dataset_version": dataset_version,
        "field_study_schema": ANNOTATION_SCHEMA_VERSION,
        "item_schema_version": ITEM_SCHEMA_VERSION,
        "annotations_sha256": hashlib.sha256(
            Path(annotations_path).read_bytes()).hexdigest(),
        "item_dataset_sha256": hashlib.sha256(
            Path(items_path).read_bytes()).hexdigest(),
        "partition": {"dev": sorted(set(dev_group_ids)),
                      "blind": sorted(set(blind_group_ids))},
        "commit_sha": commit_sha,
        "chat_signal_schema": CHAT_SCHEMA,
        "model_alias": model_alias,
        "model_version_observed": None,      # 运行后填写
        "metric_definitions_sha256": metric_definitions_digest(),
        "calibration": calibration,          # 未来若存在，必须冻结
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": BLIND_POLICY,
    }


def verify_blind_manifest(manifest: dict, annotations_path, items_path,
                          annotations) -> None:
    verify_freeze(
        {"dataset_version": manifest["dataset_version"],
         "field_study_schema": manifest["field_study_schema"],
         "annotations_sha256": manifest["annotations_sha256"],
         "item_dataset_sha256": manifest["item_dataset_sha256"],
         "items": len({a["item_id"] for a in annotations}),
         "annotation_records": len(annotations),
         "groups": sorted({a["group_id"] for a in annotations}),
         "partition": manifest["partition"]},
        annotations, annotations_path, items_path)
    if manifest.get("metric_definitions_sha256") != metric_definitions_digest():
        raise FieldStudyError("blind 清单校验失败：指标定义与当前代码不一致")


def record_model_version(manifest: dict, model_version: str) -> dict:
    """运行后记录实际模型版本（揭晓 blind 前不得查看 blind 指标）。"""
    manifest = dict(manifest)
    manifest["model_version_observed"] = model_version
    return manifest


# ---------------------------------------------------------------------------
# 弃答与回忆延迟
# ---------------------------------------------------------------------------


def abstention_summary(annotations) -> dict:
    counts = {status: 0 for status in LABEL_STATUSES}
    total = 0
    for record in annotations:
        for entry in record["labels"].values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            total += 1
    abstain = sum(counts[s] for s in ABSTAIN_STATUSES)
    return {"total_labels": total, "by_status": counts,
            "abstain_coverage": abstain / total if total else None,
            "caveat": ABSTENTION_CAVEAT}


def recall_delay_summary(annotations) -> dict:
    counts = {bucket: 0 for bucket in RECALL_DELAY_BUCKETS}
    total = 0
    for record in annotations:
        bucket = record.get("recall_delay_bucket", "unknown")
        counts[bucket] = counts.get(bucket, 0) + 1
        total += 1
    return {"records": total, "by_bucket": counts, "caveat": RETROSPECTIVE_CAVEAT}


# ---------------------------------------------------------------------------
# 评估（item 级）
# ---------------------------------------------------------------------------


def _predictions_for(outputs: dict, items, reference: dict) -> dict:
    """为每个有 reference 的 item 取唯一一次模型预测（不重复计数）。"""
    model_dim_map = dict(OBSERVER_CHOICE_DIMS)
    model_dim_map.update(OBSERVER_SCORE_DIMS)
    model_dim_map.update(OBSERVER_NOUL_DIMS)
    return model_dim_map


def evaluate(annotations, model: dict, *, dataset_version: str,
             partition: dict | None = None) -> dict:
    """离线评估：item 级指标 + IRR + abstention + cluster bootstrap CI。"""
    outputs = model["outputs"]
    reference = build_item_reference(annotations)
    item_groups = {a["item_id"]: a["group_id"] for a in annotations}

    # 维度映射（observer 维度 → 模型字段）
    dim_map = dict(OBSERVER_CHOICE_DIMS)
    dim_map.update(OBSERVER_SCORE_DIMS)
    dim_map.update(OBSERVER_NOUL_DIMS)

    pairs = {dim: {"gold": [], "pred": [], "item": []}
             for dim in dim_map}
    contested = {dim: 0 for dim in dim_map}
    for item_id, ref in reference.items():
        model_out = outputs.get(item_id)
        for dim in dim_map:
            entry = ref.get(dim)
            if entry is None:
                continue
            if entry["contested"]:
                contested[dim] += 1
                continue
            if model_out is None:
                continue
            field = dim_map[dim]
            if dim in OBSERVER_CHOICE_DIMS:
                value = model_out.get(field, {}).get("choice")
            elif dim in OBSERVER_SCORE_DIMS:
                value = model_out.get(field, {}).get("score")
            else:
                value = model_out.get(field)
            if value is None:
                continue
            pairs[dim]["gold"].append(entry["value"])
            pairs[dim]["pred"].append(float(value)
                                      if dim in OBSERVER_SCORE_DIMS
                                      else value)
            pairs[dim]["item"].append(item_id)

    choice_metrics_out = {}
    for dim in OBSERVER_CHOICE_DIMS:
        gold, pred = pairs[dim]["gold"], pairs[dim]["pred"]
        if not gold:
            continue
        m = choice_metrics(gold, pred, set(gold) | set(pred))
        m["contested_excluded"] = contested[dim]

        def _accuracy(items, _gold=gold, _pred=pred, _item=pairs[dim]["item"]):
            positions = [i for i, it in enumerate(_item) if it in set(items)]
            if not positions:
                return None
            hit = sum(1 for i in positions if _gold[i] == _pred[i])
            return hit / len(positions)

        m["cluster_bootstrap_ci95"] = cluster_bootstrap_ci(
            {it: it for it in pairs[dim]["item"]}, item_groups, _accuracy)
        choice_metrics_out[dim] = m

    score_out = {}
    for dim in OBSERVER_SCORE_DIMS:
        gold, pred = pairs[dim]["gold"], pairs[dim]["pred"]
        if gold:
            score_out[dim] = score_metrics(gold, pred)

    noul_out = {}
    for dim in OBSERVER_NOUL_DIMS:
        gold, pred = pairs[dim]["gold"], pairs[dim]["pred"]
        if gold:
            labels = [bool(g) for g in gold]
            noul_out[dim] = noul_metrics(labels, [float(p) for p in pred])

    irr = {}
    rater_units: dict[str, list] = {}
    for record in annotations:
        if record["role"] != "observer":
            continue
        for dim, entry in record["labels"].items():
            if entry["status"] != "labeled":
                continue
            rater_units.setdefault(dim, []).append(
                {record["annotator_id"]: entry["value"]})
    for dim, values in rater_units.items():
        # 同一 (item, rater) 聚合为 unit：rater -> value
        units: dict = {}
        for record in annotations:
            if record["role"] != "observer":
                continue
            entry = record["labels"].get(dim)
            if not entry or entry["status"] != "labeled":
                continue
            units.setdefault(record["item_id"], {})[
                record["annotator_id"]] = entry["value"]
        unit_list = list(units.values())
        multi = [u for u in unit_list if len(u) >= 2]
        if not multi:
            continue
        level = "ordinal" if dim in OBSERVER_SCORE_DIMS else "nominal"
        alpha = krippendorff_alpha(multi, level=level)
        alpha["note"] = IRR_CAVEAT
        irr[dim] = alpha

    annotated_ids = set(reference)
    sender_intent_records = [a for a in annotations
                             if a["role"] == "sender"
                             and a["labels"].get(INTENT_DIM, {}).get(
                                 "status") == "labeled"]
    perception_records = [a for a in annotations
                          if a["labels"].get(PERCEPTION_DIM, {}).get(
                              "status") == "labeled"]
    receiver_records = [a for a in annotations
                        if a["role"] == "receiver"
                        and a["labels"].get(EXPERIENCE_DIM, {}).get(
                            "status") == "labeled"]

    return {
        "dataset": {
            "field_study_schema": ANNOTATION_SCHEMA_VERSION,
            "analysis_plan_version": ANALYSIS_PLAN_VERSION,
            "dataset_version": dataset_version,
            "items": len(annotated_ids),
            "groups": len(item_groups and {item_groups[i]
                                           for i in annotated_ids}),
            "annotation_records": len(annotations),
            "independent_observers": len({
                a["annotator_id"] for a in annotations
                if a["role"] == "observer"}),
            "partition": (None if partition is None else
                          {name: len({a["item_id"] for a in items})
                           for name, items in partition.items()}),
        },
        "model": {"schema_version": model["schema_version"],
                  "model": model["model"],
                  "model_alias": model.get("model_alias", "unknown"),
                  "source": model["source"]},
        "reference": {"level": "item",
                      "rule": "categorical=binary majority (contested "
                              "excluded); ordinal=lower median",
                      "contested_excluded": contested},
        "abstention": abstention_summary(annotations),
        "recall_delay": recall_delay_summary(annotations),
        "inter_rater": irr,
        "choice_metrics": choice_metrics_out,
        "score_metrics": score_out,
        "noul_metrics": noul_out,
        "separate_layers": {
            "sender_intent_records": len(sender_intent_records),
            "receiver_experience_records": len(receiver_records),
            "participant_perception_records": len(perception_records),
            "note": "这三层是独立数据，不与 observer gold 混合，也不参与"
                    "模型对分指标",
        },
        "unmatched": {
            "annotations_without_output": sorted(
                annotated_ids - set(outputs)),
            "outputs_without_annotation": sorted(
                set(outputs) - annotated_ids),
        },
        "caveats": [NON_CLINICAL_CAVEAT, NOUL_CAVEAT, ABSTENTION_CAVEAT,
                    RETROSPECTIVE_CAVEAT, IRR_CAVEAT, BLIND_POLICY],
    }


def format_report(report: dict) -> str:
    lines = [
        "=== Field Study evaluation（offline, deterministic） ===",
        f"dataset: {report['dataset']['dataset_version']} "
        f"(plan {report['dataset']['analysis_plan_version']}, "
        f"annotation schema {report['dataset']['field_study_schema']})",
        f"items={report['dataset']['items']} "
        f"groups={report['dataset']['groups']} "
        f"records={report['dataset']['annotation_records']} "
        f"independent_observers="
        f"{report['dataset']['independent_observers']}",
        f"model: {report['model']['model']} "
        f"(alias {report['model']['model_alias']}, "
        f"schema {report['model']['schema_version']})",
        f"abstain coverage: {report['abstention']['abstain_coverage']}",
        f"recall delay buckets: {report['recall_delay']['by_bucket']}",
    ]
    if report["inter_rater"]:
        lines.append("inter-rater reliability (Krippendorff alpha):")
        for dim, alpha in sorted(report["inter_rater"].items()):
            lines.append(f"  {dim} ({alpha['level']}): "
                         f"{alpha['alpha']} n_units={alpha['n_units']}")
    if report["choice_metrics"]:
        lines.append("choice metrics (item level):")
        for dim, m in sorted(report["choice_metrics"].items()):
            boot = m["cluster_bootstrap_ci95"]["ci95"]
            lines.append(f"  {dim}: n={m['n']} acc={m['accuracy']:.3f} "
                         f"bootstrap_ci95=[{boot[0]:.3f},{boot[1]:.3f}] "
                         f"(seed {m['cluster_bootstrap_ci95']['seed']}, "
                         f"{m['cluster_bootstrap_ci95']['rounds']} rounds)")
    if report["score_metrics"]:
        lines.append("score metrics (item level):")
        for dim, m in sorted(report["score_metrics"].items()):
            lines.append(f"  {dim}: n={m['n']} mae={m['mae']:.3f} "
                         f"rmse={m['rmse']:.3f} bias={m['bias']:+.3f}")
    if report["noul_metrics"]:
        lines.append("noul metrics（排序指标，非概率；未校准）:")
        for dim, m in sorted(report["noul_metrics"].items()):
            lines.append(f"  {dim}: n={m['n']} auc={m['auc']} "
                         f"pr_auc={m['pr_auc']} prevalence="
                         f"{m['prevalence']}")
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
