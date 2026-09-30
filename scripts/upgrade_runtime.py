#!/usr/bin/env python3
"""Switch an idle shared service to the reviewed source runtime."""

import argparse
import json
import math
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
    parser.add_argument("--drain-timeout", type=float, default=5,
                        help="Seconds to allow idle analysis services to release their leases")
    args = parser.parse_args()
    if not math.isfinite(args.drain_timeout) or not 0 <= args.drain_timeout <= 60:
        parser.error("--drain-timeout must be between 0 and 60")
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
    deadline = time.monotonic() + args.drain_timeout
    while not state["can_stop"]:
        try:
            # Queued jobs are durable and have never received resource grants.
            # Preserve them across an idle switch; unknown or active IDs block it.
            queued = set(state["queued"]) if "queued" in state else {
                job["id"] for job in client.call("jobs.list") if job["state"] == "queued"
            }
            blocking = set(state["active"]) - queued
        except BaseException:
            client.call("service.resume")
            raise
        if blocking:
            if time.monotonic() < deadline:
                time.sleep(0.1)
                try:
                    state = client.call("service.drain")
                except BaseException:
                    client.call("service.resume")
                    raise
                continue
            client.call("service.resume")
            raise SystemExit(
                "Existing work must finish before upgrading: " + ",".join(sorted(blocking))
            )
        break
    try:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure_project.py"),
                str(args.project.resolve()),
                "--update",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        client.call("service.resume")
        raise
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
