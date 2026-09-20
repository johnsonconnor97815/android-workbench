"""Android SDK tool discovery, command execution, and APK metadata parsing.

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SPLIT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ApkError(RuntimeError):
    """An acquisition, verification, or environment failure."""


def resolve_sdk_tool(name: str, explicit: Path | None = None) -> Path:
    """Find SDK tools without requiring a project-specific shell environment."""
    override = explicit or os.environ.get(name.upper())
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ApkError(f"{name} is not executable: {candidate}")
        return candidate
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered).resolve()
    roots = [
        Path(value).expanduser()
        for value in (
            os.environ.get("ANDROID_HOME"),
            os.environ.get("ANDROID_SDK_ROOT"),
        )
        if value
    ]
    roots += [Path.home() / "Android/Sdk", Path.home() / "Library/Android/sdk"]
    for root in roots:
        candidates = (
            [root / "platform-tools/adb"]
            if name == "adb"
            else sorted(
                (root / "build-tools").glob(f"*/{name}"), key=version_key, reverse=True
            )
        )
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    raise ApkError(
        f"Cannot find {name}. Install Android SDK Platform-Tools (adb) and "
        f"Build-Tools (aapt2, apksigner), set ANDROID_HOME, or pass --{name}. "
        "apksigner also requires Java."
    )


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    argv: Sequence[str | Path],
    *,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in argv]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ApkError(f"required executable was not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ApkError(f"command timed out after {timeout}s: {command[0]}") from exc
    except OSError as exc:
        raise ApkError(f"could not launch {command[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ApkError(
            f"command failed ({result.returncode}): {command[0]}"
            + (f": {detail[-2000:]}" if detail else "")
        )
    return result


def executable_record(path: Path, version_argv: Sequence[str | Path]) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        usable = resolved.is_file() and os.access(resolved, os.X_OK)
    except OSError as exc:
        raise ApkError(f"cannot inspect required executable {path}: {exc}") from exc
    if not usable:
        raise ApkError(f"required executable is missing or not executable: {path}")
    version = run_command(version_argv).stdout.strip()
    if not version:
        version = run_command(version_argv).stderr.strip()
    if not version:
        raise ApkError(f"empty version output from {resolved}")
    return {
        "resolvedPath": str(resolved),
        "sha256": sha256_file(resolved),
        "version": version.splitlines()[0],
    }


def version_key(path: Path) -> tuple[object, ...]:
    values: list[object] = []
    for part in re.split(r"([0-9]+)", path.parent.name):
        values.append(part.zfill(20) if part.isdigit() else part)
    return tuple(values)


def require_package(value: str) -> str:
    if not PACKAGE_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a full Android package ID")
    return value


def valid_remote_path(path: str) -> bool:
    if not path or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in path
    ):
        return False
    if not path.startswith("/") or path.startswith("//"):
        return False
    normalized = PurePosixPath(path)
    return (
        normalized.is_absolute()
        and normalized != PurePosixPath("/")
        and ".." not in normalized.parts
        and str(normalized) == path
    )


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    lines = output.splitlines()
    if not lines or lines[0].strip() != "List of devices attached":
        raise ApkError("unexpected `adb devices -l` output")
    devices: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip() or line.startswith("*"):
            continue
        fields = line.split()
        if len(fields) < 2:
            raise ApkError(f"malformed adb device row: {line!r}")
        item = {"serial": fields[0], "state": fields[1]}
        for field in fields[2:]:
            if ":" in field:
                key, value = field.split(":", 1)
                item[key] = value
        devices.append(item)
    return devices


def resolve_serial(
    adb: Path,
    requested: str | None,
) -> tuple[str, dict[str, str], str]:
    devices_output = run_command([adb, "devices", "-l"]).stdout
    devices = parse_adb_devices(devices_output)
    if requested is not None:
        matches = [item for item in devices if item["serial"] == requested]
        if not matches:
            raise ApkError(f"device {requested!r} is not listed by adb")
        if matches[0]["state"] != "device":
            raise ApkError(
                f"device {requested!r} is {matches[0]['state']}, not online and authorized"
            )
        return requested, matches[0], devices_output
    online = [item for item in devices if item["state"] == "device"]
    if not online:
        raise ApkError("no online authorized Android device")
    if len(online) != 1:
        serials = ", ".join(item["serial"] for item in online)
        raise ApkError(f"multiple devices are online ({serials}); pass --serial")
    return online[0]["serial"], online[0], devices_output


def parse_pm_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        if not line.startswith("package:"):
            raise ApkError(f"unexpected `pm path` output line: {line!r}")
        path = line[len("package:") :]
        if not valid_remote_path(path):
            raise ApkError(f"invalid installed package path: {path!r}")
        if path in paths:
            raise ApkError(f"duplicate installed package path: {path}")
        paths.append(path)
    if not paths:
        raise ApkError("Package Manager returned no installed APK paths")
    return paths


def parse_dumpsys_package(output: str, _expected_package: str) -> dict[str, Any]:
    package_match = re.search(r"(?m)^\s*Package \[([^]]+)]", output)
    package_name = package_match.group(1) if package_match else None
    version_code_match = re.search(r"(?m)^\s*versionCode=(\d+)(?:\s|$)", output)
    version_name_match = re.search(r"(?m)^\s*versionName=(.*)$", output)
    split_match = re.search(r"(?m)^\s*(?:splits|splitNames)=\[([^]]*)]\s*$", output)
    split_names: list[str] | None = None
    if split_match:
        split_names = [
            item.strip() for item in split_match.group(1).split(",") if item.strip()
        ]
        if "base" not in split_names:
            split_names.insert(0, "base")
        if len(split_names) != len(set(split_names)):
            raise ApkError("Package Manager split inventory contains duplicates")
    version_name = version_name_match.group(1).strip() if version_name_match else None
    if version_name is not None and version_name.casefold() == "null":
        version_name = None
    return {
        "packageName": package_name,
        "versionCode": version_code_match.group(1) if version_code_match else None,
        "versionName": version_name,
        "splitNames": split_names,
        "rawSha256": sha256_bytes(output.encode("utf-8")),
    }


def parse_apk_metadata(output: str) -> dict[str, str | None]:
    package_line = next(
        (line for line in output.splitlines() if line.startswith("package: ")),
        None,
    )
    if package_line is None:
        raise ApkError("aapt2 did not emit a package identity")
    fields: dict[str, str] = {}
    for token in shlex.split(package_line[len("package: ") :]):
        if "=" in token:
            name, value = token.split("=", 1)
            fields[name] = value
    package_name = fields.get("name")
    version_code = fields.get("versionCode")
    if not package_name or not PACKAGE_PATTERN.fullmatch(package_name):
        raise ApkError("APK package name is missing or invalid")
    if not version_code or not version_code.isdigit():
        raise ApkError("APK versionCode is missing or invalid")
    split_name = fields.get("split")
    if split_name is not None and not SPLIT_PATTERN.fullmatch(split_name):
        raise ApkError("APK split name is invalid")
    version_name = fields.get("versionName")
    if version_name == "":
        version_name = None
    return {
        "packageName": package_name,
        "versionCode": version_code,
        "versionName": version_name,
        "splitName": split_name,
    }


def parse_signers(output: str) -> list[dict[str, Any]]:
    signer_count_match = re.search(r"(?m)^Number of signers: (\d+)$", output)
    expected_count: int | None = None
    if signer_count_match:
        try:
            expected_count = int(signer_count_match.group(1))
        except ValueError as exc:
            raise ApkError("invalid apksigner signer count") from exc
    prefix = r"(?P<prefix>Signer #\d+|V\d+(?:\.\d+)? Signer(?: #\d+)?)"
    patterns = {
        "certificateDn": re.compile(rf"^{prefix}:? certificate DN: (.*)$"),
        "certificateSha256": re.compile(
            rf"^{prefix}:? certificate SHA-256 digest: ([0-9A-Fa-f:]+)$"
        ),
        "publicKeySha256": re.compile(
            rf"^{prefix}:? public key SHA-256 digest: ([0-9A-Fa-f:]+)$"
        ),
    }
    grouped: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        for field, pattern in patterns.items():
            match = pattern.fullmatch(stripped)
            if not match:
                continue
            signer_prefix = match.group("prefix")
            value = match.group(2)
            if field.endswith("Sha256"):
                value = value.replace(":", "").lower()
                if not SHA256_PATTERN.fullmatch(value):
                    raise ApkError(f"invalid signer SHA-256 digest: {value}")
            grouped.setdefault(signer_prefix, {})[field] = value
    values = [
        {"ordinal": ordinal, **grouped[key]}
        for ordinal, key in enumerate(grouped, start=1)
    ]
    if (
        not values
        or any("certificateSha256" not in item for item in values)
        or (expected_count is not None and len(values) != expected_count)
    ):
        raise ApkError("apksigner did not emit a complete signer identity")
    return values


def inspect_apk_metadata(path: Path, aapt2: Path) -> dict[str, str | None]:
    return parse_apk_metadata(run_command([aapt2, "dump", "badging", path]).stdout)


def inspect_apk_signers(path: Path, apksigner: Path) -> dict[str, Any]:
    signing = run_command(
        [apksigner, "verify", "--verbose", "--print-certs", path],
        check=False,
    )
    if signing.returncode != 0:
        detail = (signing.stderr or signing.stdout).strip()
        raise ApkError(
            "APK signature verification failed"
            + (f": {detail[-1000:]}" if detail else "")
        )
    signer_output = signing.stdout + signing.stderr
    return {
        "signers": parse_signers(signer_output),
        "signerEvidenceSha256": sha256_bytes(signer_output.encode("utf-8")),
    }
