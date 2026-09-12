# app/api/routes/files.py
# 会话文件路由：上传（multipart）与下载。
# 隔离靠两处：验签出的 user_id 决定目录归属，file_id 必须是合法 uuid（FileStore 内校验）。
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.core.auth import get_current_user
from app.core.files import safe_filename
from app.services.agent_service import service

router = APIRouter()

# 单文件上限：沙箱 tmpfs 512m 要同时容纳输入与产物，产物另有单文件 20MB / 总 40MB 的限制，这里对齐单文件上限
MAX_UPLOAD_SIZE = 20 * 1024 * 1024
# 本期只收 .xlsx：openpyxl 不读 .xls（那是 xlrd 的活）也不读 .csv（标准库的事）
ALLOWED_SUFFIXES = {".xlsx"}


@router.post("/files")
async def upload_file(file: UploadFile = File(...), conversation_id: str = Form(None),
                      user_id: str = Depends(get_current_user)):
    """上传一个会话文件；没带 conversation_id 或带的不是自己的会话时，自动建一个空会话并返回其 id。"""
    # 先净化再判后缀：判的必须是最终落盘的那个名字。用原名判，`a.xlsx\n` 这类尾随控制字符的名字
    # suffix 会成 `.xlsx\n`，合法文件反而被拒
    name = safe_filename(file.filename or "")
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="本期只支持 .xlsx 文件")
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限")

    conv_id = conversation_id
    # 与 AgentService._ensure_conversation 同语义：他人的会话 id 当作没有，重建自己的，杜绝跨用户写入
    if not conv_id or not await service.conversation_store.belongs_to(user_id, conv_id):
        conv_id = await service.conversation_store.create_conversation(user_id, name[:20])

    saved = await service.file_store.save(user_id, conv_id, name, data, source="upload")
    return {**saved, "conversation_id": conv_id}


@router.get("/files/{file_id}")
async def download_file(file_id: str, conversation_id: str, user_id: str = Depends(get_current_user)):
    """下载会话内的文件；file_id 非法、文件不存在或不属于该会话都返回 404（不区分，避免探测）。"""
    got = await service.file_store.read(user_id, conversation_id, file_id)
    if got is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    meta, data = got
    # 文件名可能是中文，按 RFC 5987 编码（裸 filename= 只能放 ASCII）
    disposition = f"attachment; filename*=UTF-8''{quote(meta['name'])}"
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": disposition})
