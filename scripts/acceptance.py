#!/usr/bin/env python3
"""Repeat the read-only two-device acceptance using independent CLI clients."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.client import Client
from workbench.common import atomic_json, rpc, state_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--devices", nargs=2, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    if args.output.exists():
        raise SystemExit("Use a new output file")
    session = "acceptance-" + uuid.uuid4().hex
    client = Client(project, session=session + "-reader")

    def submit(suffix, **spec):
        spec["request_key"] = session + "-" + suffix
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/workbench.py"),
                "--project",
                str(project),
                "--session",
                session + "-" + suffix,
                "submit",
                "-",
            ],
            input=json.dumps(spec),
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout)

    parent = submit(
        "parent",
        operation="device.scene",
        device=args.devices[0],
        timeout=40,
        purpose="Read-only parent with one observation checkpoint",
        steps=[
            {"action": "observe"},
            {
                "action": "checkpoint",
                "seconds": 6,
                "budget": 8,
                "max_insertions": 1,
                "allow": ["device.screenshot"],
            },
            {"action": "observe"},
        ],
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if client.call("jobs.status", {"id": parent["id"]})["state"] in (
            "running",
            "paused",
        ):
            break
        time.sleep(0.05)
    queued = submit(
        "queued",
        operation="device.scene",
        device=args.devices[0],
        timeout=15,
        purpose="Independent same-device scene must wait",
        steps=[{"action": "observe"}],
    )
    child = submit(
        "child",
        operation="device.screenshot",
        device=args.devices[0],
        scene=parent["id"],
        estimate=3,
        timeout=10,
        queue_timeout=20,
        purpose="Scene-bound compatible screenshot",
    )
    other = submit(
        "other",
        operation="device.observe",
        device=args.devices[1],
        timeout=15,
        purpose="Second device can run concurrently",
    )
    jobs = {
        name: client.wait(job["id"], 60)
        for name, job in [
            ("parent", parent),
            ("queued", queued),
            ("child", child),
            ("other", other),
        ]
    }
    p, q, c, o = (jobs[name] for name in ("parent", "queued", "child", "other"))
    checks = {
        "all_succeeded": all(x["state"] == "succeeded" for x in jobs.values()),
        "same_controller_insertion": c["parent"] == p["id"],
        "inserted_before_parent_end": bool(
            c["finished"] and p["finished"] and c["finished"] < p["finished"]
        ),
        "same_device_waited": bool(
            q["started"] and p["finished"] and q["started"] >= p["finished"]
        ),
        "other_device_overlapped": bool(
            o["started"] and p["finished"] and o["started"] < p["finished"]
        ),
    }
    record = {
        "scope": "read-only observe and screenshot; separate CLI client processes",
        "checks": checks,
        "jobs": jobs,
        "service": rpc(state_dir(), "service.capabilities"),
    }
    atomic_json(args.output, record)
    print(json.dumps(checks, indent=2))
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
