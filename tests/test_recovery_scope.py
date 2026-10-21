"""Recovery scope regressions using an isolated scheduler store, no devices."""

import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from workbench.common import WorkbenchError, process_identity
from workbench.service import DEFAULTS, Service
from workbench.store import Store
from workbench.results import recoverable_resources


class RecoveryScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-recovery-scope-")
        self.root = Path(self.temp.name)
        self.service = Service.__new__(Service)
        self.service.store = Store(self.root / "queue.sqlite3")
        self.service.config = DEFAULTS.copy()
        self.service.record_device_status = lambda job: None
        self.project = str(self.root / "project")

    def tearDown(self):
        self.service.store.db.close()
        self.temp.cleanup()

    def spec(self, *, operation="broken", project=None, device=None):
        project = project or self.project
        return {"operation": operation, "project": project, "device": device,
                "resources": [{"key": "path:" + project, "mode": "write"}],
                "directory": str(self.root), "dependencies": [], "readonly": False}

    def insert(self, spec, state="failed", worker=None):
        identifier = uuid.uuid4().hex
        self.service.store.db.execute(
            "INSERT INTO jobs(id,owner,project,request_key,request_hash,state,spec,created,updated,worker) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identifier, "owner", spec["project"], identifier, "hash", state,
             json.dumps(spec), 1, 1, json.dumps(worker) if worker else None),
        )
        return identifier

    def fault(self, identifier, key=None):
        spec = self.service.store.job(identifier)["spec"]
        self.service.store.health(key or spec["resources"][0]["key"], "needs_recovery",
                                  {"job": identifier, "reason": "test_fault"})

    def repair(self, targets=None):
        spec = self.spec(operation="repair")
        spec.update(recovery=True, entry={"recovery_for": ["broken"]})
        if targets is not None:
            spec["recovery_jobs"] = targets
        self.service.prepare_recovery(spec)
        return spec

    def complete(self, spec, reported=None, resources=None):
        identifier = self.insert(spec, state="running")
        self.service.finish(self.service.store.job(identifier), {
            "state": "succeeded", "cleanup_ok": True,
            "reconciled_jobs": spec["recovery_targets"] if reported is None else reported,
            "recovered_resources": spec["resources"] if resources is None else resources,
        })
        return self.service.store.job(identifier)

    def test_default_scope_excludes_foreign_project_and_other_operation(self):
        old = self.insert(self.spec(), state="needs_recovery")
        self.fault(old)
        foreign = self.insert(self.spec(project=self.project + "-other"), state="needs_recovery")
        self.fault(foreign)
        unrelated = self.insert(self.spec(operation="unrelated"), state="needs_recovery")
        spec = self.repair()
        self.assertEqual(spec["recovery_targets"], [old])
        self.complete(spec)
        self.assertEqual(self.service.store.job(old)["state"], "failed")
        self.assertEqual(self.service.store.job(foreign)["state"], "needs_recovery")
        self.assertEqual(self.service.store.job(unrelated)["state"], "needs_recovery")
        self.assertEqual([json.loads(r["detail"])["job"] for r in
                          self.service.store.rows("SELECT * FROM resources")], [foreign])

    def test_explicit_scope_rejects_unknown_foreign_healthy_and_wrong_operation(self):
        invalid = [uuid.uuid4().hex,
                   self.insert(self.spec(project=self.project + "-other"), state="needs_recovery"),
                   self.insert(self.spec()),
                   self.insert(self.spec(operation="unrelated"), state="needs_recovery")]
        for identifier in invalid:
            with self.subTest(identifier=identifier), self.assertRaises(WorkbenchError):
                self.repair([identifier])
        for targets in ([], "bad", [invalid[0], invalid[0]], [None]):
            with self.subTest(targets=targets), self.assertRaises(WorkbenchError):
                self.repair(targets)
        spec = self.spec()
        spec["recovery_jobs"] = [invalid[0]]
        with self.assertRaises(WorkbenchError):
            self.service.prepare_recovery(spec)

    def test_new_fault_after_submission_is_not_reconciled_or_cleared(self):
        old = self.insert(self.spec(), state="needs_recovery")
        self.fault(old)
        spec = self.repair([old])
        later = self.insert(self.spec(), state="needs_recovery")
        self.fault(later)  # Same resource, new fault source after the scope froze.
        self.complete(spec)
        self.assertEqual(self.service.store.job(later)["state"], "needs_recovery")
        self.assertEqual(json.loads(self.service.store.rows("SELECT * FROM resources")[0]["detail"])["job"], later)

    def test_reported_subset_only_reconciles_verified_targets(self):
        first = self.insert(self.spec(), state="needs_recovery")
        second_spec = self.spec()
        second_spec["resources"] = [{"key": "path:" + self.project + "/second", "mode": "write"}]
        second = self.insert(second_spec, state="needs_recovery")
        self.fault(first)
        self.fault(second)
        spec = self.repair([first, second])
        self.complete(spec, reported=[first])
        self.assertEqual(self.service.store.job(first)["state"], "failed")
        self.assertEqual(self.service.store.job(second)["state"], "needs_recovery")
        self.assertEqual(len(self.service.store.rows("SELECT * FROM resources")), 1)

    def test_failed_repair_preserves_ancestor_and_only_clears_added_resources(self):
        ancestor = self.insert(self.spec(operation="host.lease"))
        self.fault(ancestor)
        inherited = self.service.store.rows("SELECT * FROM resources")
        old_spec = self.spec()
        old_spec.update(recovery=True, recovery_health=inherited)
        old_spec["resources"] += [{"key": "service:adb", "mode": "write"}]
        failed_repair = self.insert(old_spec)
        self.fault(failed_repair)
        self.fault(failed_repair, "service:adb")
        spec = self.repair([failed_repair])
        # Even an overbroad worker result cannot erase the ancestor's fault.
        self.complete(spec, resources=old_spec["resources"])
        self.assertEqual(self.service.store.rows("SELECT * FROM resources"), inherited)
        self.assertEqual(recoverable_resources(old_spec), [{"key": "service:adb", "mode": "write"}])

    def test_nested_recovery_respects_parent_child_overlap(self):
        for old_key, inherited_key in [("path:/env", "path:/env/tool"),
                                       ("path:/env/tool", "path:/env")]:
            with self.subTest(old_key=old_key):
                spec = {"recovery": True, "resources": [{"key": old_key, "mode": "write"}],
                        "recovery_health": [{"key": inherited_key}]}
                self.assertEqual(recoverable_resources(spec), [])

    def test_nested_recovery_does_not_restore_over_new_fault(self):
        ancestor = self.insert(self.spec(operation="host.lease"))
        self.fault(ancestor)
        old_spec = self.spec()
        old_spec.update(recovery=True, recovery_health=self.service.store.rows("SELECT * FROM resources"))
        failed_repair = self.insert(old_spec)
        self.fault(failed_repair)
        spec = self.repair([failed_repair])
        later = self.insert(self.spec())
        self.fault(later)
        expected = self.service.store.rows("SELECT * FROM resources")
        self.complete(spec)
        self.assertEqual(self.service.store.rows("SELECT * FROM resources"), expected)

    def test_unknown_result_target_cannot_clear_health(self):
        old = self.insert(self.spec())
        self.fault(old)
        spec = self.repair([old])
        done = self.complete(spec, reported=[uuid.uuid4().hex])
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["result"]["reason"], "recovery_result_outside_granted_targets")
        self.assertTrue(self.service.store.rows("SELECT * FROM resources"))

    def test_live_worker_and_changed_health_block_availability(self):
        old = self.insert(self.spec(), worker=process_identity(os.getpid()))
        self.fault(old)
        spec = self.repair([old])
        repair_id = self.insert(spec, state="queued")
        self.assertEqual(self.service.available(self.service.store.job(repair_id)), (False, "needs_recovery"))
        self.service.store.update(old, worker=None)
        self.assertTrue(self.service.available(self.service.store.job(repair_id))[0])
        self.service.store.health(spec["resources"][0]["key"], "needs_recovery",
                                  {"job": old, "reason": "new_generation"})
        self.assertEqual(self.service.available(self.service.store.job(repair_id)), (False, "needs_recovery"))

    def test_manual_return_only_accepts_the_reserved_transition(self):
        spec = self.spec(operation="device.recover", device="test-device")
        spec.update(recovery=True)
        key = "device:test-device"
        spec["resources"] = [{"key": key, "mode": "write"}]
        self.service.store.health(key, "manual_return_pending", {"owner": "owner"})
        self.service.prepare_recovery(spec)
        identifier = self.insert(spec, state="queued")
        self.assertEqual(self.service.available(self.service.store.job(identifier)),
                         (False, "manual_return_pending"))
        self.service.store.health(key, "needs_recovery", {"reason": "manual_returned"})
        self.assertTrue(self.service.available(self.service.store.job(identifier))[0])
        self.service.store.health(key, "needs_recovery", {"reason": "another_fault"})
        self.assertEqual(self.service.available(self.service.store.job(identifier)),
                         (False, "needs_recovery"))


if __name__ == "__main__":
    unittest.main()
