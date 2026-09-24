"""Server-side operation definitions; callers cannot downgrade resource effects."""

from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import shutil
import sys
from .common import WorkbenchError, atomic_json, file_hash, path_resource, resource

from .project import registered_path

ROOT = Path(__file__).resolve().parents[1]
SCENE_STEPS = {
    "observe",
    "screenshot",
    "ui_dump",
    "logcat",
    "launch",
    "tap",
    "swipe",
    "input",
    "keyevent",
    "wait",
    "checkpoint",
    "hook_attach",
    "hook_unload",
}


def bounded(value, low, high, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not low <= value <= high
    ):
        raise WorkbenchError(f"{name} must be between {low} and {high}")
    return value


def load_project(project):
    root = Path(project).expanduser().resolve()
    config = root / "workbench.project.json"
    if not config.is_file():
        raise WorkbenchError(f"Project is not registered: {config}")
    doc = json.loads(config.read_text())
    if doc.get("version") != 1:
        raise WorkbenchError("Unsupported project manifest")
    return root, doc


def within(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise WorkbenchError("Registered path escapes project")
    return path


def device_id(config, serial):
    devices = config.get("devices", {})
    for identifier, entry in devices.items():
        if serial in (identifier, entry["serial"]):
            return identifier, entry
    raise WorkbenchError("Device is not registered; use devices.register first")


def snapshot_tree(source, target):
    """Copy executable sources, not samples, caches or live tool environments."""
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file() or any(
            p in {".git", "__pycache__", "node_modules", ".venv"}
            for p in path.relative_to(source).parts
        ):
            continue
        if path.suffix not in {
            ".py",
            ".sh",
            ".js",
            ".json",
            ".patch",
            ".c",
            ".h",
            ".toml",
            ".txt",
        }:
            continue
        if path.stat().st_size > 16 * 1024 * 1024:
            raise WorkbenchError(f"Code snapshot file too large: {path}")
        relative = path.relative_to(source)
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        hashes[str(relative)] = file_hash(dest)
    return hashes


def normalize(request, config, directory):
    spec = copy.deepcopy(request)
    if not isinstance(spec, dict):
        raise WorkbenchError("Request must be an object")
    allowed = {
        "project",
        "operation",
        "device",
        "args",
        "steps",
        "timeout",
        "queue_timeout",
        "estimate",
        "priority",
        "dependencies",
        "purpose",
        "request_key",
        "app",
        "activity",
        "scene",
        "accept_hooks",
        "baseline",
        "expected_boot",
        "not_before",
        "lease_owner",
        "lease_mode",
        "analysis_paths",
        "try_only",
        "ui_expect",
    }
    if config.get("test_mode"):
        allowed.update({"test_readonly", "test_insertable", "test_resources", "hooks"})
    unknown = set(spec) - allowed
    if unknown:
        raise WorkbenchError(
            "Unknown or adapter-owned request fields: " + ",".join(sorted(unknown))
        )
    root, project = load_project(spec["project"])
    operation = spec.get("operation")
    spec["project"] = str(root)
    spec["timeout"] = bounded(spec.get("timeout", 600), 0.1, 86400, "timeout")
    spec["queue_timeout"] = bounded(
        spec.get("queue_timeout", 3600), 0.1, 604800, "queue_timeout"
    )
    spec["estimate"] = bounded(spec.get("estimate", 30), 0.01, 86400, "estimate")
    spec["priority"] = int(bounded(spec.get("priority", 0), -5, 5, "priority"))
    spec["dependencies"] = spec.get("dependencies", [])
    if not isinstance(spec["dependencies"], list) or len(spec["dependencies"]) > 100:
        raise WorkbenchError("Invalid dependencies")
    spec["purpose"] = str(spec.get("purpose", operation))[:2000]
    if "not_before" in spec:
        bounded(spec["not_before"], 0, 1e12, "not_before")
    for key in ("accept_hooks", "baseline", "try_only"):
        if key in spec and not isinstance(spec[key], bool):
            raise WorkbenchError(key + " must be a boolean")
    spec["resources"] = []
    spec["input_fingerprints"] = {}
    spec["snapshot_hashes"] = {}
    expectations = spec.get("ui_expect", [])
    if not isinstance(expectations, list) or len(expectations) > 20:
        raise WorkbenchError("ui_expect must be an array of at most 20 UI conditions")
    for item in expectations:
        if (
            not isinstance(item, dict)
            or not item
            or set(item) - {"resource-id", "text", "content-desc"}
        ):
            raise WorkbenchError(
                "UI conditions accept resource-id, text and content-desc"
            )
        if any(not isinstance(v, str) or len(v) > 500 for v in item.values()):
            raise WorkbenchError("UI condition values must be short strings")
    spec["python"] = project.get("python", sys.executable)
    spec["env"] = project.get("env", {})
    spec["directory"] = str(directory)
    spec["readonly"] = False
    spec["insertable"] = False
    if spec.get("device"):
        ident, entry = device_id(config, spec["device"])
        spec["device"], spec["serial"] = ident, entry["serial"]
        spec["adb"] = entry.get("adb", config.get("adb", "adb"))
        spec["resources"] += [
            resource("device:" + ident),
            resource("service:adb", "read"),
        ]
    if operation == "host.lease":
        from .leases import normalize_lease

        normalize_lease(spec, project, root)
    elif operation in {
        "device.scene",
        "device.screenshot",
        "device.observe",
        "device.info",
        "device.recover",
    }:
        if not spec.get("device"):
            raise WorkbenchError("Device required")
        if operation == "device.scene":
            steps = spec.get("steps", [])
        elif operation == "device.info":
            steps = [{"action": "observe"}]
        else:
            steps = [
                {
                    "action": "screenshot"
                    if operation == "device.screenshot"
                    else "observe"
                }
            ]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 200:
            raise WorkbenchError("Scene needs 1..200 steps")
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in SCENE_STEPS:
                raise WorkbenchError("Unknown scene step")
            if step["action"] in ("wait", "checkpoint"):
                bounded(
                    step.get("seconds", 0),
                    0,
                    min(spec["timeout"], 3600),
                    "step.seconds",
                )
            if step["action"] == "checkpoint":
                step["allow"] = step.get(
                    "allow", ["device.screenshot", "device.observe"]
                )
                if any(
                    x not in ("device.screenshot", "device.observe")
                    for x in step["allow"]
                ):
                    raise WorkbenchError(
                        "Only observations may be inserted in this version"
                    )
                step["max_insertions"] = int(
                    bounded(step.get("max_insertions", 2), 0, 10, "max_insertions")
                )
                step["budget"] = bounded(
                    step.get("budget", 10), 0.1, 60, "checkpoint budget"
                )
            if step["action"] == "launch" and not re.fullmatch(
                r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+", step.get("component", "")
            ):
                raise WorkbenchError(
                    "Launch needs an explicit package/activity component"
                )
            if step["action"] in ("tap", "swipe"):
                keys = (
                    ("x", "y")
                    if step["action"] == "tap"
                    else ("x1", "y1", "x2", "y2", "duration")
                )
                for k in keys:
                    bounded(step.get(k), 0, 100000, k)
            if step["action"] == "keyevent":
                bounded(step.get("code"), 0, 1000, "keyevent code")
            if step["action"] == "input" and (
                not isinstance(step.get("text"), str) or len(step["text"]) > 10000
            ):
                raise WorkbenchError("Input text must be at most 10000 characters")
            if step["action"] == "logcat":
                step["lines"] = int(
                    bounded(step.get("lines", 500), 1, 10000, "logcat.lines")
                )
                if step.get("buffer", "main") not in {
                    "main", "system", "crash", "events", "radio", "all"
                }:
                    raise WorkbenchError("Unsupported logcat buffer")
                if step.get("level") not in {
                    None, "V", "D", "I", "W", "E", "F", "S"
                }:
                    raise WorkbenchError("Unsupported logcat level")
                if not isinstance(step.get("clear", False), bool):
                    raise WorkbenchError("logcat.clear must be boolean")
            if step["action"] == "hook_attach":
                hook = Path(step["script"]).expanduser().resolve()
                if not hook.is_relative_to(root) or not hook.is_file():
                    raise WorkbenchError(
                        "Hook script must be a file in the registered project"
                    )
                target = directory / ("hook-" + file_hash(hook) + ".js")
                shutil.copyfile(hook, target)
                step["script"] = str(target)
                spec["snapshot_hashes"][str(target)] = file_hash(target)
                if not re.fullmatch(r"[A-Za-z0-9_.]+", step.get("package", "")):
                    raise WorkbenchError("Hook package required")
        spec["steps"] = (
            [{"action": "info"}] if operation == "device.info" else steps
        )
        spec["readonly"] = operation == "device.info" or all(
            x["action"] in {"observe", "screenshot", "ui_dump", "wait", "checkpoint"}
            or (x["action"] == "logcat" and not x.get("clear"))
            for x in steps
        )
        spec["insertable"] = operation in {
            "device.screenshot",
            "device.observe",
        } and not spec.get("prepare")
        spec["resources"].append(
            resource(path_resource(Path(spec["python"]).parent.parent), "read")
        )
        if operation == "device.recover":
            spec["recovery"] = True
            spec["insertable"] = False
    elif operation == "test.simulate" and config.get("test_mode"):
        spec["steps"] = spec.get("steps", [{"action": "wait", "seconds": 0.1}])
        spec["readonly"] = spec.get("test_readonly", True)
        spec["insertable"] = spec.get("test_insertable", False)
        for extra in spec.get("test_resources", []):
            if extra.get("mode") not in ("read", "write"):
                raise WorkbenchError("Bad test resource")
            spec["resources"].append(extra)
    else:
        entry = project.get("operations", {}).get(operation)
        if entry is None:
            raise WorkbenchError(f"Unknown registered operation: {operation}")
        shared_outputs = entry.get("shared_outputs", [])
        if not isinstance(shared_outputs, list) or not all(
            isinstance(flag, str) and flag for flag in shared_outputs
        ):
            raise WorkbenchError("shared_outputs must be an array of flags")
        if not set(shared_outputs).issubset(entry.get("outputs", [])):
            raise WorkbenchError("shared_outputs must be a subset of outputs")
        if "python" in entry:
            if not isinstance(entry["python"], str) or not entry["python"]:
                raise WorkbenchError("Registered Python must be a nonempty path")
            interpreter = (root / entry["python"]).expanduser().absolute()
            if not interpreter.is_file():
                raise WorkbenchError(
                    "Registered Python is missing; prepare its tool environment: "
                    + str(interpreter)
                )
            # Preserve the venv path: resolving its symlink would select the base interpreter.
            spec["python"] = str(interpreter)
            spec["resources"].append(
                resource(path_resource(interpreter.parent.parent), "read")
            )
        script = registered_path(root, project, entry["script"])
        args = spec.get("args", [])
        if (
            not isinstance(args, list)
            or not all(isinstance(x, str) for x in args)
            or len(args) > 256
        ):
            raise WorkbenchError("args must be an array of at most 256 strings")
        for flag, folder in entry.get("default_outputs", {}).items():
            if not any(x == flag or x.startswith(flag + "=") for x in args):
                args = [*args, flag, str(within(directory, folder))]
                spec["args"] = args
        if entry.get("subcommand") and (not args or args[0] != entry["subcommand"]):
            raise WorkbenchError(
                "This adapter requires subcommand " + entry["subcommand"]
            )
        serial_flag = entry.get("serial")
        if serial_flag is not None:
            if not spec.get("device"):
                raise WorkbenchError("Device required for this operation")
            if isinstance(serial_flag, int):
                if len(args) <= serial_flag or args[serial_flag] != spec["serial"]:
                    raise WorkbenchError("Positional serial must match selected device")
            elif serial_flag.startswith("fixed:"):
                if spec["serial"] != serial_flag[6:]:
                    raise WorkbenchError("Script is pinned to another source device")
            else:
                matches = [
                    args[i + 1] for i, x in enumerate(args[:-1]) if x == serial_flag
                ]
                matches += [
                    x.split("=", 1)[1] for x in args if x.startswith(serial_flag + "=")
                ]
                if matches != [spec["serial"]]:
                    raise WorkbenchError(
                        "Exactly one matching serial argument is required"
                    )
        elif spec.get("device"):
            raise WorkbenchError("Local operation must not reserve a phone")
        source = registered_path(
            root, project, entry.get("source", str(Path(entry["script"]).parent))
        )
        snapshot = directory / "source"
        source_manifest = snapshot_tree(source, snapshot)
        spec["source_manifest"] = source_manifest
        copy_path = snapshot / script.relative_to(source)
        if not copy_path.is_file():
            raise WorkbenchError("Entry script missing from snapshot")
        spec["script"] = str(script)
        spec["script_copy"] = str(copy_path)
        spec["source"] = str(source)
        spec["source_hash"] = file_hash(script)
        spec["snapshot_hashes"][str(copy_path)] = file_hash(copy_path)
        spec["entry"] = entry
        if entry.get("recovery"):
            repairs = entry.get("recovery_for")
            if (not entry.get("result_file")
                    or not isinstance(repairs, list) or not repairs
                    or not all(isinstance(x, str) and x for x in repairs)):
                raise WorkbenchError("Recovery adapters require result_file and recovery_for")
            if not spec.get("device") and not (
                entry.get("adb_exclusive") or entry.get("mutates_environment")
            ):
                raise WorkbenchError(
                    "Local recovery adapters require an exclusive shared resource"
                )
            spec["recovery"] = True
        spec["nested_source_manifests"] = {}
        for index, location in enumerate(entry.get("nested_sources", [])):
            nested = registered_path(root, project, location)
            spec["nested_source_manifests"][str(nested)] = snapshot_tree(
                nested, directory / ("nested-source-" + str(index))
            )
            spec["resources"].append(resource(path_resource(nested), "read"))
        if entry.get("uses_mcp"):
            spec["resources"].append(resource("analysis:" + str(root)))
        spec["readonly"] = bool(entry.get("readonly"))
        mode = "write" if entry.get("mutates_environment") else "read"
        for location in project.get("environments", []):
            spec["resources"].append(resource(path_resource(root / location), mode))
        spec["resources"].append(resource(path_resource(source), "read"))
        # Existing script paths and user-selected workspaces also participate in coordination.
        input_flags = {
            "--extension",
            "--result",
            "--bridge",
            "--apk",
            "--binary",
            "--server",
            "--gadget",
            "--harness",
            "--java-bridge",
            "--map-extension",
            "--profile",
            "--lock",
            "--so-dir",
            "--python",
            "--session",
            "--previous-python",
            "--adb",
            "--aapt2",
            "--apksigner",
            "--keytool",
        }
        directory_flags = {
            "--workspace",
            "--cache-dir",
            "--source",
            "--build-dir",
            "--prefix",
            "--output-dir",
            "--project-path",
            "--root",
        }
        for i, arg in enumerate(args):
            flag = arg.split("=", 1)[0]
            if flag not in input_flags | directory_flags:
                continue
            value = (
                arg.split("=", 1)[1]
                if "=" in arg
                else (args[i + 1] if i + 1 < len(args) else None)
            )
            if value is None:
                raise WorkbenchError("Missing path argument: " + flag)
            path = (root / value).expanduser().resolve()
            spec["resources"].append(
                resource(
                    path_resource(path),
                    "write"
                    if flag in directory_flags and not spec["readonly"]
                    else "read",
                )
            )
            if path.is_file() and flag in input_flags:
                spec["input_fingerprints"][str(path)] = file_hash(path)
        for path in (Path(spec["python"]), root / ".envrc"):
            if path.is_file():
                spec["input_fingerprints"][str(path.resolve())] = file_hash(path)
        if operation in ("apk.doctor", "apk.devices"):
            spec["resources"].append(resource("service:adb", "read"))
        if entry.get("adb_exclusive"):
            # This registered wrapper accepts arbitrary explicit ADB subcommands, including server controls.
            spec["resources"] = [
                r for r in spec["resources"] if r["key"] != "service:adb"
            ] + [resource("service:adb")]
        if entry.get("mutates_environment"):
            spec["resources"].append(resource("service:adb"))
        if entry.get("compute"):
            spec["compute"] = True
        for item in entry.get("resources", []):
            spec["resources"].append(
                resource(path_resource(root / item["path"]), item.get("mode", "write"))
            )
        spec["outputs"] = []
        for index in entry.get("output_positions", []):
            if len(args) > index:
                target = (root / args[index]).resolve()
                if not entry.get("shared_output_positions"):
                    spec["outputs"].append(str(target))
                spec["resources"].append(resource(path_resource(target)))
        for flag in entry.get("outputs", []):
            values = [args[i + 1] for i, x in enumerate(args[:-1]) if x == flag]
            values += [x.split("=", 1)[1] for x in args if x.startswith(flag + "=")]
            for value in values:
                target = (root / value).resolve()
                if flag not in entry.get("shared_outputs", []):
                    spec["outputs"].append(str(target))
                spec["resources"].append(resource(path_resource(target)))
    # Resource effects come exclusively from the registered adapter.
    spec["resources"] = list(
        {(r["key"], r["mode"]): r for r in spec["resources"]}.values()
    )
    atomic_json(directory / "normalized.json", spec)
    return spec
