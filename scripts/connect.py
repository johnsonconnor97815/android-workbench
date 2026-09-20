#!/usr/bin/env python3
"""Use the installed scheduler Python for MCP even when project Python changes."""

import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from workbench.common import state_dir

runtime = state_dir() / "current-runtime.json"
if not runtime.is_file():
    raise SystemExit(
        "Install Android Workbench runtime first: scripts/install_runtime.py"
    )
command = json.loads(runtime.read_text())["command"]
os.execv(command[0], [*command, *sys.argv[1:]])
