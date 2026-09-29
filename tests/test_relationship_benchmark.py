"""Issue #17 relationship-signal benchmark 数据测试（全部虚构、完全离线）。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import evaluation as ev
import score_diagnostics as sd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from evaluate import resolve_target_index, validate_cases_for_real  # noqa: E402
CASES_PATH = REPO_ROOT / "evaluation" / "cases_relationship_v0.4.json"
PAIRS_PATH = REPO_ROOT / "evaluation" / "relationship_pairs_v0.4.json"
CONV_PATH = (REPO_ROOT / "evaluation" / "fixtures"
             / "relationship_conversations_v0.4_synthetic.json")
TARGET_PATH = (REPO_ROOT / "evaluation" / "fixtures"
               / "baseline_v0.4_relationship.json")

CASES = ev.load_cases(CASES_PATH)
CASE_IDS = {c["id"] for c in CASES}


def _pairs():
    return sd.load_pairs(PAIRS_PATH, CASE_IDS)


# ---------------------------------------------------------------------------
# 虚构内容 / 隐私
# ---------------------------------------------------------------------------


def test_all_case_content_is_fictional_and_pii_free():
    for case in CASES:
        assert ev._has_pii(case["chat"]) is None, case["id"]
        assert ev._has_pii(case["target"]) is None, case["id"]
        # 只使用固定的虚构身份（我 / TA），不得出现真实昵称
        assert case["me"] == ["我"], case["id"]
        assert case["them"] == ["TA"], case["id"]
        assert case["notes"], f"{case['id']} 缺少说明"


def test_every_case_has_expectations_and_stable_category():
    categories = {c["category"] for c in CASES}
    assert categories == {"INIT", "CARE", "FAM", "SPEC", "ROM", "DIS", "NEG"}
    for case in CASES:
        assert case["expectations"], case["id"]


# ---------------------------------------------------------------------------
# 覆盖率（Issue #17 清单 ↔ case / pair）
# ---------------------------------------------------------------------------


def test_coverage_map_covers_every_issue_bullet():
    pairs_doc = _pairs()
    assert len(pairs_doc["pairs"]) == 13
    assert len(pairs_doc["singletons"]) == 17
    # 每个 coverage 条目都指向存在的 pair / case（load_pairs 已校验，双保险）
    pair_ids = {p["pair_id"] for p in pairs_doc["pairs"]}
    for bullet, ref in pairs_doc["coverage_map"].items():
        assert ref in pair_ids or ref in CASE_IDS, bullet
    # 六个模式 + 反直觉负例必须齐备
    assert set(pairs_doc["families"]) == {
        "initiative", "care", "familiarity", "special", "romantic",
        "boundary", "negatives"}


def test_pairs_have_stable_ids_and_expected_distinguishers():
    pairs_doc = _pairs()
    seen = set()
    for p in pairs_doc["pairs"]:
        assert p["pair_id"] not in seen
        seen.add(p["pair_id"])
        assert p["expected_distinguishers"]
        assert p["factor"]
        # 成对设计：两侧都在 benchmark 中
        assert p["baseline"] in CASE_IDS and p["contrast"] in CASE_IDS
    for s in pairs_doc["singletons"]:
        assert s["expected_distinguishing_points"]
        assert s["case_id"] in CASE_IDS


def test_frozen_real_pairs_reference_existing_case_sets():
    pairs_doc = _pairs()
    assert len(pairs_doc["frozen_real_pairs"]) >= 10
    for pair in pairs_doc["frozen_real_pairs"]:
        cases_file = REPO_ROOT / pair["case_set"]
        ids = {c["id"] for c in json.loads(cases_file.read_text(encoding="utf-8"))}
        assert pair["baseline"] in ids, pair["pair_id"]
        assert pair["contrast"] in ids, pair["pair_id"]


# ---------------------------------------------------------------------------
# synthetic provenance（绝不冒充真实 Jev 输出）
# ---------------------------------------------------------------------------


def test_conversation_fixture_declares_synthetic_provenance():
    conv = sd.load_conversation_fixture(CONV_PATH)
    assert conv["meta"]["provenance"] == "synthetic"
    assert "synthetic" in conv["meta"]["disclaimer"].lower() or \
        "合成" in conv["meta"]["disclaimer"]
    for case_id, payload in conv["cases"].items():
        for result in payload["results"]:
            assert result["model"].startswith("synthetic"), case_id


def test_target_fixture_matches_conversation_fixture():
    conv = sd.load_conversation_fixture(CONV_PATH)
    target = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    assert set(target) == set(conv["cases"])
    for case_id, payload in conv["cases"].items():
        assert target[case_id] == payload["results"][payload["target_index"]]


# ---------------------------------------------------------------------------
# 既有 harness 兼容（不修改任何冻结 expectation）
# ---------------------------------------------------------------------------


def test_new_case_set_passes_existing_harness_with_synthetic_fixture():
    results = ev.load_fixture_results(TARGET_PATH)
    aggregate = ev.evaluate_cases(CASES, results)
    assert aggregate["passed_cases"] == aggregate["total_cases"] == 43
    assert aggregate["passed_constraints"] == aggregate["total_constraints"]


def test_new_case_set_passes_real_mode_preflight():
    assert validate_cases_for_real(CASES) == []
    for case in CASES:
        history = ev._case_history(case)
        assert resolve_target_index(case, history) >= 0


def test_cli_benchmark_run_is_offline_and_reproducible(tmp_path):
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "evaluate.py"),
           "--fixtures", "--cases", str(CASES_PATH),
           "--fixture-file", str(TARGET_PATH),
           "--report", str(tmp_path / "report.json")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          timeout=300, cwd=str(REPO_ROOT))
    assert proc.returncode == 0
    assert "43/43" in proc.stdout
    assert (tmp_path / "report.json").exists()


# ---------------------------------------------------------------------------
# 诊断工具链离线 / 不触网
# ---------------------------------------------------------------------------


def test_diagnostics_sources_never_call_jev_statically():
    for rel in ("score_diagnostics.py", "scripts/run_score_diagnostics.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for token in ("system_one", "typesafe_sdk", "TypeSafeClient", "httpx",
                      "requests", "urllib.request", "socket"):
            assert token not in src, f"{rel} 含可疑 token {token}"


def test_cli_diagnostics_end_to_end_offline(tmp_path):
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "run_score_diagnostics.py"),
           "--out-dir", str(tmp_path / "out")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          timeout=300, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "out"
    for name in ("report.json", "report.md", "paired_deltas.csv",
                 "family_distributions.csv", "noul_transform_curve.csv",
                 "dilution_stress.csv", "weight_sensitivity.csv"):
        assert (out / name).exists(), name
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["meta"]["provenance"]["conversation_fixtures"].startswith("synthetic")
    assert report["real_raw"]["available"] is False
    assert "no-live-API" in report["real_raw"]["note"]


def test_cli_diagnostics_is_byte_reproducible(tmp_path):
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "run_score_diagnostics.py"),
           "--out-dir", str(tmp_path / "a")]
    cmd2 = [sys.executable, str(REPO_ROOT / "scripts" / "run_score_diagnostics.py"),
            "--out-dir", str(tmp_path / "b")]
    for c in (cmd, cmd2):
        proc = subprocess.run(c, capture_output=True, text=True, encoding="utf-8",
                              timeout=300, cwd=str(REPO_ROOT))
        assert proc.returncode == 0, proc.stderr
    for name in ("report.json", "report.md", "paired_deltas.csv",
                 "noul_transform_curve.csv"):
        a = (tmp_path / "a" / name).read_bytes()
        b = (tmp_path / "b" / name).read_bytes()
        assert a == b, name
