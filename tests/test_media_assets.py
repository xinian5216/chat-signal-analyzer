"""v0.2.0 第一阶段：媒体资产 / 占位符绑定 / 隐私边界测试。

全部使用程序生成的合成图片数据，绝不调用真实 Jev API，绝不使用真实微信图片。
"""

import base64
import json

import pytest

import analyzer
from media import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_PASTE,
    MediaAsset,
    MediaValidationError,
    SOURCE_FILE,
    ALLOWED_IMAGE_MIME,
    GIF_MIME,
    KIND_IMAGE,
    KIND_STICKER,
    KIND_VIDEO,
    bind_media,
    decode_data_url,
    dedupe_assets,
    image_placeholder_messages,
    make_asset,
    sha256_bytes,
    validate_image,
    validate_paste_batch,
)
from parser import parse_chat
from privacy import mask_messages
from report import build_json_report, build_markdown_report
from rich_paste import assets_from_value
from scoring import compute_conversation_stats
from storage import Cache, make_cache_key

# 1x1 PNG（程序生成的标准最小 PNG）
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def synthetic(mime: str, n: int = 8) -> bytes:
    """合成图片字节（非真实图片，仅用于资产结构 / 校验测试）。"""
    return bytes([0x89, 0x50, 0x4E, 0x47]) + mime.encode() + bytes(range(n))


def asset(mime="image/png", n=1, size=None, w=100, h=80):
    """合成图片资产：同类型不同 n → 不同字节（避免被去重误伤）。"""
    data = PNG_1X1 + bytes([n]) if mime == "image/png" else synthetic(mime, n)
    if size:
        data = data + b"\x00" * max(0, size - len(data))
    return make_asset(data, mime, width=w, height=h)


CHAT_PLAIN = "我: 在忙吗\nTA: 在啊"
CHAT_ONE_IMAGE = "我: 在忙吗\nTA: [图片] 微信图片_1.dat"
CHAT_TWO_IMAGES = ("我: 在忙吗\nTA: [图片] 微信图片_1.dat\n"
                   "我: 然后呢\nTA: [图片] 微信图片_2.dat")
CHAT_MIXED = "我: 你看这个\nTA: 你看这个 [图片] 微信图片_1.dat"
CHAT_STICKER = "我: 在忙吗\nTA: [动画表情]"
CHAT_VIDEO = "我: 在忙吗\nTA: [视频] 微信视频_1.mp4"
CHAT_EMOJI = "我: 在忙吗\nTA: 哈哈😂你又来了❤️"


def parse(chat):
    return mask_messages(parse_chat(chat))


# ---------------------------------------------------------------------------
# 1 / 13 / 14：纯文本与混合
# ---------------------------------------------------------------------------

def test_plain_text_only_no_bindings():
    msgs = parse(CHAT_PLAIN)
    assert image_placeholder_messages(msgs) == []
    result = bind_media(msgs, [asset()])
    assert result.auto == {}
    assert result.unmatched_assets  # 有图但无占位符 → 不猜


def test_mixed_text_and_image_placeholder():
    msgs = parse(CHAT_MIXED)
    placeholders = image_placeholder_messages(msgs)
    assert placeholders == [1]
    assert msgs[1]["content_type"] == "mixed"
    assert "你看这个" in msgs[1]["text"]
    a = asset()
    result = bind_media(msgs, [a])
    assert result.auto == {1: a.id}


# ---------------------------------------------------------------------------
# 2 / 3 / 4：绑定策略
# ---------------------------------------------------------------------------

def test_single_placeholder_single_image_auto_binds():
    msgs = parse(CHAT_ONE_IMAGE)
    a = asset()
    result = bind_media(msgs, [a])
    assert result.auto == {1: a.id}
    assert result.reason
    assert not result.unbound_messages


def test_two_placeholders_two_images_without_order_proof_no_auto_bind():
    msgs = parse(CHAT_TWO_IMAGES)
    a, b = asset("image/png", 1), asset("image/png", 2)
    result = bind_media(msgs, [a, b], order_verified=False)
    assert result.auto == {}
    assert result.unbound_messages == [1, 3]
    assert "顺序未经实测验证" in result.reason or "不确定" in result.reason


def test_two_placeholders_two_images_with_order_proof_binds_in_order():
    msgs = parse(CHAT_TWO_IMAGES)
    a, b = asset("image/png", 1), asset("image/png", 2)
    result = bind_media(msgs, [a, b], order_verified=True)
    assert result.auto == {1: a.id, 3: b.id}


def test_count_mismatch_never_auto_binds():
    msgs = parse(CHAT_ONE_IMAGE)
    a, b = asset("image/png", 1), asset("image/png", 2)
    result = bind_media(msgs, [a, b], order_verified=True)
    assert result.auto == {}
    assert "数量不一致" in result.reason


def test_no_assets_no_bindings():
    msgs = parse(CHAT_ONE_IMAGE)
    result = bind_media(msgs, [])
    assert result.auto == {}
    assert result.unbound_messages == [1]


# ---------------------------------------------------------------------------
# 5-8：静态图片类型
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mime", ALLOWED_IMAGE_MIME)
def test_allowed_static_image_types(mime):
    a = asset(mime)
    assert a.mime_type == mime and a.kind == KIND_IMAGE and a.size > 0


def test_gif_accepted_as_asset_only():
    a = asset(GIF_MIME)
    assert a.mime_type == GIF_MIME  # 只作为 asset，不分析动画内容


def test_unsupported_type_rejected():
    with pytest.raises(MediaValidationError):
        make_asset(b"xxxx", "video/mp4")


# ---------------------------------------------------------------------------
# 9 / 10 / 11：emoji / 动画表情 / 视频
# ---------------------------------------------------------------------------

def test_real_emoji_not_media():
    msgs = parse(CHAT_EMOJI)
    assert all(m["content_type"] == "text" for m in msgs)
    assert "😂" in msgs[1]["text"] and "❤️" in msgs[1]["text"]
    assert image_placeholder_messages(msgs) == []


def test_animation_placeholder_not_static_image():
    msgs = parse(CHAT_STICKER)
    assert msgs[1]["content_type"] == "media"
    assert msgs[1]["media_kinds"] == [KIND_STICKER]
    assert image_placeholder_messages(msgs) == []      # 不当静态图片
    result = bind_media(msgs, [asset()])               # 有图也不绑到表情上
    assert result.auto == {}


def test_video_placeholder_untouched():
    msgs = parse(CHAT_VIDEO)
    assert msgs[1]["media_kinds"] == [KIND_VIDEO]
    assert image_placeholder_messages(msgs) == []
    result = bind_media(msgs, [asset()])
    assert result.auto == {}


# ---------------------------------------------------------------------------
# 12：hash 去重
# ---------------------------------------------------------------------------

def test_hash_dedupe():
    a, b, c = asset("image/png", 1), asset("image/png", 1), asset("image/png", 2)
    assert a.sha256 == b.sha256 != c.sha256
    deduped = dedupe_assets([a, b, c])
    assert [x.id for x in deduped] == [a.id, c.id]


def test_same_hash_dedupe_in_binding():
    msgs = parse(CHAT_TWO_IMAGES)
    a, b = asset("image/png", 1), asset("image/png", 1)  # 同一张图
    result = bind_media(msgs, [a, b], order_verified=True)
    # 去重后只剩 1 张 → 数量不匹配 → 不自动绑定（保守）
    assert result.auto == {}


# ---------------------------------------------------------------------------
# 14 / 15 / 26：Jev state 与缓存 key 不受影响
# ---------------------------------------------------------------------------

def test_media_fields_never_enter_jev_state():
    msgs = parse(CHAT_ONE_IMAGE)
    m = msgs[1]
    state = analyzer.build_state(
        [{"speaker": "me", "text": "在忙吗", "time": None}],
        {"speaker": "them", "text": m["text"], "time": m.get("time"),
         "raw_speaker": m.get("raw_speaker"),
         "content_type": m["content_type"], "media_kinds": m["media_kinds"],
         "media_asset_ids": ["deadbeef"]},
    )
    assert set(state["target_message"]) == {"speaker", "text", "time"}
    # 即使调用方误传本地 metadata，build_state 也会按白名单剥离
    for key in ("raw_speaker", "content_type", "media_kinds", "media_asset_ids"):
        assert key not in state["target_message"]
    # 真实路径：app 只投影必要字段
    projected = {"speaker": "them", "text": m["text"], "time": m.get("time"),
                 "raw_speaker": m.get("raw_speaker")}
    state2 = analyzer.build_state(
        [{"speaker": "me", "text": "在忙吗", "time": None}], projected)
    assert set(state2["target_message"]) == {"speaker", "text", "time"}
    # 白名单 + v2.2：raw_speaker 不进入 state；版本号随 Context Builder v2
    # （上下文选择语义）与出站白名单（本地 metadata 剥离）变更而 bump，
    # 问题 schema 本身与 v2.1 完全一致
    assert analyzer.SCHEMA_VERSION == "chat-signal-v2.2"


def test_pure_text_cache_key_unchanged():
    """纯文本缓存 key 稳定，媒体资产与原始昵称均不参与。"""
    msgs = parse(CHAT_PLAIN)
    ctx = [{"speaker": c["speaker"], "text": c["text"], "time": c.get("time")}
           for c in msgs[:1]]
    state = analyzer.build_state(ctx, {"speaker": "them", "text": "在啊",
                                       "time": None, "raw_speaker": "TA"})
    key = make_cache_key(state, analyzer.build_questions_schema(),
                         analyzer.DEFAULT_MODEL, analyzer.SCHEMA_VERSION)
    assert len(key) == 64


def test_media_asset_not_json_serializable_for_cache():
    """MediaAsset 含二进制，无法进入 JSON 化缓存（结构上防止误存）。"""
    a = asset()
    with pytest.raises(TypeError):
        json.dumps({"asset": a})
    # 元数据可安全序列化（不含二进制）
    assert "data" not in json.dumps(a.metadata())


def test_media_binary_not_in_sqlite_cache(tmp_path):
    cache = Cache(tmp_path / "c.db")
    key = "k" * 64
    payload = {"emotion": {"choice": "calm"}, "marker": "[发送了一张图片，内容未知]"}
    cache.set(key, payload)
    assert cache.get(key) == payload
    conn = cache._connect()
    try:
        raw = conn.execute("SELECT result FROM analysis_cache").fetchone()[0]
    finally:
        conn.close()
    cache.close()
    assert PNG_1X1[:8] not in raw.encode("utf-8", errors="ignore")
    assert "base64" not in raw


# ---------------------------------------------------------------------------
# 17 / 18：报告不含二进制与文件名
# ---------------------------------------------------------------------------

def test_report_excludes_image_binary_and_filenames():
    msgs = parse(CHAT_ONE_IMAGE)
    a = asset()
    bind_media(msgs, [a])
    results = []
    for i, m in enumerate(msgs):
        if m["speaker"] != "them":
            continue
        results.append({"index": i, "speaker": "them", "text": m["text"],
                        "time": None, "context": [], "cached": False,
                        "result": _fake_result()})
    stats = compute_conversation_stats(results)
    md = build_markdown_report(results, stats, include_text=True)
    data = build_json_report(results, stats, include_text=True)
    blob = md + json.dumps(data, ensure_ascii=False)
    assert "微信图片" not in blob and ".dat" not in blob
    assert base64.b64encode(PNG_1X1).decode()[:24] not in blob
    assert "data:image" not in blob


def _fake_result():
    from types import SimpleNamespace as NS

    class A:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    return analyzer.extract_answers(NS(answers={
        "emotion": A(choice="calm", probabilities={"calm": 1.0}, confidence=0.9),
        "intent": A(choice="other", probabilities={"other": 1.0}, confidence=0.9),
        "warmth": A(score=2.0, probabilities={}, confidence=0.9),
        "engagement": A(score=2.0, probabilities={}, confidence=0.9),
        "special_attention": A(score=1.0, probabilities={}, confidence=0.9),
        "relationship_evidence_strength": A(score=2.0, probabilities={}, confidence=0.9),
        "relational_ease": A(score=2.0, probabilities={}, confidence=0.9),
        "romantic_signal": A(noul=0.1),
        "distancing_signal": A(noul=0.1),
    }, model="fake"))


# ---------------------------------------------------------------------------
# 19：组件返回异常 → 安全降级
# ---------------------------------------------------------------------------

def test_assets_from_value_handles_garbage():
    assets, errors = assets_from_value(None)
    assert assets == [] and errors == []

    assets, errors = assets_from_value({"images": "not-a-list"})
    assert assets == []

    assets, errors = assets_from_value({"images": [{"data": "not-a-data-url"}]})
    assert assets == [] and len(errors) == 1

    assets, errors = assets_from_value({
        "images": [{"data": "data:video/mp4;base64,AAAA"}],
        "rejected": [{"mime": "image/png", "size": 999, "reason": "测试"}],
    })
    assert assets == [] and len(errors) == 2


def test_assets_from_value_happy_path():
    b64 = base64.b64encode(PNG_1X1).decode()
    assets, errors = assets_from_value({
        "images": [{"data": f"data:image/png;base64,{b64}", "width": 1,
                    "height": 1, "size": len(PNG_1X1)}]
    })
    assert len(assets) == 1 and errors == []
    assert assets[0].mime_type == "image/png"
    assert assets[0].sha256 == sha256_bytes(PNG_1X1)


# ---------------------------------------------------------------------------
# 20：超限拒绝
# ---------------------------------------------------------------------------

def test_oversized_image_rejected():
    with pytest.raises(MediaValidationError) as exc:
        make_asset(b"\x89PNG" + b"\x00" * (MAX_IMAGE_BYTES + 10), "image/png")
    assert "过大" in str(exc.value)


def test_paste_batch_limits():
    many = [asset("image/png", i) for i in range(MAX_IMAGES_PER_PASTE + 1)]
    errors = validate_paste_batch(many)
    assert errors and "过多" in errors[0]

    huge = [asset("image/png", 1, size=MAX_IMAGE_BYTES)]
    errors = validate_paste_batch(huge * 6)
    assert errors and "总量" in errors[0]


def test_validate_image_zero_and_bad_mime():
    with pytest.raises(MediaValidationError):
        validate_image("image/png", 0)
    with pytest.raises(MediaValidationError):
        validate_image("application/zip", 100)


def test_decode_data_url_roundtrip():
    b64 = base64.b64encode(PNG_1X1).decode()
    data, mime = decode_data_url(f"data:image/png;base64,{b64}")
    assert data == PNG_1X1 and mime == "image/png"


def test_asset_metadata_shape():
    a = asset()
    meta = a.metadata()
    assert set(meta) == {"id", "kind", "mime_type", "sha256", "width",
                         "height", "size", "source"}
    assert meta["sha256"] == a.sha256


# ---------------------------------------------------------------------------
# file_uploader fallback：上传文件 → MediaAsset
# ---------------------------------------------------------------------------


class FakeUpload:
    def __init__(self, data, name, type_):
        self._data = data
        self.name = name
        self.type = type_

    def getvalue(self):
        return self._data


def test_uploader_happy_path():
    from rich_paste import assets_from_uploader

    files = [
        FakeUpload(PNG_1X1 + b"\x01", "a.png", "image/png"),
        FakeUpload(PNG_1X1 + b"\x02", "b.png", ""),          # 无 type，靠扩展名
    ]
    assets, errors = assets_from_uploader(files)
    assert len(assets) == 2 and errors == []
    assert assets[0].source == SOURCE_FILE
    assert assets[1].mime_type == "image/png"


def test_uploader_rejects_bad_type_and_oversize():
    from rich_paste import assets_from_uploader

    files = [
        FakeUpload(b"xxxx", "a.zip", "application/zip"),
        FakeUpload(b"\x89PNG" + b"\x00" * (MAX_IMAGE_BYTES + 10), "big.png",
                   "image/png"),
    ]
    assets, errors = assets_from_uploader(files)
    assert assets == [] and len(errors) == 2


def test_uploader_read_failure_does_not_abort_batch():
    from rich_paste import assets_from_uploader

    class Broken(FakeUpload):
        def getvalue(self):
            raise OSError("boom")

    files = [Broken(b"", "x.png", "image/png"),
             FakeUpload(PNG_1X1, "ok.png", "image/png")]
    assets, errors = assets_from_uploader(files)
    assert len(assets) == 1 and len(errors) == 1
