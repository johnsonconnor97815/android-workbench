"""External local-tool leases keep long-lived MCP users visible to the scheduler."""

import json
from pathlib import Path

from .common import (
    resource,
    path_resource,
    same_process,
    WorkbenchError,
    file_hash,
)


def normalize_lease(spec, project, root):
    mode = spec.pop("lease_mode", "environment")
    owner = spec.pop("lease_owner", None)
    if not owner or not same_process(owner):
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
                receipt = state.get("mcp_receipt")
                if receipt is None:
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
            if not same_process(runner.spec["lease_owner"]):
                raise WorkbenchError(
                    "External tool owner died without releasing its process tree"
                )
            runner.sleep(0.2)
    except BaseException:
        # Worker lifetime and actual analysis-server lifetime are different.
        runner.uncertain = True
        raise
