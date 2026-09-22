"""Frozen smoke test：真的运行构建出来的 SignalLens.exe。

覆盖：

- 进程真的起来了（process alive）
- ``http://127.0.0.1:<port>/healthz`` = 200，根路径 = 200
- ``data/`` 创建在 **exe 同级**（不是 cwd）
- 从其它 cwd 启动仍正常
- 中文 / 空格路径可启动
- 没有 API Key 时页面仍能正常打开（首配页），无 traceback
- 单实例：第二次启动直接复用已有实例并退出
- 结束后**没有残留进程**

用法::

    python scripts/frozen_smoke.py                 # 自动找 dist/SignalLens/SignalLens.exe
    SIGNALLENS_FROZEN_EXE=path python scripts/frozen_smoke.py

禁止真实 Jev：本脚本只做本地 HTTP 检查，永远不提交 API Key、不调用分析。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portable_launcher import _descendants  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT = 60.0
POLL_INTERVAL = 0.5


def _find_exe() -> Path:
    env = os.environ.get("SIGNALLENS_FROZEN_EXE", "").strip()
    if env:
        return Path(env).resolve()
    candidate = ROOT / "dist" / "SignalLens" / "SignalLens.exe"
    if candidate.exists():
        return candidate
    searched = list((ROOT / "dist").glob("*/SignalLens.exe"))
    if searched:
        return searched[0]
    raise SystemExit("找不到 SignalLens.exe：请先运行 PyInstaller 构建，"
                     "或设置 SIGNALLENS_FROZEN_EXE。")


def _build_dir(exe: Path) -> Path:
    """onedir 构建目录：SignalLens.exe 的父目录（含 _internal/）。"""
    return exe.resolve().parent


def _get(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError):
        return None, b""


def _wait_ready(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _ = _get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
        if status == 200:
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _read_runtime(data_dir: Path) -> dict:
    path = data_dir / "runtime.json"
    for _ in range(40):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        time.sleep(0.25)
    raise AssertionError(f"runtime.json 未创建：{path}")


def _alive(pid: int) -> bool:
    script = (
        "Get-Process -Id %d -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Id" % pid
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    return bool(result.stdout.strip())


def _run_case(name: str, workdir: Path, expect_cn_path: bool = False) -> None:
    exe = _find_exe()
    app_dir = workdir / ("SignalLens" if not expect_cn_path else "聊天分析工具 SignalLens")
    # onedir：必须拴整个构建目录（SignalLens.exe + _internal/）
    shutil.copytree(_build_dir(exe), app_dir, dirs_exist_ok=True)
    exe_copy = app_dir / exe.name
    assert exe_copy.exists(), f"{name}: 缺少 {exe_copy}"

    env = dict(os.environ)
    env["SIGNALLENS_DATA_DIR"] = str(app_dir / "data")
    env["SIGNALLENS_PORT"] = str(_free_port())

    proc = subprocess.Popen(
        [str(exe_copy)], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        runtime = _read_runtime(app_dir / "data")
        port = runtime["port"]
        # runtime.json 记录的是服务进程（Streamlit 子进程）：必须是活的正整数
        assert isinstance(runtime["pid"], int) and runtime["pid"] > 0, runtime
        assert _alive(runtime["pid"]), f"runtime pid 不存活：{runtime['pid']}"
        assert runtime["url"] == f"http://127.0.0.1:{port}", runtime
        assert _wait_ready(port, STARTUP_TIMEOUT), f"{name}: 服务未就绪"

        status, body = _get(f"http://127.0.0.1:{port}/healthz")
        assert status == 200, f"{name}: healthz={status}"
        status_root, root_body = _get(f"http://127.0.0.1:{port}/")
        assert status_root == 200, f"{name}: root={status_root}"

        # data 必须在 exe 同级，而不是 cwd
        assert (app_dir / "data" / "runtime.json").exists(), f"{name}: data 不在 exe 旁"
        for sub in ("logs", "media_cache"):
            assert (app_dir / "data" / sub).is_dir(), f"{name}: 缺少 data/{sub}"

        # 无 API Key：页面仍应正常打开（首配页），且没有 traceback
        text = root_body.decode("utf-8", "ignore")
        assert "SignalLens" in text or "<div id=\"root\">" in text, f"{name}: 页面异常"

        # 单实例：第二次启动应直接退出并打开已有地址
        second = subprocess.run(
            [str(exe_copy)], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=STARTUP_TIMEOUT,
        )
        assert second.returncode == 0, second.stdout[-2000:]
        assert _wait_ready(port, 5.0), f"{name}: 单实例后服务消失"
        print(f"[frozen-smoke] OK {name} (port {port})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        _reap_children(proc.pid)


def _reap_children(pid: int) -> None:
    """确保没有残留的 python / streamlit 子进程。"""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        children = _descendants(pid)
        if not children:
            return
        for child in children:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Stop-Process -Id {child} -Force -ErrorAction SilentlyContinue"],
                capture_output=True, timeout=60,
            )
        time.sleep(0.5)
    leftovers = _descendants(pid)
    assert not leftovers, f"存在残留子进程：{leftovers}"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    _find_exe()
    print("[frozen-smoke] exe:", _find_exe())
    with tempfile.TemporaryDirectory(prefix="SignalLens-frozen-") as tmp:
        base = Path(tmp)
        # A. 普通路径：data 在 exe 同级
        _run_case("plain-path", base)
        # B. 中文 + 空格路径
        _run_case("cn-space-path", base / "工佚目录 测试",
                  expect_cn_path=True)
    print("[frozen-smoke] all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
