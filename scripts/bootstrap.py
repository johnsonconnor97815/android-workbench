#!/usr/bin/env python3
"""Install the shared scheduler and initialize an independent analysis project."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--python", type=Path, help="Interpreter used by project operations"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rebind existing built-ins; preserve custom adapters",
    )
    parser.add_argument(
        "--configure-mcp",
        action="store_true",
        help="Add the project MCP connector and wrap existing Android MCP servers",
    )
    args = parser.parse_args()
    if args.configure_mcp and args.state:
        parser.error(
            "--configure-mcp uses the default shared state; set ANDROID_WORKBENCH_STATE for a custom state"
        )
    state = ["--state", str(args.state)] if args.state else []
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/install_runtime.py"), *state], check=True
    )
    command = [
        sys.executable,
        str(ROOT / "scripts/configure_project.py"),
        str(args.project),
    ]
    if args.python:
        command += ["--python", str(args.python)]
    if args.update:
        command.append("--update")
    subprocess.run(command, check=True)
    if args.configure_mcp:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/configure_mcp.py"), str(args.project)],
            check=True,
        )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/workbench.py"),
            *state,
            "--project",
            str(args.project),
            "start",
        ],
        check=True,
    )
    print("Project ready: " + str(args.project.resolve()))
    print(
        "Android SDK, analysis tools and Frida are installed separately through the bundled Skills."
    )


if __name__ == "__main__":
    main()
