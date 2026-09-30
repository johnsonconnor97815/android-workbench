#!/usr/bin/env python3
"""Recover an interrupted device-manager image cache download."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def image_cache_directories(normalized: dict) -> list[Path]:
    directories = []
    args = normalized.get("args", [])
    for index, value in enumerate(args[:-1]):
        if value == "--cache-dir":
            directories.append(Path(args[index + 1]).expanduser().resolve())
    if directories:
        return directories
    for item in normalized.get("resources", []):
        key = item.get("key", "")
        if (
            item.get("mode") == "write"
            and key.startswith("path:")
            and key.endswith("/device-manager/images")
        ):
            directories.append(Path(key[len("path:") :]))
    return directories


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    result = {
        "pass": False,
        "cleanup_errors": [],
        "recovered_parts": [],
    }
    try:
        grant = json.loads(Path(os.environ["AWB_INTERNAL_GRANT"]).read_text())
        targets = grant["spec"].get("recovery_targets", [])
        if not targets:
            raise RuntimeError("No interrupted image download was identified")
        for identifier in targets:
            normalized_path = (
                Path(grant["state"]) / "jobs" / identifier / "normalized.json"
            )
            normalized = json.loads(normalized_path.read_text())
            if normalized.get("operation") not in {
                "device_manager.image_download",
                "device_manager.recover_image_cache",
            }:
                raise RuntimeError("Recovery target is not an image cache operation")
            directories = image_cache_directories(normalized)
            if not directories:
                raise RuntimeError("Image download did not identify its cache directory")
            for directory in directories:
                if not directory.is_dir():
                    raise RuntimeError(f"Image cache directory is missing: {directory}")
                for partial in directory.glob("*.part*"):
                    partial.unlink()
                    result["recovered_parts"].append(str(partial))
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
