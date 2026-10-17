#!/usr/bin/env python3
"""Compare reviewed source, installed plugin payload and the live runtime."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_plugin
from scripts.install_runtime import runtime_sources
from workbench.common import rpc, state_dir


def compare_installation(installed, runtime_state=None):
    expected = build_plugin.release_manifest(build_plugin.validate())
    changes = []
    for name, checksum in expected["files"].items():
        target = installed / name
        if not target.is_file() or target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
            changes.append(name)
    for folder in build_plugin.DIRECTORIES:
        for path in (installed / folder).rglob("*"):
            relative = path.relative_to(installed)
            if build_plugin.excluded(relative) or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_file() and relative.as_posix() not in expected["files"]:
                changes.append(relative.as_posix())
    record = installed / "release-manifest.json"
    if not record.is_file() or json.loads(record.read_text()) != expected:
        changes.append("release-manifest.json")
    runtime = None
    if runtime_state is not None:
        files, fingerprint = runtime_sources(ROOT)
        current = json.loads((runtime_state / "current-runtime.json").read_text())
        live = rpc(runtime_state, "service.capabilities")
        target = Path(live["runtime_source"])
        relative_files = {p.relative_to(ROOT / "workbench").as_posix(): p for p in files}
        drift = [name for name, p in relative_files.items() if not (target / "workbench" / name).is_file() or p.read_bytes() != (target / "workbench" / name).read_bytes()]
        drift += [p.relative_to(target / "workbench").as_posix() for p in (target / "workbench").rglob("*.py") if p.relative_to(target / "workbench").as_posix() not in relative_files]
        runtime = {"matches": current["fingerprint"] == fingerprint and not drift and live["version"] == expected["version"] and target == Path(current["command"][1]).parent, "changed_files": drift, "version": live["version"], "fingerprint": current["fingerprint"]}
    return {"pass": not changes and (runtime is None or runtime["matches"]), "version": expected["version"], "fingerprint": expected["fingerprint"], "changed_files": sorted(set(changes)), "runtime": runtime}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path, required=True)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    try:
        result = compare_installation(args.installed.expanduser().resolve(), state_dir(args.state) if args.runtime else None)
    except (OSError, ValueError, KeyError) as exc:
        result = {"pass": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
