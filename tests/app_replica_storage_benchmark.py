"""Reproducible generated inventory benchmark, never operates on an App workspace.

Run each phase in a fresh process. --source selects either the preserved base
copy or the patched source copy. Counts reproduce the Avola declaration scale;
content is synthetic, and cannot establish real-snapshot or Android equivalence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import sys
import time


def io_counters():
    return {key: int(value) for key, value in (line.split(":") for line in Path("/proc/self/io").read_text().splitlines())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["generate", "baseline", "migrate", "summary", "update", "scope", "lookup", "export", "compare"])
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--classes", type=int, default=81593)
    parser.add_argument("--methods", type=int, default=473696)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source))
    from workbench.app_replica.engine import execute, _new_state
    from workbench.app_replica.store import Store
    from workbench.app_replica.ids import object_id, canonical_json
    reads, writes = [], []
    if hasattr(Store, "read_blob"):
        original = Store.read_blob
        def read(store, reference, namespace):
            reads.append({"namespace": namespace, "bytes": reference["bytes"]})
            return original(store, reference, namespace)
        Store.read_blob = read
        original_write = Store.write_bytes
        def write(store, path, content):
            writes.append({"path": path, "bytes": len(content)})
            return original_write(store, path, content)
        Store.write_bytes = write
    before = io_counters()
    started = time.monotonic()
    store = Store(args.workspace)
    state_path = args.workspace / "coordination/state.json"
    result = {}
    if args.phase == "generate":
        state = _new_state({"project": {"id": "generated-scale-fixture"}, "capacity": 3})
        state["schema"] = 1
        state["revision"] = 30
        carriers = [f"dex:fixture-{number}" for number in range(4)]
        class_ids = [object_id(carriers[i % 4], "class", f"Lfixture/C{i};") for i in range(args.classes)]
        for kind, count in (("class", args.classes), ("method", args.methods)):
            for i in range(count):
                signature = f"Lfixture/C{i};" if kind == "class" else f"Lfixture/C{i % args.classes};->m{i}(I)Ljava/lang/String;"
                identifier = class_ids[i] if kind == "class" else object_id(carriers[i % 4], kind, signature)
                item = {"id": identifier, "kind": kind, "name": signature, "signature": signature,
                    "carrier": carriers[i % 4], "source": {"path": "synthetic/index.sqlite3", "row": i},
                    "evidence": ["Synthetic original declaration", {"path": "synthetic/base.apk", "sha256": "a" * 64}],
                    "limitations": ["Generated fixture; runtime loading remains unknown"],
                    "scope": {"disposition": "unknown", "reason": "Not yet decided", "evidence": []},
                    "attributes": {"declared": True, "body_available": kind == "method", "access": ["public"],
                                   "source_detail": "evidence-" * 90}}
                if kind == "method":
                    item["class_id"] = class_ids[i % args.classes]
                    item["is_native"] = i % 50 == 0
                    item["is_abstract"] = i % 51 == 0
                else:
                    item.update(super="Ljava/lang/Object;", interfaces=["Ljava/io/Serializable;"])
                state["inventory"][identifier] = item
        for i in range(300):
            entry_id = f"entry-{i}"
            state["entries"][entry_id] = {"id": entry_id, "trigger": "synthetic", "evidence": ["manifest"],
                                             "root_object_ids": [class_ids[i]]}
        state["inventory_status"] = {"complete": False, "limitations": ["Synthetic declarations only"]}
        store.write_json("coordination/state.json", state)
        result = {"classes": args.classes, "declared_methods": args.methods, "objects": len(state["inventory"])}
        (args.workspace / "fixture.json").write_text(json.dumps({"revision": state["revision"],
            "sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(), "lookup_id": class_ids[0],
            "legacy_bytes": state_path.stat().st_size}))
    elif args.phase == "baseline":
        snapshot = execute(args.workspace, {"op": "status"})
        result = {"objects": len(snapshot["inventory"]), "revision": snapshot["revision"]}
    elif args.phase == "migrate":
        fixture = json.loads((args.workspace / "fixture.json").read_text())
        result = store.migrate(expected_revision=fixture["revision"], expected_sha256=fixture["sha256"])
    elif args.phase == "summary":
        snapshot = execute(args.workspace, {"op": "status", "summary": True})
        result = {"counts": snapshot["counts"], "coverage": execute(args.workspace, {"op": "coverage", "summary": True})["coverage"]}
    elif args.phase == "update":
        result = execute(args.workspace, {"op": "task.add", "request_id": "benchmark-task-final", "task": {
            "id": "benchmark-final", "role": "analysis", "mode": "inspect", "description": "Metadata-only benchmark"}})
    elif args.phase in {"lookup", "scope"}:
        identifier = json.loads((args.workspace / "fixture.json").read_text())["lookup_id"]
        request = {"op": "inventory.get", "object_id": identifier} if args.phase == "lookup" else {
            "op": "scope.set", "request_id": "benchmark-scope-final", "object_id": identifier,
            "disposition": "copy", "reason": "Fixture scope after durability check", "evidence": ["generated declaration"]}
        response = execute(args.workspace, request)
        result = {"ok": response["ok"], "object_id": identifier}
    elif args.phase == "export":
        result = store.export_inventory("inventory/objects.json")
    else:
        fixture = json.loads((args.workspace / "fixture.json").read_text())
        old = json.loads((args.workspace / f"coordination/legacy/{fixture['sha256']}.json").read_text())
        with store.locked() as state:
            count = 0
            for key in state["inventory"].manifest["shards"]:
                for identifier in state["inventory"]._shard(key):
                    assert state["inventory"][identifier] == old["inventory"][identifier], identifier
                    count += 1
                state["inventory"].cache.clear()
            assert count == len(old["inventory"])
            for key in set(old) - {"schema", "revision", "inventory", "receipts"}:
                assert state[key] == old[key], key
        exported = json.loads((args.workspace / "inventory/objects.json").read_text())
        assert exported["objects"] == old["inventory"]
        result = {"objects_compared": count, "all_object_fields_and_metadata_equal": True,
                  "export_equal": True, "original_legacy_sha256": fixture["sha256"]}
    elapsed = time.monotonic() - started
    after = io_counters()
    print(json.dumps({"phase": args.phase, "source": str(args.source), "workspace": str(args.workspace),
        "wall_seconds": elapsed, "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "io_delta": {key: after[key] - before[key] for key in before}, "blob_reads": reads, "file_writes": writes,
        "authority_bytes": state_path.stat().st_size, "result": result}, sort_keys=True))


if __name__ == "__main__":
    main()
