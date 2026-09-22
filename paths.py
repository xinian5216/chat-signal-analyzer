"""统一的可变数据路径管理（portable 优先）。

SignalLens 有两种运行形态：

- **开发模式**（``python -m streamlit run app.py``）：沿用仓库内路径，
  与历史行为完全一致（``.jev_cache/cache.db``、``.env``、``.media_cache/``、
  ``logs/``）。
- **Frozen portable 模式**（双击 ``SignalLens.exe``）：所有可变数据集中放在
  exe 同级的 ``data/`` 目录：:

      SignalLens/
      ├─ SignalLens.exe
      ├─ _internal/          # 只读运行时文件
      └─ data/               # 所有可变数据
         ├─ cache.sqlite3
         ├─ settings.env     # API Key 等本地配置
         ├─ media_cache/
         ├─ logs/
         └─ runtime.json     # 当前实例的 PID / 端口

设计约束：

- **portable 就是数据跟着文件夹走**：绝不在目标目录不可写时悄悄回退到
  AppData / TEMP / 用户目录，而是抛出友好的 ``DataDirError``；
- 路径只依赖 exe 目录 / 资源目录 / 数据目录，**不依赖当前 shell cwd**；
- 支持 ``SIGNALLENS_DATA_DIR`` 显式覆盖（测试与高级用户）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

DATA_DIR_ENV = "SIGNALLENS_DATA_DIR"

WRITABLE_ERROR = (
    "SignalLens 当前目录不可写。\n"
    "请将整个文件夹解压到桌面、文档或其它可写目录后重新启动。\n"
    "（不要直接压缩包内运行，也不要放进 Program Files。）"
)


class DataDirError(RuntimeError):
    """数据目录不可用（不存在且无法创建，或不可写）。"""


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结的可执行文件里。"""
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def app_dir() -> Path:
    """应用根目录：frozen 下是 exe 所在目录，开发模式下是仓库根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """只读资源目录：frozen 下是 PyInstaller 解包目录，开发模式下同 app_dir。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return app_dir()


def _explicit_data_dir() -> Path | None:
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def data_dir() -> Path:
    """可变数据根目录。

    优先级：``SIGNALLENS_DATA_DIR`` > frozen 下 ``exe 同级 data/`` >
    开发模式下仓库根目录（各子路径保持历史形态）。
    """
    explicit = _explicit_data_dir()
    if explicit is not None:
        return explicit
    if is_frozen():
        return app_dir() / "data"
    return app_dir()


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = directory / ".write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def ensure_data_dir() -> Path:
    """确保数据目录存在且可写；不可写时抛出友好的 ``DataDirError``。"""
    directory = data_dir()
    if not _is_writable(directory):
        raise DataDirError(f"{WRITABLE_ERROR}\n\n数据目录：{directory}")
    return directory


# ---------------------------------------------------------------------------
# 具体子路径
# ---------------------------------------------------------------------------


def portable_style() -> bool:
    """是否使用 portable 目录布局（data/cache.sqlite3、data/settings.env …）。"""
    return _explicit_data_dir() is not None or is_frozen()


def cache_db_path() -> Path:
    """SQLite 缓存路径。

    portable：``data/cache.sqlite3``；开发模式：``.jev_cache/cache.db``（不变）。
    """
    if portable_style():
        return data_dir() / "cache.sqlite3"
    return app_dir() / ".jev_cache" / "cache.db"


def settings_env_path() -> Path:
    """本地配置文件路径：portable 下 ``data/settings.env``，开发模式下 ``.env``。"""
    if portable_style():
        return data_dir() / "settings.env"
    return app_dir() / ".env"


def dev_env_path() -> Path:
    """开发模式的仓库 ``.env``（portable release 不携带）。"""
    return app_dir() / ".env"


def media_cache_dir() -> Path:
    """富媒体本地临时缓存目录（可能含真实微信图片，禁止提交）。"""
    if portable_style():
        return data_dir() / "media_cache"
    return app_dir() / ".media_cache"


def logs_dir() -> Path:
    if portable_style():
        return data_dir() / "logs"
    return app_dir() / "logs"


def log_file_path(name: str = "startup.log") -> Path:
    return logs_dir() / name


def runtime_file_path() -> Path:
    """单实例信息文件（PID / 端口）。"""
    return data_dir() / "runtime.json"


def describe() -> dict[str, str]:
    """路径摘要（只含路径，绝不含 API Key / 聊天内容）。

    供 UI 的诊断区与启动日志使用。
    """
    return {
        "mode": "portable" if is_frozen() else "dev",
        "app_dir": str(app_dir()),
        "resource_dir": str(resource_dir()),
        "data_dir": str(data_dir()),
        "cache_db": str(cache_db_path()),
        "settings_env": str(settings_env_path()),
        "media_cache": str(media_cache_dir()),
        "logs_dir": str(logs_dir()),
        "runtime_file": str(runtime_file_path()),
    }


def temp_dir() -> Path:
    """进程级临时目录（仅用于本进程一次性文件，不用作用户数据）。"""
    path = Path(tempfile.gettempdir()) / "SignalLens-tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_runtime_dirs() -> Path:
    """确保运行时子目录存在（cache / media_cache / logs），返回数据目录。

    供应用启动时调用一次：portable 用户能直接在 ``data/`` 里看到这些目录，
    也保证后续写入不会因为目录不存在而失败。
    """
    directory = ensure_data_dir()
    media_cache_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    cache_db_path().parent.mkdir(parents=True, exist_ok=True)
    return directory
