# Psychological Evidence v3 — Phase 2 设计方案（仅方案，本轮不实施）

输入：v2.2 真实基线（34 案例）+ v3 distancing 修正后的 15 案例真实结果
（jev-1.13.0）+ `phase2_design_review.md` 的 12 条失败约束复核。
原则：**不增加凭空的“读心”维度**；优先改问题描述，能不新增维度就不新增；
任何影响评分的改动都必须 bump `SCHEMA_VERSION` 并重跑真实基线对比。

## 一、问题清单与处置矩阵

| # | 问题 | 根因（复核结论） | 处置 | 影响面 |
|---|---|---|---|---|
| 1 | intent 无法区分话题拒绝 / 浪漫边界 / 关系疏离 | 不是缺选项，是 `distance` 描述太粗 | **仅改题目描述**：细化 `distance` 与相关选项的 criteria 措辞，显式枚举三类子情形及示例 | schema bump；评分不变 |
| 2 | warmth 把“普通友好”与“关心/情绪支持”混在一起 | 题目未区分 politeness 与 care | **仅改题目描述**：明确“礼貌/友好单独出现→中等；对困境的回应性关心→较高”；同时明确关心≠浪漫（浪漫由 romantic_signal 判断） | schema bump；评分**会变**（warmth 进 formula） |
| 3 | evidence 与证据方向混淆（案例层已证实是标定问题） | 案例误读，非题目错误 | **不改题目**；修订案例集另建（v3.1），标定时按题目原文（拒绝/回避=高信息量） | 无（案例层） |
| 4 | 调侃 vs 冒犯的过度自信 | 无语气线索时的兜底策略未定义 | **仅改题目描述**：emotion/intent 的 instructions 增加“文本无语气线索时选择可覆盖多种解读的选项并保持低置信”的指引 | schema bump；评分可能微变 |
| 5 | 回应性 / 自我披露回应 / 互惠等更具体的关系行为 | 当前题目集无对应维度 | **评估后决定：暂不新增维度**（见下） | 无 |

## 二、分项设计

### 2.1 intent：三类“拒绝”的可区分化（仅描述）

现状：`distance` = “回避、拒绝、主动拉开距离”。真实评估显示模型**已经**
用 `distance` 统一标注三类拒绝，而 distancing Noul 正确保持低位——层次没
混，混的是标签粒度。

方案：保持 Choice 结构与选项数量，将 `distance` 的描述改写为显式三分：

> `distance`: 回避、拒绝或拉开距离。按范围区分：**话题拒绝**（仅不想谈
> 当前话题）、**浪漫边界**（明确不做恋人但朋友往来可继续）、**关系疏离**
> （减少/结束持续联系）。三者都选本项；范围判断由上下文决定，证据不足时
> 选本项而非猜测范围。

同时微调 `perfunctory` 描述，避免把“有内容的简短拒绝”误标敷衍。
**不新增选项**（保 9 问结构、保缓存口径只在语义层变化）。

### 2.2 warmth：关心 ≠ 友好 ≠ 浪漫（仅描述）

方案：`warmth` instructions 增加分层定义：

- 基础层：礼貌、友好、正常社交 → 中位（2 级）；
- 回应层：对对方困境/自我披露的具体关心与情绪支持 → 上位（3–4 级）；
- 明确条款：关心与友好**不得**单独推高 romantic_signal；后者只由
  romantic_signal 判断。

预期收益：v2.2 基线中 warmth 校准偏低（关心语 2.9 够不到 3）与 v3 中
拒绝语境 warmth 压低（0.74）可部分修复；代价是 warmth 均分上移，
**base_score 与 overall 会变**，必须重跑基线并显式对比（不能宣称“改善”
除非人工约束满足更多）。

### 2.3 evidence 与方向分离：不做新维度

现状已经足够：`relationship_evidence_strength` 度量信息量，
romantic/distancing 两个 Noul 度量方向。复核证明模型在此分工上行为正确
（浪漫边界 ev=4.00 + dis=0.04）。**仅补一条 instructions 例子**，把
“拒绝/边界/回避=高信息量”写进题目（与 RELATIONSHIP_EVIDENCE_INSTRUCTIONS
原文一致，只是更显式），避免后续案例标定再次误读。

### 2.4 歧义与过度自信（仅描述）

在 emotion / intent 的 instructions 增加歧义条款：

> 当文本缺少语气线索（如单独的“哈哈”“行吧”）、调侃与冒犯都可能成立时，
> 选择能覆盖合理解读的选项，不要给极端情绪/意图过高置信。

Choice 的 `confidence` 本来就会返回，该条款让模型**显式利用**它，而不是
强迫二选一。不新增问题。

### 2.5 是否引入新维度：结论是暂不

| 候选维度 | 评估 | 结论 |
|---|---|---|
| 回应性（responsiveness） | **定义澄清：回应性指对对方需求或情绪的理解、认可和关心**（读懂对方在说什么、回应其情绪与请求），**不等于秒回或回复速度**，也不得以时间间隔推断（与项目反“读心”原则一致）。作为新维度本轮**不引入**（不进九问、不改总分），但回应质量保留为后续研究与开发方向，可通过 intent=show_care/continue_topic 与 warmth 组合近似观察 | **本轮不引入，列为后续方向** |
| 自我披露回应质量 | 有价值，但当前可由 intent（show_care/continue_topic）+ warmth 组合近似；单独成维度会增加每请求成本与 formula 复杂度 | **缓议**，v4 前先用 benchmark 观察组合指标是否足够 |
| 互动互惠（reciprocity） | 需要跨消息/跨 turn 的左右对称比较，而 state 是单 target 结构；实现它等于改 state 语义（大改，影响所有缓存与报告） | **缓议至 v4**，v3 只在 benchmark 增加互惠型 case 做观察，不进公式 |
| 情绪支持具体性 | 由 2.2 warmth 分层覆盖 | **不新增** |

## 三、变更分级与成本/兼容性

| 变更 | 类型 | schema bump | 评分变化 | 缓存 | API 成本 |
|---|---|---|---|---|---|
| intent/warmth/evidence 描述细化 + 歧义条款 | 仅问题描述 | 是（→ v3.1） | warmth 变（进 formula）；intent/evidence 不变 formula | 旧缓存自然 miss | **每目标仍 1 次请求**；问题数不变 → 单请求成本不变 |
| 新增任何维度（若未来做） | 新问题 | 是 | 需先定权重（默认 0 不进 formula） | 自然 miss | 问题的 fan-out 成本增加，需一次真实运行实测增量 |
| 修订案例集 cases_distancing_v3.1 | 数据 | 否 | 否 | 否 | 0 |

**缓存兼容性**：任何 instructions/criteria 变化都改变 question 语义 → 按
AGENTS.md 规则 bump `SCHEMA_VERSION`（v3.0 → v3.1），`.jev_cache` 不删、
自然 miss；已保存的 v2.2 / v3.0 真实报告用 `--compare` 只能做约束级 diff，
且 CLI 会显式提示 schema 语义不同（已实现）。

**API 成本**：单条 TA 消息仍是一次 `system_one`；本 Phase 全部为描述级
修改，问题数仍为 9 → 每请求计费口径不变。真实成本只有一次重跑基线
（34+15=49 次请求）与后续对比运行。

## 四、实施顺序（建议，待批准）

1. 改题目描述（intent 三分 / warmth 分层 / evidence 例子 / 歧义条款）
   + bump `v3.1` + mock 测试（文本哨兵 + 九问合一 + 缓存隔离）；
2. 另建 `cases_distancing_v3.1.json`（按题目定义重新标定 evidence 上限、
   放回 `distance` 意图、保留全部真阳性与反读心约束），预期 pre-registered；
3. 全量 pytest + 双 fixture 冒烟 + 文档同步；
4. **你单独批准后**再跑真实 v3.1 基线并与 `v3_distancing_real_meta_fixed.json`
   做约束级对比（CLI 会提示 schema 差异）。

## 五、明确不做

不改 scoring 公式与权重、不改其余五个 Score/Choice 题目语义、不引入
respondiveness 类时间维度、不碰 parser/media/UI/portable、不发版。
