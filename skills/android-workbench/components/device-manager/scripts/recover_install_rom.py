#!/usr/bin/env python3
"""Confirm that an interrupted ROM installation is waiting in sideload mode."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil

from recover_flash_factory import fastboot_transport


def main(*, ota=False) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--fastboot-serial")
    parser.add_argument("--stage", choices=("prepare", "sideload"), default="sideload")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Use a new output path; existing evidence is retained")

    result = {
        "pass": False,
        "cleanup_errors": [],
        "recovery_targets": [],
        "adb_state": None,
        "system_verified": False,
        "safe_to_retry": False,
        "stage": arguments.stage,
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
            allowed = {"device_manager.install_ota", "device_manager.recover_ota"} if ota else {
                "device_manager.install_rom", "device_manager.recover_install_rom"}
            if normalized.get("operation") not in allowed:
                raise RuntimeError("Recovery target is not a ROM installation")
            original_args = normalized.get("args", [])
            def values(flag):
                return [original_args[i + 1] for i, value in enumerate(original_args[:-1]) if value == flag] + [
                    value.split("=", 1)[1] for value in original_args if value.startswith(flag + "=")]
            stages = values("--stage")
            if len(stages) > 1 or (stages[0] if stages else "sideload") != arguments.stage:
                raise RuntimeError("Recovery stage differs from the interrupted job")
            serials = values("--fastboot-serial")
            if len(serials) > 1 or (serials[0] if serials else arguments.serial) != (
                    arguments.fastboot_serial or arguments.serial):
                raise RuntimeError("Fastboot serial differs from the interrupted job")
            result["recovery_targets"].append(identifier)
        if arguments.stage == "prepare" and not ota:
            fastboot = shutil.which("fastboot")
            if fastboot is None:
                raise RuntimeError("fastboot executable not found in PATH")
            command = [fastboot, "devices"]
        else:
            command = [spec["adb"], "-s", arguments.serial, "get-state"]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        state = (fastboot_transport(completed.stdout, arguments.fastboot_serial or arguments.serial)
                 if arguments.stage == "prepare" and not ota else completed.stdout.strip())
        result["fastboot_state" if arguments.stage == "prepare" and not ota else "adb_state"] = state
        if completed.returncode != 0:
            raise RuntimeError(
                "ADB state check failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        permitted = ({"recovery", "sideload"} if ota else {"fastboot", "fastbootd"}) if arguments.stage == "prepare" else {"sideload"}
        if state not in permitted:
            raise RuntimeError(
                "ROM recovery requires the phone in the interrupted stage's mode, got: "
                + str(state)
            )
        result["pass"] = True
    except BaseException as error:
        result["cleanup_errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
