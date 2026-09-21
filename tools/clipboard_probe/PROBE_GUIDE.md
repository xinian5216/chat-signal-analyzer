# 微信 Clipboard Probe 实测清单（v0.2.0 第一阶段）

> 用途：确认 Windows + 浏览器从微信 PC 复制聊天后，SignalLens **实际**能拿到哪些数据。
> 本清单的结论栏**由你手动填写**；不要依据任何模拟数据。
> Probe 默认不落盘、不上传；点“导出诊断信息”得到的也只有 MIME / 数量 / 尺寸 / 顺序。

## 测试前准备

1. 双击 `launcher\start.bat` 启动 SignalLens（或 `python -m streamlit run app.py`）。
2. 左侧栏 → **高级** → **剪贴板诊断**。
3. 在微信 PC 里选中目标聊天，`Ctrl+C`，然后在 Probe 区 `Ctrl+V`。
4. 每次粘贴后，把下表对应行填完再做下一个场景。

## 场景清单

| # | 场景 | text/plain | text/html | 图片数 | image MIME | item 顺序（照抄 Probe 输出） | 文本占位符顺序 | 能否自动绑定 |
|---|---|---|---|---|---|---|---|---|
| A | 只复制一条纯文字 |  |  | 0 | — |  | — | — |
| B | 只复制一条普通图片 |  |  |  |  |  |  |  |
| C | 文字 + 1 张普通图片 |  |  | 1 |  |  |  |  |
| D | 文字 + 多张普通图片（2~3 张） |  |  |  |  |  |  |  |
| E | 图片 + 文字 + 图片（交错） |  |  |  |  |  |  |  |
| F | 动画表情 |  |  |  |  |  |  |  |
| G | 视频 |  |  |  |  |  |  |  |
| H | 图片 + 动画表情 |  |  |  |  |  |  |  |
| I | 连续多条聊天（多发送者，含图片） |  |  |  |  |  |  |  |
| J | 单张图片右键 / 复制 |  |  |  |  |  |  |  |

## 每个场景要记录的关键问题

1. **text/plain**：微信是否把媒体写成 `[图片] 微信图片_xxxx.dat` 形式的占位符？
2. **text/html**：是否包含 `<img>`？若有，`src` 是本地路径还是内嵌数据？
3. **image/\***：浏览器是否拿到**真实图片二进制**？（这是本阶段能否绑定的关键）
4. **item 顺序**：`clipboardData.items` 的顺序是否与文本中占位符的出现顺序一致？
5. **Files**：是否出现 `kind === "file"` 的项？

## 结论怎么用

- 若场景 C 显示“能拿到真实 image/png” → 第一阶段自动绑定成立（1 占位符 + 1 图）。
- 若场景 D/E 的 item 顺序与文本占位符顺序**一致** → 可在 Probe 区勾选
  “我已确认剪贴板图片顺序与文本占位符顺序一致”，启用 N:N 顺序绑定。
- 若顺序**不一致或无法判断** → SignalLens 会拒绝自动绑定，改为手动匹配（符合预期，不是 bug）。
- 若动画表情 / 视频拿不到真实媒体 → 继续显示“内容未知”（符合预期）。

## 导出诊断信息（可选）

点 Probe 区的“导出诊断信息”只会得到下面这种结构，可安全贴到 issue：

```json
{
  "captured": true,
  "formats": ["text/plain", "text/html", "image/png"],
  "items": [
    {"index": 1, "kind": "string", "type": "text/plain"},
    {"index": 2, "kind": "string", "type": "text/html"},
    {"index": 3, "kind": "file",   "type": "image/png"}
  ],
  "images": [
    {"mime": "image/png", "size": 182340, "width": 1280, "height": 720, "sha256_prefix": "ab12cd34ef56"}
  ],
  "media_placeholders": 1,
  "image_placeholders": 1,
  "order_evidence": true
}
```

> 不包含：聊天文本、图片内容、文件路径、文件名、API Key。
