"""parser 模式隔离回归测试（长正文假 participant）。

真实回归：微信消息正文里粘贴了一段较长技术文本（"已知现状：""问题表现："
"仓库：""Warning: connection failed""key: value"、代码、JSON、Markdown、
URL、多个空行），结果这些正文标题被 ``detect_participants`` 当成了人物。

根因：微信块模式下 legacy 的 ``speaker: content`` 分支仍然会在 BODY 内触发
（原先只有紧跟时间戳的第一行正文受到保护）。

现在改为**格式隔离**：``detect_format()`` 先判定整份输入的模式，一旦是
``wechat_blocks``，BODY 内不再运行任何 legacy 逐行推断——只有在
"合法 sender candidate + 下一有效行完整 timestamp fullmatch"时才结束当前
消息。

昵称为虚构样本，不含任何真实私人聊天内容。
"""

import pytest

from parser import (
    FORMAT_LEGACY_COLON,
    FORMAT_TIME_NAME,
    FORMAT_UNKNOWN,
    FORMAT_WECHAT_BLOCKS,
    detect_format,
    detect_participants,
    parse_chat,
)

A = "用户A"
B = "用户B"

# 需求 3 的真实形态：一条几十行、含中文冒号标题 / JSX / Markdown / URL 的长正文
LONG_TECH_BODY = f"""{A}
2026年09月18日 19:24
已知现状：

MiniPingChart.tsx 中当前 Recharts Tooltip 类似：

<ChartTooltip
  cursor={{false}}
  content={{<CustomTooltip />}}
/>

问题表现：
- 图表左侧正常
- 靠近图表右边缘时异常

仓库：
src/components/ui/chart.tsx

Warning: connection failed
key: value
{{"a": 1, "b": [2, 3]}}
https://www.example.com/api?a=1&b=2#frag

如有必要再检查：
其它文件

{B}
2026年09月18日 19:30
我看看"""

FAKE_PARTICIPANTS = [
    "已知现状", "问题表现", "仓库", "如有必要再检查", "Warning", "key",
    "src/components/ui/chart.tsx", "https", "www", "example", "com",
]


def _assert_only_real_participants(msgs):
    participants = detect_participants(msgs)
    assert participants == [A, B], participants
    for fake in FAKE_PARTICIPANTS:
        assert fake not in participants, fake
    return participants


# ---------------------------------------------------------------------------
# detect_format
# ---------------------------------------------------------------------------


def test_detect_format_wechat_blocks():
    assert detect_format(LONG_TECH_BODY) == FORMAT_WECHAT_BLOCKS
    short = f"{A}\n2026年09月18日 19:24\n你好\n\n{B}\n2026年09月18日 19:30\n嗯"
    assert detect_format(short) == FORMAT_WECHAT_BLOCKS


def test_detect_format_legacy_and_time_name():
    assert detect_format("我: 你好\nTA: 哈哈") == FORMAT_LEGACY_COLON
    assert detect_format("22:31 我\n你干嘛呢\n\n22:32 TA\n刚洗完澡") == FORMAT_TIME_NAME
    assert detect_format("这一行完全没有任何结构") == FORMAT_UNKNOWN
    assert detect_format("") == FORMAT_UNKNOWN
    # 时间戳独占一行的旧格式：不是微信块模式（不会把正文行当成昵称）
    legacy = "\n".join([
        "2026年08月21日 13:43", "别上班偷摸打游戏了",
        "2026年08月21日 13:44", "在啊",
        "2026年08月21日 13:45", "晚点说",
    ])
    assert detect_format(legacy) != FORMAT_WECHAT_BLOCKS


def test_detect_format_supports_single_message_per_participant():
    """每人只发一条的短聊天也必须识别为微信块模式（不要求昵称重复）。"""
    chat = f"{A}\n2026年08月21日 13:43\n在吗\n\n{B}\n2026年08月21日 13:44\n在的"
    assert detect_format(chat) == FORMAT_WECHAT_BLOCKS
    msgs = parse_chat(chat, my_name=A, them_name=B)
    assert [m["speaker"] for m in msgs] == ["me", "them"]
    assert detect_participants(msgs) == [A, B]


def test_detect_format_is_conservative():
    """一行普通文本 + 一行像时间，不能贸然进入微信模式。"""
    text = "我大概16:30过去\n2026年08月21日 13:44\n哈哈哈"
    assert detect_format(text) != FORMAT_WECHAT_BLOCKS


# ---------------------------------------------------------------------------
# 长多行正文
# ---------------------------------------------------------------------------


def test_long_technical_body_keeps_two_participants():
    msgs = parse_chat(LONG_TECH_BODY, my_name=A, them_name=B)
    assert len(msgs) == 2
    _assert_only_real_participants(msgs)
    assert msgs[0]["raw_speaker"] == A
    assert msgs[0]["speaker"] == "me"
    assert msgs[0]["time"] == "2026-09-18 19:24"
    assert msgs[1]["raw_speaker"] == B
    assert msgs[1]["speaker"] == "them"
    assert msgs[1]["text"] == "我看看"


def test_long_body_content_is_preserved_verbatim():
    msgs = parse_chat(LONG_TECH_BODY)
    text = msgs[0]["text"]
    for fragment in (
        "已知现状：", "MiniPingChart.tsx 中当前 Recharts Tooltip 类似：",
        "<ChartTooltip", "cursor={false}", "content={<CustomTooltip />}", "/>",
        "问题表现：", "- 图表左侧正常", "- 靠近图表右边缘时异常",
        "仓库：", "src/components/ui/chart.tsx",
        "Warning: connection failed", "key: value",
        '{"a": 1, "b": [2, 3]}',
        "https://www.example.com/api?a=1&b=2#frag",
        "如有必要再检查：", "其它文件",
    ):
        assert fragment in text, fragment
    # 空行只做 normalize（折叠），不影响 speaker 语义；非空行内容全部保留
    assert text.count("\n") >= 14


@pytest.mark.parametrize("body", [
    "已知现状：\n有 bug",
    "问题表现：\n图表闪了一下",
    "仓库：\nsrc/components/ui/chart.tsx",
    "Warning: connection failed",
    "key: value",
    "error: timeout",
    "src/components/ui/chart.tsx:12:5",
    '{"a": 1, "b": [2, 3]}',
    "- 图表左侧正常\n- 靠近图表右边缘时异常",
    "1. 第一项\n2. 第二项",
    "| a | b |\n| - | - |\n| 1 | 2 |",
    "见 https://www.example.com/api?a=1&b=2#frag 文档",
    "16:30 集合，别迟到",
    "16:30",
    "会议改到\n16:30\n吧",
    "Ta: 她说\nTA: 他说",
    "我: 我也是",
    "Note: 这不是发言人头",
    "TODO:\n- 修 bug\n- 补测试",
])
def test_body_lines_never_become_participants(body):
    chat = f"{A}\n2026年08月21日 13:43\n{body}\n\n{B}\n2026年08月21日 13:44\n好的"
    msgs = parse_chat(chat, my_name=A, them_name=B)
    assert len(msgs) == 2, body
    _assert_only_real_participants(msgs)
    assert msgs[0]["text"] == body, body


def test_body_with_many_blank_lines_stays_one_message():
    body = "第一段\n\n\n\n第二段\n\n\n\n\n第三段"
    chat = f"{A}\n2026年08月21日 13:43\n{body}\n\n{B}\n2026年08月21日 13:44\n好的"
    msgs = parse_chat(chat, my_name=A, them_name=B)
    assert len(msgs) == 2
    # 空行只是 normalize（折叠），不触发新 speaker、不丢内容
    assert msgs[0]["text"] == "第一段\n第二段\n第三段"
    _assert_only_real_participants(msgs)


def test_thirty_line_body():
    lines = [f"第 {i} 行：说明文字 {i}" for i in range(1, 31)]
    chat = (f"{A}\n2026年08月21日 13:43\n" + "\n".join(lines)
            + f"\n\n{B}\n2026年08月21日 13:44\n好的")
    msgs = parse_chat(chat, my_name=A, them_name=B)
    assert len(msgs) == 2
    assert msgs[0]["text"] == "\n".join(lines)
    _assert_only_real_participants(msgs)


def test_next_real_header_still_splits_after_long_body():
    """长正文之后遇到真实 sender + timestamp，必须正确切块。"""
    body = "\n".join(f"第 {i} 行：key: value {i}" for i in range(1, 11))
    chat = (f"{A}\n2026年08月21日 13:43\n{body}\n\n"
            f"{B}\n2026年08月21日 13:44\n我看看\n\n"
            f"{A}\n2026年08月21日 13:45\n懂了")
    msgs = parse_chat(chat, my_name=A, them_name=B)
    assert len(msgs) == 3
    assert [m["raw_speaker"] for m in msgs] == [A, B, A]
    assert [m["speaker"] for m in msgs] == ["me", "them", "me"]
    assert msgs[0]["text"] == body
    assert msgs[1]["text"] == "我看看"
    assert msgs[2]["text"] == "懂了"
    _assert_only_real_participants(msgs)


def test_legacy_colon_mode_still_works():
    msgs = parse_chat("我: 你好\nTA: 哈哈")
    assert [m["speaker"] for m in msgs] == ["me", "them"]
    assert [m["text"] for m in msgs] == ["你好", "哈哈"]
