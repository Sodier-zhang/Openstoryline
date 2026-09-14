import copy
from pathlib import Path
from typing import Any, Dict, Optional

from moviepy import VideoFileClip
from PIL import Image, ImageOps

from open_storyline.nodes.core_nodes.generate_ai_transition import (
    GenerateAITransitionNode,
    encode_image_to_data_url,
)
from open_storyline.nodes.core_nodes.base_node import NodeMeta
from open_storyline.nodes.node_schema import (
    GenerateMontageVideoInput,
    GenerateMontageVideoOutput,
)
from open_storyline.nodes.node_state import NodeState
from open_storyline.utils.ai_transition_cancel import is_ai_transition_cancelled
from open_storyline.utils.register import NODE_REGISTRY


@NODE_REGISTRY.register()
class GenerateMontageVideoNode(GenerateAITransitionNode):
    meta = NodeMeta(
        name="generate_montage_video",
        description=(
            "Generate missing or supplemental montage video clips for generated and mixed "
            "storyboard segments using the existing configured video-generation provider."
        ),
        node_id="generate_montage_video",
        node_kind="generate_montage_video",
        require_prior_kind=["match_montage_segments", "split_shots"],
        default_require_prior_kind=["match_montage_segments", "split_shots"],
        next_available_node=["plan_timeline_pro"],
    )

    input_schema = GenerateMontageVideoInput
    output_schema = GenerateMontageVideoOutput

    async def default_process(
        self,
        node_state: NodeState,
        inputs: Dict[str, Any],
    ) -> Any:
        match_result, segments = _load_match_result(inputs)
        original_clips = _build_original_clip_map(inputs)
        return _build_output(match_result, segments, original_clips, [])

    async def process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Any:
        match_result, segments = _load_match_result(inputs)
        original_clips = _build_original_clip_map(inputs)
        targets = [
            segment
            for segment in segments
            if segment["source_type"] in {"generated", "mixed"}
        ]

        if not targets:
            node_state.node_summary.info_for_user(
                "All montage segments are covered by uploaded footage; no video generation needed"
            )
            return _build_output(match_result, segments, original_clips, [])

        runtime_cfg = self._resolve_ai_transition_runtime_cfg(inputs)
        output_dir = self._prepare_output_directory(node_state)
        generated_clips: list[dict[str, Any]] = []
        generated_by_segment: dict[str, dict[str, Any]] = {}
        target_index = 0

        await _report_progress(
            node_state,
            0,
            len(targets),
            "Montage video generation starting...",
        )

        for segment_index, segment in enumerate(segments):
            if segment["source_type"] not in {"generated", "mixed"}:
                continue

            target_index += 1
            self._raise_if_cancelled(node_state)
            first_anchor = _find_previous_anchor(
                segments,
                segment_index,
                original_clips,
                generated_by_segment,
                include_current_uploaded=segment["source_type"] == "mixed",
            )
            last_anchor = _find_next_uploaded_anchor(
                segments,
                segment_index,
                original_clips,
            )
            first_frame = _load_anchor_frame(first_anchor, at_end=True)
            last_frame = _load_anchor_frame(last_anchor, at_end=False)
            first_frame, last_frame = _align_anchor_frames(self, first_frame, last_frame)

            prompt = _build_continuous_prompt(segments, segment_index)
            requested_duration = inputs.get("duration")
            resolution = inputs.get("resolution")
            generated_path, _response, effective_duration = self._generate_video(
                provider=runtime_cfg["provider"],
                api_key=runtime_cfg["api_key"],
                model_name=runtime_cfg["model_name"],
                prompt=prompt,
                first_frame_data_url=(
                    encode_image_to_data_url(first_frame) if first_frame is not None else None
                ),
                last_frame_data_url=(
                    encode_image_to_data_url(last_frame) if last_frame is not None else None
                ),
                duration=requested_duration,
                resolution=resolution,
                output_dir=output_dir,
                cancel_checker=lambda: is_ai_transition_cancelled(
                    self.server_cache_dir,
                    node_state.session_id,
                ),
            )

            clip_id = f"montage_generated_{target_index:04d}"
            generated_clip = _inspect_generated_clip(
                path=generated_path,
                clip_id=clip_id,
                segment=segment,
                fallback_duration=effective_duration,
                prompt=prompt,
            )
            generated_clips.append(generated_clip)
            generated_by_segment[segment["segment_id"]] = generated_clip
            segment["generated_clip_ids"] = [clip_id]

            await _report_progress(
                node_state,
                target_index,
                len(targets),
                f"Montage video {target_index}/{len(targets)} generated",
            )
            node_state.node_summary.info_for_user(
                f"Generated video for {segment['segment_id']}",
                preview_urls=[generated_path],
            )

        return _build_output(match_result, segments, original_clips, generated_clips)


def _load_match_result(inputs: Dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = inputs.get("match_montage_segments") or {}
    if not isinstance(result, dict):
        raise ValueError("match_montage_segments output must be an object")
    segments = result.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("match_montage_segments output has no segments")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_segment in enumerate(segments):
        if not isinstance(raw_segment, dict):
            raise ValueError(f"matched segment {index} must be an object")
        segment = copy.deepcopy(raw_segment)
        segment_id = segment.get("segment_id")
        source_type = segment.get("source_type")
        if not isinstance(segment_id, str) or not segment_id or segment_id in seen_ids:
            raise ValueError(f"matched segment {index} has an invalid or duplicate segment_id")
        if source_type not in {"uploaded", "generated", "mixed"}:
            raise ValueError(f"matched segment {segment_id} has invalid source_type")
        if source_type in {"generated", "mixed"} and not str(
            segment.get("generation_prompt") or ""
        ).strip():
            raise ValueError(f"matched segment {segment_id} requires generation_prompt")
        seen_ids.add(segment_id)
        segment["generated_clip_ids"] = []
        normalized.append(segment)
    return result, normalized


def _build_original_clip_map(inputs: Dict[str, Any]) -> dict[str, dict[str, Any]]:
    split_result = inputs.get("split_shots") or {}
    clips = (split_result.get("clips") or []) if isinstance(split_result, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for clip in clips if isinstance(clips, list) else []:
        if not isinstance(clip, dict):
            continue
        clip_id = str(clip.get("clip_id") or "").strip()
        if clip_id:
            result[clip_id] = copy.deepcopy(clip)
    return result


def _segment_uploaded_clips(
    segment: dict[str, Any],
    original_clips: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    clip_ids = segment.get("clip_ids") or []
    missing = [clip_id for clip_id in clip_ids if clip_id not in original_clips]
    if missing:
        raise ValueError(
            f"{segment['segment_id']} references clips missing from split_shots: {missing}"
        )
    return [original_clips[clip_id] for clip_id in clip_ids]


def _find_previous_anchor(
    segments: list[dict[str, Any]],
    current_index: int,
    original_clips: dict[str, dict[str, Any]],
    generated_by_segment: dict[str, dict[str, Any]],
    *,
    include_current_uploaded: bool,
) -> Optional[dict[str, Any]]:
    if include_current_uploaded:
        current_uploaded = _segment_uploaded_clips(segments[current_index], original_clips)
        if current_uploaded:
            return current_uploaded[-1]

    for index in range(current_index - 1, -1, -1):
        segment = segments[index]
        generated = generated_by_segment.get(segment["segment_id"])
        if generated:
            return generated
        uploaded = _segment_uploaded_clips(segment, original_clips)
        if uploaded:
            return uploaded[-1]
    return None


def _find_next_uploaded_anchor(
    segments: list[dict[str, Any]],
    current_index: int,
    original_clips: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    for segment in segments[current_index + 1 :]:
        uploaded = _segment_uploaded_clips(segment, original_clips)
        if uploaded:
            return uploaded[0]
    return None


def _load_anchor_frame(
    clip: Optional[dict[str, Any]],
    *,
    at_end: bool,
) -> Optional[Image.Image]:
    if not clip:
        return None
    path = Path(str(clip.get("path") or "")).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Anchor media not found: {path}")

    if str(clip.get("kind") or "").lower() == "image":
        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).convert("RGB")

    source_ref = clip.get("source_ref") or {}
    with VideoFileClip(str(path), audio=False) as video:
        source_start = max(0.0, float(source_ref.get("start", 0) or 0) / 1000.0)
        source_end_raw = source_ref.get("end")
        if source_end_raw is None:
            source_end = source_start + float(source_ref.get("duration", 0) or 0) / 1000.0
        else:
            source_end = float(source_end_raw or 0) / 1000.0
        video_duration = float(video.duration or 0.0)
        if at_end:
            frame_time = min(source_end, video_duration) - max(0.04, 1.0 / float(video.fps or 25))
        else:
            frame_time = source_start
        frame_time = max(0.0, min(frame_time, max(0.0, video_duration - 0.001)))
        return Image.fromarray(video.get_frame(frame_time)).convert("RGB")


def _align_anchor_frames(
    node: GenerateMontageVideoNode,
    first_frame: Optional[Image.Image],
    last_frame: Optional[Image.Image],
) -> tuple[Optional[Image.Image], Optional[Image.Image]]:
    if first_frame is not None and last_frame is not None:
        first_frame, last_frame, _, _ = node._preprocess_first_last_frame(
            first_frame,
            last_frame,
        )
    return first_frame, last_frame


def _build_continuous_prompt(segments: list[dict[str, Any]], index: int) -> str:
    segment = segments[index]
    previous_hint = segments[index - 1].get("continuity_hint", "") if index > 0 else ""
    next_hint = (
        segments[index + 1].get("continuity_hint", "")
        if index + 1 < len(segments)
        else ""
    )
    return (
        f"{segment['generation_prompt']}\n"
        f"Current script text: {segment.get('text', '')}\n"
        f"Current storyboard: {segment.get('visual_intent', '')}\n"
        f"Previous continuity: {previous_hint}\n"
        f"Current continuity: {segment.get('continuity_hint', '')}\n"
        f"Next continuity: {next_hint}\n"
        "Keep the subject identity, color palette, aspect ratio, action direction, camera movement, "
        "lighting, and spatial relationship coherent with adjacent shots. Do not add any person, "
        "event, theme, product, place, or brand that is absent from the script and storyboard."
    ).strip()


def _inspect_generated_clip(
    *,
    path: str,
    clip_id: str,
    segment: dict[str, Any],
    fallback_duration: int,
    prompt: str,
) -> dict[str, Any]:
    with VideoFileClip(str(path), audio=False) as video:
        fps = float(video.fps or 0.0)
        width, height = map(int, video.size)
        duration_ms = int(round(float(video.duration or fallback_duration or 0) * 1000))
    if duration_ms <= 0:
        raise ValueError(f"Generated video for {segment['segment_id']} has invalid duration")
    return {
        "clip_id": clip_id,
        "kind": "video",
        "path": str(path),
        "fps": fps,
        "source_ref": {
            "media_id": f"montage_generated_media_{clip_id.rsplit('_', 1)[-1]}",
            "start": 0,
            "end": duration_ms,
            "duration": duration_ms,
            "width": width,
            "height": height,
        },
        "segment_id": segment["segment_id"],
        "generation_prompt": prompt,
    }


async def _report_progress(
    node_state: NodeState,
    progress: int,
    total: int,
    message: str,
) -> None:
    context = node_state.mcp_ctx
    if context is not None and hasattr(context, "report_progress"):
        await context.report_progress(progress, total, message)


def _build_output(
    match_result: dict[str, Any],
    segments: list[dict[str, Any]],
    original_clips: dict[str, dict[str, Any]],
    generated_clips: list[dict[str, Any]],
) -> dict[str, Any]:
    generated_lookup = {clip["clip_id"]: clip for clip in generated_clips}
    selected_clips: dict[str, dict[str, Any]] = {}
    groups: list[dict[str, Any]] = []

    for segment in segments:
        ordered_clip_ids: list[str] = []
        for clip in _segment_uploaded_clips(segment, original_clips):
            clip_id = clip["clip_id"]
            selected_clips.setdefault(clip_id, copy.deepcopy(clip))
            ordered_clip_ids.append(clip_id)
        for clip_id in segment.get("generated_clip_ids") or []:
            generated = generated_lookup.get(clip_id)
            if generated is None:
                raise ValueError(f"Generated clip not found: {clip_id}")
            selected_clips.setdefault(clip_id, copy.deepcopy(generated))
            ordered_clip_ids.append(clip_id)

        groups.append(
            {
                "group_id": segment["segment_id"],
                "summary": segment.get("visual_intent", ""),
                "clip_ids": ordered_clip_ids,
                "duration": float(segment.get("duration", 0) or 0),
            }
        )

    output = {
        "original_script": str(match_result.get("original_script") or ""),
        "rewritten_script": str(match_result.get("rewritten_script") or ""),
        "segments": segments,
        "clips": list(selected_clips.values()),
        "generated_clips": generated_clips,
        "groups": groups,
        "total_duration": round(sum(group["duration"] for group in groups), 2),
    }
    return GenerateMontageVideoOutput.model_validate(output).model_dump()
