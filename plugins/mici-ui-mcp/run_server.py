#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from mici_ui_mcp.server import main  # noqa: E402

if __name__ == "__main__":
  main()
