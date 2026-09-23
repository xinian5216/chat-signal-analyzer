# SignalLens Evaluation Harness（离线质量评估）

`evaluation/` + `evaluation.py` + `scripts/evaluate.py` 构成一套**离线**评估设施，
用于长期衡量 SignalLens 的分析是否“合理”。它是算法修改前的**质量门槛**，
不是准确率科学实验。

## 原则

- **不判断 Jev 绝对对错**：每个 case 只描述“应该识别出的证据 / 不应得出的结论 /
  可接受的歧义 / 维度的期望范围或关系”，不写死总分。Jev 输出有概率性，
  我们关心的是**判断方向**与**是否过度推断**。
- **Evaluator 是确定性的纯函数**：绝不调用 Jev，也绝不用第二个模型评判 Jev。
- **benchmark 通过率不是“科学准确率”**：它只是人工定义案例上的
  regression / evaluation 指标。改善只能按“人工约束满足得更多”来宣布，
  不能把模型输出数值变化本身当成改善。
- **全部聊天内容虚构**：不得包含真实昵称 / 隐私信息（加载时会做 PII 扫描，
  RFC 保留域名除外）。

## 数据

| 文件 | 说明 |
|---|---|
| `cases.json` | benchmark 案例集（34 个，分类 A–Z） |
| `fixtures/baseline_v2.2.json` | **合成** baseline 结果（CI / 框架自测用；不代表真实 Jev 输出） |

### case schema

```json
{
  "id": "b_cold_care",
  "category": "B",
  "title": "普通朋友关心：感冒多休息",
  "me": ["我"],
  "them": ["TA"],
  "chat": "（微信块 / 冒号 / 时间+昵称格式的虚构聊天）",
  "target": "要分析的 TA 消息",
  "expectations": {
    "emotion": {"allowed": ["caring", "calm"]},
    "intent": {"allowed": ["show_care"]},
    "warmth": {"min": 2, "max": 4},
    "romantic_signal": {"max_probability": 0.5, "min_probability": 0.0},
    "distancing_signal": {"max_probability": 0.4}
  },
  "must_not_infer": ["romantic_from_care_alone", "special_attention_overclaim"],
  "acceptable_ambiguity": ["关心可以解读为礼貌性关心"],
  "notes": "普通友情足以解释",
  "tags": ["anti_overclaim"],
  "context_checks": {"must_contain": [], "no_future_leakage": true}
}
```

- `expectations`：emotion/intent 用允许集；5 个 Score 维度用 `[min, max]`；
  两个 Noul 维度用 `min_probability` / `max_probability`（用于真阳性对照）。
- `must_not_infer`：反过度推断契约，当前支持的标签（阈值集中在
  `evaluation.py` 的 `MUST_NOT_INFER_RULES`，便于人工审查）：

  `romantic_from_politeness`、`romantic_from_care_alone`、`romantic_from_teasing`、
  `romantic_from_invitation`、`romantic_from_late_night`、
  `romantic_from_self_disclosure`、`romantic_from_familiarity`、`romantic_from_media`、
  `special_attention_overclaim`、`distancing_from_slow_reply`、
  `distancing_from_short_reply`、`perfunctory_from_short_reply`、
  `single_incident_overgeneralization`。

- `context_checks`（可选）：`no_future_leakage` 校验 Context Builder v2 送出的
  窗口不包含目标消息之后的内容；`must_contain` 校验目标“正在回应”的 turn 内容
  完整进入上下文（连续消息 / 自我披露场景）。

### 分类（A–Z）

普通礼貌（A）、普通朋友关心（B）、高熟悉度朋友（C）、调侃无暧昧（D）、
明显暧昧（E，真阳性）、邀约但可能只是朋友（F）、特殊关注（G）、
主动延续话题（H）、对自我披露的认真回应（I）、敷衍（J）、礼貌结束（K）、
明确拒绝（L）、明显疏离（M）、冲突（N）、情绪低落信号弱（O）、
高 ease 低 romantic（P）、高 warmth 低 evidence（Q）、高 evidence 负面方向（R）、
歧义句（S）、需要 Context Builder v2（T）、me 连续自我披露（U）、
them 连续短消息 turn（V）、工作事务（W）、夜间聊天（X）、回复间隔长（Y）、
媒体 marker（Z）。其中 14+ 个 case 带 `must_not_infer`，构成反“读心”回归门槛。

## 使用

```bash
# fixture 模式（默认，离线，CI 可用）
python scripts/evaluate.py --fixtures
python scripts/evaluate.py --fixtures --report evaluation/reports/base.json

# baseline vs candidate（约束维度对比，只按人工约束定义改善/回归）
python scripts/evaluate.py --compare base.json candidate.json

# 真实模式（人工专用；测试 / CI 永远不触发）
python scripts/evaluate.py --real --yes-run-live-api    # 且需 TYPESAFE_API_KEY
```

`--real` 双重门禁：必须显式 `--yes-run-live-api` 且存在 `TYPESAFE_API_KEY`，
否则拒绝运行（pytest / CI 环境另外直接拒绝）。真实模式的请求语义：

- 每个案例**只分析指定的 TA target**（内部使用 `analyze_messages` 的
  `only_indices`），N 个案例 = N 次请求；案例内其他 TA 历史消息不会被
  额外分析，只作为上下文；
- 目标位置由 case 的 target 文本解析为**原始消息 index**，结果选取按
  index 比对（重复文本不会错配）；
- 运行前打印预计请求数量；
- 单条请求失败 / 结果缺失只记录该案例（聚合里计 `api_error` /
  `missing_result`），不中断整个基线；
- `--report PATH` 写**聚合评估报告**（含 meta：schema 版本、模型、
  Context Builder 预算、案例数、cases.json SHA256），可被 `--compare`
  直接读取；`--raw-report PATH` 单独存放原始模型输出。

真实模式的结果可保存为匿名 JSON，作为后续 v3 的 baseline / candidate
对比输入。

## 修改算法前必须先跑 benchmark

后续修改 Jev questions / Context Builder / scoring 时：

1. 先跑 baseline（fixture 或已保存的真实结果）得到报告；
2. 应用候选改动，再跑一次；
3. 用 `--compare` 汇报：哪些约束失败消失（改善）、哪些新增（regression）；
4. 不得把 benchmark 分数包装成“科学准确率”。
