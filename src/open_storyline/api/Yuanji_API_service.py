from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Literal, Optional

import requests
from fastapi import BackgroundTasks, HTTPException, Request, UploadFile
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from open_storyline.storage.agent_memory import ArtifactStore
from open_storyline.usage_billing import read_usage_summary


UploadMediaService = Callable[
    [str, Request, Optional[List[UploadFile]], Optional[UploadFile]],
    Awaitable[Any],
]
UPLOAD_MEDIA_SERVICE_STATE_KEY = "auto_edit_upload_media_service"
STALE_PROCESSING_GRACE_SECONDS = 10.0

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
    video_url: Optional[str] = None
    billing: Optional[dict[str, Any]] = None
    upload_error: Optional[str] = None
    error: Optional[str] = None


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


def merge_system_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
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


def should_route_to_add_subtitle_workflow(payload: AutoEditRequest) -> bool:
    requirement = str(payload.requirement or "").strip().lower()
    text = str(payload.text or "").strip().lower()
    combined = f"{requirement}\n{text}"
    keywords = (
        "添加字幕",
        "加字幕",
        "加上字幕",
        "生成字幕",
        "自动字幕",
        "字幕版",
        "烧录字幕",
        "字幕",
        "subtitle",
        "subtitles",
        "caption",
        "captions",
    )
    return any(keyword in combined for keyword in keywords)


def billing_model_api_keys(cfg: Any) -> dict[str, str]:
    keys: dict[str, str] = {}

    llm_model = str(getattr(getattr(cfg, "llm", None), "model", "") or "").strip()
    llm_key = str(getattr(getattr(cfg, "llm", None), "api_key", "") or "").strip()
    if llm_model and llm_key:
        keys[llm_model] = llm_key

    vlm_model = str(getattr(getattr(cfg, "vlm", None), "model", "") or "").strip()
    vlm_key = str(getattr(getattr(cfg, "vlm", None), "api_key", "") or "").strip()
    if vlm_model and vlm_key:
        keys[vlm_model] = vlm_key

    ai_transition = getattr(cfg, "generate_ai_transition", None)
    providers = getattr(ai_transition, "providers", None) or {}
    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            continue
        model = str(provider_cfg.get("model_name") or provider_cfg.get("model") or "").strip()
        api_key = str(provider_cfg.get("api_key") or "").strip()
        if model and api_key:
            keys[model] = api_key

    return keys


def build_auto_edit_prompt(payload: AutoEditRequest) -> str:
    media_list = "\n".join(f"- {media_id}" for media_id in payload.media_ids)
    matched_workflow = ""
    if should_route_to_add_subtitle_workflow(payload):
        matched_workflow = (
            "当前已预匹配到专项 WORKFLOW SKILL："
            "`.storyline/skills/add_subtitle_workflow_skill/SKILL.md` 对应的 "
            "`add_subtitle_workflow_skill`。\n"
        )
    return (
        "【工作流路由规则】在调用任何剪辑 Node/tool 之前，必须先判断是否有可用的 "
        "【WORKFLOW SKILL】适合当前任务。若有匹配的专项 WORKFLOW SKILL，第一步必须先调用该 skill，"
        "然后严格按照该 skill 中定义的工具顺序执行；若没有任何专项 WORKFLOW SKILL 匹配，"
        "第一步必须调用 `default_editing_workflow_skill`，再按默认工作流执行。"
        "禁止在未调用 WORKFLOW SKILL 的情况下直接调用 `load_media`、`split_shots`、"
        "`local_asr`、`render_video` 等剪辑 Node/tool。\n"
        f"{matched_workflow}\n"
        "当前请求来自自动剪辑 REST API。请不要停留在剪辑计划确认阶段，"
        "请直接根据用户文案、剪辑要求和已上传素材完成自动剪辑，并在最终阶段调用 render_video 输出成片。\n\n"
        f"用户文案：\n{payload.text}\n\n"
        f"剪辑要求：\n{payload.requirement}\n\n"
        f"本次任务使用的素材 ID：\n{media_list}"
    )


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

            prompt = build_auto_edit_prompt(payload)
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
            await save_session_state(store, sess)

            messages = list(getattr(sess, "lc_messages", []) or [])
            messages.append(
                SystemMessage(
                    content=(
                        "自动剪辑 API 模式：不要等待用户确认剪辑计划；"
                        "直接分析需求、调用必要 MCP/Node，并生成最终视频。"
                    )
                )
            )
            invoke_messages = merge_system_messages(messages)

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

    missing_media_ids = [media_id for media_id in payload.media_ids if media_id not in sess.load_media]
    if missing_media_ids:
        raise HTTPException(status_code=404, detail={"missing_media_ids": missing_media_ids})

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

    billing_end_at = float(getattr(sess, "auto_edit_updated_at", 0.0) or 0.0)
    if status == "processing":
        billing_end_at = time.time()
    billing = read_usage_summary(
        sess.cfg.project.outputs_dir,
        session_id,
        currency=str(getattr(sess.cfg.billing, "currency", "USD") or "USD"),
        billing_cfg=getattr(sess.cfg, "billing", None),
        model_api_keys=billing_model_api_keys(sess.cfg),
        started_at=float(getattr(sess, "auto_edit_started_at", 0.0) or 0.0),
        ended_at=billing_end_at,
    )
    sess.auto_edit_billing = billing
    await save_session_state(store, sess)

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
            billing=billing,
            upload_error=upload_error or None,
        )
    if status == "failed":
        return EditResultResponse(
            status=status,
            billing=billing,
            error=str(getattr(sess, "auto_edit_error", "") or "auto edit failed"),
        )
    return EditResultResponse(status=status, billing=billing)

