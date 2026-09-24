"""Exercise relocation, clean initialization and bundled execution without old repositories."""

import json
import os
import ast
import re
import shlex
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.client import Client
from workbench.common import WorkbenchError, process_identity, rpc, same_process
from workbench.project import generate, registered_path, update
from workbench.registry import normalize
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


class ProjectSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-project-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.manifest = generate(self.project, ROOT, sys.executable)

    def test_registered_roots_reject_escape_and_symlink(self):
        for value in ("../outside.py", "@workbench/../outside.py", "/etc/passwd"):
            with self.subTest(value=value), self.assertRaises(WorkbenchError):
                registered_path(self.project, self.manifest, value)
        (self.project / "external").symlink_to(ROOT, target_is_directory=True)
        with self.assertRaises(WorkbenchError):
            registered_path(
                self.project, self.manifest, "external/workbench/service.py"
            )
        resolved = registered_path(
            self.project,
            self.manifest,
            "@workbench/skills/android-workbench/components/apk-export/scripts/apk_pull.py",
        )
        self.assertEqual(
            resolved,
            ROOT / "skills/android-workbench/components/apk-export/scripts/apk_pull.py",
        )

    def test_update_keeps_project_extensions_and_environment(self):
        old = {
            "version": 1,
            "python": "/project/python",
            "env": {"CUSTOM": "keep"},
            "skills": {"android-static-env": "old/static", "custom": "local-skill"},
            "operations": {
                "custom": {
                    "script": "scripts/custom.py",
                    "source": "scripts",
                    "nested_sources": ["old/static", "local-skill"],
                }
            },
        }
        before = json.dumps(old, sort_keys=True)
        actual = update(old, self.manifest)
        self.assertEqual(actual["python"], "/project/python")
        self.assertEqual(actual["env"], old["env"])
        self.assertEqual(actual["skills"]["custom"], "local-skill")
        self.assertNotIn("android-static-env", actual["skills"])
        self.assertEqual(
            actual["skills"]["android-workbench"],
            "@workbench/skills/android-workbench",
        )
        self.assertEqual(
            actual["operations"]["custom"]["nested_sources"],
            ["@workbench/skills/android-workbench", "local-skill"],
        )
        self.assertEqual(json.dumps(old, sort_keys=True), before)

    def test_default_output_remains_inside_evidence_directory(self):
        scripts = self.project / "scripts"
        scripts.mkdir()
        (scripts / "inspect.py").write_text("print('extension')\n")
        self.manifest["operations"]["custom"] = {
            "script": "scripts/inspect.py",
            "source": "scripts",
            "readonly": True,
            "outputs": ["--output"],
            "default_outputs": {"--output": "result"},
        }
        target = self.project / "workbench.project.json"
        target.write_text(json.dumps(self.manifest))
        directory = self.project / "evidence/job"
        directory.mkdir(parents=True)
        spec = normalize(
            {"project": str(self.project), "operation": "custom"}, DEFAULTS, directory
        )
        self.assertEqual(spec["args"], ["--output", str(directory / "result")])
        self.manifest["operations"]["custom"]["default_outputs"]["--output"] = (
            "../escape"
        )
        target.write_text(json.dumps(self.manifest))
        with self.assertRaises(WorkbenchError):
            normalize(
                {"project": str(self.project), "operation": "custom"},
                DEFAULTS,
                directory,
            )

    def test_shared_output_flag_must_be_declared_as_output(self):
        scripts = self.project / "scripts"
        scripts.mkdir()
        (scripts / "inspect.py").write_text("print('extension')\n")
        self.manifest["operations"]["custom"] = {
            "script": "scripts/inspect.py",
            "source": "scripts",
            "readonly": True,
            "outputs": ["--output"],
            "shared_outputs": ["--state"],
        }
        (self.project / "workbench.project.json").write_text(json.dumps(self.manifest))
        directory = self.project / "evidence/job"
        directory.mkdir(parents=True)
        with self.assertRaisesRegex(
            WorkbenchError, "shared_outputs must be a subset of outputs"
        ):
            normalize(
                {"project": str(self.project), "operation": "custom"},
                DEFAULTS,
                directory,
            )

    def test_apk_reverse_component_registers_cli_operations(self):
        names = [name for name in self.manifest["operations"] if name.startswith("apkrev.")]
        self.assertEqual(len(names), 57)
        for name in (
            "apkrev.preflight",
            "apkrev.repack",
            "apkrev.vmp_compare",
            "apkrev.devsh",
            "apkrev.device_shell",
            "apkrev.dexutil",
            "apkrev.dex_strpatch",
            "apkrev.dex_method_patch",
            "apkrev.patch_smali",
            "apkrev.smali_disasm",
            "apkrev.smali_assemble",
            "apkrev.usb_net_proxy",
        ):
            self.assertIn(name, names)
        for name in names:
            entry = self.manifest["operations"][name]
            self.assertIn("components/apk-reverse", entry["script"])
        registered_scripts = {
            Path(self.manifest["operations"][name]["script"]).name for name in names
        }
        component_scripts = {
            path.name
            for path in (
                ROOT
                / "skills/android-workbench/components/apk-reverse/scripts"
            ).glob("*.py")
        }
        self.assertEqual(registered_scripts, component_scripts)
        component_doc = (
            ROOT / "skills/android-workbench/components/apk-reverse/README.md"
        ).read_text()
        routing_doc = (
            ROOT / "skills/android-workbench/references/apk-reverse.md"
        ).read_text()
        for name in names:
            short_name = name[len("apkrev.") :]
            self.assertTrue(
                short_name in component_doc or short_name in routing_doc,
                f"Undocumented apk-reverse operation: {name}",
            )

    def test_apk_reverse_documented_commands_match_scripts(self):
        component = ROOT / "skills/android-workbench/components/apk-reverse"
        command_re = re.compile(
            r"^[ \t]*(?:\$[ \t]+)?python(?:3)?(?:\.exe)?[ \t]+"
            r"(?P<command>[^\n\\]+(?:\\[ \t]*\n[^\n\\]+)*)",
            re.MULTILINE,
        )
        failures = []
        checked = 0
        for document in sorted(component.rglob("*.md")):
            text = document.read_text()
            for match in command_re.finditer(text):
                command = re.sub(r"\\[ \t]*\n", " ", match.group("command"))
                tokens = shlex.split(command, comments=True)
                if not tokens:
                    continue
                named = tokens[0]
                if named == "<Workbench目录>/scripts/workbench.py":
                    continue
                if named.startswith("skills/apk-reverse/scripts/"):
                    failures.append(
                        f"{document.relative_to(ROOT)}: upstream layout path {named}"
                    )
                    continue

                candidates = []
                if named.startswith("scripts/"):
                    candidates.append(component / named)
                elif named.startswith("./") or named.startswith("../"):
                    candidates.append(document.parent / named)
                elif "/" not in named:
                    candidates.extend(
                        (document.parent / named, component / "scripts" / named)
                    )
                else:
                    continue
                script = next((path for path in candidates if path.is_file()), None)
                if script is None:
                    if named.startswith("scripts/") or named.startswith(("./", "../")):
                        failures.append(
                            f"{document.relative_to(ROOT)}: missing script {named}"
                        )
                    continue

                arguments = self.apk_reverse_argument_table(script)
                if not arguments:
                    continue
                checked += 1
                for token in tokens[1:]:
                    if token == "--":
                        break
                    if not token.startswith("-") or token.startswith("<"):
                        continue
                    if token.startswith("--"):
                        flags = [token.split("=", 1)[0]]
                    elif len(token) > 2:
                        flags = ["-" + character for character in token[1:]]
                    else:
                        flags = [token]
                    for flag in flags:
                        matches = [name for name in arguments if name.startswith(flag)]
                        if flag not in arguments and len(matches) != 1:
                            failures.append(
                                f"{document.relative_to(ROOT)}: {named} has no flag "
                                f"{flag} in {script.name}"
                            )
        self.assertEqual(failures, [])
        self.assertGreater(checked, 20)

    @staticmethod
    def apk_reverse_argument_table(script):
        tree = ast.parse(script.read_text(), filename=str(script))
        arguments = {}
        no_value = {"store_true", "store_false", "store_const", "count"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "add_argument":
                continue
            names = [
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
            if not any(name.startswith("-") for name in names):
                continue
            action = None
            nargs = None
            for keyword in node.keywords:
                if keyword.arg == "action":
                    action = (
                        keyword.value.value
                        if isinstance(keyword.value, ast.Constant)
                        else getattr(keyword.value, "attr", None)
                        or getattr(keyword.value, "id", None)
                    )
                elif keyword.arg == "nargs":
                    nargs = (
                        keyword.value.value
                        if isinstance(keyword.value, ast.Constant)
                        else getattr(keyword.value, "attr", None)
                        or getattr(keyword.value, "id", None)
                    )
            takes_value = action not in no_value and nargs not in {"?", "*", 0}
            for name in names:
                arguments[name] = takes_value
        return arguments


class UpgradeRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-upgrade-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def load_upgrade_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "upgrade_runtime_test", ROOT / "scripts/upgrade_runtime.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_upgrade(self, module, state, configure_side_effect=None):
        class FakeClient:
            def __init__(self, project):
                self.project = project
                self.calls = []

            def call(self, method):
                self.calls.append(method)
                if method == "service.drain":
                    return {"can_stop": True, "active": []}
                if method == "jobs.list":
                    return []
                if method == "service.resume":
                    return None
                raise AssertionError("unexpected client call: " + method)

        client = FakeClient(self.project)
        runtime = state / "runtime"
        runtime.mkdir(parents=True)
        current = {
            "fingerprint": "test",
            "command": [sys.executable, str(runtime / "run.py")],
        }
        (state / "current-runtime.json").write_text(json.dumps(current))
        commands = []

        def fake_subprocess_run(command, check=False):
            commands.append((tuple(command), check))
            if configure_side_effect is not None and command[1].endswith(
                "configure_project.py"
            ):
                raise configure_side_effect
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(sys, "argv", ["upgrade_runtime.py", "--project", str(self.project)]),
            mock.patch.object(module, "Client", return_value=client),
            mock.patch.object(
                module,
                "rpc",
                return_value={"pid": 4321, "runtime_source": str(runtime)},
            ),
            mock.patch.object(module, "state_dir", return_value=state),
            mock.patch.object(module, "process_identity", return_value={"pid": 4321}),
            mock.patch.object(module, "same_process", return_value=False),
            mock.patch.object(module.os, "kill"),
            mock.patch.object(module.subprocess, "run", side_effect=fake_subprocess_run),
            mock.patch.object(module, "start", return_value={"started": True}),
        ):
            try:
                module.main()
            finally:
                self.last_client = client
                self.last_commands = commands
        return client, commands

    def test_idle_upgrade_rebinds_project_without_resuming(self):
        module = self.load_upgrade_module()
        state = self.temp.name and Path(self.temp.name) / "state"
        client, commands = self.run_upgrade(module, state)

        self.assertNotIn("service.resume", client.calls)
        self.assertEqual(
            commands[0],
            (
                (
                    sys.executable,
                    str(ROOT / "scripts/configure_project.py"),
                    str(self.project),
                    "--update",
                ),
                True,
            ),
        )
        self.assertEqual(commands[1][0][1], str(ROOT / "scripts/install_runtime.py"))
        self.assertEqual(commands[1][1], True)

    def test_configure_failure_resumes_service_before_stopping_it(self):
        module = self.load_upgrade_module()
        state = self.project / "failed-state"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_upgrade(
                module,
                state,
                configure_side_effect=subprocess.CalledProcessError(2, "configure"),
            )

        client = self.last_client
        commands = self.last_commands
        self.assertIn("service.resume", client.calls)
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][0][1], str(ROOT / "scripts/configure_project.py")
        )


class StandaloneInstallTest(unittest.TestCase):
    def test_archive_bootstrap_managed_entry_and_mcp(self):
        with tempfile.TemporaryDirectory(prefix="awb-alone-") as folder:
            base = Path(folder)
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in {
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "ANDROID_HOME",
                    "ANDROID_SDK_ROOT",
                    "AWB_INTERNAL_GRANT",
                }
            }
            state = base / "state"
            env.update(
                {
                    "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                    "ANDROID_WORKBENCH_STATE": str(state),
                    "ANDROID_WORKBENCH_SESSION": "standalone",
                }
            )
            archive = base / "release.zip"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build_plugin.py"),
                    "--output",
                    str(archive),
                ],
                env=env,
                cwd=base,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            with zipfile.ZipFile(archive) as release:
                self.assertFalse(
                    any("/.git/" in n or "__pycache__" in n for n in release.namelist())
                )
                release.extractall(base / "unpacked")
            checkout = base / "unpacked/android-workbench"
            # A differently named checkout also works; no sibling source trees exist.
            moved = base / "checkout with spaces"
            checkout.rename(moved)
            project = base / "analysis with spaces"
            identity = None
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(moved / "scripts/bootstrap.py"),
                        "--project",
                        str(project),
                        "--configure-mcp",
                    ],
                    env=env,
                    cwd=base,
                    capture_output=True,
                    text=True,
                    timeout=40,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                capabilities = rpc(state, "service.capabilities")
                identity = process_identity(capabilities["pid"])
                self.assertTrue(
                    Path(capabilities["runtime_source"]).is_relative_to(state)
                )
                manifest = json.loads((project / "workbench.project.json").read_text())
                self.assertEqual(manifest["workbench_root"], str(moved))
                self.assertEqual(len(manifest["operations"]), 92)
                self.assertNotIn(str(ROOT), json.dumps(manifest))
                self.assertFalse((project / "android-workbench").exists())
                self.assertFalse((project / ".venv").exists())
                fake_adb = project / "fake-adb"
                fake_adb.write_text(
                    "#!"
                    + sys.executable
                    + "\nprint('List of devices attached\\nfixture device model:Test')\n"
                )
                fake_adb.chmod(0o755)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(
                            moved
                            / "skills/android-workbench/components/apk-export/scripts/apk_pull.py"
                        ),
                        "devices",
                        "--adb",
                        str(fake_adb),
                    ],
                    cwd=project,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("fixture", result.stdout)
                self.assertIn("Android Workbench job:", result.stderr)
                client = Client(project, state, "standalone")
                jobs = client.call("jobs.list")
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["state"], "succeeded")
                spec = json.loads(
                    (Path(jobs[0]["directory"]) / "normalized.json").read_text()
                )
                self.assertTrue(Path(spec["script"]).is_relative_to(moved))
                self.assertTrue(spec["source_manifest"])

                config = tomllib.loads((project / ".codex/config.toml").read_text())
                connector = config["mcp_servers"]["android-workbench"]
                self.assertEqual(
                    connector["args"][0], str(moved / "scripts/connect.py")
                )
                # Plugin and project connectors both reach the same installed service.
                plugin = json.loads((moved / ".mcp.json").read_text())["mcpServers"][
                    "android-workbench"
                ]
                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"},
                    },
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "operations_list", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "service_capabilities", "arguments": {}},
                    },
                ]
                for entry in (connector, plugin):
                    response = subprocess.run(
                        [entry["command"], *entry["args"]],
                        input="".join(json.dumps(r) + "\n" for r in requests),
                        env=env,
                        cwd=project,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(response.returncode, 0, response.stderr)
                    messages = [
                        json.loads(line) for line in response.stdout.splitlines()
                    ]
                    self.assertEqual(len(messages[1]["result"]["tools"]), 18)
                    self.assertFalse(messages[2]["result"]["isError"])
                    self.assertIn(
                        "apk.pull", messages[2]["result"]["content"][0]["text"]
                    )
                    live = json.loads(messages[3]["result"]["content"][0]["text"])
                    self.assertEqual(live["pid"], capabilities["pid"])
            finally:
                if identity is None:
                    try:
                        identity = process_identity(
                            rpc(state, "service.capabilities")["pid"]
                        )
                    except (WorkbenchError, ProcessLookupError, FileNotFoundError):
                        pass
                if identity and same_process(identity):
                    os.kill(identity["pid"], signal.SIGTERM)
                    deadline = time.monotonic() + 8
                    while same_process(identity) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if same_process(identity):
                        os.kill(identity["pid"], signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
