#!/usr/bin/env python3
"""Confirm that an interrupted factory flash is still reachable over fastboot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def fastboot_transport(output: str, serial: str) -> str | None:
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == serial:
            return fields[-1]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    result = {
        "pass": False,
        "cleanup_errors": [],
        "recovery_targets": [],
        "fastboot_state": None,
    }
    try:
        grant = json.loads(Path(os.environ["AWB_INTERNAL_GRANT"]).read_text())
        spec = grant["spec"]
        if arguments.serial != spec["serial"]:
            raise RuntimeError("Serial does not match the recovery target")
        targets = spec.get("recovery_targets", [])
        if not targets:
            raise RuntimeError("No interrupted factory flash was identified")
        for identifier in targets:
            normalized_path = (
                Path(grant["state"]) / "jobs" / identifier / "normalized.json"
            )
            normalized = json.loads(normalized_path.read_text())
            if normalized.get("operation") not in {
                "device_manager.flash_factory",
                "device_manager.recover_flash_factory",
            }:
                raise RuntimeError("Recovery target is not a factory flash")
            result["recovery_targets"].append(identifier)

        fastboot = shutil.which("fastboot")
        if fastboot is None:
            raise RuntimeError("fastboot executable not found in PATH")
        completed = subprocess.run(
            [fastboot, "devices"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        state = fastboot_transport(completed.stdout, arguments.serial)
        result["fastboot_state"] = state
        if completed.returncode != 0:
            raise RuntimeError(
                "fastboot state check failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        if state not in {"fastboot", "fastbootd"}:
            raise RuntimeError(
                "Factory-flash recovery requires fastboot or fastbootd, got: "
                + str(state)
            )
        result["pass"] = True
    except BaseException as error:
        result["cleanup_errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        if arguments.output.exists():
            result["cleanup_errors"].append("ValueError: output already exists")
            result["pass"] = False
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
