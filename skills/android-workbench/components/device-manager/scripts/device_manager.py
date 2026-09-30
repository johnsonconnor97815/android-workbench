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
OPERATOR_PRESENCE_COMMANDS = {
    "bootloader-state",
    "partition-repair",
    "flash",
    "flash-factory",
    "install-rom",
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
    "scope": {"boot_only", "system_only", "full_image", "custom"},
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


def bounded_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if len(value) <= OUTPUT_LIMIT:
        return value
    return value[:OUTPUT_LIMIT] + "\n[truncated]"


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
                "stdout": bounded_text(error.stdout or ""),
                "stderr": bounded_text(error.stderr or ""),
                "error": f"{label} timed out after {timeout} seconds",
            }
        )
        if records is not None:
            records.append(record)
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
        raise RuntimeError(record["error"]) from error
    record.update(
        {
            "returncode": completed.returncode,
            "stdout": bounded_text(completed.stdout),
            "stderr": bounded_text(completed.stderr),
        }
    )
    if records is not None:
        records.append(record)
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
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
        return transport
    if "unauthorized" in command_output(adb_state).lower():
        return "unauthorized"
    return "unknown"


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
        if fastboot_transport(completed.stdout, fastboot_serial) == transport:
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
    decisions = research["decisions"]
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
) -> list[list[str]]:
    return [
        fastboot_command(
            fastboot_path, fastboot_serial, "flash", "boot", str(recovery_boot)
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
    wrapper.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(resolved_fastboot)
        + " -s "
        + shlex.quote(fastboot_serial)
        + " \"$@\"\n"
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
        [str(script)],
        label="official flash-all.sh",
        timeout=timeout,
        dry_run=dry_run,
        records=records,
        cwd=working_directory,
        env=environment,
    )


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
        battery_match = re.search(
            r"level:\s*(\d+)", battery_record.get("stdout", "")
        )
        if battery_match:
            result["observations"]["battery_level"] = int(battery_match.group(1))
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
    ensure_fastbootd(
        adb_path,
        fastboot_path,
        serial,
        fastboot_serial,
        timeout=timeout,
        dry_run=dry_run,
        records=records,
    )
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
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


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
    parser = build_parser()
    arguments = parser.parse_args()
    if arguments.output is None:
        parser.error("--output is required")
    if (
        arguments.command
        in {
            "preflight",
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
            delete_partitions = list(arguments.delete_partition or [])
            commands = [
                fastboot_command(
                    arguments.fastboot,
                    fastboot_serial,
                    "delete-logical-partition",
                    partition,
                )
                for partition in delete_partitions
            ]
            if not delete_partitions:
                raise ValueError("partition-repair requires at least one --delete-partition")
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
            commands = flash_commands(
                arguments.fastboot,
                fastboot_serial,
                images,
                reboot=not arguments.no_reboot,
            )
            result["plan"] = [list(command) for command in commands]
            ensure_fastboot(
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
            if not arguments.dry_run:
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
                no_reboot=arguments.no_reboot,
            )
            result["research"] = research
            result["stage"] = arguments.stage
            if arguments.stage == "prepare":
                recovery_boot = validate_local_file(
                    arguments.recovery_boot, "recovery boot image"
                )
                commands = install_rom_prepare_commands(
                    arguments.fastboot,
                    fastboot_serial,
                    recovery_boot,
                )
                result["plan"] = [list(command) for command in commands]
                ensure_fastboot(
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
                result["next_action"] = (
                    "Stay beside the phone. Use the fastboot menu to choose Recovery mode. "
                    "Then choose Factory reset, followed by Format data / factory "
                    "reset. Return to the main menu, choose Apply update, then "
                    "Apply from ADB. Run the install-rom sideload stage when the "
                    "phone is waiting in sideload mode."
                )
            else:
                rom = validate_local_file(arguments.rom, "custom ROM package")
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
