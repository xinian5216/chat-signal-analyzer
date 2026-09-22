"""portable 路径与配置测试（不启动真实服务器、不调用真实 Jev）。

覆盖：

- 开发模式保持历史路径（``.jev_cache/cache.db``、``.env``）；
- ``SIGNALLENS_DATA_DIR`` / frozen 模式下数据集中到 ``data/``；
- 数据目录不可写时给出友好错误，**不回退 AppData / TEMP**；
- 运行时子目录（cache / media_cache / logs）确实创建在 data 下；
- API Key 保存到 ``data/settings.env``，清除只删配置；
- API Key **不出现**在日志 / 报告 / 缓存 / 诊断摘要里；
- 启动器端口选择、单实例校验、runtime.json、子进程清理逻辑。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import paths
import settings_store

REPO = Path(__file__).resolve().parents[1]

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="Windows-only portable launcher behavior"
)


FAKE_KEY = "tsk_test_not_a_real_key_0123456789"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从干净的环境变量开始（不读真实 .env）。"""
    monkeypatch.delenv("SIGNALLENS_DATA_DIR", raising=False)
    monkeypatch.delenv(settings_store.API_KEY_ENV, raising=False)
    yield


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_dev_mode_keeps_legacy_paths(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("SIGNALLENS_DATA_DIR", raising=False)
    assert paths.is_frozen() is False
    assert paths.cache_db_path() == REPO / ".jev_cache" / "cache.db"
    assert paths.settings_env_path() == REPO / ".env"
    assert paths.media_cache_dir() == REPO / ".media_cache"
    assert paths.logs_dir() == REPO / "logs"


def test_data_dir_override(tmp_path, monkeypatch):
    target = tmp_path / "portable-data"
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(target))
    assert paths.data_dir() == target
    assert paths.cache_db_path() == target / "cache.sqlite3"
    assert paths.settings_env_path() == target / "settings.env"
    assert paths.media_cache_dir() == target / "media_cache"
    assert paths.logs_dir() == target / "logs"
    assert paths.runtime_file_path() == target / "runtime.json"


def test_frozen_mode_uses_exe_sibling_data(tmp_path, monkeypatch):
    exe = tmp_path / "SignalLens" / "SignalLens.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("stub", encoding="utf-8")
    monkeypatch.delenv("SIGNALLENS_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    try:
        assert paths.is_frozen() is True
        assert paths.app_dir() == exe.parent
        assert paths.data_dir() == exe.parent / "data"
        assert paths.cache_db_path() == exe.parent / "data" / "cache.sqlite3"
        # 不依赖 cwd
        assert str(tmp_path) in str(paths.data_dir())
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_ensure_runtime_dirs_creates_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    created = paths.ensure_runtime_dirs()
    assert created == tmp_path / "data"
    for sub in ("logs", "media_cache"):
        assert (tmp_path / "data" / sub).is_dir()
    assert (tmp_path / "data" / "cache.sqlite3").parent.is_dir()


def test_deleted_data_dir_is_recreated(tmp_path, monkeypatch):
    """删掉 data 目录：下次启动自动重建（需求 25-D）。"""
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    paths.ensure_runtime_dirs()
    import shutil

    shutil.rmtree(tmp_path / "data")
    assert not (tmp_path / "data").exists()
    paths.ensure_runtime_dirs()
    assert (tmp_path / "data" / "logs").is_dir()
    assert (tmp_path / "data" / "media_cache").is_dir()


def test_copying_the_folder_keeps_paths_relative(tmp_path, monkeypatch):
    """复制整个 SignalLens 目录后，路径仍然相对新 exe（需求 25-E）。"""
    import shutil

    source = tmp_path / "源目录"
    copied = tmp_path / "新目录 测试"
    (source / "_internal").mkdir(parents=True)
    (source / "SignalLens.exe").write_text("stub", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "settings.env").write_text("TYPESAFE_API_KEY=x\n", encoding="utf-8")
    shutil.copytree(source, copied)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(copied / "SignalLens.exe"), raising=False)
    try:
        assert paths.data_dir() == copied / "data"
        assert paths.cache_db_path() == copied / "data" / "cache.sqlite3"
        assert paths.settings_env_path() == copied / "data" / "settings.env"
        # 与 cwd 无关
        assert paths.app_dir() == copied
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    """从其它 cwd 启动，数据路径不变（需求 25-C）。"""
    exe = tmp_path / "SignalLens" / "SignalLens.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("stub", encoding="utf-8")
    other_cwd = tmp_path / "somewhere-else"
    other_cwd.mkdir()
    monkeypatch.delenv("SIGNALLENS_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    monkeypatch.chdir(other_cwd)
    try:
        assert paths.app_dir() == exe.parent
        assert paths.data_dir() == exe.parent / "data"
        assert paths.cache_db_path() == exe.parent / "data" / "cache.sqlite3"
        assert not str(other_cwd) in str(paths.cache_db_path())
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_unwritable_data_dir_raises_friendly_error(tmp_path, monkeypatch):
    """\u4e0d\u53ef\u5199\u65f6\u5fc5\u987b\u62a5\u9519\uff0c\u800c\u4e0d\u662f\u5077\u5077\u6539\u5b58 AppData / TEMP\u3002"""
    blocker = tmp_path / "data"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(blocker))
    with pytest.raises(paths.DataDirError) as excinfo:
        paths.ensure_data_dir()
    message = str(excinfo.value)
    assert "\u4e0d\u53ef\u5199" in message
    assert str(blocker) in message
    # 不得把 AppData / TEMP 当成“回退目标”（先把路径本身去掉：pytest 临时目录
    # 本身就在 AppData\Local\Temp 下，不能因此误判）
    body = message.replace(str(blocker), "")
    lowered = body.lower()
    for banned in ("appdata", "roaming", "localappdata", "临时目录"):
        assert banned not in lowered


def test_describe_contains_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv(settings_store.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path))
    summary = paths.describe()
    assert summary["mode"] in ("dev", "portable")
    assert FAKE_KEY not in json_dump(summary)


def json_dump(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# settings_store
# ---------------------------------------------------------------------------


def test_save_and_load_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    assert settings_store.has_api_key() is False

    written = settings_store.save_api_key(FAKE_KEY)
    assert written == tmp_path / "data" / "settings.env"
    assert written.exists()
    # 立即对当前进程生效
    assert settings_store.api_key() == FAKE_KEY

    settings_file = written.read_text(encoding="utf-8")
    assert settings_file.startswith("TYPESAFE_API_KEY=tsk_")

    # 模拟重启：清空进程环境，从文件重新加载
    monkeypatch.delenv(settings_store.API_KEY_ENV, raising=False)
    settings_store.load_settings()
    assert settings_store.api_key() == FAKE_KEY


def test_process_env_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    settings_store.save_api_key(FAKE_KEY)
    monkeypatch.setenv(settings_store.API_KEY_ENV, "tsk_from_process_env")
    settings_store.load_settings()
    assert settings_store.api_key() == "tsk_from_process_env"


def test_clear_api_key_only_removes_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    path = settings_store.save_api_key(FAKE_KEY)
    path.write_text(
        "TYPESAFE_API_KEY=" + FAKE_KEY + "\nSIGNALLENS_PORT=8765\n", encoding="utf-8"
    )
    settings_store.clear_api_key()
    assert settings_store.has_api_key() is False
    # 其它键保留；聊天 / 缓存不受影响
    assert "SIGNALLENS_PORT=8765" in path.read_text(encoding="utf-8")
    assert FAKE_KEY not in path.read_text(encoding="utf-8")


def test_clear_api_key_removes_empty_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    path = settings_store.save_api_key(FAKE_KEY)
    settings_store.clear_api_key()
    assert not path.exists()


def test_save_rejects_empty_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(ValueError):
        settings_store.save_api_key("   ")


def test_settings_summary_never_contains_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))
    settings_store.save_api_key(FAKE_KEY)
    summary = settings_store.settings_summary()
    assert summary["configured"] is True
    assert FAKE_KEY not in json_dump(summary)
    assert "key_material" not in summary


def test_api_key_never_enters_cache_or_report(tmp_path, monkeypatch):
    """API Key \u4e0d\u5f97\u8fdb\u5165\u7f13\u5b58 / \u62a5\u544a / \u65e5\u5fd7\u3002"""
    import storage
    from report import build_json_report
    from scoring import compute_conversation_stats

    monkeypatch.setenv(settings_store.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path / "data"))

    cache = storage.Cache(paths.cache_db_path())
    cache.set("k", {"model": "fake"})
    cache_file = Path(paths.cache_db_path()).read_bytes().decode("utf-8", "ignore")
    assert FAKE_KEY not in cache_file
    assert FAKE_KEY not in json_dump(cache.get("k"))

    stats = compute_conversation_stats([])
    data = build_json_report([], stats, include_text=True)
    assert FAKE_KEY not in json_dump(data)


# ---------------------------------------------------------------------------
# portable_launcher（纯逻辑部分，不启动真实服务器）
# ---------------------------------------------------------------------------


def test_launcher_port_picker_skips_busy_port():
    import portable_launcher as launcher

    taken = launcher._pick_port()
    assert launcher.PREFERRED_PORT <= taken < launcher.PREFERRED_PORT + launcher.PORT_RANGE

    busy = portable_launcher_bind(taken)
    try:
        chosen = launcher._pick_port()
        assert chosen != taken
        assert launcher._port_is_free(taken) is False
    finally:
        busy.close()


def portable_launcher_bind(port: int):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


@WINDOWS_ONLY
def test_launcher_requires_valid_instance(tmp_path, monkeypatch):
    """\u4e0d\u8fba\u4fe1\u9648\u65e7 PID \u6587\u4ef6\uff1a\u5fc5\u987b\u9a8c\u8bc1 PID + \u7aef\u53e3 + health\u3002"""
    import portable_launcher as launcher

    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path))
    (tmp_path / "runtime.json").write_text(
        '{"pid": 999999, "port": 8765}', encoding="utf-8"
    )
    assert launcher.find_running_instance() is None


def test_launcher_runtime_roundtrip(tmp_path, monkeypatch):
    import portable_launcher as launcher

    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_healthy", lambda port, timeout=1.5: True)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    launcher._write_runtime(8765, 4242)
    assert launcher.find_running_instance() == 8765
    data = launcher._read_runtime()
    assert data["port"] == 8765 and data["pid"] == 4242
    assert data["url"] == "http://127.0.0.1:8765"
    launcher._remove_runtime(4242)
    assert launcher._read_runtime() is None


def test_launcher_dead_pid_is_stale(tmp_path, monkeypatch):
    import portable_launcher as launcher

    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path))
    launcher._write_runtime(8765, 4242)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(launcher, "_healthy", lambda port, timeout=1.5: True)
    assert launcher.find_running_instance() is None


def test_launcher_unhealthy_port_is_stale(tmp_path, monkeypatch):
    import portable_launcher as launcher

    monkeypatch.setenv("SIGNALLENS_DATA_DIR", str(tmp_path))
    launcher._write_runtime(8765, 4242)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(launcher, "_healthy", lambda port, timeout=1.5: False)
    assert launcher.find_running_instance() is None


def test_launcher_streamlit_command_is_localhost_only():
    import portable_launcher as launcher

    argv = launcher._streamlit_command(8765)
    assert "127.0.0.1" in argv
    assert "0.0.0.0" not in " ".join(argv)
    assert str(8765) in argv
    assert "--browser.gatherUsageStats" in argv
    assert "false" in argv
    assert "run" in argv


@WINDOWS_ONLY
def test_launcher_terminate_reaps_process():
    """\u5b50\u8fdb\u7a0b\u9000\u51fa\u540e\u4e0d\u5f97\u6709\u5b50\u8fdb\u7a0b\u9057\u7559\uff08Windows\uff09\u3002"""
    import portable_launcher as launcher

    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
         "time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    grandchild = None
    import time as _time

    try:
        deadline = _time.monotonic() + 15
        while _time.monotonic() < deadline:
            kids = launcher._descendants(proc.pid)
            if kids:
                grandchild = kids[0]
                break
            _time.sleep(0.2)
        assert grandchild is not None, "没能取到子进程 PID"
        launcher._terminate(proc)
        proc.wait(timeout=10)
        assert proc.poll() is not None
        if grandchild is not None:
            assert not _alive(grandchild), f"\u5b50\u8fdb\u7a0b\u9057\u7559\uff1a{grandchild}"
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def _alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    script = (
        "Get-Process -Id %d -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Id" % pid
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    return bool(result.stdout.strip())
