#!/usr/bin/env bash
set -euo pipefail
export OPENPILOT_ROOT="${OPENPILOT_ROOT:-$HOME/openpilot}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --project "$OPENPILOT_ROOT" --with "mcp,pillow,python-xlib" python "$DIR/run_server.py"
