"""App dispatch policy tests. Isolated host metadata/CLI fixtures, no live workers."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.app_replica import execute, ReplicaError
from workbench.app_replica.engine import _dispatch_ranks, _ready_tasks
from workbench.app_replica.cli import main
from workbench.app_replica.runner import Runner, native_next
from workbench.app_replica.snapshot import Inventory
from workbench.app_replica.store import Store


class DispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-dispatch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workflow"
        self.n = 0
        self.call("init", project={"id": "dispatch", "workbench_source": str(ROOT),
                                  "execution_root": str(self.root)}, capacity=3)

    def call(self, op, **fields):
        self.n += 1
        if op != "status":
            fields.setdefault("request_id", f"request-{self.n}")
        return execute(self.workspace, {"op": op, **fields})

    def task(self, identifier, timestamp="2026-10-15T00:00:00+00:00", **fields):
        with patch("workbench.app_replica.engine._now", return_value=timestamp):
            return self.call("task.add", task={"id": identifier, "role": "analysis", "mode": "inspect",
                             "description": "host fixture", **fields})["task_id"]

    def policy(self, task_ids, **fields):
        return self.call("dispatch.set", task_ids=task_ids, expected_revision=fields.pop("expected_revision", 0),
                         evidence=["App fixture decision"], **fields)

    def claim(self, **fields):
        return self.call("task.claim", backend="host_fixture", **fields)

    def start(self, identifier):
        claim = self.claim(task_id=identifier)
        run = claim["run"]["id"]
        self.call("run.started", run_id=run, backend_handle={"fixture": True})
        return run

    def stop(self, run, outcome):
        self.call("run.stopped", run_id=run, outcome=outcome,
                  observation={"confirmed": True, "writers_stopped": True, "evidence": ["host fixture"]})

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["--workspace", str(self.workspace), *args])
        self.assertEqual(result, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_oldest_ready_precedes_every_new_lexically_smaller_discovery(self):
        oldest = self.task("zz-old-root")
        for number in range(4):
            obj = {"id": f"method-{number}", "kind": "method", "name": f"new-{number}", "source": "fixture"}
            self.call("inventory.add", objects=[obj])
            self.call("scope.set", object_id=obj["id"], disposition="copy", reason="fixture", evidence=["source"])
            with patch("workbench.app_replica.engine._now", return_value=f"2026-10-15T00:00:0{number + 1}+00:00"):
                discovered = self.call("task.discover", task_id=oldest, target=obj["id"], source="fixture:1")["task_id"]
            self.assertLess(discovered, oldest)
            for summary in (True, False):
                self.assertEqual(self.call("status", summary=summary)["ready_task_ids"][0], oldest)
        self.assertEqual(self.claim()["task"]["id"], oldest)

    def test_default_policy_and_equal_timestamps_use_task_id_tie_break(self):
        self.task("z")
        self.task("a")
        self.task("new", "2026-10-15T00:00:01+00:00")
        snapshot = self.call("status", summary=True)
        self.assertEqual(snapshot["ready_task_ids"], ["a", "z", "new"])
        self.assertEqual(snapshot["dispatch_policy"], {"revision": 0, "task_ids": [], "fallback": "created_at_task_id"})
        self.assertNotIn("dispatch_policy", json.loads((self.workspace / "coordination/state.json").read_text()))
        self.assertEqual(self.claim()["task"]["id"], "a")

    def test_explicit_order_skips_blocked_running_delivered_waiting_and_failed(self):
        for identifier in ("fallback", "ranked-2", "ranked-1", "running", "delivered", "waiting", "failed"):
            self.task(identifier)
        self.task("blocked", requires=[{"message_id": "not-received"}])
        live_run = self.start("running")
        run = self.start("delivered")
        self.call("task.submit", task_id="delivered", run_id=run, artifacts=[],
                  self_checks=[{"name": "fixture", "result": "pass", "evidence": "fixture"}], unresolved=[])
        self.stop(run, "delivered")
        run = self.start("waiting")
        self.call("task.wait", task_id="waiting", run_id=run, requires=[{"message_id": "reply"}], checkpoint="fixture")
        self.stop(run, "waiting")
        run = self.start("failed")
        self.stop(run, "failed")
        before = self.call("status", summary=True)
        ordered = ["blocked", "running", "delivered", "waiting", "failed", "ranked-2", "ranked-1"]
        self.policy(ordered)
        after = self.call("status", summary=True)
        for key in ("tasks", "runs", "writers", "messages", "models", "capacity"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["ready_task_ids"], ["ranked-2", "ranked-1", "fallback"])
        self.assertEqual(after["runs"][live_run]["state"], "running")
        self.assertEqual(self.claim()["task"]["id"], after["ready_task_ids"][0])
        self.assertEqual(self.claim()["task"]["id"], "ranked-1")
        self.assertEqual(self.claim()["reason"], "capacity")

    def test_explicit_claim_keeps_override_readiness_capacity_and_models(self):
        self.task("ranked")
        self.task("unranked", model="gpt-6.1-sol", effort="high")
        self.task("blocked", requires=[{"message_id": "absent"}])
        self.policy(["blocked", "ranked"])
        self.assertEqual(self.claim(task_id="blocked")["reason"], "no_ready_task")
        self.assertEqual(self.claim(external_active=3)["reason"], "capacity")
        explicit = self.claim(task_id="unranked")
        self.assertEqual(explicit["task"]["id"], "unranked")
        self.assertEqual((explicit["model"], explicit["effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual(self.claim(task_id="unranked")["reason"], "no_ready_task")
        self.assertEqual(self.claim(task_id="ranked", external_active=2)["reason"], "capacity")
        self.assertEqual(self.claim()["task"]["id"], "ranked")
        self.assertEqual(self.claim()["reason"], "no_ready_task")

    def test_policy_validation_is_atomic(self):
        self.task("exists")
        valid = {"task_ids": ["exists"], "expected_revision": 0, "evidence": ["App decision"]}
        for overrides in ({"task_ids": None}, {"task_ids": "exists"}, {"task_ids": ["exists", "exists"]},
                          {"task_ids": ["absent"]}, {"task_ids": ["../escape"]}, {"task_ids": [42]},
                          {"evidence": []}, {"evidence": None}, {"evidence": "source"},
                          {"expected_revision": None}, {"expected_revision": True}, {"expected_revision": -1},
                          {"actor": " "}):
            with self.subTest(overrides=overrides):
                before = (self.workspace / "coordination/state.json").read_bytes()
                with self.assertRaises(ReplicaError):
                    self.call("dispatch.set", **(valid | overrides))
                self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), before)

    def test_policy_revision_replay_conflict_stale_and_clear(self):
        self.task("z")
        self.task("a")
        request = {"op": "dispatch.set", "request_id": "app-order", "task_ids": ["z"],
                   "expected_revision": 0, "actor": "main-for-app", "evidence": ["App decision"]}
        first = execute(self.workspace, request)
        self.assertEqual(first, execute(self.workspace, request))
        self.assertEqual(first["dispatch_policy"]["revision"], 1)
        self.assertEqual(first["dispatch_policy"]["actor"], "main-for-app")
        self.task("unrelated-discovery")
        cleared = self.policy([], expected_revision=1)
        self.assertEqual(cleared["dispatch_policy"]["revision"], 2)
        self.assertEqual(self.call("status", summary=True)["ready_task_ids"], ["a", "unrelated-discovery", "z"])
        self.assertEqual(first, execute(self.workspace, request))
        self.assertEqual(self.call("status", summary=True)["dispatch_policy"], cleared["dispatch_policy"])
        before = (self.workspace / "coordination/state.json").read_bytes()
        with self.assertRaisesRegex(ReplicaError, "different content"):
            execute(self.workspace, request | {"task_ids": ["a"]})
        with self.assertRaisesRegex(ReplicaError, "Stale dispatch policy revision"):
            self.policy(["a"], expected_revision=1)
        self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), before)

    def test_cli_apply_replays_and_summary_exposes_authoritative_policy(self):
        self.task("z")
        self.task("a")
        request = self.root / "request.json"
        request.write_text(json.dumps({"op": "dispatch.set", "task_ids": ["z"], "expected_revision": 0,
                                       "evidence": ["App decision"]}))
        first = self.cli("apply", "--file", str(request))
        self.assertEqual(first, self.cli("apply", "--file", str(request)))
        for args in (("status",), ("status", "--summary"), ("status", "--full")):
            result = self.cli(*args)
            self.assertEqual(result["dispatch_policy"], first["dispatch_policy"])
            self.assertEqual(result["ready_task_ids"], ["z", "a"])

    def test_ordering_uses_no_source_inventory_shards_and_preserves_writer(self):
        self.call("inventory.add", objects=[{"id": "original", "kind": "method", "name": "f", "source": "fixture"}])
        self.call("scope.set", object_id="original", disposition="copy", reason="fixture", evidence=["source"])
        task = self.call("task.add", task={"role": "function", "mode": "code", "object_id": "original",
                                          "description": "fixture", "outputs": ["artifacts/f.py"]})["task_id"]
        self.task("older", "2026-10-14T00:00:00+00:00")
        with patch.object(Inventory, "__iter__", side_effect=AssertionError("full inventory scan")), \
             patch.object(Inventory, "_shard", side_effect=AssertionError("source inventory shard")):
            self.policy([task])
            self.assertEqual(self.call("status", summary=True)["ready_task_ids"], [task, "older"])
            claimed = self.claim()
            self.assertEqual(claimed["task"]["id"], task)
        self.assertEqual((claimed["model"], claimed["effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual(self.call("status")["writers"][str(self.workspace / "artifacts/f.py")], task)

    def test_waiting_root_promotes_transitive_dependency_without_mutating_state(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.task("leaf")
        self.task("middle", requires=["leaf"])
        self.task("root")
        run = self.start("root")
        self.call("task.wait", task_id="root", run_id=run,
                  requires=["middle"], checkpoint="saved progress")
        self.stop(run, "waiting")
        self.policy(["root"])
        authority = self.workspace / "coordination/state.json"
        before = authority.read_bytes()
        self.assertEqual(self.call("status")["ready_task_ids"], ["leaf", "old"])
        self.assertEqual(authority.read_bytes(), before)
        self.assertEqual(self.claim()["task"]["id"], "leaf")
        self.assertEqual(self.claim(external_active=2)["reason"], "capacity")

    def test_shared_dependency_inherits_earliest_rank_even_if_explicitly_listed_later(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.task("shared")
        self.task("first", requires=["shared"])
        self.task("second", requires=["shared"])
        self.task("ranked")
        self.policy(["first", "ranked", "shared", "second"])
        self.assertEqual(self.call("status")["ready_task_ids"], ["shared", "ranked", "old"])
        self.assertEqual(self.claim()["task"]["id"], "shared")
        self.assertEqual(self.claim()["task"]["id"], "ranked")
        state = self.call("status")
        self.assertEqual(len(state["tasks"]["shared"]["runs"]), 1)

    def test_class_revision_inheritance_uses_structural_task_not_logical_owner_id(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.call("inventory.add", objects=[{"id": "class", "kind": "class", "name": "C", "source": "fixture"}])
        structural = self.call("task.add", task={"role": "class", "mode": "structure",
                               "object_id": "class", "class_id": "class", "owner": "logical-owner",
                               "description": "structure"})["task_id"]
        code = self.call("task.add", task={"role": "class", "mode": "code", "object_id": "class",
                         "class_id": "class", "description": "implementation"})["task_id"]
        self.policy([code])
        self.assertEqual(self.call("status")["ready_task_ids"], [structural, "old"])
        self.call("class.publish", class_id="class", owner="logical-owner", expected_revision=0,
                  structure={"name": "C"}, sources=["fixture"])
        self.assertEqual(self.call("status")["ready_task_ids"], [code, "old", structural])
        self.call("task.add", task={"role": "class", "mode": "code", "object_id": "class",
                  "class_id": "class", "description": "implementation", "requires": [{"class_id": "class", "revision": 2}]})
        self.assertEqual(self.call("status")["ready_task_ids"], [structural, "old"])

    def test_terminal_wait_inheritance_respects_planning_wake_exception(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.task("child")
        self.task("ranked")
        self.call("task.add", task={"id": "planner", "role": "app", "mode": "plan",
                  "description": "planner", "requires": [{"task_id": "child", "when": "stopped"}]})
        self.policy(["planner", "ranked"])
        self.assertEqual(self.call("status")["ready_task_ids"], ["child", "ranked", "old"])
        self.call("message.send", type="request", sender="main", recipient="planner", wake=True,
                  payload={"request": "new planning question"})
        self.assertEqual(self.call("status")["ready_task_ids"], ["planner", "ranked", "old", "child"])

    def test_delivered_root_does_not_promote_reopened_dependency_or_run_again(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.task("child")
        child_run = self.start("child")
        self.call("task.submit", task_id="child", run_id=child_run, artifacts=[],
                  self_checks=[{"name": "fixture", "result": "pass", "evidence": "fixture"}], unresolved=[])
        self.stop(child_run, "delivered")
        self.task("root", requires=["child"])
        run = self.start("root")
        child_revision = self.call("status")["tasks"]["child"]["revision"]
        self.call("task.submit", task_id="root", run_id=run, artifacts=[], dependency_revisions={"child": child_revision},
                  self_checks=[{"name": "fixture", "result": "pass", "evidence": "fixture"}], unresolved=[])
        self.stop(run, "delivered")
        self.policy(["root"])
        self.assertEqual(self.call("status")["ready_task_ids"], ["old"])
        self.call("task.resume", task_id="child", reason="new work", evidence=["fixture"])
        self.assertEqual(self.call("status")["ready_task_ids"], ["old", "child"])
        self.assertEqual(len(self.call("status")["tasks"]["root"]["runs"]), 1)

    def test_message_wait_does_not_invent_dependency_on_sender(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.task("sender")
        self.task("root", requires=[{"message_id": "future-reply"}])
        self.policy(["root"])
        self.assertEqual(self.call("status")["ready_task_ids"], ["old", "sender"])
        self.assertIsNone(self.claim(task_id="root")["task"])

    def test_inheritance_bounds_cycles_in_read_only_graph_and_keeps_write_rejection(self):
        self.task("old")
        self.task("a")
        self.task("b", requires=["a"])
        self.policy(["a"])
        # Persisted task-only cycles are rejected; mixed class/task edges can
        # still form cycles. The ranking traversal must never recurse forever.
        with Store(self.workspace).locked() as state:
            state["tasks"]["a"]["requires"] = [{"task_id": "b"}]
            self.assertEqual(_dispatch_ranks(Store(self.workspace), state), {"a": 0, "b": 0})
            self.assertEqual([t["id"] for t in _ready_tasks(Store(self.workspace), state)], ["old"])
        with self.assertRaisesRegex(ReplicaError, "cycle"):
            self.call("task.add", task={"id": "a", "role": "analysis", "mode": "inspect",
                      "description": "fixture", "requires": ["b"]})
        self.assertEqual(self.call("status")["tasks"]["a"]["requires"], [])

    def test_absent_policy_keeps_legacy_fifo_with_dependencies(self):
        self.task("old", "2026-10-14T00:00:00+00:00")
        self.task("leaf")
        self.task("root", requires=["leaf"])
        authority = self.workspace / "coordination/state.json"
        before = authority.read_bytes()
        self.assertEqual(self.call("status")["ready_task_ids"], ["old", "leaf"])
        self.assertEqual(authority.read_bytes(), before)
        self.assertEqual(json.loads(before)["schema"], 2)
        self.assertNotIn("dispatch_policy", json.loads(before))

    def test_mixed_class_task_cycle_stays_blocked_without_recursion(self):
        self.task("fallback")
        self.call("inventory.add", objects=[{"id": "class", "kind": "class", "name": "C", "source": "fixture"}])
        code = self.call("task.add", task={"role": "class", "mode": "code", "object_id": "class",
                         "class_id": "class", "description": "implementation"})["task_id"]
        structure = self.call("task.add", task={"role": "class", "mode": "structure", "object_id": "class",
                              "class_id": "class", "description": "structure", "requires": [code]})["task_id"]
        self.policy([code])
        self.assertEqual(self.call("status")["ready_task_ids"], ["fallback"])
        with Store(self.workspace).locked() as state:
            self.assertEqual(_dispatch_ranks(Store(self.workspace), state), {code: 0, structure: 0})

    def test_inherited_rank_does_not_bypass_scope_or_auto_resume_failure(self):
        self.task("fallback")
        self.call("inventory.add", objects=[{"id": "original", "kind": "method", "name": "f", "source": "fixture"}])
        dependency = self.call("task.add", task={"role": "function", "mode": "code", "object_id": "original",
                               "description": "implementation"})["task_id"]
        self.task("root", requires=[dependency])
        self.policy(["root"])
        self.call("scope.set", object_id="original", disposition="reuse", reason="fixture", evidence=["fixture"])
        self.assertEqual(self.call("status")["ready_task_ids"], ["fallback"])
        self.call("scope.set", object_id="original", disposition="copy", reason="fixture", evidence=["fixture"])
        run = self.start(dependency)
        self.stop(run, "failed")
        state = self.call("status")
        self.assertEqual(state["ready_task_ids"], ["fallback"])
        self.assertEqual(state["tasks"][dependency]["state"], "failed")
        self.assertEqual(len(state["tasks"][dependency]["runs"]), 1)

    def test_runner_and_native_selection_follow_policy_without_new_workers(self):
        self.task("a-old")
        self.task("z-ranked", "2026-10-15T00:00:01+00:00")
        self.policy(["z-ranked"])
        # Native descriptors reserve metadata only; no host spawn tool is called.
        result = native_next(self.workspace, external_active=0)
        self.assertEqual(result["task"]["id"] if "task" in result else
                         self.call("status")["runs"][result["run_id"]]["task_id"], "z-ranked")
        self.stop(result["run_id"], "launch_failed")
        self.task("z-next", "2026-10-15T00:00:02+00:00")
        self.policy(["z-next"], expected_revision=1)
        # Intercept launch so this fixture cannot start a model/worker process.
        backend = unittest.mock.Mock()
        runner = Runner(self.workspace, host_capacity=3, backend=backend)
        launches = []
        def no_launch(task, run, project):
            launches.append(task["id"])
            raise RuntimeError("fixture intercepted launch")
        with patch.object(runner, "_launch", side_effect=no_launch), \
             self.assertRaisesRegex(RuntimeError, "fixture intercepted launch"):
            runner.step()
        self.assertEqual(launches, ["z-next"])


if __name__ == "__main__":
    unittest.main()
