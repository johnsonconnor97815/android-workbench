"""Content reuse and explicit downstream obligations; host fixtures only."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import test_app_replica_core as core
from workbench.app_replica import ReplicaError
from workbench.app_replica.store import Store
from workbench.app_replica import engine
from workbench.app_replica.ids import stable_id


class DeliveryTests(unittest.TestCase):
    # Reuse the existing host fixture helpers without rerunning its test class.
    setUp = core.ReplicaCoreTest.setUp
    call = core.ReplicaCoreTest.call
    inventory = core.ReplicaCoreTest.inventory
    scope = core.ReplicaCoreTest.scope
    task = core.ReplicaCoreTest.task
    started = core.ReplicaCoreTest.started
    stopped = core.ReplicaCoreTest.stopped
    artifact = core.ReplicaCoreTest.artifact
    publish = core.ReplicaCoreTest.publish
    mvp_fixture = core.ReplicaCoreTest.mvp_fixture

    def deliver(self, task, *, run=None, stop=True, **changes):
        run = run or self.started(task)
        current = self.call("status")["tasks"][task["id"]]
        artifacts = []
        for path in current["outputs"]:
            target = Path(path)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("host fixture code")
            import hashlib
            artifacts.append({"path": path, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        request = {"task_id": task["id"], "run_id": run, "artifacts": artifacts,
                   "self_checks": [{"name": "source comparison", "result": "pass", "evidence": ["fixture"]}],
                   "unresolved": [], **changes}
        result = self.call("task.submit", **request)
        if stop:
            self.stopped(run)
        return result

    def method(self, name="method", **fields):
        return self.task(id=name, role="function", mode="code",
                         outputs=[f"artifacts/{name}.kt"], **fields)

    def token(self, task):
        return self.call("status")["tasks"][task["id"]]["delivery_token"]

    def resume(self, task):
        self.call("task.resume", task_id=task["id"], reason="Handle new evidence", evidence=["fixture"])

    def pair(self):
        source = self.method()
        self.deliver(source)
        consumer = self.method("consumer", requires=[source["id"]])
        token = self.token(source)
        self.deliver(consumer, dependency_tokens={source["id"]: token})
        return source, consumer, token

    def followup_fixture(self, stage="build", runtime_kind="host_fixture"):
        source = self.method()
        owner = self.method("assembly", requires=[source["id"]])
        evidence = self.artifact("artifacts/obligation.json", "Exact downstream fixture requirement")
        spec = {"id": "verify", "stage": stage, "task_id": owner["id"],
                "description": "Check this source in its assembled candidate", "evidence": [evidence]}
        if stage == "runtime":
            spec["runtime_scope"] = {"kind": runtime_kind, "conditions": {"input": "fixture"}}
        result = self.deliver(source, scope="source", followups=[spec])
        return source, owner, spec, result["delivery"]["followups"][0]["id"]

    def owner_delivery(self, source, owner, identifier, stage="build", **changes):
        return self.deliver(owner, dependency_tokens={source["id"]: self.token(source)},
                            addresses_followups=[identifier],
                            self_checks=[{"name": "actual stage fixture", "stage": stage,
                                          "result": "pass", "evidence": ["host fixture only"]}], **changes)

    def resolution(self, source, owner, identifier, **changes):
        evidence = self.artifact("artifacts/build-result.txt", "Host fixture stage result")
        return self.call("followup.resolve", followup_id=identifier, actor=owner["id"],
                         source_token=self.token(source), owner_token=self.token(owner),
                         evidence=[evidence], **changes)

    def test_identical_resubmission_preserves_consumer_despite_lifecycle_revisions(self):
        source, consumer, token = self.pair()
        revision = self.call("status")["tasks"][source["id"]]["revision"]
        self.resume(source)
        self.assertFalse(self.call("status")["tasks"][consumer["id"]]["current_delivery"])
        self.deliver(source)
        status = self.call("status")
        self.assertGreater(status["tasks"][source["id"]]["revision"], revision)
        self.assertEqual(self.token(source), token)
        self.assertTrue(status["tasks"][consumer["id"]]["current_delivery"])

    def test_live_submitted_writer_remains_unusable_until_observed_stop(self):
        source, consumer, token = self.pair()
        self.resume(source)
        run = self.started(source)
        self.deliver(source, run=run, stop=False)
        status = self.call("status")
        self.assertIsNone(status["tasks"][source["id"]]["delivery_token"])
        self.assertFalse(status["tasks"][consumer["id"]]["current_delivery"])
        self.stopped(run)
        self.assertEqual(self.token(source), token)

    def test_changed_bytes_invalidate_transitive_consumers_without_metadata_write(self):
        source, consumer, _ = self.pair()
        top = self.method("top", requires=[consumer["id"]])
        self.deliver(top, dependency_tokens={consumer["id"]: self.token(consumer)})
        (self.workspace / "artifacts/method.kt").write_text("changed source")
        status = self.call("status")
        self.assertFalse(status["tasks"][consumer["id"]]["current_delivery"])
        self.assertFalse(status["tasks"][top["id"]]["current_delivery"])

    def test_new_checked_code_still_invalidates_old_consumer_token(self):
        source, consumer, old = self.pair()
        self.resume(source)
        (self.workspace / "artifacts/method.kt").write_text("new implementation")
        self.deliver(source)
        self.assertNotEqual(old, self.token(source))
        self.assertFalse(self.call("status")["tasks"][consumer["id"]]["current_delivery"])

    def test_contract_and_input_evidence_changes_invalidate_delivery(self):
        source = self.method()
        config = self.artifact("artifacts/compiler-settings.json", "compiler configuration")
        self.deliver(source, inputs=[config])
        (self.workspace / config["path"]).write_text("different configuration")
        self.assertIsNone(self.token(source))
        (self.workspace / config["path"]).write_text("compiler configuration")
        self.assertIsNotNone(self.token(source))
        gate = self.task(id="additional-input")
        self.task(id=source["id"], role="function", mode="code", requires=[gate["id"]])
        self.assertIsNone(self.token(source))
        self.assertIn("Task implementation contract changed",
                      self.call("status")["tasks"][source["id"]]["current_delivery_issues"])

    def test_failed_resubmission_does_not_reuse_previous_pass(self):
        source, consumer, _ = self.pair()
        self.resume(source)
        self.deliver(source, self_checks=[{"name": "source check", "result": "fail", "evidence": ["mismatch"]}])
        self.assertIsNone(self.token(source))
        self.assertFalse(self.call("status")["tasks"][consumer["id"]]["current_delivery"])

    def test_cannot_bind_stale_or_missing_dependency_tokens(self):
        source, consumer, old = self.pair()
        self.resume(source)
        (self.workspace / "artifacts/method.kt").write_text("new code")
        self.deliver(source)
        self.resume(consumer)
        result = self.deliver(consumer, dependency_tokens={source["id"]: old})
        self.assertFalse(result["delivery"]["accepted"])
        self.resume(consumer)
        result = self.deliver(consumer)
        self.assertFalse(result["delivery"]["accepted"])

    def test_legacy_revision_binding_retains_strict_behavior(self):
        source = self.method()
        self.deliver(source)
        consumer = self.method("consumer", requires=[source["id"]])
        revision = self.call("status")["tasks"][source["id"]]["revision"]
        self.deliver(consumer, dependency_revisions={source["id"]: revision})
        self.resume(source)
        self.deliver(source)
        self.assertFalse(self.call("status")["tasks"][consumer["id"]]["current_delivery"])

    def test_legacy_records_are_read_without_rewriting_or_promoting_them(self):
        source = self.method()
        self.deliver(source, unresolved=["Original unknown must remain blocking"])
        # A fixture emulating an existing pre-upgrade authority, not a live edit.
        path = self.workspace / "coordination/state.json"
        state = json.loads(path.read_text())
        delivery = state["tasks"][source["id"]]["deliveries"][-1]
        for key in ("format", "scope", "task_contract", "dependency_tokens", "inputs", "followups", "addresses_followups"):
            delivery.pop(key)
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        self.assertIsNone(self.token(source))
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.call("status")["tasks"][source["id"]]["deliveries"][-1]["accepted"])

    def test_cyclic_content_dependencies_fail_closed(self):
        source = self.method()
        self.deliver(source)
        consumer = self.method("consumer")
        self.deliver(consumer, dependency_tokens={source["id"]: self.token(source)})
        consumer_token = self.token(consumer)
        self.resume(source)
        result = self.deliver(source, dependency_tokens={consumer["id"]: consumer_token})
        self.assertFalse(result["delivery"]["accepted"])
        self.assertIsNone(self.token(consumer))

    def test_class_structure_change_invalidates_transitive_tokens(self):
        cls = self.inventory()
        member = self.inventory("Lsample/A;->method()V", "method", class_id=cls)
        source = self.task(member, role="function", mode="code", outputs=["artifacts/member.kt"])
        self.publish(cls)
        self.deliver(source, class_revisions={cls: 1})
        consumer = self.method("consumer", requires=[source["id"]])
        self.deliver(consumer, dependency_tokens={source["id"]: self.token(source)})
        self.publish(cls)
        self.assertFalse(self.call("status")["tasks"][consumer["id"]]["current_delivery"])

    def test_mvp_reuses_identical_resubmission_but_not_changed_checks(self):
        task, _, request = self.mvp_fixture()
        self.call("mvp.record", **request)
        original = deepcopy(self.call("status")["tasks"][task["id"]]["deliveries"][-1])
        self.resume(task)
        self.deliver(task, self_checks=original["self_checks"])
        self.assertEqual(self.call("status")["mvps"]["first-screen"]["current_result"], "pass")
        self.resume(task)
        self.deliver(task, self_checks=[{"name": "different check", "result": "pass", "evidence": ["new evidence"]}])
        self.assertEqual(self.call("status")["mvps"]["first-screen"]["current_result"], "stale")

    def test_followup_allows_source_handoff_and_wakes_only_responsible_owner_once(self):
        source, owner, spec, identifier = self.followup_fixture()
        status = self.call("status")
        self.assertTrue(status["tasks"][source["id"]]["current_delivery"])
        self.assertIn(owner["id"], status["ready_task_ids"])
        self.assertEqual(status["followup_counts"], {"open": 1, "resolved": 0, "stale": 0})
        message_id = status["followups"][identifier]["notification_message_id"]
        self.assertEqual(status["messages"][message_id]["recipient"], owner["id"])
        self.resume(source)
        self.deliver(source, scope="source", followups=[spec])
        self.assertEqual(len(self.call("status")["messages"]), 1)
        self.assertEqual(len(self.call("status")["followups"]), 1)

    def test_followup_wake_waits_for_usable_stopped_source(self):
        source = self.method()
        owner = self.method("assembly")
        evidence = self.artifact("artifacts/requirement.txt")
        spec = {"id": "build", "stage": "build", "task_id": owner["id"],
                "description": "Build the source", "evidence": [evidence]}
        run = self.started(source)
        self.deliver(source, run=run, stop=False, scope="source", followups=[spec])
        self.assertEqual(self.call("status")["messages"], {})
        self.assertEqual(self.call("status")["followup_counts"]["open"], 1)
        self.stopped(run, "failed")
        self.assertEqual(self.call("status")["messages"], {})
        self.resume(source)
        self.deliver(source, scope="source", followups=[spec])
        self.assertEqual(len(self.call("status")["messages"]), 1)

    def test_notification_collision_preserves_message_and_does_not_prevent_real_stop(self):
        source = self.method()
        owner = self.method("assembly")
        evidence = self.artifact("artifacts/requirement.txt")
        run = self.started(source)
        result = self.deliver(source, run=run, stop=False, scope="source", followups=[{
            "id": "build", "stage": "build", "task_id": owner["id"],
            "description": "Compile source", "evidence": [evidence]}])
        identifier = result["delivery"]["followups"][0]["id"]
        message_id = stable_id("followup-message", identifier, result["delivery"]["id"])
        original = self.call("message.send", message_id=message_id, type="note", sender="fixture",
                             recipient=owner["id"], payload={"immutable": True}, evidence=["fixture"])["message"]
        self.stopped(run)
        status = self.call("status")
        self.assertEqual(status["occupied"], 0)
        self.assertEqual(status["messages"][message_id], original)
        self.assertIn("notification_error", status["followups"][identifier])

    def test_followup_never_hides_source_unknown_or_failed_checks(self):
        source, _, spec, _ = self.followup_fixture()
        for changes in ({"unresolved": ["Missing method branch"]},
                        {"self_checks": [{"name": "source", "result": "unknown", "evidence": ["missing"]}]}):
            self.resume(source)
            result = self.deliver(source, scope="source", followups=[spec], **changes)
            self.assertFalse(result["delivery"]["accepted"])
            self.assertIsNone(self.token(source))

    def test_followup_requires_owner_scope_and_hashed_evidence(self):
        source = self.method()
        owner = self.method("assembly")
        evidence = self.artifact("artifacts/obligation.txt")
        spec = {"id": "verify", "stage": "build", "task_id": owner["id"],
                "description": "Compile assembled source", "evidence": [evidence]}
        run = self.started(source)
        for change in ({"task_id": "missing"}, {"task_id": source["id"]}, {"evidence": []},
                       {"evidence": [{**evidence, "sha256": "bad"}]}, {"stage": "anything"}):
            with self.subTest(change=change), self.assertRaises(ReplicaError):
                self.deliver(source, run=run, stop=False, scope="source", followups=[{**spec, **change}])
        with self.assertRaisesRegex(ReplicaError, "explicit source"):
            self.deliver(source, run=run, stop=False, followups=[spec])

    def test_followup_resolution_is_explicit_and_stales_with_source_or_proof_changes(self):
        source, owner, _, identifier = self.followup_fixture()
        self.owner_delivery(source, owner, identifier)
        # The explicit claim + stage check are mechanically settled on stop.
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "resolved")
        self.resolution(source, owner, identifier)
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "resolved")
        (self.workspace / "artifacts/build-result.txt").write_text("changed proof")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "stale")
        (self.workspace / "artifacts/build-result.txt").write_text("Host fixture stage result")
        (self.workspace / "artifacts/method.kt").write_text("changed source")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "stale")

    def test_delivered_owner_without_explicit_coverage_cannot_resolve_followup(self):
        source, owner, _, identifier = self.followup_fixture()
        self.deliver(owner, dependency_tokens={source["id"]: self.token(source)})
        with self.assertRaisesRegex(ReplicaError, "explicitly address"):
            self.resolution(source, owner, identifier)
        self.resume(owner)
        self.deliver(owner, dependency_tokens={source["id"]: self.token(source)}, addresses_followups=[identifier])
        with self.assertRaisesRegex(ReplicaError, "passing check"):
            self.resolution(source, owner, identifier)

    def test_open_followups_prevent_scope_completion_but_not_source_completion(self):
        source, _, _, _ = self.followup_fixture()
        self.call("inventory.add", objects=[], complete=True, limitations=[])
        for summary in (True, False):
            coverage = self.call("coverage", summary=summary)["coverage"]
            self.assertTrue(coverage["source_scope_complete"])
            self.assertFalse(coverage["scope_complete"])
            self.assertFalse(coverage["runtime_verified"])

    def test_runtime_followup_requires_current_paired_scene_not_just_owner_delivery(self):
        source, owner, _, identifier = self.followup_fixture("runtime")
        self.owner_delivery(source, owner, identifier, "runtime")
        with self.assertRaises(ReplicaError):
            self.resolution(source, owner, identifier)
        obj = self.inventory("Lsample/Stage;->run()V", "method")
        self.call("entry.add", entry={"id": "startup", "trigger": "onCreate", "root_object_ids": [obj], "evidence": ["fixture"]})
        self.call("mvp.define", mvp={"id": "paired", "entry_ids": ["startup"],
                  "required_task_ids": [source["id"], owner["id"]],
                  "target_versions": {"original": "1", "candidate": "2"},
                  "conditions": {"input": "fixture"}, "goals": ["same"]})
        original = self.artifact("evidence/original.json", "original observation")
        candidate = self.artifact("evidence/candidate.json", "candidate observation")
        self.call("mvp.record", mvp_id="paired", original_version="1", candidate_version="2",
                  runtime_scope={"kind": "host_fixture", "conditions": {"input": "fixture"}},
                  observations={"original": original, "candidate": candidate},
                  results={"same": {"result": "pass", "original": original["path"], "candidate": candidate["path"]}})
        self.resolution(source, owner, identifier, mvp_id="paired")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "resolved")
        self.assertFalse(self.call("coverage")["coverage"]["runtime_verified"])
        self.resume(owner)
        self.owner_delivery(source, owner, identifier, "runtime")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "resolved")
        self.resume(owner)
        (self.workspace / "artifacts/assembly.kt").write_text("changed runtime owner code")
        self.owner_delivery(source, owner, identifier, "runtime")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "stale")
        self.resume(owner)
        (self.workspace / "artifacts/assembly.kt").write_text("host fixture code")
        self.owner_delivery(source, owner, identifier, "runtime")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "resolved")
        (self.workspace / candidate["path"]).write_text("different observation")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "stale")

    def test_host_fixture_cannot_discharge_an_android_runtime_obligation(self):
        source, owner, _, identifier = self.followup_fixture("runtime", runtime_kind="android")
        self.owner_delivery(source, owner, identifier, "runtime")
        obj = self.inventory("Lsample/Android;->run()V", "method")
        self.call("entry.add", entry={"id": "startup", "trigger": "onCreate", "root_object_ids": [obj], "evidence": ["fixture"]})
        self.call("mvp.define", mvp={"id": "host-only", "entry_ids": ["startup"],
                  "required_task_ids": [source["id"], owner["id"]],
                  "target_versions": {"original": "1", "candidate": "2"},
                  "conditions": {"input": "fixture"}, "goals": ["same"]})
        original = self.artifact("evidence/original.json", "original observation")
        candidate = self.artifact("evidence/candidate.json", "candidate observation")
        self.call("mvp.record", mvp_id="host-only", original_version="1", candidate_version="2",
                  runtime_scope={"kind": "host_fixture", "conditions": {"input": "fixture"}},
                  observations={"original": original, "candidate": candidate},
                  results={"same": {"result": "pass", "original": original["path"], "candidate": candidate["path"]}})
        with self.assertRaisesRegex(ReplicaError, "runtime scope"):
            self.resolution(source, owner, identifier, mvp_id="host-only")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "open")

    def test_shared_dependencies_are_memoized_within_one_validation_not_across_reads(self):
        source, left, _ = self.pair()
        right = self.method("right", requires=[source["id"]])
        self.deliver(right, dependency_tokens={source["id"]: self.token(source)})
        top = self.method("top", requires=[left["id"], right["id"]])
        self.deliver(top, dependency_tokens={left["id"]: self.token(left), right["id"]: self.token(right)})
        store = Store(self.workspace)
        with store.locked() as state, patch.object(engine, "_artifact", wraps=engine._artifact) as check:
            self.assertTrue(engine._delivery_current(store, state, state["tasks"][top["id"]]))
            self.assertEqual(check.call_count, 4)
            (self.workspace / "artifacts/method.kt").write_text("changed between queries")
            self.assertFalse(engine._delivery_current(store, state, state["tasks"][top["id"]]))

    def test_followups_do_not_disappear_when_source_resubmits_without_them(self):
        source, _, _, identifier = self.followup_fixture()
        token = self.token(source)
        self.resume(source)
        self.deliver(source, scope="source")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "open")
        self.assertEqual(self.token(source), token)
        self.assertEqual(len(self.call("status")["messages"]), 1)

    def test_changed_source_renotifies_owner_but_identical_resume_does_not(self):
        source, _, spec, identifier = self.followup_fixture()
        initial = self.token(source)
        for content in ("changed method", "changed method", "host fixture code"):
            self.resume(source)
            (self.workspace / "artifacts/method.kt").write_text(content)
            self.deliver(source, scope="source", followups=[spec])
        status = self.call("status")
        self.assertEqual(len(status["messages"]), 3)
        self.assertEqual(self.token(source), initial)
        self.assertEqual(status["followups"][identifier]["notified_source_token"], initial)

    def test_followup_cannot_resolve_using_unbound_source_or_foreign_owner(self):
        source = self.method()
        owner = self.method("assembly")
        proof = self.artifact("artifacts/obligation.txt")
        result = self.deliver(source, scope="source", followups=[{
            "id": "build", "task_id": owner["id"], "stage": "build", "description": "Check this source", "evidence": [proof]}])
        identifier = result["delivery"]["followups"][0]["id"]
        self.deliver(owner, addresses_followups=[identifier],
                     self_checks=[{"name": "build", "stage": "build", "result": "pass", "evidence": ["host fixture"]}])
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "open")
        with self.assertRaisesRegex(ReplicaError, "bind the source"):
            self.resolution(source, owner, identifier)
        with self.assertRaisesRegex(ReplicaError, "responsible task"):
            self.call("followup.resolve", followup_id=identifier, actor=source["id"])

    def test_stage_claim_is_not_settled_until_successful_real_stop(self):
        source, owner, _, identifier = self.followup_fixture()
        run = self.started(owner)
        self.owner_delivery(source, owner, identifier, run=run, stop=False)
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "open")
        self.stopped(run, "failed")
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "open")

    def test_stage_claim_rechecks_consumed_source_at_settlement(self):
        source, owner, _, identifier = self.followup_fixture()
        run = self.started(owner)
        self.owner_delivery(source, owner, identifier, run=run, stop=False)
        (self.workspace / "artifacts/method.kt").write_text("source changed before owner stop")
        result = self.stopped(run)
        self.assertFalse(result["task"]["deliveries"][-1]["accepted"])
        self.assertEqual(result["followup_results"], {})
        self.assertEqual(self.call("status")["followups"][identifier]["current_status"], "open")

    def test_legacy_mvp_keeps_revision_boundaries(self):
        task, _, request = self.mvp_fixture()
        self.call("mvp.record", **request)
        path = self.workspace / "coordination/state.json"
        state = json.loads(path.read_text())
        state["mvps"]["first-screen"]["records"][-1].pop("task_tokens")
        path.write_text(json.dumps(state))
        checks = state["tasks"][task["id"]]["deliveries"][-1]["self_checks"]
        self.assertEqual(self.call("status")["mvps"]["first-screen"]["current_result"], "pass")
        self.resume(task)
        self.deliver(task, self_checks=checks)
        self.assertEqual(self.call("status")["mvps"]["first-screen"]["current_result"], "stale")


if __name__ == "__main__":
    unittest.main()
