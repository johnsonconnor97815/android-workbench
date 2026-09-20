"""Exercise relocation, clean initialization and bundled execution without old repositories."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
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
            "@workbench/skills/pull-android-apk/scripts/apk_pull.py",
        )
        self.assertEqual(resolved, ROOT / "skills/pull-android-apk/scripts/apk_pull.py")

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
        self.assertEqual(
            actual["operations"]["custom"]["nested_sources"],
            ["@workbench/skills/android-static-env", "local-skill"],
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
                self.assertEqual(len(manifest["operations"]), 19)
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
                        str(moved / "skills/pull-android-apk/scripts/apk_pull.py"),
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
