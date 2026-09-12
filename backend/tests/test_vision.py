# tests/test_vision.py
# 图片转文字的降级路径测试：不依赖网络与真实模型，下载/调用失败都必须退化成空条目而不是抛。
from app.core.vision import MAX_IMAGES, describe_images

# 失败/无信息时的统一样子：描述为空、字节为 None、mime 为空
EMPTY = {"desc": "", "data": None, "mime": ""}


class _FakeVision:
    """假多模态客户端：永远返回固定描述（真实识别能力由端到端验证覆盖）。"""

    async def describe(self, image, mime="image/jpeg"):
        return "西湖断桥的雪景"


async def test_download_failure_yields_empty():
    """图片下载不到时该项为空条目（连字节一起丢），不影响整篇入库。"""
    out = await describe_images(["http://127.0.0.1:1/none.png"], vision=_FakeVision())
    assert out == [EMPTY]


async def test_caps_image_count():
    """单页图片数超上限时只处理前 MAX_IMAGES 张（攻略页动辄几十张，全转又慢又贵）。"""
    urls = [f"http://127.0.0.1:1/{i}.png" for i in range(MAX_IMAGES + 5)]
    assert len(await describe_images(urls, vision=_FakeVision())) == MAX_IMAGES


async def test_empty_urls():
    """没有图片时不调用任何东西，直接返回空列表。"""
    assert await describe_images([], vision=_FakeVision()) == []


async def test_describe_images_returns_bytes_and_mime(monkeypatch):
    """返回结构里必须带字节与 mime：原图要一起进 Storage，重下一次就是多一轮网络往返。"""
    from app.core import vision as V

    async def fake_download(http, url):
        return b"x" * (V.MIN_IMAGE_BYTES + 1), "image/png"

    monkeypatch.setattr(V, "_download", fake_download)

    class FakeVision:
        async def describe(self, image, mime):
            return "西湖断桥"

    got = await V.describe_images(["http://a/1.png"], vision=FakeVision())
    assert got == [{"desc": "西湖断桥", "data": b"x" * (V.MIN_IMAGE_BYTES + 1), "mime": "image/png"}]


async def test_describe_images_drops_useless_image_entirely(monkeypatch):
    """模型判「无有效信息」的图连字节一起丢：它既不进正文也不该进桶。"""
    from app.core import vision as V

    async def fake_download(http, url):
        return b"x" * (V.MIN_IMAGE_BYTES + 1), "image/jpeg"

    monkeypatch.setattr(V, "_download", fake_download)

    class FakeVision:
        async def describe(self, image, mime):
            return "无有效信息"

    got = await V.describe_images(["http://a/1.jpg"], vision=FakeVision())
    assert got == [EMPTY]


async def test_download_rejects_svg(monkeypatch):
    """svg 是文本可执行的 XML，桶里存一份、将来被当图片内联回吐就有 XSS 面。"""
    import httpx
    from app.core.vision import _download

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/svg+xml"},
                              content=b"<svg/>" + b"x" * 20000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await _download(http, "http://a/1.svg") is None


async def test_download_rejects_unknown_mime(monkeypatch):
    """白名单而不是黑名单：扩展名要拿来定代理端点的 Content-Type，是 XSS 面的一部分。"""
    import httpx
    from app.core.vision import _download

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/bmp"},
                              content=b"x" * 20000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await _download(http, "http://a/1.bmp") is None
