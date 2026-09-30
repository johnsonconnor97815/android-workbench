"""Exercise legacy routing and the permission collector through registered jobs."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.bridge import entry
from workbench.client import Client
from workbench.common import WorkbenchError, atomic_json, conflict, path_resource, rpc
from workbench.project import generate
from workbench.registry import normalize
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


class SkillIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-skill-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.manifest = generate(self.project, ROOT, sys.executable)
        self.save_manifest()

    def save_manifest(self):
        atomic_json(self.project / "workbench.project.json", self.manifest)

    def test_old_skill_entries_route_to_registered_components(self):
        client = Mock()
        client.submit.return_value = {"id": "offline-job"}
        client.wait.return_value = {
            "directory": str(self.root / "job"),
            "state": "succeeded",
            "result": {"exit_code": 0},
        }
        cases = (
            ("android-static-env", "static-env", "setup.py", ["plan"], "environment.setup", None),
            ("pull-android-apk", "apk-export", "apk_pull.py", ["doctor"], "apk.doctor", None),
            ("frida-modified", "frida", "deploy_server.py", ["--serial", "fake"], "frida.deploy_server", "fake"),
            ("apk-permission-audit", "permission-audit", "scan_permissions.py", ["base.apk"], "static.permission_audit", None),
        )
        for name, component, script, args, operation, device in cases:
            folders = [self.root / "installed" / name,
                       self.root / "installed/skills/android-workbench/components" / component]
            for folder in folders:
                path = folder / "scripts" / script
                path.parent.mkdir(parents=True)
                path.write_text("# installed copy\n")
                with self.subTest(skill=name, layout=str(folder)), patch("workbench.bridge.locate_project", return_value=self.project), patch.dict(os.environ, {"AWB_INTERNAL_GRANT": ""}), patch("workbench.bridge.Client", return_value=client), patch("builtins.print"), self.assertRaises(SystemExit) as stopped:
                    entry(path, args)
                self.assertEqual(stopped.exception.code, 0)
                spec = client.submit.call_args.kwargs
                self.assertEqual(spec["operation"], operation)
                self.assertEqual(spec["args"], args)
                self.assertEqual(spec.get("device"), device)

    def test_legacy_alias_does_not_authorize_unregistered_script(self):
        path = self.root / "pull-android-apk/scripts/android_tools.py"
        path.parent.mkdir(parents=True)
        path.write_text("# not an entrypoint\n")
        with patch("workbench.bridge.locate_project", return_value=self.project), patch.dict(os.environ, {"AWB_INTERNAL_GRANT": ""}), patch("workbench.bridge.Client") as client, self.assertRaisesRegex(WorkbenchError, "no unique registered adapter"):
            entry(path, [])
        client.assert_not_called()

    def test_host_execution_locks_implicit_project_writes_and_declared_external_paths(self):
        working = self.root / "external-workspace"
        working.mkdir()
        source = self.root / "input.txt"
        source.write_text("input")
        target = self.root / "shared-report.txt"
        target.write_text("existing report")
        script = self.root / "analysis-task.py"
        script.write_text("print('offline')")
        specs = []
        for operation, args in (
            ("analysis.exec", ["exec", "--command", "printf review > shared-report.txt"]),
            ("analysis.python", ["python", "--script", str(script)]),
        ):
            folder = self.root / operation
            folder.mkdir()
            spec = normalize({"project": str(self.project), "operation": operation,
                              "args": [*args, "--cwd", str(working), "--read-path", str(source), "--write-path", str(target)]}, DEFAULTS, folder)
            for path in (self.project, working, target):
                self.assertIn({"key": path_resource(path), "mode": "write"}, spec["resources"])
            self.assertIn({"key": path_resource(source), "mode": "read"}, spec["resources"])
            self.assertEqual(spec["input_fingerprints"][str(source)], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertNotIn("device", spec)
            self.assertNotIn(str(target), spec["outputs"])
            specs.append(spec)
        self.assertTrue(conflict(specs[0]["resources"], specs[1]["resources"]))
        self.assertIn(str(script), specs[1]["input_fingerprints"])
        other_project = self.root / "other-project"
        other_project.mkdir()
        atomic_json(other_project / "workbench.project.json", generate(other_project, ROOT, sys.executable))
        other = normalize({"project": str(other_project), "operation": "analysis.scratchpad",
                           "args": ["scratchpad", "--path", str(target), "--text", "other"]}, DEFAULTS, self.root / "other-job")
        self.assertTrue(conflict(specs[0]["resources"], other["resources"]))

    def test_old_host_adapter_registry_cannot_omit_current_resource_locks(self):
        for operation in ("analysis.exec", "analysis.python"):
            entry = self.manifest["operations"][operation]
            entry.pop("host_execution")
            entry.pop("input_flags")
            entry["outputs"].remove("--write-path")
            entry["shared_outputs"].remove("--write-path")
        self.save_manifest()
        target = self.root / "external-result.txt"
        spec = normalize({"project": str(self.project), "operation": "analysis.exec",
                          "args": ["exec", "--command", "printf result", "--write-path", str(target)]}, DEFAULTS, self.root / "old-adapter-job")
        for path in (self.project, target):
            self.assertIn({"key": path_resource(path), "mode": "write"}, spec["resources"])

    def prepare_collector(self):
        tools = self.project / "tools"
        tools.mkdir()
        aapt2 = tools / "aapt2"
        fixture = {
            "version": "Android Asset Packaging Tool (aapt) 2:test",
            "badging": "package: name='com.example.app' versionCode='1' versionName='1.0'\nsdkVersion:'23'\ntargetSdkVersion:'35'",
            "permissions": "uses-permission: name='android.permission.CAMERA'",
            "xmltree": 'E: manifest (line=1)\n  E: uses-permission (line=2)\n    A: android:name(0x01010003)="android.permission.CAMERA"\n  E: application (line=3)\n    E: activity (line=4)\n      A: android:name(0x01010003)="com.example.app.Main"\n',
        }
        aapt2.write_text(
            "#!" + sys.executable + "\nimport sys\n"
            "kind = sys.argv[2] if sys.argv[1] == 'dump' else 'version'\n"
            "data = " + repr(fixture) + "\nprint(data[kind])\n"
        )
        aapt2.chmod(0o755)
        sources = self.project / "existing sources"
        sources.mkdir()
        (sources / "Main.java").write_text(
            "package com.example.app;\n"
            "class Main { void ask() { requestPermissions(new String[] {\"android.permission.CAMERA\"}, 1); } }\n"
        )
        apks = [self.project / name for name in ("base.apk", "split_config.apk")]
        for i, apk in enumerate(apks):
            apk.write_bytes(b"fixture apk " + bytes([i]))
        args = ["--aapt2", str(aapt2), "--source-dir=" + str(sources), "--max-hits", "2", *map(str, apks)]
        return apks, sources, args

    def test_permission_inputs_are_hashed_and_locked_without_phone(self):
        apks, sources, args = self.prepare_collector()
        directory = self.root / "normalization"
        directory.mkdir()
        spec = normalize({"project": str(self.project), "operation": "static.permission_audit", "args": args}, DEFAULTS, directory)
        self.assertNotIn("device", spec)
        for apk in apks:
            self.assertEqual(spec["input_fingerprints"][str(apk)], hashlib.sha256(apk.read_bytes()).hexdigest())
            self.assertIn({"key": path_resource(apk), "mode": "read"}, spec["resources"])
        self.assertIn({"key": path_resource(sources), "mode": "read"}, spec["resources"])
        self.assertIn({"key": path_resource(directory / "permission-audit"), "mode": "write"}, spec["resources"])
        self.assertEqual(spec["outputs"], [str(directory / "permission-audit")])

    def test_permission_default_output_precedes_positional_separator(self):
        apks, _, _ = self.prepare_collector()
        directory = self.root / "separator"
        directory.mkdir()
        spec = normalize({"project": str(self.project), "operation": "static.permission_audit", "args": ["--no-decompile", "--", *map(str, apks)]}, DEFAULTS, directory)
        separator = spec["args"].index("--")
        self.assertLess(spec["args"].index("--output-dir"), separator)
        self.assertEqual(spec["args"][separator + 1:], list(map(str, apks)))

    def test_permission_job_collects_split_and_code_evidence(self):
        apks, sources, args = self.prepare_collector()
        original_hashes = [hashlib.sha256(apk.read_bytes()).hexdigest() for apk in apks]
        state = self.root / "state"
        state.mkdir()
        atomic_json(state / "config.json", {**DEFAULTS, "min_free_bytes": 0})
        with (self.root / "service.log").open("wb") as log:
            service = subprocess.Popen([sys.executable, str(ROOT / "scripts/workbench.py"), "--state", str(state), "serve"], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        rpc(state, "service.capabilities")
                        break
                    except WorkbenchError:
                        time.sleep(0.03)
                else:
                    self.fail("Shared test service did not start")
                client = Client(self.project, state, "permission-test")
                scripts = self.project / "fixture-scripts"
                scripts.mkdir()
                (scripts / "produce.py").write_text(
                    "import pathlib,sys\n"
                    "out=pathlib.Path(sys.argv[2]);out.mkdir(exist_ok=True)\n"
                    "for i,name in enumerate(['base.apk','split_config.apk']):\n"
                    " (out/name).write_bytes(b'fixture apk '+bytes([i]))\n"
                )
                self.manifest["operations"]["fixture.produce"] = {
                    "script": "fixture-scripts/produce.py", "source": "fixture-scripts",
                    "readonly": True, "outputs": ["--output-dir"],
                }
                self.save_manifest()
                producer = client.submit(
                    operation="fixture.produce", args=["--output-dir", str(self.project)],
                    artifact_outputs={"fixture-base-apk": str(apks[0]), "fixture-split-apk": str(apks[1])},
                )
                self.assertEqual(client.wait(producer["id"], 20)["state"], "succeeded")
                job = client.submit(
                    operation="static.permission_audit", args=args[:-2], timeout=30,
                    artifact_inputs={"fixture-base-apk": "--apk", "fixture-split-apk": "--apk"},
                )
                result = client.wait(job["id"], timeout=40)
                self.assertEqual(result["state"], "succeeded", result.get("result"))
                output = Path(result["directory"]) / "permission-audit"
                inventory = json.loads((output / "permissions-inventory.json").read_text())
                self.assertEqual([item["sha256"] for item in inventory["inputs"]], original_hashes)
                self.assertEqual(inventory["app"]["packageName"], "com.example.app")
                self.assertEqual(inventory["permissions"][0]["sourceApks"], ["base.apk", "split_config.apk"])
                self.assertEqual(inventory["permissions"][0]["codeEvidence"]["status"], "searched")
                self.assertTrue(inventory["permissions"][0]["codeEvidence"]["matches"])
                self.assertEqual(inventory["decompilation"]["sourceRoot"], str(sources))
                self.assertTrue((output / "permissions-evidence.md").is_file())
                self.assertTrue((output / "permissions-report.draft.md").is_file())
                collection = json.loads((output / "operation-result.json").read_text())
                self.assertTrue(collection["pass"])
                self.assertTrue(collection["review_required"])
                self.assertEqual([hashlib.sha256(apk.read_bytes()).hexdigest() for apk in apks], original_hashes)
                self.assertTrue(result["result"]["cleanup_ok"])
            finally:
                service.terminate()
                service.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
