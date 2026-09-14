from typing import Any, Dict
import asyncio

from open_storyline.nodes.core_nodes.base_node import BaseNode, NodeMeta
from src.open_storyline.utils.prompts import get_prompt
from open_storyline.utils.parse_json import parse_json_dict
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.node_schema import UnderstandClipsInput
from open_storyline.utils.register import NODE_REGISTRY

@NODE_REGISTRY.register()
class UnderstandClipsNode(BaseNode):
    """
    Media Understanding Node
    """

    meta = NodeMeta(
        name="understand_media",
        description="Analyze full uploaded media assets and generate descriptions for each. Requires `load_media` output",
        node_id="understand_media",
        node_kind="understand_media",
        require_prior_kind=['load_media'],
        default_require_prior_kind=['load_media'],
        next_available_node=['filter_clips', 'rewrite_montage_script'],
    )

    input_schema = UnderstandClipsInput

    async def default_process(
        self,
        node_state: NodeState,
        inputs: Dict[str, Any],
    ) -> Any:
        media_items = inputs.get("media_items", [])

        media_captions: list[dict[str, Any]] = []
        for media_item in media_items or []:
            media_id = str(media_item.get("media_id", "") or "").strip()
            media_captions.append(
                {
                    "media_id": media_id,
                    "clip_id": media_id,
                    "media_type": media_item.get("media_type"),
                    "caption": "no caption",
                    "source_ref": {
                        "media_id": media_id,
                    },
                }
            )
        node_state.node_summary.info_for_user(f"Skipped description generation for {len(media_items)} media assets")
        return {
            "media_captions": media_captions,
            "clip_captions": media_captions,
            "overall": "unknown",
        }

    async def process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Any:
        """
        inputs: Previous node results read by BaseNode.load_inputs(ctx)
        """
        media_items = inputs.get("media_items", [])
        llm = node_state.llm
        system_prompt = get_prompt("understan_medias.system_detail", lang=node_state.lang)
        user_prompt = get_prompt("understan_medias.user_detail", lang=node_state.lang)


        media_captions: list[dict[str, Any]] = []

        for media_item in media_items or []:
            media_id = str(media_item.get("media_id", "") or "").strip() or "(unknown_media)"
            media_type = str(media_item.get("media_type", "") or "").strip().lower()
            metadata = media_item.get("metadata") or {}

            out_item: dict[str, Any] = {
                "media_id": media_id,
                "clip_id": media_id,
                "media_type": media_type,
            }

            path = str(media_item.get("path", "") or "").strip()
            if not path:
                out_item["caption"] = f"Error: No path specified for media_id={media_id}"
                media_captions.append(out_item)
                continue

            # 组装 media
            media: list[Any] = []

            if media_type == "image":
                media = [{"path": path}]

            elif media_type == "video":
                in_sec = 0.0
                duration_ms = _safe_float(metadata.get("duration"), 0.0)
                out_sec = duration_ms / 1000.0 if duration_ms > 0 else 0.0
                
                if out_sec <= in_sec:
                    out_sec = in_sec + 0.1

                media = [{
                    "path": path,
                    "in_sec": in_sec,
                    "out_sec": out_sec,
                }]
            elif media_type == "audio":
                out_item["caption"] = "Audio media asset. Visual understanding is not available for audio-only input."
                out_item["aes_score"] = -1.0
                out_item["source_ref"] = _build_source_ref(media_id, metadata)
                media_captions.append(out_item)
                continue
            else:
                out_item["caption"] = f"Error: Media type not supported: {media_type}"
                media_captions.append(out_item)
                continue
    
            max_retries = 2
            raw = None
            last_exc: Exception | None = None

            for attempt in range(max_retries + 1):
                try:
                    raw = await llm.complete(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        media=media,
                        temperature=0.3,
                        top_p=0.9,
                        max_tokens=2048,
                        model_preferences=None,
                    )
                    if raw is not None:
                        last_exc = None
                        break
                except Exception as e:
                    last_exc = e

                if attempt < max_retries:
                    await asyncio.sleep(0.3 * (attempt + 1))

            if raw is None:
                out_item["caption"] = "Error: VLM request failed"
                out_item["aes_score"] = -1.0
                node_state.node_summary.add_error(repr(last_exc))
                media_captions.append(out_item)
                continue

            try:
                obj = parse_json_dict(raw)
            except Exception:
                text = (raw or "").strip()
                out_item["caption"] = text if text else "Error: Unable to parse model output"
                media_captions.append(out_item)
                continue

            out_item["caption"] = str(obj.get("caption", "") or "").strip()
            try:
                raw_score = obj.get("aes_score")
                out_item["aes_score"] = float(str(raw_score).strip())
            except (ValueError, TypeError, AttributeError):
                # If the conversion fails (such as "abc", None, "nan", etc.), assign the value -1.0
                out_item["aes_score"] = -1.0
            out_item["source_ref"] = _build_source_ref(media_id, metadata)
            media_captions.append(out_item)

        desc_lines: list[str] = []
        for desc in media_captions:
            text = str(desc.get("caption"))
            desc_lines.append(f"- {desc.get('media_id')}: {text}")

        overall_summary = ""
        if desc_lines:
            overall_system_prompt = get_prompt("understan_medias.system_overall", lang=node_state.lang)
            overall_user_prompt = get_prompt("understan_medias.user_overall", lang=node_state.lang, clips_captions=desc_lines)

            try:
                overall_summary = await llm.complete(
                    system_prompt=overall_system_prompt,
                    user_prompt=overall_user_prompt,
                    media=None,
                    temperature=0.3,
                    top_p=0.9,
                    max_tokens=1024,
                    model_preferences=None
                )
            
            except Exception as e:
                overall_summary = f"Error: Summary generation failed: {type(e).__name__}: {e}"
            node_state.node_summary.info_for_user(f"Media understanding completed. Analyzed {len(media_captions)} media assets in total. Overall description: {overall_summary}")
        return {
            "media_captions": media_captions,
            "clip_captions": media_captions,
            "overall": overall_summary
        }
    

    def _parse_input(self, node_state: NodeState, inputs: Dict[str, Any]):
        media = (inputs.get("load_media") or {}).get("media", [])
        inputs.update({"media_items": media or []})
        return inputs

def _build_source_ref(media_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    duration = _safe_float(metadata.get("duration"), 0.0)
    return {
        "media_id": media_id,
        "start": 0,
        "end": int(duration),
        "duration": int(duration),
        "height": metadata.get("height"),
        "width": metadata.get("width"),
    }

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default
