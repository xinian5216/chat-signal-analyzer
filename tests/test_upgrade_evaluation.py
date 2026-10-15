"""Model evaluation planning and source-identity guards, no live requests."""
import hashlib
import json
from pathlib import Path

import evaluation as ev
from scripts import evaluate

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evaluation" / "cases_jev114_zh.json"


def test_new_chinese_cases_are_frozen_and_have_valid_targets():
    cases = ev.load_cases(CASES)
    assert len(cases) == 20
    assert not evaluate.validate_cases_for_real(cases)
    assert hashlib.sha256(CASES.read_bytes()).hexdigest() == "abf8258ac93c5df1c3e508f4e2e4d3496adf215de809f44f46e4c78997079148"


def test_plan_does_not_enter_live_mode(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("no live calls allowed")
    monkeypatch.setattr(evaluate, "_run_real", forbidden)
    assert evaluate.main(["--plan", "--cases", str(CASES)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["total_paired_requests"] == 40
    assert plan["live_api_called"] is False


def test_fixture_report_has_source_identity_and_cannot_mix_with_real(tmp_path):
    report = tmp_path / "fixture.json"
    assert evaluate._run_fixtures(str(report)) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["meta"]["provenance"] == "synthetic"
    assert payload["meta"]["benchmark_sha256"]
    other = tmp_path / "real.json"
    payload["meta"].pop("provenance")
    other.write_text(json.dumps(payload), encoding="utf-8")
    assert evaluate._compare(str(report), str(other)) == 3
