"""Local coordination CLI. Device work remains in the managed Workbench queue."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

from .backend import BackendError
from .engine import execute, READS
from .ids import canonical_json, stable_id
from .inventory import load_inventory
from .paths import resolve_path, lexical, no_symlinks, read_regular
from .roles import source_root
from .runner import Runner, mutation, native_next, seed_app
from .store import ReplicaError, Store


def _operand(workspace, path):
    with Store(workspace).locked() as state:
        if state and state.get("schema") == 3:
            return resolve_path(workspace, state, str(path))
    return Path(path).expanduser().resolve()


def _json(path, workspace=None):
    if path == "-":
        return json.loads(sys.stdin.read())
    target = _operand(workspace, path) if workspace is not None else Path(path)
    return json.loads(read_regular(target))


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
    relocate = commands.add_parser("relocate", help="Guarded native-only schema-2/3 workspace relocation")
    relocate.add_argument("--file", required=True)
    relocate.add_argument("--dry-run", action="store_true")
    resolve = commands.add_parser("resolve", help="Resolve one explicit path, locator or file URI")
    resolve.add_argument("--kind", choices=("path", "locator", "file_uri"), required=True)
    resolve.add_argument("--value", required=True)
    resolve.add_argument("--exists", action="store_true")
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
    git_enable = commands.add_parser("git-enable", help="Save existing App source and enroll batched remote pushes")
    git_enable.add_argument("--repo", required=True, type=Path)
    git_enable.add_argument("--remote", default="origin")
    git_enable.add_argument("--branch")
    git_enable.add_argument("--include", action="append", help="Repository-relative source file/directory; default whole App repository")
    git_enable.add_argument("--exclude", action="append", help="Additional repository-relative exclusion glob")
    git_enable.add_argument("--push-count", type=int, default=5)
    git_enable.add_argument("--push-seconds", type=int, default=600)
    git_enable.add_argument("--service-state", type=Path)
    git_checkpoint = commands.add_parser("git-checkpoint", help="Synchronously save a stable group of assigned engineering outputs")
    git_checkpoint.add_argument("--task", required=True)
    git_checkpoint.add_argument("--run", required=True)
    git_checkpoint.add_argument("--path", action="append", help="Optional subset of assigned outputs; all must be stable until return")
    git_checkpoint.add_argument("--request-id", help="Reuse this ID when retrying the same save")
    for name in ("git-status", "git-flush", "git-disable", "git-resume"):
        commands.add_parser(name)
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
    workspace = no_symlinks(lexical(args.workspace, base=Path.cwd(), strict=False))
    if args.command.startswith("git-"):
        from . import git_sync
        if args.command == "git-enable":
            result = git_sync.enable(workspace, repo=args.repo, remote=args.remote, branch=args.branch,
                                     include=args.include, exclude=args.exclude, push_count=args.push_count,
                                     push_seconds=args.push_seconds, service_state=args.service_state)
            from ..cli import start
            try:
                service = start(Path(result["config"]["service_state"]))
                result["timer"] = "starting" if service.get("replica_git_sync") else "service_restart_required"
            except Exception as exc:
                result["timer"] = "unavailable"
                result["service_error"] = str(exc)
            return result
        if args.command == "git-checkpoint":
            return execute(workspace, {"op": "git.checkpoint", "request_id": args.request_id or uuid.uuid4().hex,
                                       "task_id": args.task, "run_id": args.run,
                                       **({"paths": args.path} if args.path is not None else {})})
        if args.command == "git-flush":
            return git_sync.request_push(workspace)
        if args.command == "git-disable":
            return git_sync.disable(workspace)
        if args.command == "git-resume":
            result = git_sync.resume(workspace)
            from ..cli import start
            service = start(Path(git_sync._read(Store(workspace), git_sync.CONFIG)['service_state']))
            result['timer'] = 'starting' if service.get('replica_git_sync') else 'service_restart_required'
            return result
        return {"ok": True, "git": git_sync.status(workspace)}
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
        requests = _json(args.file, workspace)
        if isinstance(requests, list):
            receipts = []
            for request in requests:
                receipt = _apply(workspace, request)
                receipts.append(receipt)
                if not receipt.get("ok"):
                    break
            return {"ok": all(item.get("ok") for item in receipts), "receipts": receipts}
        return _apply(workspace, requests)
    if args.command == "relocate":
        from .relocation import relocate
        # A restored schema-3 clone still names its previous directory. Read
        # the explicit local plan before attempting ordinary bound operations.
        request = _json(args.file)
        if request.get("op") != "workspace.relocate":
            raise ReplicaError("relocate requires workspace.relocate request")
        request.setdefault("request_id", stable_id("request", request))
        return relocate(workspace, request, dry_run=args.dry_run)
    if args.command == "resolve":
        return execute(workspace, {"op": "path.resolve", "kind": args.kind, "value": args.value, "exists": args.exists})
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
                    "mvp_results": {key: value["current_result"] for key, value in result["mvps"].items()},
                    **({"git": result["git"]} if "git" in result else {})}
        return result
    if args.command == "import":
        with Store(workspace).locked() as state:
            if state and state.get("schema") == 3:
                raise ReplicaError("Full reimport on an adopted workspace is unsupported; preserve source IDs")
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
        snapshot, task = _task(workspace, args.task)
        # A declared checks file is already supplied by --checks. Requiring it
        # a second time as --artifact turns a mechanical omission into a retry.
        check_path = _operand(workspace, args.checks)
        values = list(args.artifact)
        if str(check_path) in {str(resolve_path(workspace, snapshot, value)) for value in task["outputs"]}:
            values.append(str(check_path))
        artifacts = []
        seen = set()
        for value in values:
            path = resolve_path(workspace, snapshot, value)
            if path not in seen:
                artifacts.append({"path": str(path), "sha256": hashlib.sha256(read_regular(path)).hexdigest()})
                seen.add(path)
        return _apply(workspace, {"op": "task.submit", "task_id": args.task, "run_id": args.run,
                                 "artifacts": artifacts, "self_checks": _json(args.checks, workspace),
                                 "unresolved": _json(args.unresolved, workspace) if args.unresolved else [],
                                 "class_revisions": _json(args.class_revisions, workspace) if args.class_revisions else {},
                                 "dependency_revisions": _json(args.dependency_revisions, workspace) if args.dependency_revisions else {}})
    if args.command == "wait":
        checkpoint = _operand(workspace, args.checkpoint)
        if not checkpoint.is_file():
            raise ReplicaError("Checkpoint file must exist before waiting")
        return _apply(workspace, {"op": "task.wait", "task_id": args.task, "run_id": args.run,
                                 "requires": _json(args.requires, workspace),
                                 "checkpoint": {"path": str(checkpoint), "sha256": hashlib.sha256(read_regular(checkpoint)).hexdigest()}})
    if args.command == "native-next":
        return native_next(workspace, external_active=args.external_active, task_id=args.task)
    runner = Runner(workspace, host_capacity=args.host_capacity, slots_path=args.slots_file, executable=args.codex)
    if args.command == "step":
        return runner.step()
    # Progress events only when meaningful state changes, not every poll.
    previous = None
    for result in runner.run(args.interval):
        signature = canonical_json({key: result[key] for key in ("state", "active_run_ids", "task_states", "blocked", "git")
                                    if key in result})
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
