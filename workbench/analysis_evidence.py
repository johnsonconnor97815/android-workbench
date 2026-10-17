"""Content identity for managed results; execution metadata is not semantic proof."""

from pathlib import Path
import json
import os
import re
import shutil

from .common import digest, file_hash, WorkbenchError

SCHEMA = "workbench.analysis-evidence/v1"


def executable_identity(command, env=None, cwd=None):
    """Record the directly launched executable without running version commands."""
    raw = str(command)
    if "/" in raw:
        candidate = Path(raw)
        resolved = str((Path(cwd or Path.cwd()) / candidate).resolve())
    else:
        search_path = (env.get("PATH", os.defpath) if env is not None
                       else os.environ.get("PATH", os.defpath))
        # subprocess resolves relative PATH entries against the child's cwd.
        child_cwd = Path(cwd or Path.cwd()).resolve()
        search_path = os.pathsep.join(
            str((child_cwd / item).resolve()) if not Path(item).is_absolute() else item
            for item in search_path.split(os.pathsep)
        )
        resolved = shutil.which(raw, path=search_path)
    if not resolved:
        return {"command": str(command), "status": "unknown", "version": None}
    path = Path(resolved).resolve()
    try:
        before = path.stat()
        hashed = file_hash(path)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise OSError("Executable changed during identity capture")
        return {"command": str(command), "path": str(path), "sha256": hashed,
                "status": "captured_before_launch", "version": None}
    except OSError:
        return {"command": str(command), "path": str(path), "status": "unknown", "version": None}


def _credential_key(key):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).casefold()
    return (name in {"apikey", "accesstoken", "sessiontoken"}
            or bool(set(re.split(r"[_-]+", name))
                    & {"token", "secret", "password", "passwd", "credential", "credentials",
                       "auth", "authorization", "cookie", "cookies", "key"}))


def _parameter_identity(spec):
    arguments = []
    secret_value = False
    for arg in spec.get("args", []):
        if secret_value:
            arguments.append("[credential omitted]")
            secret_value = False
        elif str(arg).startswith("--") and _credential_key(str(arg).split("=", 1)[0].lstrip("-")):
            if "=" in str(arg):
                arguments.append(str(arg).split("=", 1)[0] + "=[credential omitted]")
            else:
                arguments.append(arg)
                secret_value = True
        else:
            arguments.append(arg)
    return {"operation": spec["operation"], "args": arguments,
            "steps": spec.get("steps", []),
            "env": {key: value for key, value in spec.get("env", {}).items() if not _credential_key(key)},
            "timeout": spec.get("timeout"), "limits": spec.get("limits", {})}


def read_analysis_metadata(spec, stdout):
    """Keep small adapter-declared coverage fields separate from process success."""
    arguments = spec.get("args", [])
    outputs = [
        arg.split("=", 1)[1] if "=" in arg else arguments[index + 1]
        for index, arg in enumerate(arguments)
        if arg.split("=", 1)[0] == "--output" and ("=" in arg or index + 1 < len(arguments))
    ]
    path = (Path(spec["project"]) / outputs[0]).resolve() if outputs else Path(stdout)
    result = {"metadata_scope": "adapter_reported", "metadata_source": str(path)}
    try:
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Adapter metadata exceeds the 4 MiB parse budget")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Adapter metadata is not an object")
        for key in ("schema", "kind", "status", "semantic_complete", "whole_apk_complete", "exhaustive", "truncated"):
            field = value.get(key)
            if isinstance(field, bool) or isinstance(field, str) and len(field) <= 128:
                result[key] = field
    except (ValueError, OSError) as exc:
        result.update(metadata_status="unknown", metadata_reason=str(exc))
    return result


def make_evidence(spec, result, artifacts, tools=()):
    """Retain compatibility: this sidecar supplements existing result/artifacts files."""
    inputs = [{"path": path, "sha256": value}
              for path, value in sorted(spec.get("input_fingerprints", {}).items())]
    adapter = {"script": spec.get("script"), "sha256": spec.get("source_hash"),
               "source_manifest_sha256": digest(spec.get("source_manifest", {})),
               "nested_source_manifests_sha256": digest(spec.get("nested_source_manifests", {}))}
    # Credential values are omitted even from hashes. This is a declared-input
    # identity, not a claim to capture the complete process environment.
    parameters = _parameter_identity(spec)
    configuration = {"sha256": digest(parameters), "environment_keys": sorted(spec.get("env", {}))}
    identity = {"inputs": inputs, "adapter": adapter, "configuration": configuration,
                "direct_executables": list(tools)}
    if result.get("external_call"):
        identity["external_call"] = result["external_call"]
    outputs = [{"path": str(Path(spec["directory"]) / item["name"]),
                "sha256": item["sha256"], "bytes": item.get("bytes")}
               for item in artifacts.get("files", []) if item.get("sha256")]
    outputs += [{"path": item["path"], "sha256": item["sha256"], "bytes": item.get("bytes")}
                for root in artifacts.get("external_outputs", [])
                for item in root.get("files", []) if item.get("sha256")]
    state = result["state"]
    value = {
        "schema": SCHEMA,
        "execution_identity": identity,
        "execution_identity_sha256": digest(identity),
        "job_id": artifacts["job"], "operation": spec["operation"],
        "outcome": {key: result.get(key) for key in (
            "state", "functional_result", "exit_code", "cleanup_ok", "uncertain", "reason")},
        "coverage": {"execution": state, "analysis_status": result.get("analysis_status", {}),
                     "semantic_review": "required", "external_engine_identity": "unknown"},
        "raw_results": outputs,
        "partial_observation": any(item.get("bytes", 0) > 0 for item in outputs) and state != "succeeded",
        "cache_reusable": False,
        "limitations": [
            "Identity covers declared inputs, adapter code and directly launched executables only.",
            "Child engine versions and undeclared environment dependencies are not established.",
            "Credential values are excluded; environment identity is incomplete.",
            "Successful execution, file integrity and retained partial output do not prove behavior.",
            "Partial observation means retained raw output, which may be diagnostics.",
            "Nested MCP calls are covered by parent output only; individual call bindings are unavailable.",
        ],
    }
    value["evidence_id"] = "ev_" + digest(value)
    return value


def verify_evidence(value):
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise WorkbenchError("Unsupported analysis evidence schema")
    body = {key: item for key, item in value.items() if key != "evidence_id"}
    if value.get("evidence_id") != "ev_" + digest(body):
        raise WorkbenchError("Analysis evidence content identity differs")
    return value
