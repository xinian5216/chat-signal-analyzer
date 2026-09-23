# Field Study — 心理学相关行为验证（试点准备）

本目录是**独立的真实反馈验证设施**，与 `evaluation/` 的虚构案例基准
（benchmark）分开：benchmark 衡量“模型是否满足人工定义约束”，
field study 衡量“模型输出与真实人类标注之间的一致性”。

## 范围与免责声明（所有文档与报告必须携带）

- 本项目是**聊天行为分析工具**：分析写下来的文字，**不提供临床心理
  诊断**，不声称能直接读取他人的真实心理；
- 人工标注区分三层：**发送者当时的交流意图** / **接收者实际感受到的
  互动** / **观察者仅根据文字能够识别的行为**；
- 允许「不确定 / 不愿回答 / 信息不足」：不强迫所有案例有确定标签，
  弃答不计为错误；
- 模型的 Noul 输出是**模型内部的序数分数**，不是现实事件概率；
- 本设施**完全离线**：`field_study.py` 不调用 Jev、不联网；
- **本轮未启动任何招募**，未经单独批准不得收集真实个人聊天。

## 文件

| 文件 | 作用 |
|---|---|
| `PROTOCOL.md` | 研究方案：设计、参与者角色、纳入/排除、指标、分组冻结、分析计划 |
| `CONSENT_ANNOTATOR.md` | 标注者知情同意模板 |
| `CONSENT_PARTICIPANT.md` | 聊天参与者（发送方/接收方）知情同意模板 |
| `DEIDENTIFICATION.md` | 脱敏规范（什么可以进数据集、什么绝对不行） |
| `WITHDRAWAL_AND_DELETION.md` | 退出与数据删除流程 |
| `annotation_schema.json` | 标注记录的 JSON Schema |
| `item_schema.json` | 脱敏 item dataset 的 JSON Schema（禁隐私字段） |
| `PROTOCOL.md` | 研究方案 v1.1（含 pilot/confirmatory 分离与 blind-lock） |
| `ANNOTATION_GUIDE.md` | 标注者 rubric：操作定义、0~4 anchors、unsure/insufficient 条件 |
| `CONSENT_ANNOTATOR.md` / `CONSENT_PARTICIPANT.md` | 知情同意模板 |
| `DEIDENTIFICATION.md` / `WITHDRAWAL_AND_DELETION.md` | 脱敏规范 / 退出删除流程 |
| `examples/annotations_synthetic.json` | 合成标注示例（供测试/演示，无真实数据） |
| `examples/items_synthetic.json` | 合成 item dataset 示例 |
| `examples/model_outputs_synthetic.json` | 合成模型输出示例（字段格式与真实报告一致） |

## 使用方式（全部离线）

```bash
python -X utf8 -c "import field_study as fs; ..."
```

- `fs.load_items(path)` / `fs.load_annotations(path)` /
  `fs.load_model_outputs(path)`：加载并校验（item 与标注均隐私白名单）；
- `fs.render_item_text(item)`：**模型输入与观察者文本的唯一渲染来源**
  （同一冻结 item dataset 派生，不得分别手工复制）；
- `fs.build_item_reference(...)`：item 级 reference（categorical 多数决 /
  ordinal 较低中位数；`contested` 排除并计数）——多观察者**不会**造成
  n 膨胀；
- `fs.partition_by_group(...)` / `fs.check_group_isolation(...)`：按
  group 划分 dev/blind（同一聊天双方不得跨集）；
- `fs.freeze_dataset(...)` / `fs.verify_freeze(...)`：冻结与复验
  （item + annotations 双哈希，划分配置，schema 版本）；
- `fs.lock_blind_manifest(...)` / `fs.verify_blind_manifest(...)`：
  blind-lock（commit SHA / chat-signal schema / 模型 alias / 指标定义哈希
  / 校准映射）；
- `fs.evaluate(...)` + `fs.format_report(...)` + `fs.save_report(...)`：
  指标与报告。

v1.1 指标口径：
- Choice：item 级 accuracy + **group bootstrap CI95（主，固定 seed 与
  次数并记录）** + Wilson CI（仅描述性）+ 混淆矩阵 + 逐类 P/R/F1；
- Score：item 级 MAE / RMSE / 偏差；
- Noul：**ROC-AUC + PR-AUC**；raw Noul **不**计算 Brier / log loss；
  Brier/log loss 仅在 dev 拟合并冻结 Platt calibration 之后、只在 blind
  partition 上计算；
- **Krippendorff's alpha**（nominal / ordinal，允许多标注者与缺失）：
  把 observer 标签称为 human reference 的前置条件；alpha 低时报告必须
  明示模型一致率的解释力限制；
- 弃答覆盖率（unsure/declined/insufficient 不计为错误、不进指标分母）、
  recall_delay_bucket 分布与回忆偏差声明；
- 报告写清 items / groups / annotation records / independent observers；
  sender / receiver / participant perception 三层独立统计，不与 observer
  gold 混合。

