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
| `examples/annotations_synthetic.json` | 合成标注示例（供测试/演示，无真实数据） |
| `examples/model_outputs_synthetic.json` | 合成模型输出示例（字段格式与真实报告一致） |

## 使用方式（全部离线）

```bash
# 生成派生/盲测报告
python -X utf8 -c "import field_study as fs; ..."
```

- `fs.load_annotations(path)` / `fs.load_model_outputs(path)`：加载并校验；
- `fs.partition_by_group(...)` / `fs.check_group_isolation(...)`：按参与者
  组划分开发集/盲测集（**同一 group 不得跨集**）；
- `fs.freeze_dataset(...)` / `fs.verify_freeze(...)`：冻结与复验；
- `fs.evaluate(...)` + `fs.format_report(...)` + `fs.save_report(...)`：
  指标、混淆矩阵、弃答覆盖率、样本量允许时的 Wilson 置信区间。

指标口径：Choice 维度报 accuracy + Wilson 95% CI + 混淆矩阵 + 逐类
P/R/F1；Score 维度报 MAE/RMSE/偏差；Noul 维度报 AUC/Brier 并附带
“非概率”告示；弃答（unsure/declined/insufficient）单独统计覆盖率，
不计入指标分母。
