#!/usr/bin/env python3
"""Export an installed Android APK set, using only Python's standard library."""

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
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from android_tools import (
    ApkError,
    canonical_bytes,
    executable_record,
    inspect_apk_metadata,
    inspect_apk_signers,
    parse_adb_devices,
    parse_dumpsys_package,
    parse_pm_paths,
    require_package,
    resolve_sdk_tool,
    resolve_serial,
    run_command,
    sha256_bytes,
    sha256_file,
)

VERSION = "0.1.0"
SCHEMA_VERSION = "1"
APK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.apk\Z")


def emit(value: object, *, error: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def sdk_tools(args: argparse.Namespace, *, adb: bool = True) -> tuple[dict, dict]:
    """Check every required tool before touching the device or creating output."""
    paths, records = {}, {}
    for name in (["adb"] if adb else []) + ["aapt2", "apksigner"]:
        paths[name] = resolve_sdk_tool(name, getattr(args, name, None))
        records[name] = executable_record(paths[name], [paths[name], "version"])
    return paths, records


def file_record(path: Path, root: Path) -> dict:
    return {
        "file": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def save_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def commands(serial: str, package: str) -> dict[str, list[str]]:
    shell = ["-s", serial, "shell"]
    return {
        "devices": ["devices", "-l"],
        "properties": [*shell, "getprop"],
        "pathsBefore": [*shell, "pm", "path", package],
        "packageBefore": [*shell, "dumpsys", "package", package],
        "pathsAfter": [*shell, "pm", "path", package],
        "packageAfter": [*shell, "dumpsys", "package", package],
    }


def capture(adb: Path, argv: list[str], *, timeout: int = 120) -> dict:
    try:
        result = run_command([adb, *argv], check=False, timeout=timeout)
        return {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except ApkError as exc:
        return {"argv": argv, "returncode": None, "stdout": "", "stderr": str(exc)}


def snapshot(evidence: dict, suffix: str, package: str) -> dict | None:
    paths = evidence[f"paths{suffix}"]
    info = evidence[f"package{suffix}"]
    if paths["returncode"] != 0 or info["returncode"] != 0:
        return None
    try:
        identity = parse_dumpsys_package(info["stdout"], package)
        identity.pop("rawSha256")
        if identity["splitNames"] is not None:
            identity["splitNames"] = sorted(identity["splitNames"])
        return {"paths": sorted(parse_pm_paths(paths["stdout"])), "identity": identity}
    except ApkError:
        return None


def device_info(evidence: dict, serial: str) -> dict:
    props = dict(
        re.findall(r"(?m)^\[([^]]+)\]: \[(.*)]$", evidence["properties"]["stdout"])
    )
    return {
        "serial": serial,
        "model": props.get("ro.product.model"),
        "sdk": props.get("ro.build.version.sdk"),
        "release": props.get("ro.build.version.release"),
        "fingerprint": props.get("ro.build.fingerprint"),
        "abis": [
            item for item in props.get("ro.product.cpu.abilist", "").split(",") if item
        ],
    }


def inspect_apk(path: Path, tools: dict) -> dict:
    metadata, signers, errors = None, [], {}
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist().count("AndroidManifest.xml") != 1:
                raise ApkError("APK must contain exactly one AndroidManifest.xml")
    except (OSError, zipfile.BadZipFile, ApkError) as exc:
        errors["container"] = str(exc)
    try:
        metadata = inspect_apk_metadata(path, tools["aapt2"])
    except (ApkError, ValueError) as exc:
        errors["metadata"] = str(exc)
    try:
        signers = sorted(
            {
                item["certificateSha256"]
                for item in inspect_apk_signers(path, tools["apksigner"])["signers"]
            }
        )
    except ApkError as exc:
        errors["signature"] = str(exc)
    return {
        "metadata": metadata,
        "signerCertificateSha256": signers,
        "inspectionErrors": errors,
    }


def assess(
    package: str, serial: str, evidence: dict, artifacts: list[dict]
) -> tuple[list[dict], dict | None]:
    """Derive completeness from evidence, including on offline verification."""
    issues = []

    def issue(code: str, subject: str = "") -> None:
        issues.append({"code": code, "subject": subject})

    for name, record in evidence.items():
        if record["returncode"] != 0:
            issue("device-command-failed", name)
    try:
        selected = [
            d
            for d in parse_adb_devices(evidence["devices"]["stdout"])
            if d["serial"] == serial and d["state"] == "device"
        ]
        if len(selected) != 1:
            issue("device-not-online", serial)
    except ApkError:
        issue("invalid-device-list")
    facts = device_info(evidence, serial)
    if any(
        not facts[field] for field in ("model", "sdk", "release", "fingerprint", "abis")
    ):
        issue("device-facts-incomplete")
    before = snapshot(evidence, "Before", package)
    after = snapshot(evidence, "After", package)
    if before is None:
        issue("initial-package-snapshot-unavailable")
    if after is None:
        issue("final-package-snapshot-unavailable")
    if before is not None and after is not None and before != after:
        issue("package-changed-during-pull")
    remote_paths = [item["remotePath"] for item in artifacts]
    if len(remote_paths) != len(set(remote_paths)):
        issue("duplicate-artifact-path")
    if before is not None:
        for path in sorted(set(before["paths"]) - set(remote_paths)):
            issue("missing-apk", path)
        for path in sorted(set(remote_paths) - set(before["paths"])):
            issue("unexpected-apk", path)
    bases = [
        item
        for item in artifacts
        if item["metadata"] and item["metadata"]["splitName"] is None
    ]
    if len(bases) != 1:
        issue("base-apk-count", str(len(bases)))
    identity = None
    if len(bases) == 1:
        base = bases[0]
        identity = {
            k: base["metadata"][k]
            for k in ("packageName", "versionCode", "versionName")
        }
        identity["signerCertificateSha256"] = base["signerCertificateSha256"]
        if identity["packageName"] != package:
            issue("requested-package-mismatch")
    actual_splits = []
    for item in artifacts:
        if item["inspectionErrors"]:
            issue("apk-inspection-failed", item["file"])
        metadata = item["metadata"]
        if metadata:
            actual_splits.append(metadata["splitName"] or "base")
        if not metadata or not identity:
            continue
        fields = ["packageName", "versionCode"]
        # Configuration splits commonly omit versionName.
        if metadata["splitName"] is None or metadata["versionName"] is not None:
            fields.append("versionName")
        if any(metadata[field] != identity[field] for field in fields):
            issue("apk-package-version-mismatch", item["file"])
        if (
            not item["signerCertificateSha256"]
            or item["signerCertificateSha256"] != identity["signerCertificateSha256"]
        ):
            issue("apk-signer-mismatch", item["file"])
    if len(actual_splits) != len(set(actual_splits)):
        issue("duplicate-split-name")
    for label, current in (("before", before), ("after", after)):
        if current is None:
            continue
        pm = current["identity"]
        if pm["packageName"] != package or not pm["versionCode"]:
            issue("package-manager-identity-incomplete", label)
        if pm["splitNames"] is None:
            issue("package-manager-splits-unavailable", label)
        elif sorted(actual_splits) != pm["splitNames"]:
            issue("split-inventory-mismatch", label)
        if identity and any(
            pm[k] != identity[k] for k in ("packageName", "versionCode", "versionName")
        ):
            issue("package-manager-apk-mismatch", label)
    return issues, identity


def checksum_text(artifacts: list[dict]) -> str:
    return "".join(f"{item['sha256']}  {item['file']}\n" for item in artifacts)


def safe_file(root: Path, record: dict) -> Path:
    if not isinstance(record, dict):
        raise ApkError("Invalid file record")
    locator = record.get("file")
    if not isinstance(locator, str):
        raise ApkError("Invalid file locator")
    relative = PurePosixPath(locator)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != locator
        or "\\" in locator
    ):
        raise ApkError(f"Unsafe file locator: {locator!r}")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ApkError(f"Symlinked archive member: {locator}")
    if not path.is_file():
        raise ApkError(f"Missing archive member: {locator}")
    if (
        type(record.get("size")) is not int
        or path.stat().st_size != record["size"]
        or sha256_file(path) != record.get("sha256")
    ):
        raise ApkError(f"Hash or size mismatch: {locator}")
    return path


def verify_archive(root: Path, tools: dict) -> dict:
    root = root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ApkError("Archive must be a regular directory, not a symlink")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ApkError("manifest.json must not be a symlink")
    envelope = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict) or set(envelope) != {"content", "contentSha256"}:
        raise ApkError("Invalid manifest envelope")
    content = envelope["content"]
    if sha256_bytes(canonical_bytes(content)) != envelope["contentSha256"]:
        raise ApkError("Manifest content hash mismatch")
    if (
        not isinstance(content, dict)
        or content.get("schemaVersion") != SCHEMA_VERSION
        or content.get("kind") != "android-apk-export"
    ):
        raise ApkError("Unsupported manifest schema")
    package, serial = content["requestedPackage"], content["serial"]
    require_package(package)
    if not isinstance(serial, str) or not serial or any(c.isspace() for c in serial):
        raise ApkError("Invalid recorded serial")
    expected_commands = commands(serial, package)
    if set(content["evidence"]) != set(expected_commands):
        raise ApkError("Missing or unexpected command evidence")
    evidence = {}
    for key, record in content["evidence"].items():
        value = json.loads(safe_file(root, record).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "argv",
            "returncode",
            "stdout",
            "stderr",
        }:
            raise ApkError(f"Invalid command evidence: {key}")
        if (
            value["argv"] != expected_commands[key]
            or not isinstance(value["stdout"], str)
            or not isinstance(value["stderr"], str)
        ):
            raise ApkError(f"Command evidence does not match package/serial: {key}")
        if value["returncode"] is not None and type(value["returncode"]) is not int:
            raise ApkError(f"Invalid command status: {key}")
        evidence[key] = value
    if content["device"] != device_info(evidence, serial):
        raise ApkError("Device identity differs from raw evidence")
    artifacts = content["artifacts"]
    if not isinstance(artifacts, list):
        raise ApkError("Invalid APK inventory")
    names = []
    for item in artifacts:
        path = safe_file(root, item)
        name = item["file"]
        if not APK_NAME.fullmatch(name) or name in names:
            raise ApkError("Duplicate or invalid APK filename")
        names.append(name)
        if (
            not isinstance(item.get("remotePath"), str)
            or PurePosixPath(item["remotePath"]).name != name
        ):
            raise ApkError("APK filename differs from its source path")
        current = inspect_apk(path, tools)
        if (
            current["metadata"] != item["metadata"]
            or current["signerCertificateSha256"] != item["signerCertificateSha256"]
        ):
            raise ApkError(f"APK identity mismatch: {name}")
        if set(current["inspectionErrors"]) != set(item["inspectionErrors"]):
            raise ApkError(f"APK validation state mismatch: {name}")
    if sorted(p.name for p in root.glob("*.apk")) != sorted(names):
        raise ApkError("Unrecorded APK in archive")
    checksums = root / "SHA256SUMS"
    if checksums.is_symlink() or checksums.read_text(encoding="utf-8") != checksum_text(
        artifacts
    ):
        raise ApkError("SHA256SUMS does not match the manifest")
    issues, identity = assess(package, serial, evidence, artifacts)
    if issues != content["issues"] or identity != content["packageIdentity"]:
        raise ApkError(
            "Manifest completeness or package identity does not match evidence"
        )
    if content["status"] != ("incomplete" if issues else "complete"):
        raise ApkError("Manifest status does not match evidence")
    return content


def pull(args: argparse.Namespace) -> dict:
    tools, tool_records = sdk_tools(args)
    serial, _, device_list = resolve_serial(tools["adb"], args.serial)
    argv = commands(serial, args.package)
    evidence = {
        "devices": {
            "argv": argv["devices"],
            "returncode": 0,
            "stdout": device_list,
            "stderr": "",
        },
        "pathsBefore": capture(tools["adb"], argv["pathsBefore"]),
        "packageBefore": capture(tools["adb"], argv["packageBefore"]),
    }
    initial_paths = evidence["pathsBefore"]
    if (
        initial_paths["returncode"] in (0, 1)
        and not initial_paths["stdout"].strip()
        and not initial_paths["stderr"].strip()
    ):
        raise ApkError(f"Package {args.package} is not installed on {serial}")
    if initial_paths["returncode"] != 0:
        raise ApkError(f"Package Manager query failed: {initial_paths['stderr']}")
    remote_paths = sorted(parse_pm_paths(initial_paths["stdout"]))
    names = [PurePosixPath(path).name for path in remote_paths]
    if len(set(names)) != len(names) or any(
        not APK_NAME.fullmatch(name) for name in names
    ):
        raise ApkError("Package Manager returned duplicate or unsafe APK filenames")
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ApkError(
            f"Output already exists: {output}. Choose a new directory or use verify."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / f".{output.name}.apk-pull.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ApkError(
            f"Another export may be using {output}; lock exists: {lock}"
        ) from exc
    os.close(fd)
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
        (stage / "evidence").mkdir()
        evidence["properties"] = capture(tools["adb"], argv["properties"])
        artifacts, failures = [], []
        for index, (remote, name) in enumerate(zip(remote_paths, names), 1):
            print(f"Pulling {index}/{len(names)}: {name}", file=sys.stderr, flush=True)
            destination = stage / name
            last_error = ""
            for attempt in range(args.retries + 1):
                destination.unlink(missing_ok=True)
                try:
                    result = run_command(
                        [tools["adb"], "-s", serial, "pull", remote, destination],
                        check=False,
                        timeout=args.pull_timeout,
                    )
                    if (
                        result.returncode == 0
                        and destination.is_file()
                        and not destination.is_symlink()
                    ):
                        break
                    last_error = (
                        result.stderr or result.stdout
                    ).strip() or "adb pull produced no regular file"
                except ApkError as exc:
                    last_error = str(exc)
                if attempt < args.retries:
                    print(
                        f"Retrying {name} ({attempt + 1}/{args.retries})",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                destination.unlink(missing_ok=True)
                failures.append({"remotePath": remote, "error": last_error[-2000:]})
                continue
            artifacts.append(
                {
                    **file_record(destination, stage),
                    "remotePath": remote,
                }
            )
        evidence["pathsAfter"] = capture(tools["adb"], argv["pathsAfter"])
        evidence["packageAfter"] = capture(tools["adb"], argv["packageAfter"])
        captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # The device acquisition interval is complete; all following checks are local.
        if os.environ.get("AWB_INTERNAL_GRANT"):
            from workbench.bridge import device_acquisition_complete
            device_acquisition_complete()
        for artifact in artifacts:
            artifact.update(inspect_apk(stage / artifact["file"], tools))
        records = {}
        for key, value in evidence.items():
            path = stage / "evidence" / f"{key}.json"
            save_json(path, value)
            records[key] = file_record(path, stage)
        issues, identity = assess(args.package, serial, evidence, artifacts)
        content = {
            "schemaVersion": SCHEMA_VERSION,
            "kind": "android-apk-export",
            "toolVersion": VERSION,
            "capturedAt": captured_at,
            "requestedPackage": args.package,
            "serial": serial,
            "device": device_info(evidence, serial),
            "packageIdentity": identity,
            "tools": tool_records,
            "pythonVersion": platform.python_version(),
            "evidence": records,
            "artifacts": artifacts,
            "pullFailures": failures,
            "issues": issues,
            "status": "incomplete" if issues else "complete",
        }
        save_json(
            stage / "manifest.json",
            {
                "content": content,
                "contentSha256": sha256_bytes(canonical_bytes(content)),
            },
        )
        (stage / "SHA256SUMS").write_text(checksum_text(artifacts), encoding="utf-8")
        verify_archive(stage, tools)
        if output.exists() or output.is_symlink():
            raise ApkError(f"Output appeared during acquisition: {output}")
        stage.rename(output)
        stage = None
        return summary(content, output)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        lock.unlink(missing_ok=True)


def summary(content: dict, root: Path) -> dict:
    return {
        "status": content["status"],
        "directory": str(root.expanduser().absolute()),
        "manifest": str(root.expanduser().absolute() / "manifest.json"),
        "packageIdentity": content["packageIdentity"],
        "serial": content["serial"],
        "apkCount": len(content["artifacts"]),
        "totalBytes": sum(item["size"] for item in content["artifacts"]),
        "issues": content["issues"],
    }


def list_devices(args: argparse.Namespace) -> dict:
    adb = resolve_sdk_tool("adb", args.adb)
    devices = parse_adb_devices(run_command([adb, "devices", "-l"]).stdout)
    if args.package:
        for device in devices:
            device["packageInstalled"] = None
            if device["state"] != "device":
                continue
            result = capture(
                adb, ["-s", device["serial"], "shell", "pm", "path", args.package]
            )
            if (
                result["returncode"] in (0, 1)
                and not result["stdout"].strip()
                and not result["stderr"].strip()
            ):
                device["packageInstalled"] = False
            elif result["returncode"] == 0:
                if not result["stdout"].strip():
                    device["packageInstalled"] = False
                else:
                    try:
                        device["apkCount"] = len(parse_pm_paths(result["stdout"]))
                        device["packageInstalled"] = True
                    except ApkError as exc:
                        device["queryError"] = str(exc)
            else:
                device["queryError"] = result["stderr"] or "Package query failed"
    return {"devices": devices}


def bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected {minimum}..{maximum}")
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands_parser = parser.add_subparsers(dest="command", required=True)
    doctor = commands_parser.add_parser(
        "doctor", help="Check Python and SDK tools before acquisition"
    )
    devices = commands_parser.add_parser(
        "devices", help="List ADB devices; optionally locate an installed package"
    )
    devices.add_argument("--adb", type=Path)
    devices.add_argument("--package", type=require_package)
    pull_parser = commands_parser.add_parser(
        "pull", help="Pull the base and every installed split APK"
    )
    pull_parser.add_argument("--package", type=require_package, required=True)
    pull_parser.add_argument("--serial")
    pull_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="A new directory; existing outputs are never overwritten",
    )
    pull_parser.add_argument(
        "--retries",
        type=bounded_integer(0, 3),
        default=1,
        help="Extra attempts per failed APK pull, 0..3 (default 1)",
    )
    pull_parser.add_argument(
        "--pull-timeout",
        type=bounded_integer(1, 3600),
        default=300,
        help="Seconds per APK pull attempt (default 300)",
    )
    verify = commands_parser.add_parser(
        "verify", help="Verify a saved export without a device or ADB"
    )
    verify.add_argument("directory", type=Path)
    for subparser in (doctor, pull_parser, verify):
        if subparser is not verify:
            subparser.add_argument("--adb", type=Path)
        subparser.add_argument("--aapt2", type=Path)
        subparser.add_argument("--apksigner", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            _, records = sdk_tools(args)
            result = {
                "status": "ready",
                "toolVersion": VERSION,
                "pythonVersion": platform.python_version(),
                "tools": records,
            }
        elif args.command == "devices":
            result = list_devices(args)
        elif args.command == "pull":
            result = pull(args)
        else:
            tools, _ = sdk_tools(args, adb=False)
            result = summary(verify_archive(args.directory, tools), args.directory)
        emit(result)
        return 1 if result.get("status") == "incomplete" else 0
    except (
        ApkError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        argparse.ArgumentTypeError,
    ) as exc:
        emit({"status": "error", "error": str(exc)}, error=True)
        return 2
    except KeyboardInterrupt:
        emit(
            {
                "status": "interrupted",
                "error": "Export interrupted; no completed archive was published.",
            },
            error=True,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
