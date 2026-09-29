# Issue #17 研究报告：分数压缩诊断与 relationship-signal benchmark

- Issue：xinian5216/chat-signal-analyzer#17（v0.4.0 P0 Research）
- 分支：`issue-17-score-compression-benchmark`（base：`main` @ `a578866`）
- 分析 schema：`chat-signal-v3.3`（**未改动**）
- 生产公式：`scoring.py`（**未改动**，权重/阈值冻结）
- 性质：**0 production behavior change**；全部新增内容为离线研究工具、虚构
  benchmark 数据、合成 fixture、测试与本文档

> **证据分级声明（全文所有结论都受此约束）**
>
> 1. **合成 fixture 证据**（v0.4 relationship benchmark）：人工构造的
>    structured Jev 结果。只能证明“**若** Jev 给出这些原始信号，本地算法之后
>    会发生什么”，**不能**证明真实 Jev 在这些聊天上会给出这些结果。
> 2. **真实 Jev 证据**（既存、已冻结、gitignored 的 raw 输出，`jev-1.13.0`）：
>    只覆盖既有案例集（main34 / distancing / phase2 / contrast），且历史运行
>    每 case 只分析 1 个 target → **只有 Layer A / 单消息 Layer B 可实证**，
>    真实 Layer C 聚合无法重建。
> 3. **解析层**（stress tests）：生产函数的确定性数学性质，与 Jev 无关。
>
> 在 Issue #17 的 no-live-API 约束下，**无法仅凭 synthetic fixtures 对真实
> Jev 的 case-level separability 作实证结论**；本文完整诊断的是
> **Jev 输出之后的本地压缩链**，并以既存真实输出对 Layer A/B 部分实证。

---

## 1. 建立了什么

### 1.1 虚构 benchmark（43 cases，13 对 + 17 单例）

- `evaluation/cases_relationship_v0.4.json`：43 个完全虚构案例（微信三行块
  格式，身份只用「我 / TA」，PII 扫描通过），沿用既有 case schema
  （expectations / must_not_infer / acceptable_ambiguity / context_checks），
  **不修改任何既有冻结 expectation**。
- `evaluation/relationship_pairs_v0.4.json`：成对 / 对照元数据（稳定
  `pair_id`、family、factor（单因素差异）、`expected_distinguishers`、
  Issue 清单 → 覆盖条目 `coverage_map`），以及 13 组**既有冻结案例集**上的
  `frozen_real_pairs`（供真实 Jev raw 层复用）。
- 分类覆盖 Issue #17 全部清单条目（`coverage_map` 31 条，测试强制校验）：

| 模式 | pair / case |
|---|---|
| 主动性 | P1 普通回复↔主动开启话题、P2 被动回应↔连续主动追问、P3 模糊推迟↔具体安排 |
| 关心与回应 | C1 礼貌关心↔具体追问、C2 当场关心↔隔天重提、C3 附和↔实质回应 |
| 熟悉与友情 | F1 熟人寒暄、F2 高频接话、F3 互相调侃、F4 哥们称呼、F5 熟悉无特殊关注 |
| 特殊关注 | S1 普通友好↔“特意给你”、S2 普通分享↔“只告诉你”、S3 一致↔区别对待、S4 单条弱线索 |
| 浪漫 | R1 亲密友情↔浪漫表达、R2 普通邀约↔双人约会、R3 调侃暧昧↔明确表白、R4 强熟悉无浪漫 |
| 疏离/边界 | D1 自然结束、D2 低投入非疏离、D3 明确拒绝、D4 持续回避、D5 尊重边界↔继续施压 |
| 反直觉负例 | N1 热情对所有人、N2 熟悉无特殊、N3 群体邀约、N4 普通关心健康、N5 哥们证据不足、N6 玩笑“想你了” |

### 1.2 synthetic structured fixtures

- `evaluation/fixtures/relationship_conversations_v0.4_synthetic.json`：
  每个 case 每条 TA 消息一条完整 extract_answers 同形结果（Layer A 字段全
  保留：choice/probabilities/confidence、5 个 Score 的 score/probabilities/
  confidence、2 个 Noul 原始概率），带 `meta.provenance = "synthetic"` 与
  `model = "synthetic-issue17-fixture"`（**校验器拒绝任何未标注 synthetic 的
  fixture**，防止冒充真实输出）。每个 Score 的 `score` 都等于其概率分布的
  期望值（校验器强制，±1e-4）。
- `evaluation/fixtures/baseline_v0.4_relationship.json`：target 级派生 fixture，
  可直接喂给既有 harness（`scripts/evaluate.py --fixtures` → 43/43 通过）。

### 1.3 诊断工具（0 生产改动、0 网络）

- `score_diagnostics.py`：三层捕获（Layer A 原样；Layer B 调
  `scoring.message_metrics`；Layer C 调 `scoring.compute_conversation_stats`
  ——**import 生产函数，不复制公式**）、paired delta 链、4 组确定性压力测试、
  分布汇总、真实 raw 加载（严格校验 + provenance 标记）、报告构建与 Markdown
  渲染。缺字段 / 结构不合法 → `DiagnosticError` **显式失败**，绝不静默出结论。
- `scripts/run_score_diagnostics.py`：CLI，产物写 `evaluation/reports/issue17/`
  （gitignored，永不提交）：`report.json` / `report.md` / 5 个 CSV。
  输出无时间戳：**相同输入重复运行逐字节一致**（有测试）。

### 1.4 测试（44 条新增）

`tests/test_relationship_benchmark.py`（数据与 harness 兼容）+
`tests/test_score_diagnostics.py`（三层 / delta / 压力 / 严格失败语义）：
虚构内容校验、完全离线（子进程 + 进程内 socket 拦截 + 静态扫描）、
生产常量未改动锚点、重复运行一致、手算 paired delta、Noul 曲线与
`transform_noul_evidence()` 逐点一致、dilution 可重复且单调、malformed
fixture 显式失败、缺字段不静默、既有 harness 不回归。

---

## 2. 三层诊断关键数据

### 2.1 Layer A（Jev structured 层）

**真实 Jev（既存冻结 raw，n=72 targets，跨 4 份 raw 文件）**：

| 指标 | min | median | max | mean |
|---|---|---|---|---|
| warmth | 0.00 | 1.69 | 3.57 | 1.54 |
| engagement | 0.00 | 1.96 | 3.91 | 1.89 |
| special_attention | 0.01 | 1.08 | 3.72 | 1.28 |
| relationship_evidence_strength | 0.01 | 2.33 | 4.00 | 2.14 |
| relational_ease | 0.18 | 2.01 | 3.50 | 2.00 |
| romantic_raw | 0.02 | 0.03 | 0.88 | 0.08 |
| distancing_raw | 0.01 | 0.02 | 0.99 | 0.10 |

数据保真度观察：真实 Jev 的概率只序列化到 2 位小数，
`|score − E[序列化概率]|` 最大 0.04（均值 ≈0.005）——score 确实是分布期望，
但序列化舍入使重算期望有微小偏差（加载器对真实数据用 0.05 容差并记录该偏差）。

**真实成对 raw delta（13 组 frozen_real_pairs，同 raw 内、schema 同质）**：

| pair | warmth | engagement | special | evidence | ease | romantic | distancing |
|---|---|---|---|---|---|---|---|
| FRR_close_vs_refuse | −2.23 | −3.00 | −2.24 | +0.93 | −3.09 | −0.24 | +0.97 |
| FRR_busy_vs_withdraw | −2.34 | −2.94 | −1.78 | +0.95 | −0.95 | −0.11 | +0.95 |
| FRR_tease_cues | +1.25 | +0.50 | +0.98 | −0.21 | +0.70 | +0.02 | 0.00 |
| FRR_care_polite_vs_help | +0.47 | +1.42 | +1.48 | +1.52 | +0.77 | +0.02 | 0.00 |
| FRR_care_validation_vs_help | +0.03 | +1.15 | +1.10 | +0.78 | +0.80 | +0.03 | 0.00 |
| FRR_same_sentence | −0.34 | −0.28 | −0.51 | +0.28 | −0.37 | −0.01 | 0.00 |
| FRR_tease_vs_flirt | +3.26 | +2.26 | +2.44 | +1.05 | −0.13 | **+0.80** | 0.00 |
| FRR_ease_vs_romance | +0.40 | +1.03 | +0.95 | +1.12 | −0.20 | **+0.70** | +0.01 |
| FRR_plain_vs_selective | +0.89 | +0.97 | **+1.15** | **+2.21** | +1.94 | +0.07 | 0.00 |
| FRR_invite_vs_date | +1.26 | +0.45 | **+1.37** | +0.98 | +0.72 | **+0.63** | +0.01 |
| FRR_close_vs_stop | −1.96 | −1.74 | −0.62 | **+3.29** | −1.49 | 0.00 | **+0.96** |
| FRR_busy_vs_avoid | −1.86 | −2.65 | −1.26 | +1.55 | −1.96 | −0.05 | **+0.95** |
| FRR_boundary_vs_stop | −0.81 | −1.62 | −0.44 | 0.00 | −0.63 | 0.00 | **+0.95** |

### 2.2 Layer B（`message_metrics()` 层）与 Layer C（聚合层）——合成链路

13 组成对案例的 delta 链（0~100 分制；raw = target 级最大 Score delta）：

| pair | raw | base_score | overall | overall/raw | romantic_ev Δ |
|---|---|---|---|---|---|
| P1_topic_start | 50.00 | 22.50 | 10.95 | 0.219 | 0.000 |
| P2_probe | 50.00 | 25.25 | 13.72 | 0.274 | 0.000 |
| P3_concrete_plan | 50.00 | 26.00 | 15.26 | 0.305 | 0.000 |
| C1_care_specific | 45.00 | 26.00 | 14.51 | 0.322 | 0.000 |
| **C2_care_recall** | 45.00 | 24.50 | **8.76** | **0.195** | 0.000 |
| C3_care_substantive | 55.00 | 32.75 | 17.05 | 0.310 | 0.000 |
| S1_for_you | 50.00 | 24.75 | 17.66 | 0.353 | 0.000 |
| S2_only_you | 50.00 | 24.50 | 13.91 | 0.278 | 0.000 |
| S3_distinct | 50.00 | 22.62 | 13.75 | 0.275 | +0.044 |
| R1_romance_expression | 45.00 | 39.54 | 27.35 | 0.608 | **+0.783** |
| R2_date_context | 52.50 | 35.21 | 27.97 | 0.533 | **+0.567** |
| R3_explicit_like | 37.50 | 33.53 | 22.69 | 0.605 | **+0.739** |
| D5_boundary_pressure | 35.00 | 15.38 | 15.38 | 0.439 | +0.219 |

真实 Jev 链路（同 raw 内 raw→base 存活率，无 Layer C）：
FRR_tease_vs_flirt 0.815、FRR_invite_vs_date 0.977、FRR_ease_vs_romance
1.000；但 FRR_plain_vs_selective 0.361、FRR_close_vs_stop 0.394。

### 2.3 各模式 overall 分布（合成，n=可出分 case）

| family | n | min | median | max |
|---|---|---|---|---|
| boundary | 4 | 18.48 | 35.93 | 63.62 |
| initiative | 6 | 31.70 | 39.90 | 50.58 |
| special | 7 | 35.50 | 40.38 | 56.31 |
| familiarity | 4 | 41.88 | 44.43 | 51.95 |
| negatives | 3 | 38.45 | 45.83 | 50.94 |
| care | 6 | 34.70 | 46.86 | 54.41 |
| romantic | 7 | 42.00 | 50.70 | 73.39 |

模式间重叠（min−max 交叠宽度 / 中位数差）：**care|negatives 中位数差仅
1.02、familiarity|negatives 1.40、care|familiarity 2.42、initiative|special
0.48**；romantic 的 min−max 与其它全部模式交叠 8.6~21.6 分。7 个模式的
overall 中位数全部落在 35.9~50.7 这段 15 分宽的带里。

---

## 3. 压力测试（确定性，纯生产函数）

### 3.1 weighted-mean dilution（1 条强事件 + N 条普通消息）

强事件 alone：overall 95.67（base 95.67，weight 0.90）。

| filler | N=1 | N=5 | N=10 | N=30 | N=100 |
|---|---|---|---|---|---|
| low（evidence 0.5） | 89.61 (−6.1) | 72.49 (−23.2) | 59.85 (−35.8) | 39.39 (−56.3) | 25.32 (−70.4) |
| mid（evidence 1.5） | 81.69 (−14.0) | 58.94 (−36.7) | 49.56 (−46.1) | 40.10 (−55.6) | 35.80 (−59.9) |

- 10 条 mid 填充即把强事件从 95.67 拉到 49.56；100 条 mid → 35.80
  （渐近填充水平）。
- `recent`（窗口 10）：N≤10 时等于 overall；**N>10 后 recent 塌缩到纯填充
  水平**（low 16.88 / mid 33.75），强事件被完全移出窗口。

### 3.2 message_weight 敏感性

`weight = (evidence/4) × relation_confidence`（生产公式实测值）：

| evidence | conf 0.3 | conf 0.5 | conf 0.7 | conf 0.9 | conf 1.0 |
|---|---|---|---|---|---|
| 0.5 | 0.038 | 0.063 | 0.088 | 0.113 | 0.125 |
| 1.0 | 0.075 | 0.125 | 0.175 | 0.225 | 0.250 |
| 2.0 | 0.150 | 0.250 | 0.350 | 0.450 | 0.500 |
| 3.0 | 0.225 | 0.375 | 0.525 | 0.675 | 0.750 |
| 4.0 | 0.300 | 0.500 | 0.700 | 0.900 | 1.000 |

强证据（ev=4）+ 中等置信（0.7）→ weight 0.70：**置信度耦合会让高价值事件
在加权均值里的份额直接打七折**；ev=2 的常见“有效消息”weight 只有 0.35。

### 3.3 Noul transform 曲线

| 区段 | raw 区间 | evidence 区间 | 斜率 |
|---|---|---|---|
| noise floor | 0.00–0.30 | 0 | 0 |
| mid gain | 0.30–0.70 | 0–0.35 | 0.875 |
| strong mark | 0.70–1.00 | 0.35–1.00 | 2.167 |

- raw 概率 30% 宽的区间被整体归零；mid 带 0.40 的 raw 跨度只产生 0.35 的
  evidence；strong 带 0.30 的 raw 跨度产生 0.65。
- **强带斜率是 mid 带的 2.48 倍**：同样的 evidence 差距，在不确定带需要
  2.48 倍的 raw 概率差距。
- 直接后果（合成链路实测）：13 对中 8 对的 romantic raw delta（0.09~0.25，
  两侧都在 floor 之下）→ **transform 后 romantic_ev delta 恰好为 0**。
  只有越过 0.70 的浪漫表白（R1/R2/R3）留下可见 evidence。

### 3.4 同 score 不同分布

| 分布 | score | variance | entropy | base(0~100) | weight |
|---|---|---|---|---|---|
| certain {2:1.0} | 2.0 | 0.00 | 0.00 | 40.00 | 0.400 |
| bimodal {1:0.5,3:0.5} | 2.0 | 1.00 | 1.00 | 40.00 | 0.400 |
| spread {0:0.25,2:0.5,4:0.25} | 2.0 | 2.00 | 1.50 | 40.00 | 0.400 |

同一 score 下 base_score / message_weight **完全一致**：score 本身不携带分布
信息，probabilities 的方差/熵差异全部被隐藏（confidence 相同时）。

---

## 4. Issue #17 问题逐条回答

1. **Jev 原始指标能否区分 benchmark 案例？**
   *真实 Jev*（既有案例集）：**强对比可区分，弱对比不可靠**。明确表白 vs
   调侃 romantic Δ=+0.80；拒联 vs 收尾 distancing Δ=+0.95~0.97、evidence
   Δ 最高 +3.29；“只告诉你/特意给你” special Δ=+1.15~1.37、evidence
   Δ=+0.98~2.21。但同句不同前文（FRR_same_sentence）全部 |Δ|≤0.51，
   调侃有无线索的 evidence Δ=−0.21（方向还反了）。*v0.4 新案例*：
   合成 fixture 不构成实证，**尚未实证**（需 #18/#19 阶段的人工批准真实运行）。

2. **`relationship_evidence_strength` 是否合理反映消息重要程度？**
   部分合理。真实数据：高信息事件（拒联 +3.29、选择性关注 +2.21、表白
   +1.05）方向正确且幅度大；但 care 分层只有 +0.78~+1.52，调侃线索反而
   −0.21。且 evidence 与 `message_weight` 相乘后（÷4）把“信息量”直接折成
   权重：ev=2 的有效消息只值 0.35~0.5 权重，中等信息事件在长对话里被
   系统性降权。**结论：作为“信息量”标尺基本合理，作为聚合权重的乘子偏压缩。**

3. **`message_weight` 是否压制高价值事件？**
   是，两重压制：(a) evidence/4 归一化让 2 级信息量只值半权；(b)
   relation_confidence 乘子让“强但不确定”的信号份额再打折（ev=4、conf 0.7
   → 0.70）。而 weight 是**方向盲**的（设计如此）：D5 施压消息
   （违背边界）的 weight 0.637 反而高于尊重边界消息 0.562。

4. **`transform_noul_evidence()` 的 noise floor / mid gain 是否造成明显压缩？**
   是，且可精确量化：floor 归零 raw 0~0.30（合成链路 13 对中 9 对的
   romantic 差异被清零）；mid 斜率 0.875 vs strong 2.167（2.48×）。
   anti-overclaim 与压缩在这里是**同一个旋钮的两面**。真实数据里
   romantic_raw 中位数只有 0.03，说明真实 Jev 大多数输出本就低于 floor——
   floor 的设定对“真阴性”是合理的，代价是把 0.30~0.70 之间的弱信号差异
   非线性压平。

5. **weighted mean 是否是强信号稀释的主要来源？**
   **是长对话场景下的主要来源**（stress 3.1）：10 条 mid 普通消息即可抹掉
   强事件 46 分；recent 窗口在 N>10 后完全丢弃强事件。但在 2~4 条 TA 消息的
   短会话里，稀释只是把 base delta 再乘 0.6~0.9（C2：单条 base 差 24.5 →
   overall 差 8.76，因为对比消息只占 3 条中的 1 条且权重低于共享消息总权重）。
   **分层结论：短会话压缩主因是 base_score 的构成 + 单条 delta 被共享消息
   平均；长会话压缩主因是 weighted mean 稀释 + recent 窗口。**

6. **不同关系模式的 overall 分布重叠程度？**
   高度重叠（合成链路，2.3 节）：7 个模式中位数挤在 15 分带内；
   care|negatives 中位数差 1.02、initiative|special 0.48——**overall 单指标
   无法区分这些模式**。romantic 分离最好（表白对 overall +22.7~+28.0），
   但其区间仍与其它模式交叠。另有 6/43 个低信息会话因
   Σweight < MIN_TOTAL_WEIGHT=0.5 **完全不出分**（阈值悬崖：0.487 不出分、
   0.50 出分）。

7. **哪些是聚合算法问题，哪些是 Jev schema 表达力问题？**
   - **聚合算法问题（本地，可修）**：weighted mean 稀释；recent 窗口退化；
     MIN_TOTAL_WEIGHT 悬崖；Noul floor 的非线性压缩；weight 的置信度乘子；
     **base_score 方向语义缺失**（见下）。
   - **schema 表达力问题（需未来评估，本 Issue 不改）**：九问里没有
     “施压 / 无视边界 / 情感胁迫”这类**负向关系行为**的独立表达——
     D5 施压消息在 Jev 层只有 engagement/special/romantic 上升，
     本地 base_score 把这些当正向加分 → 施压消息 63.62 > 尊重边界 48.25
     （**+15.4 分方向性错误**）。distancing 只覆盖“疏离”方向。
   - **两者交界**：score 把分布压成点估计（3.4 节）——schema 提供了
     probabilities/confidence，是**本地算法没用**（见 #18 建议）。

8. 见 7。

9. **no-live-API 约束下无法实证的**：
   (a) v0.4 新 43 案例在真实 Jev 上的 case-level separability（synthetic 不算）；
   (b) 真实 Layer C 聚合行为（历史 raw 单 target，无法重建会话聚合）；
   (c) 合成 authoring 与真实 Jev 校准的一致性（例如真实 Jev 对表白是否真给
   romantic ≥0.7——旁证支持：FRR 显示 +0.7~0.8，但新案例未验证）；
   (d) 模型漂移（jev-latest 与 jev-1.13.0 的输出差异）。

---

## 5. 最主要的 compression source（按证据强度排序）

1. **会话聚合（weighted mean + 共享消息平均）**：最强证据。13 对合成链路
   overall/raw 存活率 0.195~0.608；stress 3.1 定量稀释曲线；C2 案例逐条
   权重可查。真实数据侧（raw→base）FRR_plain_vs_selective 0.361 佐证
   单消息层也会大量损失。
2. **base_score 构成（0~1 加权和 + clamp）**：Score delta 只取
   0.30/0.25/0.25 的份额 → raw 50 分的 delta 进 base 平均只剩 22~33；
   romantic 只有越过 strong mark 才拿到 0.2 权重的全额贡献（R 系对存活率
   0.53~0.61，非 R 系 0.20~0.44）。
3. **Noul transform**：对弱-中 romantic 差异是**清零级**压缩（8/13 对）。
4. **message_weight 的 evidence/4 × conf 耦合**：中等信息事件系统性降权，
   加剧 1。
5. **MIN_TOTAL_WEIGHT 悬崖 / recent 窗口退化**：边界效应，改变“能否出分”
   与“recent 看到什么”。

与最初假设的偏差（如实记录）：Issue 预设“weighted mean 是最大问题”。
数据表明**短会话里 base_score 构成的压缩量与 weighted mean 相当甚至更大**
（P1：raw 50 → base 22.5 是第一次腰斩，→ overall 10.95 是第二次）；
weighted mean 的支配地位主要体现在长会话（≥10 条）。

## 6. 附带发现（不在 Issue 预设内）

- **base_score 方向语义缺陷**（合成 + 公式结构实证）：尊重边界 vs 继续施压
  的对照里，施压侧 overall 更高 15.4 分。负向关系行为没有负向通道
  （只有 distancing 惩罚项）。**留给 #18/#19，本 Issue 未改。**
- **真实 Jev 概率序列化到 2 位小数**：score 与 E[probs] 偏差最大 0.04；
  任何用 probabilities 重算期望的工具需容差。
- **低信息会话不出分**是生产既有行为（6/43），MIN_TOTAL_WEIGHT 附近存在
  0.487→None 的悬崖。

---

## 7. 对 #18 Relationship Profile v2 的数据驱动建议

1. **不要把 profile 做成单数字**：7 个模式的 overall 中位数挤在 15 分带内
   （2.3 节），单指标无区分力。profile 应是**多轴向量**（主动性 / 关心回应 /
   特殊关注 / 浪漫 / 边界行为 / 熟悉度），轴的证据分别汇总。
2. **保留并使用 probabilities + confidence**：同 score 不同分布的方差 0→2
   完全被现行链路隐藏（3.4 节）。profile 应携带每轴的分布方差 / 熵 /
   confidence，标出“不确定”而不是给伪精确均值。真实 Jev 概率只存 2 位小数
   （容差 0.04），期望值重算要留余量。
3. **Noul 保留 raw 双轨**：floor 清零了 8/13 对的弱 romantic 差异（3.3 节）。
   profile 展示层可以同时呈现 raw 概率区间（如 0.15~0.28 = “弱/存疑”）与
   transformed evidence，避免“0 证据 = 无差异”的误读。
4. **方向分轴**：正向（关心/特殊关注/浪漫）与负向（施压/无视边界/疏离）
   必须分开陈述，不得加权相抵（D5 证据：方向混合会把施压抬到尊重之上）。
5. **evidence 乘子降权要谨慎**：ev=2 的有效消息只值 0.35~0.5 权重（3.2 节）；
   profile 的证据计数不应再叠一层 evidence/4 折扣。

## 8. 对 #19 Salience-aware aggregation 的数据驱动建议

1. **稀释是可定量的，修法要对着曲线修**：stress 3.1 的 dilution 表就是
   现状基线（10 条 mid = −46 分）。候选方案（top-k salient 窗口 / 分位数
   截尾 / 双指标“基线+峰值”）应在此 benchmark 上先跑 `--compare`。
2. **必须修 base_score 的方向语义**：给负向关系行为独立通道
   （新惩罚项或正负双轴），否则 salience 加权只会让高信息施压消息
   权重更大、更糟（D5：weight 0.637 > 0.562）。
3. **recent 窗口在 N>10 后退化**（3.1 节）：强事件被移出窗口后 recent 完全
   由填充决定。时间加权或“最近高信息事件”视图值得并列保留。
4. **MIN_TOTAL_WEIGHT 悬崖需要平滑**：0.487→不出分 vs 0.50→出分
   （6/43 案例落在坑里）。建议改成连续的“信息覆盖率”展示而非硬开关。
5. **先修聚合层再考虑改 Jev**：真实数据表明 Jev raw 对强对比可区分
   （rom Δ0.63~0.80、dist Δ0.95+、spec Δ1.15+），压缩主要发生在本地链路。

## 9. 是否需要未来修改 Jev schema（证据汇总，本 Issue 不改）

- **有证据的缺口**：负向关系行为（施压 / 无视边界 / 情感胁迫）没有独立
  question——D5 对照中该语义只能通过 engagement/special/romantic 的升高
  间接体现，方向丢失。建议 #20（或 schema 迭代）评估新增 valence /
  boundary 类问题，**并在 benchmark 上先验证 separability**。
- **暂不构成 schema 缺口**：关心分层、特殊关注、浪漫、疏离在真实 raw 上
  都有可测 delta（2.1 节表），问题主要在聚合而非表达。
- **弱信号区分**（同句不同前文 |Δ|≤0.51）：可能是 schema 上下文敏感度
  不足，也可能是该对比本身在人类眼里差异就小——需要真实多评分者标注
  （field study 设施）才能判别，**目前无法实证**。

---

## 10. 复现与回滚

### 复现（全部离线、确定性）

```powershell
# 既有 harness 上验证新 benchmark（43/43）
.venv\Scripts\python scripts\evaluate.py --fixtures `
  --cases evaluation/cases_relationship_v0.4.json `
  --fixture-file evaluation/fixtures/baseline_v0.4_relationship.json

# 三层诊断（合成链路）
.venv\Scripts\python scripts\run_score_diagnostics.py

# 附带既存真实 raw（gitignored，本地存在才可用；缺失时报告自动标注 no-live-API 限制）
.venv\Scripts\python scripts\run_score_diagnostics.py `
  --real-raw evaluation/reports/v32_contrast_derived_raw.json `
             evaluation/reports/v31_run2_main34_raw.json `
             evaluation/reports/v31_run1_distancing_raw.json `
             evaluation/reports/v32_phase2_raw.json

# 测试
.venv\Scripts\python -m pytest tests -q
```

真实 raw 的来源与完整性（只读研究，永不提交）：`v32_contrast_derived_raw.json`
= v3.2 对照运行的 12 条 + `cs_care_understanding_help` 补跑的 1 条（已逐字节
核对 provenance）；其余为对应案例集的人工批准真实运行。它们位于 gitignored
的 `evaluation/reports/`，**不会进入任何提交**。

### 回滚

本 Issue 的全部改动 = 新增文件（benchmark 数据、诊断模块、CLI、测试）+
文档增补。生产代码（`analyzer.py` / `scoring.py` / `app.py` 等）零改动。
回滚方式：删除下列新增文件 + revert 本 Issue 的提交即可完整恢复原生产行为
（downtime = 0，无 migration）：

```
evaluation/cases_relationship_v0.4.json
evaluation/relationship_pairs_v0.4.json
evaluation/fixtures/relationship_conversations_v0.4_synthetic.json
evaluation/fixtures/baseline_v0.4_relationship.json
score_diagnostics.py
scripts/run_score_diagnostics.py
tests/test_score_diagnostics.py
tests/test_relationship_benchmark.py
docs/issue17-score-compression-research.md
```

（`evaluation/README.md` / `AGENTS.md` 的文档增补随 revert 一并还原。）

---

*benchmark 通过率与本文所有指标都是人工定义案例上的研究信号，不是科学
准确率；synthetic 结果不是真实 Jev 输出。*
