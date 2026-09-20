#!/usr/bin/env python3
"""Register bundled operations in any analysis project; retain optional extensions."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.common import atomic_json
from workbench.project import generate, update


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rebind built-ins, preserving custom operations and environments",
    )
    args = parser.parse_args()
    root = args.project.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = args.output or root / "workbench.project.json"
    generated = generate(root, ROOT, args.python)
    if target.exists():
        if not args.update:
            raise SystemExit(
                "Registry already exists; use --update to rebind built-ins"
            )
        before = target.read_text()
        generated = update(json.loads(before), generated, args.python)
        if json.loads(before) == generated:
            print(target)
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = root / "evidence/workbench-config" / (stamp + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(before)
        if target.read_text() != before:
            raise SystemExit("Registry changed during update; retry")
    atomic_json(target, generated)
    print(target)


if __name__ == "__main__":
    main()
