# 标注指南（ANNOTATION GUIDE）

适用层：**独立 observer**（发送者 / 接收者填写各自专属层，不使用本指南的
observer 维度）。开始正式标注前，请先在 **training examples** 上熟悉
rubric；training cases 不得进入 blind evaluation。

通用原则：
- 只按**文字表现**判断，不猜测当事人心里怎么想；
- **observable behaviour ≠ sender's actual hidden intention**；
- **warmth ≠ romance**（温暖不等于浪漫）；
- **romantic boundary ≠ relationship distancing**（浪漫边界不等于关系疏离）；
- **topic refusal ≠ conversation withdrawal**（拒绝话题不等于结束交流）；
- 拿不准就用 `unsure`，信息不够就用 `insufficient`，不想答就用 `declined`——
  这些都是有效标注。

## emotion（单选，11 项）

calm / happy / teasing / curious / confused / surprised / caring / annoyed /
awkward / sad / other。按文字最直接表现的情绪选 `other` 之外的项；无语气
线索且多种解读都成立时，选覆盖面最广的项或 `unsure`。

## intent（单选，13 项）

ask_information / confirm_understanding / explain / share_opinion /
continue_topic / show_care / tease / invite / share_personal / end_topic /
perfunctory / distance / other。
注意 `distance` 覆盖三种范围（话题拒绝 / 浪漫边界 / 关系疏离），按文字最强
证据选择即可，不必判断范围——范围由 `distancing_binary` 单独标。

## warmth / engagement / special_attention / relational_ease（0~4）

| 分 | warmth | engagement | special_attention | relational_ease |
|---|---|---|---|---|
| 0 | 明显冷淡、疏离或拒绝 | 明确不想继续交流（或反复无实质敷衍） | 没有特别关注，甚至略显疏离 | 明显陌生、拘谨、纯事务性 |
| 1 | 基本中性或纯事务性 | 最低限度、敷衍回应 | 仅普通礼貌 | 较正式或普通礼貌 |
| 2 | 友好、自然 | 普通正常参与 | 友好的个人关注 | 自然、正常、舒适的熟人互动 |
| 3 | 明显温暖并有个人层面投入 | 主动帮助对话继续（追问/开新话题/具体安排） | 明显超出普通社交的特别关注 | 明显熟悉、轻松、有默契 |
| 4 | 非常亲近、亲密或异常温暖 | 高度主动、明显投入 | 非常明显且高度个人化 | 高度熟悉、非常自然 |

- warmth  anchor：日常礼貌关心（“记得带伞”）约 1~2；针对困境的具体关心
  （“这种滋味肯定很难受”）约 3；持续亲昵约 4。
- engagement anchor：拒绝当前话题但追问另一话题 → ≥2；礼貌收尾但给出
  具体后续安排 → ≥2；单次短回复（“嗯”）无更多证据 → 1~2，不要因一次短
  回复判 0；明确拒绝继续交流 → 0。
- 提出后续计划本身不自动 4 分；只有实际体现主动贡献/具体安排才支持高分。

## romantic_binary / distancing_binary（布尔）

- romantic_binary=true：文字中出现超出普通友好的具体暧昧/调情/浪漫兴趣
  表达。礼貌、关心、熟人口嗨都不算。
- distancing_binary=true：文字明确减少或结束持续互动、回避关系本身、
  明确拒绝继续联系（“别再联系我了”“我们别互相打扰了”）。
  礼貌收尾、计划稍后再聊、单次短回复、临时忙碌、话题拒绝、浪漫边界
  （“只当朋友”）都是 **false**。

## 何时用 unsure / insufficient

- `unsure`： rubric 看完了还是无法二选一（如无线索的“哈哈”）。
- `insufficient`：文字信息不足以判断该维度（如只有媒体占位符）。
- `declined`：不愿回答该维度。
三类状态都不计为错误，会计入弃答覆盖率。

## 回忆延迟（仅 sender role）

事后回想“当时意图”请选择 recall_delay_bucket：
same_day / 1_7_days / 8_30_days / 31_plus / unknown。回忆可能有偏差，
我们会按桶报告并声明这不是无误差的心理真值。
