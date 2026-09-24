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
touch "$FAKE_STATE/frontend-ready"
echo $$ > "$FAKE_STATE/frontend-process.pid"
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
  *8000*) test -f "$FAKE_STATE/backend-ready" && printf '200' ;;
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

launcher=(env "PATH=$fake_bin:$PATH" "FAKE_STATE=$state_dir" "CLIPFORGE_REPO_DIR=$fake_repo" "$repo_dir/scripts/mac/clipforge-local.sh")
"${launcher[@]}" --start
"${launcher[@]}" --start
test "$(grep -c 'Starting backend' "$fake_repo/.clipforge-runtime/backend.log")" -eq 1
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
printf 'macOS launcher duplicate/start/stop test passed\n'
