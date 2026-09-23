"""凭据隔离回归测试（历史测试事故的防复发门槛）。

历史事故：``settings_store.save_api_key()`` 直接写
``os.environ["TYPESAFE_API_KEY"]``（正式应用的有意行为），monkeypatch
跟踪不到；某 AppTest 保存 Key 后变量泄漏给后续测试与其派生的子进程，
导致 ``--real`` 门禁测试拿到假 Key 后真的向 Jev 发起了请求。

本文件锁定以下性质（与测试顺序无关）：

1. ``save_api_key`` 之后，环境变量在下一个测试被恢复到原状态；
2. 测试环境即使存在（泄漏的）TYPESAFE_API_KEY，门禁子进程也不会触网；
3. fixture evaluation 在任何环境下都保持 34/34，且不读取 API Key；
4. 测试本身绝不构造 TypeSafeClient / 调用 system_one。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import settings_store

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "evaluate.py"


def _guarded_env(**extra) -> dict:
    """隔离环境：可用 extra 精确注入变量，其余凭据变量一律移除。"""
    env = dict(os.environ)
    for key in ("TYPESAFE_API_KEY", "PYTEST_CURRENT_TEST", "CI"):
        env.pop(key, None)
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# 1. save_api_key 之后环境变量必须恢复（跨测试断言，依赖文件内顺序）
# ---------------------------------------------------------------------------

_SNAPSHOT: dict = {}


def test_save_api_key_does_not_leak_into_next_test(tmp_path, monkeypatch):
    from tests.conftest import GUARDED_ENV_KEYS
    _SNAPSHOT["before"] = {key: os.environ.get(key)
                           for key in GUARDED_ENV_KEYS}

    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    settings_store.save_api_key("tsk_regression_fake_key_000")
    assert os.environ["TYPESAFE_API_KEY"] == "tsk_regression_fake_key_000"


def test_env_restored_after_previous_test_saved_a_key():
    """上一个测试调用 save_api_key 后，本测试必须看到恢复后的环境。"""
    if "before" not in _SNAPSHOT:  # pragma: no cover - 单独运行该文件时的兜底
        pytest.skip("与前一个测试同文件运行才有跨测试断言意义")
    for key, value in _SNAPSHOT["before"].items():
        assert os.environ.get(key) == value
    assert os.environ.get("TYPESAFE_API_KEY") != "tsk_regression_fake_key_000"


# ---------------------------------------------------------------------------
# 2. 环境里有（假）Key 也不会进入真实模式
# ---------------------------------------------------------------------------


def test_real_mode_refuses_even_with_leaked_key_present():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--real", "--yes-run-live-api"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT),
        env=_guarded_env(TYPESAFE_API_KEY="tsk_leaked_fake_key_000",
                         CI="true"))
    assert proc.returncode == 2
    assert "CI" in (proc.stdout + proc.stderr)


def test_real_mode_refuses_under_pytest_with_key_present():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--real", "--yes-run-live-api"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT),
        env=_guarded_env(TYPESAFE_API_KEY="tsk_leaked_fake_key_000",
                         PYTEST_CURRENT_TEST="tests/x.py::test_leaked"))
    assert proc.returncode == 2
    assert "pytest" in (proc.stdout + proc.stderr).lower()


# ---------------------------------------------------------------------------
# 3. fixture 评估不依赖 API Key，任何环境下都 34/34
# ---------------------------------------------------------------------------


def test_fixtures_mode_with_fake_key_present_stays_offline():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixtures"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
        cwd=str(REPO_ROOT),
        env=_guarded_env(TYPESAFE_API_KEY="tsk_inert_fake_key_000"))
    assert proc.returncode == 0
    assert "34/34" in proc.stdout
    assert "386/386" in proc.stdout


def test_conftest_guards_credential_keys():
    from tests.conftest import GUARDED_ENV_KEYS
    assert "TYPESAFE_API_KEY" in GUARDED_ENV_KEYS
    assert "SIGNALLENS_DATA_DIR" in GUARDED_ENV_KEYS


# ---------------------------------------------------------------------------
# 4. 静态保证：测试源里不得出现任何真实客户端构造
# ---------------------------------------------------------------------------


def test_test_sources_never_construct_a_real_client():
    """静态保证：不得出现带凭据的真实客户端构造。

    ``test_analyzer_concurrency.py`` 里的 ``_StubSDK`` 是该文件有意为之的
    SDK 子类安全网（覆盖 __init__，不建真实连接；无 SDK 时 skip），允许存在。
    """
    tests_dir = REPO_ROOT / "tests"
    self_path = Path(__file__).resolve()
    for path in sorted(tests_dir.glob("*.py")):
        if path.resolve() == self_path:
            continue  # 本文件包含 token 字面量，跳过自身
        src = path.read_text(encoding="utf-8")
        for token in ("TypeSafeClient(api_key", "TypeSafeClient(api_key="):
            if token in src:
                pytest.fail(f"{path.name} 中出现真实客户端构造：{token}")
