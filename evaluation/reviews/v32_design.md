# v3.2 设计方案（仅方案，本轮不实施）

输入：v3.1 三组真实评估（main34 / distancing / phase2）+ 34 例修订集离线复算
+ 对照案例集预登记。原则继承：不新增凭空“读心”维度；优先改问题描述；
可观察行为优先；未经校准不得把 confidence 当概率。

## 一、建议 v3.2 只做一件事：投入度的会话层语义

**问题**：v3.1 真实评估显示 engagement 把「拒绝当前话题但主动转向」与
「希望结束整个交流」混同：

- `p2_topic_refusal_fresh`（“这个真不想聊了，说点别的吧”）engagement **0.33**
- `dis_topic_refusal` 0.77、`dis_plan_later` 0.57
- 对照：`cs_topic_refusal_redirect`（拒绝 + 追问对方近况）预期 2~4

即：拒绝单个话题后**主动开启新话题 / 追问对方**的投入信号没有被计入，
只要消息里出现“不想聊”，投入度就被拉低。

**设计（描述级，不新增问题）**：细化 `engagement` 的 instructions：

> 投入度衡量“是否在维持这条对话线”，不是“是否同意当前话题”。
> 拒绝 / 搁置当前话题但主动开启新话题、追问对方近况、提议改天再聊，
> 都属于维持对话线，投入度不因此降低；只有明确结束交流、连续无回应式
> 短回复、或明确表示不想继续聊，才计入低投入。

**可观察正反例**（已预登记于 `cases_contrast_v3.2.json`）：

| 正例（应中高投入） | 反例（应低投入） |
|---|---|
| `cs_topic_refusal_redirect` 拒绝话题+追问 | `cs_true_contact_refusal` 明确别再联系 |
| `cs_conversation_close_plan` 收尾+约定 | `j_perfunctory_hmm` 类连续敷衍 |
| `cs_explicit_busy_with_plan` 忙碌+后续计划 | `dis_single_terse`（孤立短回复，需歧义容忍） |

**成本/兼容**：纯 instructions 变更 → bump `chat-signal-v3.2`；每目标仍 1 次
请求、仍 9 问；engagement 进 formula → 分数会变，必须重跑全部案例集并做
约束级对比（不得宣称同尺度提升）；旧缓存自然 miss。

## 二、其余机制：独立后续任务，v3.2 不同时修改

| 机制 | 现状证据 | 结论 |
|---|---|---|
| 关心分层（礼貌关心 / 情绪认可 / 理解处境 / 行动支持） | v3.1 warmth 对关心语停在 2.9（i_disclosure 2.98、u_long 2.96），`cs_care_*` 三级预期待验证 | **v3.3+ 任务**。先跑对照案例集取得三级 warmth 实测分布，再决定是否改写 warmth instructions；不得机械提高全部 warmth |
| 歧义与 confidence | 无线索调侃仍被判 annoyed（d_tease 0.02）；Choice confidence 已返回但**未校准** | **v3.3+ 任务**。不得把 confidence 解释为真实准确概率；可先在 UI/报告中以“模型自报置信度”展示，收集校准数据后再定 |
| evidence 方向分离 | 浪漫拒绝 ev=4.00 与话题拒绝 ev=3.89 并存，负面方向证据不稳定（r_evidence 3.98→2.63） | **v3.3+ 任务**：用 `cs_romantic_boundary_continue` 等对照先观测，再决定是否细化 instructions |
| 新维度（回应性/互惠） | 已论证需改 state 语义或违背原则 | **不做**（见 phase2_design.md） |

## 三、风险与验收

- 风险：engagement 语义收窄后，“简短但正常”的消息（“嗯”“好”） engagement
  可能进一步下降——验收必须看 `dis_single_terse`、`j_perfunctory_hmm`、
  `cs_care_polite_daily` 等“短但不敷衍”约束不得回归。
- 验收口径：对照案例集 13 例全量约束 + main34_v3.1 修订集 + distancing
  两版的约束级对比；任一新增失败都需人工判定是语义还是阈值问题。
- 禁止：为让数字好看调阈值；把 confidence 当概率；同 schema 内混用不同
  案例集比较通过率。
