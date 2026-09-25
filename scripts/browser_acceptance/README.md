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

## 覆盖范围（57 项）

| 阶段 | 内容 |
|---|---|
| A | 400 条虚构微信聊天导入、身份表单（选择不触发滚动、映射生效、unknown=0） |
| B | 预览分页与滚动：顶部/底部翻页、连续翻页纹丝不动、内部滚动归零、**P0 复现 `inner_scroll_then_next_first_row`**（连续翻页后内部滚到底再翻下一页）、nonce 递增、表格重挂载 |
| C | 开始分析：缓存全命中、2.5s 级完成、**0 外部请求**、分析失败 0 |
| D | 结果「全部消息」分页、锚点渲染、普通 rerun（显示媒体事件）不跳滚动不触发锚点 |
| E | 好友档案：新建、混合信号标签、无重复 key、证据编辑+删除+**落库校验**、跨好友隔离、串档确认（未确认提交被拒）、保存、删除 |
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
