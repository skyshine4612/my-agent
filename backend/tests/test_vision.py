# tests/test_vision.py
# 图片转文字的降级路径测试：不依赖网络与真实模型，下载/调用失败都必须退化成空条目而不是抛。
from types import SimpleNamespace

from app.core.knowledge.vision import MAX_IMAGES, describe_images

# 失败/无信息时的统一样子：描述为空、字节为 None、mime 为空
EMPTY = {"desc": "", "data": None, "mime": ""}


class _RecordingCompletions:
    """假 completions：记下收到的请求参数，返回一个最小可用的响应对象。"""

    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="西湖断桥"))])


async def test_describe_pins_temperature_to_zero():
    """判定必须确定：实测同一份图片字节连调 3 次，「无有效信息」的结论会摇摆。

    那不是模型能力问题，是采样——同一张栖息架上的小熊猫图三次里有一次判成装饰图，而判成
    装饰图会连字节一起丢（不进正文、不进桶），于是「同一页面抓两次图片不一样」。这个参数
    删掉不报错、只是慢慢失效，所以拿测试钉住。
    """
    from app.core.knowledge.vision import OpenAICompatVision

    vision = OpenAICompatVision(base_url="http://example.invalid", api_key="k", model="m")
    recorder = _RecordingCompletions()
    vision.client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))

    assert await vision.describe(b"ximage", "image/jpeg") == "西湖断桥"
    assert recorder.kwargs["temperature"] == 0


async def test_describe_passes_page_title_into_the_prompt():
    """页面标题是判定的关键依据：实测同一份图片字节，不带标题时熊猫照片 5 次里会摇摆
    （丢留丢丢丢），带上标题后 5 次全稳定保留 —— 「这张图对页面主题有没有用」只有知道
    页面讲什么才判得出来。"""
    from app.core.knowledge.vision import OpenAICompatVision

    vision = OpenAICompatVision(base_url="http://example.invalid", api_key="k", model="m")
    recorder = _RecordingCompletions()
    vision.client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))

    await vision.describe(b"ximage", "image/jpeg", context="成都大熊猫繁育研究基地游玩攻略")
    text = recorder.kwargs["messages"][0]["content"][0]["text"]
    assert "成都大熊猫繁育研究基地游玩攻略" in text
    assert "{title}" not in text


def test_vision_prompt_lives_in_prompts_dir():
    """提示词不写在代码里：改文案不该动 Python（system / grounding / critic 都这么放）。"""
    from app.core.prompts import load_prompt
    assert "{title}" in load_prompt("vision")


class _FakeVision:
    """假多模态客户端：永远返回固定描述（真实识别能力由端到端验证覆盖）。"""

    async def describe(self, image, mime="image/jpeg", context=""):
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
    from app.core.knowledge import vision as V

    async def fake_download(http, url):
        return b"x" * (V.MIN_IMAGE_BYTES + 1), "image/png"

    monkeypatch.setattr(V, "_download", fake_download)

    class FakeVision:
        async def describe(self, image, mime, context=""):
            return "西湖断桥"

    got = await V.describe_images(["http://a/1.png"], vision=FakeVision())
    assert got == [{"desc": "西湖断桥", "data": b"x" * (V.MIN_IMAGE_BYTES + 1), "mime": "image/png"}]


async def test_describe_images_drops_useless_image_entirely(monkeypatch):
    """模型判「无有效信息」的图连字节一起丢：它既不进正文也不该进桶。"""
    from app.core.knowledge import vision as V

    async def fake_download(http, url):
        return b"x" * (V.MIN_IMAGE_BYTES + 1), "image/jpeg"

    monkeypatch.setattr(V, "_download", fake_download)

    class FakeVision:
        async def describe(self, image, mime, context=""):
            return "无有效信息"

    got = await V.describe_images(["http://a/1.jpg"], vision=FakeVision())
    assert got == [EMPTY]


async def test_download_rejects_svg(monkeypatch):
    """svg 是文本可执行的 XML，桶里存一份、将来被当图片内联回吐就有 XSS 面。"""
    import httpx
    from app.core.knowledge.vision import _download

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/svg+xml"},
                              content=b"<svg/>" + b"x" * 20000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await _download(http, "http://a/1.svg") is None


async def test_download_rejects_unknown_mime(monkeypatch):
    """白名单而不是黑名单：扩展名要拿来定代理端点的 Content-Type，是 XSS 面的一部分。"""
    import httpx
    from app.core.knowledge.vision import _download

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/bmp"},
                              content=b"x" * 20000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await _download(http, "http://a/1.bmp") is None
