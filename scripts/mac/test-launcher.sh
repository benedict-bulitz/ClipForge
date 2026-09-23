#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
test_root="$(mktemp -d /private/tmp/clipforge-launcher-test.XXXXXX)"
fake_repo="$test_root/repo"
fake_bin="$test_root/bin"
state_dir="$test_root/state"
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$fake_repo/.venv/bin" "$fake_repo/node_modules/.bin" "$fake_repo/apps/api" "$fake_bin" "$state_dir"

cat > "$fake_repo/.venv/bin/uvicorn" <<'EOF'
#!/usr/bin/env bash
touch "$FAKE_STATE/backend-ready"
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF
cat > "$fake_bin/npm" <<'EOF'
#!/usr/bin/env bash
touch "$FAKE_STATE/frontend-ready"
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF
cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *8000*) test -f "$FAKE_STATE/backend-ready" ;;
  *3000*) test -f "$FAKE_STATE/frontend-ready" ;;
  *) exit 1 ;;
esac
EOF
cat > "$fake_bin/open" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_STATE/opened"
EOF
chmod +x "$fake_repo/.venv/bin/uvicorn" "$fake_bin/npm" "$fake_bin/curl" "$fake_bin/open"
touch "$fake_repo/node_modules/.bin/next"
chmod +x "$fake_repo/node_modules/.bin/next"

launcher=(env "PATH=$fake_bin:$PATH" "FAKE_STATE=$state_dir" "CLIPFORGE_REPO_DIR=$fake_repo" "$repo_dir/scripts/mac/clipforge-local.sh")
"${launcher[@]}" --start
"${launcher[@]}" --start
test "$(grep -c 'Starting backend' "$fake_repo/.clipforge-runtime/backend.log")" -eq 1
test "$(grep -c 'Starting frontend' "$fake_repo/.clipforge-runtime/frontend.log")" -eq 1
test "$(wc -l < "$state_dir/opened")" -eq 2
"${launcher[@]}" --stop
test ! -e "$fake_repo/.clipforge-runtime/backend.pid"
test ! -e "$fake_repo/.clipforge-runtime/frontend.pid"
printf 'macOS launcher duplicate/start/stop test passed\n'
