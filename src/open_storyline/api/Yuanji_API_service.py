from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Literal, Optional

import requests
from fastapi import BackgroundTasks, HTTPException, Request, UploadFile
from langchain.tools import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from open_storyline.storage.agent_memory import ArtifactStore


UploadMediaService = Callable[
    [str, Request, Optional[List[UploadFile]], Optional[UploadFile]],
    Awaitable[Any],
]
UPLOAD_MEDIA_SERVICE_STATE_KEY = "auto_edit_upload_media_service"
STALE_PROCESSING_GRACE_SECONDS = 10.0
MONTAGE_WORKFLOW_SKILL = "video-montage-workflow-skill"
MONTAGE_WORKFLOW_NODES = (
    "load_media",
    "split_shots",
    "understand_clips",
    "rewrite_montage_script",
    "match_montage_segments",
    "generate_montage_video",
    "plan_timeline_pro",
    "render_video",
)

EditStatus = Literal["idle", "processing", "completed", "failed"]


class AutoEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    script: str = Field(..., min_length=1, description="用于生成混剪视频的原始脚本")
    media_id: str = Field(
        ...,
        alias="media_ID",
        min_length=1,
        description="当前混剪任务使用的已上传素材 ID",
    )


class SubmitEditResponse(BaseModel):
    status: EditStatus


class EditResultResponse(BaseModel):
    status: EditStatus
    media_id: Optional[str] = None
    video_url: Optional[str] = None
    upload_error: Optional[str] = None
    error: Optional[str] = None


async def parse_auto_edit_request(request: Request) -> AutoEditRequest:
    raw_body = await request.body()
    if not raw_body.strip():
        raise HTTPException(status_code=422, detail="request body cannot be empty")

    try:
        body_text = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="request body must be UTF-8 JSON") from exc

    try:
        data = json.loads(body_text)
    except json.JSONDecodeError:
        try:
            # Some API clients paste multiline scripts directly into a JSON string.
            data = json.loads(body_text, strict=False)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "json_invalid",
                    "message": exc.msg,
                    "position": exc.pos,
                },
            ) from exc

    try:
        return AutoEditRequest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_input=False),
        ) from exc


def session_store(request: Request) -> Any:
    store = getattr(request.app.state, "sessions", None)
    if store is None:
        raise HTTPException(status_code=500, detail="session store is not configured")
    return store


def set_edit_state(
    sess: Any,
    *,
    status: EditStatus,
    result_path: str = "",
    public_video_url: str = "",
    result_media_id: str = "",
    upload_error: str = "",
    error: str = "",
) -> None:
    now = time.time()
    sess.auto_edit_status = status
    sess.auto_edit_result_path = result_path
    sess.auto_edit_public_video_url = public_video_url
    sess.auto_edit_result_media_id = result_media_id
    sess.auto_edit_upload_error = upload_error
    sess.auto_edit_error = error
    if status == "processing":
        sess.auto_edit_started_at = now
    sess.auto_edit_updated_at = now


def get_edit_status(sess: Any) -> EditStatus:
    status = str(getattr(sess, "auto_edit_status", "idle") or "idle")
    if status not in {"idle", "processing", "completed", "failed"}:
        return "idle"
    return status  # type: ignore[return-value]


def format_exception(exc: BaseException) -> str:
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions:
        parts = [f"{type(exc).__name__}: {exc}"]
        for idx, sub_exc in enumerate(sub_exceptions, 1):
            parts.append(f"\n--- sub-exception #{idx}: {type(sub_exc).__name__}: {sub_exc} ---\n")
            parts.extend(traceback.format_exception(type(sub_exc), sub_exc, sub_exc.__traceback__))
        return "".join(parts)
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def result_media_id(result_path: str) -> str:
    digest = hashlib.sha1(str(Path(result_path).resolve()).encode("utf-8")).hexdigest()
    return f"result_{digest[:10]}"


def result_upload_config(sess: Any) -> dict[str, Any]:
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


def extract_uploaded_file_result(resp_json: Any) -> tuple[str, str]:
    if not isinstance(resp_json, dict):
        return "", ""
    result = resp_json.get("result")
    if not isinstance(result, dict):
        return "", ""
    return str(result.get("id") or ""), str(result.get("url") or "")


def upload_result_video_sync(sess: Any, result_path: str) -> tuple[str, str, str]:
    cfg = result_upload_config(sess)
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
        remote_media_id, public_url = extract_uploaded_file_result(resp_json)
        if not public_url:
            return "", "", f"uploadTemp returned no result.url: {resp_json}"
        return remote_media_id, public_url, ""
    except Exception as exc:
        return "", "", f"{type(exc).__name__}: {exc}"


def montage_node_calls(payload: AutoEditRequest) -> list[tuple[str, dict[str, Any]]]:
    node_args = {
        "rewrite_montage_script": {"mode": "auto", "script": payload.script},
        "plan_timeline_pro": {
            "mode": "auto",
            "is_montage": True,
            "user_request": "",
        },
    }
    return [
        (node_id, node_args.get(node_id, {"mode": "auto"}))
        for node_id in MONTAGE_WORKFLOW_NODES
    ]


async def invoke_montage_node(
    sess: Any,
    artifact_store: ArtifactStore,
    node_id: str,
    args: dict[str, Any],
    *,
    created_after: float,
) -> dict[str, Any]:
    node_manager = getattr(sess, "node_manager", None)
    context = getattr(sess, "client_context", None)
    if node_manager is None or context is None:
        raise RuntimeError("montage workflow runtime is not initialized")

    tool = node_manager.get_tool(node_id)
    if tool is None:
        raise RuntimeError(f"montage workflow node is unavailable: {node_id}")

    tool_call_id = f"auto_edit_{node_id}_{uuid.uuid4().hex[:8]}"
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=tool_call_id,
        store=artifact_store,
    )
    try:
        if tool.coroutine is None:
            raise RuntimeError(f"montage workflow node `{node_id}` is not async")
        await tool.coroutine(runtime=runtime, **args)
    except Exception as exc:
        raise RuntimeError(
            f"{MONTAGE_WORKFLOW_SKILL} failed at node `{node_id}`: {exc}"
        ) from exc

    artifact_meta = artifact_store.get_latest_meta(
        node_id=node_id,
        session_id=sess.session_id,
    )
    if artifact_meta is None or artifact_meta.created_at < created_after:
        raise RuntimeError(f"{node_id} did not produce a current workflow artifact")

    _, artifact_data = artifact_store.load_result(artifact_meta.artifact_id)
    if not isinstance(artifact_data, dict):
        raise RuntimeError(f"{node_id} produced an invalid artifact")
    output = artifact_data.get("payload")
    if not isinstance(output, dict):
        raise RuntimeError(f"{node_id} output must be an object")

    node_kind = node_manager.id_to_kind.get(node_id, node_id)
    context.workflow_outputs[node_kind] = output
    return output


def latest_render_output_path(sess: Any, *, created_after: Optional[float] = None) -> str:
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


async def save_session_state(store: Any, sess: Any) -> None:
    save = getattr(store, "save_session_state", None)
    if save is not None:
        await save(sess)


async def complete_with_render_output(store: Any, sess: Any, output_path: str) -> None:
    remote_media_id, public_url, upload_error = await asyncio.to_thread(
        upload_result_video_sync,
        sess,
        output_path,
    )

    set_edit_state(
        sess,
        status="completed",
        result_path=output_path,
        public_video_url=public_url,
        result_media_id=remote_media_id,
        upload_error=upload_error,
    )
    await save_session_state(store, sess)


def processing_state_can_be_reconciled(sess: Any) -> bool:
    if get_edit_status(sess) != "processing":
        return False
    lock = getattr(sess, "chat_lock", None)
    if lock is not None and lock.locked():
        return False

    updated_at = float(getattr(sess, "auto_edit_updated_at", 0.0) or 0.0)
    if updated_at <= 0:
        return True
    return (time.time() - updated_at) >= STALE_PROCESSING_GRACE_SECONDS


async def reconcile_auto_edit_state(store: Any, sess: Any) -> None:
    """
    Repair a stale processing state left behind after a background auto-edit task
    has already stopped. This prevents later submissions from being blocked forever.
    """
    if not processing_state_can_be_reconciled(sess):
        return

    output_path = latest_render_output_path(sess)
    if output_path:
        await complete_with_render_output(store, sess, output_path)
        return

    set_edit_state(
        sess,
        status="failed",
        error="previous auto edit task stopped without updating status",
    )
    await save_session_state(store, sess)


def upload_media_service(request: Request) -> UploadMediaService:
    service = getattr(request.app.state, UPLOAD_MEDIA_SERVICE_STATE_KEY, None)
    if service is None:
        raise HTTPException(status_code=500, detail="upload media service is not configured")
    return service


async def run_auto_edit_task(store: Any, sess: Any, payload: AutoEditRequest) -> None:
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
                selected_path = Path(sess.load_media[payload.media_id].path).resolve()
                sess.client_context.selected_media_paths = {str(selected_path)}
                sess.client_context.active_workflow = MONTAGE_WORKFLOW_SKILL
                sess.client_context.workflow_started_at = task_started_at
                sess.client_context.workflow_outputs = {}

            artifact_store = ArtifactStore(
                sess.cfg.project.outputs_dir,
                session_id=sess.session_id,
            )
            for node_id, node_args in montage_node_calls(payload):
                await invoke_montage_node(
                    sess,
                    artifact_store,
                    node_id,
                    node_args,
                    created_after=task_started_at,
                )

            output_path = latest_render_output_path(sess, created_after=task_started_at)
            if not output_path:
                raise RuntimeError("render_video output not found")

            await complete_with_render_output(store, sess, output_path)
            return
    except Exception as exc:
        output_path = latest_render_output_path(sess, created_after=task_started_at)
        if output_path:
            await complete_with_render_output(store, sess, output_path)
            return
        set_edit_state(sess, status="failed", error=format_exception(exc))
    finally:
        if getattr(sess, "client_context", None) is not None:
            sess.client_context.selected_media_paths = None
            sess.client_context.active_workflow = None
            sess.client_context.workflow_started_at = None
            sess.client_context.workflow_outputs = {}
        await save_session_state(store, sess)


async def create_edit_session(request: Request) -> Any:
    store = session_store(request)
    sess = await store.create()
    return sess.snapshot()


async def upload_edit_media(
    session_id: str,
    request: Request,
    files: Optional[List[UploadFile]],
    file: Optional[UploadFile],
) -> Any:
    service = upload_media_service(request)
    return await service(session_id, request, files, file)


async def submit_auto_edit_task(
    session_id: str,
    payload: AutoEditRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> SubmitEditResponse:
    store = session_store(request)
    sess = await store.get_or_404(session_id)

    await reconcile_auto_edit_state(store, sess)

    if get_edit_status(sess) == "processing" or sess.chat_lock.locked():
        raise HTTPException(status_code=409, detail="current edit task is still processing")

    if payload.media_id not in sess.load_media:
        raise HTTPException(status_code=404, detail={"missing_media_ID": payload.media_id})

    set_edit_state(sess, status="processing")
    await save_session_state(store, sess)

    background_tasks.add_task(run_auto_edit_task, store, sess, payload)
    return SubmitEditResponse(status="processing")


async def get_auto_edit_result(session_id: str, request: Request) -> EditResultResponse:
    store = session_store(request)
    sess = await store.get_or_404(session_id)

    await reconcile_auto_edit_state(store, sess)

    status = get_edit_status(sess)
    if status == "failed" and not str(getattr(sess, "auto_edit_result_path", "") or ""):
        output_path = latest_render_output_path(sess)
        if output_path:
            await complete_with_render_output(store, sess, output_path)
            status = get_edit_status(sess)

    if status == "completed":
        result_path = str(getattr(sess, "auto_edit_result_path", "") or "")
        public_video_url = str(getattr(sess, "auto_edit_public_video_url", "") or "")
        remote_media_id = str(getattr(sess, "auto_edit_result_media_id", "") or "")
        upload_error = str(getattr(sess, "auto_edit_upload_error", "") or "")
        if result_path and not public_video_url:
            remote_media_id, public_video_url, upload_error = await asyncio.to_thread(
                upload_result_video_sync,
                sess,
                result_path,
            )
            sess.auto_edit_public_video_url = public_video_url
            sess.auto_edit_result_media_id = remote_media_id
            sess.auto_edit_upload_error = upload_error
            await save_session_state(store, sess)

        return EditResultResponse(
            status=status,
            media_id=remote_media_id or (result_media_id(result_path) if result_path else None),
            video_url=public_video_url or None,
            upload_error=upload_error or None,
        )
    if status == "failed":
        return EditResultResponse(
            status=status,
            error=str(getattr(sess, "auto_edit_error", "") or "auto edit failed"),
        )
    return EditResultResponse(status=status)
