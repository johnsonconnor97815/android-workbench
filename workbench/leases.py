"""External local-tool leases keep long-lived MCP users visible to the scheduler."""

from .common import (
    resource,
    path_resource,
    same_process,
    WorkbenchError,
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
                return
            if not same_process(runner.spec["lease_owner"]):
                raise WorkbenchError(
                    "External tool owner died without releasing its process tree"
                )
            runner.sleep(0.2)
    except BaseException:
        # Worker lifetime and actual analysis-server lifetime are different.
        runner.uncertain = True
        raise
