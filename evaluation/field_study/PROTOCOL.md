# Field Study 研究方案（Pilot Protocol v1）

状态：**试点准备（未启动招募）**。本方案经批准后方可执行；执行前不得
收集任何真实个人聊天数据。

## 1. 目的

检验 SignalLens 的结构化判断（emotion / intent / warmth / engagement /
special_attention / relational_ease / romantic_signal / distancing_signal）
与**真实人类标注**的一致性，并量化弃答与不确定性，而不是继续提高虚构
案例的通过率。

## 2. 非目标（明确不做）

- 不提供临床心理诊断，不评估任何人的心理健康状况；
- 不声称读取他人真实心理；所有标注仅针对“写下来的文字”；
- 不用回复速度推断感情；不用时间间隔做关系判断；
- 不把模型 Noul 输出解释为现实事件概率。

## 3. 参与者与角色

| 角色 | 人数（建议） | 任务 |
|---|---|---|
| 聊天参与者（发送方） | ≥ 8 组 | 提供自己聊天片段（脱敏后）+ 事后标注自己当时的交流意图 |
| 聊天参与者（接收方） | 同上（配对） | 标注自己当时的感受（可选） |
| 独立观察标注者 | ≥ 3 人 | 仅根据文字做行为层标注；不接触参与者 |

同一聊天双方的片段记为一个 `group_id`。

## 4. 数据与脱敏

见 `DEIDENTIFICATION.md`：数据集只含脱敏文本、粗粒度时间桶、
`group_id`/`item_id`；禁止 raw_speaker、真实昵称、联系方式、精确时间、
媒体内容。标注记录字段白名单见 `annotation_schema.json`。

## 5. 纳入 / 排除标准（预先记录）

纳入：
- 双方均为自愿参与的成年人，且聊天双方均签署同意书；
- 片段长度 10~500 字，含至少一个可识别的回合；
- 脱敏后不含隐私硬伤（见脱敏规范）。

排除：
- 涉及未成年人、医疗/法律/财务建议、危机与自伤他伤内容、雇佣或司法
  场景的片段；
- 无法脱敏到规范的片段；
- 参与者要求退出的片段。

## 6. 分组与冻结（防泄漏）

- 以 `group_id`（聊天双方）为最小单位划分：同一 group 的片段只能全部
  进入开发集或全部进入盲测集（`fs.check_group_isolation` 强制）；
- 划分配置、纳入/排除标准、预期分析指标在看到盲测结果**之前**写入
  冻结清单（`fs.freeze_dataset`：标注文件 SHA256 + group 清单 + 划分）；
- 盲测结果产生后，禁止反向调整划分、阈值或指标定义；任何后续修改必须
  以新版本数据集记录（`dataset_version` 递增）。

## 7. 预期分析指标（预先登记）

| 层 | 维度 | 指标 |
|---|---|---|
| 观察层 | emotion / intent | accuracy + Wilson 95% CI + 混淆矩阵 + 逐类 P/R/F1 |
| 观察层 | warmth / engagement / special_attention / relational_ease | MAE / RMSE / 偏差 |
| 观察层 | romantic / distancing（二元标注） | AUC / Brier（附非概率告示） |
| 意图层 | sender_intent ↔ intent | 同上 Choice 指标 |
| 体验层 | receiver_felt_experience | 描述统计（无模型对应项，不对分） |
| 数据质量 | 全部标签 | 弃答覆盖率（unsure/declined/insufficient） |

最小样本：每个 Choice 维度至少 30 个 labeled 样本才报告 CI；Noul 维度
至少 10 正 + 10 负才计算 AUC；不足则明确记“样本不足”。

## 8. 弃答与冲突处理

- 弃答是合法标注，不是缺失数据；
- 多标注者分歧（如三人中两人 uncertain）按 schema 记录每条标注者的
  状态，报告分别给出按标注者与按条目的弃答率；
- 缺模型输出的案例计入 `unmatched`，不计为错误。

## 9. 版本与可复现

- 标注 schema 版本：`field-study-annotation-v1`；
- 模型输出必须携带其 `schema_version`（如 `chat-signal-v3.2`）与模型
  版本（如 `jev-1.13.0`），写入报告；跨版本结果不得混称为同一模型；
- 报告为确定性 JSON（无时间戳），相同输入必须字节一致。

## 10. 伦理与停止条件

- 知情同意见两个 CONSENT 模板；退出与删除见
  `WITHDRAWAL_AND_DELETION.md`；
- 若参与者退出导致某 group 不完整，整组剔除并在报告中记录；
- 任何阶段发现隐私泄漏风险：立即暂停、删除、复盘后再决定是否继续。
