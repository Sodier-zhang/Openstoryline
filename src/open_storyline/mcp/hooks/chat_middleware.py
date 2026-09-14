import contextvars
from typing import Callable, Optional, Any
import json
import uuid
import ast
import asyncio

from open_storyline.config import Settings

from langgraph.types import Command
from langchain.agents.middleware import wrap_tool_call, wrap_model_call
from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from langchain_mcp_adapters.callbacks import CallbackContext
from langchain_core.callbacks import AsyncCallbackHandler

CUSTOM_MODEL_KEY = "__custom__"

_SENSITIVE_KEYS = {
    "api_key",
    "access_token",
    "authorization",
    "token",
    "password",
    "secret",
    "x-api-key",
    "apikey",
}

# GUI 日志输出通道
_MCP_LOG_SINK = contextvars.ContextVar("mcp_log_sink", default=None)
_MCP_ACTIVE_TOOL_CALL_ID = contextvars.ContextVar("mcp_active_tool_call_id", default=None)
_LLM_TOKEN_USAGE_RECORDER = contextvars.ContextVar("llm_token_usage_recorder", default=None)
def set_mcp_log_sink(sink: Optional[Callable[[dict], None]]):
    return _MCP_LOG_SINK.set(sink)

def reset_mcp_log_sink(token):
    _MCP_LOG_SINK.reset(token)

def set_llm_token_usage_recorder(recorder: Optional[Callable[[dict], None]]):
    return _LLM_TOKEN_USAGE_RECORDER.set(recorder)

def reset_llm_token_usage_recorder(token):
    _LLM_TOKEN_USAGE_RECORDER.reset(token)


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _token_usage_from_obj(obj: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(obj, "usage_metadata", None)
    if isinstance(usage, dict):
        input_tokens = _int_or_none(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_token_count")
        )
        output_tokens = _int_or_none(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("output_token_count")
        )
        total_tokens = _int_or_none(usage.get("total_tokens") or usage.get("total_token_count"))
        if input_tokens is not None or output_tokens is not None or total_tokens is not None:
            return input_tokens, output_tokens, total_tokens

    metadata = getattr(obj, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage") or metadata.get("usage") or metadata
        if isinstance(token_usage, dict):
            input_tokens = _int_or_none(
                token_usage.get("input_tokens")
                or token_usage.get("prompt_tokens")
                or token_usage.get("input_token_count")
            )
            output_tokens = _int_or_none(
                token_usage.get("output_tokens")
                or token_usage.get("completion_tokens")
                or token_usage.get("output_token_count")
            )
            total_tokens = _int_or_none(
                token_usage.get("total_tokens")
                or token_usage.get("total_token_count")
            )
            if input_tokens is not None or output_tokens is not None or total_tokens is not None:
                return input_tokens, output_tokens, total_tokens

    additional_kwargs = getattr(obj, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        token_usage = additional_kwargs.get("token_usage") or additional_kwargs.get("usage")
        if isinstance(token_usage, dict):
            input_tokens = _int_or_none(token_usage.get("input_tokens") or token_usage.get("prompt_tokens"))
            output_tokens = _int_or_none(token_usage.get("output_tokens") or token_usage.get("completion_tokens"))
            total_tokens = _int_or_none(token_usage.get("total_tokens"))
            if input_tokens is not None or output_tokens is not None or total_tokens is not None:
                return input_tokens, output_tokens, total_tokens

    return None, None, None


def record_llm_token_usage_from_response(
    response: Any,
    *,
    model: str,
    node_id: str = "",
) -> None:
    recorder = _LLM_TOKEN_USAGE_RECORDER.get()
    if recorder is None:
        return

    input_tokens, output_tokens, total_tokens = _token_usage_from_obj(response)
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    recorder({
        "model": str(model or ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "node_id": str(node_id or ""),
    })


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    return u.rstrip("/") if u else u

def _mask_secrets(obj: Any) -> Any:
    """
    Recursive desensitization: Prevent keys/tokens from being printed to various places such as 
    the console, logs, tool traces, toolmessages, etc
    """
    try:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if str(k).lower() in _SENSITIVE_KEYS:
                    out[k] = "***"
                else:
                    out[k] = _mask_secrets(v)
            return out
        if isinstance(obj, list):
            return [_mask_secrets(x) for x in obj]
        if isinstance(obj, tuple):
            return tuple(_mask_secrets(x) for x in obj)
        return obj
    except Exception:
        return "***"

def _make_chat_llm(cfg: Settings, model_name: str, streaming: bool) -> ChatOpenAI:
    model_config = (cfg.developer.chat_models_config.get(model_name) or {})
    base_url = _norm_url(model_config.get("base_url") or "")
    api_key = model_config.get("api_key")
    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        default_headers={
            "api-key": api_key,
            "Content-Type": "application/json",
        },
        timeout=cfg.llm.timeout,
        temperature=model_config.get("temperature", cfg.llm.temperature),
        streaming=streaming,
    )


def _get_llm(cfg: Settings, llm_pool: dict[tuple[str, bool], ChatOpenAI], model_name: str, streaming: bool) -> ChatOpenAI:
    hit = llm_pool.get((model_name, streaming))
    if hit:
        return hit
    new_llm = _make_chat_llm(cfg, model_name, streaming=streaming)
    llm_pool[(model_name, streaming)] = new_llm
    return new_llm


@wrap_tool_call
async def log_tool_request(request, handler):
    sink = _MCP_LOG_SINK.get()
    server_names = {"storyline"}

    def emit_event(x: str | dict):
        if sink:
            sink(x)

    tool_call_info = request.tool_call
    tool_complete_name = tool_call_info.get("name", "")

    server_name, tool_name = "", tool_complete_name
    for s in server_names:
        prefix = f"{s}_"
        if tool_complete_name.startswith(prefix):
            server_name = s
            tool_name = tool_complete_name[len(prefix):]
            break

    meta_collector = request.runtime.context.node_manager
    exclude = set(meta_collector.kind_to_node_ids.keys()) | {
        "inputs", "artifacts_dir", "artifact_id", "blobs_dir", "meta_path",
        "media_dir", "bgm_dir", "outputs_dir", "debug_dir",
    }

    extracted_args = {}
    if isinstance(tool_call_info.get("args", {}), dict):
        for arg in tool_call_info["args"].keys():
            if arg not in exclude:
                extracted_args[arg] = tool_call_info["args"].get(arg, "")
    extracted_args = _mask_secrets(extracted_args)

    tool_call_id = tool_call_info.get("id", "")
    if not tool_call_id:
        tool_call_id = f"mcp_{uuid.uuid4().hex[:8]}"
        tool_call_info["id"] = tool_call_id

    is_mcp_tool = isinstance(getattr(request.tool, "args_schema", None), dict)

    active_tok = _MCP_ACTIVE_TOOL_CALL_ID.set(tool_call_id)

    out = None
    out_json = None
    isError = False
    summary = ""

    try:
        emit_event({
            "type": "tool_start",
            "tool_call_id": tool_call_id,
            "server": server_name,
            "name": tool_name,
            "args": extracted_args,
        })
        print(f"[Agent tool start] {server_name}.{tool_name} args={extracted_args}\n")

        out = await handler(request)

        additional_kwargs = getattr(out, "additional_kwargs", None) or {}
        if additional_kwargs.get("isError") is True:
            # only when skill failed
            isError = True
            summary = _mask_secrets(getattr(out, "content", str(out)))

        else:
            if is_mcp_tool:
                if additional_kwargs.get("mcp_raw_text") is True:
                    # mcp success
                    isError = False
                    summary = getattr(out, "content", "")
                else:
                    # judge based on out.content.isError
                    out_json = ast.literal_eval(out.content)
                    isError = out_json.get("isError", False)

                    if not isError:
                        summary = out_json.get("summary", {}).get("node_summary", "")
                    else:
                        summary = _mask_secrets(out.content)

            # Skill tool success
            # it don't provide "isError" field
            else:
                isError = False
                c = getattr(out, "content", "")
                summary = f"skill_ok len={len(c) if isinstance(c, str) else 0}"

    finally:
        _MCP_ACTIVE_TOOL_CALL_ID.reset(active_tok)

    # 结束日志
    if isError:
        print(f"[Agent tool error] result:{summary}\n\n")
        emit_event({
            "type": "tool_end",
            "tool_call_id": tool_call_id,
            "server": server_name,
            "name": tool_name,
            "is_error": True,
            "summary": summary,
        })
    else:
        print(f"[Agent tool finished] result:{summary}\n\n")
        emit_event({
            "type": "tool_end",
            "tool_call_id": tool_call_id,
            "server": server_name,
            "name": tool_name,
            "is_error": False,
            "summary": _mask_secrets(summary),
        })

    return out


async def on_progress(progress: float, total: float | None, message: str| None, context: CallbackContext):
    sink = _MCP_LOG_SINK.get()
    if sink:
        sink({
            "type": "tool_progress",
            "tool_call_id": _MCP_ACTIVE_TOOL_CALL_ID.get(),
            "server": context.server_name,
            "name": context.tool_name,
            "progress": progress,
            "total": total,
            "message": message,
        })

@wrap_tool_call
async def handle_tool_errors(request, handler):
    try:
        out = await handler(request)

        if isinstance(out, Command):
            return out.update.get('messages')[0]

        elif isinstance(out, MCPToolCallResult) and not isinstance(out.content, str):
            return ToolMessage(
                content=out.content[0].get("text", ""),
                tool_call_id=out.tool_call_id,
                name=out.name,
                additional_kwargs={
                    "isError": False,
                    "mcp_raw_text": True,
                },
            )

        return out

    except Exception as e:
        tc = request.tool_call
        safe_args = _mask_secrets(tc.get("args") or {})
        tool_name = tc.get("name", "")

        return ToolMessage(
            content=(
                "Tool call failed\n"
                f"Tool name: {tool_name}\n"
                f"Tool params: {safe_args}\n"
                f"Error messege: {type(e).__name__}: {e}\n"
                "If it is a parameter issue, please correct the parameters and call again. "
                "If it is due to the lack of a preceding dependency, please call the preceding node first. "
                "If you think it's an occasional error, please try to call it again; "
                "If you think it's impossible to continue, please explain the reason to the user."
            ),
            tool_call_id=tc["id"],
            name=tool_name,
            additional_kwargs={
                "isError": True,
                "error_type": type(e).__name__,
                "error_message": str(e),
                "safe_args": safe_args,
            },
        )

class PrintStreamingTokens(AsyncCallbackHandler):
    async def on_llm_new_token(self, token: str, **kwargs) -> None:
        if token:
            print(token, end="", flush=True)
