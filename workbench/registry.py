"""Server-side operation definitions; callers cannot downgrade resource effects."""

from __future__ import annotations
import copy
import hashlib
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
        if (source.name == "Tools" and path.relative_to(source).parts[0] == "runtime"):
            continue
        if not path.is_file() or any(
            p in {".git", "__pycache__", "node_modules", ".venv"}
            for p in path.relative_to(source).parts
        ):
            continue
        if path.suffix not in {
            ".py",
            ".sh",
            ".js",
            ".cjs",
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


def analysis_reference_paths(documents, observed_hashes=None):
    """Collect bounded local references through plan/ledger/review records."""
    pending = list(documents)
    visited, references = set(), set()
    total_bytes = 0
    parsed_documents = 0
    while pending:
        document = pending.pop()
        if document in visited:
            continue
        visited.add(document)
        if not document.is_file() or document.suffix.casefold() != ".json":
            continue
        parsed_documents += 1
        if parsed_documents > 32:
            raise WorkbenchError("Analysis records exceed the 32-document reference budget")
        remaining = 16 * 1024 * 1024 - total_bytes
        with document.open("rb") as stream:
            raw = stream.read(remaining + 1)
        total_bytes += len(raw)
        if total_bytes > 16 * 1024 * 1024:
            raise WorkbenchError("Analysis records exceed the 16 MiB reference-read budget")
        if observed_hashes is not None:
            observed_hashes[document] = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        items = [data]
        while items:
            item = items.pop()
            selected = []
            if isinstance(item, dict):
                if isinstance(item.get("path"), str):
                    selected.append(item["path"])
                # Review claims use evidence arrays of strings; other records
                # carry arrays of {path,sha256} objects and recurse below.
                evidence = item.get("evidence")
                if isinstance(evidence, list):
                    selected.extend(value for value in evidence if isinstance(value, str))
                items.extend(item.values())
            elif isinstance(item, list):
                items.extend(item)
            for raw in selected:
                path = (document.parent / raw).expanduser().resolve()
                if path not in references:
                    references.add(path)
                    pending.append(path)
            if len(references) > 256:
                raise WorkbenchError("Analysis records exceed the 256-reference budget")
    return references


def normalize(request, config, directory, *, manual_return=None):
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
        "artifact_outputs",
        "artifact_inputs",
        "recovery_jobs",
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
    if manual_return is not None:
        if operation != "device.recover" or manual_return.get("transport") != "sideload":
            raise WorkbenchError("Invalid internal manual return")
        spec["manual_return"] = copy.deepcopy(manual_return)
    spec["project"] = str(root)
    spec["timeout"] = bounded(spec.get("timeout", 600), 0.1, 86400, "timeout")
    spec["queue_timeout"] = bounded(
        spec.get("queue_timeout", 3600), 0.1, 604800, "queue_timeout"
    )
    spec["limits"] = {
        "max_job_log_bytes": bounded(
            config.get("max_job_log_bytes", 32 * 1024 * 1024),
            1,
            1024**3,
            "max_job_log_bytes",
        ),
        "max_job_output_bytes": bounded(
            config.get("max_job_output_bytes", 2 * 1024**3),
            1,
            1024**4,
            "max_job_output_bytes",
        ),
    }
    spec["estimate"] = bounded(spec.get("estimate", 30), 0.01, 86400, "estimate")
    spec["priority"] = int(bounded(spec.get("priority", 0), -5, 5, "priority"))
    spec["dependencies"] = spec.get("dependencies", [])
    if not isinstance(spec["dependencies"], list) or len(spec["dependencies"]) > 100:
        raise WorkbenchError("Invalid dependencies")
    artifact_outputs = spec.get("artifact_outputs", {})
    artifact_inputs = spec.get("artifact_inputs", {})
    if not isinstance(artifact_outputs, dict) or not isinstance(artifact_inputs, dict):
        raise WorkbenchError("Artifact outputs and inputs must be objects")
    if len(artifact_outputs) > 32 or len(artifact_inputs) > 32:
        raise WorkbenchError("At most 32 artifact bindings are allowed")
    for name in (*artifact_outputs, *artifact_inputs):
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
        ):
            raise WorkbenchError("Invalid artifact name: " + str(name))
    for value in artifact_outputs.values():
        if not isinstance(value, str) or not value:
            raise WorkbenchError("Artifact output path must be a nonempty string")
    for value in artifact_inputs.values():
        if (
            not isinstance(value, str)
            or not value.startswith("--")
            or any(character.isspace() for character in value)
        ):
            raise WorkbenchError("Artifact input flag must be a simple --flag")
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
    from .toolchains import worker_environment
    spec["env"], tool_bindings = worker_environment(root, project)
    spec["toolchain_bindings"] = tool_bindings
    spec["directory"] = str(directory)
    spec["readonly"] = False
    spec["insertable"] = False
    if spec.get("device"):
        ident, entry = device_id(config, spec["device"])
        assigned_project = entry.get("project")
        if (
            assigned_project
            and spec["project"] != assigned_project
            and operation != "device.recover"
        ):
            raise WorkbenchError("Device is assigned to another project")
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
        if "confirms_cleanup" in entry and not isinstance(entry["confirms_cleanup"], bool):
            raise WorkbenchError("confirms_cleanup must be a boolean")
        if "python" in entry:
            if not isinstance(entry["python"], str) or not entry["python"]:
                raise WorkbenchError("Registered Python must be a nonempty path")
            interpreter = (root / entry["python"]).expanduser().absolute()
            relative_python = Path(entry["python"])
            if relative_python.parts[:2] == (".android-static", "venvs") and len(relative_python.parts) > 2:
                selected = tool_bindings.get(relative_python.parts[2])
                if selected:
                    interpreter = Path(selected["workspace"]) / relative_python
            prepared_later = (spec['dependencies'] and len(relative_python.parts) > 2
                              and relative_python.parts[:2] == ('.android-static', 'venvs')
                              and relative_python.parts[2] in entry.get('requires_tools', []))
            if not interpreter.is_file() and not prepared_later:
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
        if (
            entry["script"] == "@workbench/skills/android-workbench/components/analysis-agent/scripts/analysis.py"
            and entry.get("subcommand") in {"python", "exec"}
        ):
            # Older project registries predate host resource declarations. Enforce
            # the bundled adapter's current effects without rewriting the project.
            entry = {
                **entry,
                "host_execution": True,
                "input_flags": list(dict.fromkeys([
                    *entry.get("input_flags", []), "--read-path",
                    *(["--script"] if entry["subcommand"] == "python" else []),
                ])),
                "outputs": list(dict.fromkeys([
                    *entry.get("outputs", []), "--write-path",
                    *(["--publish-path"] if entry["subcommand"] == "exec" else []),
                ])),
                "shared_outputs": list(dict.fromkeys([*entry.get("shared_outputs", []), "--write-path"])),
            }
        args = spec.get("args", [])
        if (
            not isinstance(args, list)
            or not all(isinstance(x, str) for x in args)
            or len(args) > 256
        ):
            raise WorkbenchError("args must be an array of at most 256 strings")
        if entry.get('requires_frida_client') and project.get('toolchain', {}).get('auto_install'):
            version = next((args[i + 1] for i, x in enumerate(args[:-1]) if x == '--version'), None)
            version = next((x.split('=', 1)[1] for x in args if x.startswith('--version=')), version)
            if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
                raise WorkbenchError('An explicit Frida client version is required')
            python = Path(tool_bindings['frida']['workspace']) / 'Tools/runtime/frida-clients' / version / 'venv/bin/python'
            if not python.is_file() and not spec['dependencies']:
                raise WorkbenchError('Prepare the selected host client with frida.install_client first')
            spec['python'] = str(python)
        if operation.startswith("tools."):
            # Fixed tool storage belongs to this registered project even in old registries.
            workspaces = [args[i + 1] for i, x in enumerate(args[:-1]) if x == "--workspace"]
            workspaces += [x.split("=", 1)[1] for x in args if x.startswith("--workspace=")]
            if len(workspaces) > 1 or (workspaces and (root / workspaces[0]).resolve() != root):
                raise WorkbenchError("Tools workspace must match the registered project")
            spec["resources"].append(resource(path_resource(root / "Tools/runtime"),
                "write" if operation in {"tools.install", "tools.ensure"} else "read"))
        default_arguments = []
        for flag, location in entry.get("default_paths", {}).items():
            if not any(x == flag or x.startswith(flag + "=") for x in args):
                default_arguments += [flag, str(within(root, location))]
        for flag, folder in entry.get("default_outputs", {}).items():
            if not any(x == flag or x.startswith(flag + "=") for x in args):
                default_arguments += [flag, str(within(directory, folder))]
        if default_arguments:
            index = args.index("--") if "--" in args else len(args)
            args = [*args[:index], *default_arguments, *args[index:]]
            spec["args"] = args
        if entry.get("subcommand") and entry["subcommand"] not in args:
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
        spec["confirms_cleanup"] = bool(entry.get("confirms_cleanup", False))
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
        from .results import readonly_package_query, readonly_hal_query, readonly_property_query

        spec["readonly"] = spec["readonly"] or readonly_package_query(spec) or readonly_hal_query(spec) or readonly_property_query(spec)
        mode = "write" if entry.get("mutates_environment") else "read"
        for location in project.get("environments", []):
            spec["resources"].append(resource(path_resource(root / location), mode))
        spec["resources"].append(resource(path_resource(source), "read"))
        if entry.get("host_execution"):
            # Arbitrary local code can write files without an output flag. Lock the
            # whole project and its working directory rather than guessing effects
            # from shell/Python text. Additional external paths must be declared.
            spec["resources"].append(resource(path_resource(root)))
            working_directories = [
                args[i + 1] for i, value in enumerate(args[:-1]) if value == "--cwd"
            ] + [value.split("=", 1)[1] for value in args if value.startswith("--cwd=")]
            if len(working_directories) > 1:
                raise WorkbenchError("Host execution accepts at most one --cwd")
            if working_directories:
                spec["resources"].append(
                    resource(path_resource((root / working_directories[0]).expanduser().resolve()))
                )
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
            "--research",
            "--factory-zip",
            "--rom",
            "--recovery-boot",
            "--flash-record",
            "--prepared-record",
            "--backup-record",
            "--data-record",
            "--health-record",
            "--ota",
            "--addon",
            "--patched-boot",
            "--stock-boot",
            "--magisk-apk",
            "--adb",
            "--aapt2",
            "--apksigner",
            "--keytool",
        }
        for field in ("input_flags", "value_flags"):
            values = entry.get(field, [])
            if not isinstance(values, list) or not all(
                isinstance(flag, str) and flag.startswith("--") and "=" not in flag
                for flag in values
            ):
                raise WorkbenchError(field + " must be an array of --flags")
        input_flags.update(entry.get("input_flags", []))
        if operation == "device_manager.image_verify":
            input_flags.add("--image")
        if operation in {"device_manager.flash", "device_manager.backup_prepare", "device_manager.install_rom"}:
            image_flag = "--prepare-image" if operation == "device_manager.install_rom" else "--image"
            for i, argument in enumerate(args):
                if argument == image_flag:
                    value = args[i + 1] if i + 1 < len(args) else ""
                elif argument.startswith(image_flag + "="):
                    value = argument.split("=", 1)[1]
                else:
                    continue
                if "=" not in value:
                    raise WorkbenchError("Flash image must be PARTITION=PATH")
                path = (root / value.split("=", 1)[1]).expanduser().resolve()
                if not path.is_file():
                    raise WorkbenchError("Flash image is missing: " + str(path))
                spec["resources"].append(resource(path_resource(path), "read"))
                spec["input_fingerprints"][str(path)] = file_hash(path)
        if operation.startswith("device_manager."):
            # Backup and health receipts contain additional files. Hold and hash
            # those inputs for the entire job, including a nested backup manifest.
            documents = [
                (root / (arg.split("=", 1)[1] if "=" in arg else args[i + 1])).expanduser().resolve()
                for i, arg in enumerate(args)
                if arg.split("=", 1)[0] in {
                    "--research", "--backup-record", "--data-record", "--health-record", "--flash-record", "--prepared-record"}
                and ("=" in arg or i + 1 < len(args))
            ]
            visited, references = set(), set()
            while documents:
                document = documents.pop()
                if document in visited:
                    continue
                visited.add(document)
                if len(visited) > 32:
                    raise WorkbenchError("Too many nested flashing records")
                if not document.is_file() or document.stat().st_size > 16 * 1024 * 1024:
                    continue
                try:
                    data = json.loads(document.read_text())
                except (ValueError, UnicodeError):
                    continue
                pending = [data]
                while pending:
                    item = pending.pop()
                    if isinstance(item, list):
                        pending.extend(item)
                    elif isinstance(item, dict):
                        if isinstance(item.get("path"), str):
                            references.add((document.parent / item["path"]).expanduser().resolve())
                        backup = item.get("backup_record")
                        if isinstance(backup, dict) and isinstance(backup.get("path"), str):
                            # flash_plan paths follow the project's command cwd.
                            path = (root / backup["path"]).expanduser().resolve()
                            references.add(path)
                            documents.append(path)
                        pending.extend(item.values())
                    if len(references) > 4096:
                        raise WorkbenchError("Too many flashing evidence files")
            for path in references:
                spec["resources"].append(resource(path_resource(path), "read"))
                if path.is_file():
                    spec["input_fingerprints"][str(path)] = file_hash(path)
        if entry.get("analysis_plan"):
            # Plans, ledgers and review/runtime records can refer to further
            # evidence. Reserve that transitive set before running the adapter.
            documents = [
                (root / (arg.split("=", 1)[1] if "=" in arg else args[i + 1])).expanduser().resolve()
                for i, arg in enumerate(args)
                if arg.split("=", 1)[0] in {"--plan", "--evidence", "--recon", "--ledger", "--unknown-ledger"}
                and ("=" in arg or i + 1 < len(args))
            ]
            mutable_outputs = {
                (root / (arg.split("=", 1)[1] if "=" in arg else args[i + 1])).expanduser().resolve()
                for i, arg in enumerate(args)
                if arg.split("=", 1)[0] in set(entry.get("outputs", []))
                and ("=" in arg or i + 1 < len(args))
            }
            observed_hashes = {}
            references = analysis_reference_paths(documents, observed_hashes)
            references.update(documents)
            for path in references:
                spec["resources"].append(resource(path_resource(path), "read"))
                if path.is_file() and path not in mutable_outputs:
                    spec["input_fingerprints"][str(path)] = (observed_hashes[path]
                        if path in observed_hashes else file_hash(path))
        if operation == "analysis.knowledge" and not any(
            arg.split("=", 1)[0] in {"--root", "--url"} for arg in args
        ):
            # An index-only search revalidates its recorded roots. It must hold
            # the same path reservations as an explicitly supplied root search.
            indexes = [
                (root / (arg.split("=", 1)[1] if "=" in arg else args[i + 1])).expanduser().resolve()
                for i, arg in enumerate(args)
                if arg.split("=", 1)[0] == "--index" and ("=" in arg or i + 1 < len(args))
            ]
            for index in indexes:
                if not index.is_file():
                    continue
                if index.stat().st_size > 64 * 1024 * 1024:
                    raise WorkbenchError("Knowledge index exceeds the source-reservation read budget")
                try:
                    data = json.loads(index.read_text())
                except (ValueError, UnicodeError):
                    continue  # The adapter returns the explicit invalid-cache error.
                roots = data.get("roots", []) if isinstance(data, dict) else []
                if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
                    continue
                # Pin this input only until launch; the adapter legitimately
                # replaces its own cache after checking the recorded roots.
                spec["input_fingerprints"][str(index)] = file_hash(index)
                for recorded in roots:
                    spec["resources"].append(resource(path_resource((root / recorded).expanduser().resolve()), "read"))
        if operation == "analysis.query":
            databases = [
                (root / (arg.split("=", 1)[1] if "=" in arg else args[i + 1])).expanduser().resolve()
                for i, arg in enumerate(args)
                if arg.split("=", 1)[0] == "--db" and ("=" in arg or i + 1 < len(args))
            ]
            for database in databases:
                # SQLite mode=ro can create/update shared-memory bookkeeping.
                # Reserve both sidecars, including paths not present at submit.
                for suffix in ("-wal", "-shm"):
                    path = Path(str(database) + suffix)
                    spec["resources"].append(resource(path_resource(path), "write"))
                    if suffix == "-wal" and path.is_file():
                        spec["input_fingerprints"][str(path)] = file_hash(path)
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
        if entry.get("positional_inputs"):
            # Adapters declare their value-taking options so positional files are
            # distinguished from output paths, tool names and numeric limits.
            value_flags = set(entry.get("value_flags", []))
            skip_value = False
            after_separator = False
            for arg in args:
                if skip_value:
                    skip_value = False
                    continue
                if not after_separator and arg == "--":
                    after_separator = True
                    continue
                if not after_separator and arg.startswith("-"):
                    skip_value = "=" not in arg and arg in value_flags
                    continue
                path = (root / arg).expanduser().resolve()
                if not path.is_file():
                    raise WorkbenchError("Input file is missing: " + str(path))
                spec["resources"].append(resource(path_resource(path), "read"))
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
        if operation in {"tools.js_ast", "tools.js_deobfuscate", "tools.binary_layout", "tools.firmware_scan"}:
            for output in list(spec["outputs"]):
                artifacts = output + ".artifacts"
                spec["outputs"].append(artifacts)
                spec["resources"].append(resource(path_resource(Path(artifacts))))
    # Resource effects come exclusively from the registered adapter.
    tool_mode = "write" if spec.get("entry", {}).get("mutates_environment") else "read"
    for workspace in {b["workspace"] for b in tool_bindings.values()}:
        for component in ('.android-static', 'Tools/runtime', '.tools'):
            spec["resources"].append(resource(path_resource(Path(workspace) / component), tool_mode))
    if operation == "environment.setup" and "--install-system-deps" in spec.get("args", []):
        for system_path in ("/usr", "/etc", "/var", "/run", "/tmp"):
            spec["resources"].append(resource(path_resource(Path(system_path)), "write"))
    if artifact_outputs:
        if "entry" not in spec:
            raise WorkbenchError("Artifact outputs require a registered operation")
        outputs = set(spec.get("outputs", []))
        for name, value in artifact_outputs.items():
            path = (root / value).expanduser().resolve()
            inside_declared_output = any(
                path.is_relative_to(Path(output)) for output in outputs
            )
            if str(path) not in outputs and not inside_declared_output:
                raise WorkbenchError(
                    "Artifact output is not a declared operation output: " + name
                )
            spec["artifact_outputs"][name] = str(path)
    spec["resources"] = list(
        {(r["key"], r["mode"]): r for r in spec["resources"]}.values()
    )
    atomic_json(directory / "normalized.json", spec)
    return spec
