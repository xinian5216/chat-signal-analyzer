"""parser 测试。"""

import pytest

from parser import ParseError, parse_chat, resolve_speaker

SAMPLE_COLON = """我: 你刚才怎么一直没回我
TA: 可能比较沉浸
我: 玩游戏吗
TA: 对哈哈"""

SAMPLE_TIME = """22:31 我
你干嘛呢

22:32 TA
刚洗完澡"""

SAMPLE_THREE_LINE = """小明
2026年09月08日 0:09
中午一起吃饭吗

小红
2026年09月08日 0:15
好呀去哪吃

小明
2026年09月08日 0:20
公司楼下新开的那家"""


def test_colon_format():
    msgs = parse_chat(SAMPLE_COLON)
    assert len(msgs) == 4
    assert [m["speaker"] for m in msgs] == ["me", "them", "me", "them"]
    assert msgs[1]["text"] == "可能比较沉浸"


def test_time_name_format():
    msgs = parse_chat(SAMPLE_TIME)
    assert len(msgs) == 2
    assert msgs[0] == {
        "speaker": "me",
        "text": "你干嘛呢",
        "time": "22:31",
        "raw_speaker": "我",
        "content_type": "text",
        "media_kinds": [],
    }
    assert msgs[1]["speaker"] == "them"
    assert msgs[1]["time"] == "22:32"
    assert msgs[1]["text"] == "刚洗完澡"


def test_fullwidth_colon_and_multiline():
    text = "小明：在吗\n我刚看到\n\n小红：在的"
    msgs = parse_chat(text, my_name="小明", them_name="小红")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "在吗\n我刚看到"
    assert msgs[0]["speaker"] == "me"
    assert msgs[1]["speaker"] == "them"


def test_custom_nicknames():
    text = "阿强: 晚上吃什么\n阿珍: 随你"
    msgs = parse_chat(text, my_name="阿强", them_name="阿珍")
    assert [m["speaker"] for m in msgs] == ["me", "them"]


def test_only_one_nickname_other_side_inferred():
    text = "阿强: 晚上吃什么\n阿珍: 随你"
    msgs = parse_chat(text, my_name="阿强")
    assert [m["speaker"] for m in msgs] == ["me", "them"]
    msgs2 = parse_chat(text, them_name="阿珍")
    assert [m["speaker"] for m in msgs2] == ["me", "them"]


def test_unknown_speaker_marked_unknown_not_raising():
    msgs = parse_chat("阿强: 晚上吃什么\n阿珍: 随你")
    assert [m["speaker"] for m in msgs] == ["unknown", "unknown"]
    assert msgs[0]["raw_speaker"] == "阿强"


def test_no_them_messages_is_not_an_error():
    # 只有我发言也能正常解析（由 UI 提示“没有 TA 的消息”）
    msgs = parse_chat("我: 自言自语\n我: 没人理我")
    assert [m["speaker"] for m in msgs] == ["me", "me"]


def test_empty_and_garbage_input():
    with pytest.raises(ParseError):
        parse_chat("")
    with pytest.raises(ParseError):
        parse_chat("   \n  \n ")


def test_resolve_speaker_defaults():
    assert resolve_speaker("我") == "me"
    assert resolve_speaker("TA") == "them"
    assert resolve_speaker("她") == "them"
    assert resolve_speaker("张三") is None
    assert resolve_speaker("张三", my_name="张三") == "me"
    assert resolve_speaker("李四", my_name="张三") == "them"


# ---------------------------------------------------------------------------
# 三行真实格式：昵称 / 日期时间 / 内容
# ---------------------------------------------------------------------------


def test_three_line_format():
    msgs = parse_chat(SAMPLE_THREE_LINE, my_name="小明", them_name="小红")
    assert len(msgs) == 3
    assert [m["speaker"] for m in msgs] == ["me", "them", "me"]
    assert msgs[0]["raw_speaker"] == "小明"
    assert msgs[0]["time"] == "2026-09-08 00:09"
    assert msgs[0]["text"] == "中午一起吃饭吗"
    assert msgs[1]["raw_speaker"] == "小红"
    assert msgs[1]["time"] == "2026-09-08 00:15"
    assert msgs[2]["time"] == "2026-09-08 00:20"


def test_three_line_first_message_alone():
    msgs = parse_chat("2026年09月08日 0:09\n别上班偷摸打游戏了")
    assert len(msgs) == 1
    assert msgs[0]["speaker"] == "unknown"
    assert msgs[0]["text"] == "别上班偷摸打游戏了"
    assert msgs[0]["time"] == "2026-09-08 00:09"


def test_three_line_with_both_nicknames():
    text = (
        "阿珍\n2026年09月08日 0:09\n在忙吗\n\n"
        "阿珍\n2026年09月08日 0:10\n刚下班\n\n"
        "阿强\n2026年09月08日 0:11\n辛苦啦"
    )
    msgs = parse_chat(text, my_name="阿强", them_name="阿珍")
    assert [m["speaker"] for m in msgs] == ["them", "them", "me"]


def test_datetime_variants_normalize():
    variants = [
        ("2026年09月08日 0:09", "2026-09-08 00:09"),
        ("2026年9月8日 0:09", "2026-09-08 00:09"),
        ("2026-09-08 00:09", "2026-09-08 00:09"),
        ("2026/9/8 8:09", "2026-09-08 08:09"),
        ("2026-09-08 00:09:33", "2026-09-08 00:09"),
    ]
    for raw, expected in variants:
        msgs = parse_chat(f"TA\n{raw}\n内容")
        assert msgs[0]["time"] == expected, raw


def test_time_only_line_variants():
    msgs = parse_chat("TA\n22:31\n内容A")
    assert msgs[0]["time"] == "22:31"
    msgs = parse_chat("TA\n22:31\n内容A\n我\n22:32\n内容B")
    assert [m["time"] for m in msgs] == ["22:31", "22:32"]
    assert [m["speaker"] for m in msgs] == ["them", "me"]


def test_multiline_message_with_blank_lines_between():
    text = (
        "TA\n2026年09月08日 0:09\n第一行\n第二行\n\n"
        "我\n2026年09月08日 0:10\n收到"
    )
    msgs = parse_chat(text)
    assert len(msgs) == 2
    assert msgs[0]["text"] == "第一行\n第二行"
    assert msgs[0]["speaker"] == "them"
    assert msgs[1]["speaker"] == "me"


def test_continuous_same_speaker():
    text = "我: 在吗\n我: 在不在\nTA: 在的\nTA: 怎么了"
    msgs = parse_chat(text)
    assert [m["speaker"] for m in msgs] == ["me", "me", "them", "them"]
    assert [m["text"] for m in msgs] == ["在吗", "在不在", "在的", "怎么了"]


def test_three_line_arbitrary_nicknames():
    # 任意中文 / 英文 / 数字昵称，不硬编码
    text = (
        "Kevin2024\n2026年09月08日 0:09\n早\n\n"
        "小苹果🍎\n2026年09月08日 0:10\n早啊\n\n"
        "A1_兔\n2026年09月08日 0:11\n忙吗"
    )
    msgs = parse_chat(text, my_name="Kevin2024", them_name="小苹果🍎")
    assert [m["speaker"] for m in msgs] == ["me", "them", "unknown"]
    assert msgs[2]["raw_speaker"] == "A1_兔"


def test_detect_participants_order_and_dedup():
    from parser import detect_participants

    msgs = parse_chat(SAMPLE_THREE_LINE)
    assert detect_participants(msgs) == ["小明", "小红"]
    # 去重 & 忽略空名
    assert detect_participants(msgs + [dict(msgs[0], raw_speaker=""), dict(msgs[1])]) == ["小明", "小红"]


def test_three_line_without_blank_lines():
    # 消息之间没有空行，任意昵称也按“时间行前的普通文本行 = 发送者候选”识别
    text = "小明\n2026年09月08日 0:09\nA\n小红\n2026年09月08日 0:10\nB"
    msgs = parse_chat(text, my_name="小明", them_name="小红")
    assert [m["text"] for m in msgs] == ["A", "B"]
    assert [m["speaker"] for m in msgs] == ["me", "them"]


def test_mixed_formats():
    """冒号格式 + 微信三行块混糊输入。

    模式隔离后：第一个微信块头之前仍然识别冒号格式；
    一旦微信块结构开始，BODY 内不再运行冒号解析（否则正文里的
    ``已知现状：`` 之类的标题会被抠成发言人）。
    """
    text = (
        "我: 中午吃啥\n"
        "TA: 随便\n"
        "\n"
        "小雨\n2026-09-08 12:00\n我带了饭\n"
        "\n"
        "阿强: 分我点"
    )
    msgs = parse_chat(text, my_name="阿强", them_name="小雨")
    assert [m["speaker"] for m in msgs] == ["me", "them", "them"]
    assert msgs[2]["time"] == "2026-09-08 12:00"
    # 第三条消息的正文完整保留（包括看起来像冒号格式的那一行）
    assert msgs[2]["text"] == "我带了饭\n阿强: 分我点"


def test_never_guess_speaker_from_content():
    # 没有任何昵称线索时，具名发言人是 unknown，而不是凭内容猜 me/them
    msgs = parse_chat("猫猫: 在吗\n狗狗: 在的")
    assert [m["speaker"] for m in msgs] == ["unknown", "unknown"]
    # 同样，缺昵称的消息永远是 unknown
    msgs = parse_chat("猫猫: 在吗\n2026年09月08日 0:09\n嗯嗯")
    assert [m["speaker"] for m in msgs] == ["unknown", "unknown"]
