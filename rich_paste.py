"""富媒体粘贴组件封装（Streamlit Custom Component，原生 HTML/JS）。

- 组件本体：`components/rich_paste/index.html`（无 npm / 无框架 / 无 CDN）；
- 本模块负责：声明组件、把组件返回值转成 MediaAsset、异常时安全降级；
- 图片二进制只进内存：不进 Jev state、不进 SQLite、不进报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

from media import (
    MediaAsset,
    MediaValidationError,
    SOURCE_FILE,
    decode_data_url,
    make_asset,
)
COMPONENT_DIR = Path(__file__).resolve().parent / "components" / "rich_paste"


def _component_dir() -> Path:
    """组件目录：PyInstaller frozen 下优先用 bundle 内（sys._MEIPASS）路径。

    frozen 时 ``__file__`` 指向 ``_internal``，静态资源按 spec 里的目标目录
    打包，因此先按 bundle 根目录解析；开发模式行为不变。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "components" / "rich_paste"
        if candidate.exists():
            return candidate
    return COMPONENT_DIR

_component = None
_component_failed = False
_component_error = ""


def get_component():
    """惰性声明组件；不可用时返回 None（调用方应降级到纯文本输入）。"""
    global _component, _component_failed, _component_error
    if _component_failed:
        return None
    if _component is not None:
        return _component
    try:
        from streamlit.components.v1 import declare_component

        _component = declare_component("rich_paste", path=str(_component_dir()))
    except Exception as exc:  # noqa: BLE001 — 组件不可用不能拖垮整个应用
        _component_failed = True
        _component_error = f"{type(exc).__name__}: {exc}"
        return None
    return _component


def component_available() -> bool:
    return not _component_failed


def component_error() -> str:
    """组件不可用时的原因（用于 UI 提示 / 排查）。"""
    return _component_error


def assets_from_uploader(uploaded: list) -> tuple[list[MediaAsset], list[str]]:
    """把 st.file_uploader 的文件转成 (MediaAsset 列表, 错误列表)。

    这是普通用户的图片输入 fallback：点击选择或拖拽文件，稳定可靠。
    图片只进内存，不发送给 Jev、不写 SQLite、不进报告。
    """
    assets: list[MediaAsset] = []
    errors: list[str] = []
    for f in uploaded or []:
        name = getattr(f, "name", "") or ""
        mime = getattr(f, "type", "") or ""
        if not mime:
            suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            mime = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif",
            }.get(suffix, "")
        try:
            data = f.getvalue()
        except Exception as exc:  # noqa: BLE001 — 单文件读取失败不中断整批
            errors.append(f"读取 {name or '某文件'} 失败：{type(exc).__name__}")
            continue
        try:
            assets.append(make_asset(data, mime, source=SOURCE_FILE))
        except MediaValidationError as exc:
            errors.append(f"已忽略 {name or '一张图片'}：{exc}")
    return assets, errors


def assets_from_value(value: dict | None) -> tuple[list[MediaAsset], list[str]]:
    """把组件返回值转成 (MediaAsset 列表, 人类可读的错误列表)。

    单张图片失败不影响其他图片；二进制只在返回值里解码进内存。
    """
    assets: list[MediaAsset] = []
    errors: list[str] = []
    if not value:
        return assets, errors

    for r in value.get("rejected") or []:
        size_kb = (r.get("size") or 0) / 1024
        errors.append(f"已忽略一张图片（{r.get('mime') or '未知类型'}，"
                      f"{size_kb:.0f} KB）：{r.get('reason')}")

    for img in value.get("images") or []:
        try:
            data, mime = decode_data_url(img.get("data") or "")
            asset = make_asset(
                data, mime,
                width=img.get("width"), height=img.get("height"),
                source=SOURCE_FILE if img.get("from_file") else "clipboard",
            )
        except MediaValidationError as exc:
            errors.append(f"已忽略一张图片（{img.get('mime') or '未知'}）：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — 单张解码失败不中断整批
            errors.append(f"一张图片解码失败：{type(exc).__name__}")
            continue
        assets.append(asset)
    return assets, errors
