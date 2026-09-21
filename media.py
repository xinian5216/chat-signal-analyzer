"""媒体资产与 `[图片]` 占位符绑定（SignalLens v0.2.0 第一阶段）。

本模块只负责“**获得图片**”与“**把图片可靠绑定到占位符**”，
绝不解释图片内容（视觉识别属于后续阶段，见 vision.py）。

硬约束：
- 图片二进制**绝不进入** Jev state / SQLite 缓存 / Markdown·JSON 报告；
- 纯文本路径的缓存 key 不受影响（Jev state 仍只含中性 marker）；
- 绑定原则保守：无法证明顺序时拒绝自动绑定，交予用户手动匹配。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 资源限制（集中配置）
# ---------------------------------------------------------------------------
MAX_IMAGE_BYTES = 10 * 1024 * 1024        # 单图最大 10 MB
MAX_IMAGES_PER_PASTE = 20                 # 单次粘贴最多 20 张
MAX_TOTAL_BYTES = 50 * 1024 * 1024        # 单次粘贴总量最大 50 MB

# 第一阶段允许的静态图片类型（GIF 仅作为 asset 保存，不分析动画内容）
ALLOWED_IMAGE_MIME = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/bmp",
)
GIF_MIME = "image/gif"

KIND_IMAGE = "image"
KIND_STICKER = "sticker"
KIND_VIDEO = "video"

SOURCE_CLIPBOARD = "clipboard"
SOURCE_FILE = "file"


class MediaValidationError(ValueError):
    """图片超出限制或类型不支持。"""


@dataclass
class MediaAsset:
    """一张已捕获的静态图片资产（仅存本机内存 / 本地临时缓存）。"""

    id: str
    kind: str = KIND_IMAGE
    mime_type: str = ""
    sha256: str = ""
    width: int | None = None
    height: int | None = None
    size: int = 0
    source: str = SOURCE_CLIPBOARD
    data: bytes | None = None          # 内存中的二进制（绝不持久化到缓存/报告）
    path: Path | None = None           # 可选：.media_cache/ 临时路径
    analyzed: bool = False             # 视觉识别（后续阶段）是否已执行

    def metadata(self) -> dict:
        """可安全外泄的元数据（不含二进制）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "size": self.size,
            "source": self.source,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    """把 `data:image/png;base64,....` 解成 (bytes, mime)。"""
    if not data_url.startswith("data:"):
        raise MediaValidationError("不是 data URL")
    header, _, payload = data_url.partition(",")
    mime = header[5:].split(";")[0] or "application/octet-stream"
    return base64.b64decode(payload), mime


def validate_image(mime: str, size: int) -> None:
    """类型 / 大小校验；超限抛出 MediaValidationError（UI 友好提示）。"""
    if mime not in ALLOWED_IMAGE_MIME and mime != GIF_MIME:
        raise MediaValidationError(
            f"不支持的图片类型 {mime or '(未知)'}；第一阶段仅支持 "
            + " / ".join(ALLOWED_IMAGE_MIME)
        )
    if size <= 0:
        raise MediaValidationError("图片大小为 0，已忽略。")
    if size > MAX_IMAGE_BYTES:
        raise MediaValidationError(
            f"图片过大（{size / 1024 / 1024:.1f} MB > "
            f"{MAX_IMAGE_BYTES / 1024 / 1024:.0f} MB 上限），已忽略。"
        )


def make_asset(data: bytes, mime: str, width: int | None = None,
               height: int | None = None,
               source: str = SOURCE_CLIPBOARD) -> MediaAsset:
    """从二进制构造 MediaAsset（含校验与 hash）。"""
    validate_image(mime, len(data))
    digest = sha256_bytes(data)
    return MediaAsset(
        id=digest[:16],
        kind=KIND_IMAGE,
        mime_type=mime,
        sha256=digest,
        width=width,
        height=height,
        size=len(data),
        source=source,
        data=data,
    )


def dedupe_assets(assets: list[MediaAsset]) -> list[MediaAsset]:
    """按 SHA256 去重（保留首次出现顺序）。

    Hash 只用于去重 / 资产标识，不用于推断语义归属。
    """
    seen: set[str] = set()
    out: list[MediaAsset] = []
    for a in assets:
        if a.sha256 in seen:
            continue
        seen.add(a.sha256)
        out.append(a)
    return out


def validate_paste_batch(assets: list[MediaAsset]) -> list[str]:
    """整批粘贴的数量 / 总量校验，返回人类可读的错误列表（空 = 通过）。"""
    errors: list[str] = []
    if len(assets) > MAX_IMAGES_PER_PASTE:
        errors.append(
            f"单次粘贴图片过多（{len(assets)} > {MAX_IMAGES_PER_PASTE} 张上限）。"
        )
    total = sum(a.size for a in assets)
    if total > MAX_TOTAL_BYTES:
        errors.append(
            f"单次粘贴总量过大（{total / 1024 / 1024:.1f} MB > "
            f"{MAX_TOTAL_BYTES / 1024 / 1024:.0f} MB 上限）。"
        )
    return errors


# ---------------------------------------------------------------------------
# 占位符 → 图片 绑定
# ---------------------------------------------------------------------------


def image_placeholder_messages(messages: list[dict]) -> list[int]:
    """返回“含有图片占位符”的消息下标（按消息顺序）。

    包含 content_type == "media"（纯图片）与 "mixed"（文字+图片）两种。
    同一消息内的多张图片按 1 个占位符计数（保守：不会过度自动绑定）。
    """
    return [
        i for i, m in enumerate(messages)
        if KIND_IMAGE in (m.get("media_kinds") or [])
    ]


@dataclass
class BindingResult:
    """占位符与图片的绑定结果。"""

    auto: dict[int, str] = field(default_factory=dict)      # message_index -> asset_id
    unbound_messages: list[int] = field(default_factory=list)
    unmatched_assets: list[str] = field(default_factory=list)  # asset_id
    reason: str = ""

    @property
    def needs_manual(self) -> bool:
        return bool(self.unbound_messages) and bool(self.unmatched_assets)


def bind_media(messages: list[dict], assets: list[MediaAsset],
               order_verified: bool = False) -> BindingResult:
    """把 clipboard 图片绑定到 `[图片]` 占位符（保守策略）。

    仅在两种高可信情况自动绑定：

    - CASE 1：占位符消息 1 条 + 图片 1 张 → 100% 绑定；
    - CASE 2：占位符消息 N 条 + 图片 N 张，**且顺序已被 Probe 实测证明**
      （order_verified=True）→ 按顺序绑定。

    其他情况（数量不匹配、顺序未证明、多图同条消息）一律不猜，
    返回未绑定项交由用户手动匹配。
    """
    result = BindingResult()
    placeholders = image_placeholder_messages(messages)
    assets = dedupe_assets(assets)

    if not placeholders or not assets:
        result.unbound_messages = list(placeholders)
        result.unmatched_assets = [a.id for a in assets]
        if not placeholders and not assets:
            result.reason = ""
        elif not assets:
            result.reason = "检测到图片占位符，但没有捕获到图片。"
        else:
            result.reason = "捕获到图片，但文本中没有图片占位符。"
        return result

    if len(placeholders) == 1 and len(assets) == 1:
        result.auto[placeholders[0]] = assets[0].id
        result.reason = "单一占位符 + 单一图片，已自动绑定。"
        return result

    if len(placeholders) == len(assets) and order_verified:
        for idx, asset in zip(placeholders, assets):
            result.auto[idx] = asset.id
        result.reason = (f"{len(placeholders)} 个占位符与 {len(assets)} 张图片数量一致，"
                         "且剪贴板顺序已经实测验证，按顺序自动绑定。")
        return result

    # 拒绝自动绑定
    result.unbound_messages = list(placeholders)
    result.unmatched_assets = [a.id for a in assets]
    if len(placeholders) != len(assets):
        result.reason = (
            f"检测到 {len(placeholders)} 个图片占位符、{len(assets)} 张图片，"
            "数量不一致，无法自动对应。"
        )
    else:
        result.reason = (
            f"检测到 {len(placeholders)} 个图片占位符、{len(assets)} 张图片，"
            "但剪贴板顺序未经实测验证，自动对应关系不确定。"
        )
    return result
