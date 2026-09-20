#!/usr/bin/env python3
"""Install the scheduler into a versioned private runtime, separate from project .venv."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from workbench.common import atomic_json, state_dir, rpc, WorkbenchError

    directory = state_dir(args.state)
    files = sorted((ROOT / "workbench").glob("*.py"))
    fingerprint = hashlib.sha256(
        b"".join(p.name.encode() + p.read_bytes() for p in files)
    ).hexdigest()[:16]
    target = directory / "runtimes" / fingerprint
    if not target.exists():
        target.mkdir(parents=True, mode=0o700)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(target / "venv")],
            check=True,
        )
        shutil.copytree(
            ROOT / "workbench",
            target / "workbench",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        launcher = target / "run.py"
        launcher.write_text("from workbench.cli import main\nmain()\n")
        atomic_json(
            target / "source.json",
            {
                "fingerprint": fingerprint,
                "files": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files
                },
            },
        )
    command = [str(target / "venv/bin/python"), str(target / "run.py")]
    current = directory / "current-runtime.json"
    if (
        current.exists()
        and json.loads(current.read_text())["fingerprint"] != fingerprint
    ):
        try:
            active = rpc(directory, "service.capabilities")
        except WorkbenchError:
            active = None
        if active:
            raise SystemExit(
                "New runtime built, but service is active. Drain and stop it before switching: "
                + str(target)
            )
    atomic_json(current, {"fingerprint": fingerprint, "command": command})
    print(
        json.dumps(
            {"runtime": str(target), "command": command, "state": str(directory)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
