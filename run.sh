#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$ROOT_DIR/src"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-7860}"
MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="${MCP_PORT:-8001}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/.storyline/run}"
PID_FILE="${PID_FILE:-$RUN_DIR/openstoryline.pid}"
LOG_FILE="${LOG_FILE:-$RUN_DIR/openstoryline.log}"

if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    PYTHON_BIN="python3"
  fi
fi

if [ -z "${UVICORN_BIN:-}" ]; then
  if [ -x "$ROOT_DIR/.venv/bin/uvicorn" ]; then
    UVICORN_BIN="$ROOT_DIR/.venv/bin/uvicorn"
  else
    UVICORN_BIN="uvicorn"
  fi
fi

connect_host_for_wait() {
  local host="$1"
  if [ "$host" = "0.0.0.0" ] || [ "$host" = "::" ]; then
    echo "127.0.0.1"
  else
    echo "$host"
  fi
}

port_is_open() {
  local host="$1"
  local port="$2"
  (echo >"/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

wait_for_port_open() {
  local host="$1"
  local port="$2"
  local label="$3"
  local pid="${4:-}"
  for _ in $(seq 1 120); do
    if port_is_open "$host" "$port"; then
      return 0
    fi
    if [ -n "$pid" ] && ! pid_is_running "$pid"; then
      echo "${label} process exited before opening ${host}:${port}" >&2
      return 1
    fi
    sleep 0.5
  done

  echo "Timed out waiting for ${label} at ${host}:${port}" >&2
  return 1
}

wait_for_background_start() {
  local runner_pid="$1"
  local web_host="$2"
  for _ in $(seq 1 120); do
    if port_is_open "$web_host" "$PORT" && port_is_open "$MCP_HOST" "$MCP_PORT"; then
      return 0
    fi
    if ! pid_is_running "$runner_pid"; then
      echo "OpenStoryline failed to start. See log: $LOG_FILE" >&2
      return 1
    fi
    sleep 0.5
  done

  echo "Timed out waiting for OpenStoryline to start. See log: $LOG_FILE" >&2
  return 1
}

wait_for_port_closed() {
  local host="$1"
  local port="$2"
  local label="$3"
  for _ in $(seq 1 60); do
    if ! port_is_open "$host" "$port"; then
      return 0
    fi
    sleep 0.5
  done

  echo "Timed out waiting for ${label} port to close at ${host}:${port}" >&2
  return 1
}

pid_is_running() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

load_pid_file() {
  RUNNER_PID=""
  MCP_PID=""
  WEB_PID=""
  if [ -f "$PID_FILE" ]; then
    # shellcheck disable=SC1090
    . "$PID_FILE"
  fi
}

write_pid_file() {
  mkdir -p "$RUN_DIR"
  {
    echo "RUNNER_PID=$$"
    echo "MCP_PID=$MCP_PID"
    echo "WEB_PID=$WEB_PID"
    echo "HOST=$HOST"
    echo "PORT=$PORT"
    echo "MCP_HOST=$MCP_HOST"
    echo "MCP_PORT=$MCP_PORT"
  } > "$PID_FILE"
}

remove_pid_file() {
  rm -f "$PID_FILE"
}

cleanup() {
  local exit_code=$?
  trap - INT TERM EXIT

  if [ "${WEB_PID:-}" ]; then
    kill "$WEB_PID" 2>/dev/null || true
  fi
  if [ "${MCP_PID:-}" ]; then
    kill "$MCP_PID" 2>/dev/null || true
  fi

  if [ "${WEB_PID:-}" ]; then
    wait "$WEB_PID" 2>/dev/null || true
  fi
  if [ "${MCP_PID:-}" ]; then
    wait "$MCP_PID" 2>/dev/null || true
  fi

  remove_pid_file
  exit "$exit_code"
}

run_foreground() {
  mkdir -p "$RUN_DIR"

  "$PYTHON_BIN" -m open_storyline.mcp.server &
  MCP_PID=$!

  if ! wait_for_port_open "$MCP_HOST" "$MCP_PORT" "MCP server" "$MCP_PID"; then
    kill "$MCP_PID" 2>/dev/null || true
    wait "$MCP_PID" 2>/dev/null || true
    return 1
  fi

  "$UVICORN_BIN" agent_fastapi:app \
    --host "$HOST" \
    --port "$PORT" &
  WEB_PID=$!

  WEB_WAIT_HOST="$(connect_host_for_wait "$HOST")"
  if ! wait_for_port_open "$WEB_WAIT_HOST" "$PORT" "FastAPI server" "$WEB_PID"; then
    kill "$MCP_PID" "$WEB_PID" 2>/dev/null || true
    wait "$MCP_PID" "$WEB_PID" 2>/dev/null || true
    return 1
  fi

  write_pid_file
  echo "OpenStoryline is running."
  echo "Web: http://${WEB_WAIT_HOST}:${PORT}"
  echo "MCP: ${MCP_HOST}:${MCP_PORT}"

  trap cleanup INT TERM EXIT

  while true; do
    if ! pid_is_running "$MCP_PID"; then
      wait "$MCP_PID" 2>/dev/null || true
      echo "MCP server exited; stopping FastAPI server." >&2
      return 1
    fi
    if ! pid_is_running "$WEB_PID"; then
      wait "$WEB_PID" 2>/dev/null || true
      echo "FastAPI server exited; stopping MCP server." >&2
      return 1
    fi
    sleep 1
  done
}

start_background() {
  load_pid_file
  if pid_is_running "${RUNNER_PID:-}" || pid_is_running "${WEB_PID:-}" || pid_is_running "${MCP_PID:-}"; then
    echo "OpenStoryline is already running. PID file: $PID_FILE"
    return 0
  fi

  mkdir -p "$RUN_DIR"
  nohup "$0" foreground > "$LOG_FILE" 2>&1 &
  RUNNER_START_PID=$!

  WEB_WAIT_HOST="$(connect_host_for_wait "$HOST")"
  wait_for_background_start "$RUNNER_START_PID" "$WEB_WAIT_HOST"

  echo "OpenStoryline started in background."
  echo "Web: http://${WEB_WAIT_HOST}:${PORT}"
  echo "MCP: ${MCP_HOST}:${MCP_PORT}"
  echo "PID file: $PID_FILE"
  echo "Log file: $LOG_FILE"
}

stop_background() {
  load_pid_file
  if ! pid_is_running "${RUNNER_PID:-}" && ! pid_is_running "${WEB_PID:-}" && ! pid_is_running "${MCP_PID:-}"; then
    remove_pid_file
    echo "OpenStoryline is not running."
    return 0
  fi

  if pid_is_running "${RUNNER_PID:-}"; then
    kill "$RUNNER_PID" 2>/dev/null || true
  else
    [ "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
    [ "${MCP_PID:-}" ] && kill "$MCP_PID" 2>/dev/null || true
  fi

  WEB_WAIT_HOST="$(connect_host_for_wait "${HOST:-$HOST}")"
  wait_for_port_closed "$WEB_WAIT_HOST" "${PORT:-$PORT}" "FastAPI server" || true
  wait_for_port_closed "${MCP_HOST:-$MCP_HOST}" "${MCP_PORT:-$MCP_PORT}" "MCP server" || true
  remove_pid_file
  echo "OpenStoryline stopped."
}

status() {
  load_pid_file
  if pid_is_running "${RUNNER_PID:-}" || pid_is_running "${WEB_PID:-}" || pid_is_running "${MCP_PID:-}"; then
    echo "OpenStoryline is running."
    echo "Runner PID: ${RUNNER_PID:-unknown}"
    echo "Web PID: ${WEB_PID:-unknown}"
    echo "MCP PID: ${MCP_PID:-unknown}"
    echo "Web: http://$(connect_host_for_wait "${HOST:-$HOST}"):${PORT:-$PORT}"
    echo "MCP: ${MCP_HOST:-$MCP_HOST}:${MCP_PORT:-$MCP_PORT}"
  else
    echo "OpenStoryline is not running."
    return 1
  fi
}

usage() {
  echo "Usage: $0 [foreground|start|stop|restart|status]"
}

case "${1:-foreground}" in
  foreground)
    run_foreground
    ;;
  start)
    start_background
    ;;
  stop)
    stop_background
    ;;
  restart)
    stop_background
    start_background
    ;;
  status)
    status
    ;;
  *)
    usage
    exit 2
    ;;
esac
