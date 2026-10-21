"""Coordination regressions with isolated host observations, not Android proof."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.app_replica import engine
from workbench.app_replica.cli import main
from workbench.app_replica.runner import dispatch_lock, native_next
from workbench.app_replica.store import ReplicaError, Store


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-coordination-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workflow"
        self.counter = 0
        self.call("init", project={"id": "host-fixture", "workbench_source": str(ROOT)}, capacity=2)

    def call(self, op, **fields):
        self.counter += 1
        if op not in engine.READS:
            fields.setdefault("request_id", f"fixture-{self.counter}")
        return engine.execute(self.workspace, {"op": op, **fields})

    def file(self, name, content):
        path = self.workspace / "artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def task(self, name, **fields):
        return self.call("task.add", task={"id": name, "role": "analysis", "mode": "inspect",
                         "description": "Host protocol fixture", "outputs": [], **fields})["task"]

    def start(self, task):
        run = self.call("task.claim", task_id=task["id"], backend="native")["run"]
        self.assertIsNotNone(run)
        self.call("run.started", run_id=run["id"], backend_handle={"target": "fixture-" + run["id"]})
        return run["id"]

    def submit(self, task, run, **fields):
        return self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[],
                         unresolved=[], self_checks=[{"name": "host-only", "result": "pass",
                                                      "evidence": ["isolated fixture"]}], **fields)

    def stop_request(self, run):
        return {"op": "run.stopped", "run_id": run, "outcome": "delivered",
                "observation": {"confirmed": True, "writers_stopped": True,
                                "evidence": ["Synthetic host observation; no real worker invoked"]}}

    def stop(self, run):
        return self.call(**self.stop_request(run))

    def scope_task(self):
        obj = {"id": "original-interface", "kind": "class", "name": "Empty",
               "source": {"carrier": "fixture-dex-hash", "path": "original.java"}}
        self.call("inventory.add", objects=[obj])
        caller = self.task("caller")
        task = self.call("task.discover", task_id=caller["id"], target=obj["id"],
                         source=obj["source"])["task"]
        run = self.start(task)
        evidence = self.file("original.java", "package fixture; public interface Empty {}")
        request = {"op": "scope.class.publish", "task_id": task["id"], "run_id": run,
                   "expected_revision": 0, "reason": "Complete fixture declaration supports copy",
                   "evidence": [evidence],
                   "structure": {"kind": "interface", "name": "fixture.Empty", "modifiers": ["public"],
                                 "interfaces": [], "type_parameters": [], "annotations": [],
                                 "fields": [], "methods": [], "initializers": []}}
        return task, run, request

    def test_combined_scope_structure_keeps_two_workers_and_evidence_gates(self):
        task, run, request = self.scope_task()
        published = self.call(**request)
        cls = published["class"]
        self.assertEqual(cls["owner"], task["id"])
        self.assertEqual(published["object"]["scope"]["disposition"], "copy")
        discovered = self.call("task.discover", task_id="caller", target=cls["id"], source="same declaration")
        self.assertEqual(discovered["task_id"], task["id"])
        output = self.workspace / "artifacts/Empty.java"
        writer = self.call("task.add", task={"role": "class", "mode": "code", "object_id": cls["id"],
            "class_id": cls["id"], "description": "Implement fixture declaration", "outputs": [str(output)]})["task"]
        self.assertIn({"task_id": task["id"]}, writer["requires"])
        self.assertIsNone(self.call("task.claim", task_id=writer["id"], backend="native")["run"])
        delivered = self.submit(task, run, class_revisions={cls["id"]: 1})
        self.assertTrue(delivered["delivery"]["accepted"])
        self.assertEqual(delivered["delivery"]["inputs"], request["evidence"])
        self.assertIsNone(self.call("task.claim", task_id=writer["id"], backend="native")["run"])
        self.stop(run)
        scope = self.call("status", task_id=task["id"])["task"]
        self.assertTrue(scope["current_delivery"])
        writer_run = self.start(writer)
        artifact = self.file("Empty.java", "package fixture; public interface Empty {}")
        legacy = self.call("task.submit", task_id=writer["id"], run_id=writer_run, artifacts=[artifact],
            unresolved=[], class_revisions={cls["id"]: 1}, dependency_revisions={task["id"]: scope["revision"]},
            self_checks=[{"name": "fixture declaration comparison", "result": "pass", "evidence": [artifact]}])
        self.assertFalse(legacy["delivery"]["accepted"])
        self.assertTrue(any("requires a delivery token" in issue for issue in legacy["delivery"]["stale_reasons"]))
        result = self.call("task.submit", task_id=writer["id"], run_id=writer_run, artifacts=[artifact],
            unresolved=[], class_revisions={cls["id"]: 1}, dependency_tokens={task["id"]: scope["delivery_token"]},
            self_checks=[{"name": "fixture declaration comparison", "result": "pass", "evidence": [artifact]}])
        self.assertTrue(result["delivery"]["accepted"])
        self.stop(writer_run)
        status = self.call("status")
        self.assertEqual(len(status["runs"]), 2)
        self.assertFalse(any(t["mode"] == "structure" for t in status["tasks"].values()))
        self.assertTrue(status["tasks"][writer["id"]]["current_delivery"])
        Path(request["evidence"][0]["path"]).write_text("changed original")
        self.assertFalse(self.call("status", task_id=writer["id"])["task"]["current_delivery"])

    def test_combined_publish_preserves_other_structure_owner_atomically(self):
        task, run, request = self.scope_task()
        separate = self.call("task.add", task={"role": "class", "mode": "structure",
            "object_id": "original-interface", "class_id": "original-interface",
            "description": "Already assigned structure"})["task"]
        before = (self.workspace / "coordination/state.json").read_bytes()
        with self.assertRaisesRegex(ReplicaError, "logical structural owner"):
            self.call(**request)
        self.assertEqual(before, (self.workspace / "coordination/state.json").read_bytes())
        self.assertEqual(self.call("status")["classes"]["original-interface"]["owner"], separate["id"])

    def test_combined_publish_requires_fresh_evidence_and_started_configured_model(self):
        task, run, request = self.scope_task()
        for changed in ({"evidence": []}, {"structure": {}},
                        {"evidence": [{**request["evidence"][0], "sha256": "0" * 64}]}):
            with self.subTest(changed=changed), self.assertRaises(ReplicaError):
                self.call(**{**request, **changed})
        self.call("run.stopped", run_id=run, outcome="failed",
                  observation=self.stop_request(run)["observation"], reported_model="different-fixture-model")
        with self.assertRaisesRegex(ReplicaError, "no longer owns"):
            self.call(**request)

    def test_combined_publish_rejects_different_structure_profile(self):
        task, run, request = self.scope_task()
        # A different class profile is a legitimate user choice, not permission
        # to silently perform its work with this analysis worker.
        with Store(self.workspace).locked() as state:
            state["models"]["class"]["effort"] = "high"
            Store(self.workspace).commit(state)
        with self.assertRaisesRegex(ReplicaError, "structure model/effort"):
            self.call(**request)

    def test_combined_submission_missing_revision_or_failed_check_stays_invalid(self):
        task, run, request = self.scope_task()
        self.call(**request)
        self.assertFalse(self.submit(task, run)["delivery"]["accepted"])
        for result in ("unknown", "fail"):
            submitted = self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[], unresolved=[],
                class_revisions={"original-interface": 1},
                self_checks=[{"name": "unresolved declarations", "result": result, "evidence": ["fixture"]}])
            self.assertFalse(submitted["delivery"]["accepted"])
        self.stop(run)
        self.assertFalse(self.call("status", task_id=task["id"])["task"]["current_delivery"])

    def test_combined_structure_change_and_requirement_removal_cannot_bypass_owner(self):
        task, run, request = self.scope_task()
        cls = self.call(**request)["class"]
        self.submit(task, run, class_revisions={cls["id"]: 1})
        self.stop(run)
        writer = self.call("task.add", task={"role": "class", "mode": "code", "object_id": cls["id"],
            "class_id": cls["id"], "description": "Fixture dependent writer"})["task"]
        app = self.task("app", role="app", mode="plan")
        with self.assertRaisesRegex(ReplicaError, "combined scope/structure delivery prerequisite"):
            self.call("task.requires.replace", task_id=writer["id"], actor=app["id"],
                expected_revision=writer["revision"], expected_requires=writer["requires"],
                requires=[{"class_id": cls["id"], "revision": 1}], reason="Fixture removal attempt",
                evidence=request["evidence"])
        self.call("class.publish", class_id=cls["id"], owner=task["id"], expected_revision=1,
                  structure={"kind": "interface", "methods": ["newMethod"]}, sources=request["evidence"])
        self.assertIsNone(self.call("status", task_id=task["id"])["task"]["delivery_token"])
        self.assertIsNone(self.call("task.claim", task_id=writer["id"], backend="native")["run"])
        rediscovered = self.call("task.discover", task_id="caller", target=cls["id"], source="same class")
        self.assertEqual(rediscovered["task_id"], task["id"])

    def test_targeted_status_validates_transitive_inputs_without_unrelated_work(self):
        source = self.file("input.txt", "configuration")
        first = self.task("dependency")
        run = self.start(first)
        self.submit(first, run, inputs=[source])
        self.stop(run)
        token = self.call("status", task_id=first["id"])["task"]["delivery_token"]
        second = self.task("consumer", requires=[{"task_id": first["id"]}])
        run = self.start(second)
        self.submit(second, run, dependency_tokens={first["id"]: token})
        self.stop(run)
        unrelated = self.task("unrelated")
        run = self.start(unrelated)
        other_input = self.file("unrelated-input.txt", "unrelated")
        self.submit(unrelated, run, inputs=[other_input])
        self.stop(run)
        original = engine._requirement_evidence
        reads = []

        def observe(store, state, item):
            reads.append(item["path"])
            return original(store, state, item)

        with patch.object(engine, "_requirement_evidence", side_effect=observe), \
             patch.object(Store, "publish", side_effect=AssertionError("Unrelated projection scan")), \
             patch.object(engine, "_ready_tasks", side_effect=AssertionError("Unrelated scheduling scan")):
            current = self.call("status", task_id=second["id"])
        self.assertTrue(current["task"]["current_delivery"])
        self.assertIn(source["path"], reads)
        self.assertNotIn(other_input["path"], reads)
        Path(source["path"]).write_text("changed configuration")
        self.assertIsNone(self.call("status", task_id=second["id"])["task"]["delivery_token"])
        self.assertEqual(current["coordination_protocol"], 2)

    def test_native_stop_next_preserves_stop_gate_and_replays_pending_reservation(self):
        first, second = self.task("first"), self.task("second")
        run = self.start(first)
        self.submit(first, run)
        stop = self.stop_request(run)
        before = (self.workspace / "coordination/state.json").read_bytes()
        with self.assertRaisesRegex(ReplicaError, "writer-descendant"):
            native_next(self.workspace, external_active=1, stopped={
                **stop, "observation": {**stop["observation"], "writers_stopped": False}})
        self.assertEqual(before, (self.workspace / "coordination/state.json").read_bytes())
        next_run = native_next(self.workspace, external_active=1, stopped=stop)
        self.assertTrue(next_run["stopped"]["released"])
        self.assertEqual(next_run["task_id"], second["id"])
        retry = native_next(self.workspace, external_active=1, stopped=stop)
        self.assertTrue(retry["recovery"])
        self.assertEqual(next_run["run_id"], retry["run_id"])
        self.assertEqual(next_run["stopped"], retry["stopped"])
        state = self.call("status")
        self.assertEqual(state["occupied"], 1)
        self.assertEqual(state["runs"][run]["state"], "stopped")
        self.assertEqual(state["runs"][next_run["run_id"]]["state"], "reserved")

    def test_targeted_dispatch_checks_dependencies_without_global_audits(self):
        source = self.file("needed.txt", "required input")
        dependency = self.task("dependency")
        run = self.start(dependency)
        self.submit(dependency, run, inputs=[source])
        self.stop(run)
        selected = self.task("selected", requires=[{"task_id": dependency["id"]}])
        unrelated = self.task("unrelated")
        run = self.start(unrelated)
        other = self.file("unrelated.txt", "old input")
        self.submit(unrelated, run, inputs=[other])
        self.stop(run)
        Path(other["path"]).unlink()
        reads = []
        original = engine._requirement_evidence

        def observe(store, state, item):
            reads.append(item["path"])
            return original(store, state, item)

        with patch.object(Store, "publish", side_effect=AssertionError("Projection audit")):
            snapshot = self.call("dispatch.inspect", task_id=selected["id"])
        self.assertEqual(snapshot["scope"], "targeted_dispatch")
        self.assertNotIn("current_delivery", snapshot["tasks"][selected["id"]])
        with patch.object(engine, "_requirement_evidence", side_effect=observe), \
             patch.object(engine, "_ready_tasks", side_effect=AssertionError("Global scheduling")), \
             patch.object(engine, "_followup_summary", side_effect=AssertionError("Global followups")), \
             patch.object(engine, "_mvp_current", side_effect=AssertionError("Global MVP audit")):
            result = native_next(self.workspace, external_active=1, task_id=selected["id"])
        self.assertEqual(result["task_id"], selected["id"])
        self.assertIn(source["path"], reads)
        self.assertNotIn(other["path"], reads)

    def test_targeted_dispatch_rejects_changed_and_missing_transitive_inputs(self):
        source = self.file("transitive.txt", "original")
        first = self.task("first")
        run = self.start(first)
        self.submit(first, run, inputs=[source])
        self.stop(run)
        token = self.call("status", task_id=first["id"])["task"]["delivery_token"]
        second = self.task("second", requires=[{"task_id": first["id"]}])
        run = self.start(second)
        self.submit(second, run, dependency_tokens={first["id"]: token})
        self.stop(run)
        selected = self.task("selected", requires=[{"task_id": second["id"]}])
        for missing in (False, True):
            with self.subTest(missing=missing):
                path = Path(source["path"])
                path.unlink() if missing else path.write_text("changed")
                self.assertNotIn(selected["id"], self.call("status")["ready_task_ids"])
                result = native_next(self.workspace, external_active=0, task_id=selected["id"])
                self.assertIsNone(result["run"])
                self.assertEqual(result["reason"], "no_ready_task")

    def test_targeted_dispatch_revalidates_after_scheduling_snapshot(self):
        from workbench.app_replica.runner import _resume_ready

        source = self.file("racing-input.txt", "original")
        dependency = self.task("dependency")
        run = self.start(dependency)
        self.submit(dependency, run, inputs=[source])
        self.stop(run)
        selected = self.task("selected", requires=[{"task_id": dependency["id"]}])

        def change_after_read(*args, **kwargs):
            snapshot = _resume_ready(*args, **kwargs)
            Path(source["path"]).write_text("changed after snapshot")
            return snapshot

        with patch("workbench.app_replica.runner._resume_ready", side_effect=change_after_read):
            result = native_next(self.workspace, external_active=0, task_id=selected["id"])
        self.assertIsNone(result["run"])
        self.assertIsNone(self.call("status", task_id=selected["id"])["task"]["run_id"])

    def test_targeted_dispatch_recovers_any_pending_run_and_preserves_prompt(self):
        selected = self.task("selected", outputs=["artifacts/a", "artifacts/b"])
        self.task("other")
        first = native_next(self.workspace, external_active=1, task_id=selected["id"])
        for target in ("other", "not-yet-registered"):
            recovered = native_next(self.workspace, external_active=1, task_id=target)
            self.assertTrue(recovered["recovery"])
            self.assertEqual(recovered["run_id"], first["run_id"])
            self.assertEqual(recovered["directive"], first["directive"])
        global_recovery = native_next(self.workspace, external_active=1)
        self.assertEqual(global_recovery["directive"], first["directive"])
        self.assertEqual(len(self.call("status")["runs"]), 1)

    def test_targeted_dispatch_only_resumes_requested_waiter(self):
        for identifier in ("selected", "other"):
            task = self.task(identifier)
            run = self.start(task)
            self.call("task.wait", task_id=identifier, run_id=run,
                      requires=[{"message_id": identifier + "-ready"}], checkpoint="waiting")
            self.call(**{**self.stop_request(run), "outcome": "waiting"})
            self.call("message.send", message_id=identifier + "-ready", type="answer",
                      sender="main", recipient=identifier, payload={})
        first = native_next(self.workspace, external_active=1, task_id="selected")
        self.assertEqual(first["task_id"], "selected")
        self.assertEqual(self.call("status", task_id="other")["task"]["state"], "waiting")
        recovered = native_next(self.workspace, external_active=1, task_id="selected")
        self.assertEqual(first["run_id"], recovered["run_id"])
        self.assertEqual(len(self.call("status", task_id="selected")["task"]["runs"]), 2)

    def test_targeted_dispatch_preserves_capacity_and_dispatch_lock(self):
        occupied, selected = self.task("occupied"), self.task("selected")
        self.start(occupied)
        result = native_next(self.workspace, external_active=1, task_id=selected["id"])
        self.assertEqual(result["reason"], "capacity")
        self.assertIsNone(result["run"])
        with dispatch_lock(self.workspace), self.assertRaisesRegex(ReplicaError, "Another dispatcher"):
            native_next(self.workspace, external_active=0, task_id=selected["id"])
        with self.assertRaisesRegex(ReplicaError, "Unknown tasks id"):
            native_next(self.workspace, external_active=0, task_id="missing")

    def test_targeted_planning_wake_keeps_explicit_reply_dependency(self):
        app, child = self.task("app", role="app", mode="plan"), self.task("child")
        run = self.start(app)
        self.call("task.wait", task_id=app["id"], run_id=run,
                  requires=[{"task_id": child["id"], "when": "stopped"}, {"message_id": "specific-reply"}],
                  checkpoint="waiting for concrete result")
        self.call(**{**self.stop_request(run), "outcome": "waiting"})
        self.start(child)
        self.call("message.send", message_id="new-request", type="request", sender="main",
                  recipient=app["id"], wake=True, payload={})
        self.assertEqual(self.call("dispatch.inspect", task_id=app["id"])["resumable_task_ids"], [])
        blocked = native_next(self.workspace, external_active=0, task_id=app["id"])
        self.assertEqual(blocked["reason"], "no_ready_task")
        self.call("message.send", message_id="specific-reply", type="answer", sender="main",
                  recipient=app["id"], payload={})
        targeted = self.call("dispatch.inspect", task_id=app["id"])
        whole = self.call("status")
        self.assertEqual(targeted["resumable_task_ids"], whole["resumable_task_ids"])
        self.assertEqual(targeted["planning_wake_messages"], whole["planning_wake_messages"])
        started = native_next(self.workspace, external_active=0, task_id=app["id"])
        self.assertEqual(started["task_id"], app["id"])

    def test_native_stop_next_rejects_other_backend_and_invalid_external_count(self):
        task = self.task("worker")
        run = self.call("task.claim", task_id=task["id"], backend="host-fixture")["run"]
        stop = self.stop_request(run["id"])
        for count in (-1, True):
            with self.assertRaisesRegex(ReplicaError, "nonnegative integer"):
                native_next(self.workspace, external_active=count, stopped=stop)
        with self.assertRaisesRegex(ReplicaError, "native run"):
            native_next(self.workspace, external_active=0, stopped=stop)
        self.assertEqual(self.call("status")["runs"][run["id"]]["state"], "reserved")

    def test_native_stop_next_recovers_faults_after_stop_and_after_reservation(self):
        first, second = self.task("first"), self.task("second")
        run = self.start(first)
        self.submit(first, run)
        stop = {**self.stop_request(run), "request_id": "observed-stop"}
        with patch("workbench.app_replica.runner._resume_ready", side_effect=RuntimeError("after stop")), \
             self.assertRaisesRegex(RuntimeError, "after stop"):
            native_next(self.workspace, external_active=1, stopped=stop)
        status = self.call("status")
        self.assertEqual(status["runs"][run]["state"], "stopped")
        self.assertEqual(status["occupied"], 0)
        with patch("workbench.app_replica.runner.task_prompt", side_effect=RuntimeError("after reservation")), \
             self.assertRaisesRegex(RuntimeError, "after reservation"):
            native_next(self.workspace, external_active=1, stopped=stop)
        pending = self.call("status")["tasks"][second["id"]]["run_id"]
        recovered = native_next(self.workspace, external_active=1, stopped=stop)
        self.assertTrue(recovered["recovery"])
        self.assertEqual(recovered["run_id"], pending)
        self.assertEqual(len(self.call("status")["runs"]), 2)
        with self.assertRaisesRegex(ReplicaError, "request_id already used for different content"):
            native_next(self.workspace, external_active=1, stopped={**stop, "outcome": "failed"})
        self.assertEqual(self.call("status")["runs"][run]["outcome"], "delivered")

    def test_cli_stop_file_and_targeted_status(self):
        task = self.task("only-task")
        run = self.start(task)
        self.submit(task, run)
        stop = self.file("stop.json", json.dumps(self.stop_request(run)))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--workspace", str(self.workspace), "native-next",
                         "--external-active", "0", "--stopped-file", stop["path"]])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["stopped"]["released"])
        self.assertIsNone(json.loads(output.getvalue())["task"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--workspace", str(self.workspace), "status", "--task", task["id"]])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["task"]["current_delivery"])


if __name__ == "__main__":
    unittest.main()
