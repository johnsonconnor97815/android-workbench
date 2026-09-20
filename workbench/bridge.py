"""Compatibility gate for existing script entrypoints; no raw fallback in managed projects."""

import json
import os
from pathlib import Path
import sys
from .client import Client
from .common import WorkbenchError, rpc
from .project import bundle_root, registered_path


def locate_project(script):
    for origin in (Path.cwd(),):
        for root in (origin, *origin.parents):
            if (root / "workbench.project.json").is_file():
                return root
    return None


def entry(script, argv=None):
    script = Path(script).resolve()
    project = locate_project(script)
    if project is None:
        return  # Original standalone behavior outside registered workspaces.
    grant_path = os.environ.get("AWB_INTERNAL_GRANT")
    if grant_path:
        grant = json.loads(Path(grant_path).read_text())
        rpc(
            grant["state"],
            "worker.validate_entry",
            {"id": grant["job"], "pid": os.getpid(), "script": str(script)},
            grant["token"],
        )
        return
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--help"], ["-h"]) or (
        len(argv) == 2 and argv[-1] in ("--help", "-h")
    ):
        return
    manifest = json.loads((project / "workbench.project.json").read_text())
    # Installed or legacy copies route to the explicitly registered bundled source.
    for name, location in manifest.get("skills", {}).items():
        canonical = registered_path(project, manifest, location)
        if script.parent.name == "scripts" and script.parent.parent.name == name:
            candidate = canonical / "scripts" / script.name
            if candidate.is_file():
                script = candidate.resolve()
    matches = []
    for name, definition in manifest["operations"].items():
        if registered_path(project, manifest, definition["script"]) == script:
            if definition.get("subcommand") and (
                not argv or argv[0] != definition["subcommand"]
            ):
                continue
            matches.append((name, definition))
    if len(matches) != 1:
        raise WorkbenchError(
            "Entry has no unique registered adapter; use the workbench operation catalog"
        )
    operation, definition = matches[0]
    device = None
    flag = definition.get("serial")
    if isinstance(flag, int):
        if len(argv) > flag:
            device = argv[flag]
    elif isinstance(flag, str) and flag.startswith("fixed:"):
        device = flag[6:]
    elif flag:
        for i, value in enumerate(argv):
            if value == flag and i + 1 < len(argv):
                device = argv[i + 1]
            elif value.startswith(flag + "="):
                device = value.split("=", 1)[1]
    if flag is not None and not device:
        raise WorkbenchError("Managed device operations require an explicit serial")
    spec = {"operation": operation, "args": argv, "timeout": 3600}
    if device:
        spec["device"] = device
    client = Client(project)
    job = client.submit(**spec)
    print("Android Workbench job: " + job["id"], file=sys.stderr, flush=True)
    result = client.wait(job["id"])
    for name, stream in [("stdout.log", sys.stdout), ("stderr.log", sys.stderr)]:
        path = Path(result["directory"]) / name
        if path.is_file():
            stream.write(path.read_text(errors="replace"))
    print(
        json.dumps(
            {
                "job": job["id"],
                "state": result["state"],
                "result": result.get("result"),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    code = (result.get("result") or {}).get("exit_code")
    raise SystemExit(
        (code or 1) if result["state"] not in ("succeeded", "partial") else (code or 0)
    )


def device_acquisition_complete():
    """APK adapter calls once, after the final device read and before local validation."""
    grant_path = os.environ.get("AWB_INTERNAL_GRANT")
    if not grant_path:
        return
    grant = json.loads(Path(grant_path).read_text())
    params = {"id": grant["job"]}
    rpc(grant["state"], "worker.release_device_request", params, grant["token"])
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = rpc(
            grant["state"], "worker.release_device_state", params, grant["token"]
        )
        if state["released"]:
            return
        time.sleep(0.05)
    raise WorkbenchError("Device phase release was not acknowledged")


def managed_mcp_spec(project, spec):
    """Keep generated MCP entries on the stable installed-runtime connector."""
    root = Path(project).resolve()
    if not (root / "workbench.project.json").is_file():
        return spec
    manifest = json.loads((root / "workbench.project.json").read_text())
    connector = bundle_root(root, manifest) / "scripts/connect.py"
    command = spec["command"]
    args = list(spec.get("args", []))
    if "mcp-proxy" in args:
        index = args.index("mcp-proxy")
        if args[index + 1 : index + 2] != ["--"] or len(args) < index + 3:
            raise WorkbenchError("Malformed managed MCP command")
        command, args = args[index + 2], args[index + 3 :]
    return {
        **spec,
        "command": "/usr/bin/python3",
        "args": [
            str(connector),
            "--project",
            str(root),
            "mcp-proxy",
            "--",
            command,
            *args,
        ],
    }
