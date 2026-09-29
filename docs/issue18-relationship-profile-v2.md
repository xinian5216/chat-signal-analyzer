# Issue #18 设计说明：Relationship Signal Profile v2

- Issue：xinian5216/chat-signal-analyzer#18（v0.4.0 P0 Algorithm）
- 分支：`issue-18-relationship-profile-v2`（base：`main` @ `a6d3a8e`，即 #17 合并后）
- 输入契约：现有 Jev structured results（`chat-signal-v3.3`，**不改 schema / 不改九问**）
- 输出契约：`relationship_profile.build_profile()` 的 `relationship-profile-v2` dict
  （UI 与 report 的唯一 Profile 数据源）

## 0. 目标与非目标

**目标**：把可观察聊天证据拆成多维关系结构，明确表达
“我们知道什么、不知道什么、证据有多少、结论有多可靠”。**没有新的综合分数**。

**非目标（严格禁止，属后续 Issue）**：

- #19 salience-aware 聚合（top-k / percentile / event boost / 新权重）——本 Issue
  只预留 `baseline` / `salient_events` / `counter_events` 接口，不实现、不模拟；
- #20 turn / sequence 事件（话题开启、连续追问、跨日重提、邀约推进、
  reciprocity、拒绝后反应）——本 Issue 只预留 `interaction_events` 注入口；
- 修改 Jev questions / Context Builder / `SCHEMA_VERSION` / scoring 生产权重 /
  Noul transform / message_weight / weighted mean；
- 修 D5 directionality defect（只**如实表达**其限制）；
- Personal baseline、媒体分析、生成式文案、雷达图、任何“喜欢概率 / 关系健康度”
  类表述。

## 1. Dimension Mapping Matrix（实现契约）

| Dimension | Current evidence | What it can say | What it cannot say |
|---|---|---|---|
| 主动性与投入 `initiative_engagement` | `engagement`（0~4）+ intent 概率（ask_information / continue_topic / invite / share_personal） | 互动投入程度、是否存在主动互动信号 | **谁主动开启新话题**、连续追问、邀约推进结构（#20 turn-level，标 `unsupported`） |
| 关心与回应性 `care_responsiveness` | `warmth` + `intent.show_care` 概率 + `emotion.caring` 概率 | 可观察的温暖 / 回应性关心 | 特殊关注、浪漫；“低 warmth = 不关心”（纯事务 ≠ 冷漠证据） |
| 互动熟悉度 `familiarity` | `relational_ease`（0~4，解释层） | 自然、熟悉、轻松、默契 | 喜欢、特殊关注、浪漫（high familiarity **不得**抬高 romantic） |
| 特殊关注 `special_attention` | `special_attention`（0~4） | 相对普通社交的选择性 / 个人化关注 | 浪漫（“只告诉你”≠ 喜欢）；与 warmth / ease 严格分开 |
| 浪漫 / 暧昧 `romantic` | `romantic_signal` raw（0~1）+ `transform_noul_evidence` | 是否存在**明确**浪漫信号（≥0.70 档）；弱信号痕迹单独保留 | 真实内心感情；把 0.08→0.18 的 raw 漂移写成“出现浪漫倾向” |
| 关系疏离 / 后撤 `withdrawal` | `distancing_signal` raw + transformed + `intent.distance` | 关系层后撤 / 减少联系 / 结束关系（v3.0 五分类语义） | 自然收尾 / 暂时忙 / 话题拒绝 / 浪漫边界 ≠ 疏离；**distancing 低 ≠ 没有边界压力** |
| 边界压力 `boundary_pressure` | **无直接通道**（九问未覆盖） | —（明确 `unsupported`） | 可靠的施压 / 无视拒绝 / coercive 判断（#20 boundary response 事件后补） |

## 2. 硬语义规则（测试锚点）

1. **evidence ≠ direction**：`relationship_evidence_strength` 只用于 coverage /
   证据重要性，**绝不**决定 supporting / counter / 好坏方向（#17 结论 8）。
2. **absence ≠ counter**：低分 / 未发现证据 / 不确定都不是反证。counter 通道
   仅在量表给出**明确负向行为档**时开启（engagement ≤ 1、warmth ≤ 1、
   ease ≤ 1：敷衍、冷淡/拒绝、生疏是可观察行为，不是“没观察到”）；
   `special_attention` / `romantic` / `withdrawal` 的 `counter_evidence_available
   = False`（量表低分只是缺证据）。
3. **Noul 双轨**：raw probability 与 transformed evidence 必须同时保留；
   用户可见结论只能是“未发现明确证据 / 存在弱信号（不构成结论）/ 发现明确信号”
   三档；**保留弱信息 ≠ 把弱信息解释成结论**。
4. **insufficient ≠ neutral**：有效证据 < 2 条（锚 `LOW_EVIDENCE_MIN_EFFECTIVE`）
   且无明确档证据 → status `insufficient`、结论“数据不足，无法判断”，
   **禁止**给中位等级。
4b. **coverage 低 ≠ evidence 不存在（语义审计修复）**：单条**明确档**证据
   （Score 明显档 ≥ 3.0 / Noul 明确档 ≥ 0.70 / 严格负向档 < 1.0）→ status
   `evidence_limited`：“当前样本发现明确信号；覆盖有限，需更多样本确认整体
   模式”，reliability 被覆盖封顶（不高于“中等”，单条时“较低”），summary
   强制附“单条明确证据只代表当前样本，不能外推为长期关系模式”。**弱观察**
   （如 engagement 1.8 敷衍档、warmth 1.x 纯事务）不解锁该状态——单条普通
   消息仍是 `insufficient`，minimum sample protection 不被取消。
5. **unsupported ≠ low**：边界压力没有通道时显示
   “当前 schema 不支持可靠判断”，**禁止**显示“边界压力：低”。
6. **不跨维度比较强度**：Score 0~4、Noul 0~1、intent 概率 0~1 各用原生刻度与
   现有文字等级（`score_level_label` / `relational_ease_label` /
   `evidence_level_label`），不折算成 0~100 互比。
7. **D5 表达**：施压消息的高 engagement / 高 evidence 不得形成任何正向结论；
   `boundary_pressure` 恒为 `unsupported`，并在 limitations 中写明
   “高信息量 ≠ 正向关系信号”。

## 3. Profile schema（`relationship-profile-v2`）

```python
{
  "version": "relationship-profile-v2",
  "schema_version": "chat-signal-v3.3",          # 信息性字段（来源 analyzer）
  "dimensions": {
    "<key>": {
      "key", "label",
      "status": "sufficient | insufficient | evidence_limited | unsupported",
      "conclusion": str,                          # 确定性模板短句（用户可见）
      "strength":   {"level": str, "value": float|None, "scale": str, "basis": [str]},
      "coverage":   {"eligible_messages", "supporting_messages",
                     "analyzed_messages", "coverage_ratio"},
      "direction":  "supporting | counter | mixed | none | unknown",
      "reliability": {"level": "较高 | 中等 | 较低 | 不足 | 不适用",
                      "confidence": float|None, "proxy": bool, "basis": [str]},
      "supporting_count", "counter_count", "counter_evidence_available",
      "evidence": [{"message_index", "metric", "value", "confidence",
                    "role": "supporting | counter | weak_trace",
                    "source": "jev_metric | intent_probability",
                    "reason"}],
      "signal": {...dimension-specific 原生数值...},
      "uncertainty": {...概率分布方差 / 熵 或 Noul 区间...},
      "facets": {...解释层分面（如 topic_initiation: 数据不足）...},
      "capability": {...analyzed | unsupported 标记（#20 预留）...},
      "limitations": [str],
      "baseline": {...},                          # #19 预留（#18 透传空）
      "salient_events": [], "counter_events": [], # #19 预留（#18 不产生）
      "interaction_events": [],                   # #20 预留（#18 不产生）
    },
  },
  "overall_legacy": {"overall", "recent", "trend", "role": "辅助参考",
                     "conflict_note": "与画像冲突时以维度与证据为准"},
  "summary": {"lines": [str], "text": str},       # 确定性模板
  "limitations": [str],
  "capability_gaps": [str],                       # #20 缺口清单
  "reserved": {"evidence_source_types": [...5 类...],
               "salient_events": [], "counter_events": [], "interaction_events": []},
}
```

strength / coverage / direction / reliability / supporting / counter /
insufficient / unsupported 的**语义拆分**：

- **strength**：已有证据的强度结论（原生刻度 + 现有文字等级 + 依据），
  **不含**覆盖信息；
- **coverage**：多少消息可支撑该维度（有效证据条数 / 总分析条数），
  **不含**强度信息；
- **direction**：证据朝哪个方向（支持 / 相反 / 混合 / 无 / 未知），
  由 supporting/counter 规则决定，**不看 evidence 大小**；
- **reliability**：已有证据本身多稳定（各消息判断 confidence 均值 + 覆盖封顶），
  **不等于** coverage（1 条高置信消息 ≠ 高可靠：封顶规则见 §4）；
- **supporting / counter evidence**：逐条消息级可追溯记录
  （message_index + metric + value + confidence + role + reason），
  counter 严格按 §2.2 的“affirmative 负向档”规则；
- **insufficient**：一等状态（数据不足，不给等级）；
- **unsupported**：一等状态（schema 无通道，不给等级）。

## 4. 阈值（集中配置于 `relationship_profile.py` 顶部）

**复用现有锚点（不新增）**：

| 用途 | 锚点 | 值 |
|---|---|---|
| 有效证据消息 | `EFFECTIVE_MESSAGE_MIN_EVIDENCE` | ≥ 1.0 |
| 支持档（Score 三类） | 量表第 4 档起点 | ≥ 3.0 |
| 相反档 initiative | engagement 量表第 0~1 档（拒绝 / 敷衍 = 可观察低投入） | < 2.0 |
| 相反档 care | warmth 量表第 0 档（冷淡 / 拒绝；中性·纯事务只是缺证据） | < 1.0 |
| 相反档 familiarity | ease 量表第 0 档（陌生 / 拘谨；正式·礼貌只是缺证据） | < 1.0 |
| 明确 Noul 证据 | `NOUL_STRONG_MARK` | ≥ 0.70 |
| 弱 Noul 痕迹 | `NOUL_NOISE_FLOOR` | > 0.30 |
| 置信“较明确 / 存在歧义” | `CHOICE_SCORE_CONFIDENT` / `CHOICE_SCORE_AMBIGUOUS` | 0.70 / 0.45 |
| insufficient 门槛 | `LOW_EVIDENCE_MIN_EFFECTIVE` | < 2 条 |
| intent 分面明显 / 存在 | `BEHAVIOR_DOMINANT_THRESHOLD` / `BEHAVIOR_NOTABLE_THRESHOLD` | 0.40 / 0.10 |

**新增（仅 2 个，集中配置 + 边界测试）**：

| 常量 | 值 | 依据 |
|---|---|---|
| `RELIABILITY_HIGH_MIN_MESSAGES` | 3 | #17 结论：单条高置信消息不足以支撑“可靠”（`LOW_EVIDENCE_MIN_EFFECTIVE=2` 的同源思想取更保守值）；≥3 条独立有效消息才允许“较高” |
| `RELIABILITY_MIN_COVERAGE` | 0.5 | 覆盖率 < 50% 时可靠性封顶“中等”：证据稀疏时即使单条置信高也不称“较高” |

**reliability 封顶规则（确定性）**：基础等级按 confidence 均值映射
（≥0.70 较高 / ≥0.45 中等 / 否则 较低）→ 有效消息 < 2 封顶“较低”
→ 有效消息 < 3 或覆盖率 < 0.5 封顶“中等”。Noul 维度无 confidence 字段
（官方设计），reliability 为覆盖代理（`proxy: True`，最高“中等”）。

## 5. 不确定性（same-score different-distribution）

Score 维度保留每个消息的概率分布，输出确定性数学量：

- `variance = Σ p(i)·(i − E)²`（分布方差，越大越两极 / 模糊）；
- `entropy_bits = −Σ p(i)·log2 p(i)`（分布熵，越大越模糊）。

同一 score=2.0 的 `P(2)=1.0` 与 `P(0)=0.5,P(4)=0.5` 期望相同但
variance 0 vs 4、entropy 0 vs 1 bit——Profile 的 `uncertainty` 必须区分二者
（有测试）。**entropy 只表达分布集中 / 模糊程度，不是准确率。**

## 6. overall 降级

- 保留全部 legacy 字段（`stats` / overall / recent / trend 计算不动）；
- 概览页：**关系画像置顶**，overall 改为小号“辅助指数”并附
  “与画像冲突时以维度与证据为准”；
- Markdown 报告：关系画像章节位于总体结果之前；
- JSON 报告：`relationship_profile` 为 **additive** 新键，legacy 键原样保留。

## 7. #19 / #20 接口预留（本 Issue 不实现）

- #19：每维度 `baseline` / `salient_events` / `counter_events` 字段 + 顶层
  `reserved.salient_events` / `reserved.counter_events`；#18 恒为空列表，
  不模拟任何 salience 聚合。未来 salient event 只需填充事件列表，
  无需推翻 schema。
- #20：`capability` 标记（`topic_initiation` / `followup_structure` /
  `personal_recall` / `reciprocity` / `boundary_response` 当前均为
  `unsupported`）+ 每维度 `interaction_events` 注入口 + 统一
  `evidence_source_types`（含 `interaction_event` / `salient_event` /
  `manual_confirmed_event`）。#18 实际只用 `jev_metric` / `intent_probability`。

## 8. 验收语义（#17 benchmark A~I，测试强制）

| # | 不变量 | 验证 case |
|---|---|---|
| A | 高熟悉 / 低浪漫可同时成立 | `rb_fam_close_no_special`、`rb_rom_familiar_no_romance` |
| B | 高 engagement / 低 special 可同时成立 | `rb_neg_group_invite`、`rb_init_topic` |
| C | special 明确 / romantic 不足可同时成立 | `rb_special_only_you` |
| D | 明确浪漫 ≠ 调侃友情 | `rb_rom_expression` vs `rb_rom_teasing_flirt` / `rb_fam_mutual_teasing` |
| E | 自然收尾不得显示明显疏离 | `rb_dis_topic_close` |
| F | 低投入不得自动等于疏离 | `rb_dis_low_investment` |
| G | 普通关心不得升级 special / romantic | `rb_neg_health_polite` |
| H | D5 施压不得被 legacy 高分写成正向 | `rb_dis_pressure_after_refusal`（boundary `unsupported` + 无正向措辞） |
| I | 低覆盖必须 `insufficient` 而非中位 | `rb_fam_smalltalk`、`rb_dis_low_investment` |

## 8.1 语义审计记录（PR 前置审计，2026-09-29）

- **insufficient 硬门槛**：审计确认原实现存在“coverage 低抹掉明确证据”问题
  （单条 romantic raw 0.92 / 单条“以后别联系我了”distancing 0.95 都被压成
  “数据不足，无法判断”）。修复为三态（§2 规则 4b）：明确档证据 →
  `evidence_limited`（明确信号 + 覆盖有限 + 可靠性封顶 + 禁止外推）；
  单条普通 / 弱观察仍 `insufficient`。新增 5 条单条证据回归测试
  （明确浪漫 / 明确疏离 + 不与自然收尾混淆 / 单条普通 / 弱负向不解锁 /
  严格负向档解锁）。
- **raw Noul 展示**：主卡正文无 raw；折叠「查看详细指标」中的
  `暧昧 raw：40%` 百分比呈现有概率错觉风险 → 改为 `raw：0.40（…；Jev
  decision probability，非心理概率）`（仅措辞，无行为变化）；Debug 折叠区
  与 profile JSON / 研究报告继续保留 raw（明确标注，允许）。

## 9. 架构与回滚

```
existing Jev results → scoring.py（legacy stats，未改动）
                    → relationship_profile.py（纯函数，新增）
                    → profile data → app.py（概览）/ report.py（MD/JSON）
```

- `relationship_profile.py`：pure / deterministic / 无 Streamlit / 无网络 /
  无文件副作用；业务逻辑**不进** `ui_helpers.py`；
- 回滚 = revert #18 的单独 PR：删除 `relationship_profile.py` 与测试、
  还原 `report.py` / `app.py` / 文档即可；缓存 / 好友档案 / 用户数据不受影响
  （Profile 是本地派生解释层，不写任何持久化，不 invalidate 缓存）。
