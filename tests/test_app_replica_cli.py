"""CLI integration in an arbitrary directory; no model or device invocation."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.app_replica.cli import main
from workbench.app_replica.engine import execute
from workbench.app_replica.runner import Runner
from scripts.install_runtime import runtime_sources
from scripts import check_installation


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workflow"
        project = self.root / "input.json"
        project.write_text(json.dumps({"id": "fixture", "output_path": str(self.root / "output"),
                                       "execution_root": str(self.root)}))
        self.call("init", "--project", str(project), "--capacity", "2")

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--workspace", str(self.workspace), *args])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_initialization_seeds_only_app_and_survives_arbitrary_cwd(self):
        status = self.call("status", "--summary")
        self.assertEqual(status["counts"]["tasks"], 1)
        self.assertEqual(status["counts"]["inventory"], 0)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/app_replica.py"),
                                 "--workspace", str(self.workspace), "native-next", "--external-active", "1"],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        directive = json.loads(result.stdout)["directive"]
        self.assertEqual(directive["arguments"]["model"], "gpt-6-astra")
        self.assertEqual(directive["arguments"]["reasoning_effort"], "xhigh")
        self.assertEqual(directive["arguments"]["fork_turns"], "none")

    def test_content_replay_and_explicit_event_identity(self):
        path = self.root / "request.json"
        request = {"op": "message.send", "type": "requirement", "sender": "main",
                   "recipient": "app-owner", "payload": {"question": "fixture"}, "evidence": ["user"]}
        path.write_text(json.dumps(request))
        first = self.call("apply", "--file", str(path))
        second = self.call("apply", "--file", str(path))
        self.assertEqual(first["message"]["id"], second["message"]["id"])
        self.assertEqual(first["revision"], second["revision"])
        request["request_id"] = "separate-user-event"
        path.write_text(json.dumps(request))
        third = self.call("apply", "--file", str(path))
        self.assertNotEqual(first["message"]["id"], third["message"]["id"])

    def test_idle_dispatch_does_not_write_no_work_receipts(self):
        claim = execute(self.workspace, {"op": "task.claim", "request_id": "claim-fixture", "backend": "native"})
        # This native reservation is managed by the host, not the CLI backend.
        runner = Runner(self.workspace, host_capacity=1, slots_path=self.root / "slots.json")
        before = execute(self.workspace, {"op": "status"})["revision"]
        runner.step()
        runner.step()
        self.assertEqual(execute(self.workspace, {"op": "status"})["revision"], before)
        self.assertTrue(claim["run"])

    def test_unchanged_status_does_not_rewrite_every_projection(self):
        import os
        with patch("workbench.app_replica.store.os.replace", wraps=os.replace) as replace:
            self.call("status", "--summary")
            self.call("status", "--summary")
            self.assertEqual(replace.call_count, 0)
            (self.workspace / "project.json").write_text("damaged view")
            self.call("recover")
            self.assertEqual(replace.call_count, 1)

    def test_claim_output_does_not_overwrite_or_assign_other_writer(self):
        target = self.workspace / "artifacts/app-owner/report.md"
        target.parent.mkdir(parents=True)
        target.write_text("keep user content")
        self.call("claim-output", "--task", "app-owner", "--path", str(target))
        self.assertEqual(target.read_text(), "keep user content")
        status = execute(self.workspace, {"op": "status"})
        self.assertEqual(status["writers"][str(target)], "app-owner")

    def test_submit_attaches_declared_checks_and_can_correct_same_live_run(self):
        task = execute(self.workspace, {"op": "status"})["tasks"]["app-owner"]
        report = Path(task["outputs"][0])
        report.parent.mkdir(parents=True)
        report.write_text("fixture report")
        checks = report.parent / "checks.json"
        checks.write_text(json.dumps([{"name": "fixture", "result": "pass", "evidence": "host assertion"}]))
        self.call("claim-output", "--task", task["id"], "--path", str(checks))
        claim = execute(self.workspace, {"op": "task.claim", "request_id": "claim", "backend": "host_fixture"})
        run_id = claim["run"]["id"]
        execute(self.workspace, {"op": "run.started", "request_id": "started", "run_id": run_id,
                                  "backend_handle": {"fixture": True}})
        first = self.call("submit", "--task", task["id"], "--run", run_id, "--checks", str(checks))
        self.assertFalse(first["delivery"]["accepted"])
        fixed = self.call("submit", "--task", task["id"], "--run", run_id, "--checks", str(checks),
                          "--artifact", str(report))
        self.assertTrue(fixed["delivery"]["accepted"])
        self.assertEqual(len(fixed["task"]["deliveries"]), 2)
        self.assertEqual(len(fixed["delivery"]["artifacts"]), 2)
        self.assertTrue(fixed["stop_pending"])

    def test_recursive_runtime_fingerprint_and_checker_detect_subpackage_change(self):
        source = self.root / "source"
        nested = source / "workbench/app_replica"
        nested.mkdir(parents=True)
        (source / "workbench/__init__.py").write_text("VERSION = 1\n")
        (nested / "engine.py").write_text("VALUE = 1\n")
        files, first = runtime_sources(source)
        self.assertEqual(len(files), 2)
        runtime = self.root / "runtime"
        import shutil
        shutil.copytree(source / "workbench", runtime / "workbench")
        state = self.root / "state"
        state.mkdir()
        (state / "current-runtime.json").write_text(json.dumps({"fingerprint": first,
                                                               "command": ["python", str(runtime / "run.py")]}))
        installed = self.root / "installed"
        installed.mkdir()
        manifest = {"files": {}, "version": "fixture", "fingerprint": "fixture"}
        (installed / "release-manifest.json").write_text(json.dumps(manifest))
        with patch.object(check_installation, "ROOT", source), \
             patch.object(check_installation.build_plugin, "validate", return_value=[]), \
             patch.object(check_installation.build_plugin, "release_manifest", return_value=manifest), \
             patch.object(check_installation, "rpc", return_value={"runtime_source": str(runtime), "version": "fixture"}):
            self.assertTrue(check_installation.compare_installation(installed, state)["pass"])
            (nested / "engine.py").write_text("VALUE = 2\n")
            self.assertNotEqual(first, runtime_sources(source)[1])
            result = check_installation.compare_installation(installed, state)
            self.assertFalse(result["pass"])
            self.assertEqual(result["runtime"]["changed_files"], ["app_replica/engine.py"])


if __name__ == "__main__":
    unittest.main()
