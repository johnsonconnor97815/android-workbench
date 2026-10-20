"""Sequential native class groups retain per-class evidence and real stop gates."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.app_replica.engine import execute
from workbench.app_replica.runner import native_next
from workbench.app_replica.store import ReplicaError


class GroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-class-group-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workflow"
        self.counter = 0
        self.call("init", project={"id": "fixture", "workbench_source": str(ROOT)}, capacity=3)
        self.members = []
        for name in ("A", "B", "C"):
            self.call("inventory.add", objects=[{"id": name, "kind": "class", "name": name,
                      "source": {"carrier": "host-fixture", "path": name + ".smali"}}])
            task = self.call("task.add", task={"role": "analysis", "mode": "scope", "object_id": name,
                             "description": "Inspect related fixture class " + name})["task"]
            self.members.append(task["id"])
        evidence = self.workspace / "artifacts/relation.txt"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("Host fixture: A has B, B calls C; not Android evidence")
        self.evidence = {"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}

    def call(self, op, **fields):
        self.counter += 1
        fields.setdefault("request_id", "fixture-" + str(self.counter))
        return execute(self.workspace, {"op": op, **fields})

    def group(self, members=None, **extra):
        members = members or self.members[:2]
        return self.call("task.group", group_id="related", task_ids=members,
                         reason="Actual fixture dependency permits shared read-only evidence",
                         relationships=[{"from": members[0], "to": members[1], "relation": "field/call",
                                         "evidence": [self.evidence]}], **extra)

    def start(self, directive, target="/fixture/group-thread"):
        self.call("run.started", run_id=directive["run_id"], backend_handle={"target": target,
                  "requested": {"model": "gpt-6-astra", "effort": "xhigh"}})

    def submit(self, directive, *, result="pass"):
        task = self.call("status", task_id=directive["task_id"])["task"]
        cls = task["object_id"]
        self.call("scope.class.publish", task_id=task["id"], run_id=directive["run_id"], expected_revision=0,
                  reason="Fixture full declaration", evidence=[self.evidence], structure={"kind": "interface",
                  "name": cls, "fields": [], "methods": [], "interfaces": [], "initializers": []})
        return self.call("task.submit", task_id=task["id"], run_id=directive["run_id"], artifacts=[],
                         class_revisions={cls: 1}, unresolved=[], self_checks=[{"name": cls + " declaration",
                         "result": result, "evidence": [self.evidence]}])

    def stop(self, directive):
        return {"op": "run.stopped", "request_id": "stop-" + directive["run_id"],
                "run_id": directive["run_id"], "outcome": "delivered",
                "observation": {"confirmed": True, "writers_stopped": True,
                                "evidence": ["Synthetic host stop; no model or Android run"]}}

    def test_reuse_thread_only_after_stop_and_keep_separate_acceptance(self):
        before = self.call("status")
        self.group()
        after = self.call("status")
        for name in ("tasks", "classes", "writers"):
            self.assertEqual(before[name], after[name])
        first = native_next(self.workspace, external_active=1, group_id="related")
        self.assertEqual(first["directive"]["tool"], "spawn_agent")
        self.assertIn("owns ONLY the assigned task", first["directive"]["arguments"]["message"])
        self.start(first)
        self.assertTrue(self.submit(first)["delivery"]["accepted"])
        blocked = native_next(self.workspace, external_active=1, group_id="related")
        self.assertIsNone(blocked["run"])
        second = native_next(self.workspace, external_active=1, group_id="related", stopped=self.stop(first))
        self.assertEqual(second["directive"]["tool"], "followup_task")
        self.assertEqual(second["directive"]["arguments"]["target"], "/fixture/group-thread")
        self.assertEqual(second["group_continuation_from_run"], first["run_id"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.start(second)
        self.assertFalse(self.submit(second, result="unknown")["delivery"]["accepted"])
        self.call(**self.stop(second))
        self.assertTrue(self.call("status", task_id=first["task_id"])["task"]["current_delivery"])
        self.assertFalse(self.call("status", task_id=second["task_id"])["task"]["current_delivery"])
        snapshot = self.call("status")
        self.assertEqual(snapshot["classes"]["A"]["owner"], self.members[0])
        self.assertEqual(snapshot["classes"]["B"]["owner"], self.members[1])

    def test_unconfirmed_stop_and_prerequisites_still_block(self):
        self.call("task.add", task={"id": "dependency", "role": "analysis", "mode": "inspect",
                                   "description": "Actual missing prerequisite"})
        self.call("task.add", task={"role": "analysis", "mode": "scope", "object_id": "B",
                                   "description": "Same task", "requires": [{"task_id": "dependency"}]})
        self.group()
        first = native_next(self.workspace, external_active=1, group_id="related")
        retry = native_next(self.workspace, external_active=1, group_id="related")
        self.assertEqual(first["run_id"], retry["run_id"])
        self.start(first)
        self.submit(first)
        bad = self.stop(first); bad["observation"]["writers_stopped"] = False
        with self.assertRaises(ReplicaError):
            native_next(self.workspace, external_active=1, group_id="related", stopped=bad)
        blocked = native_next(self.workspace, external_active=1, group_id="related", stopped=self.stop(first))
        self.assertIsNone(blocked["run"])

    def test_reject_disconnected_existing_and_mixed_assignments(self):
        with self.assertRaisesRegex(ReplicaError, "evidenced relationship"):
            self.group(self.members)
        self.group()
        with self.assertRaisesRegex(ReplicaError, "already belongs"):
            self.call("task.group", group_id="other", task_ids=self.members[:2], reason="duplicate",
                      relationships=[])
        first = native_next(self.workspace, external_active=1, task_id=self.members[2])
        with self.assertRaisesRegex(ReplicaError, "unstarted"):
            self.call("task.group", group_id="late", task_ids=[first["task_id"]], reason="cannot adopt live owner",
                      relationships=[])

    def test_changed_relationship_evidence_blocks_claim(self):
        self.group()
        Path(self.evidence["path"]).write_text("changed original relationship")
        with self.assertRaisesRegex(ReplicaError, "SHA-256"):
            native_next(self.workspace, external_active=1, group_id="related")

    def test_mixed_roles_and_missing_groups_fail_without_mutation(self):
        with self.assertRaises(ReplicaError):
            self.call("task.group", group_id="invalid", task_ids=[{}], reason="Malformed task identity", relationships=[])
        writer = self.call("task.add", task={"role": "class", "mode": "code", "object_id": "C",
                          "class_id": "C", "description": "Distinct Sol writer", "outputs": []})["task"]
        before = (self.workspace / "coordination/state.json").read_bytes()
        with self.assertRaisesRegex(ReplicaError, "exact role/model/effort"):
            self.group([self.members[0], writer["id"]])
        self.assertEqual(before, (self.workspace / "coordination/state.json").read_bytes())
        with self.assertRaisesRegex(ReplicaError, "Unknown task group"):
            native_next(self.workspace, external_active=1, group_id="missing")

    def test_live_reused_handle_cannot_be_claimed_by_another_group_member(self):
        self.group()
        first = native_next(self.workspace, external_active=1, group_id="related")
        self.start(first)
        self.submit(first)
        self.call(**self.stop(first))
        unrelated = native_next(self.workspace, external_active=1, task_id=self.members[2])
        self.start(unrelated)  # Malformed host reuse outside group; must not compound it.
        with self.assertRaisesRegex(ReplicaError, "host thread is still active"):
            native_next(self.workspace, external_active=1, group_id="related")

    def test_configuration_and_backend_are_not_silently_replaced(self):
        self.group()
        with self.assertRaisesRegex(ReplicaError, "native backend"):
            self.call("task.claim", task_id=self.members[0], backend="codex")
        from workbench.app_replica.store import Store
        store = Store(self.workspace)
        with store.locked() as state:
            state["models"]["analysis"]["effort"] = "high"
            store.commit(state)
        with self.assertRaisesRegex(ReplicaError, "configuration changed"):
            native_next(self.workspace, external_active=1, group_id="related")

    def test_focus_is_presentation_only_and_cannot_expand_permissions(self):
        output = str(self.workspace / "artifacts/current.json")
        self.call("task.add", task={"role": "analysis", "mode": "scope", "object_id": "A",
                                   "description": "Same task", "outputs": [output]})
        before = deepcopy(self.call("status", task_id=self.members[0])["task"]["outputs"])
        with self.assertRaisesRegex(ReplicaError, "exact assigned"):
            native_next(self.workspace, external_active=1, task_id=self.members[0], focus_outputs=["/outside"])
        first = native_next(self.workspace, external_active=1, task_id=self.members[0], focus_outputs=[output])
        self.assertEqual(before, self.call("status", task_id=self.members[0])["task"]["outputs"])
        with self.assertRaisesRegex(ReplicaError, "existing reservation"):
            native_next(self.workspace, external_active=1, task_id=self.members[0], focus_outputs=[])
        self.assertEqual(first["run_id"], native_next(self.workspace, external_active=1,
                         task_id=self.members[0], focus_outputs=[output])["run_id"])


if __name__ == "__main__":
    unittest.main()
