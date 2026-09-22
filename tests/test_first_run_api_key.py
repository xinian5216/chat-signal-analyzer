"""首次 API Key 配置 UI 测试（AppTest + mock，绝不调用真实 Jev）。

覆盖需求：

- 没有 key 时不 crash、不白屏，直接显示友好的首次配置页；
- 保存后写入 ``data/settings.env``（或开发模式 ``.env``）并立即生效；
- 修改 / 清除后状态正确；
- key 不出现在页面文本、日志、报告或异常信息里。
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
FAKE_KEY = "tsk_first_run_not_a_real_key_abcdef"

CHAT = """我
2026年08月21日 21:00
第一条

TA
2026年08月21日 21:05
收到，谢谢"""


@pytest.fixture
def portable_env(tmp_path, monkeypatch):
    """模拟 portable：数据目录指向 tmp_path，且进程里没有 API Key。"""
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return tmp_path / "data"


def _texts(at) -> str:
    chunks = []
    for attr in ("markdown", "success", "info", "warning", "error", "caption",
                 "metric", "subheader", "title", "text", "dataframe", "badge"):
        for e in getattr(at, attr, []) or []:
            try:
                chunks.append(str(e.value))
            except Exception:
                pass
    return "\n".join(chunks)


def _fresh(portable_env):
    at = AppTest.from_file(str(APP_PATH), default_timeout=90)
    at.run()
    return at


def _button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"button {label!r} not found; have {[b.label for b in at.button]}")


def _text_input(at, label):
    for t in at.text_input:
        if t.label == label:
            return t
    raise AssertionError(f"text_input {label!r} not found")


def _is_password(widget) -> bool:
    """TextInput proto.type: 0 = default, 1 = password。"""
    return int(widget.proto.type) == 1


# ---------------------------------------------------------------------------
# 首次配置
# ---------------------------------------------------------------------------


def test_no_key_shows_first_run_setup(portable_env):
    at = _fresh(portable_env)
    assert not at.exception
    body = _texts(at)
    assert "SignalLens 首次设置" in body
    # 主流程不渲染（不会白屏，也不会进分析）
    assert "① 粘贴聊天记录" not in body
    # 说明保存位置
    assert "settings.env" in body
    # 输入框是 password 类型：不回显
    assert _is_password(_text_input(at, "TypeSafe API Key"))
    # key 不出现在页面上
    assert FAKE_KEY not in body


def test_save_key_writes_settings_env_and_continues(portable_env):
    at = _fresh(portable_env)
    _text_input(at, "TypeSafe API Key").set_value(FAKE_KEY)
    _button(at, "保存并继续").click()
    at.run()
    assert not at.exception

    settings = portable_env / "settings.env"
    assert settings.exists()
    content = settings.read_text(encoding="utf-8")
    assert content.startswith("TYPESAFE_API_KEY=")
    assert FAKE_KEY in content

    # 保存后立即进入主流程（无需重启）
    body = _texts(at)
    assert "① 粘贴聊天记录" in body
    assert "SignalLens 首次设置" not in body


def test_empty_key_is_rejected(portable_env):
    at = _fresh(portable_env)
    _text_input(at, "TypeSafe API Key").set_value("")
    _button(at, "保存并继续").click()
    at.run()
    assert not at.exception
    assert "不能为空" in _texts(at)
    assert not (portable_env / "settings.env").exists()
    assert "SignalLens 首次设置" in _texts(at)


def test_dev_mode_without_override_keeps_working(tmp_path, monkeypatch):
    """开发模式（无 SIGNALLENS_DATA_DIR）行为不变：仍读仓库 .env / 环境变量。"""
    monkeypatch.delenv("SIGNALLENS_DATA_DIR", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "tsk_dev_not_a_real_key")
    at = AppTest.from_file(str(APP_PATH), default_timeout=90)
    at.run()
    assert not at.exception
    body = _texts(at)
    assert "① 粘贴聊天记录" in body
    assert "SignalLens 首次设置" not in body


# ---------------------------------------------------------------------------
# 侧栏管理
# ---------------------------------------------------------------------------


def test_sidebar_api_settings_modify_and_clear(portable_env):
    import settings_store

    settings_store.save_api_key(FAKE_KEY)
    at = _fresh(portable_env)
    body = _texts(at)
    assert "TypeSafe API Key：已配置" in body
    # 不回显完整 key
    assert FAKE_KEY not in body

    # 侧边栏的 Key 输入框同样是 password
    key_box = _text_input(at, "修改 API Key")
    assert _is_password(key_box)
    key_box.set_value("tsk_second_not_a_real_key_9999")
    _button(at, "保存新 Key").click()
    at.run()
    assert not at.exception
    # 反馈通过 toast（rerun 会丢弃本轮元素，所以走 session_state）
    assert any("API Key" in str(t.value) for t in at.toast), [t.value for t in at.toast]
    assert "tsk_second_not_a_real_key_9999" in (
        portable_env / "settings.env").read_text(encoding="utf-8")

    # 清除：只删配置，聊天 / 缓存不受影响
    at.text_area[0].set_value(CHAT)
    for b in at.button:
        if b.label == "解析并替换当前聊天":
            b.click()
            break
    at.run()
    messages_before = at.session_state["messages"]

    _button(at, "清除本地 API Key").click()
    at.run()
    assert not at.exception
    # 回到未配置状态：主页让位给首次配置页（不再渲染侧边栏）
    assert "SignalLens 首次设置" in _texts(at)
    assert "① 粘贴聊天记录" not in _texts(at)
    # 聊天数据仍在
    assert at.session_state["messages"] == messages_before


def test_key_never_leaks_into_logs_or_reports(portable_env):
    import settings_store

    settings_store.save_api_key(FAKE_KEY)
    at = _fresh(portable_env)
    body = _texts(at)
    assert FAKE_KEY not in body
    # 页面 / state 里都不出现完整 key
    assert FAKE_KEY not in str(at.session_state)
    # 设置文件里只有 key 本身这一行
    content = (portable_env / "settings.env").read_text(encoding="utf-8")
    assert content.count("\n") <= 1
