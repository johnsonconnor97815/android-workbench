"""Local coordination CLI. Device work remains in the managed Workbench queue."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .backend import BackendError
from .engine import execute, READS
from .ids import canonical_json, stable_id
from .inventory import load_inventory
from .roles import source_root
from .runner import Runner, mutation, native_next, seed_app
from .store import ReplicaError, Store


def _json(path):
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())


def _apply(workspace, request):
    request = dict(request)
    if request.get("op") not in READS:
        request.setdefault("request_id", stable_id("request", request))
    return execute(workspace, request)


def _task(workspace, identifier):
    snapshot = execute(workspace, {"op": "status", "summary": True})
    if identifier not in snapshot["tasks"]:
        raise ReplicaError(f"Unknown task: {identifier}")
    return snapshot, snapshot["tasks"][identifier]


def _reserve_outputs(workspace, task, paths):
    return _apply(workspace, {"op": "task.add", "task": {
        key: task[key] for key in ("id", "role", "mode", "object_id", "class_id", "owner", "description")
    } | {"outputs": paths}})


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace", required=True, type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize files and seed the Astra App task")
    init.add_argument("--project", required=True, help="JSON project identity, sources, output paths, requirements")
    init.add_argument("--capacity", type=int, required=True, help="Actual allowed workflow execution slots")
    init.add_argument("--workbench-source", type=Path, default=Path(__file__).resolve().parents[2])
    init.add_argument("--models", help="Explicit user-approved role overrides JSON")
    apply = commands.add_parser("apply", help="Apply one JSON request or an ordered array (not an atomic batch)")
    apply.add_argument("--file", required=True, help="JSON path or - for stdin; stable content gets stable receipt ID")
    for operation in ("status", "coverage", "recover"):
        command = commands.add_parser(operation)
        if operation == "status":
            command.add_argument("--task")
            command.add_argument("--summary", action="store_true")
            command.add_argument("--full", action="store_true", help="Compatibility status with the complete inventory")
        elif operation == "coverage":
            command.add_argument("--summary", action="store_true")
    migrate = commands.add_parser("migrate", help="Explicit locked schema-1 backup, verification and schema-2 switch")
    migrate.add_argument("--expected-revision", type=int, required=True)
    migrate.add_argument("--expected-sha256", required=True)
    lookup = commands.add_parser("object", help="Read one complete inventory object by original ID")
    lookup.add_argument("--id", required=True)
    export = commands.add_parser("export", help="Explicit complete inventory projection, streamed to a file")
    export.add_argument("--output", required=True, type=Path)
    ingest = commands.add_parser("import", help="Ingest existing explicit local analysis outputs, without dispatching methods")
    ingest.add_argument("--index-db", type=Path)
    ingest.add_argument("--manifest", type=Path, action="append")
    ingest.add_argument("--recon", type=Path, action="append")
    ingest.add_argument("--dex-inventory", type=Path)
    ingest.add_argument("--native-inventory", type=Path)
    ingest.add_argument("--root", type=Path)
    discover = commands.add_parser("discover")
    discover.add_argument("--task", required=True)
    discover.add_argument("--target", required=True, help="Existing original object ID")
    discover.add_argument("--source", required=True, help="Source path/location identifying this dependency")
    discover.add_argument("--relation", default="call")
    output = commands.add_parser("claim-output")
    output.add_argument("--task", required=True)
    output.add_argument("--path", action="append", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--task", required=True)
    submit.add_argument("--run", required=True)
    submit.add_argument("--artifact", action="append", default=[])
    submit.add_argument("--checks", required=True, help="JSON self-check array with real evidence")
    submit.add_argument("--unresolved", help="JSON unresolved array, defaults []")
    submit.add_argument("--class-revisions", help="JSON class-ID:revision map actually used")
    submit.add_argument("--dependency-revisions", help="JSON dependency-task-ID:revision map actually used")
    wait = commands.add_parser("wait")
    wait.add_argument("--task", required=True)
    wait.add_argument("--run", required=True)
    wait.add_argument("--requires", required=True, help="JSON wait array; App can use {task_id,when:stopped}")
    wait.add_argument("--checkpoint", required=True, help="Saved checkpoint file")
    for name in ("step", "run"):
        runner = commands.add_parser(name, help="Dispatch actual tool-enabled Codex workers")
        runner.add_argument("--host-capacity", required=True, type=int)
        runner.add_argument("--slots-file", type=Path)
        runner.add_argument("--codex", default="codex")
        if name == "run":
            runner.add_argument("--interval", type=float, default=1.0)
    native = commands.add_parser("native-next", help="Return one reserved host tool directive, without executing it")
    native.add_argument("--external-active", required=True, type=int,
                        help="Observed host agents outside this workflow, including Main if it uses a slot")
    native.add_argument("--task")
    return result


def dispatch(args):
    workspace = args.workspace.resolve()
    if args.command == "init":
        project = _json(args.project)
        project.setdefault("workbench_source", str(args.workbench_source.resolve()))
        source_root(project)
        request = {"op": "init", "project": project, "capacity": args.capacity}
        if args.models:
            request["models"] = _json(args.models)
        result = _apply(workspace, request)
        if result.get("ok"):
            result["app"] = seed_app(workspace)
        return result
    if args.command == "apply":
        requests = _json(args.file)
        if isinstance(requests, list):
            receipts = []
            for request in requests:
                receipt = _apply(workspace, request)
                receipts.append(receipt)
                if not receipt.get("ok"):
                    break
            return {"ok": all(item.get("ok") for item in receipts), "receipts": receipts}
        return _apply(workspace, requests)
    if args.command == "migrate":
        return Store(workspace).migrate(expected_revision=args.expected_revision, expected_sha256=args.expected_sha256)
    if args.command == "object":
        return execute(workspace, {"op": "inventory.get", "object_id": args.id})
    if args.command == "export":
        return Store(workspace).export_inventory(args.output)
    if args.command in {"status", "coverage", "recover"}:
        summary = (not args.full if args.command == "status" else bool(getattr(args, "summary", False)))
        summary = bool(summary or getattr(args, "task", None))
        result = execute(workspace, {"op": args.command, "summary": summary})
        if args.command == "status" and args.task:
            if args.task not in result["tasks"]:
                raise ReplicaError(f"Unknown task: {args.task}")
            return {"ok": result["ok"], "revision": result["revision"], "task": result["tasks"][args.task]}
        if args.command == "status" and args.summary:
            return {"ok": result["ok"], "revision": result["revision"], "project": result["project"],
                    "capacity": result["capacity"], "dispatch_policy": result["dispatch_policy"],
                    "inventory_status": result["inventory_status"],
                    "counts": result["counts"], "inventory_counts_by_kind": result["inventory_counts_by_kind"],
                    "tasks": {key: {field: task[field] for field in ("role", "mode", "state", "run_id")}
                              for key, task in result["tasks"].items()},
                    "ready_task_ids": result["ready_task_ids"], "resumable_task_ids": result["resumable_task_ids"],
                    "mvp_results": {key: value["current_result"] for key, value in result["mvps"].items()}}
        return result
    if args.command == "import":
        inventory = load_inventory(args.index_db, args.manifest, args.recon,
                                   dex_inventory=args.dex_inventory, native_inventory=args.native_inventory,
                                   root=args.root)
        # Objects and all entries commit together. Replaying the same import
        # also completes a previously interrupted per-entry legacy import.
        receipts = [_apply(workspace, {"op": "inventory.import", "objects": inventory["objects"],
                                     "entries": inventory["entries"],
                                     "complete": inventory["complete"], "limitations": inventory["limitations"]})]
        return {"ok": all(item.get("ok") for item in receipts),
                "objects": len(inventory["objects"]), "entries": len(inventory["entries"]),
                "complete": inventory["complete"], "completeness": inventory["completeness"],
                "limitations": inventory["limitations"], "sources": inventory["sources"]}
    if args.command == "discover":
        return _apply(workspace, {"op": "task.discover", "task_id": args.task, "target": args.target,
                                 "source": args.source, "relation": args.relation})
    if args.command == "claim-output":
        _, task = _task(workspace, args.task)
        return _reserve_outputs(workspace, task, args.path)
    if args.command == "submit":
        _, task = _task(workspace, args.task)
        # A declared checks file is already supplied by --checks. Requiring it
        # a second time as --artifact turns a mechanical omission into a retry.
        check_path = Path(args.checks).resolve()
        values = list(args.artifact)
        if str(check_path) in task["outputs"]:
            values.append(str(check_path))
        artifacts = []
        seen = set()
        for value in values:
            path = Path(value)
            path = path if path.is_absolute() else workspace / path
            path = path.resolve()
            if path not in seen:
                artifacts.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                seen.add(path)
        return _apply(workspace, {"op": "task.submit", "task_id": args.task, "run_id": args.run,
                                 "artifacts": artifacts, "self_checks": _json(args.checks),
                                 "unresolved": _json(args.unresolved) if args.unresolved else [],
                                 "class_revisions": _json(args.class_revisions) if args.class_revisions else {},
                                 "dependency_revisions": _json(args.dependency_revisions) if args.dependency_revisions else {}})
    if args.command == "wait":
        checkpoint = Path(args.checkpoint).resolve()
        if not checkpoint.is_file():
            raise ReplicaError("Checkpoint file must exist before waiting")
        return _apply(workspace, {"op": "task.wait", "task_id": args.task, "run_id": args.run,
                                 "requires": _json(args.requires),
                                 "checkpoint": {"path": str(checkpoint), "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}})
    if args.command == "native-next":
        return native_next(workspace, external_active=args.external_active, task_id=args.task)
    runner = Runner(workspace, host_capacity=args.host_capacity, slots_path=args.slots_file, executable=args.codex)
    if args.command == "step":
        return runner.step()
    # Progress events only when meaningful state changes, not every poll.
    previous = None
    for result in runner.run(args.interval):
        signature = canonical_json({key: result[key] for key in ("state", "active_run_ids", "task_states", "blocked")})
        if signature != previous:
            print(json.dumps(result, ensure_ascii=False), flush=True)
            previous = signature
    return None


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (ReplicaError, BackendError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Dispatcher stopped; worker processes are retained. Use step/run to reconcile their actual state.", file=sys.stderr)
        return 130
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
