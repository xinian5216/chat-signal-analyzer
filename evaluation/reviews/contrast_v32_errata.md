# cases_contrast_v3.2.json 勘误记录（转录勘误，非预期调整）

**案例**：`cs_care_understanding_help`（关心分层 C：理解处境 + 提供帮助）

**问题**：首次真实分析前的目标定位校验发现，聊天正文写作
「我表哥做这行，我把合同条款帮你看看」，而预登记的 target 是
「我表哥做这行的，我把合同条款帮你看看」——正文漏一个「的」字，
导致 `resolve_target_index` 无法在 them 消息中定位 target，该案例在
v3.2 首轮评估中被记为 case setup failed（**未发起任何 API 请求**，
首轮日志中的 1 条 `api_error` 实为此设置错误，非模型/网络错误）。

**修正**：仅在聊天正文补上缺失的「的」字，使其与**原先预登记的 target**
完全一致。

**性质声明**：这是首次实际分析**之前**就存在的案例数据转录勘误，
不是根据模型结果调整预期。target、全部 expectations、其余 12 个案例、
任何历史报告均未改动。修正后需对该案例单独补评（1 次逻辑分析）。

**连带改进**：真实评估路径新增批次级前置校验（见
`scripts/evaluate.py` 的 `validate_cases_for_real`）：任一案例无法定位
target 时在首个请求前拒绝整批并列出全部问题案例；案例设置错误与真实
API 错误在聚合统计中分开记录（`case_setup_error` vs `api_error`）。
