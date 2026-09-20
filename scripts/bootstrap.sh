#!/usr/bin/env bash
set -euo pipefail

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workspace_dir"

python_bin="${PYTHON_BIN:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "ClipForge requires Python 3.11+"'
"$python_bin" -m venv .venv
.venv/bin/pip install -e "apps/api[dev,visual]"
.venv/bin/python -m pytest --version
.venv/bin/python -c 'import faster_whisper; print("Local word alignment dependency ready")'
npm install

echo "ClipForge is ready. Run: npm run dev"
