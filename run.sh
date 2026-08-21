#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$ROOT_DIR/src"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-7860}"

python -m open_storyline.mcp.server &
MCP_PID=$!

MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="${MCP_PORT:-8001}"

wait_for_mcp() {
  for _ in {1..120}; do
    if (echo >"/dev/tcp/${MCP_HOST}/${MCP_PORT}") >/dev/null 2>&1; then
      return 0
    fi

    if ! kill -0 "$MCP_PID" 2>/dev/null; then
      wait "$MCP_PID"
      return 1
    fi

    sleep 0.5
  done

  echo "Timed out waiting for MCP server at ${MCP_HOST}:${MCP_PORT}" >&2
  kill "$MCP_PID" 2>/dev/null || true
  return 1
}

wait_for_mcp

uvicorn agent_fastapi:app \
  --host "$HOST" \
  --port "$PORT" &
WEB_PID=$!

trap 'kill $MCP_PID $WEB_PID' INT TERM

wait
