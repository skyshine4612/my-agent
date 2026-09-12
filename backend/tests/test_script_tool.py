# run_skill_script 工具测试：假沙箱客户端（记录调用参数）+ 真 FileStore(tmp_path) + 临时脚本目录。
# 重点验证三件静默出错的事：白名单组合校验、file_id 替换只认已知值、产物写回带对的 source。
import base64

import pytest

from app.core.agent import NON_QUERY_TOOLS
from app.core.context import current_conversation_id, current_user_id
from app.core.files import FileStore
from app.core.skill_scripts import scan_scripts
from app.tools.script import make_run_skill_script


class _FakeSandbox:
    """假沙箱：记录每次调用的 (script, args, files)，返回预置结果。"""

    def __init__(self, result=None):
        self.result = result or {"exit_code": 0, "stdout": "done", "stderr": "",
                                 "artifacts": [], "duration_ms": 1}
        self.calls = []

    async def run(self, script, args, files, timeout=None):
        self.calls.append({"script": script, "args": args, "files": files})
        return self.result


@pytest.fixture
def env(tmp_path):
    for skill, scripts in (("excel", ["desensitize.py"]), ("travel", ["plan.py"])):
        d = tmp_path / "skills" / skill / "scripts"
        d.mkdir(parents=True)
        for s in scripts:
            (d / s).write_text("print('hi')", encoding="utf-8")
    store = FileStore(tmp_path / "files")
    current_user_id.set("u1")
    current_conversation_id.set("c1")
    return store, scan_scripts(tmp_path / "skills")


async def test_rejects_unknown_skill_script_pair(env):
    store, scripts = env
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    # excel 与 plan 各自都在白名单里，但组合不存在
    r = await tool.ainvoke({"skill": "excel", "script": "plan", "params": {}})
    assert r["ok"] is False and "没有这个脚本" in r["error"]
    assert sandbox.calls == []


async def test_enum_blocks_names_outside_whitelist(env):
    store, scripts = env
    tool = make_run_skill_script(_FakeSandbox(), store, scripts)
    with pytest.raises(Exception):
        await tool.ainvoke({"skill": "excel", "script": "rm_rf", "params": {}})


async def test_substitutes_file_id_and_sends_bytes(env):
    store, scripts = env
    saved = await store.save("u1", "c1", "a.xlsx", b"PK\x03\x04")
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize",
                            "params": {"file_id": saved["file_id"], "ops": [{"column": "联系电话"}]}})
    call = sandbox.calls[0]
    # 参数里只给文件名（带原扩展名）：真正的落点是沙箱本次执行的 work_dir，由脚本按 work_dir 定位
    assert call["args"]["file_id"] == f"{saved['file_id']}.xlsx"
    assert call["args"]["ops"] == [{"column": "联系电话"}]     # 嵌套结构里非 file_id 的值原样保留
    assert call["files"][0]["name"] == f"{saved['file_id']}.xlsx"
    assert base64.b64decode(call["files"][0]["data"]) == b"PK\x03\x04"
    assert call["script"] == "print('hi')"                    # 源码来自白名单文件，不是模型
    assert r["ok"] is True and r["output"] == "done"


async def test_unknown_file_id_passes_through_untouched(env):
    store, scripts = env
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    await tool.ainvoke({"skill": "excel", "script": "desensitize",
                        "params": {"file_id": "../../etc/passwd"}})
    assert sandbox.calls[0]["args"]["file_id"] == "../../etc/passwd"
    assert sandbox.calls[0]["files"] == []


async def test_other_users_file_id_is_not_readable(env):
    store, scripts = env
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    # 换一个用户执行：拿到别人的 file_id 也读不出字节——它不在本用户的清单里，于是按「非已知值」原样透传
    saved = await store.save("u1", "c1", "a.xlsx", b"x")
    current_user_id.set("u2")
    await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {"file_id": saved["file_id"]}})
    assert sandbox.calls[0]["files"] == []
    assert sandbox.calls[0]["args"]["file_id"] == saved["file_id"]


async def test_artifacts_are_saved_back(env):
    store, scripts = env
    src = await store.save("u1", "c1", "a.xlsx", b"x")
    sandbox = _FakeSandbox({"exit_code": 0, "stdout": "ok", "stderr": "",
                            "artifacts": [{"name": "已脱敏.xlsx",
                                           "data": base64.b64encode(b"out").decode()}],
                            "duration_ms": 1})
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {"file_id": src["file_id"]}})
    art = r["artifacts"][0]
    assert art["name"] == "已脱敏.xlsx"
    got = await store.read("u1", "c1", art["file_id"])
    assert got is not None and got[1] == b"out"
    assert got[0]["source"] == "script:excel/desensitize"


async def test_nonzero_exit_reports_stderr(env):
    store, scripts = env
    sandbox = _FakeSandbox({"exit_code": 1, "stdout": "", "stderr": "列名不存在：手机号",
                            "artifacts": [], "duration_ms": 1})
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {}})
    assert r["ok"] is False and r["error"] == "列名不存在：手机号"


def test_tool_is_declared_non_query():
    # 漏登记不会报错，只会让返回被结构化截断、产物清单凭空消失，所以由测试钉住
    assert "run_skill_script" in NON_QUERY_TOOLS
