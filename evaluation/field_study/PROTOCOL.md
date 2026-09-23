# Field Study 研究方案 v1.1（Scientific Hardening）

状态：**试点准备（未启动招募）**。v1.1 修正统计学与数据冻结问题，使设施
真正适合后续小规模人工验证。执行前不得收集任何真实个人聊天数据。

## 0. 本版修订摘要（相对 v1）

1. item 级指标：同一 item 的多个 observer 标注不再各自计为独立模型样本
   （消除伪重复）；
2. group-level cluster bootstrap CI 为主不确定性指标（固定 seed=20260923、
   2000 次，写入报告）；Wilson CI 仅作描述性参考；
3. 引入 Krippendorff's alpha（nominal / ordinal，自实现，允许缺失与多
   标注者）作为“称人类标注为 human reference”的前置条件；
4. Noul：raw Noul 只报 ROC-AUC / PR-AUC；Brier / log loss 必须先 dev
   拟合 Platt calibration 并冻结映射、只在 blind 上计算；
5. 新增脱敏 item dataset（`item_schema.json`），模型输入与观察者文本由
   同一冻结数据集 + 同一渲染函数派生；freeze manifest 同时绑定双方哈希；
6. 角色-标签合法性代码强制（sender/receiver/observer/participant 层分离）；
7. blind-lock 清单（commit SHA / schema / 模型 alias / 指标定义哈希 /
   校准映射）；
8. sender 回忆延迟分桶与偏差声明；
9. pilot 与 confirmatory 验证分离。

## 1. 目的与非目标

检验 SignalLens 的结构化判断与**真实人类标注**的一致性，并量化弃答与
不确定性。不提供临床心理诊断，不声称读取他人真实心理，不用回复速度推断
感情，不把 Noul 当作现实事件概率。

## 2. 三层（+1）标注层

| 层 | 提供者 | 说明 |
|---|---|---|
| sender ground truth | 发送者本人 | `sender_intent`，一条 item 最多一个有效发送者标签；**事后自我报告，有回忆偏差** |
| receiver experience | 接收者本人 | `receiver_felt_experience`；与模型 gold 分开 |
| participant perception | 双方均可 | `participant_perception`；单独层，**绝不与 observer gold 混合** |
| observer reference | 独立观察者（每 item ≥3 人） | 模型对分的 human reference |

角色-维度合法性由 `field_study.load_annotations` 强制。

## 3. item 级 reference 与指标口径

- categorical/binary：每 item 多数决；无多数 → `contested`，排除并计数；
- ordinal Score：labeled 值的**较低中位数**；
- 模型预测每 item 只计一次（n = items，不是 annotation records）；
- 报告必须写清：items 数、groups 数、annotation records 数、independent
  observers 数。

## 4. 不确定性：clustering 优先

同一聊天双方（group）可能贡献多条 item，不能假设为完全独立。主 CI 为
group-level bootstrap（seed 与次数固定、记录于报告）；Wilson CI 仅描述性。
group 数 < 2 时 bootstrap 不可用，报告明示。

## 5. 人类一致性前置条件

把 observer 标签称为 human reference 之前，每个多标注者维度必须报告
Krippendorff's alpha：
- categorical/binary：nominal；0~4 Score：ordinal；
- `unsure`/`declined`/`insufficient`/missing 不计入 alpha 计算；
- **alpha 低 → 模型与“人类真值”的一致率解释力有限**（报告明示）；
- 实现说明：alpha 为自实现（闭式一致性矩阵），回归测试锚定手工计算值；
  未引入第三方依赖。

## 6. Noul 评估策略

- 主指标：ROC-AUC + PR-AUC（正负不平衡时看 PR-AUC）；
- raw Noul **不**计算也不解释 Brier / log loss；
- 若要测校准：在 dev partition 拟合 Platt calibration（确定性固定迭代），
  将该映射写入 freeze manifest，**只**在 blind partition 计算 Brier /
  log loss。

## 7. 脱敏 item dataset 与冻结

- item 字段白名单见 `item_schema.json`；禁真实昵称/联系方式/精确时间/
  媒体内容；
- 模型输入与观察者文本由 `render_item_text(item)` 从同一冻结 item
  dataset 派生；
- freeze manifest 绑定：item dataset SHA256 + annotations SHA256 +
  group 清单 + 划分配置 + annotation schema version + analysis-plan
  version；任何 item 文本变化必须使校验失败。

## 8. Blind-lock 流程

揭晓 blind 结果前必须锁定：dataset_version、双方哈希、dev/blind 划分、
SignalLens commit SHA、chat-signal schema version、模型 alias、（运行后）
实际模型版本、主/次指标定义哈希、校准映射（若存在）。
一旦查看 blind performance：该 blind set 退休为 development evidence；
基于盲测结果修改模型后，必须用新的 blind groups 才能再次声称独立盲测。

## 9. 回忆偏差

sender_intent 允许且只允许粗粒度 `recall_delay_bucket`；报告按桶给出
样本数，并声明 retrospective self-report 不是无误差的心理真值。

## 10. Pilot 与 confirmatory 分离

- **pilot（feasibility）**：≥8 groups 只用于验证招募、脱敏、rubric、
  一致性、弃答率与流程可行性；**不得据此宣传“科学准确率”**；
- **confirmatory blind validation**：后续新的独立 groups；
- 正式样本量不拍脑袋：pilot 之后依据类别分布、alpha、abstention 与
  group 内相关（ICC / 组内相关系数）再制定样本量方案并写入新版本
  analysis plan。

## 11. 纳入 / 排除 / 停止条件

（与 v1 相同：双方成年且双签同意；片段 10~500 字；排除医疗/法律/财务/
危机/雇佣司法场景、不可脱敏、参与者退出。）
隐私风险或系统性 API 错误：立即停止、记录、清理。
