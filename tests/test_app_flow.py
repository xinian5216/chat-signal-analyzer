"""端到端 UI 流程测试（Streamlit AppTest，真实运行时，零真实 API）。

覆盖：
- ① 输入 → ② 确认（昵称映射）→ ③ 分析 → ④ 结果 tabs 全流程；
- **映射状态在 rerun / 切 tab 后不回退**（widget state 被清除的回归防护）；
- 切 tab / 过滤 / 展开 / 报告导出均 0 次 Jev 调用。
"""

from pathlib import Path

import analyzer
import pytest
import storage
from streamlit.testing.v1 import AppTest

from storage import Cache

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CHAT = """我: 在忙吗
TA: 收到，谢谢
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你还记得那个梗啊
TA: 你还记得那个梗啊哈哈哈
我: 请看一下附件
TA: 请确认附件是否收到
我: 这事只有你懂
TA: 行行行，还是你懂我"""

MEDIA_CHAT = """我: 在忙吗
TA: [图片] 微信图片_20260908.dat
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你看这个
TA: 你看这个 [视频] 微信视频_99.mp4
我: 好的
TA: [动画表情]"""


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_response(evidence=2.4, ease=2.3):
    from types import SimpleNamespace as NS
    return NS(answers={
        "emotion": FakeAnswer(choice="teasing",
                              probabilities={"teasing": 0.93, "calm": 0.07},
                              confidence=0.85),
        "intent": FakeAnswer(choice="tease",
                             probabilities={"tease": 0.91, "other": 0.09},
                             confidence=0.88),
        "warmth": FakeAnswer(score=2.0, probabilities={}, confidence=0.8),
        "engagement": FakeAnswer(score=2.0, probabilities={}, confidence=0.8),
        "special_attention": FakeAnswer(score=1.5, probabilities={}, confidence=0.8),
        "relationship_evidence_strength": FakeAnswer(score=evidence,
                                                     probabilities={}, confidence=0.8),
        "relational_ease": FakeAnswer(score=ease, probabilities={}, confidence=0.8),
        "romantic_signal": FakeAnswer(noul=0.2),
        "distancing_signal": FakeAnswer(noul=0.2),
    }, model="fake")


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    """让 AppTest 加载的 app 使用 FakeClient + 临时缓存。

    注意：
    1. AppTest 会**独立导入 app.py**，因此必须 patch 源模块
       （analyzer.create_client / storage.Cache），patch app 模块本身无效。
    2. 必须显式提供占位 API Key：`run_analysis` 在创建 client 之前就检查
       环境变量，CI 上没有 `.env`，否则会提前返回导致零调用。
    3. 缓存隔离到 tmp_path，既不读写本地 .jev_cache，调用计数也确定。
    """
    calls = []

    class CountingClient:
        def system_one(self, state, questions):
            calls.append(state)
            return fake_response(
                evidence=3.0 if "懂我" in state["target_message"]["text"] else 2.4
            )

    class TmpCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(analyzer, "create_client", lambda api_key: CountingClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)
    return calls


def _button_by_label(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"button {label!r} not found; have "
                         f"{[b.label for b in at.button]}")


def _texts(at) -> str:
    """收集页面上所有可见文本（markdown / success / info / warning / caption / metric）。"""
    chunks = []
    for attr in ("markdown", "success", "info", "warning", "error", "caption",
                 "metric", "subheader", "title", "text", "dataframe", "badge"):
        for e in getattr(at, attr, []) or []:
            try:
                chunks.append(str(e.value))
            except Exception:
                pass
    return "\n".join(chunks)


def _metric_labels(at) -> str:
    """metric 的 label 列表（AppTest 的 Metric.value 只给 value）。"""
    return "\n".join(str(e.proto.label) for e in at.metric)


def _parse(at, chat):
    at.text_area[0].set_value(chat)
    _button_by_label(at, "解析并预览").click()
    at.run()


def _use_text_mode(at) -> None:
    """AppTest 中无浏览器 iframe，组件返回 None；显式切到纯文本路径。"""
    at.session_state["input_mode"] = "text"
    at.run()


def test_full_flow_mapping_persists_and_zero_api(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception

    # ① 输入（AppTest 无浏览器 iframe，切到纯文本路径）
    _use_text_mode(at)
    _parse(at, CHAT)
    assert "确认聊天双方" in _texts(at)

    # ② 昵称映射（参与者就是我 / TA）
    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    assert at.session_state["applied_me"] == "我"
    assert "身份映射完成" in _texts(at)

    # 关键回归：再触发一次 rerun（不点任何分析按钮），映射显示不得回退
    at.run()
    assert "身份映射完成" in _texts(at)
    assert "请确认谁是" not in _texts(at)

    # ③ 分析（FakeClient）
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()
    assert not at.exception
    assert len(counting_client) == 5           # 5 条 TA 文本消息
    assert at.session_state["skipped_media"] == 0

    # ④ 结果 tabs 出现，且阶段指示器到 ④
    assert [t.label for t in at.tabs] == ["概览", "关键消息", "全部消息", "报告"]
    steps = [str(e.value) for e in at.markdown if "① 粘贴聊天" in str(e.value)]
    assert steps and ":blue[④ 查看 / 导出结果]" in steps[0], steps
    assert "互动亲近信号指数" in _texts(at)
    assert "有效关系消息" in _texts(at)

    # ---- UI 操作必须 0 额外 API ----
    n = len(counting_client)
    at.tabs[1].run()           # 关键消息
    at.run()
    assert len(counting_client) == n
    at.tabs[2].run()           # 全部消息
    at.run()
    assert len(counting_client) == n
    at.tabs[3].run()           # 报告
    at.run()
    assert len(counting_client) == n
    assert not at.exception


def test_tab_switch_keeps_mapping_and_results(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT)
    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()
    n = len(counting_client)

    # 来回切 tab 五次
    for idx in (1, 2, 3, 0, 2):
        at.tabs[idx].run()
        at.run()
        assert len(counting_client) == n
        assert "身份映射完成" in _texts(at)
    assert not at.exception


def test_media_chat_skips_media_and_shows_events(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, MEDIA_CHAT)

    # 解析预览：媒体计数与脱敏展示
    body = _texts(at)
    assert "检测到非文本媒体" in body
    assert "微信图片" not in body

    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()

    # TA 文本消息 = 2（哈哈你又来了 / 混合消息），纯媒体 2 条被跳过
    assert at.session_state["skipped_media"] == 2
    assert len(counting_client) == 2

    # 全部消息 tab：默认不显示媒体事件，勾选后显示
    at.tabs[2].run()
    at.run()
    n = len(counting_client)
    body = _texts(at)
    before = body.count("内容未分析")     # 仅②阶段预览表里的媒体行
    at.checkbox[0].check()          # 显示媒体事件
    at.run()
    assert len(counting_client) == n                # 仍然 0 API
    body = _texts(at)
    assert body.count("内容未分析") > before          # 媒体事件卡出现
    assert "微信图片" not in body and "微信视频" not in body
    assert not at.exception


def test_low_evidence_overview_uses_reference_mode(monkeypatch, tmp_path):
    """低信息量样本：不得把 overall 当主指标，须显示参考指数与警示文案。"""
    import storage as st_mod

    class LowEvidenceClient:
        def system_one(self, state, questions):
            from types import SimpleNamespace as NS

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return NS(answers={
                "emotion": A(choice="calm", probabilities={"calm": 1.0}, confidence=0.9),
                "intent": A(choice="other", probabilities={"other": 1.0}, confidence=0.9),
                "warmth": A(score=1.5, probabilities={}, confidence=0.9),
                "engagement": A(score=1.5, probabilities={}, confidence=0.9),
                "special_attention": A(score=1.0, probabilities={}, confidence=0.9),
                "relationship_evidence_strength": A(score=0.2, probabilities={},
                                                    confidence=0.9),
                "relational_ease": A(score=2.0, probabilities={}, confidence=0.9),
                "romantic_signal": A(noul=0.1),
                "distancing_signal": A(noul=0.1),
            }, model="fake")

    class TmpCache(st_mod.Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(analyzer, "create_client", lambda api_key: LowEvidenceClient())
    monkeypatch.setattr(st_mod, "Cache", TmpCache)

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT)
    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()
    assert not at.exception

    body = _texts(at)
    assert "当前样本关系信息不足" in body
    assert "仅供参考" in body and "不建议据此判断" in body
    assert "参考指数" in _metric_labels(at)
    assert "信息覆盖率" in _metric_labels(at)
    # 低信息量时不应把“互动亲近信号指数”作为 hero 展示
    hero = [str(e.value) for e in at.markdown if "互动亲近信号指数" in str(e.value)]
    assert not any("/ 100" in h for h in hero)


def test_input_stage_has_text_and_optional_image_upload(counting_client):
    """输入阶段 = 文本 + 可选图片上传（v0.2.0 形态；剪贴板直采见 Probe）。"""
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    labels = [b.label for b in at.button]
    assert "解析并预览" in labels
    # 不再有 rich/text 模式切换按钮
    assert "富媒体粘贴不可用？切换到纯文本输入" not in labels
    assert "切回富媒体粘贴" not in labels
    # 文本域 + 文件上传器同时存在
    assert len(at.text_area) == 1
    assert len(at.file_uploader) == 1
    assert len(counting_client) == 0


def test_report_tab_export_zero_api(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT)
    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()
    n = len(counting_client)

    at.tabs[3].run()
    at.run()
    assert len(counting_client) == n
    # 勾选“包含原文”也不得触发 API
    at.checkbox[0].check()
    at.run()
    assert len(counting_client) == n
    assert not at.exception
