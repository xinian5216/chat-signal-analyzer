# SignalLens

<p align="center">
  <b>A privacy-conscious, Jev-powered analyzer for observable emotional, conversational, and relationship signals in chat messages.</b>
</p>

SignalLens 是一个**本地单机**的 Streamlit 工具：粘贴一段聊天文本，使用
[TypeSafe Jev](https://typesafe.ai/)（System One 决策模型）对 TA 的每一条消息做
结构化分析，并按固定公式聚合出「互动亲近信号指数」。

---

## English Quick Start

- **What it is** — a local Streamlit app that uses TypeSafe Jev to classify
  emotion and intent, score warmth / engagement / special attention, and
  aggregate an *interaction closeness signal index* from chat text.
- **Privacy** — chat content is **locally redacted** (phone / email / ID / IP /
  URL / secret / card) before anything is sent to the TypeSafe Jev API.
- **Not mind-reading** — results reflect **observable textual interaction
  signals only**. They do **not** represent anyone's true mental state and are
  **not** a probability that someone likes you. Do not use this tool to decide
  whether another person is interested in you.
- **No generative LLM** is used for interpretation or summaries; all text is
  produced by deterministic templates from the existing structured results.

```bash
pip install -r requirements.txt
copy .env.example .env        # then set TYPESAFE_API_KEY=...
streamlit run app.py          # or: python -m streamlit run app.py
```

Requirements: Python 3.11+ and a TypeSafe Jev API key.

See [PRIVACY.md](PRIVACY.md) for the full privacy model and
[SECURITY.md](SECURITY.md) for how to report vulnerabilities.

---

## 1. 项目是什么

- 解析“我”和“TA”的消息（只分析 TA 发出的消息）。
- 对每条 TA 消息**只发一次 Jev 请求**，同一次请求并行取得全部指标：
  - **情绪 Choice**：平静 / 高兴 / 调侃 / 好奇 / 疑惑 / 惊讶 / 关心 / 不满 / 尴尬 / 低落 / 其他（完整概率分布）
  - **意图 Choice**：询问 / 确认 / 解释 / 表达观点 / 延续话题 / 关心 / 调侃 / 邀约 / 分享个人 / 结束话题 / 敷衍 / 疏离 / 其他（完整概率分布）
  - **Score ×5**：温暖程度、对话投入程度、特殊关注程度、关系信息量、互动熟悉度（各 5 级，含各级概率与 confidence）
  - **Noul ×2**：暧昧 / 浪漫信号概率、疏离 / 结束对话信号概率（0~1）
- 由**程序**（不是模型）按固定公式聚合出「互动亲近信号指数」，并给出
  整体 / 最近 10 条 / 前半段 vs 后半段统计与趋势。
- **互动熟悉度（relational_ease）**：v2.1 新增的**解释层**指标，衡量互动的
  自然、熟悉、轻松与默契程度，**不计入**互动亲近信号指数（详见第 9 节）。

## 2. 为什么不是“读心工具”

「互动亲近信号指数」**只代表聊天文本中可以观察到的**亲近、主动、投入、暧昧、
疏离等信号的组合，**不代表对方真实心理状态**，更不是“TA 喜欢你的概率”。
Jev 只回答你提出的窄问题（分类 / 评分 / 概率），所有解读和聚合权重都由本程序的
代码决定；请勿把输出当作对他人内心的确定判断。

## 3. 安装步骤

需要 Python 3.11+（开发时使用 3.11.16）。

```bash
# 创建虚拟环境（Windows）
python -m venv .venv
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

（也可使用 `uv venv --python 3.11 .venv && uv pip install -r requirements.txt`。）

## 4. TypeSafe API Key 配置

1. 在 <https://console.typesafe.ai/> 创建 API Key。
2. 复制 `.env.example` 为 `.env`，填入：

    ```
    TYPESAFE_API_KEY=your_typesafe_api_key_here
    ```

    也可用环境变量 `TYPESAFE_DEFAULT_MODEL` 覆盖默认模型（默认 `jev-latest`）。

- `.env` 已在 `.gitignore` 中，**永远不会进入仓库**。
- 代码中不硬编码 Key；日志与错误信息不会打印 Authorization header
  （SDK 默认会对 secret header 打码，错误分类也只使用状态码与异常类型）。

## 5. 启动命令

```bash
.venv\Scripts\activate
streamlit run app.py
```

浏览器自动打开 <http://localhost:8501>。

### Windows 一键启动（推荐）

不想记命令的用户：

```
1. 双击 launcher\install.bat   （首次：创建 .venv 并安装依赖）
2. 双击 launcher\start.bat     （每次：启动并自动打开浏览器）
```

- `install.bat`：自动寻找 Python 3.11+，在项目内创建 `.venv`，
  安装 `requirements.txt`；**不要求管理员权限，不修改系统 Python**。
- `start.bat`：只监听 `127.0.0.1:8501`，无头模式，等待端口就绪后自动打开
  浏览器；**已在运行时不会启动第二个实例**；项目路径含空格也能正常工作
  （脚本使用自身目录定位，不依赖当前工作目录）。

## 6. 输入格式示例

支持三种格式（可混合），也支持全角冒号与多行消息：

```
我: 你刚才怎么一直没回我
TA: 可能比较沉浸
我: 玩游戏吗
TA: 对哈哈
```

```
22:31 我
你干嘛呢

22:32 TA
刚洗完澡
```

微信 PC 端复制的“昵称 / 日期时间 / 内容”三行格式（消息之间通常有空行），
**每条消息都有昵称行**，昵称为任意中文 / 英文 / 数字组合：

```
昵称A
2026年09月08日 0:09
消息内容

昵称B
2026年09月08日 0:10
消息内容
```

- 时间支持 `YYYY年MM月DD日 H:mm`、`YYYY-MM-DD HH:mm`、`YYYY/M/D H:mm`、`HH:mm`
  （可带秒），统一规范化为 `YYYY-MM-DD HH:MM`。
- **发言人识别（不猜内容）**：解析后 UI 会自动检测本次聊天中出现的参与者昵称，
  由你用下拉框选择“哪个昵称是我 / 哪个昵称是 TA”。检测到两个昵称时
  选项已备好，但**不会擅自判断**哪一侧是你；指定后重新解析即可。
  也可只指定一侧，其余具名发言人自动归入对面。
- 单条消息无法确定发言人时标记为 **unknown**，**不会导致整个文件解析失败**。
- 解析后先显示“解析预览”（总条数、我/TA/unknown 计数与前 15 条明细）。
  只要存在 unknown，默认**禁止**开始 Jev 分析；可指定昵称映射后重新解析，
  或勾选“忽略 unknown 消息”后再分析。

解析后的统一结构：

```json
[{"speaker": "me", "text": "...", "time": "2026-09-08 00:09", "content_type": "text", "media_kinds": []},
 {"speaker": "them", "text": "[发送了一张图片，内容未知]", "time": "2026-09-08 00:10",
  "content_type": "media", "media_kinds": ["image"]},
 {"speaker": "unknown", "text": "...", "time": "2026-09-08 00:20"}]
```

### 非文本媒体占位符（微信复制文本）

微信复制出来的文本里，图片 / 视频 / 动画表情 / 语音 / 文件只是**占位符**
（`[图片] 微信图片_xxx.dat`、`[视频] 微信视频_xxx.mp4`、`[动画表情]`、
`[语音]`、`[文件] xxx`）。Jev 不具备图像输入能力，复制文本本身也不含实际媒体内容，因此：

- **纯媒体消息**：标记 `content_type="media"`，**不作为 Jev target、不产生
  API 请求**，也不计入 analyzed / effective / overall / recent / trend /
  信息覆盖率等任何统计；解析预览中正常显示，但标记为
  “图片 / 视频 / 动画表情 / 语音 / 文件（内容未分析）”。
- **位于上下文中的纯媒体消息**：不删除，转换为中性 marker
  （`[发送了一张图片，内容未知]` 等），并明确禁止 Jev 猜测媒体内容或情绪。
- **文字 + 媒体混合消息**（如“你看这个 [图片] xxx.dat”）：保留文字
  “你看这个”，媒体部分替换为中性 marker，该条仍然正常分析。
- **普通 Unicode emoji（😂、😭、❤️）不是媒体占位符**，原样保留并参与分析。
- 本地媒体文件名（`微信图片_...dat` 等）会被剥离，**不会进入报告导出**。
- 分析结果顶部显示“跳过非文本媒体：X 条”；Markdown / JSON 报告 metadata
  记录 `skipped_media_messages`。
- 本项目**不引入视觉模型、不读取 .dat/.mp4、不做 OCR**。

### Clipboard Probe：浏览器实际能拿到什么（v0.2.0 第一阶段）

微信复制的富媒体数据在浏览器里到底能拿到多少，需要实测。左侧栏
**高级 → 剪贴板诊断（Clipboard Probe）** 提供一个自包含的诊断区：

1. 在微信 PC 选中聊天 → `Ctrl+C`
2. 在 Probe 区 `Ctrl+V`
3. 组件内直接显示：`clipboardData.items` / `files`、MIME、图片数量与尺寸、
   item 顺序、文本内媒体占位符数量，并可导出**仅含元数据**的诊断 JSON
   （不含聊天文本、图片内容、文件路径）。

**当前实现状态（重要，请勿误解）**：

- Probe 的诊断结果在 **iframe 内自包含展示**。原因是 Streamlit 1.64 下
  自定义组件协议可以把消息投递到父窗口，但**组件值无法稳定回传到 Python**
  （已实测：ready 握手与消息投递正常，`setComponentValue` 后 Python 侧仍
  收到 `None`）。本轮因此不宣称“微信复制的图片自动进入 SignalLens”。
- 主流程的图片输入使用**原生 file_uploader**（点击或拖拽），作为普通用户
  的 fallback：上传的图片仅存于本机内存，不发送给 Jev，不写入报告。
- 绑定逻辑（`media.py`）对两种来源一致：1 占位符 + 1 图自动绑定；
  N:N 需要顺序经过 Probe 实测确认；数量不匹配一律交给用户手动匹配。
- 图片语义识别**尚未实现**（`vision.py` 仅预留接口，默认禁用）。
- 待完成真实微信 Probe 实测后，再决定下一阶段的前端桥接方案
  （Streamlit components v2 / 本地小助手等）。

## 7. 隐私说明
- Jev 是云端 API。**任何内容发出前都会先本地脱敏**：
  手机号、邮箱、身份证、IP 地址、URL（连 token 一起）、明显的 API Key、
  银行卡号形式的长数字，分别替换为 `<PHONE>` `<EMAIL>` `<ID>` `<IP>`
  `<URL>` `<SECRET>` `<CARD>`。
- 真实姓名**不自动猜测**（避免误替换），请自行确认文本中是否含真实姓名。
- 脱敏是 MVP 级启发式规则，**不能替代人工检查**——粘贴前请自行确认。
- 本地缓存（`.jev_cache/cache.db`）保存的是**脱敏后**内容的分析结果，
  已被 `.gitignore` 排除。
- 更完整的隐私模型见 [PRIVACY.md](PRIVACY.md)。

## 8. Jev 调用了哪些 primitives

每条 TA 消息一次 `client.system_one(state, questions)` 请求，包含 9 个问题（schema v2.1）：

| 指标 | primitive | 返回 |
|---|---|---|
| emotion | Choice（11 选项） | `choice` / `probabilities` / `confidence` |
| intent | Choice（13 选项） | 同上 |
| warmth / engagement / special_attention | Score（各 5 级） | `score`(0~4) / `probabilities` / `confidence` |
| relationship_evidence_strength | Score（5 级） | 同上 |
| relational_ease（v2.1 新增，解释层） | Score（5 级） | 同上 |
| romantic_signal / distancing_signal | Noul（0~1 概率） | `noul`（无 confidence 字段，官方设计如此） |

- `state` 只包含：目标消息 + 之前最多 5 条上下文 + 发言人身份 + 可选时间 +
  一条分析规则（“只判断可观察信号，不得仅凭礼貌推断浪漫兴趣”）。
  **不包含任何未来消息**——模拟“当时看到这句话时能判断出什么”。
- **缓存 schema 版本 v2 → v2.1**：v2.1 新增 `relational_ease` 问题。
  缓存 key 由（state + 问题 schema + 模型 + schema 版本）的 SHA256 构成，
  旧 v2 缓存条目**自然失效**（不会冒充新 schema 结果），不会被删除。
- 官方文档：<https://docs.typesafe.ai/>；SDK：<https://github.com/typesafe-ai/typesafe-sdk-python>
  （第三方 SDK 遵循其各自许可证，与本项目 MIT 许可证相互独立。）

## 9. 指标是怎么计算的（聚合逻辑 v2）

第 1 步——单条消息（`scoring.py`，权重与阈值全部集中配置）：

```
# Noul 原始概率 → 证据强度（原始概率只用于展示，不进公式）
transform_noul_evidence(p):
    p <= 0.30            → 0          （视为没有明确证据）
    0.30 < p < 0.70      → ((p-0.30)/0.40) * 0.35      （弱/不确定证据）
    p >= 0.70            → 0.35 + ((p-0.70)/0.30) * 0.65 （明确证据）
# 性质：0.10→0，0.30→0，0.50→0.175，0.70→0.35，0.85→0.675，1.00→1

base_score = 0.30*(warmth/4) + 0.25*(engagement/4) + 0.25*(special_attention/4)
           + 0.20*romantic_ev - 0.15*distancing_ev        # clamp 0~1
# base_score 只表示“这条消息若有关系判断价值，它偏向什么方向”

evidence_norm       = relationship_evidence_strength / 4
relation_confidence = mean(warmth.conf, engagement.conf, special.conf)
                      # 缺失时按 0.5 并记录 warning
message_weight      = clamp(evidence_norm * relation_confidence, 0, 1)
```

第 2 步——聚合（**不再等权平均**）：

```
overall = Σ(base_score × message_weight) / Σ(message_weight) × 100
recent  = 最近最多 10 条 TA 消息的同样加权平均
温暖/投入/特殊关注 = 各自 Score 的 message_weight 加权平均
暧昧/疏离总体 = 转换后 evidence 的 message_weight 加权平均
```

- 若 `Σ(message_weight) < 0.5`：不输出看似精确的分数，
  显示“当前样本缺少足够的关系层面信息，暂不生成可靠的互动亲近信号指数”。
- “哦”“嗯”“哈哈”类低信息量短回复 `message_weight ≈ 0`，
  **不会稀释总体结论**，也不会被解释为“TA 关系冷淡”。
- 总体暧昧 / 疏离主展示使用转换后 evidence 的文字等级
  （未发现明显信号 / 存在少量弱信号 / 存在一定信号 / 存在较明显信号 / 存在强信号），
  **不显示成“5% / 20%”这类容易被误读为现实心理概率的数字**；
  原始 Noul 概率仅放在折叠 debug 区。
- 趋势：TA 可分析消息 < 6 → “样本不足”；任一半段 Σweight < 0.5 → “有效信息不足”；
  否则后半段 − 前半段 ≥ +8 → 上升，≤ −8 → 下降，其余基本稳定。
- conversation-level 行为统计（主动询问 / 延续话题 / 主动关心 / 个人分享 /
  邀约 / 调侃 / 低投入回应 / 结束话题 / 疏离意图）基于 intent **完整概率分布**
  加权平均，目前**仅作解释层展示，未计入总分公式**。

### 互动熟悉度（relational_ease，v2.1 解释层）

```
relational_ease_avg = Σ(relational_ease × message_weight) / Σ(message_weight)
```

- 文字等级（阈值集中在 `scoring.py`）：`<0.9` 较生疏；`<1.7` 偏正式 / 熟悉度较低；
  `<2.5` 自然熟悉；`<3.3` 较熟悉、互动轻松；否则 高度熟悉 / 明显默契。
- **衡量**：互动是否自然、熟悉、轻松、有默契。
- **不等于**：喜欢、浪漫兴趣、暧昧、特殊关注。
  例：“哈哈你又来了”可能熟悉度高但暧昧信号低。
- **不进入** base_score / message_weight / overall / recent / trend /
  romantic / distancing 任何总分公式，仅用于单条解释、顶部汇总、报告与未来校准观察。

### 低信息量展示模式（LOW_EVIDENCE_DISPLAY_MODE）

当满足任一条件时（阈值集中在 `scoring.py`）：

- 有效关系消息 < 2；
- `Σ(message_weight) < 0.75`；
- 关系信息量标签为“较低”；

界面**不再把 overall 当主指标**，改为：

```
⚠ 当前样本关系信息不足
以下指数仅作参考，不建议据此判断整体关系亲近程度。

参考指数：XX / 100
有效消息：X / N
关系信息量：较低
```

目的是避免用大号总分制造“你们关系只有 XX 分”的错觉。
overall 仍然计算并展示，只是降低视觉优先级；Markdown 报告会附带同样的免责说明。

置信度展示（阈值集中在 `scoring.py`，可自行调整）：

- Choice / Score：`confidence ≥ 0.70` 结论较明确；`0.45~0.70` 存在一定歧义；`< 0.45` 难以判断。
- Noul：`≥ 0.70` 明显存在该信号；`0.30~0.70` 不确定；`< 0.30` 缺乏明显该信号。

**概率分布始终完整展示，不会被隐藏。**

## 10. 当前限制

- 只支持“复制文本后粘贴”，不支持微信数据库解密、OCR、语音 / 图片 / 表情语义。
- 解析器只覆盖最常见文本格式；特殊格式请用昵称兜底或手工整理。
- 脱敏为正则启发式，可能漏检或误检。
- 上下文窗口最多 5 条；不跨“引用回复”等结构。
- Jev 对中文网络用语的效果未经系统校准；结果仅供娱乐与参考。
- 若发现 Jev 对中文效果明显不好，请保留原始结果反馈，本项目不会偷偷换模型。

## 11. API 成本 / 调用次数说明

- **每条 TA 消息恰好 1 次 API 请求**（9 个问题合并一次调用，官方证实并行提问
  更便宜更快），N 条 TA 消息 = N 次请求。
- 同一条“消息 + 上下文 + 问题 schema + 模型版本”会计算 SHA256 缓存 key，
  命中本地 SQLite 缓存时**不消耗 API**（`.jev_cache/cache.db`）。
- SDK 自动重试：`max_retries=2`，指数退避，覆盖 408 / 429 / 5xx 与超时；
  重试耗尽后该条标记失败，可手动“重新分析失败项”，不会无限重试。
- **报告导出（摘要 / Markdown / JSON）完全基于已分析完成的数据，
  0 次额外 API 请求。**
- 具体定价见 <https://typesafe.ai/>。

## 12. 报告导出

分析完成后，在顶部汇总区域下方提供：

- **分析摘要（可复制）**：确定性模板生成的简短总结，不使用生成式 LLM；
- **下载 Markdown 报告**：`chat-analysis-YYYY-MM-DD-HHMM.md`，
  适合直接保存到 Obsidian，包含基本信息 / 总体结果 / 整段互动行为 /
  主要关系信号（evidence 最高最多 5 条）/ 低信息量消息 / 趋势 / 方法说明；
- **下载 JSON 原始结果**：metadata / summary / aggregate / behavior_stats /
  messages（含每条的 raw 与 transformed evidence、base_score、message_weight 等）。

隐私选项：

- **☑ 报告中包含原始聊天内容**（默认**关闭**）。关闭时导出匿名报告：
  Markdown 中只显示“TA 消息 #N”，JSON 的 `text` 字段为 `null`。
- 默认文件名不含昵称。导出内容不含 API Key、缓存路径、异常堆栈。

需要 PDF？在浏览器中按 `Ctrl+P` → 另存为 PDF（打印当前页面即可）。

## 13. 如何删除本地分析缓存

任选其一：

- 界面左侧栏点击“清除本地分析缓存”；
- 或直接删除目录：`.venv\Scripts\python -c "from storage import Cache; Cache().clear()"`
- 简单粗暴：删除整个 `.jev_cache` 文件夹。

## 14. SQLite 缓存的线程安全设计

- `Cache` 实例只保存 `db_path`，**不长期持有 `sqlite3.Connection`**；
  每次 `get/set/clear/初始化` 都通过 `_connect()` 获取短生命周期连接，
  用完立即关闭，因此同一实例可被 Streamlit 多个 rerun 线程安全复用，
  不会出现 `sqlite3.ProgrammingError: SQLite objects created in a thread
  can only be used in that same thread`。
- 连接使用 `timeout=5.0` + `PRAGMA busy_timeout=5000` + `PRAGMA journal_mode=WAL`，
  降低并发锁竞争；数据量小，不需要连接池。
- 缓存 key / schema / 数据与旧版完全兼容，既有缓存文件可直接读取。

## 测试与冒烟

```bash
# 单元测试（mock 响应，绝不调用真实 API）
pytest tests -v

# 真实 API 冒烟测试（仅 5 次请求；第二次运行应 0 请求全部命中缓存）
.venv\Scripts\python scripts\smoke_test.py
```

CI：GitHub Actions（`.github/workflows/tests.yml`）在 ubuntu-latest +
Python 3.11 上运行同一套测试，**不需要任何 API Key**。

## 项目结构

```
├─ app.py            # Streamlit UI（四阶段 + 报告导出）
├─ analyzer.py       # Jev 调用（每消息一次请求取全部指标）+ 错误分类
├─ parser.py         # 聊天文本解析 + 非文本媒体占位符识别
├─ media.py          # MediaAsset / 资源限制 / 占位符→图片保守绑定
├─ rich_paste.py     # 富媒体组件封装（含 file_uploader → MediaAsset 桥接）
├─ vision.py         # 视觉识别接口预留（默认禁用，尚未实现）
├─ report.py         # 报告导出（Markdown / JSON / 摘要，纯本地无 API）
├─ scoring.py        # 指数公式、聚合统计、置信度标签（阈值集中配置）
├─ privacy.py        # 本地脱敏
├─ storage.py        # SQLite 缓存（SHA256 key，短连接线程安全）
├─ ui_helpers.py     # 纯展示层辅助（短标签 / 徽章 / 过滤）
├─ components/rich_paste/index.html   # 剪贴板诊断组件（原生 JS，无依赖）
├─ tools/clipboard_probe/             # Clipboard Probe 报告格式化 + 实测清单
├─ launcher/         # Windows 一键启动（install.bat / start.bat）
├─ scripts/smoke_test.py
└─ tests/            # parser / privacy / scoring / storage / analyzer /
                     # report / media / launcher / ui / app flow（全部 mock）
```

## License

[MIT](LICENSE)。第三方依赖（Streamlit、typesafe-sdk、python-dotenv、pytest）
遵循其各自许可证。
