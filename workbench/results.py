"""Adapter-specific result and recovery checks; never trust caller-supplied cleanup flags."""

import json
from pathlib import Path
import re
from .common import WorkbenchError, file_hash


def legacy_record(spec):
    name = spec.get("entry", {}).get("result_file")
    outputs = spec.get("outputs", [])
    if not name or not outputs:
        return None, None
    path = Path(outputs[0]) if name == "." else Path(outputs[0]) / name
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise WorkbenchError("Invalid adapter result")
    return path, data


def confirmed_legacy_cleanup(spec, data):
    # Registered adapters must opt in before their cleanup result is trusted.
    return (
        spec.get("confirms_cleanup") is True
        and data.get("cleanup_status") == "passed"
        and data.get("cleanup_errors") == []
    )


def readonly_package_query(spec):
    """Recognize only the bundled wrapper's exact, serial-bound pm path query."""
    entry = spec.get("entry", {})
    if (spec.get("operation") != "apkrev.devsh"
            or entry.get("script") != "@workbench/skills/android-workbench/components/apk-reverse/scripts/devsh.py"
            or entry.get("source") != "@workbench/skills/android-workbench/components/apk-reverse"):
        return False
    args = spec.get("args", [])
    return (len(args) == 4 and args[0] in {"sh", "shell"}
            and args[2:] == ["--serial", spec.get("serial")]
            and re.fullmatch(r"pm path [A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", args[1]) is not None)


def verify_package_query_recovery(runner, folder, spec):
    if not readonly_package_query(spec):
        return False
    # Older runtimes labelled all wrapper requests as mutations. Only recover
    # this bounded query when its reviewed wrapper and helper are unchanged.
    for name in ("scripts/devsh.py", "scripts/device_shell.py"):
        expected = spec.get("source_manifest", {}).get(name)
        current = Path(spec["source"]) / name
        snapshot = folder / "source" / name
        if (not expected or not current.is_file() or not snapshot.is_file()
                or file_hash(current) != expected or file_hash(snapshot) != expected):
            raise WorkbenchError("Package query recovery source changed")
    runner.call("event", kind="recovery_verified", data={
        "job": folder.name, "checks": ["bounded_readonly_package_query", "unchanged_wrapper_and_helper"],
    })
    return True


def verify_native_probe_recovery(runner, folder, spec):
    path, data = legacy_record(spec)
    if not data or not confirmed_legacy_cleanup(spec, data):
        return False
    artifacts = json.loads((folder / "artifacts.json").read_text())
    fingerprints = [
        f["sha256"]
        for out in artifacts.get("external_outputs", [])
        if isinstance(out, dict)
        for f in out.get("files", [])
        if f.get("path") == str(path) and "sha256" in f
    ]
    if fingerprints != [file_hash(path)]:
        raise WorkbenchError("Recovery record differs from the published artifact")
    identity = data.get("owned_identity")
    if identity:
        pid = int(identity["pid"])
        raw = runner.device.adb(
            "shell", f"if [ -r /proc/{pid}/stat ]; then cat /proc/{pid}/stat; fi"
        )
        if raw:
            start = raw.rsplit(")", 1)[1].split()[19]
            if start == str(identity["starttime"]):
                raise WorkbenchError("Owned native probe process is still alive")
    elif data.get("target_pid"):
        raise WorkbenchError("Probe never captured its process identity")
    endpoint = data.get("endpoint", "")
    if endpoint.startswith("127.0.0.1:"):
        port = int(endpoint.rsplit(":", 1)[1])
        forwards = runner.device.adb("forward", "--list")
        if any(
            line.split()[:2] == [runner.spec["serial"], "tcp:" + str(port)]
            for line in forwards.splitlines()
        ):
            raise WorkbenchError("Owned native probe forwarding is still active")
    runner.call(
        "event",
        kind="recovery_verified",
        data={
            "job": folder.name,
            "checks": [
                "published_record_hash",
                "owned_process_absent",
                "owned_forward_absent",
            ],
        },
    )
    return True
