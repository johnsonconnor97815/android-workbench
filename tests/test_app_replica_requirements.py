"""Guarded dependency corrections use host metadata fixtures, never Android proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.app_replica import execute, ReplicaError


class RequirementCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-requires-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.n = 0
        self.call("init", project={"id": "requirements"}, capacity=2)
        self.add("app", role="app", mode="plan")
        self.blocker = self.add("whole-class")
        for identifier in ("vm-class", "media-class"):
            self.call("inventory.add", objects=[{"id": identifier, "kind": "class",
                       "name": identifier, "source": "original fixture carrier"}])
            self.call("class.publish", class_id=identifier, owner="structure-" + identifier,
                      expected_revision=0, structure={"fields": [], "methods": []})
        self.target = self.call("task.add", task={
            "role": "class", "mode": "code", "object_id": "vm-class", "class_id": "vm-class",
            "description": "Assemble genuine caller fragments", "outputs": ["artifacts/VM.kt"],
            "requires": [{"task_id": self.blocker, "when": "stopped"}, {"message_id": "source-ready"}],
        })["task_id"]
        self.call("message.send", message_id="source-ready", sender="fixture", recipient=self.target,
                  type="source", payload={"fixture": True}, evidence=["fixture"])
        evidence = self.workspace / "artifacts/decision.json"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text('{"decision":"use published declarations for caller assembly"}')
        self.evidence = {"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}

    def call(self, op, **fields):
        self.n += 1
        fields.setdefault("request_id", f"request-{self.n}")
        return execute(self.workspace, {"op": op, **fields})

    def add(self, identifier, **fields):
        return self.call("task.add", task={"id": identifier, "role": "analysis", "mode": "inspect",
                                          "description": "Host fixture", **fields})["task_id"]

    def state(self):
        return json.loads((self.workspace / "coordination/state.json").read_text())

    def request(self, target=None, **fields):
        target = target or self.target
        task = self.state()["tasks"][target]
        return {"op": "task.requires.replace", "request_id": f"correction-{self.n}", "actor": "app",
                "task_id": target, "expected_revision": task["revision"],
                "expected_requires": deepcopy(task["requires"]),
                "requires": [{"class_id": "media-class", "revision": 1},
                             {"message_id": "source-ready"}, {"class_id": "vm-class", "revision": 1}],
                "reason": "Caller assembly uses declarations; whole-class work remains pending",
                "evidence": [self.evidence], **fields}

    def rejected(self, request, message):
        before = (self.workspace / "coordination/state.json").read_bytes()
        with self.assertRaisesRegex(ReplicaError, message):
            execute(self.workspace, request)
        self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), before)

    def stop(self, run, outcome):
        return self.call("run.stopped", run_id=run, outcome=outcome,
                         observation={"confirmed": True, "writers_stopped": True,
                                      "evidence": ["isolated host fixture"]})

    def test_caller_ready_without_accepting_or_stopping_dependency(self):
        # Reproduce two rejected deliveries followed by a queued, inactive dependency.
        for _ in range(2):
            run = self.call("task.claim", task_id=self.blocker, backend="host_fixture")["run"]["id"]
            self.call("run.started", run_id=run, backend_handle="fixture")
            result = self.call("task.submit", task_id=self.blocker, run_id=run, artifacts=[],
                               self_checks=[{"name": "incomplete fixture", "result": "fail", "evidence": "fixture"}],
                               unresolved=["whole-class correspondence unverified"])
            self.assertFalse(result["delivery"]["accepted"])
            self.stop(run, "delivered")
            self.call("task.resume", task_id=self.blocker, reason="Fixture retry", evidence=["fixture"])
        self.assertIsNone(self.call("task.claim", task_id=self.target, backend="host_fixture")["task"])
        request = self.request()
        spec = {k: deepcopy(self.state()["tasks"][self.target][k]) for k in
                ("id", "role", "mode", "object_id", "class_id", "description")}
        self.call("task.add", task={**spec, "requires": []})
        self.assertEqual(self.state()["tasks"][self.target]["requires"], request["expected_requires"])
        before = self.state()
        result = execute(self.workspace, request)
        after = self.state()
        self.assertEqual(result["task_revision"], request["expected_revision"] + 1)
        self.assertEqual(after["tasks"][self.blocker], before["tasks"][self.blocker])
        for field in ("runs", "writers", "classes", "messages", "capacity", "models", "inventory_ref", "scope_ref"):
            self.assertEqual(after[field], before[field], field)
        for field, value in before["tasks"][self.target].items():
            if field not in {"requires", "revision", "requires_corrections"}:
                self.assertEqual(after["tasks"][self.target][field], value, field)
        correction = after["tasks"][self.target]["requires_corrections"][-1]
        self.assertEqual(correction["previous_requires"], request["expected_requires"])
        self.assertEqual(correction["requires"], request["requires"])
        self.assertEqual(correction["actor"], "app")
        self.assertIsNotNone(self.call("task.claim", task_id=self.target, backend="host_fixture")["run"])

    def test_stale_guards_and_noop_reject_without_commit(self):
        q = self.request()
        self.rejected({**q, "expected_revision": q["expected_revision"] + 1}, "Stale task revision")
        self.rejected({**q, "expected_requires": []}, "Stale expected_requires")
        self.rejected({**q, "requires": q["expected_requires"]}, "must differ")

    def test_app_actor_and_regular_file_evidence_required(self):
        q = self.request()
        self.rejected({**q, "actor": self.blocker}, "App planning actor")
        self.rejected({**q, "evidence": []}, "existing file evidence")
        self.rejected({**q, "evidence": [{**self.evidence, "sha256": "0" * 64}]}, "mismatched")
        self.rejected({**q, "reason": " "}, "nonempty")

    def test_own_class_invalid_references_and_cycles_reject_atomically(self):
        q = self.request()
        self.rejected({**q, "requires": []}, "own class declaration")
        self.rejected({**q, "requires": [{"task_id": "unknown"}]}, "Unknown tasks")
        self.rejected({**q, "requires": [*q["requires"], {"task_id": self.target}]}, "form a cycle")
        self.rejected({**q, "requires": [{"class_id": "vm-class", "revision": 0}]}, "integer >= 1")

    def test_message_evidence_is_read_only_and_does_not_claim_a_writer(self):
        path = self.workspace / "messages/source-ready.json"
        evidence = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        before = self.state()
        raw = path.read_bytes()
        execute(self.workspace, self.request(evidence=[self.evidence, evidence]))
        after = self.state()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(after["writers"], before["writers"])
        self.assertEqual(after["messages"], before["messages"])
        self.assertEqual(after["tasks"][self.target]["outputs"], before["tasks"][self.target]["outputs"])

    def test_evidence_symlinks_and_unregistered_outside_files_reject(self):
        link = self.workspace / "artifacts/alias.json"
        link.symlink_to(self.evidence["path"])
        self.rejected(self.request(evidence=[{**self.evidence, "path": str(link)}]), "Symlink")
        with tempfile.TemporaryDirectory(prefix="awb-outside-evidence-") as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text("outside registered roots")
            evidence = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            self.rejected(self.request(evidence=[evidence]), "outside artifacts")

    def test_reserved_running_and_pending_stop_remain_protected(self):
        target = self.add("live")
        run = self.call("task.claim", task_id=target, backend="host_fixture")["run"]["id"]
        self.rejected(self.request(target), "live or unconfirmed")
        self.call("run.started", run_id=run, backend_handle="fixture")
        self.rejected(self.request(target), "live or unconfirmed")
        self.call("task.wait", task_id=target, run_id=run, requires=[{"message_id": "future"}], checkpoint="fixture")
        self.rejected(self.request(target), "live or unconfirmed")
        self.stop(run, "waiting")
        execute(self.workspace, self.request(target))
        self.assertEqual(self.state()["tasks"][target]["state"], "waiting")
        self.assertIsNone(self.state()["tasks"][target]["run_id"])

    def test_delivered_target_is_not_reinterpreted(self):
        target = self.add("delivered")
        run = self.call("task.claim", task_id=target, backend="host_fixture")["run"]["id"]
        self.call("run.started", run_id=run, backend_handle="fixture")
        self.call("task.submit", task_id=target, run_id=run, artifacts=[],
                  self_checks=[{"name": "fixture", "result": "pass", "evidence": "fixture"}], unresolved=[])
        self.stop(run, "delivered")
        self.rejected(self.request(target), "idle queued, waiting or failed")

    def test_receipt_replay_preserves_newer_correction_and_conflicts_reject(self):
        first = self.request()
        result = execute(self.workspace, first)
        execute(self.workspace, self.request(request_id="second", requires=first["expected_requires"]))
        before = self.state()
        self.assertEqual(execute(self.workspace, first), result)
        self.assertEqual(self.state(), before)
        self.rejected({**first, "reason": "Changed request"}, "different content")


if __name__ == "__main__":
    unittest.main()
