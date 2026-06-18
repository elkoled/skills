#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENPILOT_ROOT:-$HOME/openpilot}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --project "$ROOT" --with "mcp,numpy" python "$DIR/run_server.py"
