# SignalLens 浏览器回归（全虚构数据 · 0 次真实 Jev 调用）

在真实 Chromium 里驱动 SignalLens 的端到端回归脚本。**所有聊天内容、昵称、
证据片段均为虚构**；分析结果来自本地预置缓存，全程 **0 次真实 API 请求**
（网络层有断言：任何非 localhost 请求即 FAIL）。

对应交接文档 `docs/handoff/2026-09-24-ui-wip.md` 第六/八节踩坑记录。

## 快速开始

```powershell
# 1) 安装依赖（一次性；已列入 requirements-dev.txt）
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m playwright install chromium

# 2) 运行（在仓库根，venv 里）
.venv\Scripts\python -X utf8 scripts\browser_acceptance\run_acceptance.py
```

脚本自动：① 在临时目录预置 182 条缓存；② 找空闲端口以 dev 模式启动
`streamlit run app.py`（`SIGNALLENS_DATA_DIR` 指向临时目录，fake API Key
仅用于通过应用的 key 检查，缓存全命中所以永远不会发出请求）；
③ 驱动浏览器跑完 A~F 六个阶段；④ 打印每个检查的 PASS/FAIL 与诊断，
退出码非 0 表示有 FAIL。

输出：

- 终端：每个检查一行 `PASS/FAIL  name | detail`（名称 ASCII，细节可中文）；
- JSON 报告：`%TEMP%\sl_acceptance_report.json`；
- 应用日志：`%TEMP%\sl_acceptance_streamlit.log`。

## 覆盖范围（85 项）

| 阶段 | 内容 |
|---|---|
| A | 400 条虚构微信聊天导入、身份表单（选择不触发滚动、映射生效、unknown=0） |
| B | 预览分页与滚动：顶部/底部翻页、连续翻页纹丝不动、内部滚动归零、**P0 复现 `inner_scroll_then_next_first_row`**（连续翻页后内部滚到底再翻下一页）、nonce 递增、表格重挂载 |
| C | 开始分析：缓存全命中、2.5s 级完成、**0 外部请求**、分析失败 0 |
| D | 结果「全部消息」分页、锚点渲染、普通 rerun（显示媒体事件）不跳滚动不触发锚点 |
| E | 好友档案：新建、混合信号标签、无重复 key、证据编辑+删除+**落库校验**、跨好友隔离、串档确认（未确认提交被拒）、保存、删除 |
| G | **长期行为观察（Phase 2A + 2A.1 终版）**：候选分页、**历史来源候选标注"无聊天正文"+编号归属原 run 提示**、历史候选确认**落库校验（指纹来自原 run）**、确认/排除**落库校验**、事件报告渲染与下载、页面无"尊重分/喜欢概率"输出、手动添加事件落库（**虚构 PII 片段落库已脱敏**）、**跨好友切换不继承备注**、事件编辑与删除 |
| F | ②阶段历史档案：查找、自动查看、取消查看、重新选择身份后历史视图与好友绑定失效（真 button 选择器） |
| Z | 全局：0 外部请求、无 page error、无 console error |

## 踩坑记录（重写脚本时全部绕开）

1. **`label:has-text("重新选择身份")` 超时**——它是 `<button>`。一律
   `get_by_role("button", name=...)`；segmented control 的选项是
   `role="radio"`。
2. **selectbox 必须点两次输入框**：Streamlit 1.64 的 react-aria ComboBox
   第一次点击只聚焦，第二次才展开 `stSelectboxVirtualDropdown`；chevron
   按钮和合成键盘事件都不可靠。
3. **sticky header 拦截点击**：Playwright 的 `scroll_into_view_if_needed`
   会把元素送到视口边缘，被 `stHeader`/`stToolbar` 挡住。所有点击先
   `scrollIntoView({block:'center'})`（见 `ui_click`/`ui_fill`）。
4. **checkbox 点 label 文本**：输入框被 `<label>` 覆盖，直接点输入框会被
   判「label 拦截指针事件」；且要限定 `locator("label", has_text=...)`，
   `get_by_text` 会匹配到包含全部选项的共享容器，`nth(1)` 会错点。
5. **等状态不睡死**：翻页后等页码文本 / `data-scroll-nonce` /
   `.dvn-scroller` 挂载；点击前启动 rAF 采样捕获锚点定位全过程。
6. **子进程输出必须写文件，不能用 PIPE**：Windows 管道缓冲区很小，应用写满
   后整个进程阻塞在 `write()` 上，曾伪装成「分析卡死 240s」。
7. **表单内编辑提交后才进 session_state**：st.form 里的 textarea 改动在
   提交前不到 Python，跨档案切换会丢——「编辑→保存→落库」的校验直接读
   SQLite（`read_friend_evidence`）。
8. **G 阶段（Phase 2A）新增坑**：
   - **无 key 的 `st.expander` 会在任意 rerun 后收起**——手动添加表单里改
     「行为方向」selectbox 就会触发 rerun，表单当场收起、用户白填。
     这是产品 bug（与②阶段 `history_panel` 同类），修复方式是显式
     `key="behavior_manual_panel"`；回归见
     `behavior_manual_panel_stays_open`。
   - **number_input 的 `fill` 触发异步 rerun**，落在 combobox 两连击之间
     会让下拉永远打不开——手动表单的起始/结束编号用默认值（1/2），
     不要 fill。
   - **虚拟下拉行在长面板深处"元素不稳定"**：真实 click 反复超时
     （虚拟列表重定位）。`_pick_select_in` 优先真实 click，4s 超时后退回
     `dispatch_event("click")`（仍是 react-aria 正式选项事件）。
   - **历史来源的候选追加在候选列表末尾**：检查"无聊天正文"标注前要先
     逐批翻到最后一页。
   - **确认/排除表单的编号是 1-based，传给事件层前必须 -1**：曾出现窗口
     整体偏移一位 → 事件身份与候选不匹配 → 确认后候选不消失、重复计数。
     AppTest 用 `msg_window == [0, 1]` + 候选数减一钉死这个回归。
   - **跨好友别名要区分**：G 阶段建第三份档案（跨好友隔离检查）要用**另一个
     称呼**（安安），否则 F 阶段按「予安」查找会命中多份档案，「唯一匹配
     自动查看」 regression 失去唯一性。
   - **历史候选的信号词不能用面板总说明**：「历史分析的既有指标辅助筛选」
     同时出现在 BEHAVIOR_PANEL_NOTE 里，用它当翻页停止信号会第一页假
     阳性；要用只属于候选的「（run …」后缀。
   - **候选 expander 的定位片段不能用保存按钮名**：两阶段门控下
     「确认这条事件」在预览前不渲染，`_open_expander` 要用候选独有的
     「第一步：核对并修正」。
   - **textarea 击键不触发 rerun（失焦才提交）**：测「输入→预览」必须
     用 `press_sequentially`（真实击键）+ 点击「查看最终预览」（点击前的
     失焦恰好提交值）；`fill()` 只改前端值不触发独立 rerun。
   - **两阶段下保存按钮按控件 key 作用域定位**：候选 / 手动 / 事件编辑
     都渲染「查看最终预览」，按钮顺序随页面内容变化，AppTest 用
     `behavior_preview_btn_{scope}` 精确点，浏览器用 expander 作用域。

## 已知边界（有意接受，写在这里避免误判）

- 连续翻页时偶发 42px / 2 帧的瞬时回弹（净位移为 0，glide-data-grid 换数据
  瞬间的渲染 churn）。「不跳滚动」判据因此是**净位移为 0 且末段稳定**
  （`_no_jump`），一帧不动的严格判据会偶发误报。
- 脚本以 dev 模式（`streamlit run`）验收，不依赖 PyInstaller 构建；
  frozen 构建的冒烟由 `scripts/frozen_smoke.py` 负责。

## 安全属性

- 聊天 / 昵称 / 证据全虚构（`fictional_data.py`），无真实个人信息；
- 预置缓存只写运行时会读的那一个 `cache.sqlite3`；
- `TYPESAFE_API_KEY` 用的是本地假值；网络层断言任何非 localhost 请求即 FAIL；
- 好友档案数据库写在临时数据目录，跑完即弃。
