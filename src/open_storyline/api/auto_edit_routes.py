from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from open_storyline.storage.agent_memory import ArtifactStore


router = APIRouter(prefix="/api")

EditStatus = Literal["idle", "processing", "completed", "failed"]


class CreateSessionResponse(BaseModel):
    session_id: str = Field(..., description="当前剪辑会话唯一标识")


class UploadMediaResponse(BaseModel):
    media_id: str = Field(..., description="上传素材的唯一标识")


class AutoEditRequest(BaseModel):
    text: str = Field(..., min_length=1, description="用户想要实现的视频内容")
    requirement: str = Field(..., min_length=1, description="具体剪辑要求")
    media_ids: List[str] = Field(..., min_length=1, description="当前剪辑需要使用的素材 ID")


class SubmitEditResponse(BaseModel):
    status: EditStatus


class EditResultResponse(BaseModel):
    status: EditStatus
    video_url: Optional[str] = None
    error: Optional[str] = None


def _session_store(request: Request) -> Any:
    store = getattr(request.app.state, "sessions", None)
    if store is None:
        raise HTTPException(status_code=500, detail="session store is not configured")
    return store


def _set_edit_state(
    sess: Any,
    *,
    status: EditStatus,
    result_path: str = "",
    error: str = "",
) -> None:
    sess.auto_edit_status = status
    sess.auto_edit_result_path = result_path
    sess.auto_edit_error = error


def _get_edit_status(sess: Any) -> EditStatus:
    status = str(getattr(sess, "auto_edit_status", "idle") or "idle")
    if status not in {"idle", "processing", "completed", "failed"}:
        return "idle"
    return status  # type: ignore[return-value]


def _merge_system_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    system_parts: List[str] = []
    non_system: List[BaseMessage] = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            system_parts.append(content)
        else:
            non_system.append(msg)

    if not system_parts:
        return non_system
    return [SystemMessage(content="\n\n".join(system_parts)), *non_system]


def _build_auto_edit_prompt(payload: AutoEditRequest) -> str:
    media_list = "\n".join(f"- {media_id}" for media_id in payload.media_ids)
    return (
        "当前请求来自自动剪辑 REST API。请不要停留在剪辑计划确认阶段，"
        "请直接根据用户文案、剪辑要求和已上传素材完成自动剪辑，并在最终阶段调用 render_video 输出成片。\n\n"
        f"用户文案：\n{payload.text}\n\n"
        f"剪辑要求：\n{payload.requirement}\n\n"
        f"本次任务使用的素材 ID：\n{media_list}"
    )


def _latest_render_output_path(sess: Any) -> str:
    cfg = sess.cfg
    store = ArtifactStore(cfg.project.outputs_dir, session_id=sess.session_id)
    latest = store.get_latest_meta(node_id="render_video", session_id=sess.session_id)
    if latest is None:
        return ""

    _, data = store.load_result(latest.artifact_id)
    payload = (data or {}).get("payload") if isinstance(data, dict) else {}
    output_path = str((payload or {}).get("output_path") or "")
    if output_path and os.path.exists(output_path):
        return output_path
    return ""


async def _save_session_state(store: Any, sess: Any) -> None:
    save = getattr(store, "save_session_state", None)
    if save is not None:
        await save(sess)


async def _run_auto_edit_task(store: Any, sess: Any, payload: AutoEditRequest) -> None:
    try:
        await sess.ensure_agent()

        if getattr(sess, "client_context", None) is not None:
            sess.client_context.lang = getattr(sess, "lang", "zh")

        prompt = _build_auto_edit_prompt(payload)
        sess.history.append(
            {
                "role": "user",
                "content": prompt,
                "attachments": [sess.public_media(sess.load_media[mid]) for mid in payload.media_ids if mid in sess.load_media],
            }
        )

        messages = list(getattr(sess, "lc_messages", []) or [])
        messages.append(
            SystemMessage(
                content=(
                    "自动剪辑 API 模式：不要等待用户确认剪辑计划；"
                    "直接分析需求、调用必要 MCP/Node，并生成最终视频。"
                )
            )
        )
        messages.append(HumanMessage(content=prompt))

        result = await sess.agent.ainvoke(
            {"messages": _merge_system_messages(messages)},
            context=sess.client_context,
        )

        result_messages = []
        if isinstance(result, dict):
            maybe_messages = result.get("messages")
            if isinstance(maybe_messages, list):
                result_messages = [m for m in maybe_messages if isinstance(m, BaseMessage)]

        if result_messages:
            sess.lc_messages = result_messages
            for msg in reversed(result_messages):
                if isinstance(msg, AIMessage):
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if content.strip():
                        sess.history.append({"role": "assistant", "content": content.strip()})
                    break

        output_path = _latest_render_output_path(sess)
        if not output_path:
            raise RuntimeError("render_video output not found")

        _set_edit_state(sess, status="completed", result_path=output_path)
    except Exception as exc:
        _set_edit_state(sess, status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        await _save_session_state(store, sess)


@router.post(
    "/sessions",
    response_model=CreateSessionResponse,
    tags=["Sessions"],
    summary="创建剪辑会话",
)
async def create_edit_session(request: Request) -> CreateSessionResponse:
    store = _session_store(request)
    sess = await store.create()
    return CreateSessionResponse(session_id=sess.session_id)


@router.post(
    "/sessions/{session_id}/media",
    response_model=UploadMediaResponse,
    tags=["Media"],
    summary="上传素材",
)
async def upload_edit_media(
    session_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> UploadMediaResponse:
    store = _session_store(request)
    sess = await store.get_or_404(session_id)

    async with sess.media_lock:
        store_filename = sess._reserve_store_filenames_locked([file.filename or "unnamed"])[0]

    metas = await sess.add_uploads([file], store_filenames=[store_filename])
    await _save_session_state(store, sess)

    if not metas:
        raise HTTPException(status_code=500, detail="media save failed")
    return UploadMediaResponse(media_id=metas[0].id)


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
    store = _session_store(request)
    sess = await store.get_or_404(session_id)

    if _get_edit_status(sess) == "processing":
        raise HTTPException(status_code=409, detail="current edit task is still processing")

    missing_media_ids = [media_id for media_id in payload.media_ids if media_id not in sess.load_media]
    if missing_media_ids:
        raise HTTPException(status_code=404, detail={"missing_media_ids": missing_media_ids})

    _set_edit_state(sess, status="processing")
    await _save_session_state(store, sess)

    background_tasks.add_task(_run_auto_edit_task, store, sess, payload)
    return SubmitEditResponse(status="processing")


@router.get(
    "/sessions/{session_id}/result",
    response_model=EditResultResponse,
    tags=["Auto Edit"],
    summary="获取剪辑结果",
)
async def get_auto_edit_result(session_id: str, request: Request) -> EditResultResponse:
    store = _session_store(request)
    sess = await store.get_or_404(session_id)

    status = _get_edit_status(sess)
    if status == "completed":
        return EditResultResponse(
            status=status,
            video_url=f"/api/sessions/{session_id}/result.mp4",
        )
    if status == "failed":
        return EditResultResponse(
            status=status,
            error=str(getattr(sess, "auto_edit_error", "") or "auto edit failed"),
        )
    return EditResultResponse(status=status)


@router.get(
    "/sessions/{session_id}/result.mp4",
    tags=["Auto Edit"],
    summary="下载剪辑完成的视频",
    include_in_schema=False,
)
async def download_auto_edit_result(session_id: str, request: Request) -> FileResponse:
    store = _session_store(request)
    sess = await store.get_or_404(session_id)

    if _get_edit_status(sess) != "completed":
        raise HTTPException(status_code=404, detail="result video is not ready")

    result_path = str(getattr(sess, "auto_edit_result_path", "") or "")
    if not result_path or not os.path.exists(result_path):
        raise HTTPException(status_code=404, detail="result video not found")

    outputs_root = Path(sess.cfg.project.outputs_dir).resolve()
    resolved_result = Path(result_path).resolve()
    try:
        resolved_result.relative_to(outputs_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="forbidden")

    return FileResponse(
        str(resolved_result),
        media_type="video/mp4",
        filename=resolved_result.name,
    )
