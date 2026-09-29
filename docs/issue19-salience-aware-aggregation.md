# Issue #19 设计与研究：Salience-aware aggregation —— 保留强关系事件

- Issue：xinian5216/chat-signal-analyzer#19（v0.4 P1 Algorithm）
- 分支：`issue-19-salience-aware-aggregation`（base：`main` @ `35e4134`，即 #18 合并后）
- 输入契约：现有 Jev structured results（`chat-signal-v3.3`，**不改 schema / 不改九问**）
- 输出契约：`salience.build_salience()` 的 `salience-v1` dict + `relationship-profile-v2`
  预留槽位（`baseline` / `salient_events` / `counter_events`）的正式填充
- 研究工具：`salience_diagnostics.py` + `scripts/run_salience_diagnostics.py`
  （离线、确定性、0 Jev、0 网络、不改变生产结果）

## 0. 目标与非目标

**目标**：普通互动决定 baseline；少量高信息关系事件**分类保留**（不再被平均消失）；
相反方向证据**独立存在**（不再互相抵消成“中等”）。每个事件都回答“为什么显著、
属于哪个维度、方向是什么”。

**非目标（本 Issue 禁止，违者即 bug）**：

- 不是“把高分消息权重调大”：无 top-k 生产采用、无 +20 boost、无新权重、
  无新综合分数（“以前 47 现在 78”不是本 Issue 的成功标准）；
- 不做全局正负 salience（事件首先属于维度；无“全局正向 +20”）；
- 不实现 #20 turn / sequence 事件（话题开启 / 追问 / 跨日重提 / 邀约推进 /
  reciprocity / 拒绝后反应 / personal recall）；
- 不修改 Jev questions / Context Builder / SCHEMA_VERSION / Noul transform /
  legacy weights / message_weight / weighted mean / overall / recent / trend；
- 不伪造 boundary pressure 能力；不写 behavior DB；不使用真实聊天 / 生成式 LLM。

## 1. #17 证据（为什么要做）

`docs/issue17-score-compression-research.md` 的定量结论：

1. **weighted-mean 稀释**：强事件 alone 95.67 → +10 条 mid 49.56 → +100 条 35.80；
   `recent`（窗口 10）在 N>10 后完全塌缩到填充水平（强事件被移出窗口）。
2. **message_weight 双重压制**：`weight = evidence/4 × relation_confidence`，
   ev=2 的有效消息只值 0.35~0.5 权重；weight 是**方向盲**的（D5 施压消息
   weight 0.637 > 尊重边界 0.562）。
3. **Noul floor 清零**：13 对中 8 对的弱 romantic 差异 transform 后 delta=0。
4. **MIN_TOTAL_WEIGHT 悬崖 / recent 退化**：0.487→不出分。
5. **D5 方向缺陷**：施压消息 legacy base_score 63.62 > 尊重边界 48.25
   （+15.4 分方向性错误）；`relationship_evidence_strength` = **信息量，不是方向**。

## 2. #18 契约（本 Issue 只填充预留槽位）

- Profile v2 已冻结：strength / coverage / direction / reliability /
  supporting / counter evidence / `insufficient` / `evidence_limited` /
  `unsupported`；**没有综合分数**。
- 每维度预留 `baseline` / `salient_events` / `counter_events`（#18 恒空）。
- #18 语义必须原样保留：absence ≠ counter、evidence ≠ direction、
  Noul 双轨、单条明确证据 = `evidence_limited` + 覆盖有限 + 禁止外推。
- boundary pressure 恒为 `unsupported`，等 #20 boundary response。

## 3. 候选算法（Issue 原文要求全部比较）

全部实现于 `salience_diagnostics.py`（**研究专用，绝不 import 进生产**）：

| 候选 | 机制 |
|---|---|
| weighted mean（legacy） | `Σ v·w / Σ w`（现状） |
| top-k | 恒选 k 条最高值消息求均值 |
| percentile / tail | 取 P90 尾部均值 |
| baseline + numeric boost | 基线 + 每事件固定 boost（+15） |
| capped cumulative | 超阈值部分饱和累加（cap 30） |
| **event-class channel（选定）** | 维度内分类事件 + capped qualitative tier + 事件排除出基线 |

## 4. Benchmark 方法

1. **S1~S15 冻结场景**（`evaluation/salience_scenarios_v0.4.json`，message spec
   + 冻结期望）：由 `salience_diagnostics.materialize_messages` 确定性物化为
   与 `extract_answers` 同形的 structured results，再由
   `evaluate_scenario()` 做**约束检查**（不是模型判分）。合成数据只证明
   “若 Jev 给出这些信号，本地算法会发生什么”。
2. **Dilution matrix**（§21）：强 special 事件 + N∈{0,1,5,10,30,100} 条普通消息，
   对比 legacy overall / legacy recent / baseline / 事件保留 / 事件类 / tier /
   Profile 结论。
3. **八维属性度量**：retention / outlier / repeated / counter / false-positive /
   D5 / duplication / interpretability，每格引用实测数字（§5）。
4. 确定性：输出无时间戳，重复运行逐字节一致。

## 5. Comparison matrix（§22；数据引自 `evaluation/reports/issue19/report.md`）

retention ratio =（混合 − 仅填充）/（仅事件 − 无）；N=100 行：

| 方法 | retention | outlier safety | counter preservation | direction safety | interpretability | false positives |
|---|---|---|---|---|---|---|
| weighted mean (legacy) | 0.0236（稀释失效；N=10 时 0.19） | baseline shift 0.09 | 抵消率 0.556（正反互抵） | D5 抬升 +1.05（direction-blind） | 单一分数无解释 | S6 噪声无事件概念 |
| top-k | 0.3333（稀释后只剩填充） | 0.9333（极端值必入选） | 正反混列 | 压力消息入选 top-1 = True | 只有排名 | **S6 噪声仍选出 3 条** |
| percentile / tail | 0.0099（N=10 已崩到 0.09） | 0.0903 | 正反混尾部 | direction-blind | 只有分位数 | **噪声尾部仍选出 8 条** |
| baseline + boost | 不随 N 衰减（但数值任意） | **15.0**（最差） | 需另设符号规则 = 新权重 | evidence 驱动 = direction-blind | “为何 +15”不可解释 | 噪声 boost 值 1.18 |
| capped cumulative | 饱和后不分维度保留 | 1.0 | 正反在饱和和里互抵 | direction-blind | 无逐条 reason | 阈值内噪声被累加 |
| **event-class channel（选定）** | **1.0（N 无关）** | **0.0（事件排除出基线）** | salient 1 + counter 3 并存 | 维度内方向 + D5 限制 + 无全局效价 | 逐条 reason / 替代解释 / 限制 | 噪声事件 **0**（阈值门槛） |

Dilution matrix（强事件 + N 条普通；事件= special 3.6，普通= special 1.2）：

| N | legacy overall | legacy recent | baseline special | 事件保留 | tier |
|---|---|---|---|---|---|
| 0 | 60.75 | 60.75 | —（无普通消息） | 是 | single |
| 1 | 53.40 | 53.40 | 1.20 | 是 | single |
| 5 | 43.81 | 43.81 | 1.20 | 是 | single |
| 10 | 40.52 | 35.62 | 1.20 | 是 | single |
| 30 | 37.50 | 35.62 | 1.20 | 是 | single |
| 100 | 36.22 | 35.62 | 1.20 | 是 | single |

**成功标准的正确读法**：legacy overall 可以继续下降（60.75→36.22，未修、不修），
但 salient event 在所有 N 下都保留（`explicit_special_attention`，tier `single`），
且 baseline 由普通消息决定（1.20 恒定，事件消息被排除）。

## 6. 选定方案：baseline + event-class-specific separate channel

```
Jev structured results
        │
        ├── scoring.py（legacy overall / recent / trend，未改动）
        ├── relationship_profile.py（维度基线强度 / 覆盖 / 方向 / 可靠性）
        └── salience.py（#19 新增）
                 ├── baseline        普通互动模式（不含事件消息）
                 ├── salient_events  维度内显著证据（分类保留）
                 └── counter_events  维度内相反证据（独立存在）
                          ↓
                Relationship Profile v2（填充 #18 预留槽位）
```

### 6.1 Event schema（完整示例）

```python
{
  "event_id": "jev_metric:30:special_attention:explicit_special_attention:special_attention",
  "dimension": "special_attention",          # 事件首先属于维度
  "event_class": "explicit_special_attention",
  "direction": "supporting",                 # 只相对该维度自身构念
  "source": "jev_metric",                    # #20 预留 interaction_event
  "message_index": 30,
  "metric": "special_attention",
  "value": 3.6,
  "scale": "0~4",
  "confidence": 0.85,
  "relationship_evidence_strength": 3.2,     # 信息量上下文；绝不决定方向
  "salience_level": "explicit",              # explicit | strong | moderate（分档，无连续分）
  "reason": "special_attention 3.60 ≥ 3：明显超出普通社交的特别关注（量表第 4 档起；接近最高档，明确）",
  "alternative_explanation": "特别关注也可能来自普通朋友的热心或话题高度相关；特殊关注 ≠ 浪漫。",
  "limitations": ["事件来自单条消息的可观察信号，不代表对方真实心理，也不代表长期关系模式。"],
}
```

事件身份 = `(source, message_index, dimension, event_class, metric)`；
同一条消息同一维度同类事件只产生一次；同一条消息可同时拥有
special / care / romantic 各一条（不同维度，S15）。

### 6.2 事件类（当前 schema 真正支持的全部显著性）

| event_class | dimension | direction | 触发（复用 #18“明确档”单一事实来源） |
|---|---|---|---|
| `explicit_special_attention` | special_attention | supporting | special ≥ 3.0 |
| `explicit_care_behavior` | care_responsiveness | supporting | warmth ≥ 3.0 |
| `explicit_romantic_signal` | romantic | supporting | romantic raw ≥ 0.70 |
| `explicit_relationship_withdrawal` | withdrawal | supporting | distancing raw ≥ 0.70 |
| `low_investment_or_refusal` | initiative_engagement | counter | engagement < 1.0（第 0 档） |
| `cold_or_rejecting_response` | care_responsiveness | counter | warmth < 1.0（第 0 档） |
| `stiff_or_unfamiliar_interaction` | familiarity | counter | ease < 1.0（第 0 档） |

- **eligibility**：消息须 evidence ≥ 1.0（有效消息锚点）。
- **salience_level 分档**（不是分数）：Score ≥3.5 explicit / ≥3.25 strong /
  ≥3.0 moderate；Noul ≥0.85 explicit / ≥0.70 strong；负向 <0.5 explicit / <1.0 strong。
- **同类累积（capped tier）**：1 条 `single` / 2 条 `multiple` / ≥3 条 `repeated`
  （封顶，不再升级；文案明说“多次出现不代表统计独立性”，禁止 “82.6% consistent”）。
- **counter 门槛 = 严格负向档（< 1.0）**：#18 证据条目的弱负向档（engagement
  1.x 敷衍）保持为证据条目但**不产生事件**——否则“嗯/好/哈哈”洪流会制造事件
  （S6）。absence ≠ counter 不变：special / romantic / withdrawal 无反证通道。

### 6.3 Baseline（普通互动模式）

- baseline = **非事件消息**的普通互动统计（含低信息量短回复；描述“普通”，
  不承担判断功能）；事件消息被排除，因此**单条极端值无法控制 baseline**
  （S4：baseline shift = 0.0）；
- 每维度原生刻度 + 现有文字等级（不折算 0~100、不跨维度互比）；
- 全部消息都是事件时如实显示“样本不足 / 无法给出普通互动基线”。

## 7. 被拒绝的方案（§37；为什么不用）

1. **top-k**：保留强事件（retention 0.33）但 (a) 单个异常值必入选
   （outlier shift 0.93）；(b) S6 同质噪声里**仍选出 3 条“显著”**（伪造显著）；
   (c) 正反事件混在同一列表，counter 表达差；(d) D5 压力消息入选 top-1。
2. **percentile / tail**：N≤5 时看似保留，**N=10 崩到 0.09、N=100 崩到 0.01**
   （尾部被填充占据）；同质噪声里仍产出“尾部”8 条；direction-blind。
3. **baseline + numeric boost**：区分度最大（不随 N 衰减）但 (a) 权重任意
   （为何 +15？）；(b) outlier shift 15.0 = 单条极端直接控制结果；
   (c) evidence 驱动 boost 就是 direction-blind（D5 会更糟：#17 结论 8.2）；
   (d) 重新制造新版 overall（§17 禁止）。
4. **capped cumulative**：有上限但仍是一个数字：正反在同一饱和和里互抵；
   上限 / 阈值都需任意冻结（5 条时 0.6→3.0 未饱和，cap 形同虚设）；无逐条解释。
5. **weighted mean**：现状；retention 0.02（N=100）、抵消率 0.556、D5 抬升 +1.05。
   **保留为 legacy 输出（不修）**，但不得用于事件保留。
6. **把 Profile 七维压成新综合分数**：#18 硬禁止（“六个换皮 overall”），本 Issue
   同样禁止。

**结论**：没有任何 numeric 聚合能同时满足 §23 的八项门槛；
**baseline + event-class-specific separate channel 本身就是合法结论**（§23 明示）。

## 8. Directionality 规则（最重要）

1. 事件首先属于**维度**；`direction` 只相对该维度自身构念：
   - 高 special → `special_attention.supporting`（不是全局正向 +20）；
   - 高 romantic raw → `romantic.supporting`（不自动抬 special / closeness）；
   - 高 distancing raw → `withdrawal.supporting`（疏离维度的明确证据，
     **不是**“关系证据减分项”）。
2. `relationship_evidence_strength` 只作 eligibility（≥ 1.0）与信息量上下文；
   禁止 `direction = sign(evidence)`、禁止 `positive_salience += evidence`。
3. `message_weight` 不进入本模块（direction-blind，仅 legacy 用）。
4. legacy overall / base_score 不参与任何方向判断。
5. 输出无 valence / global_direction / boost / net 字段（测试强制）。

## 9. D5 handling（强制安全测试）

#17 D5 实测数值（warmth 2.4 / engagement 3.4 / special 3.2 / evidence 3.4 /
romantic 0.55 / distancing 0.1，即边界施压消息）：

- **不生成**任何全局正向事件（词汇表里根本不存在 `positive_*` / `*_closeness_*` /
  `strong_relationship_*`）；
- romantic 0.55 属弱信号区间 → **不生成** romantic 事件（弱痕迹不升级）；
- `boundary_pressure` 维度保持 `unsupported` 且事件为空（不伪造）；
- special 3.2 确实构成**特殊关注维度**的明确档证据 → 保留为
  `explicit_special_attention`（与 #18 一致：不抹掉可观察信号），但事件强制携带
  `limitations:「该消息关系信息量高（3.40 ≥ 3.0）；高信息量 ≠ 正向关系信号（D5）：
  边界施压 / 无视拒绝的语境当前 schema 无法识别」` + `alternative_explanation`；
- 用户可见文本无“更强 / 更好 / 关系健康 / 尊重边界”类结论（测试断言）；
- 高信息消息无法映射到任何受支持事件类时只进 diagnostics
  `unclassified_high_information`（“不得解释为正向或负向结论”），绝不修补成
  假的 boundary pressure 事件。

## 10. False-positive 分析（§33 闸门，全部有测试）

| 反直觉负例 | 结果 | 验证 |
|---|---|---|
| 普通温暖 + 高 engagement（无 special） | 0 事件 | S7 / unit |
| 群邀约（rb_neg_group_invite） | 0 事件 | fixture 回归 |
| 普通健康关心（rb_neg_health_polite） | 0 special / 0 romantic | fixture 回归 / S9 只允许 care |
| 调侃“想你了”（rb_rom_teasing_flirt, raw 0.40） | 0 romantic 事件 | fixture 回归 |
| 熟悉互侃（rb_fam_mutual_teasing） | 0 romantic 事件 | fixture 回归 |
| 自然收尾（rb_dis_topic_close, dist 0.12） | 0 withdrawal 事件 | S13 / fixture 回归 |
| 40 条“嗯/好/哈哈” | 0 事件 | S6 |
| 高 familiarity（S8） | 0 事件 | S8 |

## 11. S1~S15 冻结场景结果（15/15 通过）

S1（强+30 普通）/ S2（强+100 普通）：事件保留 ✓ baseline 恒普通 ✓；
S3（3 条同类）：tier `repeated` ✓；S4（孤立极端）：事件可见 + baseline 0 位移 ✓；
S5（正反并存）：salient 1 + counter 3 并存 ✓；S6/S8/S13：0 事件 ✓；
S7/S9：只允许 care ✓；S10：special 显著、romantic 不自动 ✓；
S11：明确浪漫保留（单条 + 覆盖有限措辞）✓；S12：withdrawal 保留 + 同条消息的
冷淡 / 拒绝作为反向事件并存（不抵消）✓；S14：D5 约束 ✓；S15：跨维度各一条、
同维度同事件类不重复 ✓。

## 12. #20 capability gaps（预留但永不生成）

`topic_initiation` / `followup_structure` / `cross_day_reengagement` /
`invitation_progression` / `boundary_response` / `personal_recall`
（登记于 `salience.RESERVED_EVENT_CLASSES`；测试断言永不输出）。
`boundary_pressure` 在 #20 boundary response 事件到来前恒为 `unsupported`。

## 13. 与 behavior.py 的边界

Salience event 是**自动派生、临时、解释层**（不持久化、不代表人工确认）；
behavior event 是**人工确认的长期档案层**（candidate → 用户确认/排除 →
friend history）。本 Issue 不写 behavior DB、不绕过人工确认、不建第二套长期
事件存储；未来可经 `manual_confirmed_event` 来源类型关联（预留）。

## 14. UI / Report

- UI（Profile 卡片新增）：`基线互动（不含显著/相反事件消息）：一般 普通消息 30 条`、
  `显著证据 ▸ 出现 1 条明确特殊关注信号`、`相反证据 ▸ 出现 1 条明确低投入信号`；
  两区**并排 / 分区**显示，禁止“综合抵消后：中等”；无 salience 87.2 / 重要度 91% /
  boost +13.4 类伪精度（用户层只用“单条明确 / 多次出现 / 存在相反证据 /
  覆盖有限 / 当前不支持判断”）。
- JSON report：additive 顶层键 `salience`（version / baseline_summary /
  counts / diagnostics）；事件明细保存在
  `relationship_profile.dimensions[*].salient_events / counter_events`（不重复
  保存第三份）；legacy 键不变。
- Markdown report：每维度输出 baseline / 显著证据 / 相反证据 / 限制；
  report 0 API、确定性、不新增聊天正文持久化。

## 15. 阈值（集中于 `salience.py` 顶部）

全部复用现有锚点（SUPPORT_MIN 3.0 / NOUL_STRONG_MARK 0.70 / 第 0 档 < 1.0 /
EFFECTIVE_MESSAGE_MIN_EVIDENCE 1.0 / SCORE_LEVEL_STRONG 3.25）；仅新增 4 个
**分档常量**（不是权重）：`SALIENCE_EXPLICIT_MIN_SCORE = 3.5`（接近最高档）、
`SALIENCE_EXPLICIT_MIN_NOUL = 0.85`（接近确定）、`SALIENCE_EXPLICIT_NEG_BELOW
= 0.5`、`TIER_REPEATED_MIN = 3`（累积档封顶）。它们只决定 `salience_level` /
tier 的**措辞档位**，不影响任何方向、资格阈值或数值聚合。

## 16. 隐私与可追溯性

事件可追溯：用户结论 → event_id → dimension → message_index → metric/raw。
不新增聊天正文 / 昵称 / parser-only metadata / fingerprint 的存储或外发；
只使用当前 run 已有数据；salience 输出纯内存派生。

## 17. 已知限制（不掩盖）

1. 全部定量证据来自**合成场景**（no-live-API）：真实 Jev 是否稳定给出
   special ≥ 3.0 / romantic ≥ 0.70 尚未实证（#17 no-live-API 限制延续）。
2. 事件资格沿用 #18 明确档：真实 Jev 的分数若普遍偏低，事件会稀少
   （保守方向，可接受）；若普遍偏高，事件会变多——需要真实数据后再校准。
3. counter 事件只覆盖三个有反证通道的维度（engagement / warmth / ease 的
   第 0 档）；special / romantic / withdrawal 无反证通道（schema 限制）。
4. baseline 含低信息量短回复（描述“普通”）；它不是判断，也不与维度
   strength 混用。
5. D5 special 事件保留依据是“Jev 确实给了 3.2”；其语境（施压 vs 真诚关注）
   **当前 schema 无法区分**——这正是 #20 需要补的 boundary response 通道。
6. capped tier 是定性结构（single/multiple/repeated），不是统计显著性。

## 18. 测试与复现（全部离线、确定性）

```powershell
# 生产 salience 诊断（S1~S15 + dilution + 候选比较；产物 gitignored）
.venv\Scripts\python scripts\run_salience_diagnostics.py

# 既有 benchmark（不得修改期望）
.venv\Scripts\python scripts\evaluate.py --fixtures
.venv\Scripts\python scripts\evaluate.py --fixtures `
  --cases evaluation/cases_relationship_v0.4.json `
  --fixture-file evaluation/fixtures/baseline_v0.4_relationship.json

# #17 诊断（必须继续正常）
.venv\Scripts\python scripts\run_score_diagnostics.py

# 测试
.venv\Scripts\python -m pytest tests -q
```

## 19. 回滚

本 Issue 改动 = 新增 `salience.py` / `salience_diagnostics.py` /
`scripts/run_salience_diagnostics.py` / `evaluation/salience_scenarios_v0.4.json`
/ 测试 / 本文档 + `relationship_profile.py`（槽位填充）、`app.py` / `report.py`
（渲染）、`tests/test_report.py`（顶层键守卫，additive）的少量接线。
**回滚方式：revert 本 Issue 的提交**即可恢复 #18 状态（槽位回到恒空）。
downtime = 0；无 migration；不 invalidate cache；friend history / analysis cache /
用户数据不受影响（salience 是本地派生解释层，不写任何持久化）。
