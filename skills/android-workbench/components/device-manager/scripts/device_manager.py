#!/usr/bin/env python3
"""Manage a target Android phone for flashing, rooting, and Frida setup."""

from __future__ import annotations

# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == '__main__':
    import sys as _awb_sys
    from pathlib import Path as _AwbPath
    _awb_project = None
    for _awb_origin in (_AwbPath.cwd(),):
        for _awb_root in (_awb_origin, *_awb_origin.parents):
            if (_awb_root / 'workbench.project.json').is_file():
                _awb_project = _awb_root
                break
        if _awb_project is not None:
            break
    if _awb_project is not None:
        import json as _awb_json
        _awb_config = _awb_json.loads((_awb_project / 'workbench.project.json').read_text())
        _awb_runtime = (_awb_project / _awb_config.get('workbench_root', 'android-workbench')).resolve()
        if not (_awb_runtime / 'workbench/bridge.py').is_file():
            raise SystemExit('Android Workbench runtime missing; managed entry cannot run directly')
        _awb_sys.path.insert(0, str(_awb_runtime))
        from workbench.bridge import entry as _awb_entry
        _awb_entry(__file__)
# END ANDROID WORKBENCH ENTRY

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from flash_support import (
    prepare_backup, verify_backup, validate_backup_reference, ota_metadata,
    validate_health_record, remote_backup_path, tree_files,
    validate_installation_steps, require_prepared_record,
    validate_device_review, reviewed_partition_writes, review_binding, image_write_size,
)
import tempfile
import time
from urllib.parse import urlparse
import urllib.request
import zipfile


DEFAULT_ADB = "adb"
DEFAULT_FASTBOOT = "fastboot"
DEFAULT_REMOTE_FRIDA_PATH = "/data/local/tmp/frida-server"
DEFAULT_PARTITION_INSPECT_PARTITIONS = (
    "system_a",
    "system_b",
    "product_a",
    "product_b",
    "system_ext_a",
    "system_ext_b",
    "odm_a",
    "odm_b",
    "vendor_a",
    "vendor_b",
)
FACTORY_PARTITION_DELETE_ALLOWLIST = {
    "product_a",
    "product_b",
    "odm_a",
    "odm_b",
    "system_ext_a",
    "system_ext_b",
}
FACTORY_DYNAMIC_PARTITION_REPAIR_ORDER = ("product", "odm", "system_ext")
OUTPUT_LIMIT = 4000
FACTORY_OUTPUT_LIMIT = 1024 * 1024
FLASH_PROGRESS = None
OPERATOR_PRESENCE_COMMANDS = {
    "bootloader-state",
    "partition-repair",
    "flash",
    "flash-factory",
    "install-rom",
    "install-ota",
    "root",
    "root-prepare",
}
OPERATOR_NOTICE = (
    "Someone must stay beside the phone during this operation. Depending on the "
    "device and stage, manual button presses, recovery menu selections, setup, "
    "USB debugging authorization, or Magisk installation actions may be required. "
    "Follow the device-specific instructions at each manual step and verify the "
    "required state before continuing."
)
PARTITION_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
REMOTE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_./-]+$")
RESEARCH_REQUIRED_FIELDS = (
    "model",
    "device",
    "build",
    "bootloader",
    "method",
    "decisions",
    "sources",
    "pitfalls",
    "rollback",
)
DECISION_FIELDS = {
    "scope": {"boot_only", "system_only", "full_image", "custom", "ota"},
    "root": {"yes", "no"},
    "data": {"keep", "wipe"},
    "reboot": {"yes", "no"},
    "frida": {"yes", "no"},
}
BOOT_ONLY_PARTITIONS = {"boot", "boot_a", "boot_b"}
SYSTEM_ONLY_PARTITIONS = {
    "system",
    "system_a",
    "system_b",
    "vendor",
    "vendor_a",
    "vendor_b",
    "product",
    "product_a",
    "product_b",
}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value: str | bytes, *, limit: int = OUTPUT_LIMIT, preserve_tail: bool = False) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if len(value) <= limit:
        return value
    if preserve_tail:
        head = limit // 2
        tail = limit - head
        return value[:head] + f"\n[truncated: {len(value) - limit} characters omitted]\n" + value[-tail:]
    return value[:limit] + "\n[truncated]"


def command_output(record: dict) -> str:
    lines: list[str] = []
    for part in (record.get("stdout", ""), record.get("stderr", "")):
        lines.extend(part.splitlines())
    return "\n".join(lines).strip()


def run_command(
    command: list[str],
    *,
    label: str,
    timeout: float,
    dry_run: bool = False,
    check: bool = True,
    records: list[dict] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    output_limit: int = OUTPUT_LIMIT,
    preserve_output_tail: bool = False,
) -> dict:
    record = {
        "name": label,
        "command": command,
        "dry_run": dry_run,
    }
    if dry_run:
        record.update({"returncode": None, "stdout": "", "stderr": ""})
        if records is not None:
            records.append(record)
        return record
    if FLASH_PROGRESS is not None:
        FLASH_PROGRESS({**record, "state": "running"})
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        record.update(
            {
                "returncode": 124,
                "stdout": bounded_text(error.stdout or "", limit=output_limit, preserve_tail=preserve_output_tail),
                "stderr": bounded_text(error.stderr or "", limit=output_limit, preserve_tail=preserve_output_tail),
                "error": f"{label} timed out after {timeout} seconds",
            }
        )
        if records is not None:
            records.append(record)
        if FLASH_PROGRESS is not None:
            FLASH_PROGRESS({**record, "state": "failed"})
        raise RuntimeError(record["error"]) from error
    except OSError as error:
        record.update(
            {
                "returncode": 127,
                "stdout": "",
                "stderr": str(error),
                "error": f"{label} could not start: {error}",
            }
        )
        if records is not None:
            records.append(record)
        if FLASH_PROGRESS is not None:
            FLASH_PROGRESS({**record, "state": "failed"})
        raise RuntimeError(record["error"]) from error
    record.update(
        {
            "returncode": completed.returncode,
            "stdout": bounded_text(completed.stdout, limit=output_limit, preserve_tail=preserve_output_tail),
            "stderr": bounded_text(completed.stderr, limit=output_limit, preserve_tail=preserve_output_tail),
        }
    )
    if records is not None:
        records.append(record)
    if FLASH_PROGRESS is not None:
        FLASH_PROGRESS({**record, "state": "finished"})
    if check and completed.returncode != 0:
        failure_output = completed.stderr.strip() or completed.stdout.strip()
        if preserve_output_tail:
            failure_output = bounded_text(failure_output, limit=output_limit, preserve_tail=True)
        raise RuntimeError(
            f"{label} failed with exit code {completed.returncode}: "
            f"{failure_output}"
        )
    return record


def conditional_dry_run_step(
    label: str,
    condition: str,
    records: list[dict],
) -> None:
    records.append(
        {
            "name": label,
            "command": [],
            "dry_run": True,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "conditional": True,
            "condition": condition,
        }
    )


def adb_command(adb_path: str, serial: str, *arguments: str) -> list[str]:
    return [adb_path, "-s", serial, *arguments]


def fastboot_command(fastboot_path: str, serial: str, *arguments: str) -> list[str]:
    return [fastboot_path, "-s", serial, *arguments]


def fastboot_transport(output: str, serial: str) -> str | None:
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == serial:
            return fields[-1]
    return None


def device_shell_command(command: str, *, root: bool = False) -> str:
    if root:
        return f"su -c {shlex.quote(command)}"
    return command


def detect_mode(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    *,
    timeout: float,
    records: list[dict],
) -> str:
    adb_state = run_command(
        adb_command(adb_path, serial, "get-state"),
        label="adb get-state",
        timeout=min(timeout, 10),
        check=False,
        records=records,
    )
    if adb_state.get("stdout", "").strip() == "device":
        return "android"
    fastboot_devices = run_command(
        [fastboot_path, "devices"],
        label="fastboot devices",
        timeout=min(timeout, 10),
        check=False,
        records=records,
    )
    transport = fastboot_transport(
        fastboot_devices.get("stdout", ""), fastboot_serial
    )
    if transport in {"fastboot", "fastbootd"}:
        return read_fastboot_mode(fastboot_path, fastboot_serial, timeout=timeout, records=records)
    if "unauthorized" in command_output(adb_state).lower():
        return "unauthorized"
    return "unknown"



def read_fastboot_mode(fastboot_path, serial, *, timeout, records):
    record = run_command(fastboot_command(fastboot_path, serial, "getvar", "is-userspace"),
                         label="read fastboot mode", timeout=min(timeout, 10), check=False, records=records)
    if record.get("returncode") != 0:
        return "unknown"
    return {"yes": "fastbootd", "no": "fastboot"}.get(fastboot_getvar_value(record, "is-userspace"), "unknown")

def wait_for_fastboot(
    fastboot_path: str,
    fastboot_serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
    transport: str = "fastboot",
) -> None:
    if dry_run:
        run_command(
            [fastboot_path, "devices"],
            label="wait for fastboot",
            timeout=timeout,
            dry_run=True,
            records=records,
        )
        return
    deadline = time.monotonic() + timeout
    last_output = ""
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [fastboot_path, "devices"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=min(10, max(1, deadline - time.monotonic())),
            check=False,
        )
        last_output = completed.stdout
        if (fastboot_transport(completed.stdout, fastboot_serial) in {"fastboot", "fastbootd"}
                and read_fastboot_mode(fastboot_path, fastboot_serial,
                                      timeout=max(1, deadline - time.monotonic()), records=records) == transport):
            records.append(
                {
                    "name": "wait for fastboot",
                    "command": [fastboot_path, "devices"],
                    "dry_run": False,
                    "returncode": completed.returncode,
                    "stdout": bounded_text(completed.stdout),
                    "stderr": bounded_text(completed.stderr),
                }
            )
            return
        time.sleep(1)
    raise RuntimeError(
        f"fastboot device {fastboot_serial} did not appear as {transport} within {timeout} seconds; last output: {last_output.strip()}"
    )


def wait_for_boot(
    adb_path: str,
    serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    if dry_run:
        run_command(
            adb_command(adb_path, serial, "shell", "getprop sys.boot_completed"),
            label="wait for boot",
            timeout=timeout,
            dry_run=True,
            records=records,
        )
        return
    deadline = time.monotonic() + timeout
    last_output = ""
    while time.monotonic() < deadline:
        completed = subprocess.run(
            adb_command(adb_path, serial, "shell", "getprop sys.boot_completed"),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=min(10, max(1, deadline - time.monotonic())),
            check=False,
        )
        last_output = completed.stdout
        if completed.stdout.strip() == "1":
            records.append(
                {
                    "name": "wait for boot",
                    "command": adb_command(
                        adb_path, serial, "shell", "getprop sys.boot_completed"
                    ),
                    "dry_run": False,
                    "returncode": completed.returncode,
                    "stdout": bounded_text(completed.stdout),
                    "stderr": bounded_text(completed.stderr),
                }
            )
            return
        time.sleep(1)
    raise RuntimeError(
        f"device {serial} did not finish booting within {timeout} seconds; sys.boot_completed={last_output.strip()!r}"
    )


def ensure_fastboot(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    if dry_run:
        conditional_dry_run_step(
            "conditionally reboot to bootloader",
            "Detect the current mode; reboot only if the phone is not already in fastboot or fastbootd.",
            records,
        )
        return
    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if mode == "fastboot":
        return
    if mode == "fastbootd":
        return
    if mode != "android":
        raise RuntimeError(
            f"device {serial} is neither online in Android nor visible in fastboot"
        )
    run_command(
        adb_command(adb_path, serial, "reboot-bootloader"),
        label="reboot to bootloader",
        timeout=min(timeout, 30),
        dry_run=dry_run,
        records=records,
    )
    wait_for_fastboot(
        fastboot_path,
        fastboot_serial,
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )


def ensure_fastbootd(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    if dry_run:
        conditional_dry_run_step(
            "conditionally reboot to fastbootd",
            "Detect the current mode; reboot only if the phone is not already in fastbootd, then wait only after a reboot.",
            records,
        )
        return
    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if mode == "fastbootd":
        return
    if mode == "android":
        run_command(
            adb_command(adb_path, serial, "reboot", "fastboot"),
            label="reboot to fastbootd",
            timeout=min(timeout, 30),
            records=records,
        )
    elif mode in {"fastboot", "fastbootd"}:
        run_command(
            fastboot_command(fastboot_path, fastboot_serial, "reboot", "fastboot"),
            label="reboot to fastbootd",
            timeout=min(timeout, 30),
            records=records,
        )
    else:
        raise RuntimeError(
            f"device {serial} is not online in Android, fastboot, or fastbootd"
        )
    wait_for_fastboot(
        fastboot_path,
        fastboot_serial,
        timeout=timeout,
        dry_run=False,
        records=records,
        transport="fastbootd",
    )


def ensure_bootloader(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    if dry_run:
        conditional_dry_run_step(
            "conditionally reboot to bootloader",
            "Detect the current mode; reboot only if the phone is not already in the bootloader, then wait only after a reboot.",
            records,
        )
        return
    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if mode == "fastboot":
        return
    if mode == "android":
        run_command(
            adb_command(adb_path, serial, "reboot-bootloader"),
            label="reboot to bootloader",
            timeout=min(timeout, 30),
            records=records,
        )
    elif mode == "fastbootd":
        run_command(
            fastboot_command(fastboot_path, fastboot_serial, "reboot", "bootloader"),
            label="reboot to bootloader",
            timeout=min(timeout, 30),
            records=records,
        )
    else:
        raise RuntimeError(
            f"device {serial} is not online in Android, fastboot, or fastbootd"
        )
    wait_for_fastboot(
        fastboot_path,
        fastboot_serial,
        timeout=timeout,
        dry_run=False,
        records=records,
    )


def ensure_android(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    if dry_run:
        conditional_dry_run_step(
            "conditionally reboot to Android",
            "Detect the current mode; reboot only if the phone is not already online in Android, then wait for boot only after a reboot.",
            records,
        )
        return
    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if mode == "android":
        return
    if mode != "fastboot":
        raise RuntimeError(
            f"device {serial} is neither online in Android nor visible in fastboot"
        )
    run_command(
        fastboot_command(fastboot_path, fastboot_serial, "reboot"),
        label="reboot to Android",
        timeout=min(timeout, 30),
        dry_run=dry_run,
        records=records,
    )
    wait_for_boot(
        adb_path,
        serial,
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )


def current_slot(
    fastboot_path: str,
    fastboot_serial: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> str | None:
    result = run_command(
        fastboot_command(fastboot_path, fastboot_serial, "getvar", "current-slot"),
        label="read current slot",
        timeout=min(timeout, 15),
        dry_run=dry_run,
        check=False,
        records=records,
    )
    if dry_run:
        return None
    match = re.search(r"current-slot:\s*([ab])", command_output(result))
    return match.group(1) if match else None


def validate_partition(partition: str) -> None:
    if not PARTITION_PATTERN.fullmatch(partition):
        raise ValueError(f"invalid partition name: {partition!r}")


def validate_remote_path(remote_path: str) -> None:
    if not REMOTE_PATH_PATTERN.fullmatch(remote_path):
        raise ValueError(f"invalid remote path: {remote_path!r}")


def parse_images(values: list[str]) -> list[tuple[str, Path]]:
    images: list[tuple[str, Path]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"image must be partition=path, got {value!r}")
        partition, image = value.split("=", 1)
        validate_partition(partition)
        image_path = Path(image).expanduser().resolve()
        if not image_path.is_file():
            raise ValueError(f"image file does not exist: {image_path}")
        images.append((partition, image_path))
    if not images:
        raise ValueError("at least one --image partition=path is required")
    return images


def validate_local_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist: {resolved}")
    return resolved


def load_research(path: Path) -> dict:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"research dossier does not exist: {resolved}")
    try:
        document = json.loads(resolved.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"research dossier cannot be read: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("research dossier must be a JSON object")
    return document


def validate_research(document: dict) -> dict:
    missing = [field for field in RESEARCH_REQUIRED_FIELDS if not document.get(field)]
    if missing:
        raise ValueError(
            "research dossier is missing required fields: " + ", ".join(missing)
        )
    if document["bootloader"] not in {"unlocked", "locked"}:
        raise ValueError("research dossier bootloader must be unlocked or locked")
    if not isinstance(document["sources"], list) or not document["sources"]:
        raise ValueError("research dossier must contain at least one source")
    if not all(isinstance(source, dict) for source in document["sources"]):
        raise ValueError("research dossier sources must be objects")
    for source in document["sources"]:
        if not source.get("title") or not source.get("url"):
            raise ValueError("each research source needs title and url")
    if not isinstance(document["pitfalls"], list) or not document["pitfalls"]:
        raise ValueError("research dossier must contain at least one pitfall")
    if not isinstance(document["rollback"], str):
        raise ValueError("research dossier rollback must be a string")
    decisions = document["decisions"]
    if not isinstance(decisions, dict):
        raise ValueError("research dossier decisions must be an object")
    missing_decisions = [
        field for field in DECISION_FIELDS if field not in decisions
    ]
    if missing_decisions:
        raise ValueError(
            "research dossier decisions is missing: "
            + ", ".join(missing_decisions)
        )
    for field, allowed in DECISION_FIELDS.items():
        if decisions[field] not in allowed:
            raise ValueError(
                f"research dossier decision {field} must be one of: "
                + ", ".join(sorted(allowed))
            )
    return {
        "schema": 1,
        "model": document["model"],
        "device": document["device"],
        "build": document["build"],
        "bootloader": document["bootloader"],
        "method": document["method"],
        **({"flash_plan": document["flash_plan"]} if "flash_plan" in document else {}),
        **({"root_partition": document["root_partition"]} if "root_partition" in document else {}),
        "decisions": decisions,
        "sources": document["sources"],
        "pitfalls": document["pitfalls"],
        "rollback": document["rollback"],
        "status": "complete",
    }


def validate_flash_decisions(
    research: dict,
    images: list[tuple[str, Path]],
    *,
    no_reboot: bool,
) -> None:
    if research["bootloader"] != "unlocked":
        raise ValueError("partition flashing requires an unlocked bootloader")
    decisions = research["decisions"]
    partitions = [partition for partition, _ in images]
    if len(set(partitions)) != len(partitions):
        raise ValueError("duplicate flash partitions are not allowed")
    if decisions["data"] == "keep" and any(
        partition.removesuffix("_a").removesuffix("_b") in {"userdata", "metadata"}
        for partition in partitions
    ):
        raise ValueError("keep data forbids writing userdata or metadata")
    if decisions["data"] == "wipe":
        raise ValueError("generic flash does not perform a data wipe; use a documented factory or ROM installation")
    if decisions["reboot"] == "no" and not no_reboot:
        raise ValueError("research dossier chose no reboot; pass --no-reboot")
    if decisions["reboot"] == "yes" and no_reboot:
        raise ValueError("research dossier chose reboot; do not pass --no-reboot")
    scope = decisions["scope"]
    if scope == "boot_only":
        invalid = [
            partition
            for partition, _ in images
            if partition not in BOOT_ONLY_PARTITIONS
        ]
        if invalid:
            raise ValueError(
                "boot_only scope only allows: "
                + ", ".join(sorted(BOOT_ONLY_PARTITIONS))
                + "; got "
                + ", ".join(invalid)
            )
    if scope == "system_only":
        invalid = [
            partition
            for partition, _ in images
            if partition not in SYSTEM_ONLY_PARTITIONS
        ]
        if invalid:
            raise ValueError(
                "system_only scope only allows: "
                + ", ".join(sorted(SYSTEM_ONLY_PARTITIONS))
                + "; got "
                + ", ".join(invalid)
            )


def validate_factory_flash_decisions(research: dict, *, no_reboot: bool) -> None:
    if research["bootloader"] != "unlocked":
        raise ValueError("factory image installation requires an unlocked bootloader")
    decisions = research["decisions"]
    if decisions["scope"] != "full_image":
        raise ValueError("factory image installation requires the full_image scope")
    if decisions["data"] != "wipe":
        raise ValueError("factory image installation requires the wipe data decision")
    if decisions["reboot"] == "no" and not no_reboot:
        raise ValueError("research dossier chose no reboot; pass --no-reboot")
    if decisions["reboot"] == "yes" and no_reboot:
        raise ValueError("research dossier chose reboot; do not pass --no-reboot")


def validate_root_decisions(research: dict, *, no_reboot: bool) -> None:
    decisions = research["decisions"]
    validate_root_intent(research)
    if research["bootloader"] != "unlocked":
        raise ValueError("root requires an unlocked bootloader")
    if decisions["reboot"] == "no" and not no_reboot:
        raise ValueError("research dossier chose no reboot; pass --no-reboot")
    if decisions["reboot"] == "yes" and no_reboot:
        raise ValueError("research dossier chose reboot; do not pass --no-reboot")


def validate_root_intent(research: dict) -> None:
    if research["decisions"]["root"] != "yes":
        raise ValueError("research dossier chose not to root this device")
    if research.get("root_partition") != "boot":
        raise ValueError(
            "root preparation, collection and flashing support only boot; "
            "research must explicitly record root_partition: boot. "
            "init_boot and recovery root flows are not supported"
        )


def validate_frida_decisions(research: dict) -> None:
    decisions = research["decisions"]
    if decisions["root"] != "yes":
        raise ValueError("Frida server installation requires the root decision")
    if decisions["frida"] != "yes":
        raise ValueError("research dossier chose not to install Frida")


def validate_install_rom_decisions(research: dict, *, no_reboot: bool) -> None:
    if research["bootloader"] != "unlocked":
        raise ValueError("custom ROM installation requires an unlocked bootloader")
    decisions = research["decisions"]
    if decisions["scope"] != "custom":
        raise ValueError("custom ROM installation requires the custom scope")
    if decisions["data"] != "wipe":
        raise ValueError("custom ROM installation requires the wipe data decision")
    if decisions["reboot"] == "no" and not no_reboot:
        raise ValueError("research dossier chose no reboot; pass --no-reboot")
    if decisions["reboot"] == "yes" and no_reboot:
        raise ValueError("research dossier chose reboot; do not pass --no-reboot")


def research_cache_key(document: dict) -> str:
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_research_cache(path: Path) -> dict:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return {"version": 1, "entries": {}}
    try:
        cache = json.loads(resolved.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"research cache cannot be read: {error}") from error
    if (
        not isinstance(cache, dict)
        or cache.get("version") != 1
        or not isinstance(cache.get("entries"), dict)
    ):
        raise ValueError("research cache must be version 1 with an entries object")
    return cache


def save_research_cache(path: Path, cache: dict) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def image_cache_path(cache_dir: Path, expected_sha256: str, url: str) -> Path:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".img", ".zip", ".apk"}:
        suffix = ".img"
    return cache_dir / f"{expected_sha256}{suffix}"


def remote_content_length(url: str, *, timeout: float) -> int:
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "android-workbench"},
    )
    with urllib.request.urlopen(request, timeout=min(timeout, 30)) as response:
        if response.status != 200:
            raise RuntimeError(f"image server returned HTTP {response.status}")
        value = response.headers.get("Content-Length")
        if value is None or not value.isdigit():
            raise RuntimeError("image server did not return a valid Content-Length")
        return int(value)


def chunk_ranges(size: int, chunks: int) -> list[tuple[int, int]]:
    if size <= 0 or chunks <= 0:
        raise ValueError("image size and chunk count must be positive")
    ranges = []
    start = 0
    for index in range(chunks):
        end = size - 1 if index == chunks - 1 else start + size // chunks - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def curl_download_range(
    curl_path: str,
    url: str,
    start: int,
    end: int,
    output: Path,
    *,
    timeout: float,
) -> None:
    command = [
        curl_path,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--range",
        f"{start}-{end}",
        "--retry",
        "20",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "30",
        "--speed-limit",
        "1024",
        "--speed-time",
        "60",
        "--output",
        str(output),
        url,
    ]
    try:
        subprocess.run(command, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"image range {start}-{end} timed out after {timeout} seconds"
        ) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"image range {start}-{end} failed with exit code {error.returncode}"
        ) from error


def download_image_chunks(
    curl_path: str,
    url: str,
    partial: Path,
    *,
    timeout: float,
    chunks: int = 8,
) -> list[Path]:
    size = remote_content_length(url, timeout=timeout)
    ranges = chunk_ranges(size, chunks)
    chunk_paths = [
        partial.with_name(partial.name + f".{index:03d}")
        for index in range(chunks)
    ]

    def download_one(index: int) -> Path:
        start, end = ranges[index]
        output = chunk_paths[index]
        expected = end - start + 1
        if output.is_file() and output.stat().st_size == expected:
            return output
        if output.is_file() and output.stat().st_size > expected:
            output.unlink()
        resume_path = output.with_name(output.name + ".resume")
        resume_path.unlink(missing_ok=True)
        resume_start = start + (output.stat().st_size if output.is_file() else 0)
        curl_download_range(
            curl_path,
            url,
            resume_start,
            end,
            resume_path,
            timeout=timeout,
        )
        if not resume_path.is_file():
            raise RuntimeError(f"image range {start}-{end} produced no output")
        with output.open("ab" if output.is_file() else "wb") as target, resume_path.open("rb") as source:
            while block := source.read(1024 * 1024):
                target.write(block)
        resume_path.unlink(missing_ok=True)
        actual = output.stat().st_size
        if actual != expected:
            raise RuntimeError(
                f"image range {start}-{end} has size {actual}, expected {expected}"
            )
        return output

    with ThreadPoolExecutor(max_workers=chunks) as executor:
        futures = [executor.submit(download_one, index) for index in range(chunks)]
        for future in as_completed(futures):
            future.result()
    return chunk_paths


def download_image(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    timeout: float,
    chunks: int = 8,
) -> dict:
    if not 1 <= chunks <= 64:
        raise ValueError("image download chunks must be between 1 and 64")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    chunk_paths = [
        partial.with_name(partial.name + f".{index:03d}") for index in range(8)
    ]
    resumed = partial.is_file() or any(path.is_file() for path in chunk_paths)
    curl_path = shutil.which("curl")
    if curl_path is not None:
        if url.startswith(("http://", "https://")) and (
            any(path.is_file() for path in chunk_paths) or not partial.is_file()
        ):
            downloaded_chunks = download_image_chunks(
                curl_path,
                url,
                partial,
                timeout=timeout,
                chunks=chunks,
            )
            with partial.open("wb") as target:
                for chunk in downloaded_chunks:
                    with chunk.open("rb") as source:
                        while block := source.read(1024 * 1024):
                            target.write(block)
        else:
            command = [
                curl_path,
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--continue-at",
                "-",
                "--retry",
                "20",
                "--retry-all-errors",
                "--retry-delay",
                "2",
                "--connect-timeout",
                "30",
                "--speed-limit",
                "1024",
                "--speed-time",
                "60",
                "--output",
                str(partial),
                url,
            ]
            try:
                subprocess.run(command, check=True, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    f"image download timed out after {timeout} seconds; partial download retained"
                ) from error
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    f"image download failed with exit code {error.returncode}; partial download retained"
                ) from error
    else:
        request = urllib.request.Request(
            url, headers={"User-Agent": "android-workbench"}
        )
        with urllib.request.urlopen(
            request, timeout=timeout
        ) as response, partial.open("wb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
    size = partial.stat().st_size
    digest = hashlib.sha256()
    with partial.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        partial.unlink(missing_ok=True)
        for path in chunk_paths:
            path.unlink(missing_ok=True)
        raise ValueError(
            f"downloaded image sha256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    partial.replace(destination)
    for path in chunk_paths:
        path.unlink(missing_ok=True)
    return {
        "url": url,
        "size": size,
        "sha256": actual_sha256,
        "cached": False,
        "resumed": resumed,
    }


def validate_flash_plan(research, images, *, serial, fastboot_serial, dry_run, ota=False):
    """Bind reviewed device facts and image digests before any mode transition."""
    plan = research.get("flash_plan")
    if plan is None and dry_run:
        return {"offline_checks": "incomplete", "missing": ["flash_plan"],
                "device_checks": "pending", "execution_ready": False}
    if not isinstance(plan, dict):
        raise ValueError("real flashing requires a reviewed flash_plan; run an offline dry-run first")
    fields = ("serial", "bootloader_version", "target_fingerprint")
    if not ota:
        fields += ("fastboot_serial",)
    for field in fields:
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError("flash_plan requires " + field)
    if plan["serial"] != serial or (not ota and plan["fastboot_serial"] != fastboot_serial):
        raise ValueError("flash_plan serial does not match the selected phone")
    if plan.get("slot") not in {"a", "b", "none"}:
        raise ValueError("flash_plan slot must be a, b, or none")
    if plan.get("target_slot") not in {"a", "b", "none"}:
        raise ValueError("flash_plan target_slot must be a, b, or none")
    if (plan["slot"] == "none") != (plan["target_slot"] == "none"):
        raise ValueError("source and target slot layouts disagree")
    if plan["slot"] == "none" and plan.get("activate_slot") is not None:
        raise ValueError("non-A/B devices cannot activate a slot")
    if plan.get("mode") not in ({"recovery"} if ota else {"fastboot", "fastbootd"}):
        raise ValueError("flash_plan mode does not match the installation transport")
    acceptance = plan.get("acceptance_checks")
    if not dry_run and (not isinstance(acceptance, list) or not acceptance
            or not all(isinstance(name, str) and name.strip() for name in acceptance)):
        raise ValueError("real installation requires device-specific acceptance_checks")
    anti_rollback = plan.get("anti_rollback")
    if (not isinstance(anti_rollback, dict)
            or anti_rollback.get("compatible") is not True
            or not isinstance(anti_rollback.get("source"), str)
            or not anti_rollback["source"].startswith("https://")):
        raise ValueError("flash_plan requires a sourced anti_rollback compatibility review")
    expected = plan.get("images")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("flash_plan requires image SHA-256 values by partition or role")
    checked = {}
    for role, path in images:
        digest = expected.get(role)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("flash_plan is missing a valid SHA-256 for " + role)
        actual = sha256_file(path)
        if actual != digest.lower():
            raise ValueError("flash image SHA-256 mismatch for " + role)
        checked[role] = {"path": str(path), "sha256": actual}
    backup = validate_backup_reference(research, dry_run=dry_run)
    if not dry_run:
        backup.update(validate_device_review(research))
    return {"offline_checks": "passed", "images": checked, **backup,
            "device_checks": "pending", "execution_ready": False}


def check_flash_device(arguments, research, *, fastboot_serial, records):
    """Re-read the selected bootloader immediately before writing."""
    plan = research["flash_plan"]
    ensure = ensure_fastbootd if plan["mode"] == "fastbootd" else ensure_bootloader
    ensure(arguments.adb, arguments.fastboot, arguments.serial, fastboot_serial,
           timeout=arguments.timeout, dry_run=False, records=records)
    expected = {"is-userspace": "yes" if plan["mode"] == "fastbootd" else "no",
                "product": research["device"], "unlocked": "yes",
                "version-bootloader": plan["bootloader_version"]}
    expected["current-slot" if plan["slot"] != "none" else "has-slot:boot"] = (
        plan["slot"] if plan["slot"] != "none" else "no")
    if plan["slot"] != "none":
        expected["slot-count"] = "2"
    hardware = plan.get("hardware_checks", {}).get("fastboot", {})
    if any(key in expected and expected[key] != value for key, value in hardware.items()):
        raise ValueError("hardware_checks conflicts with required device facts")
    expected.update(hardware)
    for key, value in expected.items():
        record = run_command(
            fastboot_command(arguments.fastboot, fastboot_serial, "getvar", key),
            label="pre-write " + key, timeout=min(arguments.timeout, 30), records=records)
        actual = fastboot_getvar_value(record, key)
        if key == "unlocked" and actual == "true":
            actual = "yes"
        if actual != value:
            raise ValueError(f"pre-write {key} mismatch: expected {value!r}, got {actual!r}")



def reviewed_write_command(fastboot, serial, rule, path):
    options = [] if rule["slot"] == "none" else ["--slot", rule["slot"]]
    return fastboot_command(fastboot, serial, *options, "flash", rule["partition"], str(path))


def check_partition_slot(arguments, serial, rule, records, *, image):
    partition = rule["partition"]
    base = partition[:-2] if partition.endswith(("_a", "_b")) else partition
    key = "has-slot:" + base
    record = run_command(fastboot_command(arguments.fastboot, serial, "getvar", key),
                         label="pre-write partition slots", timeout=min(arguments.timeout, 30), records=records)
    expected = "yes" if rule["slot"] != "none" or base != partition else "no"
    if fastboot_getvar_value(record, key) != expected:
        raise ValueError("partition slot layout differs from the device-specific write rule")
    targets = ([partition + "_a", partition + "_b"] if rule["slot"] == "all" else
               [partition + "_" + rule["slot"]] if rule["slot"] in {"a", "b"} else [partition])
    required = image_write_size(image)
    for target in targets:
        variable = "partition-size:" + target
        record = run_command(fastboot_command(arguments.fastboot, serial, "getvar", variable),
                             label="pre-write partition capacity", timeout=min(arguments.timeout, 30), records=records)
        capacity = parse_partition_size(fastboot_getvar_value(record, variable))
        if capacity < required:
            raise ValueError(f"image requires {required} bytes but {target} has {capacity}; "
                             "partition growth requires a separately supported, researched flow")


def parse_partition_size(value):
    # fastboot partition-size is hexadecimal, including responses without 0x.
    if not isinstance(value, str) or not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", value):
        raise ValueError("partition size is unavailable or invalid")
    size = int(value, 16)
    if size <= 0:
        raise ValueError("partition does not exist or has no usable capacity")
    return size

def record_flash_stage(result, research, phase):
    result.update(flash_plan=research.get("flash_plan"), flash_phase=phase,
                  workflow_complete=False)


def verify_flashed_system(arguments, records):
    previous = load_research(arguments.flash_record)
    if (previous.get("device") != arguments.serial or previous.get("status") != "complete"
            or previous.get("command") not in {"flash", "flash-factory", "install-rom", "install-ota"}
            or previous.get("flash_phase") != "awaiting_verification"):
        raise ValueError("flash verification requires a completed write record for this phone")
    plan = previous.get("flash_plan")
    research = previous.get("research", {})
    if not isinstance(plan, dict) or not plan.get("target_fingerprint") or not research.get("device"):
        raise ValueError("flash record does not contain a verifiable target")
    if plan.get("target_slot") not in {"a", "b", "none"}:
        raise ValueError("flash record does not identify the target boot slot")
    health = validate_health_record(getattr(arguments, "health_record", None), plan, arguments.serial,
                                    flash_record_sha256=sha256_file(arguments.flash_record))
    wait_for_boot(arguments.adb, arguments.serial, timeout=arguments.timeout,
                  dry_run=False, records=records)
    observed = {}
    for key, expected in {"ro.product.device": research["device"],
                          "ro.build.fingerprint": plan["target_fingerprint"],
                          "ro.boot.slot_suffix": "" if plan["target_slot"] == "none"
                          else "_" + plan["target_slot"]}.items():
        record = run_command(adb_command(arguments.adb, arguments.serial, "shell", "getprop", key),
                             label="verify " + key, timeout=min(arguments.timeout, 30), records=records)
        observed[key] = record["stdout"].strip()
        if observed[key] != expected:
            raise ValueError("flashed system does not match the expected " + key)
    accepted = health["health_checks"] == "passed"
    return {"flash_phase": "verified" if accepted else "awaiting_acceptance", "workflow_complete": accepted,
            "observed": observed, "flash_record": str(arguments.flash_record.resolve()),
            "flash_record_sha256": sha256_file(arguments.flash_record), **health}


def battery_observations(output):
    """Keep health separate from charge level and temperature; missing means unknown."""
    observed = {}
    for field, name in (("level", "battery_level"), ("health", "battery_health"),
                        ("status", "battery_status"), ("temperature", "battery_temperature_tenths_c")):
        match = re.search(r"^\s*" + field + r":\s*(-?\d+)\s*$", output, re.MULTILINE)
        if match:
            observed[name] = int(match.group(1))
    if "Current Battery Service state:" in output:
        observed["battery_updates_stopped"] = "UPDATES STOPPED" in output
    return observed


def check_android_source(arguments, research, records, *, require_baseline=False):
    """Read the selected Android device without rebooting or changing settings."""
    plan = research["flash_plan"]
    if not isinstance(plan, dict) or plan.get("serial") != arguments.serial:
        raise ValueError("source check requires a flash_plan bound to the selected phone")
    if plan.get("slot") not in {"a", "b", "none"}:
        raise ValueError("source check requires the observed slot")
    source = plan.get("source_fingerprint")
    minimum = plan.get("minimum_battery")
    if require_baseline and (not isinstance(source, str) or not source.strip()):
        raise ValueError("installation requires the reviewed source_fingerprint (firmware baseline)")
    if require_baseline and (type(minimum) is not int or not 1 <= minimum <= 100):
        raise ValueError("installation requires a guide-based minimum_battery from 1 to 100")
    if minimum is not None and (type(minimum) is not int or not 1 <= minimum <= 100):
        raise ValueError("minimum_battery must be an integer from 1 to 100")
    observed = {}
    expected_values = {"ro.product.device": research["device"],
                          "ro.build.fingerprint": source,
                          "ro.bootloader": plan.get("bootloader_version"),
                          "ro.boot.slot_suffix": "" if plan["slot"] == "none" else "_" + plan["slot"]}
    hardware = plan.get("hardware_checks", {}).get("android", {})
    if any(key in expected_values and expected_values[key] != value for key, value in hardware.items()):
        raise ValueError("hardware_checks conflicts with required Android facts")
    expected_values.update(hardware)
    for key, expected in expected_values.items():
        if expected is None:
            continue
        record = run_command(adb_command(arguments.adb, arguments.serial, "shell", "getprop", key),
                             label="pre-install " + key, timeout=min(arguments.timeout, 30), records=records)
        actual = record["stdout"].strip()
        if actual != expected:
            raise ValueError("source firmware/device mismatch for " + key)
        observed[key] = actual
    if minimum is not None:
        record = run_command(adb_command(arguments.adb, arguments.serial, "shell", "dumpsys", "battery"),
                             label="pre-install battery", timeout=min(arguments.timeout, 30), records=records)
        level = re.search(r"^\s*level:\s*(\d+)\s*$", record["stdout"], re.MULTILINE)
        if not level or not minimum <= int(level.group(1)) <= 100:
            raise ValueError("battery is unknown or below the reviewed minimum_battery")
        battery = battery_observations(record["stdout"])
        if battery.get("battery_health") != 2:
            raise ValueError("battery health is unknown or abnormal; resolve before installation")
        if battery.get("battery_updates_stopped") is not False:
            raise ValueError("battery live updates are stopped or unconfirmed; resolve before installation")
        observed.update(battery)
        observed["battery_percent"] = int(level.group(1))
    return observed


def install_ota(arguments, result, records):
    if not arguments.confirm and not arguments.dry_run:
        raise ValueError("install-ota requires --confirm")
    research = load_research(arguments.research)
    validate_research(research)
    if research["decisions"]["scope"] != "ota" or research["decisions"]["data"] != "keep":
        raise ValueError("full OTA requires scope ota and data keep")
    image = validate_local_file(arguments.ota, "full OTA package")
    result.update(research=research, stage=arguments.stage)
    result["flash_checks"] = validate_flash_plan(
        research, [("ota", image)], serial=arguments.serial,
        fastboot_serial=None, dry_run=arguments.dry_run, ota=True)
    result["ota_metadata"] = ota_metadata(image, research)
    if arguments.stage == "prepare":
        commands = [adb_command(arguments.adb, arguments.serial, "reboot", "recovery")]
        if not arguments.dry_run:
            result["source_checks"] = check_android_source(arguments, research, records, require_baseline=True)
        phase = "awaiting_sideload"
        next_action = "Stay beside the phone. In stock recovery, choose Apply update from ADB, then run install-ota sideload with this prepared record."
    else:
        if not arguments.dry_run:
            result["prepared_record_sha256"] = require_prepared_record(
                arguments.prepared_record, research, arguments.serial,
                command="install-ota", phase="awaiting_sideload")
        commands = [adb_command(arguments.adb, arguments.serial, "sideload", str(image))]
        phase = "awaiting_verification"
        next_action = "Confirm recovery reports installation success, reboot from its menu, authorize USB debugging, then run flash-verify."
        if research["decisions"]["reboot"] == "no":
            next_action = "Confirm recovery reports installation success. Leave the phone in recovery as reviewed; final verification requires a later authorized boot."
        wait_for_adb_state(arguments.adb, arguments.serial, "sideload", timeout=arguments.timeout,
                           dry_run=arguments.dry_run, records=records)
    result["plan"] = commands
    record_flash_stage(result, research, phase)
    if not arguments.dry_run:
        validate_flash_plan(research, [("ota", image)], serial=arguments.serial,
                            fastboot_serial=None, dry_run=False, ota=True)
    execute_plan(commands, timeout=arguments.timeout, dry_run=arguments.dry_run, records=records)
    result.update(next_action=next_action, status="dry_run" if arguments.dry_run else "complete")


def flash_commands(
    fastboot_path: str,
    fastboot_serial: str,
    images: list[tuple[str, Path]],
    *,
    reboot: bool,
) -> list[list[str]]:
    commands = [
        fastboot_command(fastboot_path, fastboot_serial, "flash", partition, str(image))
        for partition, image in images
    ]
    if reboot:
        commands.append(fastboot_command(fastboot_path, fastboot_serial, "reboot"))
    return commands


def install_rom_prepare_commands(
    fastboot_path: str,
    fastboot_serial: str,
    recovery_boot: Path,
    *,
    partition: str = "boot",
) -> list[list[str]]:
    if partition not in {"boot", "vendor_boot", "recovery"}:
        raise ValueError("unsupported recovery image partition")
    return [
        fastboot_command(
            fastboot_path, fastboot_serial, "flash", partition, str(recovery_boot)
        )
    ]


def install_rom_sideload_commands(
    adb_path: str,
    serial: str,
    rom: Path,
    *,
    reboot: bool,
) -> list[list[str]]:
    commands = [adb_command(adb_path, serial, "sideload", str(rom))]
    if reboot:
        commands.append(adb_command(adb_path, serial, "reboot"))
    return commands


def root_prepare_commands(
    adb_path: str,
    serial: str,
    stock_boot: Path,
    magisk_apk: Path,
    *,
    remote_boot: str,
) -> list[list[str]]:
    validate_remote_path(remote_boot)
    return [
        adb_command(adb_path, serial, "install", "-r", str(magisk_apk)),
        adb_command(adb_path, serial, "push", str(stock_boot), remote_boot),
    ]


def root_collect_commands(
    adb_path: str,
    serial: str,
    remote_patched_boot: str,
    destination: Path,
) -> list[list[str]]:
    validate_remote_path(remote_patched_boot)
    if destination.exists():
        raise ValueError(f"patched boot output already exists: {destination}")
    return [adb_command(adb_path, serial, "pull", remote_patched_boot, str(destination))]


def parse_android_info(text: str) -> dict[str, list[str]]:
    requirements: dict[str, list[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("require "):
            continue
        requirement = stripped[len("require ") :].strip()
        if "=" not in requirement:
            continue
        key, values = requirement.split("=", 1)
        requirements[key.strip()] = [value.strip() for value in values.split("|")]
    return requirements


def factory_zip_layout(factory_zip: Path, *, require_super_empty: bool = False) -> dict:
    try:
        with zipfile.ZipFile(factory_zip) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"factory image cannot be read: {error}") from error
    scripts = [name for name in names if Path(name).name == "flash-all.sh"]
    image_archives = [
        name
        for name in names
        if name.endswith(".zip") and Path(name).name.startswith("image-")
    ]
    if len(scripts) != 1 or len(image_archives) != 1:
        raise ValueError(
            "factory image must contain exactly one flash-all.sh and one image zip"
        )
    layout = {"script": scripts[0], "image": image_archives[0], "entries": names}
    try:
        with zipfile.ZipFile(factory_zip) as archive:
            with archive.open(layout["image"]) as image_stream:
                with zipfile.ZipFile(image_stream) as image_archive:
                    image_names = image_archive.namelist()
                    android_info_entries = [
                        name
                        for name in image_names
                        if Path(name).name == "android-info.txt"
                    ]
                    if len(android_info_entries) != 1:
                        raise ValueError(
                            "factory image archive must contain exactly one android-info.txt"
                        )
                    android_info = image_archive.read(android_info_entries[0]).decode(
                        "utf-8", errors="replace"
                    )
                    super_entries = [
                        name
                        for name in image_names
                        if Path(name).name == "super_empty.img"
                    ]
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(
            "factory image archive cannot be read while checking its metadata: "
            + str(error)
        ) from error
    layout["android_info"] = android_info_entries[0]
    layout["requirements"] = parse_android_info(android_info)
    layout["supported_dynamic_partitions"] = [
        partition
        for partition in FACTORY_DYNAMIC_PARTITION_REPAIR_ORDER
        if any(Path(name).name == f"{partition}.img" for name in image_names)
    ]
    if require_super_empty:
        if len(super_entries) != 1:
            raise ValueError(
                "factory image must contain exactly one super_empty.img for --reset-super"
            )
        layout["super_empty"] = super_entries[0]
    return layout


def validate_factory_model_consistency(
    research: dict,
    layout: dict,
    *,
    actual_product: str | None = None,
) -> dict:
    allowed_boards = layout["requirements"].get("board")
    if not allowed_boards:
        raise ValueError("factory android-info.txt is missing 'require board='")
    research_device = research["device"]
    if research_device not in allowed_boards:
        raise ValueError(
            "research dossier device does not match the factory image: "
            f"{research_device} is not one of {', '.join(allowed_boards)}"
        )
    result = {
        "research_device": research_device,
        "factory_allowed_boards": allowed_boards,
        "actual_product": actual_product,
        "device_check": "pending" if actual_product is None else "complete",
    }
    if actual_product is not None and actual_product not in allowed_boards:
        raise ValueError(
            "connected phone product does not match the factory image: "
            f"{actual_product} is not one of {', '.join(allowed_boards)}"
        )
    return result


def prepare_factory_flash(
    factory_zip: Path,
    fastboot_path: str,
    fastboot_serial: str,
    directory: Path,
    *,
    require_super_empty: bool = False,
) -> tuple[Path, Path, Path, Path | None]:
    resolved_fastboot = shutil.which(fastboot_path)
    if resolved_fastboot is None:
        raise ValueError(f"fastboot executable not found in PATH: {fastboot_path}")
    layout = factory_zip_layout(
        factory_zip, require_super_empty=require_super_empty
    )
    root = directory.resolve()
    with zipfile.ZipFile(factory_zip) as archive:
        for name in layout["entries"]:
            destination = (directory / name).resolve()
            if not destination.is_relative_to(root):
                raise ValueError(f"unsafe factory image entry: {name}")
            archive.extract(name, directory)
    script = directory / layout["script"]
    script.chmod(0o700)
    wrapper_directory = directory / "android-workbench-fastboot"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "fastboot"
    marker = shlex.quote(str(wrapper_directory / "fastboot-failed"))
    wrapper.write_text(
        "#!/bin/sh\n"
        + "if [ -e " + marker + " ]; then echo 'Previous fastboot command failed; stopping' >&2; exit 1; fi\n"
        + "for arg in \"$@\"; do case \"$arg\" in -s|--serial|--serial=*|-s?*) "
          "echo 'Factory script may not override selected device' >&2; "
        + "touch " + marker + "; exit 1;; esac; done\n"
        + shlex.quote(resolved_fastboot) + " -s " + shlex.quote(fastboot_serial) + " \"$@\"\n"
        + "status=$?\nif [ \"$status\" -ne 0 ]; then printf '%s\\n' \"$status\" > " + marker + "; fi\nexit \"$status\"\n"
    )
    wrapper.chmod(0o700)
    super_empty = None
    if "super_empty" in layout:
        nested_name = Path(layout["super_empty"])
        if nested_name.is_absolute() or ".." in nested_name.parts:
            raise ValueError(f"unsafe super image entry: {layout['super_empty']}")
        with zipfile.ZipFile(directory / layout["image"]) as image_archive:
            image_archive.extract(layout["super_empty"], directory)
        super_empty = directory / layout["super_empty"]
    return script, wrapper_directory, directory / Path(layout["script"]).parent, super_empty


def read_device_product(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    *,
    timeout: float,
    records: list[dict],
) -> str:
    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if mode == "android":
        record = run_command(
            adb_command(adb_path, serial, "shell", "getprop", "ro.product.device"),
            label="read device product",
            timeout=min(timeout, 15),
            check=False,
            records=records,
        )
        product = record.get("stdout", "").strip()
    elif mode in {"fastboot", "fastbootd"}:
        record = run_command(
            fastboot_command(fastboot_path, fastboot_serial, "getvar", "product"),
            label="read device product",
            timeout=min(timeout, 15),
            check=False,
            records=records,
        )
        product = fastboot_getvar_value(record, "product")
    else:
        raise RuntimeError(
            f"device {serial} is not online in Android, fastboot, or fastbootd"
        )
    if not product:
        raise RuntimeError("device product could not be read before flashing")
    return product


def factory_repair_candidates(
    inspection: dict,
    base_partition: str,
) -> list[str]:
    candidates = []
    for slot in ("a", "b"):
        partition = f"{base_partition}_{slot}"
        observations = inspection.get("partitions", {}).get(partition, {})
        if observations.get("is_logical") is True:
            candidates.append(partition)
    return candidates


def wipe_factory_super(
    fastboot_path: str,
    fastboot_serial: str,
    super_empty: Path,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> dict:
    return run_command(
        fastboot_command(
            fastboot_path,
            fastboot_serial,
            "--slot",
            "all",
            "wipe-super",
            str(super_empty),
        ),
        label="reset dynamic partitions",
        timeout=timeout,
        dry_run=dry_run,
        check=False,
        records=records,
    )


def reset_factory_super(
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    super_empty: Path,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
    repair_dynamic_partitions: bool = False,
    supported_dynamic_partitions: tuple[str, ...] = (),
) -> None:
    ensure_fastbootd(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )
    if repair_dynamic_partitions:
        last_wipe: dict | None = None
        for base_partition in FACTORY_DYNAMIC_PARTITION_REPAIR_ORDER:
            if base_partition not in supported_dynamic_partitions:
                continue
            inspection = partition_inspect(
                adb_path=adb_path,
                fastboot_path=fastboot_path,
                serial=serial,
                fastboot_serial=fastboot_serial,
                partitions=[f"{base_partition}_a", f"{base_partition}_b"],
                timeout=timeout,
                dry_run=dry_run,
                records=records,
            )
            candidates = (
                [f"{base_partition}_a", f"{base_partition}_b"]
                if dry_run
                else factory_repair_candidates(inspection, base_partition)
            )
            for partition in candidates:
                run_command(
                    fastboot_command(
                        fastboot_path, fastboot_serial, "delete-logical-partition", partition
                    ),
                    label=f"delete logical partition {partition}",
                    timeout=min(timeout, 30),
                    dry_run=dry_run,
                    records=records,
                )
            last_wipe = wipe_factory_super(
                fastboot_path,
                fastboot_serial,
                super_empty,
                timeout=timeout,
                dry_run=dry_run,
                records=records,
            )
            if dry_run or last_wipe.get("returncode") == 0:
                break
        if last_wipe is None:
            raise ValueError(
                "the factory image does not contain a supported dynamic partition for repair"
            )
        if not dry_run and last_wipe.get("returncode") != 0:
            raise RuntimeError(
                "factory wipe-super remained blocked after staged partition repair: "
                + (command_output(last_wipe) or "fastboot returned no output")
            )
    else:
        run_command(
            fastboot_command(
                fastboot_path,
                fastboot_serial,
                "--slot",
                "all",
                "wipe-super",
                str(super_empty),
            ),
            label="reset dynamic partitions",
            timeout=timeout,
            dry_run=dry_run,
            records=records,
        )
    run_command(
        fastboot_command(fastboot_path, fastboot_serial, "reboot", "bootloader"),
        label="reboot to bootloader",
        timeout=min(timeout, 30),
        dry_run=dry_run,
        records=records,
    )
    wait_for_fastboot(
        fastboot_path,
        fastboot_serial,
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )


def run_factory_flash(
    script: Path,
    wrapper_directory: Path,
    working_directory: Path,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    environment = dict(os.environ)
    environment["PATH"] = (
        str(wrapper_directory) + os.pathsep + environment.get("PATH", "")
    )
    run_command(
        ["/bin/sh", "-e", str(script)],
        label="official flash-all.sh",
        timeout=timeout,
        dry_run=dry_run,
        records=records,
        cwd=working_directory,
        env=environment,
        output_limit=FACTORY_OUTPUT_LIMIT,
        preserve_output_tail=True,
    )
    if not dry_run and (wrapper_directory / "fastboot-failed").exists():
        raise RuntimeError("factory script hid a failed fastboot command; installation is incomplete")


def wait_for_adb_state(
    adb_path: str,
    serial: str,
    state: str,
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    if state not in {"device", "recovery", "sideload", "bootloader"}:
        raise ValueError(f"unsupported adb state: {state}")
    run_command(
        adb_command(adb_path, serial, f"wait-for-{state}"),
        label=f"wait for {state}",
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )


def root_commands(
    fastboot_path: str,
    fastboot_serial: str,
    patched_boot: Path,
    *,
    partition: str,
    slot: str,
    reboot: bool,
) -> list[list[str]]:
    if partition != "boot":
        raise ValueError("root flashing supports only the researched boot partition")
    if slot == "both":
        partitions = ["boot_a", "boot_b"]
    elif slot in {"a", "b"}:
        partitions = [f"boot_{slot}"]
    else:
        partitions = ["boot"]
    commands = [
        fastboot_command(
            fastboot_path, fastboot_serial, "flash", partition, str(patched_boot)
        )
        for partition in partitions
    ]
    if reboot:
        commands.append(fastboot_command(fastboot_path, fastboot_serial, "reboot"))
    return commands


def install_magisk_commands(
    adb_path: str,
    serial: str,
    magisk_apk: Path,
) -> list[list[str]]:
    return [adb_command(adb_path, serial, "install", "-r", str(magisk_apk))]


def install_frida_commands(
    adb_path: str,
    serial: str,
    server: Path,
    remote_path: str,
    *,
    start: bool,
) -> list[list[str]]:
    validate_remote_path(remote_path)
    commands = [
        adb_command(adb_path, serial, "shell", device_shell_command("id", root=True)),
        adb_command(adb_path, serial, "push", str(server), remote_path),
        adb_command(
            adb_path,
            serial,
            "shell",
            device_shell_command(f"chmod 755 {shlex.quote(remote_path)}", root=True),
        ),
    ]
    if start:
        start_command = (
            f"nohup {shlex.quote(remote_path)} >/dev/null 2>&1 &"
        )
        commands.append(
            adb_command(
                adb_path,
                serial,
                "shell",
                device_shell_command(start_command, root=True),
            )
        )
    return commands


def inspect_root_access(
    adb_path: str, serial: str, *, timeout: float, records: list[dict], check_root: bool = False
) -> dict:
    access = {"status": "unknown", "caller_uid": None, "checked_at": utc()}
    caller = run_command(
        adb_command(adb_path, serial, "shell", "id -u"),
        label="root caller uid", timeout=min(timeout, 15), check=False, records=records,
    )
    if caller.get("returncode") == 0 and caller.get("stdout", "").strip().isdigit():
        access["caller_uid"] = int(caller["stdout"].strip())
    binary = run_command(
        adb_command(adb_path, serial, "shell", "command -v su"),
        label="su path", timeout=min(timeout, 15), check=False, records=records,
    )
    access["su_path"] = binary.get("stdout", "").strip() or None
    if binary.get("returncode") == 1 and not access["su_path"] and not binary.get("stderr", "").strip():
        access.update(status="su_missing", reason="su was not found in the caller's PATH")
        return access
    if binary.get("returncode") != 0:
        access["reason"] = command_output(binary) or "su presence check failed"
        return access
    try:
        version = run_command(
            adb_command(adb_path, serial, "shell", "su -v"),
            label="su version", timeout=min(timeout, 15), check=False, records=records,
        )
    except RuntimeError:
        if not records or records[-1].get("name") != "su version" or records[-1].get("returncode") != 124:
            raise
        version = records[-1]
    access["su_version"] = (version.get("stdout", "").strip() or None) if version.get("returncode") == 0 else None
    if not check_root:
        access.update(status="not_checked", reason="root access was not requested")
        return access
    try:
        record = run_command(
            adb_command(adb_path, serial, "shell", device_shell_command("id", root=True)),
            label="root id", timeout=min(timeout, 15), check=False, records=records,
        )
    except RuntimeError:
        if not records or records[-1].get("name") != "root id" or records[-1].get("returncode") != 124:
            raise
        access["reason"] = "root check timed out; authorization or transport state is unconfirmed"
        return access
    if record.get("returncode") == 0 and re.search(r"\buid=0\b", record.get("stdout", "")):
        access["status"] = "granted"
    else:
        detail = command_output(record)
        access["status"] = "denied" if "permission denied" in detail.lower() else "unknown"
        access["reason"] = detail or "root command did not confirm uid=0"
    return access


def preflight(
    *,
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    timeout: float,
    fastboot_mode: bool = False,
    check_root: bool = False,
) -> dict:
    records: list[dict] = []
    result = {
        "schema": 1,
        "utc": utc(),
        "device": serial,
        "fastboot_device": fastboot_serial,
        "mode": "unknown",
        "observations": {},
        "unavailable": {},
        "steps": records,
        "status": "incomplete",
    }
    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if fastboot_mode and mode == "android":
        run_command(
            adb_command(adb_path, serial, "reboot-bootloader"),
            label="reboot to bootloader",
            timeout=min(timeout, 30),
            records=records,
        )
        wait_for_fastboot(
            fastboot_path,
            fastboot_serial,
            timeout=timeout,
            dry_run=False,
            records=records,
        )
        mode = "fastboot"
    result["mode"] = mode
    if mode == "android":
        result["observations"]["adb_authorization"] = "authorized"
        properties = {
            "android": "ro.build.version.release",
            "api": "ro.build.version.sdk",
            "model": "ro.product.model",
            "device": "ro.product.device",
            "abis": "ro.product.cpu.abilist",
            "slot": "ro.boot.slot_suffix",
            "build_fingerprint": "ro.build.fingerprint",
            "bootloader_locked": "ro.boot.flash.locked",
        }
        for name, prop in properties.items():
            record = run_command(
                adb_command(adb_path, serial, "shell", "getprop", prop),
                label=f"getprop {prop}",
                timeout=min(timeout, 15),
                check=False,
                records=records,
            )
            value = record.get("stdout", "").strip()
            if value:
                result["observations"][name] = value
            else:
                result["unavailable"][name] = "property empty"
        boot = run_command(
            adb_command(adb_path, serial, "shell", "cat /proc/sys/kernel/random/boot_id"),
            label="boot id", timeout=min(timeout, 15), check=False, records=records,
        )
        if boot.get("returncode") == 0 and boot.get("stdout", "").strip():
            result["observations"]["boot_id"] = boot["stdout"].strip()
        result["root_access"] = inspect_root_access(
            adb_path, serial, timeout=timeout, records=records, check_root=check_root,
        )
        if result["root_access"]["status"] == "granted":
            result["observations"]["root"] = "available"
        else:
            result["unavailable"]["root"] = result["root_access"]["reason"]
        frida_record = run_command(
            adb_command(adb_path, serial, "shell", "pidof", "frida-server"),
            label="frida-server pid",
            timeout=min(timeout, 15),
            check=False,
            records=records,
        )
        frida_pid = frida_record.get("stdout", "").strip()
        if frida_pid:
            result["observations"]["frida_server_pid"] = frida_pid
        else:
            result["unavailable"]["frida_server"] = "not running"
        battery_record = run_command(
            adb_command(adb_path, serial, "shell", "dumpsys", "battery"),
            label="battery",
            timeout=min(timeout, 15),
            check=False,
            records=records,
        )
        if battery_record.get("returncode") == 0:
            result["observations"].update(battery_observations(battery_record.get("stdout", "")))
        else:
            result["unavailable"]["battery"] = "battery query failed"
        storage_record = run_command(
            adb_command(adb_path, serial, "shell", "df", "-k", "/data"),
            label="storage",
            timeout=min(timeout, 15),
            check=False,
            records=records,
        )
        if storage_record.get("stdout", "").strip():
            result["observations"]["data_partition"] = storage_record["stdout"].strip()
    elif mode in {"fastboot", "fastbootd"}:
        for variable in ("product", "current-slot", "unlocked"):
            record = run_command(
                fastboot_command(
                    fastboot_path, fastboot_serial, "getvar", variable
                ),
                label=f"fastboot getvar {variable}",
                timeout=min(timeout, 15),
                check=False,
                records=records,
            )
            output = command_output(record)
            if output:
                result["observations"][f"fastboot_{variable.replace('-', '_')}"] = output
            else:
                result["unavailable"][f"fastboot_{variable.replace('-', '_')}"] = "empty"
    else:
        result["observations"]["adb_authorization"] = (
            "unauthorized" if mode == "unauthorized" else "unknown"
        )
        result["unavailable"]["mode"] = "device not online in Android or fastboot"
    if fastboot_mode and mode == "fastboot":
        result["inspected_mode"] = "fastboot"
        ensure_android(
            adb_path,
            fastboot_path,
            serial,
            fastboot_serial,
            timeout=timeout,
            dry_run=False,
            records=records,
        )
        result["mode"] = "android"
    if result.get("inspected_mode") == "fastboot":
        required = {"fastboot_product", "fastboot_current_slot"}
    elif mode == "android":
        required = {"android", "model", "device"}
    elif mode in {"fastboot", "fastbootd"}:
        required = {"fastboot_product", "fastboot_current_slot"}
    else:
        required = set()
    result["status"] = "complete" if required and required.issubset(result["observations"]) else "incomplete"
    return result


def fastboot_getvar_value(record: dict, name: str) -> str | None:
    match = re.search(
        rf"(?:\(bootloader\)\s*)?{re.escape(name)}\s*:\s*([^\r\n]+)",
        command_output(record),
    )
    return match.group(1).strip() if match else None


def partition_inspect_commands(
    fastboot_path: str,
    fastboot_serial: str,
    partitions: list[str],
) -> list[list[str]]:
    commands = [
        fastboot_command(fastboot_path, fastboot_serial, "getvar", "current-slot"),
        fastboot_command(fastboot_path, fastboot_serial, "getvar", "is-userspace"),
        fastboot_command(
            fastboot_path, fastboot_serial, "getvar", "super-partition-name"
        ),
    ]
    for partition in partitions:
        commands.extend(
            [
                fastboot_command(
                    fastboot_path, fastboot_serial, "getvar", f"is-logical:{partition}"
                ),
                fastboot_command(
                    fastboot_path,
                    fastboot_serial,
                    "getvar",
                    f"partition-size:{partition}",
                ),
            ]
        )
    return commands


def partition_inspect(
    *,
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    partitions: list[str],
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> dict:
    commands = partition_inspect_commands(
        fastboot_path, fastboot_serial, partitions
    )
    if dry_run:
        for command in commands:
            run_command(
                command,
                label="inspect partition",
                timeout=timeout,
                dry_run=True,
                records=records,
            )
        return {"mode": "dry_run", "partitions": {}}

    mode = detect_mode(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        records=records,
    )
    if mode != "fastbootd":
        raise RuntimeError(
            "partition inspection requires fastbootd because logical partitions are "
            f"managed there; current mode is {mode}"
        )

    global_variables = ("current-slot", "is-userspace", "super-partition-name")
    global_values: dict[str, str | None] = {}
    for variable in global_variables:
        record = run_command(
            fastboot_command(fastboot_path, fastboot_serial, "getvar", variable),
            label=f"getvar {variable}",
            timeout=min(timeout, 30),
            check=False,
            records=records,
        )
        global_values[variable] = fastboot_getvar_value(record, variable)

    partition_values: dict[str, dict[str, str | bool | None]] = {}
    for partition in partitions:
        observations: dict[str, str | bool | None] = {}
        for variable in (f"is-logical:{partition}", f"partition-size:{partition}"):
            record = run_command(
                fastboot_command(fastboot_path, fastboot_serial, "getvar", variable),
                label=f"getvar {variable}",
                timeout=min(timeout, 30),
                check=False,
                records=records,
            )
            value = fastboot_getvar_value(record, variable)
            if variable.startswith("is-logical:"):
                observations["is_logical"] = (
                    None
                    if value is None
                    else value.strip().lower() in {"yes", "true", "1"}
                )
            else:
                observations["partition_size"] = value
        partition_values[partition] = observations

    return {
        "mode": mode,
        "current_slot": global_values["current-slot"],
        "is_userspace": global_values["is-userspace"],
        "super_partition_name": global_values["super-partition-name"],
        "partitions": partition_values,
    }


def partition_repair(
    *,
    adb_path: str,
    fastboot_path: str,
    serial: str,
    fastboot_serial: str,
    delete_partitions: list[str],
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> dict:
    if (not delete_partitions or len(delete_partitions) != len(set(delete_partitions))
            or any(part not in FACTORY_PARTITION_DELETE_ALLOWLIST for part in delete_partitions)):
        raise ValueError("duplicate or unsupported logical partition deletion")
    ensure_fastbootd(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )
    if not dry_run:
        inspection = partition_inspect(adb_path=adb_path, fastboot_path=fastboot_path, serial=serial,
                                       fastboot_serial=fastboot_serial, partitions=delete_partitions,
                                       timeout=timeout, dry_run=False, records=records)
        for partition in delete_partitions:
            observed = inspection["partitions"][partition]
            if observed.get("is_logical") is not True:
                raise ValueError("refusing to delete an unverified logical partition: " + partition)
            parse_partition_size(observed.get("partition_size"))
    for partition in delete_partitions:
        run_command(
            fastboot_command(
                fastboot_path, fastboot_serial, "delete-logical-partition", partition
            ),
            label=f"delete logical partition {partition}",
            timeout=min(timeout, 30),
            dry_run=dry_run,
            records=records,
        )
    return {
        "mode": "fastbootd",
        "deleted_partitions": list(delete_partitions),
    }


def execute_plan(
    commands: list[list[str]],
    *,
    timeout: float,
    dry_run: bool,
    records: list[dict],
) -> None:
    for command in commands:
        run_command(
            command,
            label="execute",
            timeout=timeout,
            dry_run=dry_run,
            records=records,
        )


def write_result(output: Path, result: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    def global_options(defaults):
        options = argparse.ArgumentParser(add_help=False)
        options.add_argument("--serial", default=defaults["serial"])
        options.add_argument("--adb", default=defaults["adb"])
        options.add_argument("--fastboot", default=defaults["fastboot"])
        options.add_argument(
            "--fastboot-serial", default=defaults["fastboot_serial"]
        )
        options.add_argument("--timeout", type=float, default=defaults["timeout"])
        options.add_argument("--output", type=Path, default=defaults["output"])
        return options

    before_subcommand = global_options(
        {
            "serial": None,
            "adb": DEFAULT_ADB,
            "fastboot": DEFAULT_FASTBOOT,
            "fastboot_serial": None,
            "timeout": 180.0,
            "output": None,
        }
    )
    after_subcommand = global_options(
        {name: argparse.SUPPRESS for name in (
            "serial",
            "adb",
            "fastboot",
            "fastboot_serial",
            "timeout",
            "output",
        )}
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[before_subcommand])
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser(
        "preflight",
        parents=[after_subcommand],
        help="inspect Android, fastboot, root, and Frida state",
    )
    preflight_parser.add_argument(
        "--check-root", action="store_true",
        help="verify root with su -c id; this may show a phone-side authorization prompt",
    )

    verify_parser = subparsers.add_parser(
        "flash-verify", parents=[after_subcommand],
        help="verify the booted device and target build after a completed flash",
    )
    verify_parser.add_argument("--flash-record", type=Path, required=True)
    verify_parser.add_argument("--health-record", type=Path)

    for name in ("backup-prepare", "backup-verify", "backup-data"):
        backup_parser = subparsers.add_parser(name, parents=[after_subcommand],
                                              help="create or check file-backed backup evidence")
        backup_parser.add_argument("--research", type=Path, required=True)
        if name == "backup-verify":
            backup_parser.add_argument("--backup-record", type=Path, required=True)
        else:
            backup_parser.add_argument("--backup-dir", type=Path, required=True)
            if name == "backup-prepare":
                backup_parser.add_argument("--image", action="append", required=True, metavar="PARTITION=PATH")
                backup_parser.add_argument("--data-record", type=Path)
                backup_parser.add_argument("--discard-data-reason")
            else:
                backup_parser.add_argument("--remote-path", action="append", required=True)

    ota_parser = subparsers.add_parser("install-ota", parents=[after_subcommand],
                                      help="prepare stock recovery and sideload a reviewed full OTA")
    ota_parser.add_argument("--stage", choices=("prepare", "sideload"), required=True)
    ota_parser.add_argument("--ota", type=Path, required=True)
    ota_parser.add_argument("--prepared-record", type=Path)
    ota_parser.add_argument("--research", type=Path, required=True)
    ota_parser.add_argument("--confirm", action="store_true")
    ota_parser.add_argument("--dry-run", action="store_true")
    ota_parser.set_defaults(timeout=1800.0)

    bootloader_parser = subparsers.add_parser(
        "bootloader-state",
        parents=[after_subcommand],
        help="reboot through fastboot to read bootloader state, then return to Android",
    )
    bootloader_parser.add_argument("--confirm", action="store_true")

    partition_inspect_parser = subparsers.add_parser(
        "partition-inspect",
        parents=[after_subcommand],
        help="read dynamic-partition state in fastbootd without changing it",
    )
    partition_inspect_parser.add_argument(
        "--partition",
        action="append",
        metavar="NAME",
    )
    partition_inspect_parser.add_argument("--dry-run", action="store_true")

    partition_repair_parser = subparsers.add_parser(
        "partition-repair",
        parents=[after_subcommand],
        help="enter fastbootd and optionally delete blocking product partitions",
    )
    partition_repair_parser.add_argument(
        "--delete-partition",
        action="append",
        choices=sorted(FACTORY_PARTITION_DELETE_ALLOWLIST),
        metavar="NAME",
    )
    partition_repair_parser.add_argument("--research", type=Path, required=True)
    partition_repair_parser.add_argument("--confirm", action="store_true")
    partition_repair_parser.add_argument("--dry-run", action="store_true")

    install_rom_parser = subparsers.add_parser(
        "install-rom",
        parents=[after_subcommand],
        help="prepare LineageOS recovery and sideload a custom ROM package",
    )
    install_rom_parser.add_argument(
        "--stage", choices=("prepare", "sideload"), required=True
    )
    install_rom_parser.add_argument("--recovery-boot", type=Path)
    install_rom_parser.add_argument("--rom", type=Path)
    install_rom_parser.add_argument("--prepared-record", type=Path)
    install_rom_parser.add_argument("--prepare-image", action="append", default=[], metavar="PARTITION=PATH")
    install_rom_parser.add_argument("--addon", type=Path, action="append", default=[])
    install_rom_parser.add_argument("--package-index", type=int, default=0)
    install_rom_parser.add_argument("--research", type=Path)
    install_rom_parser.add_argument("--no-reboot", action="store_true")
    install_rom_parser.add_argument("--confirm", action="store_true")
    install_rom_parser.add_argument("--dry-run", action="store_true")
    install_rom_parser.set_defaults(timeout=1800.0)

    research_parser = subparsers.add_parser(
        "research",
        parents=[after_subcommand],
        help="validate a device-specific flashing research dossier",
    )
    research_parser.add_argument("--dossier", type=Path, required=True)
    research_parser.add_argument("--cache", type=Path)
    research_parser.add_argument("--refresh", action="store_true")

    image_download_parser = subparsers.add_parser(
        "image-download",
        parents=[after_subcommand],
        help="download a flashing image into a content-addressed cache",
    )
    image_download_parser.add_argument("--url", required=True)
    image_download_parser.add_argument("--sha256", required=True)
    image_download_parser.add_argument("--label", required=True)
    image_download_parser.add_argument("--partition")
    image_download_parser.add_argument("--cache-dir", type=Path, required=True)
    image_download_parser.add_argument("--chunks", type=int, default=8)
    image_download_parser.add_argument("--refresh", action="store_true")

    image_verify_parser = subparsers.add_parser(
        "image-verify",
        parents=[after_subcommand],
        help="verify a local flashing image and write an evidence record",
    )
    image_verify_parser.add_argument("--image", type=Path, required=True)
    image_verify_parser.add_argument("--sha256", required=True)
    image_verify_parser.add_argument("--label", required=True)
    image_verify_parser.add_argument("--partition")

    flash_parser = subparsers.add_parser(
        "flash", parents=[after_subcommand], help="flash partition images"
    )
    flash_parser.add_argument(
        "--image",
        action="append",
        required=True,
        metavar="PARTITION=PATH",
    )
    flash_parser.add_argument("--no-reboot", action="store_true")
    flash_parser.add_argument("--research", type=Path)
    flash_parser.add_argument("--confirm", action="store_true")
    flash_parser.add_argument("--dry-run", action="store_true")

    factory_parser = subparsers.add_parser(
        "flash-factory",
        parents=[after_subcommand],
        help="run an official Google factory image flash-all script",
    )
    factory_parser.add_argument("--factory-zip", type=Path, required=True)
    factory_parser.add_argument("--research", type=Path, required=True)
    factory_parser.add_argument(
        "--reset-super",
        action="store_true",
        help="reset dynamic partitions with the factory super_empty.img before flashing",
    )
    factory_parser.add_argument(
        "--repair-dynamic-partitions",
        action="store_true",
        help=(
            "with --reset-super, inspect and delete only image-supported blocking "
            "product, odm, or system_ext logical partitions between wipe attempts"
        ),
    )
    factory_parser.add_argument("--confirm", action="store_true")
    factory_parser.add_argument("--dry-run", action="store_true")

    root_prepare_parser = subparsers.add_parser(
        "root-prepare",
        parents=[after_subcommand],
        help="install Magisk and copy the stock boot image to the phone",
    )
    root_prepare_parser.add_argument("--stock-boot", type=Path, required=True)
    root_prepare_parser.add_argument("--magisk-apk", type=Path, required=True)
    root_prepare_parser.add_argument(
        "--remote-boot", default="/sdcard/Download/boot.img"
    )
    root_prepare_parser.add_argument("--research", type=Path, required=True)
    root_prepare_parser.add_argument("--confirm", action="store_true")
    root_prepare_parser.add_argument("--dry-run", action="store_true")

    root_collect_parser = subparsers.add_parser(
        "root-collect",
        parents=[after_subcommand],
        help="collect the Magisk-patched boot image from the phone",
    )
    root_collect_parser.add_argument("--destination", type=Path, required=True)
    root_collect_parser.add_argument(
        "--remote-directory", default="/sdcard/Download"
    )
    root_collect_parser.add_argument("--research", type=Path, required=True)
    root_collect_parser.add_argument("--confirm", action="store_true")
    root_collect_parser.add_argument("--dry-run", action="store_true")

    root_parser = subparsers.add_parser(
        "root", parents=[after_subcommand], help="flash a Magisk-patched boot image"
    )
    root_parser.add_argument("--patched-boot", type=Path, required=True)
    root_parser.add_argument("--magisk-apk", type=Path)
    root_parser.add_argument(
        "--slot", choices=("current", "a", "b", "both"), default="current"
    )
    root_parser.add_argument("--no-reboot", action="store_true")
    root_parser.add_argument("--research", type=Path)
    root_parser.add_argument("--confirm", action="store_true")
    root_parser.add_argument("--dry-run", action="store_true")

    frida_parser = subparsers.add_parser(
        "install-frida",
        parents=[after_subcommand],
        help="install a local Frida server binary",
    )
    frida_parser.add_argument("--server", type=Path, required=True)
    frida_parser.add_argument("--remote-path", default=DEFAULT_REMOTE_FRIDA_PATH)
    frida_parser.add_argument("--research", type=Path)
    frida_parser.add_argument("--start", action="store_true")
    frida_parser.add_argument("--confirm", action="store_true")
    frida_parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    global FLASH_PROGRESS
    FLASH_PROGRESS = None
    parser = build_parser()
    arguments = parser.parse_args()
    if arguments.output is None:
        parser.error("--output is required")
    if (
        arguments.command
        in {
            "preflight",
            "flash-verify",
            "backup-data",
            "install-ota",
            "bootloader-state",
            "partition-inspect",
            "partition-repair",
            "flash",
            "flash-factory",
            "root",
            "root-prepare",
            "root-collect",
            "install-frida",
            "install-rom",
        }
        and not arguments.serial
    ):
        parser.error("this command requires --serial")
    if arguments.output.exists():
        parser.error("Use a new output path; existing evidence is retained")
    fastboot_serial = arguments.fastboot_serial or arguments.serial
    records: list[dict] = []
    result = {
        "schema": 1,
        "utc": utc(),
        "device": arguments.serial,
        "fastboot_device": fastboot_serial,
        "command": arguments.command,
        "steps": records,
    }
    if arguments.command in {"flash", "flash-factory", "install-rom", "install-ota", "partition-repair"}:
        FLASH_PROGRESS = lambda step: write_result(
            arguments.output, {**result, "status": "in_progress", "active_step": step,
                               "workflow_complete": False})
    if arguments.command in OPERATOR_PRESENCE_COMMANDS:
        result["operator_notice"] = OPERATOR_NOTICE
        prefix = "For a real run: " if getattr(arguments, "dry_run", False) else ""
        print(prefix + OPERATOR_NOTICE, file=sys.stderr, flush=True)
    try:
        if arguments.command == "preflight":
            result.update(
                preflight(
                    adb_path=arguments.adb,
                    fastboot_path=arguments.fastboot,
                    serial=arguments.serial,
                    fastboot_serial=fastboot_serial,
                    timeout=arguments.timeout,
                    check_root=arguments.check_root,
                )
            )
        elif arguments.command == "flash-verify":
            result.update(verify_flashed_system(arguments, records))
            result["status"] = "complete"
        elif arguments.command in {"backup-prepare", "backup-verify", "backup-data"}:
            research = load_research(arguments.research)
            validate_research(research)
            if arguments.command == "backup-verify":
                result["backup"] = verify_backup(arguments.backup_record, research)
                result["restore_requires_fresh_plan"] = True
            elif arguments.command == "backup-prepare":
                images = parse_images(arguments.image)
                result.update(prepare_backup(research, images, arguments.backup_dir,
                    data_record=arguments.data_record, discard_reason=arguments.discard_data_reason))
            else:
                plan = research.get("flash_plan", {})
                if plan.get("serial") != arguments.serial:
                    raise ValueError("data backup serial does not match the selected phone")
                paths = [remote_backup_path(path) for path in arguments.remote_path]
                if len(paths) != len(set(paths)):
                    raise ValueError("duplicate data backup paths")
                directory = arguments.backup_dir.expanduser().resolve()
                if directory.exists():
                    raise ValueError("Use a new backup directory; existing files are retained")
                result["observed"] = check_android_source(arguments, research, records, require_baseline=True)
                directory.mkdir(parents=True)
                for index, path in enumerate(paths):
                    target = directory / str(index)
                    target.mkdir()
                    run_command(adb_command(arguments.adb, arguments.serial, "pull", "-a", path, str(target)),
                                label="backup shared storage " + path, timeout=arguments.timeout, records=records)
                result.update(serial=arguments.serial, source_fingerprint=plan["source_fingerprint"],
                              coverage="user_selected_shared_storage", remote_paths=paths,
                              files=tree_files(directory), includes_private_app_data=False)
                manifest = directory / "data-manifest.json"
                write_result(manifest, {**result, "status": "complete"})
                result.update(data_manifest=str(manifest), data_manifest_sha256=sha256_file(manifest))
            result["status"] = "complete"
        elif arguments.command == "install-ota":
            install_ota(arguments, result, records)
        elif arguments.command == "partition-inspect":
            partitions = list(
                arguments.partition or DEFAULT_PARTITION_INSPECT_PARTITIONS
            )
            for partition in partitions:
                validate_partition(partition)
            commands = partition_inspect_commands(
                arguments.fastboot, fastboot_serial, partitions
            )
            result.update(
                {
                    "partitions_requested": partitions,
                    "plan": [list(command) for command in commands],
                }
            )
            result.update(
                partition_inspect(
                    adb_path=arguments.adb,
                    fastboot_path=arguments.fastboot,
                    serial=arguments.serial,
                    fastboot_serial=fastboot_serial,
                    partitions=partitions,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
            )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "partition-repair":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("partition-repair requires --confirm")
            research = load_research(arguments.research)
            validate_research(research)
            validate_factory_flash_decisions(research, no_reboot=False)
            result["flash_checks"] = validate_flash_plan(
                research, [], serial=arguments.serial,
                fastboot_serial=fastboot_serial, dry_run=arguments.dry_run)
            delete_partitions = list(arguments.delete_partition or [])
            if not delete_partitions:
                raise ValueError("partition-repair requires at least one --delete-partition")
            if not arguments.dry_run:
                if research["flash_plan"].get("delete_partitions") != delete_partitions:
                    raise ValueError("delete_partitions differ from the researched repair plan")
                if research["flash_plan"]["mode"] != "fastbootd":
                    raise ValueError("partition repair requires flash_plan mode fastbootd")
                check_flash_device(arguments, research, fastboot_serial=fastboot_serial, records=records)
            commands = [
                fastboot_command(
                    arguments.fastboot,
                    fastboot_serial,
                    "delete-logical-partition",
                    partition,
                )
                for partition in delete_partitions
            ]
            result.update(
                {
                    "research": research,
                    "delete_partitions": delete_partitions,
                    "plan": [list(command) for command in commands],
                    "mode_transition": {
                        "action": "ensure fastbootd",
                        "conditional": True,
                        "description": "Detect the current mode and reboot only when needed.",
                    },
                }
            )
            result.update(
                partition_repair(
                    adb_path=arguments.adb,
                    fastboot_path=arguments.fastboot,
                    serial=arguments.serial,
                    fastboot_serial=fastboot_serial,
                    delete_partitions=delete_partitions,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
            )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "bootloader-state":
            if not arguments.confirm:
                raise ValueError("bootloader-state requires --confirm")
            result.update(
                preflight(
                    adb_path=arguments.adb,
                    fastboot_path=arguments.fastboot,
                    serial=arguments.serial,
                    fastboot_serial=fastboot_serial,
                    timeout=arguments.timeout,
                    fastboot_mode=True,
                )
            )
        elif arguments.command == "research":
            dossier = load_research(arguments.dossier)
            if arguments.cache is not None and arguments.cache.resolve() == arguments.output.resolve():
                raise ValueError("research cache and output must use different paths")
            cache_key = research_cache_key(dossier)
            cache = (
                load_research_cache(arguments.cache)
                if arguments.cache is not None
                else None
            )
            if (
                cache is not None
                and not arguments.refresh
                and cache_key in cache["entries"]
            ):
                result.update(cache["entries"][cache_key])
                result["cached"] = True
            else:
                validated = validate_research(dossier)
                validated["cache_key"] = cache_key
                validated["cached_at"] = utc()
                result.update(validated)
                result["cached"] = False
                if cache is not None:
                    cache["entries"][cache_key] = validated
                    save_research_cache(arguments.cache, cache)
        elif arguments.command == "image-download":
            expected_sha256 = arguments.sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ValueError("image sha256 must be 64 hexadecimal characters")
            cache_dir = arguments.cache_dir.expanduser().resolve()
            image_path = image_cache_path(cache_dir, expected_sha256, arguments.url)
            if image_path.is_file() and not arguments.refresh:
                actual_sha256 = sha256_file(image_path)
                if actual_sha256 != expected_sha256:
                    raise ValueError(
                        "cached image sha256 mismatch; remove the corrupted cache entry or use --refresh"
                    )
                image_record = {
                    "url": arguments.url,
                    "size": image_path.stat().st_size,
                    "sha256": actual_sha256,
                    "cached": True,
                }
            else:
                image_record = download_image(
                    arguments.url,
                    image_path,
                    expected_sha256,
                    timeout=arguments.timeout,
                    chunks=arguments.chunks,
                )
            result.update(
                {
                    "label": arguments.label,
                    "partition": arguments.partition,
                    "cache_dir": str(cache_dir),
                    "image": str(image_path),
                    **image_record,
                    "status": "complete",
                }
            )
        elif arguments.command == "image-verify":
            expected_sha256 = arguments.sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ValueError("image sha256 must be 64 hexadecimal characters")
            image_path = arguments.image.expanduser().resolve()
            if not image_path.is_file():
                raise ValueError(f"image does not exist: {image_path}")
            actual_sha256 = sha256_file(image_path)
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"image sha256 mismatch: expected {expected_sha256}, got {actual_sha256}"
                )
            result.update(
                {
                    "label": arguments.label,
                    "partition": arguments.partition,
                    "image": str(image_path),
                    "size": image_path.stat().st_size,
                    "sha256": actual_sha256,
                    "status": "complete",
                }
            )
        elif arguments.command == "flash":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("flash requires --confirm")
            if arguments.research is None:
                raise ValueError("flash requires --research")
            images = parse_images(arguments.image)
            research = load_research(arguments.research)
            validate_research(research)
            validate_flash_decisions(
                research,
                images,
                no_reboot=arguments.no_reboot,
            )
            result["research"] = research
            result["flash_checks"] = validate_flash_plan(
                research, images, serial=arguments.serial,
                fastboot_serial=fastboot_serial, dry_run=arguments.dry_run)
            record_flash_stage(result, research, "awaiting_verification")
            commands = flash_commands(
                arguments.fastboot,
                fastboot_serial,
                images,
                reboot=not arguments.no_reboot,
            )
            if not arguments.dry_run or (research.get("flash_plan") or {}).get("partition_writes"):
                writes = reviewed_partition_writes(research["flash_plan"], images)
                plan = research["flash_plan"]
                activate = plan.get("activate_slot")
                if activate not in {None, "a", "b"} or plan["target_slot"] != (activate or plan["slot"]):
                    raise ValueError("target_slot requires an explicit reviewed activate_slot or must remain unchanged")
                if any(rule["mode"] != plan["mode"] for rule, _ in writes):
                    raise ValueError("generic flash requires a single reviewed mode")
                validate_flash_decisions(research, [(rule["partition"], path) for rule, path in writes],
                                         no_reboot=arguments.no_reboot)
                commands = [reviewed_write_command(arguments.fastboot, fastboot_serial, rule, path)
                            for rule, path in writes]
                if activate:
                    commands.append(fastboot_command(arguments.fastboot, fastboot_serial, "set_active", activate))
                if not arguments.no_reboot:
                    commands.append(fastboot_command(arguments.fastboot, fastboot_serial, "reboot"))
            result["plan"] = [list(command) for command in commands]
            if not arguments.dry_run:
                check_flash_device(arguments, research, fastboot_serial=fastboot_serial, records=records)
                for rule, path in writes:
                    check_partition_slot(arguments, fastboot_serial, rule, records, image=path)
                validate_flash_plan(research, images, serial=arguments.serial,
                                    fastboot_serial=fastboot_serial, dry_run=False)
                result["flash_checks"].update(device_checks="passed", execution_ready=True)
            execute_plan(
                commands,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            if not arguments.no_reboot and not arguments.dry_run:
                wait_for_boot(
                    arguments.adb,
                    arguments.serial,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "flash-factory":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("flash-factory requires --confirm")
            if arguments.repair_dynamic_partitions and not arguments.reset_super:
                raise ValueError(
                    "--repair-dynamic-partitions requires --reset-super"
                )
            factory_zip = validate_local_file(
                arguments.factory_zip, "official factory image"
            )
            research = load_research(arguments.research)
            validate_research(research)
            validate_factory_flash_decisions(
                research,
                no_reboot=False,
            )
            layout = factory_zip_layout(
                factory_zip, require_super_empty=arguments.reset_super
            )
            model_consistency = validate_factory_model_consistency(research, layout)
            result["flash_checks"] = validate_flash_plan(
                research, [("factory", factory_zip)], serial=arguments.serial,
                fastboot_serial=fastboot_serial, dry_run=arguments.dry_run)
            record_flash_stage(result, research, "awaiting_verification")
            if research.get("flash_plan", {}).get("mode", "fastboot") != "fastboot":
                raise ValueError("factory installation must start in fastboot bootloader mode")
            if not arguments.dry_run:
                if research["flash_plan"].get("factory_options") != {
                        "reset_super": arguments.reset_super,
                        "repair_dynamic_partitions": arguments.repair_dynamic_partitions}:
                    raise ValueError("factory_options differ from the researched installation")
                check_flash_device(arguments, research, fastboot_serial=fastboot_serial, records=records)
                validate_flash_plan(research, [("factory", factory_zip)], serial=arguments.serial,
                                    fastboot_serial=fastboot_serial, dry_run=False)
                result["flash_checks"].update(device_checks="passed", execution_ready=True)
                actual_product = read_device_product(
                    arguments.adb,
                    arguments.fastboot,
                    arguments.serial,
                    fastboot_serial,
                    timeout=arguments.timeout,
                    records=records,
                )
                model_consistency = validate_factory_model_consistency(
                    research,
                    layout,
                    actual_product=actual_product,
                )
            if arguments.reset_super:
                super_plan = fastboot_command(
                    arguments.fastboot,
                    fastboot_serial,
                    "--slot",
                    "all",
                    "wipe-super",
                    f"{factory_zip}::{layout['image']}::{layout['super_empty']}",
                )
                factory_plan = [
                    super_plan,
                    fastboot_command(
                        arguments.fastboot, fastboot_serial, "reboot", "bootloader"
                    ),
                    [
                        str(factory_zip),
                        layout["script"],
                        f"fastboot serial {fastboot_serial}",
                    ],
                ]
            else:
                factory_plan = [
                    [
                        str(factory_zip),
                        layout["script"],
                        f"fastboot serial {fastboot_serial}",
                    ]
                ]
            result.update(
                {
                    "research": research,
                    "factory_image": str(factory_zip),
                    "factory_script": layout["script"],
                    "factory_image_zip": layout["image"],
                    "super_empty": layout.get("super_empty"),
                    "plan": factory_plan,
                    "reset_super": arguments.reset_super,
                    "repair_dynamic_partitions": arguments.repair_dynamic_partitions,
                    "supported_dynamic_partitions": layout[
                        "supported_dynamic_partitions"
                    ],
                    "model_consistency": model_consistency,
                }
            )
            if arguments.dry_run:
                if arguments.reset_super:
                    reset_factory_super(
                        arguments.adb,
                        arguments.fastboot,
                        arguments.serial,
                        fastboot_serial,
                        Path(f"{factory_zip}::{layout['image']}::{layout['super_empty']}"),
                        timeout=arguments.timeout,
                        dry_run=True,
                        records=records,
                        repair_dynamic_partitions=arguments.repair_dynamic_partitions,
                        supported_dynamic_partitions=tuple(
                            layout["supported_dynamic_partitions"]
                        ),
                    )
                else:
                    ensure_bootloader(
                        arguments.adb,
                        arguments.fastboot,
                        arguments.serial,
                        fastboot_serial,
                        timeout=arguments.timeout,
                        dry_run=True,
                        records=records,
                    )
                run_factory_flash(
                    Path(layout["script"]),
                    Path("android-workbench-fastboot"),
                    Path("."),
                    timeout=arguments.timeout,
                    dry_run=True,
                    records=records,
                )
            else:
                with tempfile.TemporaryDirectory(
                    prefix="android-workbench-factory-"
                ) as temporary_directory:
                    directory = Path(temporary_directory) / "factory"
                    directory.mkdir()
                    (
                        script,
                        wrapper_directory,
                        working_directory,
                        super_empty,
                    ) = prepare_factory_flash(
                        factory_zip,
                        arguments.fastboot,
                        fastboot_serial,
                        directory,
                        require_super_empty=arguments.reset_super,
                    )
                    if arguments.reset_super:
                        if super_empty is None:
                            raise ValueError("factory super_empty.img was not extracted")
                        reset_factory_super(
                            arguments.adb,
                            arguments.fastboot,
                            arguments.serial,
                            fastboot_serial,
                            super_empty,
                            timeout=arguments.timeout,
                            dry_run=False,
                            records=records,
                            repair_dynamic_partitions=arguments.repair_dynamic_partitions,
                            supported_dynamic_partitions=tuple(
                                layout["supported_dynamic_partitions"]
                            ),
                        )
                    else:
                        ensure_bootloader(
                            arguments.adb,
                            arguments.fastboot,
                            arguments.serial,
                            fastboot_serial,
                            timeout=arguments.timeout,
                            dry_run=False,
                            records=records,
                        )
                    run_factory_flash(
                        script,
                        wrapper_directory,
                        working_directory,
                        timeout=arguments.timeout,
                        dry_run=False,
                        records=records,
                    )
                result["next_action"] = (
                    "Stay beside the phone. Let it boot to the setup wizard, "
                    "complete setup, enable "
                    "USB debugging, and authorize this computer before running ADB "
                    "commands."
                )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "install-rom":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("install-rom requires --confirm")
            if arguments.research is None:
                raise ValueError("install-rom requires --research")
            if arguments.stage == "prepare" and arguments.recovery_boot is None:
                raise ValueError("install-rom prepare requires --recovery-boot")
            if arguments.stage == "sideload" and arguments.rom is None:
                raise ValueError("install-rom sideload requires --rom")
            research = load_research(arguments.research)
            validate_research(research)
            validate_install_rom_decisions(
                research,
                no_reboot=(research["decisions"]["reboot"] == "no") if arguments.stage == "prepare"
                          or (arguments.addon and arguments.package_index < len(arguments.addon)) else arguments.no_reboot,
            )
            result["research"] = research
            result["stage"] = arguments.stage
            selected_image = validate_local_file(
                arguments.recovery_boot if arguments.stage == "prepare" else arguments.rom,
                "ROM installation image")
            role = "recovery" if arguments.stage == "prepare" else "rom"
            selected_images = [(role, selected_image)]
            extra_images = parse_images(arguments.prepare_image) if arguments.prepare_image else []
            if arguments.stage == "sideload" and extra_images:
                raise ValueError("--prepare-image is only allowed in the prepare stage")
            selected_images += extra_images
            addons = [validate_local_file(path, "ROM add-on") for path in arguments.addon]
            selected_images += [("addon:" + str(index), path) for index, path in enumerate(addons, 1)]
            reviewed_addons = {key for key in research.get("flash_plan", {}).get("images", {})
                               if key.startswith("addon:")}
            if reviewed_addons != {"addon:" + str(index) for index in range(1, len(addons) + 1)}:
                raise ValueError("supplied add-ons must match every reviewed add-on in order")
            if not 0 <= arguments.package_index <= len(addons):
                raise ValueError("package-index must select the ROM (0) or a supplied add-on (1..N)")
            if arguments.stage == "prepare" and arguments.package_index != 0:
                raise ValueError("prepare does not select an add-on")
            if arguments.stage == "sideload" and addons and arguments.package_index < len(addons) and not arguments.no_reboot:
                raise ValueError("do not reboot before all add-ons are installed; pass --no-reboot")
            result["flash_checks"] = validate_flash_plan(
                research, selected_images, serial=arguments.serial,
                fastboot_serial=fastboot_serial, dry_run=arguments.dry_run)
            record_flash_stage(result, research, "awaiting_sideload" if arguments.stage == "prepare"
                               else "awaiting_addon" if arguments.package_index < len(addons)
                               else "awaiting_verification")
            result["package_index"] = arguments.package_index
            if arguments.stage == "prepare":
                partition = research.get("flash_plan", {}).get("recovery_partition")
                if partition is None and not arguments.dry_run:
                    raise ValueError("flash_plan requires the device-specific recovery_partition")
                manual = research.get("flash_plan", {}).get("recovery_steps")
                if not arguments.dry_run and (not isinstance(manual, list) or not manual
                        or not all(isinstance(step, str) and step.strip() for step in manual)):
                    raise ValueError("flash_plan requires sourced device-specific recovery_steps")
                recovery_boot = validate_local_file(
                    arguments.recovery_boot, "recovery boot image"
                )
                steps = validate_installation_steps(research.get("flash_plan", {}),
                    [(partition or "boot", recovery_boot), *extra_images], recovery_partition=partition or "boot")
                commands = [fastboot_command(arguments.fastboot, fastboot_serial, "flash", part, str(path))
                            for part, path, mode in steps]
                writes = []
                if not arguments.dry_run or (research.get("flash_plan") or {}).get("partition_writes"):
                    ordered_images = [("recovery" if part == partition else part, path) for part, path, mode in steps]
                    writes = reviewed_partition_writes(research["flash_plan"], ordered_images, recovery_partition=partition)
                    if any(rule["partition"] != part or rule["mode"] != mode
                           for (rule, _), (part, _, mode) in zip(writes, steps)):
                        raise ValueError("preparation commands differ from reviewed partition_writes")
                    commands = [reviewed_write_command(arguments.fastboot, fastboot_serial, rule, path)
                                for rule, path in writes]
                result["prepare_steps"] = [{"partition": part, "mode": mode} for part, path, mode in steps]
                result["plan"] = [list(command) for command in commands]
                if not arguments.dry_run:
                    result["source_checks"] = check_android_source(arguments, research, records, require_baseline=True)
                    validate_flash_plan(research, selected_images, serial=arguments.serial,
                                        fastboot_serial=fastboot_serial, dry_run=False)
                for index, ((part, path, mode), command) in enumerate(zip(steps, commands)):
                    if not arguments.dry_run:
                        step_research = {**research, "flash_plan": {**research["flash_plan"], "mode": mode}}
                        check_flash_device(arguments, step_research, fastboot_serial=fastboot_serial, records=records)
                        # Check every target available in this transport before the first
                        # write in the group, then recheck each immediate write target.
                        end = index + 1
                        if index == 0 or steps[index - 1][2] != mode:
                            while end < len(steps) and steps[end][2] == mode:
                                end += 1
                        for checked_index in range(index, end):
                            check_partition_slot(arguments, fastboot_serial, writes[checked_index][0], records,
                                                 image=writes[checked_index][1])
                        validate_flash_plan(research, selected_images, serial=arguments.serial,
                                            fastboot_serial=fastboot_serial, dry_run=False)
                        result["flash_checks"].update(device_checks="passed", execution_ready=True)
                    execute_plan([command], timeout=arguments.timeout, dry_run=arguments.dry_run, records=records)
                result["next_action"] = manual or ["Follow the device-specific guide to reset data and enter ADB sideload."]
            else:
                if not arguments.dry_run:
                    result["prepared_record_sha256"] = require_prepared_record(
                        arguments.prepared_record, research, arguments.serial, command="install-rom",
                        phase="awaiting_sideload" if arguments.package_index == 0 else "awaiting_addon",
                        package_index=None if arguments.package_index == 0 else arguments.package_index - 1)
                    result["flash_checks"]["preparation_checks"] = "passed"
                rom = selected_image if arguments.package_index == 0 else addons[arguments.package_index - 1]
                commands = install_rom_sideload_commands(
                    arguments.adb,
                    arguments.serial,
                    rom,
                    reboot=not arguments.no_reboot,
                )
                result["plan"] = [list(command) for command in commands]
                wait_for_adb_state(
                    arguments.adb,
                    arguments.serial,
                    "sideload",
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
                if not arguments.dry_run:
                    validate_flash_plan(research, selected_images, serial=arguments.serial,
                                        fastboot_serial=fastboot_serial, dry_run=False)
                    result["flash_checks"].update(device_checks="sideload_transport_confirmed", execution_ready=True)
                execute_plan(
                    commands,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
                if not arguments.no_reboot:
                    wait_for_boot(
                        arguments.adb,
                        arguments.serial,
                        timeout=arguments.timeout,
                        dry_run=arguments.dry_run,
                        records=records,
                    )
                if arguments.package_index < len(addons):
                    result["next_action"] = "Follow the guide to re-enter ADB sideload; submit the next package-index with this record as --prepared-record."
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "root-prepare":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("root-prepare requires --confirm")
            stock_boot = validate_local_file(
                arguments.stock_boot, "official stock boot image"
            )
            magisk_apk = validate_local_file(arguments.magisk_apk, "Magisk APK")
            research = load_research(arguments.research)
            validate_research(research)
            validate_root_intent(research)
            result["research"] = research
            commands = root_prepare_commands(
                arguments.adb,
                arguments.serial,
                stock_boot,
                magisk_apk,
                remote_boot=arguments.remote_boot,
            )
            result["plan"] = [list(command) for command in commands]
            ensure_android(
                arguments.adb,
                arguments.fastboot,
                arguments.serial,
                fastboot_serial,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            execute_plan(
                commands,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            result["remote_boot"] = arguments.remote_boot
            result["next_action"] = (
                "Stay beside the phone. Open Magisk, choose Install, then "
                "Select and Patch a File, and "
                "select boot.img in Download. Run root-collect after Magisk writes "
                "magisk_patched_*.img."
            )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "root-collect":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("root-collect requires --confirm")
            destination = arguments.destination.expanduser().resolve()
            if destination.exists():
                raise ValueError(f"patched boot output already exists: {destination}")
            research = load_research(arguments.research)
            validate_research(research)
            validate_root_intent(research)
            result["research"] = research
            result["destination"] = str(destination)
            ensure_android(
                arguments.adb,
                arguments.fastboot,
                arguments.serial,
                fastboot_serial,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            if arguments.dry_run:
                remote_patched_boot = (
                    arguments.remote_directory.rstrip("/")
                    + "/magisk_patched.img"
                )
            else:
                find_record = run_command(
                    adb_command(
                        arguments.adb,
                        arguments.serial,
                        "shell",
                        "find",
                        arguments.remote_directory,
                        "-maxdepth",
                        "1",
                        "-type",
                        "f",
                        "-name",
                        "magisk_patched_*.img",
                    ),
                    label="find Magisk patched boot",
                    timeout=min(arguments.timeout, 30),
                    records=records,
                )
                matches = [
                    line.strip()
                    for line in find_record.get("stdout", "").splitlines()
                    if line.strip()
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "expected exactly one magisk_patched_*.img in "
                        + arguments.remote_directory
                        + "; found "
                        + str(len(matches))
                    )
                remote_patched_boot = matches[0]
            commands = root_collect_commands(
                arguments.adb,
                arguments.serial,
                remote_patched_boot,
                destination,
            )
            result["remote_patched_boot"] = remote_patched_boot
            result["plan"] = [list(command) for command in commands]
            execute_plan(
                commands,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            result["patched_boot_sha256"] = (
                sha256_file(destination) if not arguments.dry_run else None
            )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "root":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("root requires --confirm")
            if arguments.research is None:
                raise ValueError("root requires --research")
            patched_boot = validate_local_file(
                arguments.patched_boot, "patched boot image"
            )
            research = load_research(arguments.research)
            validate_research(research)
            validate_root_decisions(
                research,
                no_reboot=arguments.no_reboot,
            )
            result["research"] = research
            magisk_apk = (
                validate_local_file(arguments.magisk_apk, "Magisk APK")
                if arguments.magisk_apk
                else None
            )
            magisk_commands = (
                install_magisk_commands(
                    arguments.adb,
                    arguments.serial,
                    magisk_apk,
                )
                if magisk_apk is not None
                else []
            )
            if magisk_commands:
                ensure_android(
                    arguments.adb,
                    arguments.fastboot,
                    arguments.serial,
                    fastboot_serial,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
                execute_plan(
                    magisk_commands,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
            ensure_fastboot(
                arguments.adb,
                arguments.fastboot,
                arguments.serial,
                fastboot_serial,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            slot = arguments.slot
            if slot == "current" and not arguments.dry_run:
                detected_slot = current_slot(
                    arguments.fastboot,
                    fastboot_serial,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
                if detected_slot is None:
                    raise RuntimeError("fastboot did not report current-slot")
                slot = detected_slot
            commands = root_commands(
                arguments.fastboot,
                fastboot_serial,
                patched_boot,
                partition=research["root_partition"],
                slot=slot,
                reboot=not arguments.no_reboot,
            )
            result["slot"] = slot
            result["plan"] = [
                list(command)
                for command in (
                    *magisk_commands,
                    *commands,
                )
            ]
            execute_plan(
                commands,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            if not arguments.no_reboot and not arguments.dry_run:
                wait_for_boot(
                    arguments.adb,
                    arguments.serial,
                    timeout=arguments.timeout,
                    dry_run=arguments.dry_run,
                    records=records,
                )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        elif arguments.command == "install-frida":
            if not arguments.confirm and not arguments.dry_run:
                raise ValueError("install-frida requires --confirm")
            server = validate_local_file(arguments.server, "Frida server binary")
            if arguments.research is None:
                raise ValueError("install-frida requires --research")
            research = load_research(arguments.research)
            validate_research(research)
            validate_frida_decisions(research)
            result["research"] = research
            ensure_android(
                arguments.adb,
                arguments.fastboot,
                arguments.serial,
                fastboot_serial,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            commands = install_frida_commands(
                arguments.adb,
                arguments.serial,
                server,
                arguments.remote_path,
                start=arguments.start,
            )
            result["plan"] = [list(command) for command in commands]
            execute_plan(
                commands,
                timeout=arguments.timeout,
                dry_run=arguments.dry_run,
                records=records,
            )
            result["status"] = "dry_run" if arguments.dry_run else "complete"
        else:
            raise ValueError(f"unsupported command: {arguments.command}")
    except (OSError, RuntimeError, ValueError) as error:
        result.update({"status": "blocked", "error": str(error)})
        write_result(arguments.output, result)
        print(f"blocked: {arguments.output}")
        return 2
    write_result(arguments.output, result)
    print(f"{result['status']}: {arguments.output}")
    return 0 if result["status"] in {"complete", "dry_run"} else 2


if __name__ == "__main__":
    sys.exit(main())
