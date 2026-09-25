"""标点 / Emoji 昵称的三行块解析回归（真实缺陷修复）。

真实缺陷（用户报告）：微信昵称「无聊！！！」含全角感叹号，被
``parser._NAME_BAD_CHARS`` 挡住 ``looks_like_sender_name()``，合法三行
微信格式报「开头第 1 行无法识别为消息」。

修复方式：不删过滤规则，而是在**结构位置**（昵称行 + 下一行完整合法
时间戳）使用宽松校验 ``_looks_like_struct_sender``——额外放行 ！？!?，
其余防护（URL / 冒号 / 括号 / 句子级标点 / 时间 / 超长 / 无字母）全部保留。

本文件覆盖任务书要求的全部场景：

- 首条消息昵称为「无聊！！！」；
- 双方带标点昵称；
- 相同昵称重复出现；
- 正文带标点和时间（不得变成新昵称）；
- 自动参与者检测；
- 昵称与消息原文逐字节保留（不偷偷删标点）；
- 反向防护：感叹句 / 网址 / 时间 / 代码 / 媒体占位符 / 句子级标点。

全部为**本地纯解析**，绝不调用真实 Jev API；昵称与正文均为虚构样本。
"""

import pytest
from parser import (
    _looks_like_struct_sender,
    detect_format,
    detect_participants,
    looks_like_sender_name,
    parse_chat,
)
from parser import FORMAT_WECHAT_BLOCKS

# 用户报告的真实输入（昵称虚构化处理保留原结构：第一行就是带标点昵称）
USER_REPORT_CHAT = """无聊！！！
2026年08月26日 13:44
四年了

陌寒.
2026年08月26日 13:45
还得起码再陪一年呢"""

ME = "无聊！！！"
THEM = "陌寒."


# ---------------------------------------------------------------------------
# 1) 用户真实缺陷：首条消息昵称含全角感叹号
# ---------------------------------------------------------------------------


def test_user_reported_chat_parses():
    """「无聊！！！」作为首条消息昵称：不再报「开头第 1 行无法识别为消息」。"""
    msgs = parse_chat(USER_REPORT_CHAT, my_name=ME, them_name=THEM)
    assert len(msgs) == 2
    assert msgs[0]["raw_speaker"] == "无聊！！！"
    assert msgs[0]["speaker"] == "me"
    assert msgs[0]["time"] == "2026-08-26 13:44"
    assert msgs[0]["text"] == "四年了"
    assert msgs[1]["raw_speaker"] == "陌寒."
    assert msgs[1]["speaker"] == "them"
    assert msgs[1]["text"] == "还得起码再陪一年呢"
    assert detect_format(USER_REPORT_CHAT) == FORMAT_WECHAT_BLOCKS


def test_punctuation_nickname_preserved_byte_for_byte():
    """昵称里的标点必须逐字节保留，绝不偷偷删除。"""
    msgs = parse_chat(USER_REPORT_CHAT, my_name=ME, them_name=THEM)
    raw = msgs[0]["raw_speaker"]
    assert raw == "无聊！！！"
    assert "！" in raw and raw.count("！") == 3
    assert raw != "无聊"                  # 没有被削掉标点
    assert msgs[0]["text"] == "四年了"     # 正文同样原样保留


# ---------------------------------------------------------------------------
# 2) 双方带标点昵称 / Emoji 昵称
# ---------------------------------------------------------------------------


def test_both_parties_with_punctuation_nicknames():
    chat = """怎么了？
2026年08月26日 13:44
没事，走吧

无聊！！！
2026年08月26日 13:45
好"""
    msgs = parse_chat(chat, my_name="怎么了？", them_name="无聊！！！")
    assert [m["speaker"] for m in msgs] == ["me", "them"]
    assert [m["raw_speaker"] for m in msgs] == ["怎么了？", "无聊！！！"]
    assert [m["text"] for m in msgs] == ["没事，走吧", "好"]


def test_emoji_nickname_supported():
    chat = "无聊😂\n2026年08月26日 13:44\n在吗\n\n陌寒.\n2026年08月26日 13:45\n在的"
    msgs = parse_chat(chat, my_name="无聊😂", them_name="陌寒.")
    assert detect_participants(msgs) == ["无聊😂", "陌寒."]
    assert msgs[0]["raw_speaker"] == "无聊😂"


def test_ascii_exclamation_and_question_nicknames():
    for name in ("Hey!!!", "What?", "a!b?", "嗯!!"):
        chat = f"{name}\n2026年08月26日 13:44\n你好\n\n陌寒.\n2026年08月26日 13:45\n在的"
        msgs = parse_chat(chat, my_name=name, them_name="陌寒.")
        assert msgs[0]["raw_speaker"] == name, name
        assert msgs[0]["speaker"] == "me", name


# ---------------------------------------------------------------------------
# 3) 相同昵称重复出现
# ---------------------------------------------------------------------------


def test_same_punctuation_nickname_repeats():
    chat = """无聊！！！
2026年08月26日 13:44
在吗

无聊！！！
2026年08月26日 13:46
还在吗

陌寒.
2026年08月26日 13:47
在的"""
    msgs = parse_chat(chat, my_name=ME, them_name=THEM)
    assert len(msgs) == 3
    assert [m["speaker"] for m in msgs] == ["me", "me", "them"]
    assert [m["text"] for m in msgs] == ["在吗", "还在吗", "在的"]
    # 参与者去重且不含任何正文片段
    assert detect_participants(msgs) == ["无聊！！！", "陌寒."]


# ---------------------------------------------------------------------------
# 4) 正文带标点和时间：不得变成新昵称
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "太好了！我们16:30出发？",
    "气死我了！！！",
    "为什么？？",
    "真的假的？哈哈哈哈！",
    "第一行\n16:30\n还是第二行",
    "版本1:30应该能好",
    "今天真开心，我们走吧",
    "完了。",
    "走吧；别等了",
    "等……",
])
def test_punctuated_bodies_never_become_participants(body):
    chat = (f"{ME}\n2026年08月26日 13:44\n{body}\n\n"
            f"{THEM}\n2026年08月26日 13:45\n好的")
    msgs = parse_chat(chat, my_name=ME, them_name=THEM)
    assert len(msgs) == 2, body
    assert detect_participants(msgs) == [ME, THEM], body
    assert msgs[0]["text"] == body, body
    assert msgs[0]["raw_speaker"] == ME, body


def test_multiline_punctuated_body_stays_one_message():
    body = "太好了！\n我们16:30出发？\n别迟到！"
    chat = f"{ME}\n2026年08月26日 13:44\n{body}\n\n{THEM}\n2026年08月26日 13:45\n好的"
    msgs = parse_chat(chat, my_name=ME, them_name=THEM)
    assert len(msgs) == 2
    assert msgs[0]["text"] == body
    assert msgs[1]["text"] == "好的"


# ---------------------------------------------------------------------------
# 5) 反向防护：URL / 代码 / 媒体 / 时间 不得被当成昵称
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "https://www.example.com/api?a=1",
    "if (x) { return 1; }",
    'print("hi!")',
    "[图片]",
    "[语音] 7\"",
    "16:30",
    "2026年08月26日 13:44",
])
def test_structural_bodies_that_look_dangerous_stay_body(body):
    """这些内容即使出现在正文里也绝不成为 participant。"""
    chat = f"{ME}\n2026年08月26日 13:44\n{body}\n\n{THEM}\n2026年08月26日 13:45\n嗯"
    msgs = parse_chat(chat, my_name=ME, them_name=THEM)
    assert detect_participants(msgs) == [ME, THEM], body
    assert msgs[0]["raw_speaker"] == ME, body
    assert msgs[1]["raw_speaker"] == THEM, body


def test_body_line_before_timestamp_not_a_nickname_when_sentence_like():
    """句子级标点（逗号 / 句号）行即使在时间戳行之前也不是昵称候选。"""
    # 结构上「今天真开心，我们走吧」紧邻下一行时间戳：逗号是句子特征，
    # 不得被当成昵称；该行作为正文归入上一条消息。
    chat = ("陌寒.\n2026年08月26日 13:45\n今天真开心，我们走吧\n"
            "无聊！！！\n2026年08月26日 13:46\n嗯")
    msgs = parse_chat(chat, my_name=ME, them_name=THEM)
    assert detect_participants(msgs) == [THEM, ME]
    assert msgs[0]["text"] == "今天真开心，我们走吧"


# ---------------------------------------------------------------------------
# 6) 自动参与者检测
# ---------------------------------------------------------------------------


def test_detect_participants_only_from_real_header_positions():
    msgs = parse_chat(USER_REPORT_CHAT)
    assert detect_participants(msgs) == ["无聊！！！", "陌寒."]
    # 正文片段绝不出现在参与者里
    for token in ("四年了", "还得起码再陪一年呢", "！"):
        assert token not in detect_participants(msgs)


def test_unmapped_punctuation_nicknames_are_unknown_not_guessed():
    """没有手工映射时不猜测发言人：全部 unknown。"""
    msgs = parse_chat(USER_REPORT_CHAT)
    assert [m["speaker"] for m in msgs] == ["unknown", "unknown"]
    # 只映射一侧时，另一侧归入对面（既有契约）
    msgs_me = parse_chat(USER_REPORT_CHAT, my_name=ME)
    assert [m["speaker"] for m in msgs_me] == ["me", "them"]
    msgs_them = parse_chat(USER_REPORT_CHAT, them_name=THEM)
    # 只映射 TA 一侧：陌寒. → them，其余具名发言人 无聊！！！ → me
    assert [m["speaker"] for m in msgs_them] == ["me", "them"]


# ---------------------------------------------------------------------------
# 7) 严格校验 / 结构校验的分工
# ---------------------------------------------------------------------------


def test_struct_check_allows_exclamations_strict_does_not():
    for name in ("无聊！！！", "怎么了？", "Hey!!!", "气死我了？"):
        assert _looks_like_struct_sender(name) is True, name
        assert looks_like_sender_name(name) is False, name


@pytest.mark.parametrize("line", [
    "今天真开心，我们走吧",       # 逗号：句子特征
    "完了。",                    # 句号
    "走吧；别等了",              # 分号
    "等……",                     # 省略号
    "甲、乙",                    # 顿号
    "https://example.com",      # URL
    "16:30",                    # 时间
    "2026年08月26日 13:44",     # 完整时间
    "[图片]",                    # 媒体占位符
    "{}",                        # 代码花括号
    "<b>",                       # 尖括号
    "",                          # 空
    "x" * 31,                    # 超长
    "12345",                     # 无字母
])
def test_struct_check_still_rejects_sentence_and_body_patterns(line):
    assert _looks_like_struct_sender(line) is False, line


def test_legacy_colon_header_unchanged_for_punctuation_names():
    """冒号格式不放行标点昵称（该路径没有时间戳结构确认）。

    边界（与修复前一致，不在本轮范围）：首行就是「标点昵称: 内容」时
    仍无法识别，抛 ParseError 提示用户手工填昵称——本轮只修复**三行
    微信格式**，不改冒号路径的严格校验。
    """
    with pytest.raises(Exception):
        parse_chat("无聊！！！: 你好\n陌寒.: 在的")
    # 无标点昵称的冒号格式不受影响
    msgs = parse_chat("我: 你好\nTA: 在的")
    assert [m["speaker"] for m in msgs] == ["me", "them"]


def test_legacy_timestamp_only_format_still_not_misparsed():
    """时间戳独占一行的旧格式：正文行（含标点）不得变成昵称。"""
    chat = "\n".join([
        "2026年08月26日 13:44", "太好了！",
        "2026年08月26日 13:45", "在啊",
        "2026年08月26日 13:46", "气死我了？！",
    ])
    msgs = parse_chat(chat)
    assert detect_participants(msgs) == []
    assert [m["text"] for m in msgs] == ["太好了！", "在啊", "气死我了？！"]


def test_wechat_chat_without_blank_lines_and_punct_nicknames():
    chat = ("无聊！！！\n2026年08月26日 13:44\n在吗\n"
            "陌寒.\n2026年08月26日 13:45\n在的")
    msgs = parse_chat(chat, my_name=ME, them_name=THEM)
    assert [m["raw_speaker"] for m in msgs] == [ME, THEM]
    assert [m["speaker"] for m in msgs] == ["me", "them"]


def test_single_message_punctuation_nickname():
    """只发一条的短聊天也支持（不要求昵称重复出现）。"""
    chat = f"{ME}\n2026年08月26日 13:44\n在吗\n\n{THEM}\n2026年08月26日 13:45\n在的"
    assert detect_format(chat) == FORMAT_WECHAT_BLOCKS
    msgs = parse_chat(chat)
    assert detect_participants(msgs) == [ME, THEM]
