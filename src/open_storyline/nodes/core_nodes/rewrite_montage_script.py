import re
from typing import Any, Dict

from open_storyline.nodes.core_nodes.base_node import BaseNode, NodeMeta
from open_storyline.nodes.node_schema import (
    RewriteMontageScriptInput,
    RewriteMontageScriptOutput,
)
from open_storyline.nodes.node_state import NodeState
from open_storyline.utils.parse_json import parse_json_dict
from open_storyline.utils.prompts import get_prompt
from open_storyline.utils.register import NODE_REGISTRY


@NODE_REGISTRY.register()
class RewriteMontageScriptNode(BaseNode):
    meta = NodeMeta(
        name="rewrite_montage_script",
        description=(
            "Rewrite a user-provided script with reference to analyzed uploaded footage, then split "
            "it into uploaded-footage and AI-generated storyboard segments using the configured LLM."
        ),
        node_id="rewrite_montage_script",
        node_kind="rewrite_montage_script",
        require_prior_kind=["understand_media"],
        default_require_prior_kind=[],
        next_available_node=["match_montage_segments"],
    )

    input_schema = RewriteMontageScriptInput
    output_schema = RewriteMontageScriptOutput

    async def default_process(
        self,
        node_state: NodeState,
        inputs: Dict[str, Any],
    ) -> Any:
        script = _require_script(inputs)
        segments = _build_default_segments(script, node_state.lang)
        node_state.node_summary.info_for_user(
            f"Script retained and split into {len(segments)} storyboard segment(s) without LLM rewriting"
        )
        return _build_output(script, script, segments)

    async def process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Any:
        script = _require_script(inputs)
        clip_captions, overall = _load_material_context(inputs)
        material_block = _build_material_block(clip_captions, node_state.lang)
        available_clip_ids = {
            item["clip_id"]
            for item in clip_captions
            if isinstance(item.get("clip_id"), str) and item["clip_id"]
        }

        system_prompt = get_prompt(
            "rewrite_montage_script.system",
            lang=node_state.lang,
        )
        user_prompt = get_prompt(
            "rewrite_montage_script.user",
            lang=node_state.lang,
            script=script,
            overall=overall,
            uploaded_materials=material_block,
        )

        llm_cfg = self.server_cfg.llm
        raw = await node_state.llm.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            media=None,
            temperature=llm_cfg.temperature if llm_cfg.temperature is not None else 0.1,
            top_p=0.9,
            max_tokens=4096,
            model_preferences=None,
        )

        try:
            obj = parse_json_dict(raw)
            rewritten_script, segments = _normalize_llm_output(obj, available_clip_ids)
        except Exception as exc:
            node_state.node_summary.info_for_llm(
                f"rewrite montage script failed: {type(exc).__name__}: {exc}"
            )
            raise ValueError("LLM returned an invalid rewritten script or storyboard structure") from exc

        uploaded_count = sum(segment["source_type"] == "uploaded" for segment in segments)
        generated_count = len(segments) - uploaded_count
        node_state.node_summary.info_for_user(
            f"Script rewritten into {len(segments)} storyboard segment(s): "
            f"{uploaded_count} uploaded, {generated_count} generated"
        )
        return _build_output(script, rewritten_script, segments)


def _require_script(inputs: Dict[str, Any]) -> str:
    script = inputs.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("script must be a non-empty string")
    return script.strip()


def _load_material_context(inputs: Dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    understood = inputs.get("understand_media") or inputs.get("understand_clips") or {}
    media_captions = understood.get("media_captions") or understood.get("clip_captions") or []
    if not isinstance(media_captions, list):
        media_captions = []
    media_captions = [item for item in media_captions if isinstance(item, dict)]

    overall = understood.get("overall") or ""
    if not isinstance(overall, str):
        overall = str(overall)
    return media_captions, overall.strip()


def _build_material_block(clip_captions: list[dict[str, Any]], lang: str) -> str:
    no_material_message = (
        "没有可用的上传素材，所有分镜都必须使用 AI 生成视频。"
        if lang == "zh"
        else "No uploaded media assets are available. All storyboard segments must use generated video."
    )
    if not clip_captions:
        return no_material_message

    lines: list[str] = []
    for item in clip_captions:
        clip_id = str(item.get("clip_id") or item.get("media_id") or "").strip()
        if not clip_id:
            continue
        caption = str(item.get("caption") or "").strip()[:300]
        aes_score = item.get("aes_score")
        line = f"- clip_id={clip_id}; media_id={clip_id}; caption={caption or 'unknown'}"
        if aes_score is not None:
            line += f"; aes_score={aes_score}"
        lines.append(line)

    if not lines:
        return no_material_message
    return "\n".join(lines)


def _normalize_llm_output(
    obj: Dict[str, Any],
    available_clip_ids: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    rewritten_script = obj.get("rewritten_script")
    if not isinstance(rewritten_script, str) or not rewritten_script.strip():
        raise ValueError("missing rewritten_script")

    raw_segments = obj.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("segments must be a non-empty list")

    required_text_fields = (
        "text",
        "visual_intent",
        "tone",
        "camera_motion",
        "continuity_hint",
    )
    segments: list[dict[str, Any]] = []

    for index, item in enumerate(raw_segments, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"segments[{index - 1}] must be an object")

        normalized: dict[str, Any] = {"segment_id": f"segment_{index:04d}"}
        for field_name in required_text_fields:
            value = item.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"segments[{index - 1}].{field_name} must be a non-empty string")
            normalized[field_name] = value.strip()

        duration = item.get("duration")
        if isinstance(duration, bool):
            raise ValueError(f"segments[{index - 1}].duration must be numeric")
        try:
            duration = round(float(duration), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"segments[{index - 1}].duration must be numeric") from exc
        if duration <= 0 or duration > 30:
            raise ValueError(f"segments[{index - 1}].duration must be within (0, 30] seconds")
        normalized["duration"] = duration

        source_type = item.get("source_type")
        if source_type not in {"uploaded", "generated"}:
            raise ValueError(
                f"segments[{index - 1}].source_type must be uploaded or generated"
            )

        raw_clip_ids = item.get("clip_ids") or []
        if not isinstance(raw_clip_ids, list):
            raise ValueError(f"segments[{index - 1}].clip_ids must be a list")
        clip_ids = list(
            dict.fromkeys(
                clip_id.strip()
                for clip_id in raw_clip_ids
                if isinstance(clip_id, str) and clip_id.strip()
            )
        )
        invalid_clip_ids = [clip_id for clip_id in clip_ids if clip_id not in available_clip_ids]
        if invalid_clip_ids:
            raise ValueError(
                f"segments[{index - 1}] contains unavailable clip_ids: {invalid_clip_ids}"
            )

        generation_prompt = item.get("generation_prompt") or ""
        if not isinstance(generation_prompt, str):
            raise ValueError(f"segments[{index - 1}].generation_prompt must be a string")
        generation_prompt = generation_prompt.strip()

        if source_type == "uploaded":
            if not clip_ids:
                raise ValueError(f"segments[{index - 1}] uploaded segment requires clip_ids")
            generation_prompt = ""
        else:
            if clip_ids:
                raise ValueError(f"segments[{index - 1}] generated segment cannot contain clip_ids")
            if not generation_prompt:
                raise ValueError(
                    f"segments[{index - 1}] generated segment requires generation_prompt"
                )

        normalized["source_type"] = source_type
        normalized["clip_ids"] = clip_ids
        normalized["generation_prompt"] = generation_prompt
        segments.append(normalized)

    return rewritten_script.strip(), segments


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


def _build_default_segments(script: str, lang: str) -> list[dict[str, Any]]:
    parts = [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(script) if part.strip()]
    if not parts:
        parts = [script]

    if lang == "zh":
        visual_prefix = "直接呈现该段脚本的主体、场景和动作："
        tone = "自然、连贯"
        camera_motion = "稳定镜头，根据主体动作使用缓慢推拉或跟随"
        continuity_hint = "延续相邻分镜的主体、动作方向、光线和色调"
    else:
        visual_prefix = "Directly show the subject, setting, and action described by: "
        tone = "Natural and coherent"
        camera_motion = "Stable framing with a slow push, pull, or follow when motivated by action"
        continuity_hint = "Maintain subject, action direction, lighting, and color across adjacent shots"

    return [
        {
            "segment_id": f"segment_{index:04d}",
            "text": text,
            "visual_intent": f"{visual_prefix}{text}",
            "duration": _estimate_duration(text),
            "tone": tone,
            "camera_motion": camera_motion,
            "continuity_hint": continuity_hint,
            "source_type": "generated",
            "clip_ids": [],
            "generation_prompt": f"{visual_prefix}{text}",
        }
        for index, text in enumerate(parts, start=1)
    ]


def _estimate_duration(text: str) -> float:
    word_count = len(text.split())
    if word_count > 1:
        return round(min(8.0, max(2.0, word_count / 2.5)), 2)
    return round(min(8.0, max(2.0, len(text) / 4.0)), 2)


def _build_output(
    original_script: str,
    rewritten_script: str,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    output = {
        "original_script": original_script,
        "rewritten_script": rewritten_script,
        "segments": segments,
        "total_duration": round(sum(segment["duration"] for segment in segments), 2),
    }
    return RewriteMontageScriptOutput.model_validate(output).model_dump()
