"""本地配置读写（API Key 等）。

优先级（与 python-dotenv 的“不覆盖已有环境变量”语义一致）：

1. **当前进程环境变量**——已存在的永远优先，不会被文件覆盖；
2. ``SIGNALLENS_DATA_DIR/settings.env``（portable 模式下即 ``data/settings.env``）；
3. 开发模式仓库根目录的 ``.env``（portable release 不携带）。

安全约束：

- 只有 ``TYPESAFE_API_KEY`` 一个键由 UI 管理；
- 写入时尽量收紧文件权限（Windows 上尽力而为）；
- **任何函数都不返回、不打印、不记录 key 本身**——只返回“已配置/未配置”
  之类的布尔状态；需要展示时也只说明状态，不回显。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv, set_key

import paths
from paths import DataDirError, dev_env_path, ensure_data_dir, settings_env_path

API_KEY_ENV = "TYPESAFE_API_KEY"
SETTINGS_KEYS = (API_KEY_ENV,)


def load_settings() -> None:
    """按优先级加载本地配置到进程环境（不覆盖已有环境变量）。

    在开发模式下若两者是同一个文件（仓库 ``.env``），只加载一次。
    """
    data_settings = settings_env_path()
    if data_settings.exists():
        load_dotenv(dotenv_path=str(data_settings), override=False)

    # 开发模式（未指定 data 目录且非 frozen）才兼音仓库根目录的
    # .env；portable / release 模式不读它，行为可预期（release 本身也不打包 .env）。
    if not paths.portable_style():
        dev_env = dev_env_path()
        if dev_env.exists() and dev_env.resolve() != data_settings.resolve():
            load_dotenv(dotenv_path=str(dev_env), override=False)


def api_key() -> str:
    """当前生效的 API Key（空串 = 未配置）。绝不记录、绝不外泄。"""
    return os.environ.get(API_KEY_ENV, "").strip()


def has_api_key() -> bool:
    return bool(api_key())


def save_api_key(key: str) -> Path:
    """把 API Key 写入本地配置文件（portable: data/settings.env）。

    返回写入的文件路径。**不返回 key、不打印 key。**
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("API Key 不能为空。")
    ensure_data_dir()
    path = settings_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # set_key 会保留文件里已有的其它键
    set_key(str(path), API_KEY_ENV, key, quote_mode="never")
    _tighten_permissions(path)
    # 立即对当前进程生效（无需重启）
    os.environ[API_KEY_ENV] = key
    return path


def clear_api_key() -> Path:
    """清除本地保存的 API Key（只删配置，不碰聊天 / 缓存 / 媒体）。"""
    os.environ.pop(API_KEY_ENV, None)
    path = settings_env_path()
    if not path.exists():
        return path
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith(f"{API_KEY_ENV}=") or stripped.startswith(
            f"export {API_KEY_ENV}="
        ):
            continue
        if stripped:
            lines.append(raw)
    if lines:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _tighten_permissions(path)
    else:
        path.unlink(missing_ok=True)
    return path


def settings_summary() -> dict:
    """配置状态摘要（**不含 key 本身**），供 UI 展示与测试断言。"""
    path = settings_env_path()
    configured = has_api_key()
    return {
        "configured": configured,
        "settings_path": str(path),
        "source": (
            "process_env" if configured and not os.environ.get("_SL_ENV_FROM_FILE")
            else ("settings_file" if path.exists() else "none")
        ),
    }


def _tighten_permissions(path: Path) -> None:
    """尽量把配置文件限制为仅当前用户可读写（Windows 上尽力而为）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_data_dir_error(exc: BaseException) -> bool:
    return isinstance(exc, DataDirError)
