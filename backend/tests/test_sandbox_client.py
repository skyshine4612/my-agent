# 沙箱客户端测试：用 httpx.MockTransport 假沙箱，不连容器、不连网络。
import json

import httpx

from app.core.sandbox import SandboxClient


def _client(handler, timeout=60):
    return SandboxClient("http://sandbox:8001", timeout, transport=httpx.MockTransport(handler))


async def test_run_forwards_payload_and_returns_result():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"exit_code": 0, "stdout": "ok", "stderr": "",
                                         "artifacts": [], "duration_ms": 12})

    client = _client(handler)
    r = await client.run("print('ok')", {"a": 1}, [{"name": "f1", "data": "eA=="}], timeout=30)
    assert seen["url"] == "http://sandbox:8001/run"
    assert seen["body"] == {"script": "print('ok')", "args": {"a": 1},
                            "files": [{"name": "f1", "data": "eA=="}], "timeout": 30}
    assert r["stdout"] == "ok"
    await client.close()


async def test_timeout_is_default_when_not_given():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "",
                                         "artifacts": [], "duration_ms": 0})

    client = _client(handler, timeout=60)
    await client.run("x", {}, [])
    assert seen["body"]["timeout"] == 60
    await client.close()


async def test_client_waits_longer_than_sandbox():
    # 双端超时：httpx 等的是沙箱超时 + 5s，让沙箱有机会把「超时」当结构化响应返回
    client = _client(lambda request: httpx.Response(200, json={}), timeout=60)
    assert client._client.timeout.read == 65
    await client.close()


async def test_network_error_is_normalized():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    r = await client.run("x", {}, [])
    assert r["exit_code"] == -1
    assert "沙箱不可达" in r["stderr"]
    assert r["artifacts"] == []
    await client.close()


async def test_bad_status_is_normalized():
    client = _client(lambda request: httpx.Response(500, text="boom"))
    r = await client.run("x", {}, [])
    assert r["exit_code"] == -1
    assert "沙箱" in r["stderr"]
    await client.close()


async def test_health_returns_bool():
    ok = _client(lambda request: httpx.Response(200, json={"ok": True}))
    bad = _client(lambda request: httpx.Response(500))
    assert await ok.health() is True
    assert await bad.health() is False
    await ok.close()
    await bad.close()
