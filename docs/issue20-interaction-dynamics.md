# Issue #20 设计说明：Interaction Dynamics —— turn / sequence 级互动结构事件

- Issue：xinian5216/chat-signal-analyzer#20（v0.4 P1 Algorithm）
- 分支：`issue-20-interaction-dynamics`（base：`main` @ `0b3684e`，即 #19 合并后）
- 输入契约：**有序本地消息**（time-sorted；与 analyzer results 同 index 空间）
  + 现有 Jev structured results（`chat-signal-v3.3`，**不改 schema / 不改九问**）
- 输出契约：`interaction_dynamics.build_interaction_events()` 的
  `interaction-dynamics-v1` dict → Profile v2 `interaction_events` 槽位 +
  `salience`（`source = "interaction_event"`）+ behavior 人工审核候选适配器
- 研究/验收工具：`interaction_diagnostics.py` +
  `scripts/run_interaction_diagnostics.py`（离线、确定性、0 Jev、0 网络）

## 0. 目标与非目标

**目标**：第一次可靠描述“**互动是怎么发生的**”——几条连续消息组成了什么互动
结构；并对无法可靠判断的序列语义明确说“不知道”。

**非目标（硬禁止）**：Jev questions / schema / Context Builder / scoring /
Noul transform / #19 阈值 / 新 overall / 关系健康分 / 尊重分 / 喜欢概率 /
人格分析 / 回复速度推断 / 图片视频分析 / personal baseline / 真实聊天 /
生成式 LLM / embedding。全部本地、deterministic-first、可回溯、可解释、保守。

## 1. 总原则：Observable event ≠ Psychological conclusion

允许“TA 在用户回答后连续提出了两次相关问题”；禁止“TA 很在乎你”。
允许“用户拒绝后，TA 再次提出了同一要求”；禁止“TA 不尊重你这个人”。
允许“TA 在长时间间隔后首先发送消息重新开始互动”；禁止“TA 一直在想你”。

事件描述**可观察结构**，绝不描述人格、意图、感情。全部事件文案是确定性模板；
测试对断言式心理措辞（在乎 / 想你 / 很尊重你 / 人格类型 / 关系健康 …）做黑名单
断言（否定式免责声明“不得据此推断…”按仓库约定不禁）。

## 2. 架构

```
有序本地消息（analysis_messages / messages）+ Jev results
        │
        ▼
interaction_dynamics.build_interaction_events()      # pure / causal-only
        │
        ├── events（auto_supported | review_required | deferred）
        ├── observations（reciprocity 等结构观察）
        └── capabilities（能力矩阵）
        │
        ├── salience.build_salience(results, interaction=…)   # #19 通道
        │       └── auto_supported → source="interaction_event" 的 salient/counter 事件
        ├── relationship_profile.build_profile(…, interaction=…)
        │       └── dimensions[*].interaction_events + boundary_pressure 按结构重写
        └── interaction_behavior_candidates(interaction, messages)   # behavior 适配器
                └── 并入现有 pending 审核队列（身份走 behavior.event_identity）
```

- **不写 DB、不新增 schema**：互动事件默认 ephemeral；要长期保存必须走
  behavior 现有“候选 → 用户确认/排除 → friend history”流程（§12）。
- **causal-only**：每个事件只使用 `<= window.end_index` 的消息
  （prefix invariance，§4）。

## 3. 事件 schema（完整示例）

```python
{
  "event_type": "followup_sequence",           # 见 §5~§10 事件类表
  "source": "interaction_event",
  "dimension": "initiative",                   # initiative | care | boundary_pressure | observation
  "direction": "supporting",                   # supporting | counter | observation
  "actor": "TA",                               # TA | 用户 | both
  "window": {"start_index": 1, "end_index": 4,
             "anchor_indices": [1, 2, 3, 4]},  # anchor = 实际支撑事件的证据消息
  "time_basis": "full_timestamp",              # full_timestamp | ordered_only | mixed
  "trigger": {"rule": "statement_ask_answer_ask",
              "observations": ["用户陈述（消息 #2）", "TA 提问（消息 #3，Jev ask_information 明确）",
                               "用户回应（消息 #4）", "TA 再次提问（消息 #5）"]},
  "strength": "observable",                    # observable | explicit
  "review_status": "auto_supported",           # auto_supported | review_required | deferred
  "identity": "<sha256>",                      # 本地去重 / 审核身份（不进默认报告）
  "fingerprints": ["<merge.message_fingerprint>", ...],   # 本地（不进默认报告）
  "reason": "TA 在用户回应后针对同一轮内容继续追问（结构观察）",
  "alternative_explanation": "连续提问可能来自任务需要或信息处理习惯，不一定代表关心。",
  "limitations": ["互动结构事件只描述可观察的序列行为，不推断对方意图、感情或人格。",
                  "事件窗口以本地消息序号表示；序号可能因导入 / 排序变化，身份以消息指纹为准。"],
  "capability": {...},                          # 该事件类的能力矩阵条目
  # 事件类特有字段：care_signal / party_structure / boundary_kind /
  #               media_content_unknown / explicit_pressure_language / message_window
}
```

## 4. Prefix invariance（未来泄漏：第一优先级安全属性）

- 所有检测器**只向前扫描**，事件只引用 `<= end_index` 的消息；
- `interaction_diagnostics.prefix_invariance()` 对**每个场景**比较“完整运行中
  在第 n 条前已知的事件”与“只运行前 n 条”的事件集合（按 identity）；
- 测试 `tests/test_interaction_dynamics.py::TestPrefixInvariance` 对 24 个
  场景逐例断言 + I20 future-leakage trap（第 10 条的 recall 不得在前 5 条
  运行时出现）；
- 若某算法需要未来消息才能决定过去事件 → 不允许进入生产（当前无此类算法）。

## 5. 事件身份（与 message index 分离）

- `identity = sha256("interaction-event-v1\n" + event_type + "\n" + actor + "\n"
  + sorted(unique fingerprints))`：**不用 index**，append / prepend / reorder
  后保持稳定（I21 / I22 回归测试）；
- `fingerprints` 用 `merge.message_fingerprint`（raw_speaker + time + text +
  media_kinds，与 media 绑定迁移同源）；
- identity / fingerprints **只本地使用**（去重、behavior 审核）；默认 JSON /
  Markdown 报告一律剥离（`interaction_dynamics.report_view()`），用户可见
  只有 message index / window / reason；
- behavior 适配器把身份映射到 **behavior 既有命名空间**
  （`bv.event_identity(fingerprints, dimension, behavior_type)`），与规则候选 /
  已审核事件天然去重——不新增 DB unique schema（§37 / §53）。

## 6. Conversation Re-engagement（第一类）

- 规则：两条**文本消息**（前一条任意说话方）间隔 ≥
  `REENGAGEMENT_GAP_MINUTES` 且都带**可靠 full timestamp**，间隔后首条消息
  的行为方即重启方；纯媒体消息也可触发（重启本身内容无关，标记
  `media_content_unknown`）；
- 映射：actor=TA → initiative 维度 supporting（可进正式 salient 事件）；
  actor=用户 → observation（**不算 TA 主动**，I4）；
- `REENGAGEMENT_GAP_MINUTES = behavior.INITIATIVE_GAP_MINUTES`（180 分钟，
  复用集中锚点；**operational rule 不是科学阈值**）。诊断工具另测
  2h / 6h / 12h / 24h：在本场景集上判决一致（连续聊天不误切、隔日场景全部
  识别），故不新增阈值常量（避免与 behavior 层漂移）；
- 禁入 romantic / special_attention（I5：重启后只发文件通知也只是 initiative
  观察）；
- 无可靠 full timestamp（time_only / missing / 非法时刻）→ **不计算**间隔，
  不得声称“隔了很久 / 三天后”（I19）。

## 7. Follow-up Sequence（第二类）

- 规则（bounded 4-turn 窗口）：me 陈述（非问句）→ TA 提问（Jev
  ask_information ≥ 0.5 或 intent=ask_information）→ me 回应（非问句）→ TA
  再提问；链式结构延伸为**一条链一个事件**（不重叠）；
- 映射：initiative 维度 supporting（structural observation）；
  **仅当**窗口内出现 Jev `show_care / caring` 结构化信号（≥ 0.5）时，额外
  在 care 维度生成一条 `followup_sequence_with_care_signal` 事件——
  连续提问本身 ≠ 关心（技术问答也会连续提问）；
- 负例：普通问答（我问 TA 答 ×2，I1）、单个问题（I2 变体）、我方的问句起头
  （不算陈述）→ 均无事件；
- 禁入 special / romantic。

## 8. Invitation Progression（第三类）

- 规则：同一行为人（TA）的邀约（Jev invite ≥ 0.5 或 intent=invite）从
  **缺少具体时间/地点**走向包含**具体时间**（`TIME_CONCRETE_RE`：星期/钟点，
  不含“周末/晚上”等宽泛词）或**地点**（`PLACE_RE`）；
- `party_structure`：命中群体词（大家/我们几个/一起聚餐…）→ group；
  命中双人词（就咱俩/只有我们俩…）→ dyadic；否则 **unknown**（不猜）；
- 映射：initiative supporting；**严禁 romantic**（I7 group / I8 dyadic 都
  不得浪漫事件）；alternative_explanation 明说“不代表浪漫”；
- 时间语义：识别“邀约从模糊走向具体”（安排结构），不声称“开启新话题”
  （§13：那是语义理解，属未实现能力）。

## 9. Personal Recall（第四类：最高风险）

- 现状：**candidate-only + review_required**（§22 / §49：这是合法结论，
  不是失败）。自动层只输出“TA 后续消息**可能**明确关联到此前用户提到的具体
  事项”，必须人工确认后才可写“后续主动重新提起此前具体事项”；
- 规则（deterministic lexical anchor）：
  - user 消息（full timestamp）与后续 TA 消息（full timestamp）间隔
    ≥ `RECALL_MIN_GAP_MINUTES`（24h，同日关键词复现视为普通话题延续）；
  - TA 消息须是问句（问号 / 吗 / 呢 / Jev ask_information）；
  - **distinctive anchor**：两者间最长公共子串（≥2 字符），且 (a) 不在
    generic 名单、(b) 不完全落在 generic 词覆盖区内（拒绝“天工”这类跨词
    子串）；anchor 不唯一时取最长 1~2 个；
  - 用户在该间隔内重新提过同一 anchor → **拒绝**（用户先重开话题，I16）；
- 负例（全部有回归）：generic 重合“今天工作”（I17）、同关键词不同事项
  “我妈要去医院 vs 路过医院”（I18）、同日复现、时间不完整（I19）、
  用户先重提（I16）；
- **不实现**：paraphrase / semantic 关联（“做检查”↔“检查结果”）——
  deterministic 无法可靠完成，`deferred`（capability matrix 明示）；
- 映射：care 维度 **review_required 候选**（经 behavior 人工确认后才进
  正式 evidence）；**永不**直接进 special / romantic。

## 10. Reciprocity（第五类：只观察，不打分）

- 只数可观察 turn：TA/用户提问轮次（问号/吗/呢 或 Jev ask_information）、
  分享轮次（Jev share_personal ≥ 0.5；用户侧以可见文本长度 ≥ 6 近似——
  用户消息无 Jev 结果，明示为近似）、重启次数、邀约推进次数；
- 小样本保护：可计数 turn < `RECIPROCITY_MIN_TURNS`（4）→ insufficient，
  只输出“可计数的互动轮次不足，互惠结构暂不描述”；
- 输出确定性描述 + **“仅描述当前导入样本，不代表长期关系模式”**；
- **无百分比、无 reciprocity score、无回复速度**（§26：时间只用于排序 /
  gap / 序列边界）；人格化表述禁止（I14）。

## 11. Boundary Response（第六类：D5 正式路径）

### 11.1 boundary opportunity（用户可观察边界）

| kind | 例 | 说明 |
|---|---|---|
| explicit_refusal | 不想去 / 不用了 / 别再问 | 明确拒绝 |
| pause | 我现在不想聊这个 | 要求暂停话题 |
| reschedule | 今天不行 / 改天吧 | 改期（≠拒绝这个人） |
| romantic_boundary | 只做朋友 / 保持距离 | 划定浪漫边界 |

模式常量：复用 `behavior.REFUSAL_RE` + 新增 PAUSE / RESCHEDULE /
ROMANTIC_BOUNDARY（composition，不复制第二份词典）。

### 11.2 TA 反应分类（boundary 后 ≤ `BOUNDARY_RESPONSE_TURNS`=2 个 TA turn 内
第一个有文本的 TA turn）

| 分类 | 判定 | 事件 | review |
|---|---|---|---|
| continued_request | CONTINUE_REQUEST_RE（含 PRESSURE_RE 组合）| boundary_pressure（仅 explicit_refusal 时）/ boundary_continued_request | explicit_refusal → auto_supported；其余 review_required |
| adjusted | 接受词 + 时间/改期提议 | boundary_adjusted | auto_supported |
| accepted | 接受词，无新请求 | boundary_accepted | auto_supported |
| ambiguous | 有时间提议但无接受词（“那晚上呢？”）| boundary_ambiguous | **review_required** |
| unclassified / 沉默 | 无匹配 / TA 未发言 | 无事件（只进 observations） | — |

- **absence ≠ counter**：沉默不构成任何证据（§55）；
- **无 opportunity → 不作判断**：boundary_pressure 维度 status =
  `insufficient`（“没有可用于观察拒绝后反应的明确边界情境”）；
- **模糊一律 review_required**（§33：“今天不方便 → 那晚上呢？”可能是正常
  协调，不自动叫 pressure）。

### 11.3 Profile boundary_pressure 升级（§54）

| 情形 | status | direction | 事件 |
|---|---|---|---|
| 引擎不可用（无消息） | unsupported | unknown | — |
| 无 boundary opportunity | insufficient | unknown | — |
| 有 accepted / adjusted | evidence_limited | counter | counter_events |
| 有 pressure | evidence_limited | supporting | salient_events |
| 只有候选 | evidence_limited | unknown | interaction_events |
| 两者并存 | evidence_limited | mixed | 各列表 |

counter_evidence_available 在该维度变为 True（**互动结构通道**提供了反证
通道；Jev 九问仍无）。reliability 为结构代理（无 Jev confidence），明示
“不使用 legacy base_score / engagement 代判”——**D5 不再依赖 legacy 分**
（#17 D5：施压 63.62 > 尊重 48.25 的方向性错误正式有了解释通道）。

## 12. Behavior 审核集成（§35~§37）

- `interaction_behavior_candidates(interaction, messages)` 把事件映射为
  `behavior.EventCandidate`（维度/类型走既有 registry，全部通过
  `bv.valid_pair`）：followup→initiative/topic_continuation；
  reengagement→initiative/proactive_contact；invitation→initiative/
  concrete_arrangement；recall→care/continued_attention；
  boundary accepted/adjusted→respect/refusal_reaction；
  continued/pressure→respect/pressure_or_disdain；ambiguous→respect/
  refusal_reaction；
- app.py `_behavior_pending_candidates` 将互动候选**并入现有 pending 队列**：
  身份 = behavior 既有 `event_identity` → 与规则候选、已确认/已排除事件
  天然去重（重复导入不重复出现，I21；用户修改维度/类型后原候选身份仍生效）；
- **没有第二套审核 DB / UI / audit**；互动事件默认不落库，必须用户确认。

## 13~15. 能力边界（Capability Matrix）

| Capability | Auto | Review required | Deferred | Time required |
|---|---|---|---|---|
| conversation_reengagement | yes | no | no | full timestamp |
| followup_sequence | yes | no | no | ordered sequence only |
| invitation_progression | yes | no | no | ordered sequence only |
| personal_recall | **candidate-only** | yes | paraphrase / semantic matching | full timestamp |
| reciprocity | observation-only | no | score / percentage | ordered sequence only |
| boundary_response | yes（explicit refusal + explicit response） | ambiguous paths | no | ordered sequence only |

未实现（#20 之后 or 永不）：语义级“开启新话题”、personal recall 的语义关联、
reciprocity 分数 / 百分比、回复速度推断、boundary_pressure 的直接单消息判断
（#18 unsupported 的 Jev 通道部分不变；本 Issue 只加互动结构通道）。

## 16. Benchmark 与验收

- `evaluation/interaction_scenarios_v0.4.json`：I1~I24 冻结场景（合成 spec +
  确定性物化），每个场景默认做 **prefix invariance** 检查；
- `scripts/run_interaction_diagnostics.py`：端到端（interaction → salience →
  profile）约束检查 + gap 阈值比较 + capability matrix；产物写 gitignored 的
  `evaluation/reports/issue20/`；
- 通过标准（当前）：**24/24 场景约束通过**（含 I20 future-leakage、
  I21/I22 身份、I14 单侧观察、I24 模糊 review）；
- 旧 benchmark（34-case / 43-case）与 #17 / #19 诊断全部继续通过，
  **期望零修改**。

## 17. UI / Report

- **UI**（app.py）：Profile 卡片新增「互动结构」分区（事件 + 审核状态标签 +
  「查看互动窗口」展开：index 范围 / 观察 / 其他可能 / 限制——
  **默认不展示聊天正文**，正文查看仍走行为审核面板的会话窗口）；
  互动来源的 salience 事件在显著/相反证据明细里以窗口形式渲染；
  boundary_pressure 卡按 §11.3 显示能力化状态；
  行为审核面板自动包含互动候选（同一队列 / 分页 / 确认 / 排除）；
- **Report**：JSON additive 顶层键 `interaction_dynamics`（报告视图：无
  fingerprint / identity，`include_text=False` 默认不含聊天正文）；
  Markdown 新增「互动结构证据」章节（事件表 + 逐条理由 / 其他可能 / 限制 +
  能力边界）；0 API、确定性（回归测试）。

## 18. 隐私 / 数据安全 / 回滚

- 不新增任何持久化；不写 behavior DB；不发送 fingerprint / raw_speaker /
  聊天正文到 Jev 或报告默认视图；
- 无 migration；不 invalidate cache；friend history 零影响；
- **回滚 = revert 本 Issue 提交**：`interaction_events` 槽位回到恒空、
  boundary_pressure 回到 unsupported、报告键消失；downtime = 0。

## 19. 已知限制（不掩盖）

1. 全部定量证据来自**合成场景**（no-live-API 延续 #17 限制）：真实聊天上
   recall anchor / 邀约具体化 / boundary 模式的命中率未实证。
2. personal recall 是 lexical anchor 候选：paraphrase 一律 deferred；
   anchor 名单（generic 词表）是精度/召回的手闸，未来需真实数据校准。
3. boundary 反应的极性判断依赖有限的正则模式；未覆盖的表达（礼貌推脱、
   命令式回避等）落入 unclassified（只进 observations，不猜）。
4. reciprocity 的用户侧“分享”以文本长度近似（用户消息无 Jev 结果）。
5. re-engagement 的 gap 阈值与 behavior 层共用 180 分钟：对“午休级”间隔
   （3h）会判为重启——保守但可能偏敏感，operational rule 而非科学阈值。
6. followup 的“同一轮内容”未做语义校验：技术上 TA 两问可能换话题——
   结构上仍成立（追问存在），但“针对同一陈述”的强语义留给人工核对。
7. 纯媒体 turn 只证明存在；混合消息只分析可见文本（parser 语义）。
