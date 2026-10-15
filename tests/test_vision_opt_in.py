"""Image preparation and API boundary tests. All images are synthetic, all clients fake."""
from io import BytesIO
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from media import make_asset
from storage import Cache
from vision import (prepare_image, analyze_prepared, image_questions,
                    VisionValidationError)


def asset(fmt="PNG", size=(400, 400), color="red"):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format=fmt)
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "BMP": "image/bmp"}[fmt]
    return make_asset(stream.getvalue(), mime)


class Client:
    def __init__(self):
        self.calls = []

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return NS(model=kwargs["model"], answers={
            key: NS(choice=next(iter(question.criteria)), confidence=0.8)
            for key, question in kwargs["questions"].items()})


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "BMP"])
def test_normalizes_supported_local_formats(fmt):
    prepared = prepare_image(asset(fmt, (1800, 1000)))
    with Image.open(BytesIO(prepared.data)) as image:
        assert image.format == "PNG"
        assert max(image.size) <= 1568
        assert min(image.size) >= 256
        assert 78400 <= image.width * image.height <= 2458624
        assert not image.getexif()


def test_padding_small_stickers():
    prepared = prepare_image(asset(size=(64, 64)))
    assert prepared.width == prepared.height == 280


def test_redaction_applied_before_crop_and_hash_changes():
    source = asset()
    original = prepare_image(source)
    masked = prepare_image(source, redactions=[(0, 0, .5, .5)])
    cropped = prepare_image(source, crop=(.5, .5, 1, 1))
    assert len({original.sha256, masked.sha256, cropped.sha256}) == 3
    with Image.open(BytesIO(masked.data)) as image:
        assert image.getpixel((20, 20)) == (0, 0, 0)
        assert image.getpixel((350, 350)) == (255, 0, 0)


def test_rejects_corrupt_animation_long_screenshot_and_invalid_rectangles():
    source = asset(size=(300, 3000))
    with pytest.raises(VisionValidationError, match="过长"):
        prepare_image(source)
    assert prepare_image(source, crop=(0, 0, 1, .15))
    source.data = b"not an image"
    with pytest.raises(VisionValidationError):
        prepare_image(source)
    with pytest.raises(VisionValidationError):
        prepare_image(asset(), crop=(.8, 0, .1, 1))
    stream = BytesIO()
    Image.new("RGB", (300, 300), "red").save(stream, format="PNG", save_all=True,
        append_images=[Image.new("RGB", (300, 300), "blue")], duration=100, loop=0)
    with pytest.raises(VisionValidationError, match="动画"):
        prepare_image(make_asset(stream.getvalue(), "image/png"))


def test_pixel_budget_is_checked(monkeypatch):
    monkeypatch.setattr("vision.MAX_SOURCE_PIXELS", 100)
    with pytest.raises(VisionValidationError, match="像素"):
        prepare_image(asset())


def test_no_upload_without_confirmation():
    client = Client()
    with pytest.raises(VisionValidationError, match="确认"):
        analyze_prepared(client, prepare_image(asset()), [], model="jev-1.13.0")
    assert client.calls == []


def test_upload_uses_approved_pixels_redacted_context_and_experimental_header(tmp_path):
    client = Client()
    image = prepare_image(asset(), redactions=[(0, 0, .5, .5)])
    context = [{"speaker": "me", "text": "邮箱 demo@example.com", "raw_speaker": "原昵称", "fingerprint": "private"}]
    cache = Cache(tmp_path / "cache.db")
    result = analyze_prepared(client, image, context, model="jev-1.13.0", approved=True, cache=cache)
    call = client.calls[0]
    assert call["extra_headers"] == {"TypeSafe-Danger-Zone-Images": "true"}
    assert "demo@example.com" not in str(call["state"]["conversation_context"])
    assert "原昵称" not in str(call["state"]) and "fingerprint" not in str(call["state"])
    assert result["review_required"] and result["model"] == "jev-1.13.0"
    again = analyze_prepared(client, image, context, model="jev-1.13.0", approved=True, cache=cache)
    assert again["cached"] and len(client.calls) == 1
    analyze_prepared(client, prepare_image(asset(color="blue")), context,
                     model="jev-1.13.0", approved=True, cache=cache)
    analyze_prepared(client, image, context, model="jev-1.14.0", approved=True, cache=cache)
    assert len(client.calls) == 3
    import json
    assert "data" not in json.dumps(result)
    assert image.data[:20] not in (tmp_path / "cache.db").read_bytes()


def test_model_mismatch_and_invalid_choice_are_not_cached():
    class Wrong(Client):
        def system_one(self, **kwargs):
            result = super().system_one(**kwargs)
            result.model = "jev-1.14.0"
            return result
    with pytest.raises(VisionValidationError, match="版本"):
        analyze_prepared(Wrong(), prepare_image(asset()), [], model="jev-1.13.0", approved=True)


def test_metadata_is_not_copied_into_processed_pixels():
    from PIL.PngImagePlugin import PngInfo
    stream = BytesIO()
    metadata = PngInfo()
    metadata.add_text("Comment", "private-person-name")
    original = Image.new("RGB", (300, 300), "white")
    exif = Image.Exif()
    exif[315] = "private-person-name"
    original.save(stream, format="PNG", pnginfo=metadata, exif=exif)
    image = prepare_image(make_asset(stream.getvalue(), "image/png"))
    assert b"private-person-name" not in image.data
    with Image.open(BytesIO(image.data)) as clean:
        assert not clean.getexif() and not clean.info


def test_transparent_pixels_are_composited_instead_of_revealing_hidden_rgb():
    stream = BytesIO()
    source = Image.new("RGBA", (300, 300), (255, 0, 0, 0))
    source.putpixel((150, 150), (255, 0, 0, 128))
    source.save(stream, format="PNG")
    prepared = prepare_image(make_asset(stream.getvalue(), "image/png"))
    with Image.open(BytesIO(prepared.data)) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((150, 150)) == (255, 127, 127)
