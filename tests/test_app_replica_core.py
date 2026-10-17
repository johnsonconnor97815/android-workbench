"""Workflow tests use files/processes on the host, never Android/runtime proof."""

import hashlib
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.app_replica import ReplicaError, execute
from workbench.app_replica.ids import object_id
from workbench.app_replica.store import Store


def concurrent_claim(workspace, number, queue, task_id=None):
    try:
        queue.put(execute(workspace, {"op": "task.claim", "request_id": f"claim-{number}",
                                      "backend": {"kind": "host_fixture", "worker": number},
                                      **({"task_id": task_id} if task_id else {})}))
    except Exception as exc:
        queue.put({"error": repr(exc)})


class ReplicaCoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-replica-core-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.counter = 0
        self.call("init", project={"id": "sample", "source_path": "/original/source"}, capacity=2)

    def call(self, op, **kwargs):
        # These compatibility assertions intentionally inspect full inventory.
        if op == "status" and "summary" not in kwargs:
            kwargs.setdefault("full", True)
        self.counter += 1
        if op not in {"status", "recover", "coverage"}:
            kwargs.setdefault("request_id", f"request-{self.counter}")
        return execute(self.workspace, {"op": op, **kwargs})

    def inventory(self, name="Lsample/A;", kind="class", **fields):
        identifier = object_id("dex:original-sha256", kind, name)
        obj = {"id": identifier, "kind": kind, "name": name, "signature": name,
               "carrier": "dex:original-sha256", "source": {"path": "source.java", "line": 1}, **fields}
        self.call("inventory.add", objects=[obj])
        return identifier

    def scope(self, identifier, disposition="copy"):
        return self.call("scope.set", object_id=identifier, disposition=disposition,
                         reason="Explicit fixture decision", evidence=["fixture source"])

    def task(self, identifier=None, role="analysis", mode="inspect", **fields):
        spec = {"role": role, "mode": mode, "description": "Scoped fixture work", **fields}
        if identifier:
            spec["object_id"] = identifier
        else:
            spec.setdefault("id", f"work-{self.counter}")
        return self.call("task.add", task=spec)["task"]

    def started(self, task):
        claim = self.call("task.claim", task_id=task["id"], backend={"kind": "host_fixture"})
        self.assertIsNotNone(claim["run"], claim)
        run_id = claim["run"]["id"]
        self.call("run.started", run_id=run_id, backend_handle={"thread_id": "fixture-thread"})
        return run_id

    def stopped(self, run_id, outcome="delivered"):
        return self.call("run.stopped", run_id=run_id, outcome=outcome,
                         observation={"confirmed": True, "writers_stopped": True,
                                      "evidence": ["host fixture process/descendant stop"]})

    def artifact(self, path="artifacts/result.txt", content="fixture result"):
        target = self.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {"path": path, "sha256": hashlib.sha256(content.encode()).hexdigest()}

    def submit(self, task, run, **fields):
        return self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[],
                         self_checks=[{"name": "host fixture assertion", "result": "pass", "evidence": ["fixture"]}],
                         unresolved=[], **fields)

    def publish(self, class_id, **fields):
        status = self.call("status")
        cls = status["classes"].get(class_id, {"owner": None, "revision": 0})
        return self.call("class.publish", class_id=class_id, owner=cls["owner"] or "structural-owner",
                         expected_revision=cls["revision"], structure={"fields": []}, **fields)

    def test_init_has_unknown_empty_inventory_and_replays_exact_receipt(self):
        request = {"op": "task.add", "request_id": "repeat", "task": {
            "id": "analysis", "role": "analysis", "mode": "architecture", "description": "Read originals"}}
        first = execute(self.workspace, request)
        second = execute(self.workspace, request)
        self.assertEqual(first, second)
        self.assertEqual(len(self.call("status")["tasks"]), 1)
        request["task"]["description"] = "Different work"
        with self.assertRaisesRegex(ReplicaError, "different content"):
            execute(self.workspace, request)
        status = self.call("status")
        self.assertEqual(status["inventory"], {})
        self.assertFalse(status["inventory_status"]["complete"])

    def test_analysis_root_validated_at_init(self):
        for number, value in enumerate((None, "", "  ", [], {}, 42)):
            with self.subTest(value=value):
                workspace = self.workspace / f"invalid-analysis-{number}"
                with self.assertRaisesRegex(ReplicaError, "project analysis_root must be a nonempty string"):
                    execute(workspace, {"op": "init", "request_id": "init",
                                        "project": {"id": "invalid", "analysis_root": value}, "capacity": 1})
                self.assertFalse((workspace / "coordination/state.json").exists())

    def test_analysis_root_claim_and_submit_absolute_and_relative_paths(self):
        for name, declared in (("absolute", str(self.workspace / "absolute-analysis")),
                               ("relative", "../relative-analysis")):
            with self.subTest(name=name):
                workspace = self.workspace / name
                execute(workspace, {"op": "init", "request_id": "init",
                                    "project": {"id": name, "analysis_root": declared}, "capacity": 1})
                root = (workspace / declared).resolve()
                output = root / "evidence.json"
                claim_path = str(output) if name == "absolute" else declared + "/evidence.json"
                execute(workspace, {"op": "task.add", "request_id": "task", "task": {
                    "id": "analysis", "role": "analysis", "mode": "architecture",
                    "description": "Collect evidence", "outputs": [claim_path]}})
                with self.assertRaisesRegex(ReplicaError, "logical writer"):
                    execute(workspace, {"op": "task.add", "request_id": "other", "task": {
                        "id": "other", "role": "analysis", "mode": "inspect",
                        "description": "Duplicate writer", "outputs": [str(root / "nested/../evidence.json")]}})
                run = execute(workspace, {"op": "task.claim", "request_id": "claim",
                                          "task_id": "analysis", "backend": "host_fixture"})["run"]["id"]
                execute(workspace, {"op": "run.started", "request_id": "start", "run_id": run,
                                    "backend_handle": "host-fixture"})
                root.mkdir(parents=True)
                output.write_text("evidence")
                result = execute(workspace, {"op": "task.submit", "request_id": "submit",
                    "task_id": "analysis", "run_id": run,
                    "artifacts": [{"path": claim_path, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}],
                    "self_checks": [{"name": "fixture", "result": "pass", "evidence": ["host assertion"]}],
                    "unresolved": []})
                self.assertTrue(result["delivery"]["accepted"])
                self.assertEqual(result["delivery"]["artifacts"][0]["path"], str(output))

    def test_analysis_root_keeps_path_and_metadata_guards(self):
        workspace = self.workspace / "workflow"
        root = self.workspace / "analysis"
        execute(workspace, {"op": "init", "request_id": "init",
                            "project": {"id": "guards", "analysis_root": str(root)}, "capacity": 1})
        root.mkdir()
        (root / "escape").symlink_to(self.workspace, target_is_directory=True)
        for number, path in enumerate((root / "../outside.json", root / "escape/outside.json")):
            with self.subTest(path=path), self.assertRaisesRegex(ReplicaError, "outside artifacts"):
                execute(workspace, {"op": "task.add", "request_id": f"escape-{number}", "task": {
                    "id": f"escape-{number}", "role": "analysis", "mode": "inspect",
                    "description": "Must reject escape", "outputs": [str(path)]}})
        broad = self.workspace / "broad"
        execute(broad, {"op": "init", "request_id": "init",
                        "project": {"id": "broad", "analysis_root": str(broad)}, "capacity": 1})
        for name in ("coordination/state.json", "inventory/object.json", "classes/class.json",
                     "tasks/task/task.json", "messages/message.json", "mvp/scene/result.json", "project.json"):
            with self.subTest(metadata=name), self.assertRaisesRegex(ReplicaError, "workflow metadata"):
                execute(broad, {"op": "task.add", "request_id": "metadata-" + name.replace("/", "-"), "task": {
                    "id": "metadata", "role": "analysis", "mode": "inspect",
                    "description": "Must reject metadata", "outputs": [str(broad / name)]}})

    def test_analysis_root_cannot_be_added_or_changed_by_project_update(self):
        with self.assertRaisesRegex(ReplicaError, "immutable"):
            self.call("project.update", changes={"analysis_root": "new-analysis"}, evidence=["drift"])
        workspace = self.workspace / "declared"
        execute(workspace, {"op": "init", "request_id": "init",
                            "project": {"id": "declared", "analysis_root": "analysis"}, "capacity": 1})
        with self.assertRaisesRegex(ReplicaError, "immutable"):
            execute(workspace, {"op": "project.update", "request_id": "change",
                                "changes": {"analysis_root": "elsewhere"}, "evidence": ["drift"]})
        self.assertEqual(execute(workspace, {"op": "status"})["project"]["analysis_root"], "analysis")

    def test_inventory_stable_original_identity_and_no_eager_graph(self):
        cls = self.inventory()
        member = self.inventory("Lsample/A;->run()V", "method", class_id=cls)
        status = self.call("status")
        self.assertEqual(status["tasks"], {})
        self.assertEqual(status["inventory"][member]["scope"]["disposition"], "unknown")
        with self.assertRaisesRegex(ReplicaError, "original carrier"):
            self.call("inventory.add", objects=[{"id": cls, "kind": "class", "name": "Renamed",
                                                 "carrier": "changed", "signature": "changed", "source": "x"}])

    def test_same_original_with_multiple_physical_sources_is_merged(self):
        identifier = self.inventory()
        obj = self.call("status")["inventory"][identifier]
        obj.pop("scope")
        obj["source"] = {"path": "alternate-index.sqlite", "row": 2}
        self.call("inventory.add", objects=[obj])
        merged = self.call("status")["inventory"][identifier]
        self.assertEqual(len(merged["sources"]), 2)
        self.assertEqual(merged["source"], {"path": "source.java", "line": 1})

    def test_discovery_deduplicates_callers_and_requires_only_structure(self):
        cls = self.inventory()
        target = self.inventory("Lsample/A;->target()V", "method", class_id=cls)
        self.scope(target)
        callers = [self.task(id=f"caller-{number}") for number in range(2)]
        discovered = [self.call("task.discover", task_id=caller["id"], target=target,
                                source={"path": "Caller.java", "line": number + 1}) for number, caller in enumerate(callers)]
        self.assertEqual(discovered[0]["task_id"], discovered[1]["task_id"])
        status = self.call("status")
        task = status["tasks"][discovered[0]["task_id"]]
        self.assertEqual(set(task["callers"]), {caller["id"] for caller in callers})
        self.assertEqual(len(task["origins"]), 2)
        self.assertEqual(task["requires"], [{"class_id": cls, "revision": 1}])
        self.assertEqual(len(status["tasks"]), 4)  # two callers, structure, one method
        self.assertEqual(status["tasks"][callers[0]["id"]]["requires"], [])
        self.assertNotIn(task["id"], status["ready_task_ids"])
        self.publish(cls)
        self.assertIn(task["id"], self.call("status")["ready_task_ids"])

    def test_unknown_scope_routes_analysis_and_reuse_does_not_implement(self):
        target = self.inventory("Lsample/U;->run()V", "method")
        caller = self.task()
        discovery = self.call("task.discover", task_id=caller["id"], target=target, source="line 3")
        self.assertEqual(discovery["task"]["role"], "analysis")
        self.scope(target, "reuse")
        count = len(self.call("status")["tasks"])
        next_discovery = self.call("task.discover", task_id=caller["id"], target=target, source="line 4")
        self.assertIsNone(next_discovery["task_id"])
        self.assertEqual(count, len(self.call("status")["tasks"]))

    def test_concurrent_process_claims_obey_capacity_and_unique_ownership(self):
        for number in range(8):
            self.task(id=f"work-{number}")
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        children = [context.Process(target=concurrent_claim, args=(str(self.workspace), number, queue)) for number in range(8)]
        for child in children:
            child.start()
        results = [queue.get(timeout=10) for child in children]
        for child in children:
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        self.assertFalse(any("error" in result for result in results), results)
        claims = [result for result in results if result.get("run")]
        self.assertEqual(len(claims), 2)
        self.assertEqual(len({result["task"]["id"] for result in claims}), 2)
        self.assertEqual(self.call("status")["occupied"], 2)

    def test_claim_observed_external_capacity_and_reserved_unknown_are_occupied(self):
        first = self.task()
        self.assertIsNone(self.call("task.claim", backend="native", external_active=2)["run"])
        result = self.call("task.claim", task_id=first["id"], backend="native", external_active=1)
        self.assertEqual(result["run"]["state"], "reserved")
        other = self.task()
        self.assertIsNone(self.call("task.claim", task_id=other["id"], backend="native", external_active=1)["run"])
        self.call("recover")
        self.assertEqual(self.call("status")["occupied"], 1)

    def test_wait_does_not_release_until_confirmed_stop_and_message_wakes(self):
        task = self.task()
        run = self.started(task)
        self.call("task.wait", task_id=task["id"], run_id=run,
                  requires=[{"message_id": "answer"}], checkpoint={"progress": "saved"})
        self.assertEqual(self.call("status")["occupied"], 1)
        with self.assertRaisesRegex(ReplicaError, "unconfirmed"):
            self.call("task.resume", task_id=task["id"], reason="Ready", evidence=["message"])
        with self.assertRaisesRegex(ReplicaError, "descendant"):
            self.call("run.stopped", run_id=run, outcome="waiting", observation={"confirmed": True, "evidence": ["pid exited"]})
        self.assertEqual(self.call("status")["occupied"], 1)
        self.stopped(run, "waiting")
        self.assertNotIn(task["id"], self.call("status")["resumable_task_ids"])
        self.call("message.send", message_id="answer", type="answer", sender="class-owner", recipient=task["id"], payload={"answer": "published"})
        self.assertIn(task["id"], self.call("status")["resumable_task_ids"])
        self.call("task.resume", task_id=task["id"], reason="Answer received", evidence=["answer"])
        claim = self.call("task.claim", task_id=task["id"], backend="native")
        self.assertEqual(claim["run"]["continuation"], {"thread_id": "fixture-thread"})

    def test_delivery_is_not_dependency_ready_until_stop(self):
        task = self.task()
        dependent = self.task(requires=[task["id"]])
        run = self.started(task)
        self.submit(task, run)
        self.assertEqual(self.call("status")["occupied"], 1)
        self.assertNotIn(dependent["id"], self.call("status")["ready_task_ids"])
        self.stopped(run)
        self.assertIn(dependent["id"], self.call("status")["ready_task_ids"])

    def test_class_revisions_isolated_and_stale_delivery_is_preserved(self):
        cls = self.inventory()
        unrelated = self.inventory("Lsample/B;")
        member = self.inventory("Lsample/A;->run()V", "method", class_id=cls)
        artifact = self.artifact("artifacts/member.java")
        task = self.task(member, role="function", mode="code", outputs=[artifact["path"]])
        self.publish(cls)
        run = self.started(task)
        self.publish(unrelated)
        delivery = self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[artifact],
                             self_checks=[{"name": "fixture", "result": "pass", "evidence": ["asserted"]}],
                             unresolved=[], class_revisions={cls: 1})
        self.assertFalse(delivery["delivery"]["stale"])
        self.publish(cls)
        stopped = self.stopped(run)
        self.assertTrue(stopped["task"]["deliveries"][-1]["stale"])
        self.assertFalse(stopped["task"]["deliveries"][-1]["accepted"])
        with self.assertRaisesRegex(ReplicaError, "Stale class"):
            self.call("class.publish", class_id=cls, owner=self.call("status")["classes"][cls]["owner"],
                      expected_revision=1, structure={"fields": ["overwritten"]})
        self.assertEqual(self.call("status")["classes"][cls]["revision"], 2)

    def test_class_code_requires_structure_and_uses_sol(self):
        cls = self.inventory()
        task = self.task(cls, role="class", mode="code", class_id=cls)
        self.assertNotIn(task["id"], self.call("status")["ready_task_ids"])
        self.publish(cls)
        claim = self.call("task.claim", task_id=task["id"], backend="native")
        self.assertEqual(claim["model"], "gpt-6.1-sol")
        self.assertEqual(claim["effort"], "high")

    def test_terminal_wait_wakes_app_after_failure_and_success_wait_stays_blocked(self):
        dependency = self.task(id="work")
        planning = self.task(id="app-planning", role="app", mode="plan")
        implementation = self.task(id="implementation", requires=[dependency["id"]])
        app_run = self.started(planning)
        self.call("task.wait", task_id=planning["id"], run_id=app_run,
                  requires=[{"task_id": dependency["id"], "when": "stopped"}], checkpoint="await outcome")
        self.stopped(app_run, "waiting")
        run = self.started(dependency)
        self.stopped(run, "failed")
        status = self.call("status")
        self.assertIn(planning["id"], status["resumable_task_ids"])
        self.assertNotIn(implementation["id"], status["ready_task_ids"])
        self.call("task.resume", task_id=dependency["id"], reason="Repair the failed child", evidence=["failed run"])
        self.assertNotIn(planning["id"], self.call("status")["resumable_task_ids"])
        repair = self.started(dependency)
        self.assertNotIn(planning["id"], self.call("status")["resumable_task_ids"])
        self.stopped(repair, "failed")
        self.assertIn(planning["id"], self.call("status")["resumable_task_ids"])
        self.call("task.resume", task_id=planning["id"], reason="Inspect failed child", evidence=["stopped run"])

    def test_actionable_message_wakes_idle_owner_once_without_duplicate_live_run(self):
        task = self.task(id="structure-owner")
        run = self.started(task)
        self.submit(task, run)
        self.call("message.send", message_id="question", type="request", sender="caller",
                  recipient=task["id"], payload="new structural question", wake=True)
        self.assertNotIn(task["id"], self.call("status")["wake_task_ids"])
        self.stopped(run)
        self.assertIn(task["id"], self.call("status")["wake_task_ids"])
        self.call("task.resume", task_id=task["id"], reason="Handle durable question", evidence=["question"])
        next_run = self.started(task)
        self.assertEqual(self.call("status")["runs"][next_run]["inbox_message_ids"], ["question"])
        self.submit(task, next_run)
        self.stopped(next_run)
        self.assertNotIn(task["id"], self.call("status")["wake_task_ids"])
        self.call("message.send", message_id="note", type="note", sender="caller",
                  recipient=task["id"], payload="FYI")
        self.assertNotIn(task["id"], self.call("status")["wake_task_ids"])

    def test_app_planning_wake_defers_restarted_child_observation_once(self):
        child = self.task(id="observed-child")
        app = self.task(id="app-planning", role="app", mode="plan")
        run = self.started(app)
        requires = [{"task_id": child["id"], "when": "stopped"}]
        self.call("task.wait", task_id=app["id"], run_id=run,
                  requires=requires, checkpoint="observe child")
        self.stopped(run, "waiting")
        child_run = self.started(child)
        self.stopped(child_run, "failed")
        self.assertIn(app["id"], self.call("status")["resumable_task_ids"])
        self.call("task.resume", task_id=child["id"], reason="New child work", evidence=["fixture"])
        self.assertNotIn(app["id"], self.call("status")["resumable_task_ids"])
        self.call("message.send", message_id="note", type="note", sender="main",
                  recipient=app["id"], payload="No wake requested")
        self.call("message.send", message_id="other-owner", type="request", sender="main",
                  recipient=child["id"], payload="Different recipient", wake=True)
        with self.assertRaisesRegex(ReplicaError, "prerequisites"):
            self.call("task.resume", task_id=app["id"], reason="No planning wake", evidence=["note"])
        self.call("message.send", message_id="plan-next", type="request", sender="main",
                  recipient=app["id"], payload="Schedule ready entry code", wake=True)
        status = self.call("status")
        self.assertEqual(status["planning_wake_messages"], {app["id"]: ["plan-next"]})
        self.assertIn(app["id"], status["resumable_task_ids"])
        self.assertEqual(status["tasks"][app["id"]]["requires"], requires)
        self.assertEqual(status["tasks"][child["id"]]["state"], "queued")
        self.call("task.resume", task_id=app["id"], reason="Handle planning request", evidence=["plan-next"])
        self.assertIn(app["id"], self.call("status")["ready_task_ids"])
        claim = self.call("task.claim", task_id=app["id"], backend="host_fixture")
        self.assertEqual(claim["run"]["inbox_message_ids"], ["plan-next"])
        self.assertEqual(claim["run"]["continuation"], {"thread_id": "fixture-thread"})
        next_run = claim["run"]["id"]
        self.call("run.started", run_id=next_run, backend_handle={"thread_id": "fixture-thread"})
        self.call("task.wait", task_id=app["id"], run_id=next_run,
                  requires=requires, checkpoint="planning recorded")
        self.stopped(next_run, "waiting")
        # Each execute reloads persisted state: the assigned wake is not reusable.
        status = self.call("status")
        self.assertEqual(status["tasks"][app["id"]]["requires"], requires)
        self.assertNotIn(app["id"], status["resumable_task_ids"])
        self.assertEqual(status["planning_wake_messages"], {})
        self.call("message.send", message_id="plan-again", type="request", sender="main",
                  recipient=app["id"], payload="New independent planning work", wake=True)
        self.assertIn(app["id"], self.call("status")["resumable_task_ids"])

    def test_app_planning_wake_keeps_mixed_hard_prerequisites(self):
        for kind in ("message", "class", "delivery"):
            with self.subTest(kind=kind):
                child = self.task(id=f"observed-{kind}")
                app = self.task(id=f"planning-{kind}", role="app", mode="plan")
                hard_task = None
                if kind == "message":
                    hard = {"message_id": "required-reply"}
                elif kind == "class":
                    class_id = self.inventory("Lsample/Required;")
                    hard = {"class_id": class_id, "revision": 1}
                else:
                    hard_task = self.task(id="required-delivery")
                    hard = {"task_id": hard_task["id"]}
                run = self.started(app)
                requires = [{"task_id": child["id"], "when": "stopped"}, hard]
                self.call("task.wait", task_id=app["id"], run_id=run,
                          requires=requires, checkpoint="needs actual input")
                self.stopped(run, "waiting")
                wake_id = f"planning-wake-{kind}"
                self.call("message.send", message_id=wake_id, type="request", sender="main",
                          recipient=app["id"], payload="Planning work", wake=True)
                status = self.call("status")
                self.assertNotIn(app["id"], status["resumable_task_ids"])
                self.assertNotIn(app["id"], status["planning_wake_messages"])
                with self.assertRaisesRegex(ReplicaError, "prerequisites"):
                    self.call("task.resume", task_id=app["id"], reason="Hard input absent", evidence=[wake_id])
                if kind == "message":
                    self.call("message.send", message_id="required-reply", type="answer", sender="worker",
                              recipient=app["id"], payload="Required answer")
                elif kind == "class":
                    self.publish(class_id)
                else:
                    hard_run = self.started(hard_task)
                    self.submit(hard_task, hard_run)
                    self.assertNotIn(app["id"], self.call("status")["resumable_task_ids"])
                    self.stopped(hard_run)
                status = self.call("status")
                self.assertEqual(status["planning_wake_messages"][app["id"]], [wake_id])
                self.assertEqual(status["tasks"][app["id"]]["requires"], requires)
                self.call("task.resume", task_id=app["id"], reason="Hard input now present", evidence=[wake_id])
                self.assertIn(app["id"], self.call("status")["ready_task_ids"])

    def test_planning_wake_does_not_relax_other_roles_or_app_modes(self):
        for number, (role, mode) in enumerate((("analysis", "inspect"), ("class", "structure"),
                                               ("class", "code"), ("function", "code"), ("app", "inspect"))):
            with self.subTest(role=role, mode=mode):
                child = self.task(id=f"observed-other-{number}")
                owner = self.task(id=f"owner-{number}", role=role, mode=mode)
                run = self.started(owner)
                self.call("task.wait", task_id=owner["id"], run_id=run,
                          requires=[{"task_id": child["id"], "when": "stopped"}], checkpoint="wait")
                self.stopped(run, "waiting")
                self.call("message.send", message_id=f"wake-other-{number}", type="request", sender="main",
                          recipient=owner["id"], payload={"role": "app", "mode": "plan"}, wake=True)
                status = self.call("status")
                self.assertNotIn(owner["id"], status["resumable_task_ids"])
                self.assertNotIn(owner["id"], status["planning_wake_messages"])
                with self.assertRaisesRegex(ReplicaError, "prerequisites"):
                    self.call("task.resume", task_id=owner["id"], reason="Message cannot change role", evidence=["fixture"])

    def test_app_planning_wake_keeps_live_owner_and_atomic_claim_guards(self):
        child = self.task(id="busy-child")
        app = self.task(id="app-planning", role="app", mode="plan")
        run = self.started(app)
        self.call("task.wait", task_id=app["id"], run_id=run,
                  requires=[{"task_id": child["id"], "when": "stopped"}], checkpoint="wait")
        self.call("message.send", message_id="planning-request", type="request", sender="main",
                  recipient=app["id"], payload="Planning work", wake=True)
        self.assertNotIn(app["id"], self.call("status")["resumable_task_ids"])
        self.assertEqual(self.call("status")["planning_wake_messages"], {})
        with self.assertRaisesRegex(ReplicaError, "unconfirmed"):
            self.call("task.resume", task_id=app["id"], reason="Still writing", evidence=["planning-request"])
        self.stopped(run, "waiting")
        self.call("task.resume", task_id=app["id"], reason="Prior writer stopped", evidence=["planning-request"])
        self.assertIsNone(self.call("task.claim", task_id=app["id"], backend="host_fixture", external_active=2)["run"])
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        children = [context.Process(target=concurrent_claim, args=(str(self.workspace), number, queue, app["id"]))
                    for number in range(2)]
        for process in children:
            process.start()
        results = [queue.get(timeout=10) for process in children]
        for process in children:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertFalse(any("error" in result for result in results), results)
        claims = [result for result in results if result.get("run")]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["run"]["inbox_message_ids"], ["planning-request"])
        self.assertEqual(self.call("status")["occupied"], 1)

    def test_failed_planning_launch_retains_message_evidence_without_retry_loop(self):
        child = self.task(id="observed-child")
        app = self.task(id="app-planning", role="app", mode="plan")
        run = self.started(app)
        requires = [{"task_id": child["id"], "when": "stopped"}]
        self.call("task.wait", task_id=app["id"], run_id=run, requires=requires, checkpoint="wait")
        self.stopped(run, "waiting")
        self.call("message.send", message_id="planning-request", type="request", sender="main",
                  recipient=app["id"], payload="Planning work", wake=True)
        self.call("task.resume", task_id=app["id"], reason="Handle request", evidence=["planning-request"])
        claim = self.call("task.claim", task_id=app["id"], backend="host_fixture")
        self.stopped(claim["run"]["id"], "failed")
        status = self.call("status")
        self.assertEqual(status["tasks"][app["id"]]["state"], "failed")
        self.assertEqual(status["tasks"][app["id"]]["requires"], requires)
        self.assertEqual(status["runs"][claim["run"]["id"]]["inbox_message_ids"], ["planning-request"])
        self.assertIn("planning-request", status["messages"])
        self.assertNotIn(app["id"], status["wake_task_ids"])
        self.call("message.send", message_id="launch-repaired", type="request", sender="main",
                  recipient=app["id"], payload="Concrete launch failure repaired", wake=True)
        self.assertIn(app["id"], self.call("status")["wake_task_ids"])

    def test_failure_after_submission_and_final_model_mismatch_invalidate_delivery(self):
        for number, outcome in enumerate(("failed", "interrupted", "delivered")):
            task = self.task(id=f"task-{number}")
            run = self.started(task)
            self.submit(task, run)
            response = self.call("run.stopped", run_id=run, outcome=outcome,
                                 backend_handle={"thread_id": f"final-thread-{number}"},
                                 reported_model="wrong-model" if outcome == "delivered" else "gpt-6-astra",
                                 observation={"confirmed": True, "writers_stopped": True, "evidence": ["fixture process stop"]})
            self.assertFalse(response["task"]["deliveries"][-1]["accepted"])
            self.assertEqual(response["task"]["state"], "failed")
            self.assertEqual(response["run"]["backend_handle"]["thread_id"], f"final-thread-{number}")

    def test_true_wait_cycle_rejected_and_current_state_preserved(self):
        first = self.task(id="first")
        second = self.task(id="second", requires=[first["id"]])
        run = self.started(first)
        with self.assertRaisesRegex(ReplicaError, "cycle"):
            self.call("task.wait", task_id=first["id"], run_id=run,
                      requires=[second["id"]], checkpoint="bad cycle")
        self.assertEqual(self.call("status")["tasks"][first["id"]]["state"], "running")

    def test_failed_backend_after_wait_does_not_silently_resume(self):
        task = self.task()
        run = self.started(task)
        self.call("task.wait", task_id=task["id"], run_id=run,
                  requires=[{"message_id": "answer-after-failure"}], checkpoint="partially saved")
        result = self.stopped(run, "failed")
        self.assertEqual(result["task"]["state"], "failed")
        self.call("message.send", message_id="answer-after-failure", type="answer", sender="app", recipient=task["id"], payload="ready")
        self.assertNotIn(task["id"], self.call("status")["resumable_task_ids"])

    def test_unique_structural_owner_and_output_writer(self):
        cls = self.inventory()
        self.publish(cls)
        with self.assertRaisesRegex(ReplicaError, "structural owner"):
            self.call("class.publish", class_id=cls, owner="different-owner", expected_revision=1, structure={})
        self.task(id="writer", outputs=["artifacts/shared.java"])
        with self.assertRaisesRegex(ReplicaError, "logical writer"):
            self.task(id="other-writer", outputs=["artifacts/shared.java"])
        self.assertNotIn("other-writer", self.call("status")["tasks"])

    def test_artifact_hash_current_file_and_ownership_required(self):
        artifact = self.artifact()
        task = self.task(outputs=[artifact["path"]])
        run = self.started(task)
        with self.assertRaisesRegex(ReplicaError, "SHA-256"):
            self.call("task.submit", task_id=task["id"], run_id=run,
                      artifacts=[{**artifact, "sha256": "bad"}], self_checks=[], unresolved=[])
        result = self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[artifact],
                           self_checks=[{"name": "fixture", "result": "pass", "evidence": ["asserted"]}], unresolved=[])
        self.assertTrue(result["delivery"]["accepted"])
        self.stopped(run)
        (self.workspace / artifact["path"]).write_text("changed after delivery")
        dependent = self.task(requires=[task["id"]])
        self.assertNotIn(dependent["id"], self.call("status")["ready_task_ids"])

    def directory_correction(self, task, directories, **changes):
        current = self.call("status")["tasks"][task["id"]]
        args = {"task_id": task["id"], "owner": task["owner"],
                "expected_revision": current["revision"], "directories": directories,
                "reason": "Directory was mistakenly claimed as a deliverable file",
                "evidence": [self.artifact("artifacts/correction-evidence.json")]}
        return self.call("task.outputs.remove_directories", **{**args, **changes})

    def test_directory_correction_preserves_files_writer_history_and_requires_new_delivery(self):
        artifact = self.artifact("artifacts/compile/nested/result.txt")
        task = self.task(outputs=["artifacts/compile", artifact["path"]])
        run = self.started(task)
        result = self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[artifact],
                           self_checks=[{"name": "fixture", "result": "pass", "evidence": ["fixture"]}], unresolved=[])
        self.assertFalse(result["delivery"]["accepted"])
        self.stopped(run)
        before = self.call("status")["tasks"][task["id"]]
        result = self.directory_correction(task, ["artifacts/compile"], request_id="directory-fix")
        after = self.call("status")
        current = after["tasks"][task["id"]]
        self.assertEqual(current["deliveries"], before["deliveries"])
        self.assertEqual(current["requires"], before["requires"])
        self.assertEqual(current["state"], before["state"])
        self.assertEqual(current["revision"], before["revision"] + 1)
        self.assertEqual(current["owner"], task["owner"])
        path = str(self.workspace / artifact["path"])
        self.assertEqual(current["outputs"], [path])
        self.assertEqual(after["writers"][path], task["id"])
        self.assertNotIn(str(self.workspace / "artifacts/compile"), after["writers"])
        self.assertEqual((self.workspace / artifact["path"]).read_text(), "fixture result")
        # Exact receipt replay neither repeats the correction nor touches files.
        replay = self.directory_correction(task, ["artifacts/compile"], request_id="directory-fix",
                                           expected_revision=before["revision"])
        self.assertEqual(replay, result)
        self.assertEqual(len(self.call("status")["tasks"][task["id"]]["output_directory_corrections"]), 1)
        self.call("task.resume", task_id=task["id"], reason="Submit corrected file-only outputs",
                  evidence=["directory-fix receipt"])
        next_run = self.started(task)
        delivery = self.call("task.submit", task_id=task["id"], run_id=next_run, artifacts=[artifact],
                             self_checks=[{"name": "fixture", "result": "pass", "evidence": ["fixture"]}], unresolved=[])
        self.assertTrue(delivery["delivery"]["accepted"])

    def test_directory_correction_rejects_live_and_waiting_live_task(self):
        artifact = self.artifact("artifacts/compile/result.txt")
        task = self.task(outputs=["artifacts/compile", artifact["path"]])
        run = self.started(task)
        with self.assertRaisesRegex(ReplicaError, "live run"):
            self.directory_correction(task, ["artifacts/compile"])
        self.call("task.wait", task_id=task["id"], run_id=run,
                  requires=[{"message_id": "pending-reply"}], checkpoint="wait")
        with self.assertRaisesRegex(ReplicaError, "live run"):
            self.directory_correction(task, ["artifacts/compile"])

    def test_directory_correction_rejects_files_missing_paths_and_foreign_registration(self):
        artifact = self.artifact("artifacts/compile/result.txt")
        task = self.task(outputs=[artifact["path"], "artifacts/missing"])
        for path in [artifact["path"], "artifacts/missing"]:
            with self.subTest(path=path), self.assertRaisesRegex(ReplicaError, "existing directory"):
                self.directory_correction(task, [path])
        other = self.task(id="other-directory-owner", outputs=["artifacts/compile"])
        with self.assertRaisesRegex(ReplicaError, "not assigned"):
            self.directory_correction(task, ["artifacts/compile"])
        self.assertEqual(self.call("status")["writers"][str(self.workspace / "artifacts/compile")], other["id"])

    def test_directory_correction_requires_child_files_to_retain_same_writer(self):
        artifact = self.artifact("artifacts/compile/result.txt")
        task = self.task(outputs=["artifacts/compile"])
        before = self.call("status")["tasks"][task["id"]]
        with self.assertRaisesRegex(ReplicaError, "Every child file"):
            self.directory_correction(task, ["artifacts/compile"])
        self.task(id="different-child-owner", outputs=[artifact["path"]])
        with self.assertRaisesRegex(ReplicaError, "Every child file"):
            self.directory_correction(task, ["artifacts/compile"])
        self.assertEqual(self.call("status")["tasks"][task["id"]], before)

    def test_directory_correction_rejects_symlink_children_and_is_atomic(self):
        artifact = self.artifact("artifacts/first/result.txt")
        target = self.workspace / "artifacts/second"
        target.mkdir()
        (target / "link").symlink_to(self.workspace / artifact["path"])
        task = self.task(outputs=["artifacts/first", "artifacts/second", artifact["path"]])
        before = self.call("status")["tasks"][task["id"]]
        with self.assertRaisesRegex(ReplicaError, "unsupported filesystem entry"):
            self.directory_correction(task, ["artifacts/first", "artifacts/second"])
        after = self.call("status")
        self.assertEqual(after["tasks"][task["id"]], before)
        self.assertEqual(after["writers"][str(self.workspace / "artifacts/first")], task["id"])

    def test_directory_correction_requires_revision_owner_unique_paths_and_bound_evidence(self):
        (self.workspace / "artifacts/empty").mkdir(parents=True)
        task = self.task(outputs=["artifacts/empty"])
        for changes in [{"expected_revision": 999}, {"owner": "someone-else"},
                        {"directories": []}, {"directories": ["artifacts/empty", "artifacts/empty"]},
                        {"evidence": []}, {"reason": ""},
                        {"evidence": [{"path": "artifacts/empty", "sha256": "invalid"}]}]:
            with self.subTest(changes=changes), self.assertRaises(ReplicaError):
                self.directory_correction(task, changes.pop("directories", ["artifacts/empty"]), **changes)

    def test_unresolved_or_unknown_checks_never_satisfy_dependency(self):
        task = self.task()
        dependent = self.task(requires=[task["id"]])
        run = self.started(task)
        self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[],
                  self_checks=[{"name": "not observed", "result": "unknown", "evidence": ["missing SDK"]}],
                  unresolved=["No device evidence"])
        self.stopped(run)
        self.assertNotIn(dependent["id"], self.call("status")["ready_task_ids"])

    def test_implementation_without_code_artifacts_cannot_be_accepted(self):
        task = self.task(role="function", mode="code")
        run = self.started(task)
        submission = self.submit(task, run)
        self.assertFalse(submission["delivery"]["accepted"])
        self.assertIn("Implementation delivery has no code artifact", submission["delivery"]["stale_reasons"])

    def test_named_pipe_cannot_block_artifact_validation(self):
        import os
        path = self.workspace / "artifacts/pipe"
        path.parent.mkdir()
        os.mkfifo(path)
        task = self.task(outputs=["artifacts/pipe"])
        run = self.started(task)
        with self.assertRaisesRegex(ReplicaError, "regular file"):
            self.call("task.submit", task_id=task["id"], run_id=run,
                      artifacts=[{"path": "artifacts/pipe", "sha256": "not-a-file"}], self_checks=[], unresolved=[])

    def test_projection_failure_commits_receipt_and_recovery_does_not_overwrite_code(self):
        task = self.task()
        artifact = self.artifact()
        before = (self.workspace / artifact["path"]).read_bytes()
        with patch.object(Store, "publish", side_effect=OSError("fixture disk failure")):
            response = self.call("message.send", request_id="committed-message", type="note", sender="app", recipient=task["id"], payload="hello")
        self.assertTrue(response["committed"])
        self.assertIn("fixture disk failure", response["projection_error"])
        replay = self.call("message.send", request_id="committed-message", type="note", sender="app", recipient=task["id"], payload="hello")
        self.assertEqual(response["message"], replay["message"])
        self.assertEqual(len(self.call("status")["messages"]), 1)
        (self.workspace / "tasks" / task["id"] / "task.json").write_text('{"state":"forged"}')
        recovery = self.call("recover")
        projected = json.loads((self.workspace / "tasks" / task["id"] / "task.json").read_text())
        self.assertEqual(projected["state"], "queued")
        self.assertEqual(projected["_projection"]["revision"], recovery["revision"])
        self.assertEqual((self.workspace / artifact["path"]).read_bytes(), before)

    def test_crash_after_authority_commit_replays_single_reservation(self):
        task = self.task()
        request = {"op": "task.claim", "request_id": "crash-claim", "task_id": task["id"], "backend": "host_fixture"}
        script = "from workbench.app_replica.engine import execute\nfrom workbench.app_replica.store import Store\nimport json,os,sys\nStore.publish=lambda *args: os._exit(41)\nexecute(sys.argv[1],json.loads(sys.argv[2]))\n"
        crashed = subprocess.run([sys.executable, "-c", script, str(self.workspace), json.dumps(request)], cwd=ROOT,
                                 capture_output=True, text=True)
        self.assertEqual(crashed.returncode, 41, crashed.stderr)
        replay = execute(self.workspace, request)
        self.assertEqual(replay["run"]["state"], "reserved")
        self.assertEqual(len(self.call("status")["runs"]), 1)
        self.assertEqual(self.call("status")["occupied"], 1)

    def test_authority_commit_failure_does_not_apply_metadata(self):
        revision = self.call("status")["revision"]
        with patch.object(Store, "commit", side_effect=OSError("fixture before rename")):
            with self.assertRaises(OSError):
                self.task(id="uncommitted")
        self.assertEqual(self.call("status")["revision"], revision)
        self.assertNotIn("uncommitted", self.call("status")["tasks"])

    def test_metadata_symlinks_and_output_path_traversal_rejected(self):
        with self.assertRaisesRegex(ReplicaError, "outside artifacts"):
            self.task(outputs=["../../outside.java"])
        cls = self.inventory()
        outside = self.workspace / "other"
        outside.mkdir()
        (self.workspace / "classes").symlink_to(outside, target_is_directory=True)
        response = self.publish(cls)
        self.assertTrue(response["committed"])
        self.assertIn("symlink", response["projection_error"])
        self.assertFalse((outside / f"{cls}.json").exists())

    def test_capacity_not_freed_by_configuration_mismatch_or_recovery(self):
        task = self.task(role="function", mode="code")
        claim = self.call("task.claim", task_id=task["id"], backend="native")
        self.assertEqual(claim["model"], "gpt-6.1-sol")
        started = self.call("run.started", run_id=claim["run"]["id"], backend_handle="real-handle",
                            reported_model="different-model")
        self.assertFalse(started["configuration_match"])
        self.call("recover")
        self.assertEqual(self.call("status")["occupied"], 1)

    def test_project_decision_history_is_durable_and_origin_paths_protected(self):
        self.call("project.update", actor="app", changes={"plan": {"first_entry": "startup"}, "sdk": {"ads": "exclude"}}, evidence=["app decision"])
        self.call("project.update", actor="main", changes={"requirements": ["preserve deep links"]}, evidence=["user request"])
        project = self.call("status")["project"]
        self.assertEqual(project["plan"]["first_entry"], "startup")
        self.assertEqual(len(project["history"]), 2)
        self.assertEqual(project["history"][-1]["actor"], "main")
        with self.assertRaisesRegex(ReplicaError, "immutable"):
            self.call("project.update", changes={"source_path": "/different/original"}, evidence=["unapproved drift"])

    def test_coverage_keeps_unknown_residual_and_native_library_limitation(self):
        copied = self.inventory("Lsample/C;")
        unknown = self.inventory("libnative.so", "native_library", limitations=["Native functions not enumerated"])
        excluded = self.inventory("Lsample/Unused;")
        self.scope(copied)
        self.scope(excluded, "exclude")
        self.call("inventory.add", objects=[], complete=False, limitations=["Native functions not enumerated"])
        result = self.call("coverage")["coverage"]
        self.assertIn(copied, result["groups"]["copy_pending"])
        self.assertIn(unknown, result["groups"]["unknown"])
        self.assertIn(excluded, result["groups"]["exclude"])
        self.assertFalse(result["scope_complete"])

    def mvp_fixture(self):
        obj = self.inventory("Lsample/Stage;->run()V", "method")
        self.scope(obj)
        self.call("entry.add", entry={"id": "startup", "trigger": "Application.onCreate", "root_object_ids": [obj], "evidence": ["manifest fixture"]})
        artifact = self.artifact("artifacts/method.java")
        task = self.task(obj, role="function", mode="code", outputs=[artifact["path"]], entry_ids=["startup"])
        run = self.started(task)
        self.call("task.submit", task_id=task["id"], run_id=run, artifacts=[artifact], unresolved=[],
                  self_checks=[{"name": "fixture source check", "result": "pass", "evidence": ["fixture"]}])
        self.stopped(run)
        self.call("mvp.define", mvp={"id": "first-screen", "entry_ids": ["startup"], "required_task_ids": [task["id"]],
                                    "target_versions": {"original": "1.0", "candidate": "fixture-1"},
                                    "conditions": {"network": "offline"}, "goals": ["opens"]})
        original = self.artifact("evidence/original.json", '{"opens":true}')
        candidate = self.artifact("evidence/candidate.json", '{"opens":true,"fixture":true}')
        request = {"mvp_id": "first-screen", "original_version": "1.0", "candidate_version": "fixture-1",
                   "runtime_scope": {"kind": "host_fixture", "conditions": {"network": "offline"}},
                   "observations": {"original": original, "candidate": candidate},
                   "results": {"opens": {"result": "pass", "original": "original.json#/opens", "candidate": "candidate.json#/opens"}}}
        return task, artifact, request

    def test_paired_stage_specific_results_current_artifacts_and_scope(self):
        task, artifact, request = self.mvp_fixture()
        record = self.call("mvp.record", **request)
        self.assertEqual(record["record"]["result"], "pass")
        self.assertEqual(record["record"]["runtime_scope"]["kind"], "host_fixture")
        self.assertFalse(self.call("coverage")["coverage"]["runtime_verified"])
        self.assertEqual(self.call("status")["mvps"]["first-screen"]["current_result"], "pass")
        (self.workspace / artifact["path"]).write_text("changed")
        self.assertEqual(self.call("status")["mvps"]["first-screen"]["current_result"], "stale")

    def test_discovery_only_associations_do_not_stale_completed_mvp(self):
        task, _, request = self.mvp_fixture()
        self.call("mvp.record", **request)
        original_revision = self.call("status")["tasks"][task["id"]]["revision"]
        caller = self.task(id="new-caller")
        self.call("task.discover", task_id=caller["id"], target=task["object_id"], source="new call line")
        self.call("task.discover", task_id=caller["id"], target=task["object_id"], source="new call line")
        status = self.call("status")
        self.assertEqual(status["tasks"][task["id"]]["revision"], original_revision)
        self.assertEqual(status["mvps"]["first-screen"]["current_result"], "pass")

    def test_goal_references_must_identify_the_paired_observation_files(self):
        _, _, request = self.mvp_fixture()
        request["results"]["opens"]["original"] = "unrelated-evidence.json"
        with self.assertRaisesRegex(ReplicaError, "paired observation file"):
            self.call("mvp.record", **request)

    def test_stage_missing_pair_unknown_or_wrong_versions_cannot_pass(self):
        _, _, request = self.mvp_fixture()
        with self.assertRaisesRegex(ReplicaError, "Paired versions"):
            self.call("mvp.record", **{**request, "candidate_version": "wrong-version"})
        with self.assertRaisesRegex(ReplicaError, "Both original"):
            self.call("mvp.record", **{**request, "observations": {"candidate": request["observations"]["candidate"]}})
        request["results"]["opens"]["result"] = "unknown"
        response = self.call("mvp.record", **request)
        self.assertEqual(response["record"]["result"], "unknown")


if __name__ == "__main__":
    unittest.main()
