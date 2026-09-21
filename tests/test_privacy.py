"""privacy 脱敏测试。"""

from privacy import mask_messages, mask_text


def test_phone():
    assert mask_text("打这个电话 13812345678 找我") == "打这个电话 <PHONE> 找我"


def test_phone_not_matching_short_numbers():
    # QQ 号 / 普通数字不应被误伤
    assert mask_text("我 QQ 是 1234567") == "我 QQ 是 1234567"


def test_email():
    assert mask_text("发我邮箱 zhang.san_99@qq.com 谢谢") == "发我邮箱 <EMAIL> 谢谢"


def test_id_card():
    assert mask_text("身份证号 110101199003077758 已登记") == "身份证号 <ID> 已登记"


def test_ip():
    assert mask_text("从 192.168.1.1 登录的") == "从 <IP> 登录的"


def test_url():
    assert (
        mask_text("链接 https://example.com/p?token=abc123secret 看看")
        == "链接 <URL> 看看"
    )


def test_api_key_patterns():
    assert mask_text("key=AKIAIOSFODNN7EXAMPLE1") == "<SECRET>"
    assert mask_text('api_key: "sk-abcdefghijklmnop1234"') == 'api_key: <SECRET>'


def test_bank_card():
    assert mask_text("卡号 6222021234567890123 绑好了") == "卡号 <CARD> 绑好了"


def test_card_does_not_eat_id_card():
    # 18 位身份证号应先被 ID 规则替换，而不是 CARD
    assert "<ID>" in mask_text("110101199003077758")


def test_chinese_text_untouched():
    text = "可能比较沉浸，刚洗完澡哈哈"
    assert mask_text(text) == text


def test_mask_messages_keeps_structure():
    msgs = [
        {"speaker": "them", "text": "我电话 13812345678", "time": "22:31"},
        {"speaker": "me", "text": "好", "time": None},
    ]
    out = mask_messages(msgs)
    assert out[0]["text"] == "我电话 <PHONE>"
    assert out[0]["speaker"] == "them"
    assert out[1]["text"] == "好"
    # 不修改原始列表
    assert msgs[0]["text"] == "我电话 13812345678"
