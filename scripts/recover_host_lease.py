#!/usr/bin/env python3
"""Verify and reconcile released host leases and failed read-only recovery checks."""

# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == "__main__":
    import sys as _awb_sys
    from pathlib import Path as _AwbPath

    _awb_project = None
    for _awb_root in (_AwbPath.cwd(), *_AwbPath.cwd().parents):
        if (_awb_root / "workbench.project.json").is_file():
            _awb_project = _awb_root
            break
    if _awb_project is not None:
        import json as _awb_json

        _awb_config = _awb_json.loads(
            (_awb_project / "workbench.project.json").read_text()
        )
        _awb_runtime = (
            _awb_project / _awb_config.get("workbench_root", "android-workbench")
        ).resolve()
        if not (_awb_runtime / "workbench/bridge.py").is_file():
            raise SystemExit(
                "Android Workbench runtime missing; managed entry cannot run directly"
            )
        _awb_sys.path.insert(0, str(_awb_runtime))
        from workbench.bridge import entry as _awb_entry

        _awb_entry(__file__)
# END ANDROID WORKBENCH ENTRY


import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.common import same_process


def read_json(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)

    grant = read_json(Path(os.environ["AWB_INTERNAL_GRANT"]))
    result = {
        "pass": False,
        "cleanup_errors": [],
        "checked_jobs": [],
        "reconciled_without_release": [],
        "checks": [],
        "completedUtc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        targets = grant["spec"].get("recovery_targets", [])
        for identifier in targets:
            folder = Path(grant["state"]) / "jobs" / identifier
            normalized = read_json(folder / "normalized.json")
            if normalized.get("operation") not in {
                "host.lease",
                "project.recover_host_lease",
                "environment.recover_host_lease",
            }:
                raise RuntimeError(
                    "Manual review needed for recovery of "
                    + normalized.get("operation", "<unknown>")
                )
            if normalized.get("operation") == "host.lease":
                if normalized.get("readonly") is not True:
                    raise RuntimeError("Host lease was not read-only")
                if normalized.get("lease_released") is not True:
                    result["reconciled_without_release"].append(identifier)
                if any(r.get("mode") != "read" for r in normalized.get("resources", [])):
                    raise RuntimeError("Host lease reserved a writable resource")
                lease_owner = normalized.get("lease_owner")
                if same_process(lease_owner):
                    raise RuntimeError("Original lease owner is still alive")
                lease_process = normalized.get("lease_process")
                if same_process(lease_process):
                    raise RuntimeError("Tracked upstream process is still alive")
            else:
                old_result = read_json(folder / "result.json")
                if old_result.get("state") != "failed":
                    raise RuntimeError("Recovery check did not fail safely")
                errors = old_result.get("cleanup_errors") or []
                if errors == ["RuntimeError: No affected host lease was identified"]:
                    # Older duplicate checks failed before touching anything.
                    collection = read_json(folder / "host-lease-recovery/result.json")
                    if collection.get("pass") is not False or collection.get("checked_jobs") != []:
                        raise RuntimeError("Empty recovery check was not confirmed")
                elif errors != ["RuntimeError: Host lease was not explicitly released"]:
                    raise RuntimeError("Unexpected recovery-check failure")

            for name in ("active-process.json", "residual-processes.json"):
                path = folder / name
                if not path.is_file():
                    continue
                data = read_json(path)
                identities = data if isinstance(data, list) else [data.get("identity")]
                if any(same_process(x) for x in identities if x):
                    raise RuntimeError("Old host-lease process is still alive")

            result["checked_jobs"].append(identifier)
        result["checks"] = [
            "host_lease_read_only",
            "lease_explicitly_released_or_dead_owner_reconciled",
            "original_owner_absent",
            "tracked_upstream_absent",
            "old_processes_absent",
            "failed_recovery_check_was_safe",
        ] if targets else ["no_affected_leases"]
        result["pass"] = True
    except BaseException as error:
        result["cleanup_errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        result["completedUtc"] = datetime.now(timezone.utc).isoformat()
        (args.output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
