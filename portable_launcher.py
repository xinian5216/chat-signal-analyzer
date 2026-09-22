"""SignalLens Windows Portable 启动器（PyInstaller console app 入口）。

双击 ``SignalLens.exe`` 以后发生的事：

1. 确定 exe 所在目录，把 ``SIGNALLENS_DATA_DIR`` 设为 ``exe 同级 data/``；
2. 创建并校验 ``data/`` **可写**——不可写时给出友好错误并退出
   （绝不偷偷改存 AppData / TEMP）；
3. 单实例检查：``data/runtime.json`` 里记录的端口健康检查通过 →
   说明已有一个 SignalLens 在跑 → 直接打开浏览器，新启动器退出；
4. 在 8765~8785 里挑一个可用端口，只绑定 **127.0.0.1**；
5. 以子进程方式启动 Streamlit（``server.headless`` + 关闭 usage stats）；
6. 轮询本地 health endpoint（有明确总超时）→ ready 后用默认浏览器打开；
7. 用户关闭这个控制台窗口 → 同时结束 Streamlit 子进程，不残留进程。

控制台只打印友好信息：**绝不打印 API Key、聊天正文、昵称或媒体内容**。

第一版保留控制台窗口（不用 ``--windowed``）：普通用户看得懂“关闭此窗口
即可退出”，出错时也有地方显示。
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# ---- 配置（可用环境变量覆盖，便于测试）-----------------------------------
PREFERRED_PORT = int(os.environ.get("SIGNALLENS_PORT", "8765") or 8765)
PORT_RANGE = 20                       # 8765 ~ 8785
HEALTH_PATH = "/healthz"
POLL_INTERVAL = 0.25                  # 秒
STARTUP_TIMEOUT = 25.0                # 秒：总超时，绝不无限等待
CHILD_ENV_FLAG = "SIGNALLENS_INTERNAL_STREAMLIT"
RUNTIME_FILENAME = "runtime.json"

APP_TITLE = "SignalLens"


class StartupError(RuntimeError):
    """启动失败（端口、服务器、超时等），消息面向最终用户。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    """控制台输出（只接受无害文本；调用方不得传入 key / 聊天内容）。"""
    try:
        print(message, flush=True)
    except Exception:
        pass


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return _exe_dir()


def _app_entry() -> Path:
    """Streamlit 入口脚本：frozen 下在 bundle 里，开发模式下在仓库根目录。"""
    candidate = _resource_dir() / "app.py"
    if not candidate.exists():
        candidate = _exe_dir() / "app.py"
    if not candidate.exists():
        raise StartupError(
            "找不到 SignalLens 主程序文件（app.py）。\n"
            "请确认 ZIP 已完整解压后重试。"
        )
    return candidate


def _data_dir() -> Path:
    override = os.environ.get("SIGNALLENS_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _exe_dir() / "data"


def _runtime_file() -> Path:
    return _data_dir() / RUNTIME_FILENAME


def _ensure_data_dir() -> Path:
    directory = _data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        raise StartupError(
            "SignalLens 当前目录不可写。\n"
            "请将整个文件夹解压到桌面、文档或其它可写目录后重新启动。\n"
            "（不要直接在压缩包内运行，也不要放进 Program Files。）\n\n"
            f"数据目录：{directory}"
        )
    # 预建运行时子目录（浏览器首次打开会话前，用户就能看到 data/logs 等）
    for sub in ("logs", "media_cache"):
        try:
            (directory / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return directory


def _port_is_free(port: int) -> bool:
    """端口是否可绑定（不设 SO_REUSEADDR；否则会误判为空闲）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _descendants(pid: int) -> list[int]:
    """子进程 PID 列表（Windows + PowerShell，标准库实现）。"""
    if os.name != "nt" or pid <= 0:
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ParentProcessId -eq %d } | "
        "Select-Object -ExpandProperty ProcessId" % pid
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in result.stdout.split() if x.strip().isdigit()]


def _pick_port() -> int:
    for port in range(PREFERRED_PORT, PREFERRED_PORT + PORT_RANGE):
        if _port_is_free(port):
            return port
    raise StartupError(
        f"找不到可用端口（{PREFERRED_PORT} ~ {PREFERRED_PORT + PORT_RANGE - 1} 都被占用）。\n"
        "请关闭其它占用本地端口的程序后重试。"
    )


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{HEALTH_PATH}"


def _healthy(port: int, timeout: float = 1.5) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(_health_url(port), timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _pid_alive(pid: int) -> bool:
    """Windows 上检查进程是否仍存在（不依赖第三方库）。"""
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def _read_runtime() -> dict | None:
    try:
        raw = _runtime_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write_runtime(port: int, pid: int) -> None:
    payload = {
        "pid": pid,
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "started_at": time.time(),
    }
    try:
        _runtime_file().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # runtime.json 只是单实例辅助信息，写失败不阻塞启动


def _remove_runtime(pid: int) -> None:
    data = _read_runtime()
    if data and data.get("pid") == pid:
        try:
            _runtime_file().unlink(missing_ok=True)
        except OSError:
            pass


def find_running_instance() -> int | None:
    """返回已在运行的 SignalLens 端口；没有则 None。

    **不轻信陈旧 PID 文件**：必须同时验证 PID 仍活着、端口在监听且
    health endpoint 返回 200。
    """
    data = _read_runtime()
    if not data:
        return None
    port = data.get("port")
    pid = data.get("pid")
    if not isinstance(port, int) or not (0 < port < 65536):
        return None
    if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
        return None
    return port if _healthy(port) else None


# ---------------------------------------------------------------------------
# Streamlit 子进程
# ---------------------------------------------------------------------------


def _streamlit_command(port: int) -> list[str]:
    entry = _app_entry()
    if getattr(sys, "frozen", False):
        # frozen：用自身再拉一个子进程，靠环境变量进入“跑 Streamlit”分支
        argv = [sys.executable]
    else:
        argv = [sys.executable, "-m", "streamlit"]
    argv += [
        "run", str(entry),
        # frozen \u5305\u91cc streamlit \u4e0d\u5728 site-packages\uff0cStreamlit \u4f1a\u628a
        # global.developmentMode \u9ed8\u8ba4\u5f00\u6210 true\uff0c\u800c developmentMode=true \u65f6
        # --server.port \u4f1a\u88ab\u62d2\u7edd\uff08"server.port does not work when
        # global.developmentMode is true"）→ 发布版必须显式关掉。
        "--global.developmentMode", "false",
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--server.fileWatcherType", "none",
    ]
    return argv


def _spawn_streamlit(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env[CHILD_ENV_FLAG] = "1"
    env.setdefault("SIGNALLENS_DATA_DIR", str(_data_dir()))
    # 不让子进程继承父进程的“内部”标记以外的调试开关
    env.pop("SIGNALLENS_PORT", None)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        _streamlit_command(port),
        env=env,
        cwd=str(_exe_dir()),
        creationflags=creationflags,
    )


def _run_streamlit_in_process() -> int:
    """子进程分支：在当前进程里跑 Streamlit CLI（frozen 单 EXE 复用）。"""
    from streamlit.web import cli

    # cli.main() 读取 sys.argv；这里只保留 "run <app.py>" 之后的参数
    argv = list(sys.argv[1:])
    if not argv:
        raise StartupError("内部错误：缺少 Streamlit 参数。")
    sys.argv = [sys.argv[0]] + argv
    cli.main()
    return 0


def _wait_until_ready(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(port, timeout=1.0):
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _open_browser(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=8)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass


def _logs_hint() -> str:
    return str(_data_dir() / "logs")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def launcher_main() -> int:
    _ensure_data_dir()

    running_port = find_running_instance()
    if running_port:
        _log(f"{APP_TITLE} 已经在运行。")
        _log(f"地址：http://127.0.0.1:{running_port}")
        _log("已在浏览器中打开；这个新窗口可以关闭。")
        _open_browser(running_port)
        return 0

    try:
        port = _pick_port()
    except StartupError as exc:
        _log(f"启动失败：{exc}")
        return 2

    _log(f"{APP_TITLE} 正在运行")
    _log("")
    _log(f"地址：http://127.0.0.1:{port}")
    _log("浏览器会自动打开；如果没打开，请手动访问上面的地址。")
    _log("关闭此窗口即可退出 SignalLens。")
    _log("")

    try:
        process = _spawn_streamlit(port)
    except OSError as exc:
        _log(f"启动失败：无法启动本地服务（{exc}）。")
        _log(f"详细日志目录：{_logs_hint()}")
        return 3

    _write_runtime(port, process.pid)
    try:
        if not _wait_until_ready(port):
            if process.poll() is not None:
                _log("启动失败：本地服务已退出。")
            else:
                _log("启动失败：等待本地服务超时。")
            _log(f"详细日志目录：{_logs_hint()}")
            return 4
        _open_browser(port)
        _log("浏览器已打开。关闭此窗口即可退出 SignalLens。")
        while True:
            code = process.poll()
            if code is not None:
                _log(f"本地服务已退出（代码 {code}）。")
                return 0 if code == 0 else 5
            time.sleep(0.4)
    except KeyboardInterrupt:
        _log("正在退出…")
        return 0
    finally:
        _terminate(process)
        _remove_runtime(process.pid)


def main() -> int:
    if os.environ.get(CHILD_ENV_FLAG) == "1":
        return _run_streamlit_in_process()
    return launcher_main()


if __name__ == "__main__":
    sys.exit(main())
