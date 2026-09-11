# tests/test_vision.py
# 图片转文字的降级路径测试：不依赖网络与真实模型，下载/调用失败都必须退化成空串而不是抛。
from app.core.vision import MAX_IMAGES, describe_images


class _FakeVision:
    """假多模态客户端：永远返回固定描述（真实识别能力由端到端验证覆盖）。"""

    async def describe(self, image, mime="image/jpeg"):
        return "西湖断桥的雪景"


async def test_download_failure_yields_empty():
    """图片下载不到时该项为空串，不影响整篇入库。"""
    out = await describe_images(["http://127.0.0.1:1/none.png"], vision=_FakeVision())
    assert out == [""]


async def test_caps_image_count():
    """单页图片数超上限时只处理前 MAX_IMAGES 张（攻略页动辄几十张，全转又慢又贵）。"""
    urls = [f"http://127.0.0.1:1/{i}.png" for i in range(MAX_IMAGES + 5)]
    assert len(await describe_images(urls, vision=_FakeVision())) == MAX_IMAGES


async def test_empty_urls():
    """没有图片时不调用任何东西，直接返回空列表。"""
    assert await describe_images([], vision=_FakeVision()) == []
