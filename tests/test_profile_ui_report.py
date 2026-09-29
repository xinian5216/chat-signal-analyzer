"""Issue #18 集成测试：report（JSON additive / Markdown / 0 API / 确定性）与
结果页 UI（Profile 优先 / insufficient 不显示为中等 / unsupported 正确显示 /
overall 保留但降级 / 无“喜欢概率”类措辞）。"""

import json
import re
import socket
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import analyzer
import storage
from interaction_dynamics import build_interaction_events
from relationship_profile import build_profile
from report import build_json_report, build_markdown_report
from scoring import compute_conversation_stats
from storage import Cache

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CHAT = """我: 在忙吗
TA: 收到，谢谢你
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你还记得那个梗啊
TA: 你还记得那个梗啊哈哈
我: 请看一下附件
TA: 请确认附件是否收到
我: 这事只有你懂
TA: 行行行，还是你懂我"""

SHORT_CHAT = """我: 在忙吗
TA: 还行"""


class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_response(evidence=2.4, ease=2.3, warmth=2.0, special=1.5,
                  distancing=0.2, intent_choice="teasing",
                  intent_probs=None):
    from types import SimpleNamespace as NS
    return NS(answers={
        "emotion": FakeAnswer(choice="teasing",
                              probabilities={"teasing": 0.93, "calm": 0.07},
                              confidence=0.85),
        "intent": FakeAnswer(choice=intent_choice,
                             probabilities=intent_probs
                             or {"tease": 0.91, "other": 0.09},
                             confidence=0.88),
        "warmth": FakeAnswer(score=warmth,
                             probabilities={str(int(warmth)): 1.0},
                             confidence=0.8),
        "engagement": FakeAnswer(score=2.0, probabilities={"2": 1.0},
                                 confidence=0.8),
        "special_attention": FakeAnswer(score=special,
                                        probabilities={str(int(special)): 1.0},
                                        confidence=0.8),
        "relationship_evidence_strength": FakeAnswer(score=evidence,
                                                     probabilities={str(int(evidence)): 1.0},
                                                     confidence=0.8),
        "relational_ease": FakeAnswer(score=ease, probabilities={str(int(ease)): 1.0},
                                      confidence=0.8),
        "romantic_signal": FakeAnswer(noul=0.2),
        "distancing_signal": FakeAnswer(noul=distancing),
    }, model="fake")


def make_result(warmth=2.0, engagement=2.0, special=1.5,
                romantic=0.2, distancing=0.2, evidence=2.4, ease=2.3):
    return {
        "emotion": {"choice": "teasing", "probabilities": {"teasing": 0.93},
                    "confidence": 0.85},
        "intent": {"choice": "tease", "probabilities": {"tease": 0.91},
                   "confidence": 0.88},
        "warmth": {"score": warmth, "probabilities": {"2": 1.0}, "confidence": 0.8},
        "engagement": {"score": engagement, "probabilities": {"2": 1.0},
                       "confidence": 0.8},
        "special_attention": {"score": special, "probabilities": {"1": 0.5, "2": 0.5},
                              "confidence": 0.8},
        "relationship_evidence_strength": {"score": evidence,
                                           "probabilities": {"2": 1.0},
                                           "confidence": 0.8},
        "relational_ease": {"score": ease, "probabilities": {"3": 1.0},
                            "confidence": 0.8},
        "romantic_signal": romantic,
        "distancing_signal": distancing,
    }


def make_entries(n=6):
    return [{"index": i, "speaker": "them", "time": f"22:3{i % 10}",
             "text": f"消息{i}", "result": make_result()}
            for i in range(n)]


# ---------------------------------------------------------------------------
# Report：JSON additive / Markdown / 确定性 / 0 API
# ---------------------------------------------------------------------------


def test_json_report_profile_is_additive_and_legacy_intact():
    results = make_entries()
    stats = compute_conversation_stats(results)
    payload = build_json_report(results, stats, include_text=False)
    assert payload["relationship_profile"]["version"] == "relationship-profile-v2"
    assert set(payload["relationship_profile"]["dimensions"])
    # legacy 字段原样保留（additive，不破坏既有 schema）
    for key in ("metadata", "summary", "aggregate", "behavior_stats", "messages"):
        assert key in payload
    aggregate = payload["aggregate"]
    for key in ("overall", "overall_sufficient", "recent", "trend",
                "total_weight", "warmth_avg", "romantic_evidence",
                "distancing_evidence", "low_evidence_display"):
        assert key in aggregate
    assert aggregate["overall"] == stats["overall"]
    assert payload["summary"]["text"]


def test_markdown_report_profile_precedes_overall():
    results = make_entries()
    stats = compute_conversation_stats(results)
    md = build_markdown_report(results, stats, include_text=False)
    profile_pos = md.index("## 关系画像（Relationship Profile v2）")
    overall_pos = md.index("## 总体结果")
    assert profile_pos < overall_pos
    assert "### 边界压力" in md
    assert "不支持" in md
    # legacy 章节完整保留
    for section in ("# 聊天信号分析报告", "## 基本信息", "## 整段互动行为",
                    "## 主要关系信号", "## 趋势", "## 分析摘要", "## 方法说明"):
        assert section in md
    assert "互动亲近信号指数" in md


def test_report_profile_section_is_deterministic():
    results = make_entries()
    stats = compute_conversation_stats(results)
    md1 = build_markdown_report(results, stats, include_text=False)
    md2 = build_markdown_report(results, stats, include_text=False)
    sec1 = md1[md1.index("## 关系画像"):md1.index("## 总体结果")]
    sec2 = md2[md2.index("## 关系画像"):md2.index("## 总体结果")]
    assert sec1 == sec2
    p1 = build_json_report(results, stats)["relationship_profile"]
    p2 = build_json_report(results, stats)["relationship_profile"]
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)


def test_report_export_never_calls_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("报告导出不允许网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(analyzer, "create_client",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("报告导出不允许创建客户端")))
    results = make_entries()
    stats = compute_conversation_stats(results)
    build_markdown_report(results, stats, include_text=True)
    build_json_report(results, stats, include_text=True)
    build_profile(results, stats=stats)


def test_report_profile_contains_no_chat_text():
    results = make_entries()
    stats = compute_conversation_stats(results)
    md = build_markdown_report(results, stats, include_text=False)
    for e in results:
        assert e["text"] not in md
    payload = build_json_report(results, stats, include_text=False)
    profile_text = json.dumps(payload["relationship_profile"],
                              ensure_ascii=False)
    for e in results:
        assert e["text"] not in profile_text


# ---------------------------------------------------------------------------
# UI（Streamlit AppTest，FakeClient，0 真实 API）
# ---------------------------------------------------------------------------


@pytest.fixture
def counting_client(monkeypatch, tmp_path):
    calls = []

    class CountingClient:
        def system_one(self, state, questions):
            calls.append(state)
            return fake_response()

    class TmpCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    import paths
    monkeypatch.setattr(paths, "friend_history_db_path",
                        lambda: tmp_path / "friend_history.db")
    monkeypatch.setattr(analyzer, "create_client",
                        lambda api_key: CountingClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)
    return calls


def _button_by_label(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"button {label!r} not found")


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


def _metric_labels(at) -> str:
    return "\n".join(str(e.proto.label) for e in at.metric)


def _run_to_results(at, chat):
    at.session_state["input_mode"] = "text"
    at.run()
    at.text_area[0].set_value(chat)
    _button_by_label(at, "解析并替换当前聊天").click()
    at.run()
    at.selectbox[0].select("我")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    _button_by_label(at, "开始 Jev 分析").click()
    at.run()
    assert not at.exception


def test_overview_profile_is_primary_and_overall_demoted(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _run_to_results(at, CHAT)
    body = _texts(at)
    # Profile 置顶且为主要解释模块
    assert "关系画像" in body
    assert "画像摘要" in body
    assert "主动性与投入" in body and "浪漫 / 暧昧信号" in body
    # overall 保留但降级为辅助指标（不再是 ### 大标题）
    labels = _metric_labels(at)
    assert "辅助指数" in labels
    assert "互动亲近信号指数" in body
    hero = [str(e.value) for e in at.markdown if re.match(r"###\s*\d", str(e.value))]
    assert not hero
    # 不得出现伪精确 / 读心式输出（禁止“喜欢概率：”这类数值输出形式；
    # 免责声明中“不是喜欢概率”的否定句是应有措辞，不禁）
    for banned in ("喜欢概率：", "恋爱可能性", "关系健康度", "72.4%", "读心",
                   "尊重分：", "好感度："):
        assert banned not in body, banned
    assert len(counting_client) > 0


def test_overview_shows_unsupported_boundary_pressure(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _run_to_results(at, CHAT)
    body = _texts(at)
    assert "边界压力" in body
    assert "当前 schema 不支持可靠判断" in body or "不支持" in body
    # unsupported 不得显示成“边界压力：低”
    assert "边界压力：低" not in body
    assert "边界压力　低" not in body


def test_low_evidence_shows_insufficient_not_mid(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _run_to_results(at, SHORT_CHAT)
    body = _texts(at)
    assert "关系画像" in body
    assert "数据不足，无法判断" in body
    # 低信息量：保留参考指数与警示（既有行为）
    assert "当前样本关系信息不足" in body
    labels = _metric_labels(at)
    assert "参考指数" in labels


# ---------------------------------------------------------------------------
# Issue #19：salience 通道（JSON additive / Markdown 分区 / UI 显著与相反证据）
# ---------------------------------------------------------------------------


def _event_entries():
    """虚构：30 条普通 + 1 条明确特殊关注 + 1 条明显冷淡（反向）。"""
    ordinary = make_result()
    special = make_result(warmth=2.6, engagement=3.0, special=3.6, evidence=3.2)
    cold = make_result(warmth=0.4, engagement=0.5, special=0.8, evidence=2.6,
                       ease=0.8, distancing=0.3)
    specs = [ordinary] * 30 + [special, cold]
    return [{"index": i, "speaker": "them", "time": f"22:{i % 60:02d}",
             "text": f"消息{i}", "result": r} for i, r in enumerate(specs)]


def test_json_report_salience_key_additive_no_duplication():
    results = _event_entries()
    stats = compute_conversation_stats(results)
    payload = build_json_report(results, stats, include_text=False)
    assert payload["salience"]["version"] == "salience-v1"
    assert payload["salience"]["salient_event_count"] == 1
    assert payload["salience"]["counter_event_count"] == 3
    # 事件明细只保存在 relationship_profile 维度内（顶层不重复保存事件列表）
    assert "events" not in payload["salience"]
    dims = payload["relationship_profile"]["dimensions"]
    assert len(dims["special_attention"]["salient_events"]) == 1
    assert dims["special_attention"]["baseline"]["value"] is not None
    # legacy 键不变
    assert payload["aggregate"]["overall"] == stats["overall"]


def test_markdown_report_shows_baseline_salient_counter():
    results = _event_entries()
    stats = compute_conversation_stats(results)
    md = build_markdown_report(results, stats, include_text=False)
    assert "基线互动" in md
    assert "显著证据" in md and "相反证据" in md
    assert "明确特殊关注信号" in md
    assert "不与显著证据抵消或平均" in md
    # 无伪精度 / 无 boost 类数值
    for banned in ("salience 8", "boost", "重要度", "加分", "综合抵消后"):
        assert banned not in md, banned


def test_report_events_section_is_deterministic():
    results = _event_entries()
    stats = compute_conversation_stats(results)
    first = build_markdown_report(results, stats, include_text=False)
    second = build_markdown_report(results, stats, include_text=False)
    strip = lambda s: re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d", "", s)
    assert strip(first) == strip(second)


@pytest.fixture
def event_client(monkeypatch, tmp_path):
    """返回“明确特殊关注 + 明显冷淡”答案的假客户端（0 真实 API）。"""
    calls = []

    class EventClient:
        def system_one(self, state, questions):
            calls.append(state)
            return fake_response(special=3.6, warmth=0.4, evidence=3.2)

    class TmpCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    import paths
    monkeypatch.setattr(paths, "friend_history_db_path",
                        lambda: tmp_path / "friend_history.db")
    monkeypatch.setattr(analyzer, "create_client",
                        lambda api_key: EventClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)
    return calls


def test_ui_profile_card_shows_salient_and_counter_sections(event_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _run_to_results(at, CHAT)
    body = _texts(at)
    # §26：显著证据与相反证据分区并存，禁止抵消成“中等”
    assert "显著证据" in body
    assert "相反证据" in body
    assert "明确特殊关注信号" in body
    assert "基线互动" in body
    assert "综合抵消" not in body and "抵消后" not in body
    # §27：不暴露伪精度
    for banned in ("salience ", "重要度", "boost", "加分"):
        assert banned not in body, banned


# ---------------------------------------------------------------------------
# Issue #20\uff1aInteraction Dynamics\uff08JSON additive / Markdown / UI \u4e92\u52a8\u7ed3\u6784\uff09
# ---------------------------------------------------------------------------

CHAT_FOLLOWUP = """\u6211: \u6700\u8fd1\u80c3\u4e0d\u8212\u670d
TA: \u600e\u4e48\u4e86\uff1f
\u6211: \u53ef\u80fd\u5403\u574f\u4e86
TA: \u73b0\u5728\u8fd8\u75bc\u5417\uff1f"""


def _interaction_entries():
    """\u865a\u6784\u8fde\u7eed\u8ffd\u95ee\uff08TA \u4e24\u95ee\uff0cme \u9648\u8ff0/\u56de\u5e94\uff09\u3002"""
    return [msg("me", "\u6700\u8fd1\u80c3\u4e0d\u8212\u670d", "2026-03-02 10:00"),
            msg("them", "\u600e\u4e48\u4e86\uff1f", "2026-03-02 10:01"),
            msg("me", "\u53ef\u80fd\u5403\u574f\u4e86", "2026-03-02 10:02"),
            msg("them", "\u73b0\u5728\u8fd8\u75bc\u5417\uff1f", "2026-03-02 10:03")]


def msg(speaker, text, time="2026-03-02 10:00", **kw):
    out = {"speaker": speaker, "text": text, "time": time,
           "raw_speaker": "我" if speaker == "me" else "TA",
           "content_type": kw.pop("content_type", "text"),
           "media_kinds": kw.pop("media_kinds", [])}
    out.update(kw)
    return out


def _ask_result():
    r = make_result()
    r["intent"] = {"choice": "ask_information",
                   "probabilities": {"ask_information": 0.9, "other": 0.1},
                   "confidence": 0.88}
    return r


def _followup_results(messages):
    ta = [m for m in messages if m["speaker"] == "them"]
    return [{"index": 1, "speaker": "them", "text": ta[0]["text"],
             "time": ta[0]["time"], "context": [],
             "result": _ask_result()},
            {"index": 3, "speaker": "them", "text": ta[1]["text"],
             "time": ta[1]["time"], "context": [],
             "result": _ask_result()}]


def test_json_report_interaction_key_additive():
    messages = _interaction_entries()
    results = _followup_results(messages)
    stats = compute_conversation_stats(results)
    interaction = build_interaction_events(messages, results)
    payload = build_json_report(results, stats, include_text=False,
                                interaction=interaction)
    assert payload["interaction_dynamics"]["version"] == "interaction-dynamics-v1"
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "fingerprints" not in dumped
    assert "identity" not in dumped
    events = payload["interaction_dynamics"]["events"]
    assert any(e["event_type"] == "followup_sequence" for e in events)
    for key in ("metadata", "summary", "aggregate", "behavior_stats",
                "messages", "relationship_profile", "salience"):
        assert key in payload


def test_json_report_interaction_default_empty():
    results = make_entries()
    stats = compute_conversation_stats(results)
    payload = build_json_report(results, stats, include_text=False)
    entry = payload["interaction_dynamics"]
    assert entry["version"] == "interaction-dynamics-v1"
    assert entry["events"] == []


def test_markdown_report_interaction_section():
    messages = _interaction_entries()
    results = _followup_results(messages)
    stats = compute_conversation_stats(results)
    interaction = build_interaction_events(messages, results)
    md = build_markdown_report(results, stats, include_text=False,
                               interaction=interaction)
    assert "\u4e92\u52a8\u7ed3\u6784\u8bc1\u636e" in md
    assert "followup_sequence" in md
    assert "\u4ec5\u63cf\u8ff0\u5f53\u524d\u5bfc\u5165\u6837\u672c" in md
    assert "\u6700\u8fd1\u80c3\u4e0d\u8212\u670d" not in md  # \u9ed8\u8ba4\u4e0d\u5199\u804a\u5929\u6b63\u6587


def test_markdown_report_interaction_section_is_deterministic():
    messages = _interaction_entries()
    results = _followup_results(messages)
    stats = compute_conversation_stats(results)
    interaction = build_interaction_events(messages, results)
    strip = lambda s: re.sub(r"\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d", "", s)
    first = build_markdown_report(results, stats, include_text=False,
                                  interaction=interaction)
    second = build_markdown_report(results, stats, include_text=False,
                                   interaction=interaction)
    assert strip(first) == strip(second)


@pytest.fixture
def ask_client(monkeypatch, tmp_path):
    """TA \u4e00\u5f8b\u8fd4\u56de\u8be2\u95ee\u610f\u56fe\u7684\u5047\u5ba2\u6237\u7aef\uff08\u89e6\u53d1\u8fde\u7eed\u8ffd\u95ee\u7ed3\u6784\uff09\u3002"""
    class AskClient:
        def system_one(self, state, questions):
            return fake_response(intent_choice="ask_information",
                                 intent_probs={"ask_information": 0.9,
                                               "other": 0.1})

    class TmpCache(Cache):
        def __init__(self, *a, **k):
            super().__init__(tmp_path / "cache.db")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    import paths
    monkeypatch.setattr(paths, "friend_history_db_path",
                        lambda: tmp_path / "friend_history.db")
    monkeypatch.setattr(analyzer, "create_client",
                        lambda api_key: AskClient())
    monkeypatch.setattr(storage, "Cache", TmpCache)


def test_ui_profile_shows_interaction_structure(ask_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _run_to_results(at, CHAT_FOLLOWUP)
    body = _texts(at)
    assert "\u4e92\u52a8\u7ed3\u6784" in body
    assert "\u8fde\u7eed\u8ffd\u95ee" in body
    expander_labels = " ".join(str(e.label) for e in at.expander)
    assert "\u67e5\u770b\u4e92\u52a8\u7a97\u53e3" in expander_labels
    assert "\u7ed3\u6784\u89c2\u5bdf" in body


def test_ui_interaction_wording_no_psychology(ask_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _run_to_results(at, CHAT_FOLLOWUP)
    body = _texts(at)
    # \u5426\u5b9a\u5f0f\u514d\u8d23\u58f0\u660e\uff08\u201c\u66f4\u4e0d\u662f TA \u559c\u6b22\u4f60\u7684\u6982\u738c\u201d\uff09\u7b26\u5408\u4ed3\u5e93\u7ea6\u5b9a\uff0c\u4e0d\u7981\uff1b
    # \u8fd9\u91cc\u53ea\u7981\u65ad\u8a00\u5f0f\u5fc3\u7406 / \u4eba\u683c\u63a8\u6d4b\u3002
    for banned in ("\u5728\u4e4e", "\u60f3\u4f60", "\u5f88\u5c0a\u91cd\u4f60",
                   "\u4eba\u683c\u7c7b\u578b",
                   "\u597d\u611f\u589e\u52a0", "\u5173\u7cfb\u53d8\u597d"):
        assert banned not in body, banned
