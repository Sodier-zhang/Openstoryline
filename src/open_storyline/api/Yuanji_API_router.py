from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse

from open_storyline.api.Yuanji_API_service import (
    UPLOAD_MEDIA_SERVICE_STATE_KEY,
    AutoEditRequest,
    EditResultResponse,
    SubmitEditResponse,
    UploadMediaService,
    create_edit_session as create_edit_session_service,
    download_auto_edit_result as download_auto_edit_result_service,
    get_auto_edit_result as get_auto_edit_result_service,
    submit_auto_edit_task as submit_auto_edit_task_service,
    upload_edit_media as upload_edit_media_service,
)


router = APIRouter()


@router.post(
    "/sessions",
    tags=["Sessions"],
    summary="创建剪辑会话",
)
async def create_edit_session(request: Request) -> Any:
    return await create_edit_session_service(request)


@router.post(
    "/sessions/{session_id}/media",
    tags=["Media"],
    summary="上传素材",
)
async def upload_edit_media(
    session_id: str,
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
) -> Any:
    return await upload_edit_media_service(session_id, request, files, file)


@router.post(
    "/sessions/{session_id}/edit",
    response_model=SubmitEditResponse,
    tags=["Auto Edit"],
    summary="提交自动剪辑任务",
)
async def submit_auto_edit_task(
    session_id: str,
    payload: AutoEditRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> SubmitEditResponse:
    return await submit_auto_edit_task_service(session_id, payload, request, background_tasks)


@router.get(
    "/sessions/{session_id}/result",
    response_model=EditResultResponse,
    tags=["Auto Edit"],
    summary="获取剪辑结果",
)
async def get_auto_edit_result(session_id: str, request: Request) -> EditResultResponse:
    return await get_auto_edit_result_service(session_id, request)


@router.get(
    "/sessions/{session_id}/result.mp4",
    tags=["Auto Edit"],
    summary="下载剪辑完成的视频",
    include_in_schema=False,
)
async def download_auto_edit_result(session_id: str, request: Request) -> FileResponse:
    return await download_auto_edit_result_service(session_id, request)


def register_auto_edit_routes(
    app: FastAPI,
    *,
    upload_media_service: UploadMediaService,
) -> None:
    setattr(app.state, UPLOAD_MEDIA_SERVICE_STATE_KEY, upload_media_service)
    app.include_router(router, prefix="/api")
