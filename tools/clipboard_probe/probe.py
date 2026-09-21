"""Clipboard Probe：诊断 Windows + 浏览器从微信复制聊天后能拿到哪些数据。

本模块只做“格式化 / 汇总 / 导出诊断信息”，不落盘、不上传、不打印图片内容。
诊断信息导出（export_probe_diagnostics）只包含 MIME / 数量 / size / 尺寸 /
item 顺序，**不含**聊天文本、图片内容、文件路径与文件名。
"""

from __future__ import annotations

# 已知关注的剪贴板格式（用于 Probe 报告展示）
KNOWN_FORMATS = (
    "text/plain",
    "text/html",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/bmp",
    "image/gif",
)


def probe_summary(value: dict) -> dict:
    """从组件返回值提取 Probe 摘要（仅元数据）。"""
    images = value.get("images") or []
    items = value.get("items") or []
    return {
        "has_plain": bool(value.get("text_plain")),
        "has_html": bool(value.get("text_html")),
        "image_count": len(images),
        "item_count": len(items),
        "file_items": sum(1 for i in items if i.get("kind") == "file"),
        "string_items": sum(1 for i in items if i.get("kind") == "string"),
        "media_placeholders": value.get("media_placeholders", 0),
        "image_placeholders": value.get("image_placeholders", 0),
        "order_evidence": bool(value.get("order_evidence")),
        "rejected": list(value.get("rejected") or []),
        "formats": list(value.get("formats") or []),
    }


def format_probe_report(value: dict) -> str:
    """人类可读的 Probe 报告（Markdown）。"""
    if not value:
        return "尚未粘贴任何内容。"
    s = probe_summary(value)
    lines = ["**检测到：**", ""]

    lines.append(f"- text/plain：{'✅' if s['has_plain'] else '—'}")
    lines.append(f"- text/html：{'✅' if s['has_html'] else '—'}")
    lines.append(f"- 图片：{s['image_count']} 张")
    lines.append(f"- Files：{s['file_items']} 项")
    lines.append(f"- Clipboard items：{s['item_count']} 项")
    lines.append(f"- 文本内媒体占位符：{s['media_placeholders']} 个"
                 f"（其中图片占位符 {s['image_placeholders']} 个）")
    lines.append(f"- 顺序证据（图片数 == 图片占位符数）："
                 f"{'✅' if s['order_evidence'] else '—'}")
    if s["rejected"]:
        lines.append(f"- 已忽略：{len(s['rejected'])} 项")
    lines.append("")

    items = value.get("items") or []
    if items:
        lines.append("**Clipboard items 顺序：**")
        lines.append("")
        for i in items:
            detail = ""
            if i.get("kind") == "file" and i.get("type", "").startswith("image/"):
                img = next(
                    (x for x in (value.get("images") or []) if x.get("mime") == i.get("type")),
                    None,
                )
                if img:
                    dim = (f"{img['width']}×{img['height']}"
                           if img.get("width") and img.get("height") else "尺寸未知")
                    detail = f"（{dim}，{img['size'] / 1024:.0f} KB）"
            lines.append(f"- #{i.get('index')} {i.get('kind')} {i.get('type')}{detail}")
        lines.append("")

    images = value.get("images") or []
    if images:
        lines.append("**图片资产（仅元数据）：**")
        lines.append("")
        for n, img in enumerate(images, start=1):
            dim = (f"{img['width']}×{img['height']}"
                   if img.get("width") and img.get("height") else "尺寸未知")
            lines.append(
                f"- #{n} {img.get('mime')} {dim} "
                f"{img.get('size', 0) / 1024:.0f} KB sha256:{str(img.get('sha256'))[:12]}…"
            )
        lines.append("")

    if s["rejected"]:
        lines.append("**被忽略的项：**")
        lines.append("")
        for r in s["rejected"]:
            lines.append(f"- {r.get('mime')} {r.get('size', 0) / 1024:.0f} KB — {r.get('reason')}")
        lines.append("")

    return "\n".join(lines)


def export_probe_diagnostics(value: dict) -> dict:
    """导出诊断信息（可安全分享）：只有 MIME / 数量 / size / 尺寸 / 顺序。

    明确不含：聊天文本、图片内容、文件路径、文件名、任何 secret。
    """
    if not value:
        return {"captured": False}
    s = probe_summary(value)
    return {
        "captured": True,
        "formats": s["formats"],
        "items": [
            {"index": i.get("index"), "kind": i.get("kind"), "type": i.get("type")}
            for i in (value.get("items") or [])
        ],
        "images": [
            {
                "mime": img.get("mime"),
                "size": img.get("size"),
                "width": img.get("width"),
                "height": img.get("height"),
                "sha256_prefix": str(img.get("sha256"))[:12],
            }
            for img in (value.get("images") or [])
        ],
        "rejected": s["rejected"],
        "media_placeholders": s["media_placeholders"],
        "image_placeholders": s["image_placeholders"],
        "order_evidence": s["order_evidence"],
    }
