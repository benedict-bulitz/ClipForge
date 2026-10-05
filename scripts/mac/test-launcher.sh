#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
test_root="$(mktemp -d /private/tmp/clipforge-launcher-test.XXXXXX)"
fake_repo="$test_root/repo"
fake_bin="$test_root/bin"
missing_bin="$test_root/missing-bin"
state_dir="$test_root/state"
cleanup() { [[ "${KEEP_TEST_ROOT:-0}" == "1" ]] || rm -rf "$test_root"; }
trap cleanup EXIT
mkdir -p "$fake_repo/.venv/bin" "$fake_repo/node_modules/.bin" "$fake_repo/apps/api" "$fake_bin" "$missing_bin" "$state_dir"
test "$(/usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$repo_dir/macos/ClipForge.app/Contents/Info.plist")" == "true"

cat > "$fake_repo/.venv/bin/uvicorn" <<'EOF'
#!/usr/bin/env bash
echo $$ > "$FAKE_STATE/backend-process.pid"
touch "$FAKE_STATE/backend-ready"
trap 'rm -f "$FAKE_STATE/backend-ready" "$FAKE_STATE/backend-process.pid"; exit 0' TERM INT EXIT
while :; do sleep 1; done
EOF
cat > "$fake_bin/npm" <<'EOF'
#!/usr/bin/env bash
if [[ "${FAKE_FRONTEND_FAIL:-0}" == "1" ]]; then
  exit 127
fi
echo $$ > "$FAKE_STATE/frontend-process.pid"
if [[ "${FAKE_FRONTEND_NEVER_READY:-0}" != "1" ]]; then
  touch "$FAKE_STATE/frontend-ready"
fi
trap 'rm -f "$FAKE_STATE/frontend-ready" "$FAKE_STATE/frontend-process.pid"; exit 0' TERM INT EXIT
while :; do sleep 1; done
EOF
cat > "$fake_bin/ps" <<'EOF'
#!/usr/bin/env bash
pid=""
for arg in "$@"; do
  [[ "$arg" =~ ^[0-9]+$ ]] && pid="$arg"
done
if [[ "$*" == *"lstart"* ]]; then
  printf 'Mon Jan 1 00:00:00 2026\n'
elif [[ "$*" == *"command="* ]]; then
  if [[ -f "$FAKE_STATE/backend-process.pid" && "$pid" == "$(<"$FAKE_STATE/backend-process.pid")" ]]; then
    printf 'uvicorn clipforge.main:app --reload --port 8000\n'
  elif [[ -f "$FAKE_STATE/frontend-process.pid" && "$pid" == "$(<"$FAKE_STATE/frontend-process.pid")" ]]; then
    printf 'npm run dev:web\n'
  fi
fi
EOF
cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_STATE/curl.log"
case "$*" in
  *8000*) test -f "$FAKE_STATE/backend-ready" && printf '%s' "${FAKE_HEALTH_BODY:-200}" ;;
  *localhost:3000*)
    [[ "${FAKE_FRONTEND_HOST:-both}" != "127" ]] && test -f "$FAKE_STATE/frontend-ready" && printf '200'
    ;;
  *127.0.0.1:3000*)
    [[ "${FAKE_FRONTEND_HOST:-both}" != "localhost" ]] && test -f "$FAKE_STATE/frontend-ready" && printf '200'
    ;;
  *) exit 1 ;;
esac
EOF
cat > "$fake_bin/open" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_STATE/opened"
EOF
chmod +x "$fake_repo/.venv/bin/uvicorn" "$fake_bin/npm" "$fake_bin/curl" "$fake_bin/open" "$fake_bin/ps"
cat > "$missing_bin/node" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$missing_bin/node"
ln -s "$fake_bin/curl" "$missing_bin/curl"
ln -s "$fake_bin/open" "$missing_bin/open"
ln -s "$fake_bin/ps" "$missing_bin/ps"
touch "$fake_repo/node_modules/.bin/next"
chmod +x "$fake_repo/node_modules/.bin/next"

printf '.clipforge-runtime/\n.venv/\nnode_modules/\n' > "$fake_repo/.gitignore"
git -C "$fake_repo" init -q -b launcher-branch
git -C "$fake_repo" add .gitignore
git -C "$fake_repo" -c user.name=test -c user.email=test@example.invalid commit -q -m init
fake_commit="$(git -C "$fake_repo" rev-parse HEAD)"
fake_repo_physical="$(cd "$fake_repo" && pwd -P)"

launcher=(env "PATH=$fake_bin:$PATH" "FAKE_STATE=$state_dir" "CLIPFORGE_REPO_DIR=$fake_repo" "$repo_dir/scripts/mac/clipforge-local.sh")
"${launcher[@]}" --start
backend_pid="$(<"$state_dir/backend-process.pid")"
frontend_pid="$(<"$state_dir/frontend-process.pid")"
kill -0 "$backend_pid"
kill -0 "$frontend_pid"
backend_record_before="$(<"$fake_repo/.clipforge-runtime/backend.pid")"
frontend_record_before="$(<"$fake_repo/.clipforge-runtime/frontend.pid")"
"${launcher[@]}" --start
test "$(<"$fake_repo/.clipforge-runtime/backend.pid")" == "$backend_record_before"
test "$(<"$fake_repo/.clipforge-runtime/frontend.pid")" == "$frontend_record_before"
test "$(grep -c 'Starting backend' "$fake_repo/.clipforge-runtime/backend.log")" -eq 1
# Every start records exactly which checkout (path + branch + SHA + dirty) it ran.
grep -q "Launcher checkout: repo=$fake_repo_physical branch=launcher-branch commit=$fake_commit dirty=no" "$fake_repo/.clipforge-runtime/backend.log"
grep -q "Starting backend from repo=$fake_repo_physical branch=launcher-branch commit=$fake_commit dirty=no" "$fake_repo/.clipforge-runtime/backend.log"
grep -q 'Backend started: ' "$fake_repo/.clipforge-runtime/backend.log"
grep -q 'Backend already running: ' "$fake_repo/.clipforge-runtime/backend.log"
status_output="$("${launcher[@]}" --status)"
grep -q "checkout: repo=$fake_repo_physical branch=launcher-branch commit=$fake_commit dirty=no" <<< "$status_output"
test "$(grep -c 'Starting frontend' "$fake_repo/.clipforge-runtime/frontend.log")" -eq 1
grep -q "npm=$fake_bin/npm" "$fake_repo/.clipforge-runtime/frontend.log"
grep -q 'url=http://localhost:3000/ http_status=200 curl_exit=0' "$fake_repo/.clipforge-runtime/frontend.log"
test "$(wc -l < "$state_dir/opened")" -eq 2
grep -qx 'http://localhost:3000' "$state_dir/opened"
grep -Eq $'^[0-9]+\tMon Jan 1 00:00:00 2026$' "$fake_repo/.clipforge-runtime/backend.pid"
grep -Eq $'^[0-9]+\tMon Jan 1 00:00:00 2026$' "$fake_repo/.clipforge-runtime/frontend.pid"
cp "$fake_repo/.clipforge-runtime/backend.pid" "$state_dir/backend-pid-before-stop"
sleep 1000 & unrelated_pid=$!
"${launcher[@]}" --stop
kill -0 "$unrelated_pid"
kill "$unrelated_pid" 2>/dev/null || true
wait "$unrelated_pid" 2>/dev/null || true
test ! -e "$fake_repo/.clipforge-runtime/backend.pid"
test ! -e "$fake_repo/.clipforge-runtime/frontend.pid"
test ! -e "$state_dir/backend-process.pid"

rm -f "$state_dir/backend-ready" "$state_dir/frontend-ready" "$state_dir/curl.log"
FAKE_FRONTEND_HOST=127 "${launcher[@]}" --start
grep -q 'url=http://localhost:3000/ http_status=none curl_exit=1' "$fake_repo/.clipforge-runtime/frontend.log"
grep -q 'url=http://127.0.0.1:3000/ http_status=200 curl_exit=0' "$fake_repo/.clipforge-runtime/frontend.log"
"${launcher[@]}" --stop

rm -f "$state_dir/backend-ready" "$state_dir/frontend-ready"
set +e
FAKE_FRONTEND_FAIL=1 CLIPFORGE_READY_ATTEMPTS=1 "${launcher[@]}" --start
startup_status=$?
set -e
test "$startup_status" -ne 0
test ! -e "$state_dir/backend-ready"
test ! -e "$state_dir/backend-process.pid"
test ! -e "$fake_repo/.clipforge-runtime/backend.pid"
test ! -e "$fake_repo/.clipforge-runtime/frontend.pid"
grep -q "Resolved node=.* npm=$fake_bin/npm" "$fake_repo/.clipforge-runtime/frontend.log"
grep -q 'Frontend readiness attempt=1 elapsed=.*url=http://localhost:3000/ http_status=none curl_exit=1' "$fake_repo/.clipforge-runtime/frontend.log"

rm -f "$state_dir/backend-ready" "$state_dir/frontend-ready"
set +e
FAKE_FRONTEND_NEVER_READY=1 CLIPFORGE_READY_ATTEMPTS=1 "${launcher[@]}" --start
timeout_status=$?
set -e
test "$timeout_status" -ne 0
test ! -e "$state_dir/backend-process.pid"
test ! -e "$state_dir/frontend-process.pid"
test ! -e "$fake_repo/.clipforge-runtime/backend.pid"
test ! -e "$fake_repo/.clipforge-runtime/frontend.pid"

rm -f "$state_dir/backend-ready" "$state_dir/backend-process.pid"
set +e
CLIPFORGE_NODE_SEARCH_PATHS="$missing_bin" CLIPFORGE_READY_ATTEMPTS=1 \
  env "PATH=$missing_bin:/usr/bin:/bin" "FAKE_STATE=$state_dir" "CLIPFORGE_REPO_DIR=$fake_repo" \
  "$repo_dir/scripts/mac/clipforge-local.sh" --start
missing_npm_status=$?
set -e
test "$missing_npm_status" -ne 0
grep -q "Unable to resolve Node.js/npm" "$fake_repo/.clipforge-runtime/frontend.log"
test ! -e "$state_dir/backend-process.pid"
test ! -e "$fake_repo/.clipforge-runtime/backend.pid"

# An opt-in expectation refuses a different checkout before anything starts.
rm -f "$state_dir/backend-ready" "$state_dir/backend-process.pid" "$state_dir/frontend-ready" "$state_dir/frontend-process.pid"
set +e
CLIPFORGE_EXPECTED_BRANCH=some-other-branch "${launcher[@]}" --start
wrong_branch_status=$?
set -e
test "$wrong_branch_status" -ne 0
test ! -e "$state_dir/backend-process.pid"
grep -q 'Refusing to start: expected branch some-other-branch but .* is on launcher-branch' "$fake_repo/.clipforge-runtime/backend.log"
printf 'deadbeef\n' > "$fake_repo/.clipforge-runtime/expected-commit"
set +e
"${launcher[@]}" --start
wrong_commit_status=$?
set -e
test "$wrong_commit_status" -ne 0
test ! -e "$state_dir/backend-process.pid"
grep -q "Refusing to start: expected commit deadbeef but .* is at $fake_commit" "$fake_repo/.clipforge-runtime/backend.log"
printf '%s\n' "${fake_commit:0:12}" > "$fake_repo/.clipforge-runtime/expected-commit"
CLIPFORGE_EXPECTED_BRANCH=launcher-branch "${launcher[@]}" --start
kill -0 "$(<"$state_dir/backend-process.pid")"
"${launcher[@]}" --stop
rm -f "$fake_repo/.clipforge-runtime/expected-commit"

# The backend's own reported identity is logged; a different checkout warns.
ln -s "$(command -v python3)" "$fake_repo/.venv/bin/python"
rm -f "$state_dir/backend-ready" "$state_dir/frontend-ready"
matching_health="{\"status\":\"ok\",\"runtime\":{\"repo_path\":\"$fake_repo_physical\",\"branch\":\"launcher-branch\",\"commit\":\"$fake_commit\",\"dirty\":false}}"
FAKE_HEALTH_BODY="$matching_health" "${launcher[@]}" --start
grep -q "Backend started: repo=$fake_repo_physical branch=launcher-branch commit=$fake_commit dirty=no" "$fake_repo/.clipforge-runtime/backend.log"
test "$(grep -c 'WARNING: running backend differs' "$fake_repo/.clipforge-runtime/backend.log" || true)" -eq 0
other_health='{"status":"ok","runtime":{"repo_path":"/elsewhere/ClipForge","branch":"old-branch","commit":"9a88b86000000000000000000000000000000000","dirty":true}}'
FAKE_HEALTH_BODY="$other_health" "${launcher[@]}" --start
grep -q 'Backend already running: repo=/elsewhere/ClipForge branch=old-branch commit=9a88b86000000000000000000000000000000000 dirty=yes' "$fake_repo/.clipforge-runtime/backend.log"
grep -q 'WARNING: running backend differs from launcher checkout' "$fake_repo/.clipforge-runtime/backend.log"
"${launcher[@]}" --stop
rm -f "$fake_repo/.venv/bin/python"

# A dirty checkout is reported as dirty.
touch "$fake_repo/uncommitted-patch.py"
rm -f "$state_dir/backend-ready" "$state_dir/frontend-ready"
"${launcher[@]}" --start
grep -q "Launcher checkout: repo=$fake_repo_physical branch=launcher-branch commit=$fake_commit dirty=yes" "$fake_repo/.clipforge-runtime/backend.log"
"${launcher[@]}" --stop
printf 'macOS launcher duplicate/start/stop test passed\n'
