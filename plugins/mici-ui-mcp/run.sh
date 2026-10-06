#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# own small env, independent of the openpilot venv. uv caches it so later starts are instant
exec uv run --quiet --no-project --with "mcp>=2.3,<3" --with pillow --with python-xlib python "$DIR/run_server.py"
