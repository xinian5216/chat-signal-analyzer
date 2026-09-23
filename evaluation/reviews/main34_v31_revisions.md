# cases_main34_v3.1 修订日志

依据 v3.1 三组真实评估的人工复核。原则：不为迎合本轮模型输出而调整阈值；
每条修订区分为**语义修正 / 合理歧义 / 探索性校准**，仅记录项不动阈值。
原始 cases.json 保留不动。

## 一、应用的修订（12 条，涉及 9 个案例）

| 案例 | 字段 | 原值 | 新值 | 依据 | 类型 |
|---|---|---|---|---|---|
| l_friendzone | expectations.distancing_signal | {"min_probability": 0.5, "max_probability": 1.0} | （移除该维度预期） | 浪漫边界不等于关系疏离（v3.0 五分类第 4 类）；原 min_probability=0.5 写于旧定义之下，属过期语义 | 语义修正 |
| m_distant_stepback | expectations.distancing_signal | {"min_probability": 0.6, "max_probability": 1.0} | （移除该维度预期） | “最近有点忙，可能没空聊了”同时可读作临时忙碌（非疏离）与关系后撤（疏离），单句证据不足；不设 distancing 约束，歧义观察 | 合理歧义 |
| r_evidence_high_negative | expectations.distancing_signal | {"min_probability": 0.5, "max_probability": 1.0} | （移除该维度预期） | “这个问题不想再聊了”是话题级边界（第 3 类），不是对持续互动的疏离；原 min 0.5 系旧定义残留 | 语义修正 |
| r_evidence_high_negative | expectations.relationship_evidence_strength | {"min": 3, "max": 4} | {"min": 2, "max": 4} | 明确边界+要求停止追问是中高等关系信息量（负面方向）；v3.1“信息量≠方向”下 2~4 均合理，取代原 3~4 硬下界 | 语义修正 |
| d_tease_no_flirt | expectations.emotion | {"allowed": ["teasing", "happy", "calm"]} | {"allowed": ["teasing", "happy", "calm", "annoyed", "other"]} | 文本无语气线索：熟人调侃与真实不满均成立；annoyed 亦为合理解读 | 合理歧义 |
| d_tease_no_flirt | expectations.intent | {"allowed": ["tease"]} | {"allowed": ["tease", "share_opinion", "other"]} | 同上：调侃与陈述不满都是合理意图 | 合理歧义 |
| d_tease_no_flirt | expectations.warmth | {"min": 1, "max": 3} | {"min": 0, "max": 3} | 歧义下 warmth 0~3 均可辩（旧 min=1 只在调侃解读下成立） | 合理歧义 |
| d_tease_no_flirt | expectations.relationship_evidence_strength | {"min": 0, "max": 2} | {"min": 0, "max": 3} | 歧义下证据量中等；旧 max=2 只在调侃解读下成立 | 合理歧义 |
| h_topic_continue | expectations.emotion.allowed | ["happy", "calm", "other"] | ["happy", "calm", "other", "teasing"] | target 含戏谑式展开（“你要是在肯定想撸”），teasing 是合理情绪标签；原允许集过窄 | 语义修正 |
| w_work_send | expectations.intent.allowed | ["confirm_understanding", "explain"] | ["confirm_understanding", "explain", "continue_topic"] | “整理好就发你”推进了事务线程，continue_topic 与 confirm_understanding/explain 并列合理 | 合理歧义 |
| o_tired_low_mood | expectations.intent.allowed | ["other", "share_opinion", "perfunctory"] | ["other", "share_opinion", "perfunctory", "share_personal"] | “今天很累”是个人状态陈述，share_personal 本就是题目选项定义内的合理标签 | 语义修正 |
| z_media_voice_context | expectations.intent.allowed | ["show_care", "other", "perfunctory"] | ["show_care", "other", "perfunctory", "end_topic"] | “晚点再说”在字面上结束当前回合，end_topic 合理；原允许集未覆盖 | 合理歧义 |

## 二、仅记录、未改动（10 条）

| 案例 | 字段 | 说明 |
|---|---|---|
| c_shared_meme | warmth.min=2 | 模型 1.99 属贴线抖动；不为单轮输出放宽阈值 |
| i_disclosure_support | warmth.min=3 | 模型 2.98 贴线；关心语 warmth 校准列为 v3.2 观察 |
| u_disclosure_burst_long | warmth.min=3 | 同上 2.96 贴线 |
| y_slow_friendly_reply | engagement.min=2 | 模型 1.96 贴线 |
| q_warm_high_evidence_low | warmth.min=3 | 模型 2.07；同上观察 |
| t_burst_context | warmth.min=2 | 模型 1.78；事务性约定的 warmth 预期保留 |
| p_ease_high_romantic_low | relational_ease 4~4 | 复核已认定该阈值过严；是否修订待人工单独批准 |
| g_remembers_detail | special_attention.min=3 | 复核争议项（记住细节是否超普通朋友基线）；保留 |
| n_conflict_unhappy | special_attention.max=2 | 模型 2.28 轻微越界；观察 |
| e_flirt_goodnight | intent.allowed | 模型给 share_opinion 与消息性质不符，判为 borderline 误标；不为通过而放宽 |

## 三、影响约束数量

- 应用的修订直接影响约束：情感允许集 ×2、意图允许集 ×4、score 区间 ×2、
  移除 dimension 预期 ×3、evidence 区间 ×1（合计 12 条 edits）。
- 注意：移除 l_friendzone / m_distant_stepback / r_evidence_high_negative 的
  distancing 约束**不是**为了让模型通过，而是原约束写于 v3.0 之前的旧定义；
  在现行语义下这些 target 分属“浪漫边界 / 临时忙碌 / 话题级边界”，本就应为 false。

## 四、配套新增

- evaluation/cases_contrast_v3.2.json：为 m_distant_stepback 的歧义结论补充
  “明确忙碌 vs 明确关系后撤”等对照案例（见该文件头部说明），预期已冻结。
