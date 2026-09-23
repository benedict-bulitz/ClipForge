#!/usr/bin/env bash
set -euo pipefail

# Local macOS controller for the repository checkout. It deliberately owns
# only PIDs it recorded, never broad Python/Node process classes.
REPO_DIR="${CLIPFORGE_REPO_DIR:-/Users/bene/Documents/ChatGPT/ClipForge}"
RUNTIME_DIR="$REPO_DIR/.clipforge-runtime"
BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
BACKEND_LOG="$RUNTIME_DIR/backend.log"
FRONTEND_LOG="$RUNTIME_DIR/frontend.log"
BACKEND_URL="http://127.0.0.1:8000/api/health"
FRONTEND_URL="http://127.0.0.1:3000"

say_error() {
  local message="$1"
  printf 'ClipForge: %s\n' "$message" >&2
  osascript -e "display alert \"ClipForge\" message \"${message//\"/\\\"}\" as critical" >/dev/null 2>&1 || true
}

require_runtime() {
  if [[ ! -x "$REPO_DIR/.venv/bin/uvicorn" || ! -x "$REPO_DIR/node_modules/.bin/next" ]]; then
    say_error "ClipForge is not bootstrapped yet. Run scripts/bootstrap.sh once from the repository."
    exit 1
  fi
}

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

process_started_at() { ps -p "$1" -o lstart= 2>/dev/null | sed 's/^ *//'; }

owned_process_running() {
  local pid_file="$1" expected="$2"
  [[ -f "$pid_file" ]] || return 1
  local stored_pid stored_started current_started command
  IFS=$'\t' read -r stored_pid stored_started < "$pid_file" || return 1
  [[ "$stored_pid" =~ ^[0-9]+$ ]] && kill -0 "$stored_pid" 2>/dev/null || return 1
  current_started="$(process_started_at "$stored_pid")"
  command="$(ps -p "$stored_pid" -o command= 2>/dev/null || true)"
  [[ -n "$current_started" && "$current_started" == "$stored_started" && "$command" == *"$expected"* ]]
}

record_process() {
  local pid="$1" pid_file="$2"
  printf '%s\t%s\n' "$pid" "$(process_started_at "$pid")" > "$pid_file"
}

backend_ready() { curl --fail --silent --show-error --max-time 1 "$BACKEND_URL" >/dev/null 2>&1; }
frontend_ready() { curl --fail --silent --show-error --max-time 1 "$FRONTEND_URL" >/dev/null 2>&1; }

wait_until_ready() {
  local label="$1" check="$2"
  local attempt
  for attempt in $(seq 1 45); do
    "$check" && return 0
    sleep 1
  done
  say_error "$label did not become ready. See $RUNTIME_DIR for local logs."
  return 1
}

start_backend() {
  backend_ready && return 0
  if owned_process_running "$BACKEND_PID_FILE" "clipforge.main:app"; then
    wait_until_ready "ClipForge backend" backend_ready
    return
  fi
  rm -f "$BACKEND_PID_FILE"
  printf '[%s] Starting backend\n' "$(timestamp)" >> "$BACKEND_LOG"
  (
    cd "$REPO_DIR/apps/api"
    nohup "$REPO_DIR/.venv/bin/uvicorn" clipforge.main:app --reload --port 8000 \
      >> "$BACKEND_LOG" 2>&1 &
    record_process "$!" "$BACKEND_PID_FILE"
  )
  wait_until_ready "ClipForge backend" backend_ready
}

start_frontend() {
  frontend_ready && return 0
  if owned_process_running "$FRONTEND_PID_FILE" "npm run dev:web"; then
    wait_until_ready "ClipForge frontend" frontend_ready
    return
  fi
  rm -f "$FRONTEND_PID_FILE"
  printf '[%s] Starting frontend\n' "$(timestamp)" >> "$FRONTEND_LOG"
  (
    cd "$REPO_DIR"
    nohup npm run dev:web >> "$FRONTEND_LOG" 2>&1 &
    record_process "$!" "$FRONTEND_PID_FILE"
  )
  wait_until_ready "ClipForge frontend" frontend_ready
}

stop_owned_process() {
  local pid_file="$1" expected="$2"
  owned_process_running "$pid_file" "$expected" || { rm -f "$pid_file"; return 0; }
  local pid
  IFS=$'\t' read -r pid _ < "$pid_file"
  kill -TERM "$pid" 2>/dev/null || true
  local attempt
  for attempt in $(seq 1 5); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  rm -f "$pid_file"
}

stop_services() {
  stop_owned_process "$FRONTEND_PID_FILE" "npm run dev:web"
  stop_owned_process "$BACKEND_PID_FILE" "clipforge.main:app"
  osascript -e 'display notification "Stopped launcher-owned services." with title "ClipForge"' >/dev/null 2>&1 || true
}

status_services() {
  backend_ready && printf 'backend=ready\n' || printf 'backend=not-ready\n'
  frontend_ready && printf 'frontend=ready\n' || printf 'frontend=not-ready\n'
}

main() {
  case "${1:---start}" in
    --stop) stop_services ;;
    --restart) stop_services; require_runtime; start_backend; start_frontend; open "$FRONTEND_URL" ;;
    --status) status_services ;;
    --start)
      [[ -d "$REPO_DIR" ]] || { say_error "The ClipForge repository was not found at $REPO_DIR."; exit 1; }
      mkdir -p "$RUNTIME_DIR"
      require_runtime
      start_backend
      start_frontend
      open "$FRONTEND_URL"
      ;;
    *)
      printf 'Usage: %s [--start|--stop|--restart|--status]\n' "$0" >&2
      exit 64
      ;;
  esac
}

main "$@"
