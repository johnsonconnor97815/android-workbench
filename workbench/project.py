"""Project registration and explicit references to this repository's bundled code."""

import copy
import os
from pathlib import Path
import sys
from .common import WorkbenchError

BUNDLE_PREFIX = "@workbench/"
SKILLS = ("android-static-env", "frida-modified", "pull-android-apk")


def bundle_root(root, project):
    location = project.get("workbench_root")
    # Older registries used a checkout directly below the analysis project.
    candidate = (root / location).resolve() if location else root / "android-workbench"
    if not (candidate / "workbench/bridge.py").is_file():
        raise WorkbenchError(
            "Workbench checkout missing; rerun scripts/configure_project.py --update"
        )
    return candidate


def registered_path(root, project, value):
    """Only project files or explicitly registered Workbench files are executable."""
    if not isinstance(value, str) or not value:
        raise WorkbenchError("Registered path must be a nonempty string")
    base = root
    if value.startswith(BUNDLE_PREFIX):
        base = bundle_root(root, project)
        value = value[len(BUNDLE_PREFIX) :]
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise WorkbenchError("Registered path escapes its source root")
    return path


def builtin_operations():
    operations = {}

    def add(name, skill, script, **entry):
        source = BUNDLE_PREFIX + "skills/" + skill
        operations[name] = {
            "script": source + "/scripts/" + script,
            "source": source,
            **entry,
        }

    apk = "pull-android-apk"
    add(
        "apk.pull",
        apk,
        "apk_pull.py",
        serial="--serial",
        subcommand="pull",
        readonly=True,
        outputs=["--output"],
    )
    add(
        "apk.verify",
        apk,
        "apk_pull.py",
        subcommand="verify",
        readonly=True,
        compute=True,
    )
    add("apk.doctor", apk, "apk_pull.py", subcommand="doctor", readonly=True)
    add("apk.devices", apk, "apk_pull.py", subcommand="devices", readonly=True)
    definitions = {
        "inspect_device": {
            "serial": "--serial",
            "readonly": True,
            "outputs": ["--output"],
        },
        "deploy_server": {
            "serial": "--serial",
            "outputs": ["--record"],
            "result_file": ".",
        },
        "probe_native": {
            "serial": "--serial",
            "outputs": ["--record"],
            "result_file": ".",
        },
        "probe_uncrackable": {
            "serial": "--serial",
            "outputs": ["--output"],
            "result_file": "results.json",
        },
        "fetch": {"mutates_environment": True},
        "apply_profile": {"mutates_environment": True},
        "build": {"compute": True, "mutates_environment": True},
        "sync_resources": {"mutates_environment": True},
        "provenance": {"readonly": True},
    }
    for name, entry in definitions.items():
        add("frida." + name, "frida-modified", name + ".py", **entry)
    for name in ("setup", "mcp_setup"):
        add(
            "environment." + name,
            "android-static-env",
            name + ".py",
            mutates_environment=True,
            **({"uses_mcp": True} if name == "mcp_setup" else {}),
        )
    for name in ("smoke", "mcp_smoke", "mcp_probe", "inspect_so"):
        add(
            "static." + name,
            "android-static-env",
            name + ".py",
            readonly=True,
            compute=True,
            **({"uses_mcp": True} if name in ("mcp_smoke", "mcp_probe") else {}),
        )
    return operations


def generate(root, checkout, python=None):
    root, checkout = Path(root).resolve(), Path(checkout).resolve()
    interpreter = (
        Path(python).expanduser().absolute() if python else root / ".venv/bin/python"
    )
    if not interpreter.is_file():
        if python:
            raise WorkbenchError(
                "Python interpreter does not exist: " + str(interpreter)
            )
        interpreter = Path(sys.executable).absolute()
    sdk = (
        Path(os.environ.get("ANDROID_HOME", root / ".android-static/sdk"))
        .expanduser()
        .resolve()
    )
    doc = {
        "version": 1,
        "workbench_root": str(checkout),
        "python": str(interpreter),
        "environments": [".venv", ".tools", ".android-static", str(sdk)],
        "mcp_environments": [
            ".android-static/bin",
            ".android-static/venvs",
            ".android-static/tools",
            ".android-static/sdk",
            ".venv",
        ],
        "env": {
            "PATH": os.pathsep.join(
                [
                    str(root / ".venv/bin"),
                    str(root / ".android-static/bin"),
                    str(sdk / "platform-tools"),
                    os.environ.get("PATH", os.defpath),
                ]
            ),
            "ANDROID_HOME": str(sdk),
            "ANDROID_SDK_ROOT": str(sdk),
        },
        "skills": {name: BUNDLE_PREFIX + "skills/" + name for name in SKILLS},
        "operations": builtin_operations(),
    }
    bundle_root(root, doc)
    for entry in doc["operations"].values():
        if not registered_path(root, doc, entry["script"]).is_file():
            raise WorkbenchError("Bundled operation missing: " + entry["script"])
    return doc


def update(existing, generated, python=None):
    """Rebind built-ins while retaining custom operations and project environments."""
    if existing.get("version") != 1:
        raise WorkbenchError("Unsupported project manifest")
    result = copy.deepcopy(existing)
    result["workbench_root"] = generated["workbench_root"]
    if python or "python" not in result:
        result["python"] = generated["python"]
    old_skills = existing.get("skills", {})
    result.setdefault("skills", {}).update(generated["skills"])
    result.setdefault("operations", {}).update(generated["operations"])
    # Preserve nested resource coverage when an older wrapper calls a bundled Skill.
    replacements = {
        location: generated["skills"][name]
        for name, location in old_skills.items()
        if name in generated["skills"]
    }
    for entry in result["operations"].values():
        if "nested_sources" in entry:
            entry["nested_sources"] = [
                replacements.get(source, source) for source in entry["nested_sources"]
            ]
    return result
