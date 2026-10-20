"""Prompt snapshots use isolated host metadata, never App/runtime evidence."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.app_replica import execute, ReplicaError
from workbench.app_replica.roles import task_prompt
from workbench.app_replica.store import Store


class PromptManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-prompt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "original/workflow"
        self.project = {"id": "host-prompt-fixture", "workbench_source": str(ROOT)}
        self.task = {"id": "writer", "role": "class", "mode": "code", "revision": 1,
                     "outputs": ["artifacts/first.java", "artifacts/check.json"],
                     "runs": [], "checkpoint": {"note": "durable current progress"}}

    def run_record(self, identifier, **fields):
        self.task["runs"].append(identifier)
        self.task["revision"] += 1
        return {"id": identifier, "task_id": "writer", "model": "gpt-6.1-sol", "effort": "high",
                "state": "reserved", "inbox_message_ids": ["current-question"], **fields}

    def prompt(self, run):
        return task_prompt(self.workspace, self.project, self.task, run)

    def index(self, run_id):
        return json.loads((self.workspace / f"coordination/prompt-manifests/runs/{run_id}.json").read_text())

    def manifest(self, run_id):
        reference = self.index(run_id)["references"][-1]
        return Store(self.workspace).read_blob(reference, "prompt-manifests")

    def expanded(self, prompt):
        return json.loads(next(line.split(": ", 1)[1] for line in prompt.splitlines()
                               if line.startswith(("Effective assigned outputs (initial run): ",
                                                   "Current focus/new assigned outputs: "))))

    def test_initial_run_expands_all_and_preserves_source_contract(self):
        run = self.run_record("run-first")
        before = deepcopy((self.project, self.task, run))
        prompt = self.prompt(run)
        self.assertEqual(self.expanded(prompt), [str(self.workspace / value) for value in self.task["outputs"]])
        manifest = self.manifest(run["id"])
        self.assertEqual(manifest["task_revision"], self.task["revision"])
        self.assertEqual(manifest["run_revision"], None)
        self.assertEqual(len(manifest["run_record_sha256"]), 64)
        self.assertEqual(manifest["baseline_status"], "initial_run")
        self.assertEqual(manifest["outputs"], [{"raw": value, "effective": str(self.workspace / value)}
                                                for value in self.task["outputs"]])
        reference = self.index(run["id"])["references"][0]
        self.assertFalse(Path(reference["path"]).is_absolute())
        self.assertEqual(reference["version"], 1)
        self.assertIn(reference["sha256"], prompt)
        self.assertIn(f'"bytes": {reference["bytes"]}', prompt)
        self.assertIn("class CODE mode", prompt)
        self.assertIn("no invented token, character, material or turn budget", prompt)
        self.assertIn("never revert other writers", prompt)
        self.assertEqual(before, (self.project, self.task, run))

    def test_resumed_and_new_threads_use_verified_delta_without_memory_assumption(self):
        first = self.run_record("run-first")
        self.prompt(first)
        for identifier, continuation in [("run-resume", {"thread_id": "historical-thread"}),
                                         ("run-new-thread", None)]:
            run = self.run_record(identifier, continuation=continuation,
                                  focus_outputs=[self.task["outputs"][1]])
            prompt = self.prompt(run)
            self.assertEqual(self.expanded(prompt), [str(self.workspace / self.task["outputs"][1])])
            self.assertNotIn(str(self.workspace / self.task["outputs"][0]), prompt)
            self.assertEqual(self.manifest(identifier)["added_outputs"], [])
            self.assertIn("do not assume a resumed or new thread remembers", prompt)
            self.assertIn("current-question", prompt)
            self.assertIn("Read the current checkpoint in tasks/writer/task.json", prompt)

    def test_legacy_prior_run_is_not_treated_as_all_new(self):
        self.run_record("legacy-no-manifest")
        run = self.run_record("run-current", continuation={"target": "new-native-worker"})
        prompt = self.prompt(run)
        self.assertEqual(self.expanded(prompt), [])
        self.assertIn("No verified delta baseline", prompt)
        manifest = self.manifest(run["id"])
        self.assertEqual(manifest["baseline_status"], "unavailable")
        self.assertEqual(manifest["added_outputs"], [])
        self.assertEqual(len(manifest["outputs"]), 2)

    def test_raw_identity_changes_are_added_and_removed(self):
        self.prompt(self.run_record("run-first"))
        self.task["outputs"] = ["artifacts/renamed.java", "artifacts/check.json", "artifacts/new.java"]
        run = self.run_record("run-current", focus_outputs=["artifacts/check.json"])
        prompt = self.prompt(run)
        manifest = self.manifest(run["id"])
        self.assertEqual(manifest["added_outputs"], ["artifacts/renamed.java", "artifacts/new.java"])
        self.assertEqual(manifest["removed_outputs"], ["artifacts/first.java"])
        self.assertEqual(self.expanded(prompt), [str(self.workspace / value) for value in self.task["outputs"]])
        self.assertIn("removed identities: 1", prompt)

    def test_relocation_reads_relative_baseline_and_recomputes_effective_paths(self):
        old_root = self.workspace.parent
        raw = str(old_root / "output/Original.java")
        self.task["outputs"] = [raw]
        self.prompt(self.run_record("run-origin"))
        old_manifest = self.manifest("run-origin")
        for revision, destination in [(1, "clone"), (2, "second-clone")]:
            target = self.root / destination
            shutil.copytree(self.workspace, target / "workflow")
            self.workspace = target / "workflow"
            self.project["_path_binding"] = {"revision": revision,
                "projection": str(self.workspace / "coordination/path-bindings.json"),
                "bindings": [{"from": str(old_root), "to": str(target)}]}
            run = self.run_record(f"run-bound-{revision}", focus_outputs=[raw])
            prompt = self.prompt(run)
            manifest = self.manifest(run["id"])
            self.assertEqual(manifest["outputs"], [{"raw": raw, "effective": str(target / "output/Original.java")}])
            self.assertEqual(manifest["added_outputs"], [])
            self.assertEqual(self.expanded(prompt), [str(target / "output/Original.java")])
            self.assertNotIn(raw, prompt)
            self.assertIn("direct-read exception", prompt)
            self.assertIn("without the App operand resolver", prompt)
        self.assertEqual(old_manifest, self.manifest("run-origin"))

    def test_reoffered_run_retains_snapshots_and_dynamic_paths_remain_formal(self):
        run = self.run_record("run-first")
        self.prompt(run)
        initial = self.index(run["id"])["references"][-1]
        self.prompt(run)
        self.assertEqual(self.index(run["id"])["references"], [initial])
        self.task["revision"] += 1
        self.task["outputs"].append("artifacts/added-during-run.java")
        prompt = self.prompt(run)
        references = self.index(run["id"])["references"]
        self.assertEqual(len(references), 2)
        self.assertEqual(references[0], initial)
        self.assertEqual(len(Store(self.workspace).read_blob(initial, "prompt-manifests")["outputs"]), 2)
        self.assertEqual(len(self.manifest(run["id"])["outputs"]), 3)
        self.assertIn("including paths reserved later by task.add", prompt)

    def test_reoffered_same_run_after_relocation_refreshes_its_snapshot(self):
        raw = str(self.workspace.parent / "output/Original.java")
        self.task["outputs"] = [raw]
        run = self.run_record("run-reoffered")
        self.prompt(run)
        origin = self.workspace.parent
        target = self.root / "clone"
        shutil.copytree(self.workspace, target / "workflow")
        self.workspace = target / "workflow"
        self.project["_path_binding"] = {"revision": 1,
            "projection": str(self.workspace / "coordination/path-bindings.json"),
            "bindings": [{"from": str(origin), "to": str(target)}]}
        prompt = self.prompt(run)
        self.assertEqual(self.expanded(prompt), [str(target / "output/Original.java")])
        self.assertEqual(len(self.index(run["id"])["references"]), 2)
        self.assertEqual(self.manifest(run["id"])["binding_revision"], 1)

    def test_corrupt_blob_or_index_fails_closed(self):
        self.prompt(self.run_record("run-first"))
        index_path = self.workspace / "coordination/prompt-manifests/runs/run-first.json"
        index_bytes = index_path.read_bytes()
        reference = self.index("run-first")["references"][-1]
        blob_path = self.workspace / reference["path"]
        blob_bytes = blob_path.read_bytes()
        run = self.run_record("run-current")
        for corrupt in [b"{}", blob_bytes + b" "]:
            blob_path.write_bytes(corrupt)
            with self.assertRaisesRegex(ReplicaError, "hash mismatch"):
                self.prompt(run)
            self.assertFalse((self.workspace / "coordination/prompt-manifests/runs/run-current.json").exists())
        blob_path.write_bytes(blob_bytes)
        for corrupt in [b"not JSON", b'{}']:
            index_path.write_bytes(corrupt)
            with self.assertRaises(ReplicaError):
                self.prompt(run)
        index_path.write_bytes(index_bytes)
        wrong = self.manifest("run-first")
        wrong["task_id"] = "someone-else"
        invalid_ref = {**Store(self.workspace).write_blob("prompt-manifests", wrong), "version": 1}
        index = self.index("run-first")
        index["references"] = [invalid_ref]
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ReplicaError, "contents"):
            self.prompt(run)

    def test_invalid_or_missing_referenced_manifest_is_not_legacy(self):
        self.prompt(self.run_record("run-first"))
        index_path = self.workspace / "coordination/prompt-manifests/runs/run-first.json"
        index = self.index("run-first")
        run = self.run_record("run-current")
        index["references"][0]["version"] = 2
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ReplicaError, "Unsupported"):
            self.prompt(run)
        index["references"][0]["version"] = 1
        index_path.write_text(json.dumps(index))
        (self.workspace / index["references"][0]["path"]).unlink()
        with self.assertRaisesRegex(ReplicaError, "Cannot read immutable data"):
            self.prompt(run)

    def test_focus_never_infers_suffix_or_descendant_permissions(self):
        self.task["outputs"] = ["artifacts/a.directory", "artifacts/b.json"]
        (self.workspace / "artifacts/b.json").mkdir(parents=True)
        self.prompt(self.run_record("run-first"))
        run = self.run_record("run-current")
        self.assertEqual(self.expanded(self.prompt(run)), [])
        for focus in [["artifacts/a.directory/child.java"], ["artifacts/unassigned"],
                      ["artifacts/b.json", "artifacts/b.json"], "artifacts/b.json"]:
            with self.subTest(focus=focus), self.assertRaisesRegex(ReplicaError, "exact assigned"):
                self.prompt({**run, "focus_outputs": focus})

    def test_prompt_generation_does_not_change_authority_or_writer_ownership(self):
        execute(self.workspace, {"op": "init", "request_id": "init", "project": self.project, "capacity": 1})
        execute(self.workspace, {"op": "task.add", "request_id": "add", "task": {
            "id": "writer", "role": "class", "mode": "code", "description": "Host-only fixture",
            "outputs": self.task["outputs"]}})
        claim = execute(self.workspace, {"op": "task.claim", "request_id": "claim", "task_id": "writer", "backend": "native"})
        authority = self.workspace / "coordination/state.json"
        before = authority.read_bytes()
        task_projection = (self.workspace / "tasks/writer/task.json").read_bytes()
        task_prompt(self.workspace, self.project, claim["task"], claim["run"])
        self.assertEqual(authority.read_bytes(), before)
        self.assertEqual((self.workspace / "tasks/writer/task.json").read_bytes(), task_projection)
        self.assertEqual(json.loads(before)["writers"], json.loads(authority.read_bytes())["writers"])
        with self.assertRaisesRegex(ReplicaError, "Output already"):
            execute(self.workspace, {"op": "task.add", "request_id": "foreign-writer", "task": {
                "id": "foreign", "role": "class", "mode": "code", "description": "Cannot steal the output",
                "outputs": [self.task["outputs"][0]]}})

    def test_large_fixture_expands_only_new_and_explicit_focus_paths(self):
        historical = [f"artifacts/history/source-mapping-and-delivery-evidence-{number:04d}.json" for number in range(643)]
        self.task["outputs"] = historical[:]
        self.prompt(self.run_record("run-first"))
        new = [f"artifacts/current/implementation-{number:04d}.java" for number in range(23)]
        self.task["outputs"] += new
        prompt = self.prompt(self.run_record("run-current", focus_outputs=historical[:2]))
        expanded = self.expanded(prompt)
        self.assertEqual(len(self.manifest("run-current")["outputs"]), 666)
        self.assertEqual(len(expanded), 25)
        self.assertEqual(len(set(expanded) & {str(self.workspace / value) for value in historical}), 2)
        full_line = "Effective assigned outputs: " + json.dumps([str(self.workspace / value) for value in self.task["outputs"]])
        self.assertLess(len(prompt.encode()), len(full_line.encode()))

    def test_extraction_mode_contract_is_preserved(self):
        self.task.update(role="analysis", mode="extract")
        run = self.run_record("run-extract", model="gpt-6-luna", effort="low")
        prompt = self.prompt(run)
        self.assertIn("EXTRACTION mode: source-linked mechanical extraction only, using Luna", prompt)
        self.assertIn("Requested execution: gpt-6-luna / low", prompt)


if __name__ == "__main__":
    unittest.main()
