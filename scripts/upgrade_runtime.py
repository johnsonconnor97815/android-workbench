#!/usr/bin/env python3
"""Switch an idle shared service to the reviewed source runtime."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.client import Client
from workbench.common import (
    rpc,
    state_dir,
    process_identity,
    same_process,
)
from workbench.cli import start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    directory = state_dir()
    client = Client(args.project)
    capabilities = rpc(directory, "service.capabilities")
    # Compare the live service identity; never kill an arbitrary PID from an old file.
    current = json.loads((directory / "current-runtime.json").read_text())
    if Path(capabilities["runtime_source"]) != Path(current["command"][1]).parent:
        raise SystemExit(
            "Live runtime differs from installed pointer; inspect before upgrading"
        )
    state = client.call("service.drain")
    if not state["can_stop"]:
        client.call("service.resume")
        raise SystemExit(
            "Existing work must finish before upgrading: " + ",".join(state["active"])
        )
    identity = process_identity(capabilities["pid"])
    os.kill(identity["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while same_process(identity) and time.monotonic() < deadline:
        time.sleep(0.1)
    if same_process(identity):
        raise SystemExit("Service did not exit; runtime unchanged")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/install_runtime.py")], check=True
    )
    print(json.dumps(start(directory), indent=2))


if __name__ == "__main__":
    main()
