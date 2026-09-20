#!/usr/bin/env python3
"""Called by shell compatibility entrypoints before their original body."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.bridge import entry, locate_project

if locate_project(sys.argv[1]) is None:
    import os

    os.chdir(str(Path(sys.argv[1]).resolve().parents[1]))
entry(sys.argv[1], sys.argv[2:])
