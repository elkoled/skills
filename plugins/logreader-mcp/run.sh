#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENPILOT_ROOT:-$HOME/openpilot}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# the checkout's own venv (compiled cereal/opendbc) with mcp layered on top. uv run --project
# would sync that venv and rewrite its uv.lock on every launch
exec uv run --quiet --no-project --python "$ROOT/.venv/bin/python" --with "mcp>=2.3,<3" python "$DIR/run_server.py"
