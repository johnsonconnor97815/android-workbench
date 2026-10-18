"""Adapter-specific result and recovery checks; never trust caller-supplied cleanup flags."""

import json
import importlib.util
from pathlib import Path
import re
from .common import WorkbenchError, file_hash


def verify_ota_prepare_handover(folder, spec):
    """Verify a published successful OTA preparation without contacting a phone."""
    folder = Path(folder)
    try:
        original = json.loads((folder / "normalized.json").read_text())
        outcome = json.loads((folder / "result.json").read_text())
        if original.get("operation") != "device_manager.install_ota":
            raise ValueError("handover requires an OTA preparation job")
        for key in ("device", "serial", "project"):
            if not spec.get(key) or original.get(key) != spec[key]:
                raise ValueError("OTA preparation " + key + " differs from handover")
        if (outcome.get("state") != "succeeded" or outcome.get("cleanup_ok") is not True
                or outcome.get("uncertain") is True):
            raise ValueError("OTA preparation did not finish successfully")
        outputs = original.get("outputs", [])
        if not outputs:
            raise ValueError("OTA preparation has no published output")
        path = Path(outputs[0])
        artifacts = json.loads((folder / "artifacts.json").read_text())
        hashes = [item.get("sha256") for output in artifacts.get("external_outputs", [])
                  for item in output.get("files", []) if item.get("path") == str(path)]
        actual = file_hash(path)
        if hashes != [actual]:
            raise ValueError("OTA preparation record differs from published hash")
        record = json.loads(path.read_text())
        if record.get("device") != spec["serial"]:
            raise ValueError("OTA preparation record belongs to another phone")
        if any(record.get(key) != value for key, value in {
            "status": "complete", "command": "install-ota", "stage": "prepare",
            "flash_phase": "awaiting_sideload",
        }.items()):
            raise ValueError("OTA preparation record has the wrong command, stage or phase")
        research = record["research"]
        plan = research["flash_plan"]
        if plan.get("serial") != spec["serial"] or plan.get("mode") != "recovery":
            raise ValueError("OTA preparation research has the wrong phone or mode")
        if research.get("decisions", {}).get("scope") != "ota":
            raise ValueError("OTA preparation research has the wrong scope")
        support_path = Path(__file__).resolve().parents[1] / "skills/android-workbench/components/device-manager/scripts/flash_support.py"
        module_spec = importlib.util.spec_from_file_location("awb_handover_flash_support", support_path)
        support = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(support)
        support.validate_device_review(research)
        return {"prepared_job": folder.name, "record_path": str(path),
                "record_sha256": actual, "research_sha256": support.review_binding(research)}
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        raise WorkbenchError("Invalid OTA manual return: " + str(error)) from error


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


def readonly_hal_query(spec):
    """Only bare lshal listing; no debug, options, redirection or shell expansion."""
    entry = spec.get("entry", {})
    if (spec.get("operation") != "apkrev.devsh"
            or entry.get("script") != "@workbench/skills/android-workbench/components/apk-reverse/scripts/devsh.py"
            or entry.get("source") != "@workbench/skills/android-workbench/components/apk-reverse"):
        return False
    serial = spec.get("serial")
    return bool(serial) and any(spec.get("args") == args for command in ("sh", "shell") for args in (
        ["--serial", serial, command, "lshal"], [command, "lshal", "--serial", serial]))


def readonly_property_query(spec):
    """One named Android property through the reviewed, serial-bound wrapper."""
    entry=spec.get('entry', {})
    if (spec.get('operation')!='apkrev.devsh'
            or entry.get('script')!='@workbench/skills/android-workbench/components/apk-reverse/scripts/devsh.py'
            or entry.get('source')!='@workbench/skills/android-workbench/components/apk-reverse'):
        return False
    args=spec.get('args', [])
    return (bool(spec.get('serial')) and len(args)==4 and args[0] in {'sh','shell'}
            and args[2:]==['--serial',spec['serial']]
            and re.fullmatch(r'getprop [A-Za-z0-9_.-]+',args[1]) is not None)


def verify_property_query_recovery(runner, folder, spec):
    if not readonly_property_query(spec):
        return False
    return verify_bounded_query_sources(runner, folder, spec, 'property')


def verify_package_query_recovery(runner, folder, spec):
    if not readonly_package_query(spec):
        return False
    return verify_bounded_query_sources(runner, folder, spec, "package")


def verify_hal_query_recovery(runner, folder, spec):
    if not readonly_hal_query(spec):
        return False
    return verify_bounded_query_sources(runner, folder, spec, "hal")


def verify_bounded_query_sources(runner, folder, spec, kind):
    # Older runtimes labelled all wrapper requests as mutations. Only recover
    # this bounded query when its reviewed wrapper and helper are unchanged.
    for name in ("scripts/devsh.py", "scripts/device_shell.py"):
        expected = spec.get("source_manifest", {}).get(name)
        current = Path(spec["source"]) / name
        snapshot = folder / "source" / name
        if (not expected or not current.is_file() or not snapshot.is_file()
                or file_hash(current) != expected or file_hash(snapshot) != expected):
            raise WorkbenchError(kind.capitalize() + " query recovery source changed")
    runner.call("event", kind="recovery_verified", data={
        "job": folder.name, "checks": ["bounded_readonly_" + kind + "_query", "unchanged_wrapper_and_helper"],
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
