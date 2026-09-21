"""视觉识别接口预留（SignalLens v0.2.0 第一阶段：**不实现**）。

本阶段目标只是“获得图片”与“绑定占位符”，不做任何图片语义识别。
接口先占位，下一阶段再决定供应商；当前默认禁用：

- 不接入 OpenAI Vision / Gemini Vision / Qwen-VL / Claude Vision / 本地模型；
- 不 OCR、不抽帧、不 ASR；
- 不上传图片到任何第三方。

Jev 分析看到的仍然只有中性 marker：
    [发送了一张图片，内容未知]
"""

from __future__ import annotations

from media import MediaAsset

VISION_STATUS_DISABLED = "尚未启用"
VISION_STATUS_READY = "可用"


class VisionAnalyzer:
    """图片语义分析器接口（第一阶段：禁用）。

    子类化或替换本类以实现真正的识别；实现前必须显式设置 enabled=True。
    """

    enabled: bool = False
    status: str = VISION_STATUS_DISABLED

    def analyze_image(self, asset: MediaAsset) -> dict:
        """分析单张图片，返回结构化描述。

        第一阶段抛出 NotImplementedError——调用方必须处理禁用状态。
        """
        raise NotImplementedError(
            "视觉识别尚未启用：SignalLens v0.2.0 第一阶段只获取图片，不解释内容。"
        )


# 全局单例（默认禁用）
vision_analyzer = VisionAnalyzer()


def analyze_media_asset(asset: MediaAsset) -> dict | None:
    """分析媒体资产；未启用时返回 None（绝不抛给 UI）。"""
    if not vision_analyzer.enabled:
        return None
    return vision_analyzer.analyze_image(asset)


def vision_status() -> str:
    return vision_analyzer.status
