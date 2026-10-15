# Jev 1.14 接入与验证

## 发布边界

默认继续固定 jev-1.13.0。1.14 是显式可选项。没有真实中文对照结果，
不宣称新版更准确，不自动把 jev-latest 的变化带入生产结果。
图片、字面历史检索、事件语义辅助均为默认关闭的实验功能。
Box 和 262k 长上下文没有接入，因为当前场景优先保证证据来源与短上下文质量。

## 缓存与不可变历史

- 完整九问（含文字说明、选项定义和选项顺序）自动形成指纹。
- 明确模型版本、cache-v2 和上下文策略参与缓存 key；移动别名绕过持久缓存。
- 缺少实际模型或与请求版本不符的结果不会写入缓存；明确版本不符按失败处理。
- 每批统计全部实际模型及分析条件，不能只取第一条代表整批。
- 长期比较同时检查实际模型、schema 和已记录的分析条件。
- 旧快照在内存中从已有逐条结果重建版本统计；不改写旧库、不删除旧缓存。
- 九问语义与原聚合公式未变，chat-signal-v3.3 保留；可选策略有独立版本。

## 中文模型对照

新增 evaluation/cases_jev114_zh.json（20 个虚构案例），覆盖短句歧义、反话、
朋友关心、浪漫边界、关系后撤、话题拒绝、忙碌后的安排、明确浪漫正例、
中英混用、方言、引用、否定和聊天正文指令干扰。

案例预期在真实调用前冻结；不得按输出回调阈值或修改标签。
主比较还应分别运行既有 cases.json、cases_distancing_v3.1.json、
cases_contrast_v3.2.json、cases_relationship_v0.4.json。不同案例集不得混比。

先检查计划（零 API）：

~~~powershell
.venv\Scripts\python -X utf8 scripts/evaluate.py --plan --cases evaluation/cases_jev114_zh.json
~~~

按照仓库要求，以下两条只能由人工显式执行（各 20 个 target、无缓存，共 40 次请求）：

~~~powershell
.venv\Scripts\python -X utf8 scripts/evaluate.py --real --yes-run-live-api --model jev-1.13.0 --cases evaluation/cases_jev114_zh.json --raw-report evaluation/reports/zh113-raw.json --report evaluation/reports/zh113.json
.venv\Scripts\python -X utf8 scripts/evaluate.py --real --yes-run-live-api --model jev-1.14.0 --cases evaluation/cases_jev114_zh.json --raw-report evaluation/reports/zh114-raw.json --report evaluation/reports/zh114.json
.venv\Scripts\python -X utf8 scripts/evaluate.py --compare evaluation/reports/zh113.json evaluation/reports/zh114.json
~~~

除模型外保持相同问题、上下文、案例、指标。比较器拒绝混合/未知实际模型、
不一致的问题或上下文配置。缺失版本来源的旧 raw 不会被贴上当前模型标签。
应分别报告误报、漏报、约束失败、API 失败与具体案例，不把合成 fixtures
通过率当成模型科学准确率。新案例尚无真实结果，也没有伪造“新版已通过”的 fixture。
Choice 顺序扰动应作为后续独立实验，不能与模型升级同时改变后宣称模型收益。

## 图片实验验收

上传→保守绑定→本地裁剪/遮挡→生成最终预览→逐图确认→开始分析。
绑定、图片像素或聊天顺序变化会使旧确认失效。未知/未确认图片不上传。
仅批准后的静态 PNG/JPEG 内容经官方 Image 接口发送，包含必要实验 header。
本地允许 WebP/BMP 等输入，但先解码并规范化；动画拒绝，长截图要求分段。
视觉结果只是可观察用途/语气及可辨性，始终需人工核对，不自动计入关系画像。
图片失败不丢文字结果，切页和导出不调用 API；导出不包含图片内容。

## 事件语义辅助

仅对已有 review_required 的 boundary_response / invitation_progression /
personal_recall_candidate 辅助判断，最多 8 次。只读取事件 end_index 及之前的
原文；闭集回答不能自动确认事件、追加档案或修改 salience。超额候选仍保留，
界面显示未发送数量。语义结果不是兑现证据，更不是心理或关系结果预测。

## 依赖复现

typesafe-sdk 固定 0.7.5，具备官方 Image/Box 接口；本功能未使用 Box。
Streamlit 固定仓库已实测的 1.64.0。新环境安装 1.66.0 时，未修改的原版
身份下拉框在 AppTest 中也无法保留选择；1.64.0 恢复原有流程。
图片处理依赖 Pillow，所有中间图片只驻留本机会话内存。

## 官方资料

- https://typesafe.ai/blog/jev-1-14
- https://docs.typesafe.ai/models
- https://docs.typesafe.ai/images
- https://docs.typesafe.ai/sdk/python/changelog
