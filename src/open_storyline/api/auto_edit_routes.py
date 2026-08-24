from __future__ import annotations

import os
import hashlib
import asyncio
import mimetypes
import time
import uuid
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Literal, Optional

import requests
from fastapi import APIRouter, BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from open_storyline.storage.agent_memory import ArtifactStore


router = APIRouter()

UploadMediaService = Callable[
    [str, Request, Optional[List[UploadFile]], Optional[UploadFile]],
    Awaitable[Any],
]
UPLOAD_MEDIA_SERVICE_STATE_KEY = "auto_edit_upload_media_service"

EditStatus = Literal["idle", "processing", "completed", "failed"]


class AutoEditRequest(BaseModel):
    text: str = Field(..., min_length=1, description="用户想要实现的视频内容")
    requirement: str = Field(..., min_length=1, description="具体剪辑要求")
    media_ids: List[str] = Field(..., min_length=1, description="当前剪辑需要使用的素材 ID")


class SubmitEditResponse(BaseModel):
    status: EditStatus


class EditResultResponse(BaseModel):
    status: EditStatus
    media_id: Optional[str] = None
    url: Optional[str] = None
    video_url: Optional[str] = None
    local_video_url: Optional[str] = None
    upload_error: Optional[str] = None
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
    public_video_url: str = "",
    result_media_id: str = "",
    upload_error: str = "",
    error: str = "",
) -> None:
    sess.auto_edit_status = status
    sess.auto_edit_result_path = result_path
    sess.auto_edit_public_video_url = public_video_url
    sess.auto_edit_result_media_id = result_media_id
    sess.auto_edit_upload_error = upload_error
    sess.auto_edit_error = error


def _get_edit_status(sess: Any) -> EditStatus:
    status = str(getattr(sess, "auto_edit_status", "idle") or "idle")
    if status not in {"idle", "processing", "completed", "failed"}:
        return "idle"
    return status  # type: ignore[return-value]


def _format_exception(exc: BaseException) -> str:
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions:
        parts = [f"{type(exc).__name__}: {exc}"]
        for idx, sub_exc in enumerate(sub_exceptions, 1):
            parts.append(f"\n--- sub-exception #{idx}: {type(sub_exc).__name__}: {sub_exc} ---\n")
            parts.extend(traceback.format_exception(type(sub_exc), sub_exc, sub_exc.__traceback__))
        return "".join(parts)
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _result_media_id(result_path: str) -> str:
    digest = hashlib.sha1(str(Path(result_path).resolve()).encode("utf-8")).hexdigest()
    return f"result_{digest[:10]}"


def _public_base_url(request: Request) -> str:
    configured = os.getenv("OPENSTORYLINE_PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_host:
        proto = (forwarded_proto or request.url.scheme or "http").split(",", 1)[0].strip()
        host = forwarded_host.split(",", 1)[0].strip()
        return f"{proto}://{host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def _result_video_url(request: Request, session_id: str) -> str:
    return f"{_public_base_url(request)}/api/sessions/{session_id}/result.mp4"


def _result_upload_config(sess: Any) -> dict[str, Any]:
    cfg = getattr(sess.cfg, "result_upload", None)
    if cfg is None or not bool(getattr(cfg, "enabled", False)):
        return {"enabled": False}

    access_token = str(getattr(cfg, "access_token", "") or "").strip()
    if not access_token:
        access_token = str(os.getenv("MARKETING_ACCESS_TOKEN") or "").strip()

    return {
        "enabled": True,
        "upload_temp_url": str(getattr(cfg, "upload_temp_url", "") or "").strip(),
        "access_token": access_token,
        "title": str(getattr(cfg, "title", "") or "").strip(),
        "description": str(getattr(cfg, "description", "") or "").strip(),
        "timeout": float(getattr(cfg, "timeout", 120.0) or 120.0),
    }


def _extract_uploaded_file_result(resp_json: Any) -> tuple[str, str]:
    if not isinstance(resp_json, dict):
        return "", ""
    result = resp_json.get("result")
    if not isinstance(result, dict):
        return "", ""
    return str(result.get("id") or ""), str(result.get("url") or "")


def _upload_result_video_sync(sess: Any, result_path: str) -> tuple[str, str, str]:
    cfg = _result_upload_config(sess)
    if not cfg.get("enabled"):
        return "", "", ""

    upload_url = str(cfg.get("upload_temp_url") or "")
    access_token = str(cfg.get("access_token") or "")
    if not upload_url:
        return "", "", "result_upload.upload_temp_url is not configured"

    file_path = Path(result_path)
    mime_type = mimetypes.guess_type(file_path.name)[0] or "video/mp4"
    params = {
        "title": cfg.get("title") or file_path.name,
        "description": cfg.get("description") or "",
    }
    headers = {"X-Access-Token": access_token} if access_token else {}

    try:
        with file_path.open("rb") as f:
            resp = requests.post(
                upload_url,
                params=params,
                headers=headers,
                files={"file": (file_path.name, f, mime_type)},
                timeout=float(cfg.get("timeout") or 120.0),
            )
        resp.raise_for_status()
        resp_json = resp.json()
        remote_media_id, public_url = _extract_uploaded_file_result(resp_json)
        if not public_url:
            return "", "", f"uploadTemp returned no result.url: {resp_json}"
        return remote_media_id, public_url, ""
    except Exception as exc:
        return "", "", f"{type(exc).__name__}: {exc}"


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


def _latest_render_output_path(sess: Any, *, created_after: Optional[float] = None) -> str:
    cfg = sess.cfg
    store = ArtifactStore(cfg.project.outputs_dir, session_id=sess.session_id)
    latest = store.get_latest_meta(node_id="render_video", session_id=sess.session_id)
    if latest is None:
        return ""
    if created_after is not None and float(latest.created_at) < created_after:
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


async def _complete_with_render_output(store: Any, sess: Any, output_path: str) -> None:
    remote_media_id, public_url, upload_error = await asyncio.to_thread(
        _upload_result_video_sync,
        sess,
        output_path,
    )

    _set_edit_state(
        sess,
        status="completed",
        result_path=output_path,
        public_video_url=public_url,
        result_media_id=remote_media_id,
        upload_error=upload_error,
    )
    await _save_session_state(store, sess)


def _upload_media_service(request: Request) -> UploadMediaService:
    service = getattr(request.app.state, UPLOAD_MEDIA_SERVICE_STATE_KEY, None)
    if service is None:
        raise HTTPException(status_code=500, detail="upload media service is not configured")
    return service


async def _run_auto_edit_task(store: Any, sess: Any, payload: AutoEditRequest) -> None:
    task_started_at = time.time()
    try:
        async with sess.chat_lock:
            sess.cancel_event.clear()
            await sess.ensure_agent()

            ensure_system_prompt = getattr(sess, "_ensure_system_prompt", None)
            if ensure_system_prompt is not None:
                ensure_system_prompt()

            if getattr(sess, "client_context", None) is not None:
                sess.client_context.lang = getattr(sess, "lang", "zh")

            prompt = _build_auto_edit_prompt(payload)
            attachments = [
                sess.public_media(sess.load_media[mid])
                for mid in payload.media_ids
                if mid in sess.load_media
            ]
            sess.history.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "role": "user",
                    "content": prompt,
                    "attachments": attachments,
                    "ts": time.time(),
                }
            )

            sanitize_messages = getattr(sess, "_sanitize_tool_protocol_in_lc_messages", None)
            if sanitize_messages is not None:
                sanitize_messages()

            sess.lc_messages.append(HumanMessage(content=prompt))
            await _save_session_state(store, sess)

            messages = list(getattr(sess, "lc_messages", []) or [])
            messages.append(
                SystemMessage(
                    content=(
                        "自动剪辑 API 模式：不要等待用户确认剪辑计划；"
                        "直接分析需求、调用必要 MCP/Node，并生成最终视频。"
                    )
                )
            )
            invoke_messages = _merge_system_messages(messages)

            result = await sess.agent.ainvoke(
                {"messages": invoke_messages},
                context=sess.client_context,
            )

            result_messages = []
            if isinstance(result, dict):
                maybe_messages = result.get("messages")
                if isinstance(maybe_messages, list):
                    result_messages = [m for m in maybe_messages if isinstance(m, BaseMessage)]

            if len(result_messages) >= len(invoke_messages):
                new_messages = result_messages[len(invoke_messages):]
            else:
                new_messages = result_messages
            if new_messages:
                sess.lc_messages.extend(new_messages)
                for msg in reversed(new_messages):
                    if isinstance(msg, AIMessage):
                        content = msg.content if isinstance(msg.content, str) else str(msg.content)
                        if content.strip():
                            sess.history.append(
                                {
                                    "id": uuid.uuid4().hex[:12],
                                    "role": "assistant",
                                    "content": content.strip(),
                                    "ts": time.time(),
                                }
                            )
                        break

            output_path = _latest_render_output_path(sess, created_after=task_started_at)
            if not output_path:
                raise RuntimeError("render_video output not found")

            await _complete_with_render_output(store, sess, output_path)
            return
    except Exception as exc:
        output_path = _latest_render_output_path(sess, created_after=task_started_at)
        if output_path:
            await _complete_with_render_output(store, sess, output_path)
            return
        _set_edit_state(sess, status="failed", error=_format_exception(exc))
    finally:
        await _save_session_state(store, sess)


@router.post(
    "/sessions",
    tags=["Sessions"],
    summary="创建剪辑会话",
)
async def create_edit_session(request: Request) -> Any:
    store = _session_store(request)
    sess = await store.create()
    return sess.snapshot()


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
    service = _upload_media_service(request)
    return await service(session_id, request, files, file)


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

    if _get_edit_status(sess) == "processing" or sess.chat_lock.locked():
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
    if status == "failed" and not str(getattr(sess, "auto_edit_result_path", "") or ""):
        output_path = _latest_render_output_path(sess)
        if output_path:
            await _complete_with_render_output(store, sess, output_path)
            status = _get_edit_status(sess)

    if status == "completed":
        result_path = str(getattr(sess, "auto_edit_result_path", "") or "")
        public_video_url = str(getattr(sess, "auto_edit_public_video_url", "") or "")
        remote_media_id = str(getattr(sess, "auto_edit_result_media_id", "") or "")
        upload_error = str(getattr(sess, "auto_edit_upload_error", "") or "")
        if result_path and not public_video_url:
            remote_media_id, public_video_url, upload_error = await asyncio.to_thread(
                _upload_result_video_sync,
                sess,
                result_path,
            )
            sess.auto_edit_public_video_url = public_video_url
            sess.auto_edit_result_media_id = remote_media_id
            sess.auto_edit_upload_error = upload_error
            await _save_session_state(store, sess)

        local_video_url = _result_video_url(request, session_id)
        return EditResultResponse(
            status=status,
            media_id=remote_media_id or (_result_media_id(result_path) if result_path else None),
            url=public_video_url or None,
            video_url=public_video_url or None,
            local_video_url=local_video_url,
            upload_error=upload_error or None,
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
    cache_root = Path(sess.cfg.local_mcp_server.server_cache_dir)
    if not cache_root.is_absolute():
        cache_root = Path.cwd() / cache_root
    allowed_roots = (outputs_root, cache_root.resolve())
    resolved_result = Path(result_path).resolve()
    if not any(resolved_result.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="forbidden")

    return FileResponse(
        str(resolved_result),
        media_type="video/mp4",
        filename=resolved_result.name,
    )


def register_auto_edit_routes(
    app: FastAPI,
    *,
    upload_media_service: UploadMediaService,
) -> None:
    setattr(app.state, UPLOAD_MEDIA_SERVICE_STATE_KEY, upload_media_service)
    app.include_router(router, prefix="/api")
