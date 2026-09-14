from typing import Any, Dict
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

from open_storyline.nodes.core_nodes.base_node import BaseNode, NodeMeta
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.node_schema import LocalASRInput
from open_storyline.utils.register import NODE_REGISTRY

@NODE_REGISTRY.register()
class LocalASRNode(BaseNode):

    meta = NodeMeta(
        name="local_asr",
        description="Perform ASR on video clips using the configured ASR provider",
        node_id="local_asr",
        node_kind="asr",
        require_prior_kind=['split_shots'],
        default_require_prior_kind=['split_shots'],
        next_available_node=['group_clips'],
    )

    input_schema = LocalASRInput
    _DOUBAO_PROCESSING_CODES = {"20000001", "20000002"}
    _DOUBAO_SUCCESS_CODE = "20000000"
    _DOUBAO_SILENT_CODE = "20000003"

    def _load_asr_model(self):

        if hasattr(self, "asr_model"):
            return self.asr_model
        else:
            from funasr import AutoModel

            self.asr_model = AutoModel(
                model="paraformer-zh",
                vad_model="fsmn-vad",
                punc_model="ct-punc",
                vad_kwargs={"max_single_segment_time": 30000},
            )
            return self.asr_model

    def _provider_name(self) -> str:
        cfg = getattr(self.server_cfg, "asr", None)
        provider = str(getattr(cfg, "default_provider", "local") or "local").strip()
        return provider or "local"

    def _provider_cfg(self, provider: str) -> Dict[str, Any]:
        cfg = getattr(self.server_cfg, "asr", None)
        providers = getattr(cfg, "providers", None) or {}
        provider_cfg = providers.get(provider, {})
        return provider_cfg if isinstance(provider_cfg, dict) else {}

    @staticmethod
    def _empty_asr_info(clip: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "clip_id": clip["clip_id"],
            "path": clip["path"],
            "kind": clip["kind"],
            "source_ref": clip.get("source_ref", {}),
            "fps": clip.get("fps", 30),
            "asr_res": {},
        }
        
    def extract_audio_wav(self, video_path: str, tmpdir: str):
        # 1. Determine if there is an audio track
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            video_path
        ]

        result = subprocess.run(probe_cmd, capture_output=True, text=True)

        if not result.stdout.strip():
            return None
        
        out_wav = os.path.join(tmpdir, "audio.wav")

        # 3. Extract audio
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-af", "afftdn,agate=threshold=-40dB:ratio=10:attack=20:release=100",
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            out_wav
        ]

        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return out_wav

    def _prepare_doubao_audio_url(
        self,
        *,
        audio_wav: str,
        node_state: NodeState,
        clip_id: str,
        provider_cfg: Dict[str, Any],
    ) -> str:
        public_base_url = str(provider_cfg.get("public_base_url") or "").strip().rstrip("/")
        if not public_base_url:
            raise ValueError(
                "asr.providers.doubao_asr.public_base_url is required. "
                "Expose the project outputs directory over HTTP(S), then set this value "
                "to that public URL prefix, for example https://example.com/outputs."
            )

        outputs_dir = Path(self.server_cfg.project.outputs_dir)
        upload_dir = outputs_dir / "asr_uploads" / node_state.session_id / node_state.artifact_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        safe_clip_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(clip_id))
        public_audio_path = upload_dir / f"{safe_clip_id}_{uuid.uuid4().hex[:8]}.wav"
        shutil.copyfile(audio_wav, public_audio_path)

        rel_path = public_audio_path.resolve().relative_to(outputs_dir.resolve()).as_posix()
        return f"{public_base_url}/{quote(rel_path)}"

    def _doubao_headers(self, provider_cfg: Dict[str, Any], task_id: str, *, submit: bool) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": str(provider_cfg.get("resource_id") or "volc.seedasr.auc"),
            "X-Api-Request-Id": task_id,
        }
        api_key = str(provider_cfg.get("api_key") or "").strip()
        if not api_key:
            raise ValueError(
                "Doubao ASR API key is not configured. Fill asr.providers.doubao_asr.api_key "
                "in config.toml."
            )
        headers["X-Api-Key"] = api_key
        if submit:
            headers["X-Api-Sequence"] = "-1"
        return headers

    @staticmethod
    def _doubao_status(resp: requests.Response) -> str:
        return str(resp.headers.get("X-Api-Status-Code") or "").strip()

    @staticmethod
    def _doubao_message(resp: requests.Response) -> str:
        return str(resp.headers.get("X-Api-Message") or resp.text or resp.reason or "").strip()

    def _submit_doubao_task(
        self,
        *,
        audio_url: str,
        task_id: str,
        provider_cfg: Dict[str, Any],
    ) -> None:
        audio: Dict[str, Any] = {
            "format": "wav",
            "url": audio_url,
        }
        language = str(provider_cfg.get("language") or "").strip()
        if language:
            audio["language"] = language

        request_cfg: Dict[str, Any] = {
            "model_name": str(provider_cfg.get("model_name") or "bigmodel"),
            "enable_itn": bool(provider_cfg.get("enable_itn", True)),
            "enable_punc": bool(provider_cfg.get("enable_punc", True)),
            "enable_ddc": bool(provider_cfg.get("enable_ddc", False)),
            "show_utterances": bool(provider_cfg.get("show_utterances", True)),
        }

        payload = {
            "user": {"uid": str(provider_cfg.get("uid") or "openstoryline")},
            "audio": audio,
            "request": request_cfg,
        }
        resp = requests.post(
            str(provider_cfg.get("submit_url") or "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"),
            headers=self._doubao_headers(provider_cfg, task_id, submit=True),
            json=payload,
            timeout=float(provider_cfg.get("request_timeout", 30.0) or 30.0),
        )
        status = self._doubao_status(resp)
        if resp.status_code >= 400 or status != self._DOUBAO_SUCCESS_CODE:
            raise RuntimeError(
                f"Doubao ASR submit failed: http={resp.status_code}, "
                f"status={status}, message={self._doubao_message(resp)}"
            )

    def _query_doubao_task(
        self,
        *,
        task_id: str,
        provider_cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + float(provider_cfg.get("timeout", 600.0) or 600.0)
        interval = float(provider_cfg.get("poll_interval", 2.0) or 2.0)
        query_url = str(provider_cfg.get("query_url") or "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query")

        while True:
            resp = requests.post(
                query_url,
                headers=self._doubao_headers(provider_cfg, task_id, submit=False),
                json={},
                timeout=float(provider_cfg.get("request_timeout", 30.0) or 30.0),
            )
            status = self._doubao_status(resp)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Doubao ASR query failed: http={resp.status_code}, "
                    f"status={status}, message={self._doubao_message(resp)}"
                )
            if status == self._DOUBAO_SUCCESS_CODE:
                return resp.json() if resp.text.strip() else {}
            if status == self._DOUBAO_SILENT_CODE:
                return {"result": {"text": "", "utterances": []}}
            if status not in self._DOUBAO_PROCESSING_CODES:
                raise RuntimeError(
                    f"Doubao ASR query failed: status={status}, message={self._doubao_message(resp)}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Doubao ASR task timed out: task_id={task_id}")
            time.sleep(max(0.2, interval))

    @staticmethod
    def _normalize_doubao_result(result_json: Dict[str, Any]) -> Dict[str, Any]:
        result = result_json.get("result") if isinstance(result_json, dict) else {}
        if not isinstance(result, dict):
            result = {}

        text = str(result.get("text") or "")
        utterances = result.get("utterances") or []
        sentence_info = []
        timestamps = []
        if isinstance(utterances, list):
            for idx, item in enumerate(utterances):
                if not isinstance(item, dict):
                    continue
                start = int(item.get("start_time") or 0)
                end = int(item.get("end_time") or start)
                sentence_info.append({
                    "sentence_id": idx,
                    "text": str(item.get("text") or ""),
                    "start": start,
                    "end": end,
                })
                timestamps.append([start, end])

        return {
            "text": text,
            "timestamp": timestamps,
            "sentence_info": sentence_info,
            "raw": result_json,
        }

    def _run_doubao_asr(
        self,
        *,
        audio_wav: str,
        node_state: NodeState,
        clip_id: str,
        provider_cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_id = str(uuid.uuid4())
        audio_url = self._prepare_doubao_audio_url(
            audio_wav=audio_wav,
            node_state=node_state,
            clip_id=clip_id,
            provider_cfg=provider_cfg,
        )
        self._submit_doubao_task(audio_url=audio_url, task_id=task_id, provider_cfg=provider_cfg)
        result_json = self._query_doubao_task(task_id=task_id, provider_cfg=provider_cfg)
        return self._normalize_doubao_result(result_json)

    async def default_process(
        self,
        node_state,
        inputs: Dict[str, Any],
    ) -> Any:
        return {}

    async def process(self, node_state: NodeState, inputs: Dict[str, Any]) -> Any:
        
        clips = inputs["split_shots"].get('clips', [])
        provider = self._provider_name()
        provider_cfg = self._provider_cfg(provider)
        asr_model = self._load_asr_model() if provider == "local" else None

        asr_infos = []
        for clip in clips:
            video_path = clip["path"]
            kind = clip["kind"]
            source_ref = clip.get("source_ref", {})
            fps = clip.get("fps", 30)

            # only process video clips, for other kinds of clips, directly return empty asr text
            if kind != "video":
                asr_infos.append(self._empty_asr_info(clip))
                continue
            
            with tempfile.TemporaryDirectory() as tmpdir:
                
                # extract audio wav from video clip, if no audio track, directly return empty asr text
                audio_wav = self.extract_audio_wav(video_path, tmpdir)
                if audio_wav is None:
                    asr_infos.append(self._empty_asr_info(clip))
                    node_state.node_summary.info_for_llm(f"Clip {clip['clip_id']} has no audio track, skipped for asr.")
                    continue

                if provider == "local":
                    # funasr supports audio file input directly and handles audio loading internally.
                    res = asr_model.generate(
                        input=audio_wav,
                        sentence_timestamp=True
                    )
                    asr_res = res[0] if res else {}
                elif provider == "doubao_asr":
                    asr_res = self._run_doubao_asr(
                        audio_wav=audio_wav,
                        node_state=node_state,
                        clip_id=str(clip["clip_id"]),
                        provider_cfg=provider_cfg,
                    )
                else:
                    raise ValueError(f"Unsupported ASR provider: {provider}")

                asr_infos.append({
                    "clip_id": clip["clip_id"],
                    "path": video_path,
                    "kind": kind,
                    "source_ref": source_ref,
                    "fps": fps,
                    "asr_res": asr_res,
                })

        return {
            "asr_infos": asr_infos,
        }
    
    def _combine_tool_outputs(self, node_state, outputs):
        
        asr_infos = outputs.get("asr_infos", [])
        regularized_asr_infos = []

        for asr_info in asr_infos:
            clip_id = asr_info["clip_id"]
            kind = asr_info["kind"]
            asr_res = asr_info.get("asr_res", {})

            regularized_asr_infos.append({
                "clip_id": clip_id,
                "kind": kind,
                "path": asr_info["path"],
                "asr_text": asr_res.get("text", "") if asr_res else "",
                "asr_timestamps": asr_res.get("timestamp", []) if asr_res else [],
                "asr_sentence_info": asr_res.get("sentence_info", []) if asr_res else [],
                "source_ref": asr_info.get("source_ref", {}),
                "fps": asr_info.get("fps", 30),
            })
        return {
            "asr_infos": regularized_asr_infos,
        }
