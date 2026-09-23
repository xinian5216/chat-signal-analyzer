# cases_distancing_v3.1 修订记录（逐项：旧值 → 新值 → 理由）

依据 `phase2_design_review.md` 的人工复核结论：**只修订经复核确认的案例
标定错误**，校准差异与观察项一律保留原值。原始
`cases_distancing.json` 与其真实成绩（v3_distancing_real*.json）**不动**。

| # | 案例 | 字段 | 旧值 | 新值 | 理由 |
|---|---|---|---|---|---|
| 1 | dis_romantic_boundary | relationship_evidence_strength.max | 3 | 4 | 复核确认：`RELATIONSHIP_EVIDENCE_INSTRUCTIONS` 原文即规定“拒绝、关系表达等消息信息量高”。“我有喜欢的人了，我们还是当朋友吧”是明确的关系边界陈述，ev=4.00 符合题目定义；旧上限把“信息量高”误当作“方向积极”。 |
| 2 | dis_romantic_boundary_soft | relationship_evidence_strength.max | 3 | 4 | 同上：“只把你当朋友”是明确的双方关系定义句，ev=3.98 正确。 |
| 3 | dis_romantic_boundary_soft | intent.allowed | [share_opinion, show_care, other] | 追加 distance | intent 题目的 `distance` 定义就是“回避、拒绝、主动拉开距离”；浪漫边界=拒绝，模型给出 distance 是按题目定义作答（复核确认案例过窄）。 |
| 4 | dis_topic_refusal | intent.allowed | [share_opinion, other, end_topic] | 追加 distance | 同上：话题拒绝属于“拒绝”（复核确认）。 |
| 5 | dis_vague_no_desire | intent.allowed | [share_opinion, end_topic, other] | 追加 distance | 同上：“最近不太想聊天”属回避性表述（复核确认）。 |
| 6 | dis_topic_close_reengage | emotion.allowed | [calm, happy, other] | 追加 curious | 复核结论“允许集未含属设计疏漏，模型不算误判”：“话说你上次那事后来怎么样了”带探询口吻，curious 可辩。 |

**明确不修订**（复核结论为校准差异/观察项，保留原值并记录在案）：

| 案例 | 约束 | 模型值 | 处置 |
|---|---|---|---|
| dis_topic_refusal | evidence ≤3 | 3.89 | 观察项：话题级拒绝的关系信息量是否达高级，留待 v3.1 真实运行验证 |
| dis_topic_close_reengage | evidence ≤2 | 2.92 | 轻微校准差，保留 |
| dis_vague_no_desire | evidence ≤3 | 3.93 | “略高但可辩”，保留 |
| dis_plan_later / dis_topic_refusal | engagement ≥1 | 0.52 / 0.81 | 模糊语境保守值，校准观察，保留 |
| dis_romantic_boundary_soft | warmth ≥1 | 0.74 | 拒绝语境 warmness 压低，校准观察，保留 |

新文件 `evaluation/cases_distancing_v3.1.json` 与
`evaluation/fixtures/baseline_v3.1_distancing.json`；本记录随案例集一起
版本化，后续任何再修订必须另建 v3.2 并追加记录。
