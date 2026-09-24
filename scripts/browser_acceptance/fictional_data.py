# -*- coding: utf-8 -*-
"""浏览器回归用的**全虚构**微信聊天数据。

硬约束：

- 所有昵称、消息正文均为虚构，不含任何真实人物、地点、机构、号码；
- 正文不含任何隐私模式（手机号 / 邮箱 / 身份证 / URL）——``mask_messages``
  对其是恒等变换，seed 缓存与真实运行的 state 因此完全一致；
- 纯文本消息，不含媒体占位符（媒体场景由单元测试覆盖，浏览器验收
  聚焦分页 / 滚动 / 档案交互）；
- 确定性生成（固定 seed）：同一台机器、任何一天重跑，聊天文本逐字节一致，
  seed 的缓存 key 因此稳定命中。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

# 两位虚构参与者（禁止使用任何真实昵称）
ME = "林小满"
TA = "周予安"

# 第二份测试用的别名（第二位好友档案 = 同一位 TA 的另一个称呼，全虚构）
TA_ALIAS_2 = "予安"

# 400 条：与历史验收规模一致（预览 40 条/页 → 10 页）
CHAT_MESSAGE_COUNT = 400

# 含这条正文的 TA 消息会被 seed 成「关系信息量最高 + 明确疏离概率」，
# 从而同时入选支持性证据与相反证据 → 触发混合信号标签（P0 回归点）。
MIXED_TRIGGER_TEXT = "我们以后还是少聊点吧"

_BODY_POOL: tuple[str, ...] = (
    "早上好，昨晚睡得好吗",
    "文档我已经改好了，你再看一眼",
    "这块逻辑还差一个边界情况要处理",
    "周末有空吗，想约你去看那个新展",
    "刚从健身房回来，累瘫了",
    "明天下午三点记得交周报",
    "好，那我按这个方案先做一版",
    "你推荐的那本书我看了一半了",
    "下雨了，记得带伞",
    "晚上一起吃饭吗，我订位",
    "刚才在开会，没看手机",
    "这个 bug 我查了两个小时了",
    "谢谢你帮我问那件事",
    "今晚月亮特别圆，拍了张照",
    "我周末可能要加班，先看看进度",
    "行，那就先这样定了",
    "你平时周末都干什么",
    "这家店的咖啡真的一般",
    "方案我批了，注意控制成本",
    "明天见，别迟到",
    "晚安，好梦",
    "早，今天天气不错",
    "我先把框架搭起来，细节后面补",
    "你说的那个工具叫什么来着",
    "已阅，没问题",
    "哈哈，你也太逗了",
    "刚散会，累死了",
    "我下周要出差三天",
    "帮你带了一杯美式，老样子",
    "今天是截止日，别忘了提交",
    "我看了一下，数据没问题",
    "周末的票我买好了",
    "有点困，先眯一会儿",
    "OK，收到",
    "你昨天推荐的那部电影我看完了",
    "见面聊吧，打字太慢",
    "今天心情不错，项目上线了",
    "这个颜色不太好看，换一个",
    "我到了，你在哪",
    "地铁今天人好多，站了两站地",
    "晚饭随便对付了一口，不太好吃",
    "刚开完评审，结论是再改一版",
    "阳台的多肉发芽了，拍给你看",
    "记得关窗，晚上要降温",
    "我把链接发你，收藏好",
    "这首歌唱得还不错，推荐给你",
    "今天走了八千步，破纪录了",
)


def _format_stamp(moment: datetime) -> str:
    return f"{moment.year}年{moment.month:02d}月{moment.day:02d}日 " \
           f"{moment.hour:02d}:{moment.minute:02d}"


def build_chat(count: int = CHAT_MESSAGE_COUNT,
               seed: int = 20260924) -> str:
    """生成 ``count`` 条虚构微信三行块聊天文本（确定性）。

    - 时间戳严格升序且都是合法日历时刻（timeline 排序是恒等操作）；
    - 说话人带连续段（同一个人的连续消息），turn 结构接近真实聊天；
    - TA 消息里固定出现一次 :data:`MIXED_TRIGGER_TEXT`（混合信号用例）。
    """
    rng = random.Random(seed)
    start = datetime(2026, 8, 3, 9, 12)
    clock = start
    speaker = TA
    placed_mixed = False
    lines: list[str] = []
    for i in range(count):
        # 55% 概率延续上一个说话人 → 自然的消息段结构
        if rng.random() > 0.55:
            speaker = ME if speaker == TA else TA
        clock += timedelta(minutes=rng.randint(3, 16))
        if speaker == TA and not placed_mixed and i >= count // 2:
            body = MIXED_TRIGGER_TEXT
            placed_mixed = True
        else:
            body = _BODY_POOL[i % len(_BODY_POOL)]
            if body == MIXED_TRIGGER_TEXT:      # 兜底：别让池子重复触发
                body = _BODY_POOL[(i + 7) % len(_BODY_POOL)]
        lines.append(f"{speaker}\n{_format_stamp(clock)}\n{body}")
    assert placed_mixed, "混合信号用例必须出现一次"
    return "\n\n".join(lines) + "\n"


if __name__ == "__main__":   # 手动抽查生成结果
    text = build_chat()
    print(text[:600])
    print("...")
    print(f"total_lines={len(text.splitlines())}")
