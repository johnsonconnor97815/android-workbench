"""External local-tool leases keep long-lived MCP users visible to the scheduler."""

import json
from pathlib import Path
import time

from .common import (
    resource,
    path_resource,
    WorkbenchError,
    file_hash,
    SchedulerUnavailable,
    process_status,
    process_group_status,
    descendants,
)


def retry_lease_call(function, *args, **kwargs):
    """Retry transport failures only; callers must supply an idempotent operation."""
    for attempt in range(3):
        try:
            return function(*args, **kwargs)
        except SchedulerUnavailable:
            if attempt == 2:
                raise
            time.sleep(0.2)


def verify_lease_tree(spec):
    """A close checkpoint cannot excuse a currently live or unreadable tree."""
    tracked = spec.get("lease_process")
    if tracked is not None and process_group_status(tracked) != "dead":
        raise WorkbenchError("Upstream process tree is alive or unknown; stop it before release")
    owner = spec.get("lease_owner")
    owner_status = process_status(owner)
    if owner_status == "unknown" or (owner_status == "dead" and not spec.get("lease_tree_closed")):
        raise WorkbenchError("Lease owner is absent or unknown; supervised tree closure required")
    if owner_status == "alive" and descendants(owner["pid"], strict=True):
        raise WorkbenchError("Lease owner descendants are still alive")


def normalize_lease(spec, project, root):
    mode = spec.pop("lease_mode", "environment")
    owner = spec.pop("lease_owner", None)
    if process_status(owner) != "alive":
        raise WorkbenchError("Live lease owner required")
    spec["lease_owner"] = owner
    spec["readonly"] = True
    if mode == "environment":
        spec["resources"] = [
            resource(path_resource(root / p), "read")
            for p in project.get("mcp_environments", project.get("environments", []))
        ]
    elif mode == "analysis":
        # Serialize mutation of the shared analysis workspace. Independent project roots remain parallel.
        spec["resources"] = [resource("analysis:" + str(root))]
        for p in spec.pop("analysis_paths", []):
            spec["resources"].append(resource(path_resource(p)))
    else:
        raise WorkbenchError("Unknown external-tool lease mode")
    spec["lease_mode"] = mode
    return spec


def execute_lease(runner):
    try:
        while True:
            runner.authorize()
            state = runner.call("lease_state")
            if state.get("released"):
                # The service separately confirmed physical cleanup. A missing
                # or invalid call receipt must not turn that into a stale lock.
                runner.uncertain = False
                receipt = state.get("mcp_receipt")
                if receipt is None:
                    if state.get("mcp_expected"):
                        runner.analysis_status = {"mcp_call_outcome": "incomplete",
                                                  "semantic_review_required": True}
                        return "partial"
                    return None  # Ordinary environment/analysis leases retain their behavior.
                path = Path(receipt["path"])
                if (path != runner.directory / "mcp-call.json" or not path.is_file()
                        or path.stat().st_size != receipt["bytes"] or file_hash(path) != receipt["sha256"]):
                    raise WorkbenchError("MCP call receipt changed before publication")
                data = json.loads(path.read_text())
                raw = data["retained_response"]
                raw_path = Path(raw["path"])
                if (raw_path != runner.directory / "mcp-response.bin" or not raw_path.is_file()
                        or raw_path.stat().st_size != raw["bytes"] or file_hash(raw_path) != raw["sha256"]):
                    raise WorkbenchError("MCP retained response changed before publication")
                runner.external_call = {"tool_name": receipt["tool_name"],
                                        "request_identity_sha256": receipt["request_identity_sha256"],
                                        "provider": receipt["provider"], "identity_scope": "proxy_reported",
                                        "response": data["response"]}
                runner.analysis_status = {"mcp_call_outcome": data["response"]["outcome"],
                                          "response_truncated": data["response"]["truncated"],
                                          "semantic_review_required": True}
                return receipt["outcome"]
            owner_status = process_status(runner.spec["lease_owner"])
            if owner_status == "unknown":
                raise WorkbenchError("External tool owner state is unknown; process tree remains unconfirmed")
            if owner_status == "dead":
                raise WorkbenchError(
                    "External tool owner died without releasing its process tree"
                )
            runner.sleep(0.2)
    except BaseException:
        # Worker lifetime and actual analysis-server lifetime are different.
        # A release may have reached the service while the worker's read timed
        # out. Its durable close checkpoint is independent of RPC availability.
        try:
            latest = json.loads((runner.directory / "normalized.json").read_text())
            runner.uncertain = not (latest.get("lease_released") is True
                                    and latest.get("lease_tree_closed") is True)
        except (OSError, ValueError):
            runner.uncertain = True
        raise
