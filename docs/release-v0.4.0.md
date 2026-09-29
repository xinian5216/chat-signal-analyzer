# SignalLens v0.4.0 Release Notes

- 版本：`0.4.0`（VERSION 文件）
- 前身：v0.3.0（chat-signal-v3.3，Jev 九问 / 评分公式不变）
- 数据兼容：analysis cache 可继续使用；friend history 无 migration；
  v0.3 的 data/ 保留即可升级

## Highlights

### Relationship Profile v2（主要解释层）

结果页以「关系画像」为主要解释层，把可观察信号拆成 7 个维度（主动性与投入 /
关心与回应性 / 互动熟悉度 / 特殊关注 / 浪漫·暧昧 / 关系疏离·后撤 / 边界压力），
每维分别给出 strength / coverage / direction / reliability / supporting /
counter evidence，并把「数据不足」「当前 schema 不支持」作为一等状态。
**没有新的综合关系分数**；legacy overall 保留但降级为辅助指标。

### Salience-aware evidence channel

普通互动决定 baseline；少量高信息关系事件以**分类事件**单独保留（不因普通
消息多而被平均消失）；相反方向证据独立存在（不抵消成「中等」）。事件首先
属于某个维度，没有全局正负 salience；`relationship_evidence_strength` 与
`message_weight` 不决定方向。

### Interaction Dynamics（本地 deterministic-first）

新的 turn / sequence 层描述互动结构：明显间隔后的重启、连续追问、邀约从模糊
到具体、互惠结构观察、**拒绝后的边界回应**。每个事件只由它结束位置之前的
消息决定（prefix invariance：未来的消息不会改变过去已知事件）。

### Boundary response（D5 的正式结构通道）

v0.3 的单消息评分无法表达「用户明确拒绝后 TA 仍持续推进同一请求」。v0.4
用 `explicit boundary opportunity → TA 后续响应 → 同一事项的回指式继续语 →
boundary_pressure 互动证据` 补上这条通道；边界压力维度从「恒不支持」升级为
机会驱动（没有可观察机会 → 数据不足，不是「边界压力低」）。**不创建尊重分 /
喜欢概率 / 人格标签。**

## Safety / interpretation

- evidence-first：每条结论都可回溯到消息级指标或结构窗口；
- 不读心、不输出「喜欢概率 / 关系健康度 / 综合关系分」；
- review-required 证据（personal recall 候选、模糊边界回应）绝不进入正式结论，
  只在待审核队列出现，由用户确认后才进入长期档案；
- 高歧义序列明确降级（recall 的语义改写 deferred；模糊边界一律待核对）；
- 纯媒体消息内容未知，不参与任何语义事件。

## Compatibility

- `chat-signal-v3.3` 不变：Jev 问题、请求次数、分析缓存键全部不变；
- 缓存可继续使用（v0.4 不要求重新请求 Jev）；
- friend history **无 DB migration**；v0.3 已确认的行为事件照常工作；
- JSON 报告为 additive 新键（`relationship_profile` / `salience` /
  `interaction_dynamics`），旧键不变；
- 升级 = 关闭程序 → 备份 data/ → 替换程序文件 → 启动；回滚 = 恢复旧程序文件
  （data/ 保留）。downtime = 0（本地程序）。

## Known limitations

- 真实世界准确率**未经科学验证**：算法基准是离线合成 benchmark
  （17/34/43/15/32 场景），只证明「若 Jev 给出这些信号，本地算法会怎样」；
- personal recall 仅对严格 distinctive-anchor 案例生成待核对候选；语义改写
  （paraphrase）deferred；
- 边界回应是词法规则，保守优先：无法绑定同一事项时一律待人工核对；
- 180 分钟是「明显间隔后重启」的操作性阈值，不是心理学阈值；
- 合成 benchmark 不等同真实准确率，不应作为宣传口径。

## Packaging

- Windows Portable：`SignalLens-v0.4.0-Windows-x64-portable.zip`
  （onedir，解压即用；只监听 127.0.0.1；无遥测）
- 校验：同页 `.sha256` sidecar；本地可用
  `python scripts/validate_release.py` 复核 ZIP 内容 / 版本一致性 / SHA256。
