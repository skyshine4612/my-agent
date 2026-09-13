# run_skill_script 工具测试：假沙箱客户端（记录调用参数）+ 真 FileStore（假 Storage、真库）+ 临时脚本目录。
# 重点验证三件静默出错的事：白名单组合校验、文件引用只认本会话文件名、产物写回带对的 source。
import base64

import pytest
import pytest_asyncio

from app.core.agent import NON_QUERY_TOOLS
from app.core.context import current_conversation_id, current_user_id
from app.core.files import FileStore
from app.core.skill_scripts import scan_scripts
from app.tools.script import make_run_skill_script
from tests.fake_storage import make_client


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
def scripts(tmp_path):
    """白名单脚本目录：run_skill_script 读的是这里的源码，不是模型给的。"""
    for skill, names in (("excel", ["desensitize.py"]), ("travel", ["plan.py"])):
        d = tmp_path / "skills" / skill / "scripts"
        d.mkdir(parents=True)
        for s in names:
            (d / s).write_text("print('hi')", encoding="utf-8")
    return scan_scripts(tmp_path / "skills")


@pytest_asyncio.fixture
async def env(db, user_id, new_conversation, scripts):
    """(store, scripts, uid, cid)：真 FileStore——字节进假 Storage，元数据进真库。

    uid / cid 必须是库里真实存在的行（files 表两个外键），
    再像以前那样用 "u1" / "c1" 字面量灌 contextvar 会直接撞外键。
    """
    storage, _ = make_client()
    cid = await new_conversation()
    current_user_id.set(user_id)
    current_conversation_id.set(cid)
    return FileStore(storage), scripts, user_id, cid


async def test_rejects_unknown_skill_script_pair(env):
    store, scripts, uid, cid = env
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    # excel 与 plan 各自都在白名单里，但组合不存在
    r = await tool.ainvoke({"skill": "excel", "script": "plan", "params": {}})
    assert r["ok"] is False and "没有这个脚本" in r["error"]
    assert sandbox.calls == []


async def test_enum_blocks_names_outside_whitelist(env):
    store, scripts, uid, cid = env
    tool = make_run_skill_script(_FakeSandbox(), store, scripts)
    with pytest.raises(Exception):
        await tool.ainvoke({"skill": "excel", "script": "rm_rf", "params": {}})


async def test_resolves_name_and_sends_bytes(env):
    store, scripts, uid, cid = env
    await store.save(uid, cid, "a.xlsx", b"PK\x03\x04")
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize",
                            "params": {"file": "a.xlsx", "ops": [{"column": "联系电话"}]}})
    call = sandbox.calls[0]
    # 参数原样透传（清单里的名字就是沙箱 work_dir 里的名字），字节另走 files 通道
    assert call["args"]["file"] == "a.xlsx"
    assert call["args"]["ops"] == [{"column": "联系电话"}]     # 嵌套结构里非文件名的值原样保留
    assert call["files"][0]["name"] == "a.xlsx"
    assert base64.b64decode(call["files"][0]["data"]) == b"PK\x03\x04"
    assert call["script"] == "print('hi')"                    # 源码来自白名单文件，不是模型
    assert r["ok"] is True and r["output"] == "done"


async def test_unknown_name_passes_through_untouched(env):
    store, scripts, uid, cid = env
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    # 不在本会话清单里的值原样透传：路径穿越串也走这条路，由脚本 basename 后 open 失败兜住
    await tool.ainvoke({"skill": "excel", "script": "desensitize",
                        "params": {"file": "../../etc/passwd"}})
    assert sandbox.calls[0]["args"]["file"] == "../../etc/passwd"
    assert sandbox.calls[0]["files"] == []


async def test_file_id_shape_is_not_resolved(env):
    """别名已去掉：照抄工具结果里的 uuid 不会被解析成文件，而是原样传下去撞壁、再靠脚本自纠。"""
    store, scripts, uid, cid = env
    saved = await store.save(uid, cid, "a.xlsx", b"x")
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {"file": saved["file_id"]}})
    assert sandbox.calls[0]["files"] == []
    assert sandbox.calls[0]["args"]["file"] == saved["file_id"]


async def test_name_equal_to_an_artifact_is_not_rewritten(env):
    """值恰好等于某个产物名时原样传进沙箱。

    `out_name="已脱敏.xlsx"` 进去还是 `已脱敏.xlsx`，不会被拼成 `已脱敏.xlsx.xlsx`。
    """
    store, scripts, uid, cid = env
    await store.save(uid, cid, "已脱敏.xlsx", b"x", source="script:excel/desensitize")
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize",
                            "params": {"file": "已脱敏.xlsx", "out_name": "已脱敏.xlsx"}})
    assert sandbox.calls[0]["args"]["out_name"] == "已脱敏.xlsx"
    assert sandbox.calls[0]["args"]["file"] == "已脱敏.xlsx"
    assert [f["name"] for f in sandbox.calls[0]["files"]] == ["已脱敏.xlsx"]   # 同名只收一次
    assert r["ok"] is True


async def test_other_users_file_is_not_readable(env, make_user):
    store, scripts, uid, cid = env
    sandbox = _FakeSandbox()
    tool = make_run_skill_script(sandbox, store, scripts)
    # 换一个用户执行：同名文件不在本用户的清单里，于是原样透传、不给字节
    await store.save(uid, cid, "a.xlsx", b"x")
    current_user_id.set(await make_user())
    await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {"file": "a.xlsx"}})
    assert sandbox.calls[0]["files"] == []
    assert sandbox.calls[0]["args"]["file"] == "a.xlsx"


async def test_artifacts_are_saved_back(env):
    store, scripts, uid, cid = env
    await store.save(uid, cid, "a.xlsx", b"x")
    sandbox = _FakeSandbox({"exit_code": 0, "stdout": "ok", "stderr": "",
                            "artifacts": [{"name": "已脱敏.xlsx",
                                           "data": base64.b64encode(b"out").decode()}],
                            "duration_ms": 1})
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {"file": "a.xlsx"}})
    art = r["artifacts"][0]
    assert art["name"] == "已脱敏.xlsx"
    got = await store.read(uid, cid, art["file_id"])
    assert got is not None and got[1] == b"out"
    assert got[0]["source"] == "script:excel/desensitize"


async def test_nonzero_exit_reports_stderr(env):
    store, scripts, uid, cid = env
    sandbox = _FakeSandbox({"exit_code": 1, "stdout": "", "stderr": "列名不存在：手机号",
                            "artifacts": [], "duration_ms": 1})
    tool = make_run_skill_script(sandbox, store, scripts)
    r = await tool.ainvoke({"skill": "excel", "script": "desensitize", "params": {}})
    assert r["ok"] is False and r["error"] == "列名不存在：手机号"


def test_tool_is_declared_non_query():
    # 漏登记不会报错，只会让返回被结构化截断、产物清单凭空消失，所以由测试钉住
    assert "run_skill_script" in NON_QUERY_TOOLS
