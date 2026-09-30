#!/usr/bin/env python3
"""Confirm that an interrupted ROM installation is waiting in sideload mode."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    result = {
        "pass": False,
        "cleanup_errors": [],
        "recovery_targets": [],
        "adb_state": None,
    }
    try:
        grant = json.loads(Path(os.environ["AWB_INTERNAL_GRANT"]).read_text())
        spec = grant["spec"]
        if arguments.serial != spec["serial"]:
            raise RuntimeError("Serial does not match the recovery target")
        targets = spec.get("recovery_targets", [])
        if not targets:
            raise RuntimeError("No interrupted ROM installation was identified")
        for identifier in targets:
            normalized_path = (
                Path(grant["state"]) / "jobs" / identifier / "normalized.json"
            )
            normalized = json.loads(normalized_path.read_text())
            if normalized.get("operation") not in {
                "device_manager.install_rom",
                "device_manager.recover_install_rom",
            }:
                raise RuntimeError("Recovery target is not a ROM installation")
            result["recovery_targets"].append(identifier)
        completed = subprocess.run(
            [spec["adb"], "-s", arguments.serial, "get-state"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        state = completed.stdout.strip()
        result["adb_state"] = state
        if completed.returncode != 0:
            raise RuntimeError(
                "ADB state check failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        if state != "sideload":
            raise RuntimeError(
                "ROM recovery requires the phone to wait in sideload mode, got: "
                + state
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
