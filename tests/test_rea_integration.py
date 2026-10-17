"""Temporary managed-service integration; every device observation is simulated."""

import json
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from workbench.analysis_evidence import verify_evidence
from workbench.client import Client
from workbench.common import WorkbenchError, atomic_json, file_hash, path_resource, rpc
from workbench.project import builtin_operations
from workbench.registry import normalize
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


class ReaIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rea-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.operations = builtin_operations()
        self.manifest = {"version": 1, "python": sys.executable,
                         "workbench_root": str(ROOT), "operations": self.operations}
        atomic_json(self.project / "workbench.project.json", self.manifest)
        fake = self.root / "fake-adb"
        fake.write_text("#!" + sys.executable + "\nimport sys\nargs=sys.argv[3:]\n"
                        "if args == ['shell','cat','/proc/sys/kernel/random/boot_id']: print('simulated-boot')\n"
                        "elif args[:3] == ['shell','dumpsys','activity']: print('mResumedActivity: com.fixture/.Main')\n"
                        "elif args[:2] == ['shell','pidof']: print('123')\n")
        fake.chmod(0o755)
        self.config = {**DEFAULTS, "min_free_bytes": 0,
                       "devices": {"simulated": {"serial": "test-only", "adb": str(fake)}}}
        atomic_json(self.state / "config.json", self.config)
        self.log = (self.root / "service.log").open("wb")
        self.process = subprocess.Popen([sys.executable, str(ROOT / "scripts/workbench.py"),
                                         "--state", str(self.state), "serve"],
                                        stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_service)
        for _ in range(100):
            try:
                rpc(self.state, "service.capabilities")
                break
            except WorkbenchError:
                time.sleep(0.03)
        else:
            self.fail("Temporary service did not start")
        self.client = Client(self.project, self.state, "integration")

    def stop_service(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()

    def submit(self, operation, args=None, **options):
        job = self.client.submit(operation=operation, args=args or [], **options)
        return self.client.wait(job["id"], timeout=15)

    def normalized(self, operation, args):
        directory = self.root / "normalized-job"
        return normalize({"project": str(self.project), "operation": operation, "args": args},
                         self.config, directory)

    def test_managed_unknown_record_publishes_verified_sidecar(self):
        ledger = self.project / "unknowns.json"
        output = self.project / "unknown-result.json"
        job = self.submit("analysis.unknown_record", ["unknown-record", "--ledger", str(ledger),
                          "--question", "Which source owns this branch?", "--output", str(output)])
        self.assertEqual(job["state"], "succeeded", job)
        artifacts = self.client.call("jobs.artifacts", {"id": job["id"]})
        reference = job["result"]["analysis_evidence"]
        self.assertTrue(any(item["name"] == "analysis-evidence.json" for item in artifacts["published"]["files"]))
        sidecar = Path(job["directory"]) / reference["name"]
        self.assertEqual(file_hash(sidecar), reference["sha256"])
        value = verify_evidence(json.loads(sidecar.read_text()))
        self.assertEqual(value["evidence_id"], reference["evidence_id"])
        self.assertTrue(value["execution_identity"]["adapter"]["source_manifest_sha256"])
        self.assertTrue(value["execution_identity"]["direct_executables"])
        self.assertFalse(value["cache_reusable"])
        self.assertEqual(json.loads(output.read_text())["question"], "Which source owns this branch?")

    def test_partial_adapter_retains_partial_raw_output_in_sidecar(self):
        source = self.project / "fixture-source"
        source.mkdir()
        script = source / "partial.py"
        script.write_text("import json,pathlib,sys\n"
                          "p=pathlib.Path(sys.argv[sys.argv.index('--output')+1]);p.mkdir()\n"
                          "(p/'summary.json').write_text(json.dumps({'coverage':'partial'}))\n"
                          "(p/'raw.txt').write_text('retained partial observation')\nraise SystemExit(3)\n")
        self.manifest["operations"]["fixture.partial"] = {
            "script": "fixture-source/partial.py", "source": "fixture-source", "readonly": True,
            "outputs": ["--output"], "partial_codes": [3]}
        atomic_json(self.project / "workbench.project.json", self.manifest)
        job = self.submit("fixture.partial", ["--output", str(self.project / "partial")])
        self.assertEqual(job["state"], "partial", job)
        sidecar = json.loads((Path(job["directory"]) / "analysis-evidence.json").read_text())
        self.assertTrue(sidecar["partial_observation"])
        self.assertTrue(any(item["path"].endswith("raw.txt") for item in sidecar["raw_results"]))
        self.assertEqual(sidecar["outcome"]["state"], "partial")
        self.assertFalse(sidecar["cache_reusable"])

    def runtime_fixture(self):
        job = self.submit("device.observe", device="simulated")
        self.assertEqual(job["state"], "succeeded", job)
        observation = Path(job["directory"]) / "observation-0.json"
        params = {"job_id": job["id"], "device": "simulated",
                  "observation_window": {"started_at": job["started"],
                                         "finished_at": job["result"]["finished_at"]},
                  "observations": [{"kind": "state", "path": str(observation), "sha256": file_hash(observation)}]}
        return job, params

    def test_runtime_rpc_and_managed_plan_callback_and_project_boundary(self):
        producer, params = self.runtime_fixture()
        self.assertTrue(self.client.call("jobs.verify_runtime", params)["verified"])
        record = self.project / "runtime.json"
        atomic_json(record, {"runtime_verified": True, **params})
        plan = self.project / "plan.json"
        plan_job = self.submit("analysis.plan", ["plan", "--question", "安装并截图", "--output", str(plan)])
        self.assertEqual(plan_job["state"], "succeeded", plan_job)
        # The normal plan CLI uses request routing, which makes this a runtime-only plan.
        updated = self.submit("analysis.plan_update", ["plan-update", "--plan", str(plan),
                              "--step", "runtime", "--status", "completed", "--evidence", str(record),
                              "--note", "Simulated observation provenance verified"])
        self.assertEqual(updated["state"], "succeeded", updated)
        self.assertEqual(json.loads(plan.read_text())["steps"][0]["status"], "completed")
        other = self.root / "other-project"
        other.mkdir()
        atomic_json(other / "workbench.project.json", self.manifest)
        outsider = Client(other, self.state, "outsider")
        with self.assertRaisesRegex(WorkbenchError, "different project"):
            outsider.call("jobs.verify_runtime", params)
        # Mutable publication metadata cannot grant authority to a new file.
        invented = self.project / "invented.txt"
        invented.write_text("caller-selected fake observation")
        manifest_path = Path(producer["directory"]) / "artifacts.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["external_outputs"].append({"path": str(invented), "files": [
            {"path": str(invented), "sha256": file_hash(invented), "bytes": invented.stat().st_size}]})
        atomic_json(manifest_path, manifest)
        forged = {**params, "observations": [{"kind": "file", "path": str(invented), "sha256": file_hash(invented)}]}
        self.assertFalse(self.client.call("jobs.verify_runtime", forged)["verified"])

    def test_transitive_plan_ledger_evidence_and_mutable_outputs_are_pinned(self):
        evidence = self.project / "observation.txt"
        evidence.write_text("evidence")
        ledger = self.project / "ledger.json"
        atomic_json(ledger, {"questions": [{"evidence": [{"path": str(evidence)}],
                                              "history": [{"evidence": [{"path": str(evidence)}]}]}]})
        plan = self.project / "plan.json"
        atomic_json(plan, {"unknown_ledger": {"path": str(ledger)}, "steps": []})
        normalized = self.normalized("analysis.plan_update", ["plan-update", "--plan", str(plan),
                                     "--step", "static", "--status", "pending", "--note", "reopen"])
        self.assertNotIn(str(plan), normalized["input_fingerprints"])
        self.assertIn(str(ledger), normalized["input_fingerprints"])
        self.assertIn(str(evidence), normalized["input_fingerprints"])
        normalized = self.normalized("analysis.unknown_update", ["unknown-update", "--ledger", str(ledger),
                                     "--id", "some", "--expected-revision", "1", "--status", "open", "--reason", "reopen",
                                     "--output", str(self.project / "result.json")])
        self.assertNotIn(str(ledger), normalized["input_fingerprints"])
        self.assertIn(str(evidence), normalized["input_fingerprints"])

    def test_query_helpers_pinned_and_index_only_knowledge_reserves_roots(self):
        database = self.project / "navigation.sqlite3"
        database.write_bytes(b"fixture")
        wal = Path(str(database) + "-wal")
        wal.write_bytes(b"wal-fixture")
        normalized = self.normalized("analysis.query", ["--db", str(database), "--query-json", "{}"])
        self.assertIn(str(wal), normalized["input_fingerprints"])
        for suffix in ("-wal", "-shm"):
            self.assertTrue(any(item["key"] == path_resource(str(database) + suffix)
                                and item["mode"] == "write" for item in normalized["resources"]))
        manifests = normalized["nested_source_manifests"]
        self.assertIn("apk_code_index.py", manifests[str(ROOT / "scripts")])
        self.assertIn("apk_navigation.py", manifests[str(ROOT / "scripts")])
        documents = self.project / "references"
        documents.mkdir()
        index = self.project / "knowledge.json"
        atomic_json(index, {"roots": [str(documents)], "urls": []})
        normalized = self.normalized("analysis.knowledge", ["knowledge", "--index", str(index), "--query", "signature"])
        self.assertIn(str(index), normalized["input_fingerprints"])
        self.assertTrue(any(item["key"] == path_resource(documents) for item in normalized["resources"]))

    def test_managed_query_executes_pinned_helpers_and_publishes_navigation(self):
        from scripts import apk_code_index as engine
        from tests.test_apk_code_index import fixture_dex

        analysis = self.project / "raw-analysis"
        (analysis / "inventory").mkdir(parents=True)
        (analysis / "native").mkdir()
        raw = fixture_dex()
        (analysis / "input.dex").write_bytes(raw)
        (analysis / "inventory/dex.jsonl").write_text(json.dumps({
            "dex_id": "fixture-dex", "sha256": engine.digest(raw), "entry_name": "classes.dex",
            "extracted_path": "input.dex"}) + "\n")
        (analysis / "native/libraries.jsonl").write_text("")
        with contextlib.redirect_stdout(io.StringIO()):
            engine.build(SimpleNamespace(root=analysis, native_focus=None, app_prefix=["Lsample/"]))
        output = self.project / "query-result.json"
        request = {"schema": "apk-query/v1", "kind": "callees", "selector": {
            "descriptor": "Lsample/A;->show()V", "artifact": "fixture-dex", "method_idx": 1}}
        job = self.submit("analysis.query", ["--db", str(analysis / "code-index/code-index.sqlite3"),
                          "--query-json", json.dumps(request), "--output", str(output)])
        self.assertEqual(job["state"], "succeeded", job)
        value = json.loads(output.read_text())
        self.assertEqual(value["schema"], "apk-query/v1")
        self.assertEqual(value["items"][0]["pc"], 6)
        self.assertEqual(value["items"][0]["kind"], "invoke-static")
        self.assertEqual(job["result"]["analysis_status"]["schema"], "apk-query/v1")
        self.assertFalse(job["result"]["analysis_status"]["whole_apk_complete"])

    def test_managed_offline_js_reads_without_executing_source(self):
        source = self.project / "bundle.js"
        marker = self.project / "must-not-exist"
        source.write_text("require('child_process').execSync('touch " + str(marker) + "');\n"
                          "import helper from './helper.js';\nfetch('https://example.invalid/api');")
        output = self.project / "js-result.json"
        job = self.submit("analysis.offline_js", ["js", "--input", str(source), "--output", str(output)])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertFalse(marker.exists())
        value = json.loads(output.read_text())
        self.assertEqual(value["schema"], "analysis-offline/v1")
        self.assertFalse(value["semantic_complete"])
        self.assertEqual(value["inputs"][0]["sha256"], file_hash(source))
        self.assertEqual(job["result"]["analysis_status"]["metadata_scope"], "adapter_reported")
        self.assertFalse(job["result"]["analysis_status"]["semantic_complete"])
        partial = self.submit("analysis.offline_js", ["js", "--input", str(source), "--max-items", "1", "--output", str(self.project / "partial-js-result.json")])
        self.assertEqual(partial["state"], "succeeded", partial)
        self.assertEqual(partial["result"]["analysis_status"]["status"], "partial")
        self.assertTrue(partial["result"]["analysis_status"]["truncated"])


if __name__ == "__main__":
    unittest.main()
