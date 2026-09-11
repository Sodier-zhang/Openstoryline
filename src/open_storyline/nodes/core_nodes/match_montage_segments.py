import json
from typing import Any, Dict

from open_storyline.nodes.core_nodes.base_node import BaseNode, NodeMeta
from open_storyline.nodes.node_schema import (
    MatchMontageSegmentsInput,
    MatchMontageSegmentsOutput,
)
from open_storyline.nodes.node_state import NodeState
from open_storyline.utils.parse_json import parse_json_dict
from open_storyline.utils.prompts import get_prompt
from open_storyline.utils.register import NODE_REGISTRY


@NODE_REGISTRY.register()
class MatchMontageSegmentsNode(BaseNode):
    meta = NodeMeta(
        name="match_montage_segments",
        description=(
            "Match rewritten montage storyboard segments to real uploaded clips, mark unmatched "
            "segments for generation, and support mixed real/generated segments."
        ),
        node_id="match_montage_segments",
        node_kind="match_montage_segments",
        require_prior_kind=["rewrite_montage_script", "understand_clips"],
        default_require_prior_kind=["rewrite_montage_script", "understand_clips"],
        next_available_node=["generate_montage_video"],
    )

    input_schema = MatchMontageSegmentsInput
    output_schema = MatchMontageSegmentsOutput

    async def default_process(
        self,
        node_state: NodeState,
        inputs: Dict[str, Any],
    ) -> Any:
        rewrite_result, segments = _load_rewrite_result(inputs)
        clip_lookup = _build_clip_lookup(inputs)
        matched = _apply_existing_decisions(segments, clip_lookup, node_state.lang)
        matched = _enforce_unique_clip_usage(matched, clip_lookup, node_state.lang)
        return _build_output(rewrite_result, matched)

    async def process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Any:
        rewrite_result, segments = _load_rewrite_result(inputs)
        clip_lookup = _build_clip_lookup(inputs)

        if not clip_lookup:
            matched = _mark_all_generated(segments, node_state.lang)
            node_state.node_summary.info_for_user(
                f"No valid uploaded clips found; marked all {len(matched)} segment(s) for generation"
            )
            return _build_output(rewrite_result, matched)

        system_prompt = get_prompt("match_montage_segments.system", lang=node_state.lang)
        user_prompt = get_prompt(
            "match_montage_segments.user",
            lang=node_state.lang,
            rewritten_script=rewrite_result["rewritten_script"],
            segments=json.dumps(segments, ensure_ascii=False, indent=2),
            uploaded_overall=_load_uploaded_overall(inputs),
            uploaded_clips=_build_clip_block(clip_lookup),
        )

        llm_cfg = self.server_cfg.llm
        max_tokens = min(8192, max(2048, 1024 + len(segments) * 320))
        raw = await node_state.llm.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            media=None,
            temperature=llm_cfg.temperature if llm_cfg.temperature is not None else 0.1,
            top_p=0.9,
            max_tokens=max_tokens,
            model_preferences=None,
        )

        try:
            decisions = _extract_decisions(parse_json_dict(raw))
            matched = _merge_decisions(
                segments,
                decisions,
                clip_lookup,
                lang=node_state.lang,
            )
            matched = _enforce_unique_clip_usage(matched, clip_lookup, node_state.lang)
        except Exception as exc:
            node_state.node_summary.info_for_llm(
                f"match montage segments failed: {type(exc).__name__}: {exc}"
            )
            raise ValueError("LLM returned an invalid montage segment matching result") from exc

        counts = {
            source_type: sum(item["source_type"] == source_type for item in matched)
            for source_type in ("uploaded", "generated", "mixed")
        }
        node_state.node_summary.info_for_user(
            f"Matched {len(matched)} segment(s): {counts['uploaded']} uploaded, "
            f"{counts['generated']} generated, {counts['mixed']} mixed"
        )
        return _build_output(rewrite_result, matched)


def _load_rewrite_result(inputs: Dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = inputs.get("rewrite_montage_script") or {}
    if not isinstance(result, dict):
        raise ValueError("rewrite_montage_script output must be an object")

    rewritten_script = result.get("rewritten_script")
    segments = result.get("segments")
    if not isinstance(rewritten_script, str) or not rewritten_script.strip():
        raise ValueError("rewrite_montage_script output is missing rewritten_script")
    if not isinstance(segments, list) or not segments:
        raise ValueError("rewrite_montage_script output has no storyboard segments")

    required_fields = (
        "segment_id",
        "text",
        "visual_intent",
        "duration",
        "tone",
        "camera_motion",
        "continuity_hint",
    )
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            raise ValueError(f"rewrite segment {index} must be an object")
        for field_name in required_fields:
            if field_name not in item:
                raise ValueError(f"rewrite segment {index} is missing {field_name}")
        segment_id = item["segment_id"]
        if not isinstance(segment_id, str) or not segment_id or segment_id in seen_ids:
            raise ValueError(f"rewrite segment {index} has an invalid or duplicate segment_id")
        seen_ids.add(segment_id)
        normalized.append(dict(item))
    return result, normalized


def _build_clip_lookup(inputs: Dict[str, Any]) -> dict[str, dict[str, Any]]:
    understood = inputs.get("understand_clips") or {}
    captions = (understood.get("clip_captions") or []) if isinstance(understood, dict) else []
    lookup: dict[str, dict[str, Any]] = {}

    for item in captions if isinstance(captions, list) else []:
        if not isinstance(item, dict):
            continue
        clip_id = str(item.get("clip_id") or "").strip()
        source_ref = item.get("source_ref") or {}
        media_id = str(source_ref.get("media_id") or "").strip() if isinstance(source_ref, dict) else ""
        caption = str(item.get("caption") or "").strip()
        if not clip_id or not media_id or not caption or caption.startswith("Error:"):
            continue
        lookup[clip_id] = {
            "clip_id": clip_id,
            "media_id": media_id,
            "caption": caption[:300],
            "aes_score": item.get("aes_score"),
        }
    return lookup


def _load_uploaded_overall(inputs: Dict[str, Any]) -> str:
    understood = inputs.get("understand_clips") or {}
    if not isinstance(understood, dict):
        return ""
    overall = understood.get("overall") or ""
    return overall.strip() if isinstance(overall, str) else str(overall)


def _build_clip_block(clip_lookup: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in clip_lookup.values():
        line = (
            f"- clip_id={item['clip_id']}; media_id={item['media_id']}; "
            f"caption={item['caption']}"
        )
        if item.get("aes_score") is not None:
            line += f"; aes_score={item['aes_score']}"
        lines.append(line)
    return "\n".join(lines)


def _extract_decisions(obj: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = obj.get("matches")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("matches must be a non-empty list")
    return decisions


def _merge_decisions(
    segments: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    clip_lookup: dict[str, dict[str, Any]],
    *,
    lang: str = "en",
) -> list[dict[str, Any]]:
    segment_ids = [item["segment_id"] for item in segments]
    decision_map: dict[str, dict[str, Any]] = {}

    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ValueError(f"matches[{index}] must be an object")
        segment_id = decision.get("segment_id")
        if segment_id not in segment_ids:
            raise ValueError(f"matches[{index}] contains an unknown segment_id")
        if segment_id in decision_map:
            raise ValueError(f"duplicate matching decision for {segment_id}")
        decision_map[segment_id] = decision

    missing_ids = [segment_id for segment_id in segment_ids if segment_id not in decision_map]
    if missing_ids:
        raise ValueError(f"matching decisions are missing segments: {missing_ids}")

    return [
        _merge_single_decision(
            segment,
            decision_map[segment["segment_id"]],
            clip_lookup,
            lang=lang,
        )
        for segment in segments
    ]


def _merge_single_decision(
    segment: dict[str, Any],
    decision: dict[str, Any],
    clip_lookup: dict[str, dict[str, Any]],
    *,
    lang: str = "en",
) -> dict[str, Any]:
    source_type = decision.get("source_type")
    if source_type not in {"uploaded", "generated", "mixed"}:
        raise ValueError(f"{segment['segment_id']} has invalid source_type")

    raw_clip_ids = decision.get("clip_ids") or []
    if not isinstance(raw_clip_ids, list):
        raise ValueError(f"{segment['segment_id']}.clip_ids must be a list")
    clip_ids = list(
        dict.fromkeys(
            value.strip()
            for value in raw_clip_ids
            if isinstance(value, str) and value.strip()
        )
    )
    invalid_ids = [clip_id for clip_id in clip_ids if clip_id not in clip_lookup]
    if invalid_ids:
        raise ValueError(f"{segment['segment_id']} contains unavailable clip_ids: {invalid_ids}")

    generation_prompt = decision.get("generation_prompt") or ""
    match_reason = decision.get("match_reason") or ""
    if not isinstance(generation_prompt, str) or not isinstance(match_reason, str):
        raise ValueError(f"{segment['segment_id']} contains invalid text fields")
    generation_prompt = generation_prompt.strip()
    match_reason = match_reason.strip()
    if not match_reason:
        reasons = (
            {
                "uploaded": "已校验的上传素材能够覆盖该分镜。",
                "generated": "没有已校验的上传素材能够覆盖该分镜。",
                "mixed": "上传素材只能覆盖该分镜的一部分。",
            }
            if lang == "zh"
            else {
                "uploaded": "Validated uploaded footage covers this storyboard segment.",
                "generated": "No validated uploaded footage covers this storyboard segment.",
                "mixed": "Uploaded footage covers only part of this storyboard segment.",
            }
        )
        match_reason = reasons[source_type]

    if source_type == "uploaded":
        if not clip_ids:
            raise ValueError(f"{segment['segment_id']} uploaded segment requires clip_ids")
        generation_prompt = ""
    elif source_type == "generated":
        if clip_ids:
            raise ValueError(f"{segment['segment_id']} generated segment cannot contain clip_ids")
        if not generation_prompt:
            generation_prompt = _generation_prompt(segment, lang)
    else:
        if not clip_ids:
            raise ValueError(
                f"{segment['segment_id']} mixed segment requires clip_ids"
            )
        if not generation_prompt:
            generation_prompt = _generation_prompt(segment, lang)

    media_ids = list(dict.fromkeys(clip_lookup[clip_id]["media_id"] for clip_id in clip_ids))
    result = {
        key: segment[key]
        for key in (
            "segment_id",
            "text",
            "visual_intent",
            "duration",
            "tone",
            "camera_motion",
            "continuity_hint",
        )
    }
    result.update(
        {
            "source_type": source_type,
            "media_ids": media_ids,
            "clip_ids": clip_ids,
            "generation_prompt": generation_prompt,
            "match_reason": match_reason,
        }
    )
    return result


def _enforce_unique_clip_usage(
    segments: list[dict[str, Any]],
    clip_lookup: dict[str, dict[str, Any]],
    lang: str,
) -> list[dict[str, Any]]:
    used_clip_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for raw_segment in segments:
        segment = dict(raw_segment)
        original_clip_ids = segment.get("clip_ids") or []
        clip_ids = [
            clip_id
            for clip_id in original_clip_ids
            if clip_id not in used_clip_ids
        ]
        removed_duplicates = len(clip_ids) != len(original_clip_ids)
        used_clip_ids.update(clip_ids)
        segment["clip_ids"] = clip_ids
        segment["media_ids"] = list(
            dict.fromkeys(clip_lookup[clip_id]["media_id"] for clip_id in clip_ids)
        )

        if removed_duplicates and not clip_ids:
            segment["source_type"] = "generated"
            segment["generation_prompt"] = _generation_prompt(segment, lang)
            segment["match_reason"] = (
                "匹配素材已被前序分镜使用，为避免重复画面，改用 AI 生成。"
                if lang == "zh"
                else "The matching clip is already used by an earlier segment; generate this shot to avoid repetition."
            )

        normalized.append(segment)

    return normalized


def _apply_existing_decisions(
    segments: list[dict[str, Any]],
    clip_lookup: dict[str, dict[str, Any]],
    lang: str,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for segment in segments:
        clip_ids = [
            clip_id
            for clip_id in segment.get("clip_ids") or []
            if clip_id in clip_lookup
        ]
        source_type = "uploaded" if clip_ids else "generated"
        prompt = "" if clip_ids else _generation_prompt(segment, lang)
        reason = (
            "保留脚本改写阶段匹配的有效上传素材。"
            if clip_ids and lang == "zh"
            else "Keep valid uploaded footage selected during script rewriting."
            if clip_ids
            else "没有经过校验的匹配素材，使用 AI 生成。"
            if lang == "zh"
            else "No validated matching footage is available; use AI generation."
        )
        matched.append(
            _merge_single_decision(
                segment,
                {
                    "source_type": source_type,
                    "clip_ids": clip_ids,
                    "generation_prompt": prompt,
                    "match_reason": reason,
                },
                clip_lookup,
            )
        )
    return matched


def _mark_all_generated(segments: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    return [
        _merge_single_decision(
            segment,
            {
                "source_type": "generated",
                "clip_ids": [],
                "generation_prompt": _generation_prompt(segment, lang),
                "match_reason": (
                    "没有可用的上传素材，需要生成该分镜。"
                    if lang == "zh"
                    else "No uploaded footage is available for this segment."
                ),
            },
            {},
        )
        for segment in segments
    ]


def _generation_prompt(segment: dict[str, Any], lang: str) -> str:
    existing = segment.get("generation_prompt")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    if lang == "zh":
        return (
            f"画面：{segment['visual_intent']}；情绪：{segment['tone']}；"
            f"运镜：{segment['camera_motion']}；衔接：{segment['continuity_hint']}；"
            f"时长：{segment['duration']}秒。"
        )
    return (
        f"Visual: {segment['visual_intent']}; tone: {segment['tone']}; "
        f"camera: {segment['camera_motion']}; continuity: {segment['continuity_hint']}; "
        f"duration: {segment['duration']} seconds."
    )


def _build_output(
    rewrite_result: dict[str, Any],
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    output = {
        "original_script": str(rewrite_result.get("original_script") or ""),
        "rewritten_script": rewrite_result["rewritten_script"],
        "segments": segments,
        "total_duration": round(sum(float(item["duration"]) for item in segments), 2),
    }
    return MatchMontageSegmentsOutput.model_validate(output).model_dump()
