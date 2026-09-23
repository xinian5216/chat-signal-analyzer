# v3 人工复核记录：distancing 语义修正前的基准争议

本文件是 v3 Phase 1 的人工复核记录，**不修改** `evaluation/cases.json` 的
34 个案例及其阈值。v2.2 真实基线（jev-1.13.0，2026-09-23，17/34 案例，
362/386 约束）的冻结副本：

- `evaluation/reports/frozen_v2.2_real.json`（聚合；SHA256 C8DB0106…187E）
- `evaluation/reports/frozen_v2.2_raw.json`（原始；SHA256 5B6B05F0…B7A）

（reports 目录 gitignored；冻结副本仅用于本地防覆盖，不提交。）

## 一、对失败案例的人工复核

| 案例 | v2.2 实际 | 阈值 | 复核结论 |
|---|---|---|---|
| `l_friendzone` | distancing 0.42 | ≥0.5 | **阈值有争议，暂不改**。浪漫边界（“只当很好的朋友”）不必然等于一般性关系疏离；v3 的 distancing 定义改造后，该案例的语义前提本身需要重新讨论（浪漫边界 ≠ 关系疏离）。在 v3 泛化案例集中用专门 case 表达“浪漫边界但普通往来继续”。 |
| `d_tease_no_flirt` | emotion=annoyed、intent=share_opinion、warmth=0.08、evidence=3.02 | teasing/tease、warmth≥1、evidence≤2 | **争议：上下文不足**。“你这审美没救了，拍成这样也敢发”没有语气线索，调侃与真实冒犯都是合理解释； warmth≈0 属于模型在缺失线索时的负面兜底。处理：不改阈值；在 v3 泛化集中加入**有明确调侃线索**的对照 case（前文互相吐槽模式），区分“缺线索”与“有线索仍判错”。 |
| `g_remembers_detail` | special_attention 2.16 | ≥3 | **争议：普通朋友基线**。“你上次说过啊，我就记住了”是否超过普通朋友基线，人工可辩。暂不改阈值；列为本轮之后的观察项（special_attention 语义不在 v3 Phase 1 范围）。 |
| `p_ease_high_romantic_low` | relational_ease 3.27 | ==4 | **阈值过严，暂不改**。要求恰好等于 4 而模型给出 3.27，且 warmth/engagement/romantic 全部达标；本 case 的核心断言（ease 高、romantic 低）未被违反。作为阈值记录保留，供 v3 之后阈值校准时参考。 |
| 贴线 warmth/engagement（`i_disclosure_support` 2.92、`u_disclosure_burst_long` 2.92、`q_warm_high_evidence_low` 2.25、`c_shared_meme` 1.81、`y_slow_friendly_reply` 1.97） | 均为轻微校准差异 | — | **判定为校准差异，非方向性错误**。distancing v3 不处理；warmth 校准列为后续观察项。 |

**核心语义发现（驱动 v3 改造）**：distancing 在礼貌收尾（`k_polite_close` 0.69）、
语音+推迟（`z_media_voice_context` 0.67）、疲劳短句（`o_tired_low_mood` 0.51）
上系统性误报，且 `l_friendzone`（浪漫边界）未达到 0.5——说明旧定义
“结束交流、回避互动、降低投入或刻意拉开距离”把**会话层行为**与
**关系层疏离**混为一谈。v3 将 distancing 收窄到“对持续互动 / 双方关系的
明确疏离”，显式排除礼貌收尾、计划稍后再聊、一次短回复、临时疲劳、
单话题拒绝与浪漫边界。

## 二、harness 修复记录（非阈值）

v2.2 真实基线首次运行在 34/34 请求完成后因 `_score_under_category`
缺少 `special_attention` 映射而崩溃、响应丢失（后修复为五维度
over/under 分类表 + 原始结果先落盘）。本次复核同时修正 evaluator 的
一个提示文本缺陷：emotion 与 intent 两条约束的 detail 都写成
`emotion/intent=…`，改为按维度名输出。**仅文本，不影响任何判定**。

## 三、v3 前固定（pre-registered）的独立案例设计原则

`evaluation/cases_distancing.json` 的预期**在看过任何 v3 模型输出之前**
固定，覆盖：礼貌收尾、真实关系后撤、普通友情边界（浪漫边界但往来继续）、
持续拒绝联系、临时疲劳、礼貌但不想继续某话题、计划稍后再聊、
单条短回复等。任何事后调整都必须作为新记录追加到本文件，不得静默改标签。
