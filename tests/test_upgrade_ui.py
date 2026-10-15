"""Opt-in UI flow tests: synthetic chat/images, no real network or user data."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from streamlit.testing.v1 import AppTest

import analyzer
import paths
from tests.test_app_flow import (_use_text_mode, _parse, _button_by_label,
                                  _switch_view, MEDIA_CHAT, CHAT, fake_response)
from tests.test_vision_opt_in import asset

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def clients(monkeypatch, tmp_path):
    calls = []
    class Client:
        def __init__(self, model):
            self.model = model
        def system_one(self, state, questions, **kwargs):
            calls.append({"state": state, "questions": questions, **kwargs})
            if "image" in state or "event_type" in state:
                return NS(model=self.model, answers={name: NS(choice=next(iter(q.criteria)), confidence=.9)
                    for name, q in questions.items()})
            response = fake_response()
            response.model = self.model
            return response
        def close(self):
            pass
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setattr(analyzer, "create_client",
                        lambda api_key, model=None: Client(model or analyzer.DEFAULT_MODEL))
    return calls


def fresh(chat):
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    _use_text_mode(at)
    _parse(at, chat)
    at.selectbox(key="sel_me").select("我")
    at.selectbox(key="sel_ta").select("TA")
    at.run()
    _button_by_label(at, "应用昵称映射并重新解析").click()
    at.run()
    return at


def test_114_is_explicit_and_result_provenance_survives_settings_changes(clients):
    at = fresh(CHAT)
    assert at.selectbox(key="selected_analysis_model").value == "jev-1.13.0"
    at.selectbox(key="selected_analysis_model").select("jev-1.14.0").run()
    assert not clients
    _button_by_label(at, "开始 Jev 分析").click().run()
    assert len(clients) == 5
    assert {e["result"]["model"] for e in at.session_state["results"]} == {"jev-1.14.0"}
    at.selectbox(key="selected_analysis_model").select("jev-1.13.0").run()
    _switch_view(at, "报告")
    assert len(clients) == 5
    from report import build_json_report
    report = build_json_report(at.session_state["results"], at.session_state["stats"])
    assert report["metadata"]["model"] == "jev-1.14.0"
    assert not at.exception


def image_flow():
    at = fresh(MEDIA_CHAT)
    image = asset()
    at.session_state["media_assets"] = [image]
    at.session_state["media_bindings"] = {1: image.id}
    at.run()
    at.checkbox(key="visual_enabled").check().run()
    _button_by_label(at, "生成最终上传预览").click().run()
    return at, image


def test_image_is_not_sent_until_final_preview_is_approved(clients):
    at, image = image_flow()
    assert not clients
    _button_by_label(at, "开始 Jev 分析").click().run()
    assert len(clients) == 2
    assert at.session_state["visual_results"] == []
    approval = next(c for c in at.checkbox if str(c.key).startswith("vision_approved_"))
    approval.check().run()
    _button_by_label(at, "开始 Jev 分析").click().run()
    assert len(clients) == 3  # text is cached; one explicitly approved image
    assert "image" in clients[-1]["state"]
    assert clients[-1]["extra_headers"] == {"TypeSafe-Danger-Zone-Images": "true"}
    assert len(at.session_state["visual_results"]) == 1
    from report import build_json_report
    output = build_json_report(at.session_state["results"], at.session_state["stats"])
    assert output["metadata"]["visual_analysis"]["analyzed"] == 1
    import json
    assert "daily_sharing" not in json.dumps(output)  # visual content not exported
    before = len(clients)
    _switch_view(at, "报告")
    assert len(clients) == before and not at.exception


def test_changed_binding_invalidates_upload_permission(clients):
    at, image = image_flow()
    approval = next(c for c in at.checkbox if str(c.key).startswith("vision_approved_"))
    approval.check().run()
    match = next(s for s in at.selectbox if str(s.key).startswith("media_match_1_"))
    # selectbox has a format_func: select by displayed label.
    match.select("（不分析）").run()
    _button_by_label(at, "开始 Jev 分析").click().run()
    assert len(clients) == 2 and not at.session_state["visual_results"]


def test_image_only_chat_shows_completed_visual_results(clients):
    at = fresh("我: 看到了吗\nTA: [图片]")
    image = asset()
    at.session_state["media_assets"] = [image]
    at.session_state["media_bindings"] = {1: image.id}
    at.run()
    at.checkbox(key="visual_enabled").check().run()
    _button_by_label(at, "生成最终上传预览").click().run()
    next(c for c in at.checkbox if str(c.key).startswith("vision_approved_")).check().run()
    _button_by_label(at, "开始 Jev 分析").click().run()
    assert len(clients) == 1 and at.session_state["results"] == []
    assert at.segmented_control and not at.exception


def test_image_failure_keeps_text_and_does_not_display_exception_content(clients, monkeypatch):
    import vision
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive-content-must-not-be-displayed")
    monkeypatch.setattr(vision, "analyze_prepared", fail)
    at, image = image_flow()
    next(c for c in at.checkbox if str(c.key).startswith("vision_approved_")).check().run()
    _button_by_label(at, "开始 Jev 分析").click().run()
    assert len(at.session_state["results"]) == 2
    assert at.session_state["stats"]["visual_analysis"]["failed"] == 1
    assert not at.exception
    from tests.test_app_flow import _texts
    assert "sensitive-content" not in _texts(at)


def test_event_assistance_is_explicit_and_does_not_confirm_events(clients):
    at = fresh("我: 我不想去聚餐\nTA: 表格今天必须发我")
    at.checkbox(key="event_review_enabled").check().run()
    assert clients == []
    _button_by_label(at, "开始 Jev 分析").click().run()
    reviews = at.session_state["event_review_results"]
    assert reviews and all(e["result"]["review_required"] for e in reviews)
    assert at.session_state["stats"]["event_semantic_review"]["auto_confirmed"] is False
    assert not at.exception
