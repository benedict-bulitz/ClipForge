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
FRONTEND_BROWSER_URL="http://localhost:3000"
FRONTEND_READINESS_URLS=("$FRONTEND_BROWSER_URL/" "http://127.0.0.1:3000/")
COMMON_NODE_PATHS="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ -n "${CLIPFORGE_NODE_SEARCH_PATHS+x}" ]]; then
  [[ -n "$CLIPFORGE_NODE_SEARCH_PATHS" ]] && PATH="${PATH:-}:$CLIPFORGE_NODE_SEARCH_PATHS"
else
  PATH="${PATH:-}:$COMMON_NODE_PATHS"
fi
export PATH

NPM_BIN=""
NODE_BIN=""
BACKEND_STARTED_BY_RUN=0
FRONTEND_STARTED_BY_RUN=0
READY_ATTEMPTS="${CLIPFORGE_READY_ATTEMPTS:-45}"
READINESS_ATTEMPT=0
READINESS_ELAPSED_SECONDS=0

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

resolve_command() {
  local command_name="$1"
  local resolved=""
  resolved="$(command -v "$command_name" 2>/dev/null || true)"
  if [[ -z "$resolved" && -z "${CLIPFORGE_NODE_SEARCH_PATHS+x}" ]]; then
    resolved="$(/bin/zsh -lic "command -v $command_name" 2>/dev/null | tail -n 1 || true)"
  fi
  if [[ -n "$resolved" && -x "$resolved" ]]; then
    printf '%s\n' "$resolved"
    return 0
  fi
  return 1
}

resolve_node_tools() {
  NPM_BIN="$(resolve_command npm || true)"
  NODE_BIN="$(resolve_command node || true)"
  if [[ -z "$NPM_BIN" || -z "$NODE_BIN" ]]; then
    printf '[%s] Unable to resolve Node.js/npm. npm=%s node=%s PATH=%s\n' \
      "$(timestamp)" "${NPM_BIN:-missing}" "${NODE_BIN:-missing}" "$PATH" >> "$FRONTEND_LOG"
    say_error "Node.js/npm could not be found. Install Node.js or add it to the macOS shell PATH. See $FRONTEND_LOG."
    return 1
  fi
  printf '[%s] Resolved node=%s npm=%s\n' "$(timestamp)" "$NODE_BIN" "$NPM_BIN" >> "$FRONTEND_LOG"
}

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

process_started_at() { ps -p "$1" -o lstart= 2>/dev/null | sed 's/^ *//'; }

process_pid() {
  local pid_file="$1" pid _metadata
  [[ -f "$pid_file" ]] || return 1
  IFS=$'\t' read -r pid _metadata < "$pid_file" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

owned_process_running() {
  local pid_file="$1" expected="$2"
  [[ -f "$pid_file" ]] || return 1
  local stored_pid stored_started current_started command
  stored_pid="$(process_pid "$pid_file")" || return 1
  IFS=$'\t' read -r _ stored_started < "$pid_file" || return 1
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

# --- Code identity -----------------------------------------------------------
# The launcher runs whatever is checked out in $REPO_DIR. These helpers make
# that visible (path + branch + SHA + dirty) and, when an expectation is
# configured, refuse to start a different checkout. Local git only; no network.
CHECKOUT_BRANCH=""
CHECKOUT_COMMIT=""
CHECKOUT_DIRTY=""
CHECKOUT_PATH=""

read_checkout_identity() {
  CHECKOUT_PATH="$(cd "$REPO_DIR" 2>/dev/null && pwd -P || printf '%s' "$REPO_DIR")"
  CHECKOUT_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
  CHECKOUT_BRANCH="$(git -C "$REPO_DIR" symbolic-ref -q --short HEAD 2>/dev/null || true)"
  if [[ -z "$CHECKOUT_COMMIT" ]]; then
    CHECKOUT_COMMIT="unknown"
    CHECKOUT_BRANCH="unknown"
    CHECKOUT_DIRTY="unknown"
    return 0
  fi
  [[ -n "$CHECKOUT_BRANCH" ]] || CHECKOUT_BRANCH="(detached)"
  local changes
  if changes="$(GIT_OPTIONAL_LOCKS=0 git -C "$REPO_DIR" status --porcelain --untracked-files=normal 2>/dev/null)"; then
    [[ -n "$changes" ]] && CHECKOUT_DIRTY="yes" || CHECKOUT_DIRTY="no"
  else
    CHECKOUT_DIRTY="unknown"
  fi
}

checkout_summary() {
  printf 'repo=%s branch=%s commit=%s dirty=%s' \
    "$CHECKOUT_PATH" "$CHECKOUT_BRANCH" "$CHECKOUT_COMMIT" "$CHECKOUT_DIRTY"
}

# Opt-in expectation: environment first, then one-line files in the runtime dir.
expected_value() {
  local env_value="$1" file="$RUNTIME_DIR/$2" value=""
  if [[ -n "$env_value" ]]; then
    value="$env_value"
  elif [[ -f "$file" ]]; then
    IFS= read -r value < "$file" || true
  fi
  printf '%s' "${value//[[:space:]]/}"
}

verify_expected_checkout() {
  local expected_branch expected_commit problem=""
  expected_branch="$(expected_value "${CLIPFORGE_EXPECTED_BRANCH:-}" expected-branch)"
  expected_commit="$(expected_value "${CLIPFORGE_EXPECTED_COMMIT:-}" expected-commit)"
  if [[ -n "$expected_branch" && "$expected_branch" != "$CHECKOUT_BRANCH" ]]; then
    problem="expected branch $expected_branch but $REPO_DIR is on $CHECKOUT_BRANCH"
  elif [[ -n "$expected_commit" && "$CHECKOUT_COMMIT" != "$expected_commit"* ]]; then
    problem="expected commit $expected_commit but $REPO_DIR is at $CHECKOUT_COMMIT"
  fi
  if [[ -n "$problem" ]]; then
    printf '[%s] Refusing to start: %s\n' "$(timestamp)" "$problem" >> "$BACKEND_LOG"
    say_error "Refusing to start: $problem."
    return 1
  fi
  return 0
}

# What the running backend reports about itself (path/branch/SHA/dirty).
backend_runtime_summary() {
  local body python_bin="$REPO_DIR/.venv/bin/python"
  body="$(curl --fail --silent --max-time 2 "$BACKEND_URL" 2>/dev/null || true)"
  if [[ -z "$body" ]]; then
    printf 'unavailable'
    return 0
  fi
  if [[ -x "$python_bin" ]]; then
    printf '%s' "$body" | "$python_bin" -c '
import json, sys
try:
    runtime = json.load(sys.stdin).get("runtime") or {}
except Exception:
    runtime = {}
dirty = runtime.get("dirty")
print("repo=%s branch=%s commit=%s dirty=%s" % (
    runtime.get("repo_path") or "unknown",
    runtime.get("branch") or ("(detached)" if runtime.get("commit") else "unknown"),
    runtime.get("commit") or "unknown",
    "unknown" if dirty is None else ("yes" if dirty else "no"),
), end="")
' 2>/dev/null || printf 'unparsed'
  else
    printf 'unparsed'
  fi
}

log_backend_runtime() {
  local how="$1" reported
  reported="$(backend_runtime_summary)"
  printf '[%s] Backend %s: %s\n' "$(timestamp)" "$how" "$reported" >> "$BACKEND_LOG"
  if [[ "$CHECKOUT_COMMIT" != "unknown" && "$reported" == repo=* && "$reported" != "repo=$CHECKOUT_PATH branch=$CHECKOUT_BRANCH commit=$CHECKOUT_COMMIT "* ]]; then
    printf '[%s] WARNING: running backend differs from launcher checkout (%s)\n' \
      "$(timestamp)" "$(checkout_summary)" >> "$BACKEND_LOG"
    osascript -e 'display notification "The running backend is not the launcher checkout. See .clipforge-runtime/backend.log." with title "ClipForge"' >/dev/null 2>&1 || true
  fi
}

prepare_checkout() {
  read_checkout_identity
  printf '[%s] Launcher checkout: %s\n' "$(timestamp)" "$(checkout_summary)" >> "$BACKEND_LOG"
  verify_expected_checkout
}

frontend_process_state() {
  local pid="none" alive="no" listening="no"
  pid="$(process_pid "$FRONTEND_PID_FILE" 2>/dev/null || printf 'none')"
  [[ "$pid" != "none" ]] && kill -0 "$pid" 2>/dev/null && alive="yes"
  lsof -nP -iTCP:3000 -sTCP:LISTEN >/dev/null 2>&1 && listening="yes"
  printf 'pid=%s alive=%s listening=%s' "$pid" "$alive" "$listening"
}

frontend_ready() {
  local url http_status curl_status process_state
  process_state="$(frontend_process_state)"
  for url in "${FRONTEND_READINESS_URLS[@]}"; do
    if http_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 1 "$url" 2>>"$FRONTEND_LOG")"; then
      curl_status=0
    else
      curl_status=$?
    fi
    printf '[%s] Frontend readiness attempt=%s elapsed=%ss url=%s http_status=%s curl_exit=%s %s\n' \
      "$(timestamp)" "$READINESS_ATTEMPT" "$READINESS_ELAPSED_SECONDS" "$url" \
      "${http_status:-none}" "$curl_status" "$process_state" >> "$FRONTEND_LOG"
    if [[ "$curl_status" -eq 0 && "$http_status" =~ ^2[0-9][0-9]$ ]]; then
      return 0
    fi
  done
  return 1
}

wait_until_ready() {
  local label="$1" check="$2"
  local attempt started_at
  started_at="$(date +%s)"
  for attempt in $(seq 1 "$READY_ATTEMPTS"); do
    READINESS_ATTEMPT="$attempt"
    READINESS_ELAPSED_SECONDS="$(( $(date +%s) - started_at ))"
    "$check" && return 0
    sleep 1
  done
  say_error "$label did not become ready. See $RUNTIME_DIR for local logs."
  return 1
}

start_backend() {
  if backend_ready; then
    log_backend_runtime "already running"
    return 0
  fi
  if owned_process_running "$BACKEND_PID_FILE" "clipforge.main:app"; then
    wait_until_ready "ClipForge backend" backend_ready || return 1
    log_backend_runtime "already starting"
    return 0
  fi
  rm -f "$BACKEND_PID_FILE"
  printf '[%s] Starting backend from %s\n' "$(timestamp)" "$(checkout_summary)" >> "$BACKEND_LOG"
  (
    cd "$REPO_DIR/apps/api"
    nohup "$REPO_DIR/.venv/bin/uvicorn" clipforge.main:app --reload --port 8000 \
      >> "$BACKEND_LOG" 2>&1 &
    record_process "$!" "$BACKEND_PID_FILE"
  )
  BACKEND_STARTED_BY_RUN=1
  wait_until_ready "ClipForge backend" backend_ready || return 1
  log_backend_runtime "started"
}

start_frontend() {
  frontend_ready && return 0
  resolve_node_tools || return 1
  if owned_process_running "$FRONTEND_PID_FILE" "npm run dev:web"; then
    wait_until_ready "ClipForge frontend" frontend_ready
    return
  fi
  rm -f "$FRONTEND_PID_FILE"
  printf '[%s] Starting frontend\n' "$(timestamp)" >> "$FRONTEND_LOG"
  (
    cd "$REPO_DIR"
    nohup "$NPM_BIN" run dev:web >> "$FRONTEND_LOG" 2>&1 &
    record_process "$!" "$FRONTEND_PID_FILE"
  )
  FRONTEND_STARTED_BY_RUN=1
  wait_until_ready "ClipForge frontend" frontend_ready
}

stop_owned_process() {
  local pid_file="$1" expected="$2"
  owned_process_running "$pid_file" "$expected" || { rm -f "$pid_file"; return 0; }
  local pid
  pid="$(process_pid "$pid_file")" || { rm -f "$pid_file"; return 0; }
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  local attempt
  for attempt in $(seq 1 5); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  pkill -KILL -P "$pid" 2>/dev/null || true
  rm -f "$pid_file"
}

cleanup_failed_start() {
  local status="${1:-$?}"
  printf '[%s] Startup cleanup status=%s backend_started=%s frontend_started=%s\n' \
    "$(timestamp)" "$status" "$BACKEND_STARTED_BY_RUN" "$FRONTEND_STARTED_BY_RUN" >> "$BACKEND_LOG"
  if [[ "$status" -ne 0 ]]; then
    if [[ "$FRONTEND_STARTED_BY_RUN" -eq 1 ]]; then
      stop_owned_process "$FRONTEND_PID_FILE" "npm run dev:web"
      rm -f "$FRONTEND_PID_FILE"
    fi
    if [[ "$BACKEND_STARTED_BY_RUN" -eq 1 ]]; then
      printf '[%s] Startup failed; stopping launcher-owned backend\n' "$(timestamp)" >> "$BACKEND_LOG"
      stop_owned_process "$BACKEND_PID_FILE" "clipforge.main:app"
      rm -f "$BACKEND_PID_FILE"
    fi
  fi
  return 0
}

stop_services() {
  stop_owned_process "$FRONTEND_PID_FILE" "npm run dev:web"
  stop_owned_process "$BACKEND_PID_FILE" "clipforge.main:app"
  osascript -e 'display notification "Stopped launcher-owned services." with title "ClipForge"' >/dev/null 2>&1 || true
}

status_services() {
  read_checkout_identity
  printf 'checkout: %s\n' "$(checkout_summary)"
  if backend_ready; then
    printf 'backend=ready\n'
    printf 'backend runtime: %s\n' "$(backend_runtime_summary)"
  else
    printf 'backend=not-ready\n'
  fi
  frontend_ready && printf 'frontend=ready\n' || printf 'frontend=not-ready\n'
}

main() {
  case "${1:---start}" in
    --stop) stop_services ;;
    --restart)
      stop_services
      mkdir -p "$RUNTIME_DIR"
      require_runtime
      prepare_checkout || return 1
      trap 'cleanup_failed_start "$?"' EXIT
      if ! start_backend; then
        cleanup_failed_start 1
        trap - EXIT
        return 1
      fi
      if ! start_frontend; then
        cleanup_failed_start 1
        trap - EXIT
        return 1
      fi
      trap - EXIT
      open "$FRONTEND_BROWSER_URL"
      ;;
    --status) status_services ;;
    --start)
      [[ -d "$REPO_DIR" ]] || { say_error "The ClipForge repository was not found at $REPO_DIR."; exit 1; }
      mkdir -p "$RUNTIME_DIR"
      require_runtime
      prepare_checkout || return 1
      trap 'cleanup_failed_start "$?"' EXIT
      if ! start_backend; then
        cleanup_failed_start 1
        trap - EXIT
        return 1
      fi
      if ! start_frontend; then
        cleanup_failed_start 1
        trap - EXIT
        return 1
      fi
      trap - EXIT
      open "$FRONTEND_BROWSER_URL"
      ;;
    *)
      printf 'Usage: %s [--start|--stop|--restart|--status]\n' "$0" >&2
      exit 64
      ;;
  esac
}

main "$@"
