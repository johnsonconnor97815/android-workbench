"""Project registration and explicit references to this repository's bundled code."""

import copy
import os
from pathlib import Path
import sys
from .common import WorkbenchError

BUNDLE_PREFIX = "@workbench/"
SKILL = "android-workbench"
LEGACY_SKILLS = (
    "android-analysis",
    "android-device",
    "android-static-env",
    "frida-modified",
    "pull-android-apk",
    "apk-permission-audit",
)
LEGACY_COMPONENTS = {
    "android-static-env": "static-env",
    "frida-modified": "frida",
    "pull-android-apk": "apk-export",
    "apk-reverse": "apk-reverse",
    "apk-permission-audit": "permission-audit",
}
SKILL_SOURCE = BUNDLE_PREFIX + "skills/" + SKILL
COMPONENTS = {
    "static-env": "components/static-env",
    "frida": "components/frida",
    "apk-export": "components/apk-export",
    "apk-reverse": "components/apk-reverse",
    "analysis-agent": "components/analysis-agent",
    "device-manager": "components/device-manager",
    "permission-audit": "components/permission-audit",
}


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

    def add(name, component, script, **entry):
        source = SKILL_SOURCE + "/" + COMPONENTS[component]
        operations[name] = {
            "script": source + "/scripts/" + script,
            "source": source,
            **entry,
        }

    apk = "apk-export"
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
    device_manager = "device-manager"
    device_manager_definitions = {
        "preflight": {
            "subcommand": "preflight",
            "serial": "--serial",
            "readonly": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "partition_inspect": {
            "subcommand": "partition-inspect",
            "serial": "--serial",
            "adb_exclusive": True,
            "readonly": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "partition_repair": {
            "subcommand": "partition-repair",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "recover_partition_repair": {
            "script": "recover_partition_repair.py",
            "serial": "--serial",
            "adb_exclusive": True,
            "recovery": True,
            "recovery_for": [
                "device_manager.partition_repair",
                "device_manager.recover_partition_repair",
            ],
            "outputs": ["--output"],
            "default_outputs": {"--output": "recovery-result.json"},
            "result_file": ".",
        },
        "bootloader_state": {
            "subcommand": "bootloader-state",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "research": {
            "subcommand": "research",
            "readonly": False,
            "outputs": ["--output", "--cache"],
            "shared_outputs": ["--cache"],
            "default_outputs": {"--output": "operation-result.json"},
            "default_paths": {"--cache": "device-manager/research-cache.json"},
        },
        "image_download": {
            "subcommand": "image-download",
            "readonly": False,
            "outputs": ["--output", "--cache-dir"],
            "shared_outputs": ["--cache-dir"],
            "default_outputs": {"--output": "operation-result.json"},
            "default_paths": {"--cache-dir": "device-manager/images"},
        },
        "recover_image_cache": {
            "script": "recover_image_cache.py",
            "readonly": False,
            "mutates_environment": True,
            "recovery": True,
            "recovery_for": [
                "device_manager.image_download",
                "device_manager.recover_image_cache",
            ],
            "resources": [{"path": "device-manager/images", "mode": "write"}],
            "outputs": ["--output"],
            "default_outputs": {"--output": "recovery-result.json"},
            "result_file": ".",
        },
        "image_verify": {
            "subcommand": "image-verify",
            "readonly": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "flash": {
            "subcommand": "flash",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "flash_factory": {
            "subcommand": "flash-factory",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "recover_flash_factory": {
            "script": "recover_flash_factory.py",
            "serial": "--serial",
            "adb_exclusive": True,
            "recovery": True,
            "recovery_for": [
                "device_manager.flash_factory",
                "device_manager.recover_flash_factory",
            ],
            "outputs": ["--output"],
            "default_outputs": {"--output": "recovery-result.json"},
            "result_file": ".",
        },
        "install_rom": {
            "subcommand": "install-rom",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "recover_install_rom": {
            "script": "recover_install_rom.py",
            "serial": "--serial",
            "recovery": True,
            "recovery_for": [
                "device_manager.install_rom",
                "device_manager.recover_install_rom",
            ],
            "outputs": ["--output"],
            "default_outputs": {"--output": "recovery-result.json"},
            "result_file": ".",
        },
        "root": {
            "subcommand": "root",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "root_prepare": {
            "subcommand": "root-prepare",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "root_collect": {
            "subcommand": "root-collect",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output", "--destination"],
            "default_outputs": {"--output": "operation-result.json"},
        },
        "install_frida": {
            "subcommand": "install-frida",
            "serial": "--serial",
            "adb_exclusive": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "operation-result.json"},
        },
    }
    for name, entry in device_manager_definitions.items():
        script = entry.get("script", "device_manager.py")
        add(
            "device_manager." + name,
            device_manager,
            script,
            **{key: value for key, value in entry.items() if key != "script"},
        )
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
        add("frida." + name, "frida", name + ".py", **entry)
    for name in ("setup", "mcp_setup"):
        add(
            "environment." + name,
            "static-env",
            name + ".py",
            mutates_environment=True,
            **({"uses_mcp": True} if name == "mcp_setup" else {}),
        )
    operations["environment.recover_host_lease"] = {
        "script": BUNDLE_PREFIX + "scripts/recover_host_lease.py",
        "source": BUNDLE_PREFIX + "scripts",
        "readonly": True,
        "mutates_environment": True,
        "recovery": True,
        "recovery_for": ["host.lease", "project.recover_host_lease", "environment.recover_host_lease"],
        "outputs": ["--output"],
        "default_outputs": {"--output": "host-lease-recovery"},
        "result_file": "result.json",
    }
    for name in ("smoke", "mcp_smoke", "mcp_probe", "inspect_so"):
        add(
            "static." + name,
            "static-env",
            name + ".py",
            readonly=True,
            compute=True,
            **({"uses_mcp": True} if name in ("mcp_smoke", "mcp_probe") else {}),
        )
    operations["static.inspect_so"].update(
        python=".android-static/venvs/native-python/bin/python", outputs=["--output"]
    )
    add(
        "static.permission_audit",
        "permission-audit",
        "scan_permissions.py",
        readonly=True,
        compute=True,
        outputs=["--output-dir"],
        default_outputs={"--output-dir": "permission-audit"},
        result_file="operation-result.json",
        input_flags=["--source-dir", "--jadx"],
        positional_inputs=True,
        value_flags=["--apk", "--output-dir", "--source-dir", "--aapt2", "--jadx", "--max-hits"],
    )

    analysis = "analysis-agent"
    analysis_definitions = {
        "route": {
            "subcommand": "route",
            "readonly": True,
            "outputs": ["--output"],
        },
        "overview": {
            "subcommand": "overview",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "files": {
            "subcommand": "files",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "entry_points": {
            "subcommand": "entry-points",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "manifest": {
            "subcommand": "manifest",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "resources": {
            "subcommand": "resources",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "preview": {
            "subcommand": "preview",
            "readonly": True,
            "compute": True,
            "outputs": ["--output", "--extract"],
        },
        "decompile": {
            "subcommand": "decompile",
            "readonly": False,
            "compute": True,
            "outputs": ["--output", "--output-dir"],
        },
        "code": {
            "subcommand": "code",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "signature": {
            "subcommand": "signature",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "strings": {
            "subcommand": "strings",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "snapshot": {
            "subcommand": "snapshot",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "knowledge": {
            "subcommand": "knowledge",
            "readonly": False,
            "compute": True,
            "outputs": ["--index", "--output"],
            "shared_outputs": ["--index"],
        },
        "python": {
            "subcommand": "python",
            "host_execution": True,
            "readonly": False,
            "compute": True,
            "input_flags": ["--script", "--read-path"],
            "outputs": ["--output", "--save-session", "--write-path"],
            "shared_outputs": ["--save-session", "--write-path"],
        },
        "exec": {
            "subcommand": "exec",
            "host_execution": True,
            "readonly": False,
            "compute": True,
            "input_flags": ["--read-path"],
            "outputs": ["--output", "--write-path"],
            "shared_outputs": ["--write-path"],
        },
        "scratchpad": {
            "subcommand": "scratchpad",
            "readonly": True,
            "outputs": ["--output", "--path"],
            "shared_outputs": ["--path"],
        },
    }
    for name, entry in analysis_definitions.items():
        add("analysis." + name, analysis, "analysis.py", **entry)

    apkrev = "apk-reverse"

    def add_apkrev(name, script, **entry):
        source = SKILL_SOURCE + "/" + COMPONENTS[apkrev]
        operations["apkrev." + name] = {
            "script": source + "/scripts/" + script,
            "source": source,
            **entry,
        }

    apkrev_definitions = {
        "doctor": {
            "script": "doctor.py",
            "serial": "--device",
            "readonly": True,
        },
        "capabilities": {
            "script": "capabilities.py",
            "readonly": True,
        },
        "device_shell": {
            "script": "device_shell.py",
            "readonly": True,
            "compute": True,
        },
        "preflight": {
            "script": "preflight.py",
            "serial": "--serial",
            "readonly": True,
        },
        "apk_diff": {
            "script": "apk_diff.py",
            "readonly": True,
            "compute": True,
        },
        "blob_decode": {
            "script": "blob_decode.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "coldstart": {
            "script": "coldstart.py",
            "serial": "--serial",
            "outputs": ["--out"],
        },
        "dart_disasm": {
            "script": "dart_disasm.py",
            "python": ".android-static/venvs/native-python/bin/python",
            "readonly": True,
            "compute": True,
        },
        "dart_pool_strings": {
            "script": "dart_pool_strings.py",
            "readonly": True,
            "compute": True,
            "output_positions": [1],
            "shared_output_positions": True,
        },
        "dart_pprefs": {
            "script": "dart_pprefs.py",
            "readonly": True,
            "compute": True,
            "output_positions": [1],
            "shared_output_positions": True,
        },
        "datastore_inject": {
            "script": "datastore_inject.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--file"],
        },
        "devsh": {
            "script": "devsh.py",
            "serial": "--serial",
            "adb_exclusive": True,
        },
        "dex_check_verifier": {
            "script": "dex_check_verifier.py",
            "readonly": True,
            "compute": True,
        },
        "dex_classdiff": {
            "script": "dex_classdiff.py",
            "readonly": True,
            "compute": True,
        },
        "dex_dump_validate": {
            "script": "dex_dump_validate.py",
            "readonly": True,
            "compute": True,
        },
        "dexutil": {
            "script": "dexutil.py",
            "readonly": True,
            "compute": True,
        },
        "dex_find_insn": {
            "script": "dex_find_insn.py",
            "readonly": True,
            "compute": True,
        },
        "dex_method_patch": {
            "script": "dex_method_patch.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--output"],
        },
        "dex_mem_scan": {
            "script": "dex_mem_scan.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--dump"],
        },
        "dex_patch_bytes": {
            "script": "dex_patch_bytes.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--out", "--report"],
        },
        "dex_strings": {
            "script": "dex_strings.py",
            "readonly": True,
            "compute": True,
        },
        "dex_strpatch": {
            "script": "dex_strpatch.py",
            "readonly": True,
            "compute": True,
            "output_positions": [1],
        },
        "elf_plt": {
            "script": "elf_plt.py",
            "readonly": True,
            "compute": True,
        },
        "find_refs": {
            "script": "find_refs.py",
            "readonly": True,
            "compute": True,
        },
        "frida_rpc_serve": {
            "script": "frida_rpc_serve.py",
            "serial": "--device-serial",
        },
        "grab_crash": {
            "script": "grab_crash.py",
            "serial": "--serial",
            "readonly": True,
        },
        "install_test": {
            "script": "install_test.py",
            "serial": "--serial",
            "outputs": ["--shots"],
        },
        "java2c_probe": {
            "script": "java2c_probe.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--dir"],
        },
        "lib_map": {
            "script": "lib_map.py",
            "serial": "--serial",
            "readonly": True,
        },
        "lsposed_scaffold": {
            "script": "lsposed_scaffold.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "mt_mcp_probe": {
            "script": "mt_mcp_probe.py",
            "readonly": True,
        },
        "native_crash": {
            "script": "native_crash.py",
            "python": ".android-static/venvs/native-python/bin/python",
            "readonly": True,
            "compute": True,
        },
        "patch_smali": {
            "script": "patch_smali.py",
            "readonly": False,
            "compute": True,
        },
        "probe_api": {
            "script": "probe_api.py",
            "readonly": True,
            "compute": True,
        },
        "protobuf_decode": {
            "script": "protobuf_decode_raw.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "repack": {
            "script": "repack.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--out", "--split-out-dir", "--workdir"],
        },
        "run_probe": {
            "script": "run_probe.py",
            "serial": "--device",
            "outputs": ["--log"],
        },
        "scan_leaks": {
            "script": "scan_leaks.py",
            "readonly": True,
            "compute": True,
        },
        "sig_probe": {
            "script": "sig_probe.py",
            "serial": "--serial",
            "readonly": True,
        },
        "smali_disasm": {
            "script": "smtool.py",
            "subcommand": "d",
            "readonly": True,
            "compute": True,
            "output_positions": [2],
        },
        "smali_assemble": {
            "script": "smtool.py",
            "subcommand": "a",
            "readonly": False,
            "compute": True,
            "output_positions": [2],
        },
        "snap": {
            "script": "snap.py",
            "serial": "--serial",
            "readonly": True,
            "outputs": ["--out"],
        },
        "so_constpatch": {
            "script": "so_constpatch.py",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "spawn_patch_detach": {
            "script": "spawn_patch_detach.py",
            "serial": "--serial",
            "outputs": ["--out-prefix"],
        },
        "stalker_report": {
            "script": "stalker_report.py",
            "readonly": True,
            "compute": True,
        },
        "svc_scan": {
            "script": "svc_scan.py",
            "python": ".android-static/venvs/native-python/bin/python",
            "readonly": True,
            "compute": True,
        },
        "tls_check": {
            "script": "tls_check.py",
            "readonly": True,
            "compute": True,
        },
        "usb_net_proxy": {
            "script": "usb_net_proxy.py",
            "readonly": True,
            "compute": True,
            "output_positions": [1],
        },
        "kernel_gates": {
            "script": "kernelsu_syscall_mask.py",
            "subcommand": "gates",
            "readonly": True,
        },
        "kernel_generate": {
            "script": "kernelsu_syscall_mask.py",
            "subcommand": "generate",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "kernel_verify": {
            "script": "kernelsu_syscall_mask.py",
            "subcommand": "verify",
            "readonly": True,
            "compute": True,
        },
        "rasc_build": {
            "script": "rasc_build.py",
            "mutates_environment": True,
            "compute": True,
            "outputs": ["--work"],
        },
        "vmp_audit": {
            "script": "vmp_diff_harness.py",
            "subcommand": "audit",
            "readonly": True,
            "compute": True,
        },
        "vmp_build": {
            "script": "vmp_diff_harness.py",
            "subcommand": "build",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "vmp_compare": {
            "script": "vmp_diff_harness.py",
            "subcommand": "compare",
            "readonly": True,
            "compute": True,
        },
        "vmp_emit_smali": {
            "script": "vmp_diff_harness.py",
            "subcommand": "emit-smali",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
        "vmp_simulate": {
            "script": "vmp_diff_harness.py",
            "subcommand": "simulate",
            "readonly": True,
            "compute": True,
            "outputs": ["--out"],
        },
    }
    for name, entry in apkrev_definitions.items():
        add_apkrev(name, **entry)
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
        "skills": {SKILL: SKILL_SOURCE},
        "operations": builtin_operations(),
    }
    bundle_root(root, doc)
    for entry in doc["operations"].values():
        if not registered_path(root, doc, entry["script"]).is_file():
            raise WorkbenchError("Bundled operation missing: " + entry["script"])
        if not set(entry.get("shared_outputs", [])).issubset(
            entry.get("outputs", [])
        ):
            raise WorkbenchError(
                "Bundled operation has shared_outputs outside outputs: "
                + entry["script"]
            )
    return doc


def device_management_manifest(root, checkout, python=None):
    """A project-free manifest for shared phone management operations."""
    document = generate(root, checkout, python)
    document["operations"] = {
        name: entry
        for name, entry in document["operations"].items()
        if name.startswith("device_manager.")
    }
    return document


def update(existing, generated, python=None):
    """Rebind built-ins while retaining custom operations and project environments."""
    if existing.get("version") != 1:
        raise WorkbenchError("Unsupported project manifest")
    result = copy.deepcopy(existing)
    result["workbench_root"] = generated["workbench_root"]
    if python or "python" not in result:
        result["python"] = generated["python"]
    old_skills = existing.get("skills", {})
    result["skills"] = {
        name: location
        for name, location in result.get("skills", {}).items()
        if name not in LEGACY_SKILLS
    }
    result["skills"].update(generated["skills"])
    result.setdefault("operations", {}).update(generated["operations"])
    # Preserve nested resource coverage when an older wrapper calls a bundled component.
    replacements = {
        location: generated["skills"][SKILL]
        for name in LEGACY_SKILLS
        for location in (old_skills.get(name), BUNDLE_PREFIX + "skills/" + name)
        if location
    }
    for entry in result["operations"].values():
        if "nested_sources" in entry:
            entry["nested_sources"] = [
                replacements.get(source, source) for source in entry["nested_sources"]
            ]
    return result
