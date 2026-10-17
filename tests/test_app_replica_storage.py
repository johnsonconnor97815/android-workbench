"""Isolated storage tests: host fixtures, no model/device/socket operations."""
from copy import deepcopy
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
from workbench.app_replica import execute, ReplicaError
from workbench.app_replica.snapshot import Inventory, Scopes, bucket
from workbench.app_replica.store import Store


def concurrent_scope(workspace, number):
    execute(workspace, {"op": "scope.set", "request_id": f"scope-{number}", "object_id": f"obj-{number}",
        "disposition": "copy", "reason": "fixture", "evidence": [number]})


def concurrent_class(workspace, number):
    try:
        execute(workspace, {"op": "class.publish", "request_id": f"class-{number}", "class_id": "obj-0",
            "owner": "one-owner", "expected_revision": 1, "structure": {"winner": number}, "sources": [number]})
        result = "committed"
    except ReplicaError as exc:
        result = str(exc)
    (Path(workspace) / f"result-{number}.txt").write_text(result)


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-storage-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.n = 0
        self.call("init", project={"id": "storage"}, capacity=3)
        self.objects = [{"id": f"obj-{i}", "kind": "class" if i == 0 else "method", "name": f"original-{i}",
            "source": {"path": "original", "line": i}, "class_id": "obj-0", "signature": f"signature-{i}",
            "evidence": [i], "metadata": {"defined": True, "inherited": False}} for i in range(20)]

    def call(self, op, **fields):
        self.n += 1
        if op not in {"status", "coverage", "recover", "inventory.get", "inventory.export"}:
            fields.setdefault("request_id", f"fixture-{self.n}")
        return execute(self.workspace, {"op": op, **fields})

    def disk(self):
        return json.loads((self.workspace / "coordination/state.json").read_text())

    def entry(self, number):
        return {"id": f"entry-{number}", "trigger": "fixture", "evidence": ["manifest"], "root_object_ids": [f"obj-{number}"]}

    def legacy(self):
        self.call("inventory.add", objects=self.objects)
        self.call("scope.set", object_id="obj-1", disposition="copy", reason="fixture", evidence=["source"])
        self.call("entry.add", entry=self.entry(0))
        self.call("task.add", task={"id": "inspect", "role": "analysis", "mode": "inspect", "description": "Keep owner"})
        self.call("class.publish", class_id="obj-0", owner="structure-owner", expected_revision=0,
                  structure={"fields": ["definedField"], "super": "Lparent;"}, sources=["source:1"])
        # Preserve a nonempty lifecycle, dependency, delivery and paired host
        # fixture record across migration, in addition to source inventory.
        self.call("task.discover", task_id="inspect", target="obj-1", source="fixture:call")
        self.call("task.add", task={"id": "inspect", "role": "analysis", "mode": "inspect",
                                   "outputs": ["artifacts/fixture.txt"]})
        artifact = self.workspace / "artifacts/fixture.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("host fixture implementation evidence")
        run = self.call("task.claim", task_id="inspect", backend="host_fixture")["run"]["id"]
        self.call("run.started", run_id=run, backend_handle={"thread": "fixture-thread"})
        self.call("task.submit", task_id="inspect", run_id=run,
                  artifacts=[{"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
                  self_checks=[{"name": "fixture", "result": "pass", "evidence": ["host assertion"]}], unresolved=[])
        self.call("run.stopped", run_id=run, outcome="delivered",
                  observation={"confirmed": True, "writers_stopped": True, "evidence": ["host fixture only"]})
        self.call("message.send", type="note", sender="app", recipient="inspect", task_id="inspect", payload="keep message")
        self.call("mvp.define", mvp={"id": "fixture-mvp", "entry_ids": ["entry-0"], "required_task_ids": ["inspect"],
                  "target_versions": {"original": "fixture-original", "candidate": "fixture-candidate"},
                  "conditions": {"fixture": True}, "goals": ["same"]})
        observations = {}
        for side in ("original", "candidate"):
            path = self.workspace / f"artifacts/{side}.txt"
            path.write_text(f"{side} host fixture observation")
            observations[side] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        self.call("mvp.record", mvp_id="fixture-mvp", original_version="fixture-original", candidate_version="fixture-candidate",
                  runtime_scope={"kind": "host_fixture", "conditions": {"fixture": True}}, observations=observations,
                  results={"same": {"result": "pass", **{side: observations[side]["path"] for side in observations}}})
        store = Store(self.workspace)
        with store.locked() as state:
            state["inventory"] = dict(state["inventory"])
            state["receipts"] = {key: store.receipt(value) for key, value in state["receipts"].items()}
            state.pop("inventory_ref")
            state.pop("scope_ref")
            state["schema"] = 1
            store.write_json("coordination/state.json", state)
        original = (self.workspace / "coordination/state.json").read_bytes()
        return state, original, hashlib.sha256(original).hexdigest()

    def test_migration_exact_objects_metadata_receipts_backup_and_replay(self):
        old, original, digest = self.legacy()
        store = Store(self.workspace)
        result = store.migrate(expected_revision=old["revision"], expected_sha256=digest)
        self.assertTrue(result["ok"])
        self.assertEqual(store.metadata_path(result["migration"]["backup"]).read_bytes(), original)
        with store.locked() as new:
            self.assertEqual(dict(new["inventory"]), old["inventory"])
            self.assertEqual({k: store.receipt(v) for k, v in new["receipts"].items()}, old["receipts"])
            for key in set(old) - {"schema", "revision", "inventory", "receipts"}:
                self.assertEqual(new[key], old[key], key)
        self.assertTrue(store.migrate(expected_revision=old["revision"], expected_sha256=digest)["replayed"])
        self.assertNotIn("inventory", self.disk())
        self.assertLess((self.workspace / "coordination/state.json").stat().st_size, len(original))

    def test_legacy_writes_and_wrong_revision_hash_or_schema_rejected(self):
        old, original, digest = self.legacy()
        with self.assertRaisesRegex(ReplicaError, "explicit migrate"):
            self.call("message.send", type="note", sender="x", recipient="x", payload="x")
        for revision, hash_value in ((old["revision"] + 1, digest), (old["revision"], "0" * 64)):
            with self.assertRaises(ReplicaError):
                Store(self.workspace).migrate(expected_revision=revision, expected_sha256=hash_value)
            self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), original)
        Store(self.workspace).write_json("coordination/state.json", dict(old, schema=999))
        with self.assertRaisesRegex(ReplicaError, "Unsupported"):
            self.call("status", summary=True)

    def test_migration_hard_crash_before_and_after_authority_switch(self):
        old, original, digest = self.legacy()
        for phase in ("staged-rename", "before", "after"):
            with self.subTest(phase=phase):
                Store(self.workspace).write_json("coordination/state.json", old)
                if phase == "staged-rename":
                    import shutil
                    # Only this isolated schema-1 fixture is authoritative now.
                    shutil.rmtree(self.workspace / "coordination/inventory")
                code = '''from workbench.app_replica.store import Store
import os,sys
original=Store.write_bytes
def injected(self,path,content):
    if path == "coordination/state.json":
        if sys.argv[4] == "after": original(self,path,content)
        os._exit(41)
    return original(self,path,content)
Store.write_bytes=injected
original_sync=Store._fsync_dir
def sync(path):
    if sys.argv[4] == "staged-rename" and path.name == "shards": os._exit(41)
    return original_sync(path)
Store._fsync_dir=staticmethod(sync)
Store(sys.argv[1]).migrate(expected_revision=int(sys.argv[2]),expected_sha256=sys.argv[3])
'''
                process = subprocess.run([sys.executable, "-c", code, str(self.workspace), str(old["revision"]), digest, phase],
                                         cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(process.returncode, 41, process.stderr)
                self.assertEqual(self.disk()["schema"], 2 if phase == "after" else 1)
                if phase != "after":
                    self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), original)
                result = Store(self.workspace).migrate(expected_revision=old["revision"], expected_sha256=digest)
                self.assertTrue(result["ok"])
                self.assertEqual(self.call("inventory.export")["objects"], old["inventory"])

    def test_projection_failure_after_migration_is_committed_and_recovers(self):
        old, original, digest = self.legacy()
        with patch.object(Store, "publish", side_effect=OSError("injected projection error")):
            result = Store(self.workspace).migrate(expected_revision=old["revision"], expected_sha256=digest)
        self.assertTrue(result["committed"])
        self.assertFalse(result["ok"])
        self.assertEqual(self.disk()["schema"], 2)
        self.assertTrue(Store(self.workspace).migrate(expected_revision=old["revision"], expected_sha256=digest)["ok"])

    def test_summary_task_update_and_coverage_do_not_scan_inventory(self):
        self.call("inventory.add", objects=self.objects)
        with patch.object(Inventory, "__iter__", side_effect=AssertionError("full scan")), \
             patch.object(Inventory, "_shard", side_effect=AssertionError("inventory read")), \
             patch.object(Scopes, "_shard", side_effect=AssertionError("scope read")):
            self.assertNotIn("inventory", self.call("status"))
            self.assertEqual(self.call("status", summary=True)["counts"]["inventory"], 20)
            self.assertEqual(self.call("coverage", summary=True)["coverage"]["counts"]["unknown"], 20)
            self.call("task.add", task={"id": "small", "role": "analysis", "mode": "inspect", "description": "metadata"})

    def test_scope_only_changes_scope_shards_and_changed_projections(self):
        self.call("inventory.add", objects=self.objects)
        before = self.disk()["inventory_ref"]
        writes = []
        original = Store.write_bytes
        def record(store, path, content):
            writes.append(path)
            return original(store, path, content)
        with patch.object(Store, "write_bytes", record):
            self.call("scope.set", object_id="obj-3", disposition="exclude", reason="fixture", evidence=[1])
        self.assertEqual(before, self.disk()["inventory_ref"])
        self.assertFalse(any("inventory/shards/" in p or "inventory/manifests/" in p for p in writes))
        self.assertTrue(any("scopes/shards/" in p for p in writes))
        self.assertNotIn("project.json", writes)
        self.assertNotIn("inventory_scopes", self.disk())
        self.assertEqual(self.call("coverage")["coverage"]["counts"], self.call("coverage", summary=True)["coverage"]["counts"])

    def test_lookup_stream_export_exact_and_corruption_symlink_rejected(self):
        self.call("inventory.add", objects=self.objects)
        full = self.call("inventory.export")["objects"]
        for identifier, obj in full.items():
            self.assertEqual(self.call("inventory.get", object_id=identifier)["object"], obj)
        result = Store(self.workspace).export_inventory("inventory/objects.json")
        data = Path(result["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), result["sha256"])
        self.assertEqual(json.loads(data)["objects"], full)
        with Store(self.workspace).locked() as state:
            reference = state["inventory"].manifest["shards"][bucket("obj-0")]
        path = self.workspace / reference["path"]
        data = path.read_bytes()
        path.write_bytes(data + b" ")
        with self.assertRaisesRegex(ReplicaError, "hash mismatch"):
            self.call("inventory.get", object_id="obj-0")
        path.unlink()
        outside = self.workspace / "outside.json"
        outside.write_bytes(data)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ReplicaError, "escapes workspace"):
            self.call("inventory.get", object_id="obj-0")

    def test_batch_import_atomic_idempotent_and_resumes_partial_entries(self):
        self.call("inventory.add", objects=self.objects)
        self.call("entry.add", entry=self.entry(0))
        fields = {"objects": self.objects, "entries": [self.entry(i) for i in range(20)], "complete": False,
                  "limitations": ["fixture remains static"]}
        request = {"op": "inventory.import", "request_id": "import", **fields}
        first = execute(self.workspace, request)
        reference = self.disk()["inventory_ref"]
        self.assertEqual(first, execute(self.workspace, request))
        self.assertEqual(reference, self.disk()["inventory_ref"])
        self.assertEqual(len(self.call("status", summary=True)["entries"]), 20)
        before = (self.workspace / "coordination/state.json").read_bytes()
        bad = deepcopy(fields)
        bad["objects"].append({"id": "new", "kind": "class", "name": "new", "source": "x"})
        bad["entries"].append({"id": "bad", "trigger": "x", "root_object_ids": ["absent"]})
        with self.assertRaises(ReplicaError):
            self.call("inventory.import", **bad)
        self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), before)
        self.assertNotIn("new", self.call("inventory.export")["objects"])

    def test_concurrent_scope_mutations_no_lost_updates(self):
        self.call("inventory.add", objects=self.objects)
        revision = self.call("status", summary=True)["revision"]
        ctx = multiprocessing.get_context("fork")
        children = [ctx.Process(target=concurrent_scope, args=(str(self.workspace), i)) for i in range(20)]
        for child in children:
            child.start()
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.call("status", summary=True)["revision"], revision + 20)
        self.assertEqual(self.call("coverage", summary=True)["coverage"]["counts"]["copy_pending"], 20)
        for i in range(20):
            self.assertEqual(self.call("inventory.get", object_id=f"obj-{i}")["object"]["scope"]["evidence"], [i])

    def test_cli_batch_import_one_commit_default_status_and_explicit_export(self):
        import contextlib
        import io
        from workbench.app_replica import cli
        supplied = {"objects": self.objects, "entries": [self.entry(i) for i in range(20)],
                    "complete": False, "limitations": ["static fixture"], "completeness": {}, "sources": []}
        revision = self.disk()["revision"]
        output = io.StringIO()
        with patch.object(cli, "load_inventory", return_value=supplied), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "import"]), 0)
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "import"]), 0)
        self.assertEqual(self.disk()["revision"], revision + 1)
        with patch.object(Inventory, "__iter__", side_effect=AssertionError("full scan")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "status"]), 0)
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "status", "--summary"]), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "object", "--id", "obj-3"]), 0)
            self.assertEqual(cli.main(["--workspace", str(self.workspace), "export", "--output", "inventory/objects.json"]), 0)
        self.assertEqual(len(json.loads((self.workspace / "inventory/objects.json").read_text())["objects"]), 20)

    def test_concurrent_class_revision_check_preserves_single_owner(self):
        self.call("inventory.add", objects=self.objects)
        self.call("class.publish", class_id="obj-0", owner="one-owner", expected_revision=0, structure={"original": True})
        context = multiprocessing.get_context("fork")
        children = [context.Process(target=concurrent_class, args=(str(self.workspace), number)) for number in range(2)]
        for child in children:
            child.start()
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        results = [(self.workspace / f"result-{i}.txt").read_text() for i in range(2)]
        self.assertEqual(results.count("committed"), 1)
        self.assertEqual(sum("Stale class revision" in value for value in results), 1)
        record = self.call("status")["classes"]["obj-0"]
        self.assertEqual(record["revision"], 2)
        self.assertEqual(record["owner"], "one-owner")
        self.assertEqual(record["structure"], {"winner": results.index("committed")})

    def test_immutable_refinement_only_replaces_affected_bucket(self):
        self.call("inventory.add", objects=self.objects)
        with Store(self.workspace).locked() as state:
            previous = deepcopy(state["inventory"].manifest)
        changed = deepcopy(self.objects[3])
        changed["source"] = {"path": "second-index", "line": 7}
        changed["evidence"].append("new evidence")
        self.call("inventory.add", objects=[changed])
        with Store(self.workspace).locked() as state:
            current = state["inventory"].manifest
        modified = {key for key in previous["shards"] if previous["shards"][key] != current["shards"][key]}
        self.assertEqual(modified, {bucket("obj-3")})
        self.assertEqual(current["count"], 20)
        obj = self.call("inventory.get", object_id="obj-3")["object"]
        self.assertIn(changed["source"], obj["sources"])
        self.assertIn("new evidence", obj["evidence"])

    def test_migration_backup_corruption_rejects_replay_without_new_commit(self):
        old, original, digest = self.legacy()
        store = Store(self.workspace)
        result = store.migrate(expected_revision=old["revision"], expected_sha256=digest)
        before = (self.workspace / "coordination/state.json").read_bytes()
        store.metadata_path(result["migration"]["backup"]).write_bytes(original + b" ")
        with self.assertRaisesRegex(ReplicaError, "backup SHA-256 mismatch"):
            store.migrate(expected_revision=old["revision"], expected_sha256=digest)
        self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), before)

    def test_recover_repairs_nonobject_and_forged_projection_markers(self):
        target = self.workspace / "project.json"
        for raw in ([], 42, None, {"id": "storage", "_projection": {"editable": True}}):
            with self.subTest(raw=raw):
                target.write_text(json.dumps(raw))
                self.assertTrue(self.call("recover")["ok"])
                repaired = json.loads(target.read_text())
                self.assertEqual(repaired["id"], "storage")
                self.assertFalse(repaired["_projection"]["editable"])
        import os
        target.unlink()
        os.mkfifo(target)
        response = self.call("recover")
        self.assertFalse(response["ok"])
        self.assertIn("regular file", response["projection_error"])

    def test_reused_blob_and_parent_names_are_fsynced_before_referencing(self):
        store = Store(self.workspace)
        reference = store.write_blob("inventory/shards", {"durable": True})
        with patch.object(store, "_sync_parents", wraps=store._sync_parents) as sync, \
             patch("workbench.app_replica.store.os.fsync", wraps=__import__("os").fsync) as fsync:
            self.assertEqual(store.write_blob("inventory/shards", {"durable": True}), reference)
            sync.assert_called_once_with((self.workspace / reference["path"]).parent)
            self.assertGreater(fsync.call_count, 1)

    def test_ready_implementation_tasks_never_read_source_inventory(self):
        self.call("inventory.add", objects=self.objects)
        self.call("class.publish", class_id="obj-0", owner="owner", expected_revision=0, structure={"fields": []})
        tasks = [self.call("task.add", task={"object_id": f"obj-{i}", "class_id": "obj-0",
                    "role": "function", "mode": "code", "description": "scoped fixture method"})["task_id"]
                 for i in range(1, 20)]
        self.call("scope.set", object_id="obj-1", disposition="exclude", reason="fixture", evidence=[1])
        with patch.object(Inventory, "_shard", side_effect=AssertionError("source inventory materialized")):
            snapshot = self.call("status")
            self.assertEqual(set(snapshot["ready_task_ids"]), set(tasks[1:]))
            self.assertEqual(self.call("coverage", summary=True)["coverage"]["counts"]["exclude"], 1)
            self.assertIsNotNone(self.call("task.claim", backend="host_fixture")["run"])

    def test_blob_failure_precommit_preserves_authority_then_exact_retry(self):
        before = (self.workspace / "coordination/state.json").read_bytes()
        original = Store.write_blob
        def fail(store, namespace, value):
            if namespace == "receipts":
                raise OSError("injected receipt publication failure")
            return original(store, namespace, value)
        request = {"op": "inventory.import", "request_id": "retry-import", "objects": self.objects, "entries": [self.entry(0)]}
        with patch.object(Store, "write_blob", fail), self.assertRaises(OSError):
            execute(self.workspace, request)
        self.assertEqual((self.workspace / "coordination/state.json").read_bytes(), before)
        result = execute(self.workspace, request)
        self.assertEqual(result, execute(self.workspace, request))
        self.assertEqual(self.call("status", summary=True)["counts"]["inventory"], 20)


if __name__ == "__main__":
    unittest.main()
