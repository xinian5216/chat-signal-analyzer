"""微信三行块解析稳定性测试（v0.2.1 回归）。

全部为**本地纯解析**，绝不调用真实 Jev API。

覆盖真实微信复制里出现过的误判：

- ``https://www.xinian5216.com`` 被 ``昵称: 内容`` 规则拆成
  ``speaker=https`` / ``content=//…``，从而变成假 participant；
- 正文里的 ``行 16:30到悦城？`` / ``行那就16:30吧`` 被当成时间行 /
  发送者候选，产生 ``行16`` / ``行那就16`` 假 participant；
- 多个空行改变 speaker 语义。

样本昵称一律使用虚构名称，不含任何真实私人聊天内容。
"""

from parser import (
    MEDIA_KIND_VOICE,
    MEDIA_MARKERS,
    classify_media,
    detect_participants,
    parse_chat,
    timestamp_of_line,
)

# 匿名合成回归样本（由真实问题提炼，昵称虚构）
ANON_WECHAT = """用户A
2026年08月14日 21:02
https://www.example.com

用户B
2026年08月14日 21:02
这是何物

用户A
2026年08月21日 13:43
行 16:30到商场？还是再早点晚点的 看你吧

用户B
2026年08月21日 13:44
可以可以

用户A
2026年08月21日 13:45
行那就16:30吧

用户B
2026年08月22日 16:28
[语音] 7\""""


# ---------------------------------------------------------------------------
# 时间戳必须整行 fullmatch
# ---------------------------------------------------------------------------

ALLOWED_TIMESTAMP_LINES = [
    ("2026年08月21日 13:43", "2026-08-21 13:43"),
    ("2026年08月21日  9:32", "2026-08-21 09:32"),
    ("2026-08-21 13:43", "2026-08-21 13:43"),
    ("2026/8/21 13:43", "2026-08-21 13:43"),
    ("13:43", "13:43"),
    ("2026年08月21日 13:43:07", "2026-08-21 13:43"),
]

FORBIDDEN_TIMESTAMP_LINES = [
    "行 16:30到大悦城？还是再早点晚点的 看你吧",
    "行那就16:30吧 那个点我估计不至于最人多吃饭的时候",
    "我大概16:30过去",
    "版本1:30应该能好",
    "16:30到大悦城",
    "16",
    "",
    "   ",
]


def test_timestamp_fullmatch_allowed():
    for raw, expected in ALLOWED_TIMESTAMP_LINES:
        assert timestamp_of_line(raw) == expected, raw


def test_timestamp_substring_is_never_a_timestamp_line():
    for raw in FORBIDDEN_TIMESTAMP_LINES:
        assert timestamp_of_line(raw) is None, raw


def test_body_time_lines_are_body_not_time():
    """“16:30”出现在正文里时必须保持正文，绝不开启新 speaker。"""
    for body in (
        "行 16:30到商场？",
        "行那就16:30吧",
        "我大概16:30过去",
        "版本1:30应该能好",
    ):
        msgs = parse_chat(f"用户A\n2026年08月21日 13:43\n{body}")
        assert len(msgs) == 1, body
        assert msgs[0]["text"] == body, body
        assert msgs[0]["raw_speaker"] == "用户A", body


# ---------------------------------------------------------------------------
# URL / 域名 / 英文 / 冒号 不成为 participant
# ---------------------------------------------------------------------------

URL_BODIES = [
    "https://www.xinian5216.com",
    "http://example.com/path?a=1",
    "https://www.example.com/index.html",
    "www.example.com",
    "ftp://files.example.com/pub",
]


def test_url_bodies_do_not_become_participants():
    for url in URL_BODIES:
        chat = f"用户A\n2026年08月21日 13:43\n{url}\n\n用户B\n2026年08月21日 13:44\n嗯嗯"
        msgs = parse_chat(chat, my_name="用户A", them_name="用户B")
        assert detect_participants(msgs) == ["用户A", "用户B"], url
        assert msgs[0]["text"] == url, url


def test_url_is_not_split_into_speaker_and_content():
    """“speaker: content”分支永不被 URL scheme 触发。"""
    for url in ("https://example.com", "http://example.com/x?y=1",
                "ftp://example.com", "file:///C:/tmp/a.txt"):
        msgs = parse_chat(f"我: {url}")
        assert len(msgs) == 1, url
        assert msgs[0]["raw_speaker"] == "我", url
        assert msgs[0]["text"] == url, url


def test_url_kept_verbatim_for_privacy_layer():
    """URL 必须完整保留在 message.text 里（脱敏交给 privacy 层决定）。"""
    url = "https://www.xinian5216.com/a/b?c=d#e"
    msgs = parse_chat(f"用户A\n2026年08月21日 13:43\n{url}")
    assert msgs[0]["text"] == url


def test_participants_only_from_sender_header_positions():
    msgs = parse_chat(ANON_WECHAT)
    assert detect_participants(msgs) == ["用户A", "用户B"]
    for token in ("https", "http", "行16", "行那就16", "www", "16:30",
                  "example", "com"):
        assert token not in detect_participants(msgs)


def test_english_colon_and_digits_in_body_do_not_open_speaker():
    chat = (
        "用户A\n2026年08月21日 13:43\n版本1:30应该能好\n\n"
        "用户B\n2026年08月21日 13:44\nOK: noted\n\n"
        "用户A\n2026年08月21日 13:45\n文件名 report_v2.pdf 看到了"
    )
    msgs = parse_chat(chat, my_name="用户A", them_name="用户B")
    assert detect_participants(msgs) == ["用户A", "用户B"]
    assert msgs[0]["text"] == "版本1:30应该能好"
    assert msgs[2]["text"] == "文件名 report_v2.pdf 看到了"


# ---------------------------------------------------------------------------
# 匿名合成回归样本
# ---------------------------------------------------------------------------


def test_anonymous_regression_fixture():
    msgs = parse_chat(ANON_WECHAT, my_name="用户A", them_name="用户B")
    assert detect_participants(msgs) == ["用户A", "用户B"]
    assert [m["speaker"] for m in msgs] == [
        "me", "them", "me", "them", "me", "them"
    ]
    # URL 与两个 16:30 句子正文完整保留
    assert msgs[0]["text"] == "https://www.example.com"
    assert msgs[2]["text"] == "行 16:30到商场？还是再早点晚点的 看你吧"
    assert msgs[4]["text"] == "行那就16:30吧"
    assert all(m["content_type"] == "text" for m in msgs[:5])
    # 语音：media / voice / 7 秒
    voice = msgs[5]
    assert voice["content_type"] == "media"
    assert voice["media_kinds"] == [MEDIA_KIND_VOICE]
    assert voice["duration_seconds"] == 7
    assert voice["text"] == "[发送了一条 7 秒语音，内容未知]"


# ---------------------------------------------------------------------------
# 空行无害 / 旧格式兼容
# ---------------------------------------------------------------------------


def test_extra_blank_lines_do_not_change_speakers():
    chat = (
        "用户A\n2026年08月21日 13:43\n行 16:30到商场？\n\n\n"
        "还是再看看\n\n"
        "用户B\n2026年08月21日 13:44\n可以可以"
    )
    msgs = parse_chat(chat, my_name="用户A", them_name="用户B")
    assert detect_participants(msgs) == ["用户A", "用户B"]
    assert len(msgs) == 2
    assert msgs[0]["text"] == "行 16:30到商场？\n还是再看看"
    assert msgs[0]["speaker"] == "me"
    assert msgs[1]["speaker"] == "them"


def test_legacy_colon_format_still_works():
    msgs = parse_chat("我: 你好\nTA: 哈哈")
    assert [m["speaker"] for m in msgs] == ["me", "them"]
    assert [m["text"] for m in msgs] == ["你好", "哈哈"]


def test_legacy_timestamp_only_format_not_misparsed():
    """时间戳独占一行的旧格式：正文行不得变成昵称。"""
    chat = "\n".join([
        "2026年08月21日 13:43", "别上班偷摸打游戏了",
        "2026年08月21日 13:44", "在啊",
        "2026年08月21日 13:45", "晚点说",
    ])
    msgs = parse_chat(chat)
    assert detect_participants(msgs) == []
    assert [m["text"] for m in msgs] == ["别上班偷摸打游戏了", "在啊", "晚点说"]
    assert [m["speaker"] for m in msgs] == ["unknown"] * 3


def test_participant_need_not_repeat():
    """短聊天里只发一条消息的真实参与者也是参与者。"""
    chat = "用户A\n2026年08月21日 13:43\n在吗\n\n用户B\n2026年08月21日 13:44\n在的"
    msgs = parse_chat(chat)
    assert detect_participants(msgs) == ["用户A", "用户B"]


def test_wechat_chat_without_blank_lines():
    chat = "用户A\n2026年08月21日 13:43\nA\n用户B\n2026年08月21日 13:44\nB"
    msgs = parse_chat(chat, my_name="用户A", them_name="用户B")
    assert [m["text"] for m in msgs] == ["A", "B"]
    assert [m["speaker"] for m in msgs] == ["me", "them"]


def test_multiline_body_with_time_like_lines():
    """多行正文里的时间行也不会把消息拆开。"""
    chat = (
        "用户A\n2026年08月21日 13:43\n"
        "第一行\n16:30\n还是第二行\n\n"
        "用户B\n2026年08月21日 13:44\n好的"
    )
    msgs = parse_chat(chat, my_name="用户A", them_name="用户B")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "第一行\n16:30\n还是第二行"


# ---------------------------------------------------------------------------
# 语音
# ---------------------------------------------------------------------------

VOICE_FORMS = [
    ("[语音]", None, MEDIA_MARKERS[MEDIA_KIND_VOICE]),
    ("[语音] 7\"", 7, "[发送了一条 7 秒语音，内容未知]"),
    ("[语音]7\"", 7, "[发送了一条 7 秒语音，内容未知]"),
    ("[语音] 7''", 7, "[发送了一条 7 秒语音，内容未知]"),
    ("[语音] 7″", 7, "[发送了一条 7 秒语音，内容未知]"),
    ("[语音] 7秒", 7, "[发送了一条 7 秒语音，内容未知]"),
]


def test_voice_forms_are_media_with_duration():
    for raw, duration, marker in VOICE_FORMS:
        out = classify_media(raw)
        assert out["content_type"] == "media", raw
        assert out["media_kinds"] == [MEDIA_KIND_VOICE], raw
        assert out["text"] == marker, raw
        if duration is None:
            assert "duration_seconds" not in out, raw
        else:
            assert out["duration_seconds"] == duration, raw


def test_voice_with_wechat_filename_is_media_and_no_filename():
    out = classify_media("[语音] 微信语音_20260922162808.amr")
    assert out["content_type"] == "media"
    assert out["media_kinds"] == [MEDIA_KIND_VOICE]
    assert "amr" not in out["text"] and "微信语音" not in out["text"]


def test_voice_different_durations_are_different_messages():
    a = classify_media("[语音] 7\"")
    b = classify_media("[语音] 8\"")
    assert a["text"] != b["text"]
    assert a["duration_seconds"] == 7 and b["duration_seconds"] == 8


def test_voice_message_field_shape():
    """时长只是可选本地字段，旧 message schema 不被破坏。"""
    text_msgs = parse_chat("我: 在吗\nTA: 在的")
    assert set(text_msgs[0]) == {
        "speaker", "text", "time", "raw_speaker", "content_type", "media_kinds"
    }
    voice = parse_chat("我: 在吗\nTA: [语音] 7\"")
    assert voice[1]["duration_seconds"] == 7
    assert set(voice[1]) == set(text_msgs[0]) | {"duration_seconds"}
