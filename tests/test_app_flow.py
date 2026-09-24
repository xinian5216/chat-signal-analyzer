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

# 多片段追加用的合成微信聊天（虚构昵称，片段 B 与 A 在末尾重叠一条）
CHAT_A = """我
2026年08月21日 21:00
第一条

TA
2026年08月21日 21:05
收到，谢谢"""

CHAT_B_OVERLAP = """TA
2026年08月21日 21:05
收到，谢谢

我
2026年08月21日 21:10
第二条

TA
2026年08月21日 21:15
好的"""

CHAT_B_NEW_PARTICIPANT = """TA
2026年08月21日 21:05
收到，谢谢

老王
2026年08月21日 21:20
第三方也说一句"""

# 合成昵称样本（虚构）：带句点的昵称 + 句点昵称的映射链路回归
NICKNAME_CHAT = """陌寒.
2026年08月14日 11:33
她我觉得挺好的

赵老狗
2026年08月14日 11:34
行

赵老狗
2026年08月14日 11:34
我看下学期的老师都不太认识

陌寒.
2026年08月14日 11:34
挺负责的"""

NICKNAME_CHAT_B = """赵老狗
2026年08月14日 11:40
好的

陌寒.
2026年08月14日 11:41
行 16:30到商场？"""


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
    _button_by_label(at, "解析并替换当前聊天").click()
    at.run()


def _use_text_mode(at) -> None:
    """AppTest 中无浏览器 iframe，组件返回 None；显式切到纯文本路径。"""
    at.session_state["input_mode"] = "text"
    at.run()


def _append(at, chat) -> None:
    """在输入区粘贴片段 B 并点“追加到当前聊天”。"""
    at.text_area[0].set_value(chat)
    _button_by_label(at, "追加到当前聊天").click()
    at.run()


def _apply_mapping(at) -> None:
    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()


def _switch_view(at, view: str) -> None:
    """切换结果视图（懒渲染导航，0 Jev API）。"""
    at.segmented_control[0].set_value(view)
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

    # ④ 结果视图导航出现，且阶段指示器到 ④
    assert [s.label for s in at.segmented_control] == ["结果视图"]
    assert [o for o in at.segmented_control[0].options] == [
        "概览", "关键消息", "全部消息", "报告", "长期观察"
    ]
    steps = [str(e.value) for e in at.markdown if "① 粘贴聊天" in str(e.value)]
    assert steps and ":blue[④ 查看 / 导出结果]" in steps[0], steps
    assert "互动亲近信号指数" in _texts(at)
    assert "关系信息量" in _texts(at)     # 概览视图自身就有（reference / normal 两种模式）

    # ---- UI 操作必须 0 额外 API ----
    n = len(counting_client)
    _switch_view(at, "关键消息")
    assert len(counting_client) == n
    _switch_view(at, "全部消息")
    assert len(counting_client) == n
    _switch_view(at, "报告")
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

    # 来回切换视图五次（懒渲染导航，0 额外 API）
    for view in ("关键消息", "全部消息", "报告", "概览", "全部消息"):
        _switch_view(at, view)
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

    # 全部消息视图：默认不显示媒体事件，勾选后显示
    _switch_view(at, "全部消息")
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
    """输入阶段 = 文本 + 可选图片上传 + 替换 / 追加两个恒定按钮。"""
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    labels = [b.label for b in at.button]
    assert "解析并替换当前聊天" in labels
    assert "追加到当前聊天" in labels
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

    _switch_view(at, "报告")
    assert len(counting_client) == n
    # 勾选“包含原文”也不得触发 API
    at.checkbox[0].check()
    at.run()
    assert len(counting_client) == n
    assert not at.exception


# ---------------------------------------------------------------------------
# 多片段追加（微信一次复制的条数有限）
# ---------------------------------------------------------------------------


def test_append_overlapping_chunk_zero_api_and_dedup(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception
    _use_text_mode(at)
    _parse(at, CHAT_A)
    assert len(at.session_state["messages"]) == 2

    _append(at, CHAT_B_OVERLAP)
    assert not at.exception

    # 追加 / 合并 / 去重全部本地完成：0 次 Jev 调用
    assert len(counting_client) == 0
    # A 2 条 + B 3 条，重叠 1 条 → 4 条
    assert len(at.session_state["messages"]) == 4
    stats = at.session_state["append_stats"]
    assert (stats.chunk_size, stats.duplicates, stats.added, stats.total) == (3, 1, 2, 4)

    body = _texts(at)
    assert "已追加片段" in body
    assert "本次 3 条" in body and "检测重复 1 条" in body and "新增 2 条" in body
    assert "当前总消息 4 条" in body
    assert "未调用 Jev" in body
    # 追加后旧结果失效，需要重新分析
    assert at.session_state["results"] is None


def test_append_keeps_identity_mapping(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT_A)
    _apply_mapping(at)
    assert at.session_state["applied_me"] == "我"
    mapping_before = (at.session_state["applied_me"], at.session_state["applied_ta"])

    _append(at, CHAT_B_OVERLAP)
    assert not at.exception
    # 参与者集合没变 → 昵称映射原样保留，不要求重新选择
    assert (at.session_state["applied_me"], at.session_state["applied_ta"]) \
        == mapping_before
    assert [m["speaker"] for m in at.session_state["messages"]] == [
        "me", "them", "me", "them"
    ]
    assert "请重新确认" not in _texts(at)


def test_append_new_participant_requires_reconfirmation(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT_A)
    _apply_mapping(at)
    assert at.session_state["applied_me"] == "我"

    _append(at, CHAT_B_NEW_PARTICIPANT)
    assert not at.exception
    body = _texts(at)
    assert "新的参与者" in body and "老王" in body
    assert "不会自动把第三方归为 TA" in body
    # 映射被重置，等待用户重新确认；新参与者不是自动变成 TA
    assert at.session_state["applied_me"] is None
    assert at.session_state["applied_ta"] is None


def test_append_metadata_never_enters_jev_state(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT_A)
    _apply_mapping(at)
    _append(at, CHAT_B_OVERLAP)      # 参与者不变 → 映射保留
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()
    assert not at.exception
    assert counting_client                # 确实分析了几条
    for state in counting_client:
        assert set(state["target_message"]) == {
            "speaker", "text", "time"
        }
        for key in ("fingerprint", "chunk_id", "source", "duration_seconds"):
            assert key not in state["target_message"]
        for c in state["conversation_context"]:
            assert set(c) == {"speaker", "text", "time"}


def test_apply_nickname_mapping_maps_all_messages(counting_client):
    """回归（f16625b）：应用“我 / TA”映射后不得全部变成 unknown。

    修复前：participants 正常（2 个），但 rebuild 读的是尚未写入的
    applied_me/applied_ta，导致 我 0 条 / TA 0 条 / unknown 全部。
    """
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, NICKNAME_CHAT)

    # 初次解析：raw_speaker 正确，speaker 全 unknown（还没指定昵称）
    before = at.session_state["messages"]
    assert [m["raw_speaker"] for m in before] == [
        "陌寒.", "赵老狗", "赵老狗", "陌寒."
    ]
    assert all(m["speaker"] == "unknown" for m in before)

    at.selectbox[0].select("陌寒.")
    at.selectbox[1].select("赵老狗")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    assert not at.exception

    after = at.session_state["messages"]
    assert [m["raw_speaker"] for m in after] == [
        "陌寒.", "赵老狗", "赵老狗", "陌寒."
    ]
    assert [m["speaker"] for m in after] == ["me", "them", "them", "me"]

    body = _texts(at)
    assert "身份映射完成" in body
    assert "我：2 条" in body and "TA：2 条" in body and "unknown：0 条" in body
    assert "unknown：4 条" not in body
    assert len(counting_client) == 0          # 映射阶段 0 次 API


def test_append_keeps_mapping_and_no_unknown(counting_client):
    """回归（f16625b）：追加片段后原消息不得全部变 unknown，映射必须保留。"""
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, NICKNAME_CHAT)
    at.selectbox[0].select("陌寒.")
    at.selectbox[1].select("赵老狗")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    assert [m["speaker"] for m in at.session_state["messages"]] == [
        "me", "them", "them", "me"
    ]

    _append(at, NICKNAME_CHAT_B)
    assert not at.exception
    msgs = at.session_state["messages"]
    assert len(msgs) == 6
    assert [m["speaker"] for m in msgs] == [
        "me", "them", "them", "me", "them", "me"
    ]
    assert [m for m in msgs if m["speaker"] == "unknown"] == []
    assert at.session_state["applied_me"] == "陌寒."
    assert at.session_state["applied_ta"] == "赵老狗"
    # URL / 16:30 正文与语音链路依旧正常
    assert "行 16:30到商场？" in [m["text"] for m in msgs]


def test_replace_button_resets_chat(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _use_text_mode(at)
    _parse(at, CHAT_A)
    _append(at, CHAT_B_OVERLAP)
    assert len(at.session_state["messages"]) == 4

    # 再粘贴另一段并“解析并替换当前聊天”
    at.text_area[0].set_value("我: 全新的一段\nTA: 知道了")
    _button_by_label(at, "解析并替换当前聊天").click()
    at.run()
    assert not at.exception
    assert len(at.session_state["messages"]) == 2
    assert len(counting_client) == 0
    assert at.session_state["append_stats"] is None
    assert at.session_state["raw_chunks"] == ["我: 全新的一段\nTA: 知道了"]
