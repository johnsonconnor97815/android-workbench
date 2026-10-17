"""Deterministic metadata operations for discovery-driven reconstruction.

This module records decisions and observed process lifecycle. It neither makes
semantic decisions for agents nor starts/stops their execution itself.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from .ids import canonical_json, object_id, stable_id, task_id as make_task_id
from .roles import DEFAULT_MODELS
from .store import ReplicaError, Store


READS = {"status", "recover", "coverage", "inventory.get", "inventory.export"}
DISPOSITIONS = {"copy", "reuse", "platform", "exclude", "unknown"}
ROLES = {"main", "app", "analysis", "class", "function"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value, label="id"):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value)
            or value in {".", ".."} or len(value) > 250):
        # IDs become filenames; reserve five bytes for the .json suffix under
        # the filesystem's 255-byte component limit. Source reads are unlimited.
        raise ReplicaError(f"Invalid {label}: {value!r}")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ReplicaError(f"{label} must be a nonempty string")
    return value


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ReplicaError(f"{label} must be an object")
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise ReplicaError(f"{label} must be an array")
    return value


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReplicaError(f"{label} must be an integer >= {minimum}")
    return value


def _get(state, collection, identifier):
    _id(identifier, collection + " id")
    try:
        return state[collection][identifier]
    except KeyError as exc:
        raise ReplicaError(f"Unknown {collection} id: {identifier}") from exc


def _merge_list(target, values):
    for value in values:
        if value not in target:
            target.append(deepcopy(value))


def _touch(record):
    record["revision"] += 1


def _new_state(request):
    project = deepcopy(_mapping(request.get("project"), "project"))
    if not any(project.get(key) for key in ("id", "identity", "name")):
        raise ReplicaError("project needs an id, identity or name")
    capacity = _integer(request.get("capacity"), "capacity", 1)
    for key in ("output_path", "output_dir", "output", "analysis_root"):
        if key in project:
            _text(project[key], "project " + key)
    if "output_paths" in project:
        for path in _array(project["output_paths"], "project output_paths"):
            _text(path, "project output path")
    models = deepcopy(DEFAULT_MODELS)
    for key, value in _mapping(request.get("models", {}), "models").items():
        if key not in models:
            raise ReplicaError(f"Unknown model role/mode: {key}")
        models[key].update(_mapping(value, "model configuration"))
    return {"schema": 2, "revision": 0, "created_at": _now(), "project": project,
            "capacity": capacity, "models": models, "inventory": {},
            "inventory_status": {"complete": False, "limitations": ["Inventory completeness has not been confirmed"]},
            "entries": {}, "classes": {}, "tasks": {}, "runs": {}, "messages": {},
            "mvps": {}, "writers": {}, "discoveries": {}, "receipts": {}}


def _inventory_add(state, items):
    identifiers = []
    for raw in _array(items, "objects"):
        item = deepcopy(_mapping(raw, "inventory object"))
        item.pop("disposition", None)
        for field in ("kind", "name"):
            _text(item.get(field), "inventory " + field)
        if not item.get("source"):
            raise ReplicaError("inventory source is required")
        if not item.get("id"):
            if not item.get("carrier") or not item.get("signature"):
                raise ReplicaError("Object without id requires carrier and original signature")
            item["id"] = object_id(item["carrier"], item["kind"], item["signature"])
        identifier = _id(item["id"], "object id")
        # If source identity is supplied, reject an ID inconsistent with it.
        if item.get("carrier"):
            if identifier != object_id(item["carrier"], item["kind"], item.get("signature", item["name"])):
                raise ReplicaError("Object id does not match original carrier/signature")
        if item.get("class_id"):
            _id(item["class_id"], "class id")
        item.setdefault("evidence", [])
        item.setdefault("limitations", [])
        for field in ("evidence", "limitations", "sources", "additional_evidence", "origins", "entry_names", "abis"):
            if field in item:
                _array(item[field], "inventory " + field)
        previous = state["inventory"].get(identifier)
        if previous:
            original = deepcopy(previous)
            evidence_fields = {"scope", "evidence", "limitations", "source", "sources",
                               "additional_evidence", "origins", "entry_names", "abis"}
            immutable = {key: value for key, value in previous.items() if key not in evidence_fields}
            comparable = {key: value for key, value in item.items() if key not in evidence_fields}
            if any(key in immutable and immutable[key] != value for key, value in comparable.items()):
                raise ReplicaError(f"Inconsistent inventory redefinition: {identifier}")
            for key, value in comparable.items():
                previous.setdefault(key, value)
            _merge_list(previous.setdefault("sources", []), [previous["source"], item["source"]])
            for field in ("evidence", "limitations", "sources", "additional_evidence", "origins", "entry_names", "abis"):
                _merge_list(previous.setdefault(field, []), item.get(field, []))
            if previous != original:
                state["inventory"][identifier] = previous
        else:
            # Scope is a separate explicit decision; ingestion does not decide it.
            item.pop("scope", None)
            item["scope"] = {"disposition": "unknown", "reason": "Not yet decided", "evidence": []}
            state["inventory"][identifier] = item
        identifiers.append(identifier)
    return identifiers


def _requirements(state, requirements):
    result = []
    for raw in _array(requirements, "requires"):
        item = {"task_id": raw} if isinstance(raw, str) else deepcopy(_mapping(raw, "requirement"))
        kinds = [key for key in ("task_id", "class_id", "message_id") if key in item]
        if len(kinds) != 1:
            raise ReplicaError("requirement needs exactly one task_id, class_id or message_id")
        key = kinds[0]
        _id(item[key], key)
        if key == "task_id":
            _get(state, "tasks", item[key])
            if item.get("when", "delivered") not in {"delivered", "stopped"}:
                raise ReplicaError("Task requirement when must be delivered or stopped")
        elif key == "class_id":
            _get(state, "inventory", item[key])
            item["revision"] = _integer(item.get("revision", 1), "class requirement revision", 1)
        if key != "task_id" and "when" in item:
            raise ReplicaError("Only task requirements have a when condition")
        # A not-yet-received message may be an explicit wait condition.
        if item not in result:
            result.append(item)
    return result


def _class(state, identifier):
    _get(state, "inventory", identifier)
    if identifier not in state["classes"]:
        state["classes"][identifier] = {"id": identifier, "owner": None, "writer": None,
                                         "revision": 0, "structure": {}, "sources": [], "history": []}
    return state["classes"][identifier]


def _output_path(store, state, value):
    path = Path(_text(value, "output path")).expanduser()
    if not path.is_absolute():
        path = store.workspace / path
    path = path.resolve()
    artifact_root = (store.workspace / "artifacts").resolve()
    if not artifact_root.is_relative_to(store.workspace):
        raise ReplicaError("Artifacts directory escapes workspace")
    roots = [artifact_root]
    project = state["project"]
    configured = project.get("output_paths", [])
    if isinstance(configured, str):
        configured = [configured]
    for key in ("output_path", "output_dir", "output", "analysis_root"):
        if isinstance(project.get(key), str):
            configured = [*configured, project[key]]
    for value in configured:
        root = Path(value).expanduser()
        roots.append((root if root.is_absolute() else store.workspace / root).resolve())
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ReplicaError(f"Output outside artifacts or declared project output paths: {path}")
    # Even a broad output declaration cannot authorize overwriting metadata.
    if path.is_relative_to(store.workspace):
        relative = path.relative_to(store.workspace)
        if relative.parts[0] in {"coordination", "inventory", "classes", "tasks", "messages", "mvp"} or relative == Path("project.json"):
            raise ReplicaError("Output overlaps workflow metadata")
    return str(path)


def _ensure_structure(store, state, identifier, associations):
    record = _class(state, identifier)
    if record["revision"]:
        return None
    task = _task_add(store, state, {"role": "class", "mode": "structure", "object_id": identifier,
                                  "class_id": identifier, "description": "Establish original class declarations and relations",
                                  "sources": [state["inventory"][identifier]["source"]],
                                  "entry_ids": associations.get("entry_ids", []),
                                  "mvp_ids": associations.get("mvp_ids", [])})
    return task


def _task_add(store, state, raw):
    spec = deepcopy(_mapping(raw, "task"))
    role = spec.get("role")
    mode = _text(spec.get("mode"), "task mode")
    for field in ("sources", "origins"):
        if field in spec:
            _array(spec[field], field)
    if role not in ROLES:
        raise ReplicaError(f"Unknown role: {role}")
    target = spec.get("object_id")
    class_id = spec.get("class_id")
    if target:
        obj = _get(state, "inventory", target)
        class_id = class_id or obj.get("class_id")
        if class_id and obj.get("class_id") and class_id != obj["class_id"]:
            raise ReplicaError("Task class does not match original member owner")
    if class_id:
        _class(state, class_id)
    identifier = make_task_id(role, mode, target, class_id) if (target or class_id) else _id(spec.get("id"), "task id")
    if spec.get("id") and spec["id"] != identifier:
        raise ReplicaError(f"Task id must be canonical: {identifier}")
    for existing in state["tasks"].values():
        if target and existing.get("object_id") == target and existing["mode"] == mode and existing["role"] != role:
            raise ReplicaError("An object/mode already has a different logical role")
    requirements = _requirements(state, spec.get("requires", []))
    associations = {}
    for field in ("entry_ids", "mvp_ids"):
        associations[field] = _array(spec.get(field, []), field)
        collection = "entries" if field == "entry_ids" else "mvps"
        for value in associations[field]:
            _get(state, collection, value)
    outputs = [_output_path(store, state, value) for value in _array(spec.get("outputs", []), "outputs")]
    existing = state["tasks"].get(identifier)
    owner = spec.get("owner", existing["owner"] if existing else identifier)
    _id(owner, "task owner")
    for path in outputs:
        previous = state["writers"].get(path)
        if previous and previous != identifier:
            raise ReplicaError(f"Output already has a logical writer: {path}")
    if existing:
        if existing["owner"] != owner:
            raise ReplicaError("A canonical task cannot change logical owner")
        prior_implementation = (deepcopy(existing["requires"]), deepcopy(existing["outputs"]))
        if class_id and existing["class_id"] != class_id:
            if existing["class_id"] or existing["run_id"]:
                raise ReplicaError("Cannot change original class membership of an assigned/live task")
            existing["class_id"] = class_id
        if existing["requires"] != requirements and requirements:
            if existing["run_id"]:
                raise ReplicaError("Cannot change live task requirements; use task.wait")
            _merge_list(existing["requires"], requirements)
        for field, values in {**associations, "sources": spec.get("sources", []),
                              "origins": spec.get("origins", []), "outputs": outputs}.items():
            _merge_list(existing[field], values)
        # Caller/source/entry associations do not change the implementation.
        # Repeated discovery must not stale an otherwise current delivery/MVP.
        if prior_implementation != (existing["requires"], existing["outputs"]):
            _touch(existing)
        result = existing
    else:
        result = {"id": identifier, "role": role, "mode": mode, "owner": owner,
                  "object_id": target, "class_id": class_id,
                  "description": _text(spec.get("description"), "task description"),
                  "sources": _array(spec.get("sources", []), "sources"), "outputs": outputs,
                  "requires": requirements, **associations, "origins": spec.get("origins", []),
                  "callers": [], "revision": 1, "state": "queued", "run_id": None,
                  "runs": [], "deliveries": [], "checkpoint": None,
                  "model": spec.get("model"), "effort": spec.get("effort"),
                  "created_at": _now()}
        state["tasks"][identifier] = result
    for path in outputs:
        state["writers"][path] = identifier
    if role == "class" and class_id:
        record = state["classes"][class_id]
        field = "writer" if mode == "code" else "owner" if mode == "structure" else None
        if field:
            designation = identifier if field == "writer" else owner
            if record[field] and record[field] != designation:
                raise ReplicaError(f"Class already has a designated {field}")
            record[field] = designation
    if class_id and (role == "function" or (role == "class" and mode == "code")):
        _ensure_structure(store, state, class_id, associations)
        _merge_list(result["requires"], [{"class_id": class_id, "revision": 1}])
    return result


def _artifact(store, state, raw):
    item = deepcopy(_mapping(raw, "artifact"))
    path = _output_path(store, state, item.get("path"))
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ReplicaError(f"Artifact is not a regular file: {path}")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise ReplicaError(f"Cannot read artifact: {path}: {exc}") from exc
    supplied = item.get("sha256")
    if supplied is None or supplied != digest:
        raise ReplicaError(f"Artifact SHA-256 missing or mismatched: {path}")
    item.update(path=path, sha256=digest)
    return item


def _remove_directory_outputs(store, state, request):
    task = _get(state, "tasks", request.get("task_id"))
    if task["run_id"]:
        raise ReplicaError("Cannot reconcile outputs while the task has a live run")
    if _id(request.get("owner"), "task owner") != task["owner"]:
        raise ReplicaError("Directory correction must preserve the logical owner")
    if _integer(request.get("expected_revision"), "expected_revision", 1) != task["revision"]:
        raise ReplicaError(f"Stale task revision; current revision is {task['revision']}")
    reason = _text(request.get("reason"), "directory correction reason")
    evidence = [_artifact(store, state, item) for item in
                _array(request.get("evidence"), "directory correction evidence")]
    if not evidence:
        raise ReplicaError("Directory correction needs existing file evidence")
    paths = [_output_path(store, state, value) for value in
             _array(request.get("directories"), "directories")]
    if not paths or len(set(paths)) != len(paths):
        raise ReplicaError("Directory correction needs nonempty unique paths")
    for value in paths:
        if value not in task["outputs"] or state["writers"].get(value) != task["id"]:
            raise ReplicaError(f"Directory is not assigned to this task: {value}")
        path = Path(value)
        if not path.is_dir():
            raise ReplicaError(f"Correction only removes existing directory registrations: {value}")
        for child in path.rglob("*"):
            if child.is_symlink() or (not child.is_dir() and not child.is_file()):
                raise ReplicaError(f"Directory contains an unsupported filesystem entry: {child}")
            if child.is_file() and (str(child) not in task["outputs"]
                                    or state["writers"].get(str(child)) != task["id"]):
                raise ReplicaError(f"Every child file must remain assigned to this task: {child}")
    correction = {"directories": paths, "reason": reason, "evidence": evidence,
                  "owner": task["owner"], "previous_revision": task["revision"],
                  "recorded_at": _now()}
    task["outputs"] = [value for value in task["outputs"] if value not in paths]
    for value in paths:
        del state["writers"][value]
    task.setdefault("output_directory_corrections", []).append(correction)
    _touch(task)
    return {"task_id": task["id"], "task_revision": task["revision"],
            "correction": correction, "remaining_outputs": task["outputs"]}


def _revisions(state, mapping, collection):
    result = {}
    for identifier, revision in _mapping(mapping, collection + " revisions").items():
        _get(state, collection, identifier)
        result[identifier] = _integer(revision, "revision")
    return result


def _delivery_issues(store, state, task, delivery):
    issues = []
    if (task["role"] == "function" or (task["role"] == "class" and task["mode"] == "code")) and not delivery["artifacts"]:
        issues.append("Implementation delivery has no code artifact")
    if set(task["outputs"]) != {item["path"] for item in delivery["artifacts"]}:
        issues.append("Delivery does not contain every assigned output")
    if task["role"] == "class" and task["mode"] == "structure" and task.get("class_id"):
        if not state["classes"][task["class_id"]]["revision"]:
            issues.append("Class structure has not been published")
    required_classes = {req["class_id"] for req in task["requires"] if "class_id" in req}
    if task.get("class_id") and task["role"] in {"function", "class"} and task["mode"] != "structure":
        required_classes.add(task["class_id"])
    for identifier in required_classes:
        if identifier not in delivery["class_revisions"]:
            issues.append(f"Missing required class revision: {identifier}")
    for req in task["requires"]:
        if "task_id" in req and req.get("when", "delivered") == "delivered":
            if req["task_id"] not in delivery["dependency_revisions"]:
                issues.append(f"Missing required task revision: {req['task_id']}")
    run = state["runs"][delivery["run_id"]]
    if any(run.get("reported_" + field) not in {None, run[field]} for field in ("model", "effort")):
        issues.append("Backend reported a different model/effort")
    for collection, key in (("classes", "class_revisions"), ("tasks", "dependency_revisions")):
        for identifier, revision in delivery[key].items():
            if state[collection].get(identifier, {}).get("revision") != revision:
                issues.append(f"Stale {collection} revision: {identifier}")
    for artifact in delivery["artifacts"]:
        try:
            _artifact(store, state, artifact)
        except ReplicaError as exc:
            issues.append(str(exc))
    return issues


def _delivery_current(store, state, task):
    if task["state"] != "delivered" or task["run_id"] or not task["deliveries"]:
        return False
    delivery = task["deliveries"][-1]
    return delivery["accepted"] and not _delivery_issues(store, state, task, delivery)


def _ready(store, state, task):
    if task["state"] != "queued" or task["run_id"]:
        return False
    identifier = task.get("object_id")
    if identifier and (task["role"] == "function" or (task["role"] == "class" and task["mode"] == "code")):
        # Task creation already validates the immutable object identity. Read
        # only its scope here: many ready tasks must not load all source shards.
        scope = state["inventory"].scopes.get(identifier, {"disposition": "unknown"})
        if scope["disposition"] in {"reuse", "platform", "exclude"}:
            return False
    return _continuation_prerequisites(store, state, task)


def _dispatch_policy(state):
    # Existing schema-2 workspaces need no migration; an absent policy is FIFO.
    return state.get("dispatch_policy", {"revision": 0, "task_ids": [], "fallback": "created_at_task_id"})


def _dispatch_ranks(store, state):
    """Inherit App order through unmet prerequisites without changing readiness."""
    order = _dispatch_policy(state)["task_ids"]
    if not order:
        return {}
    structures = {}
    for task in state["tasks"].values():
        if task["role"] == "class" and task["mode"] == "structure":
            record = state["classes"].get(task.get("class_id"), {})
            if record.get("owner") == task["owner"]:
                structures.setdefault(task["class_id"], []).append(task["id"])
    ranks, satisfied = {}, {}
    for rank, identifier in enumerate(order):
        pending = [identifier]
        while pending:
            identifier = pending.pop()
            # Earlier roots win shared dependencies; this also bounds cycles.
            if identifier in ranks and ranks[identifier] <= rank:
                continue
            ranks[identifier] = rank
            task = state["tasks"][identifier]
            if task["state"] not in {"queued", "waiting", "reserved", "running"}:
                continue
            defer_terminal = bool(_planning_wake_messages(state, task))
            for requirement in task["requires"]:
                if "task_id" in requirement:
                    when = requirement.get("when", "delivered")
                    if when == "stopped" and defer_terminal:
                        continue
                    key = (requirement["task_id"], when)
                    if key not in satisfied:
                        satisfied[key] = _prerequisites(store, state, {"requires": [requirement]})
                    if not satisfied[key]:
                        pending.append(requirement["task_id"])
                elif "class_id" in requirement:
                    class_id = requirement["class_id"]
                    if state["classes"].get(class_id, {}).get("revision", 0) < requirement["revision"]:
                        pending.extend(structures.get(class_id, []))
                # A message does not identify a schedulable dependency owner.
    return ranks


def _ready_tasks(store, state):
    ranks = _dispatch_ranks(store, state)
    fallback = len(_dispatch_policy(state)["task_ids"])
    ready = [task for task in state["tasks"].values() if _ready(store, state, task)]
    return sorted(ready, key=lambda task: (ranks.get(task["id"], fallback),
                                            task.get("created_at", ""), task["id"]))


def _pending_messages(state, task):
    assigned = {identifier for run_id in task["runs"]
                for identifier in state["runs"][run_id].get("inbox_message_ids", [])}
    return [identifier for identifier, message in state["messages"].items()
            if message.get("wake") and message["recipient"] == task["id"] and identifier not in assigned]


def _planning_wake_messages(state, task):
    if task["role"] != "app" or task["mode"] != "plan":
        return []
    return _pending_messages(state, task)


def _continuation_prerequisites(store, state, task):
    # Planning can handle a new request while an observed child works again.
    # Delivery, declaration and specific-reply dependencies remain mandatory.
    return _prerequisites(store, state, task,
                          defer_terminal_waits=bool(_planning_wake_messages(state, task)))


def _prerequisites(store, state, task, *, defer_terminal_waits=False):
    for req in task["requires"]:
        if "task_id" in req:
            dependency = state["tasks"][req["task_id"]]
            if req.get("when", "delivered") == "stopped":
                if defer_terminal_waits:
                    continue
                if dependency["state"] == "queued" or dependency["run_id"] or not dependency["runs"] or state["runs"][dependency["runs"][-1]]["state"] != "stopped":
                    return False
            elif not _delivery_current(store, state, dependency):
                return False
        elif "class_id" in req:
            if state["classes"].get(req["class_id"], {}).get("revision", 0) < req["revision"]:
                return False
        elif req["message_id"] not in state["messages"]:
            return False
    return True


def _live_run(state, task_id, run_id):
    task = _get(state, "tasks", task_id)
    run = _get(state, "runs", run_id)
    if task["run_id"] != run_id or run["task_id"] != task_id or run["state"] == "stopped":
        raise ReplicaError("Run no longer owns this task")
    return task, run


def _check_wait_cycles(state):
    visiting, visited = set(), set()

    def visit(identifier):
        if identifier in visiting:
            raise ReplicaError("True task prerequisites form a cycle; calls are not waits")
        if identifier in visited:
            return
        visiting.add(identifier)
        for requirement in state["tasks"][identifier]["requires"]:
            if "task_id" in requirement:
                visit(requirement["task_id"])
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in state["tasks"]:
        visit(identifier)


def _checks(raw):
    result = deepcopy(_array(raw, "self_checks"))
    for check in result:
        _mapping(check, "self-check")
        _text(check.get("name"), "self-check name")
        if check.get("result") not in {"pass", "fail", "unknown"}:
            raise ReplicaError("Self-check result must be pass, fail or unknown")
        if not check.get("evidence"):
            raise ReplicaError("Self-check needs evidence")
    return result


def _coverage_summary(store, state):
    counts = {key: 0 for key in ("copy_delivered", "copy_pending", "reuse", "platform", "exclude", "unknown")}
    scopes = state["inventory"].scope_counts()
    for key in ("reuse", "platform", "exclude", "unknown"):
        counts[key] = scopes[key]
    implementations = {}
    for task in state["tasks"].values():
        if task.get("object_id") and (task["role"] == "function" or (task["role"] == "class" and task["mode"] == "code")):
            implementations.setdefault(task["object_id"], []).append(task)
    for identifier, tasks in implementations.items():
        scope = state["inventory"].scopes.get(identifier, {"disposition": "unknown"})
        if scope["disposition"] == "copy" and all(_delivery_current(store, state, task) for task in tasks):
            counts["copy_delivered"] += 1
    counts["copy_pending"] = scopes["copy"] - counts["copy_delivered"]
    residual = counts["copy_pending"] + counts["unknown"]
    return {"counts": counts, "residual_count": residual,
            "inventory_complete": state["inventory_status"]["complete"],
            "limitations": state["inventory_status"]["limitations"],
            "scope_complete": bool(state["inventory_status"]["complete"] and not residual),
            "runtime_verified": False}


def _coverage(store, state):
    groups = {key: [] for key in ("copy_delivered", "copy_pending", "reuse", "platform", "exclude", "unknown")}
    for identifier, obj in state["inventory"].items():
        disposition = obj["scope"]["disposition"]
        if disposition == "copy":
            tasks = [task for task in state["tasks"].values() if task.get("object_id") == identifier
                     and (task["role"] == "function" or (task["role"] == "class" and task["mode"] == "code"))]
            disposition = "copy_delivered" if tasks and all(_delivery_current(store, state, task) for task in tasks) else "copy_pending"
        groups[disposition].append(identifier)
    residual = groups["copy_pending"] + groups["unknown"]
    return {"groups": groups, "counts": {key: len(value) for key, value in groups.items()},
            "residual": residual, "inventory_complete": state["inventory_status"]["complete"],
            "limitations": state["inventory_status"]["limitations"],
            "scope_complete": bool(state["inventory_status"]["complete"] and not residual),
            "runtime_verified": False}


def _dispatch(store, state, request):
    op = request["op"]
    if op == "project.update":
        changes = deepcopy(_mapping(request.get("changes"), "project changes"))
        if not changes or not request.get("evidence"):
            raise ReplicaError("Project update requires changes and evidence")
        protected = {"id", "identity", "source", "source_path", "source_paths", "source_dir",
                     "output", "output_path", "output_paths", "output_dir", "analysis_root", "history"}
        if protected.intersection(changes):
            raise ReplicaError("Project origin identity/source/output paths and history are immutable")
        previous = {key: deepcopy(state["project"].get(key)) for key in changes}
        state["project"].update(changes)
        state["project"].setdefault("history", []).append({
            "request_id": request["request_id"], "actor": request.get("actor", "app"),
            "changes": changes, "previous": previous, "evidence": deepcopy(request["evidence"]),
            "recorded_at": _now()})
        return {"project": state["project"]}
    if op == "dispatch.set":
        current = _dispatch_policy(state)
        if _integer(request.get("expected_revision"), "expected_revision") != current["revision"]:
            raise ReplicaError(f"Stale dispatch policy revision; current revision is {current['revision']}")
        identifiers = _array(request.get("task_ids"), "dispatch task_ids")
        seen = set()
        for identifier in identifiers:
            _get(state, "tasks", identifier)
            if identifier in seen:
                raise ReplicaError(f"Duplicate dispatch task id: {identifier}")
            seen.add(identifier)
        evidence = _array(request.get("evidence"), "dispatch evidence")
        if not evidence:
            raise ReplicaError("Dispatch policy requires App decision evidence")
        actor = _text(request.get("actor", "app"), "dispatch actor")
        state["dispatch_policy"] = {"revision": current["revision"] + 1,
                                    "task_ids": deepcopy(identifiers), "fallback": "created_at_task_id",
                                    "actor": actor, "evidence": deepcopy(evidence),
                                    "request_id": request["request_id"], "recorded_at": _now()}
        return {"dispatch_policy": state["dispatch_policy"]}
    if op == "inventory.add":
        identifiers = _inventory_add(state, request.get("objects"))
        if "complete" in request:
            if not isinstance(request["complete"], bool):
                raise ReplicaError("inventory complete must be boolean")
            state["inventory_status"]["complete"] = request["complete"]
        if "limitations" in request:
            state["inventory_status"]["limitations"] = _array(request["limitations"], "limitations")
        return {"object_ids": identifiers}
    if op == "scope.set":
        obj = _get(state, "inventory", request.get("object_id"))
        disposition = request.get("disposition")
        if disposition not in DISPOSITIONS:
            raise ReplicaError("Invalid scope disposition")
        reason = _text(request.get("reason"), "scope reason")
        evidence = request.get("evidence")
        if disposition != "unknown" and not evidence:
            raise ReplicaError("Scope decisions require evidence; unreached is not exclusion evidence")
        obj["scope"] = {"disposition": disposition, "reason": reason, "evidence": evidence or []}
        state["inventory"].set_scope(obj["id"], obj["scope"])
        return {"object": obj}
    if op == "inventory.import":
        result = _dispatch(store, state, {**request, "op": "inventory.add"})
        entries = _array(request.get("entries"), "entries")
        for entry in entries:
            _dispatch(store, state, {"op": "entry.add", "entry": entry})
        return {"objects": len(result["object_ids"]), "entries": len(entries)}
    if op == "entry.add":
        entry = deepcopy(_mapping(request.get("entry"), "entry"))
        identifier = _id(entry.get("id"), "entry id")
        if not entry.get("trigger") or not entry.get("evidence"):
            raise ReplicaError("Entry requires trigger and evidence")
        roots = _array(entry.get("root_object_ids", entry.get("roots", [])), "root_object_ids")
        if not roots:
            raise ReplicaError("Entry needs original root objects")
        for root in roots:
            _get(state, "inventory", root)
        entry["root_object_ids"] = roots
        if identifier in state["entries"] and state["entries"][identifier] != entry:
            raise ReplicaError("Entry already defined with different content")
        state["entries"][identifier] = entry
        return {"entry": entry}
    if op == "task.add":
        task = _task_add(store, state, request.get("task"))
        return {"task_id": task["id"], "task": task}
    if op == "task.outputs.remove_directories":
        return _remove_directory_outputs(store, state, request)
    if op == "task.discover":
        caller = _get(state, "tasks", request.get("task_id"))
        target = request.get("target")
        if isinstance(target, str):
            obj = _get(state, "inventory", target)
        else:
            identifier = _inventory_add(state, [target])[0]
            obj = state["inventory"][identifier]
        source = request.get("source")
        if not source:
            raise ReplicaError("Discovery needs original source evidence")
        discovery = {"caller": caller["id"], "target": obj["id"], "source": source,
                     "relation": request.get("relation", "call")}
        discovery_id = stable_id("discovery", discovery)
        state["discoveries"][discovery_id] = discovery
        disposition = obj["scope"]["disposition"]
        if disposition in {"reuse", "platform", "exclude"}:
            return {"discovery": discovery, "task_id": None, "disposition": disposition}
        entry_ids = [*caller["entry_ids"], *request.get("entry_ids", [])]
        mvp_ids = [*caller["mvp_ids"], *request.get("mvp_ids", [])]
        spec = {"object_id": obj["id"], "sources": [source], "origins": [discovery],
                "entry_ids": list(dict.fromkeys(entry_ids)), "mvp_ids": list(dict.fromkeys(mvp_ids))}
        if disposition == "unknown":
            spec.update(role="analysis", mode="scope", description="Resolve discovered object's unknown scope")
        elif obj["kind"] in {"class", "native_module", "native_library", "resource", "resource_group", "engineering", "manifest", "dex"}:
            spec.update(role="class", mode="structure", class_id=obj["id"], description="Establish discovered structure")
        else:
            spec.update(role="function", mode="code", class_id=obj.get("class_id"), description="Implement discovered original function")
        task = _task_add(store, state, spec)
        _merge_list(task["callers"], [caller["id"]])
        return {"discovery": discovery, "task_id": task["id"], "task": task, "disposition": disposition}
    if op == "class.publish":
        record = _class(state, request.get("class_id"))
        owner = _id(request.get("owner"), "class owner")
        if record["owner"] and record["owner"] != owner:
            raise ReplicaError("Only the logical structural owner may update this class")
        if _integer(request.get("expected_revision"), "expected_revision") != record["revision"]:
            raise ReplicaError(f"Stale class revision; current revision is {record['revision']}")
        structure = deepcopy(_mapping(request.get("structure"), "structure"))
        writer = request.get("writer", record["writer"])
        if writer:
            _id(writer, "class writer")
            if record["writer"] and writer != record["writer"]:
                raise ReplicaError("Class already has a designated code writer")
        if record["revision"]:
            record["history"].append({key: deepcopy(value) for key, value in record.items() if key != "history"})
        record.update(owner=owner, writer=writer, structure=structure)
        _merge_list(record["sources"], _array(request.get("sources", request.get("evidence", [])), "class sources"))
        _touch(record)
        return {"class": record}
    if op == "task.claim":
        backend = deepcopy(request.get("backend"))
        if not backend:
            raise ReplicaError("Claim needs backend identity")
        external = request.get("external_active", 0)
        if isinstance(external, list):
            external = len(external)
        external = _integer(external, "external_active")
        occupied = sum(run["state"] != "stopped" for run in state["runs"].values())
        if occupied + external >= state["capacity"]:
            return {"task": None, "run": None, "reason": "capacity", "occupied": occupied, "external_active": external}
        if request.get("task_id"):
            candidate = _get(state, "tasks", request["task_id"])
            task = candidate if _ready(store, state, candidate) else None
        else:
            task = next(iter(_ready_tasks(store, state)), None)
        if task is None:
            return {"task": None, "run": None, "reason": "no_ready_task"}
        model_key = task["role"]
        if task["role"] == "class" and task["mode"] == "code":
            model_key = "class.code"
        elif task["role"] == "analysis" and task["mode"] == "extract":
            model_key = "analysis.extract"
        model = task.get("model") or state["models"][model_key]["model"]
        effort = task.get("effort") or state["models"][model_key]["effort"]
        identifier = "run-" + uuid.uuid4().hex
        prior = state["runs"][task["runs"][-1]] if task["runs"] else None
        run = {"id": identifier, "task_id": task["id"], "state": "reserved", "backend": backend,
               "model": model, "effort": effort, "requested_model": model, "requested_effort": effort,
               "reported_model": None, "reported_effort": None, "backend_handle": None,
               "continuation": prior.get("backend_handle") if prior else None,
               "reserved_at": _now(), "external_active": external,
               "inbox_message_ids": _pending_messages(state, task)}
        task.update(state="reserved", run_id=identifier)
        task["runs"].append(identifier)
        _touch(task)
        state["runs"][identifier] = run
        return {"task": task, "run": run, "model": model, "effort": effort}
    if op == "run.started":
        run = _get(state, "runs", request.get("run_id"))
        task, run = _live_run(state, run["task_id"], run["id"])
        if run["state"] != "reserved":
            raise ReplicaError("Only a reserved run can be marked started")
        handle = request.get("backend_handle")
        if not handle:
            raise ReplicaError("Actual backend handle is required")
        for field in ("model", "effort"):
            requested = request.get("requested_" + field, run[field])
            if requested != run[field]:
                raise ReplicaError("Started configuration differs from reservation")
            if request.get("reported_" + field) is not None:
                _text(request["reported_" + field], "reported " + field)
            run["reported_" + field] = request.get("reported_" + field)
        run.setdefault("configuration_observations", []).append({
            "phase": "started", "reported_model": run["reported_model"],
            "reported_effort": run["reported_effort"], "recorded_at": _now()})
        run.update(state="running", backend_handle=deepcopy(handle), started_at=_now())
        task["state"] = "running"
        _touch(task)
        return {"run": run, "task": task,
                "configuration_match": all(run["reported_" + field] in {None, run[field]} for field in ("model", "effort"))}
    if op == "task.wait":
        task, run = _live_run(state, request.get("task_id"), request.get("run_id"))
        if run["state"] not in {"running", "waiting_pending_stop"}:
            raise ReplicaError("Only a running task can checkpoint a wait")
        requirements = _requirements(state, request.get("requires"))
        if not requirements or any(req.get("task_id") == task["id"] for req in requirements):
            raise ReplicaError("Wait needs concrete prerequisites other than itself")
        if "checkpoint" not in request:
            raise ReplicaError("Wait needs a checkpoint")
        task.update(state="waiting", checkpoint=deepcopy(request["checkpoint"]))
        _merge_list(task["requires"], requirements)
        run["state"] = "waiting_pending_stop"
        _touch(task)
        return {"task": task, "run": run, "stop_pending": True}
    if op == "task.submit":
        task, run = _live_run(state, request.get("task_id"), request.get("run_id"))
        if run["state"] not in {"running", "waiting_pending_stop", "delivery_pending_stop"}:
            raise ReplicaError("Only a running task can submit")
        artifacts = [_artifact(store, state, item) for item in _array(request.get("artifacts"), "artifacts")]
        for artifact in artifacts:
            path = artifact["path"]
            if path not in task["outputs"]:
                raise ReplicaError("Artifact was not assigned to this logical writer")
            if state["writers"].get(path) != task["id"]:
                raise ReplicaError("Artifact is owned by a different writer")
        checks = _checks(request.get("self_checks"))
        unresolved = deepcopy(_array(request.get("unresolved"), "unresolved"))
        delivery = {"id": stable_id("delivery", run["id"], request["request_id"]), "run_id": run["id"],
                    "artifacts": artifacts, "self_checks": checks, "unresolved": unresolved,
                    "class_revisions": _revisions(state, request.get("class_revisions", {}), "classes"),
                    "dependency_revisions": _revisions(state, request.get("dependency_revisions", {}), "tasks"),
                    "submitted_at": _now()}
        issues = _delivery_issues(store, state, task, delivery)
        delivery.update(stale=bool(issues), stale_reasons=issues,
                        accepted=bool(checks) and all(check["result"] == "pass" for check in checks) and not unresolved and not issues)
        task["deliveries"].append(delivery)
        task["state"] = "delivered"
        run["state"] = "delivery_pending_stop"
        _touch(task)
        return {"task": task, "run": run, "delivery": delivery, "stop_pending": True}
    if op == "run.stopped":
        run = _get(state, "runs", request.get("run_id"))
        task, run = _live_run(state, run["task_id"], run["id"])
        observation = deepcopy(_mapping(request.get("observation"), "stop observation"))
        if observation.get("confirmed") is not True or observation.get("writers_stopped") is not True or not observation.get("evidence"):
            raise ReplicaError("Stop requires confirmed process and writer-descendant observation with evidence")
        outcome = _text(request.get("outcome"), "stop outcome")
        if outcome not in {"delivered", "waiting", "failed", "launch_failed", "interrupted"}:
            raise ReplicaError("Invalid stop outcome")
        if outcome == "launch_failed" and run.get("started_at"):
            raise ReplicaError("Started run cannot be a confirmed launch failure")
        if "backend_handle" in request:
            run["backend_handle"] = deepcopy(request["backend_handle"])
        for field in ("model", "effort"):
            if "reported_" + field in request:
                if request["reported_" + field] is not None:
                    _text(request["reported_" + field], "reported " + field)
                    run["reported_" + field] = deepcopy(request["reported_" + field])
        run.setdefault("configuration_observations", []).append({
            "phase": "stopped", "reported_model": request.get("reported_model"),
            "reported_effort": request.get("reported_effort"), "recorded_at": _now()})
        run.update(state="stopped", stopped_at=_now(), stop_observation=observation, outcome=outcome)
        task["run_id"] = None
        if task["state"] == "delivered":
            delivery = task["deliveries"][-1]
            issues = _delivery_issues(store, state, task, delivery)
            if issues:
                delivery.update(stale=True, stale_reasons=issues, accepted=False)
            if outcome != "delivered":
                delivery.update(accepted=False, backend_outcome=outcome)
                task["state"] = "failed"
            elif any(run.get("reported_" + field) not in {None, run[field]} for field in ("model", "effort")):
                task["state"] = "failed"
        elif task["state"] != "waiting" or outcome != "waiting":
            task["state"] = "failed"
        _touch(task)
        return {"run": run, "task": task, "released": True}
    if op == "task.resume":
        task = _get(state, "tasks", request.get("task_id"))
        if task["run_id"]:
            raise ReplicaError("Prior run is still live or unconfirmed; cannot resume")
        if task["state"] not in {"waiting", "failed", "delivered"}:
            raise ReplicaError("Only waiting, failed or delivered work can resume")
        reason = _text(request.get("reason"), "resume reason")
        if not request.get("evidence"):
            raise ReplicaError("Resume needs evidence")
        if not _continuation_prerequisites(store, state, task):
            raise ReplicaError("Task prerequisites are not satisfied")
        task.update(state="queued", resume_reason=reason, resume_evidence=request["evidence"])
        _touch(task)
        return {"task": task}
    if op == "message.send":
        fields = {key: deepcopy(request.get(key)) for key in ("type", "sender", "recipient", "task_id", "related_request_id", "payload", "evidence")}
        for key in ("type", "sender", "recipient"):
            _text(fields[key], "message " + key)
        if fields["task_id"]:
            _get(state, "tasks", fields["task_id"])
        wake = request.get("wake", False)
        if not isinstance(wake, bool):
            raise ReplicaError("message wake must be boolean")
        if wake:
            _get(state, "tasks", fields["recipient"])
        identifier = _id(request.get("message_id", stable_id("message", request["request_id"])), "message id")
        message = {"id": identifier, **fields, "wake": wake}
        if identifier in state["messages"] and state["messages"][identifier] != message:
            raise ReplicaError("Immutable message already exists with different content")
        state["messages"][identifier] = message
        return {"message": message}
    if op == "mvp.define":
        return _mvp_define(state, request)
    if op == "mvp.record":
        return _mvp_record(store, state, request)
    raise ReplicaError(f"Unknown operation: {op}")


def _mvp_define(state, request):
    spec = deepcopy(request.get("mvp", {key: value for key, value in request.items()
                                        if key not in {"op", "request_id"}}))
    _mapping(spec, "mvp")
    identifier = _id(spec.get("id"), "mvp id")
    if identifier in state["mvps"]:
        raise ReplicaError("MVP is already defined; define a new explicit scope/version")
    entries = _array(spec.get("entry_ids"), "entry_ids")
    tasks = _array(spec.get("required_task_ids"), "required_task_ids")
    if not entries or not tasks:
        raise ReplicaError("MVP requires entries and implementation tasks")
    for entry in entries:
        _get(state, "entries", entry)
    for task in tasks:
        _get(state, "tasks", task)
    goals = _array(spec.get("goals"), "goals")
    identifiers = []
    for goal in goals:
        goal_id = _id(goal if isinstance(goal, str) else _mapping(goal, "goal").get("id"), "goal id")
        identifiers.append(goal_id)
    if not goals or len(identifiers) != len(set(identifiers)):
        raise ReplicaError("MVP needs distinct named observation goals")
    if not spec.get("conditions") or not spec.get("target_versions"):
        raise ReplicaError("MVP requires target_versions and explicit runtime conditions")
    record = {**spec, "id": identifier, "records": [], "result": "unknown"}
    state["mvps"][identifier] = record
    for task_id in tasks:
        _merge_list(state["tasks"][task_id]["mvp_ids"], [identifier])
    return {"mvp": record}


def _observation(store, state, raw):
    value = deepcopy(_mapping(raw, "paired observation"))
    # Observations may live outside code outputs, but must be explicit regular
    # files within the workspace (or declared output roots), with real hashes.
    path = Path(_text(value.get("path"), "observation path")).expanduser()
    if not path.is_absolute():
        path = store.workspace / path
    path = path.resolve()
    if not path.is_relative_to(store.workspace):
        _output_path(store, state, str(path))
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ReplicaError("Observation must be a regular file")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise ReplicaError(f"Cannot read observation: {exc}") from exc
    if value.get("sha256") != digest:
        raise ReplicaError("Observation SHA-256 missing or mismatched")
    value["path"] = str(path)
    return value


def _mvp_record(store, state, request):
    mvp = _get(state, "mvps", request.get("mvp_id"))
    versions = {"original": request.get("original_version"), "candidate": request.get("candidate_version")}
    if not all(versions.values()) or versions != mvp["target_versions"]:
        raise ReplicaError("Paired versions must match explicit MVP target_versions")
    scope = deepcopy(_mapping(request.get("runtime_scope"), "runtime_scope"))
    if not scope.get("kind") or not scope.get("conditions"):
        raise ReplicaError("Runtime scope requires kind and conditions")
    if scope["conditions"] != mvp["conditions"]:
        raise ReplicaError("Runtime conditions differ from the defined MVP")
    if scope["kind"] in {"android", "device"} and not scope.get("device"):
        raise ReplicaError("Android observations need explicit device identity")
    observations = _mapping(request.get("observations"), "observations")
    if set(observations) != {"original", "candidate"}:
        raise ReplicaError("Both original and candidate observations are required")
    paired = {key: _observation(store, state, value) for key, value in observations.items()}
    if paired["original"]["path"] == paired["candidate"]["path"]:
        raise ReplicaError("Paired observations must identify distinct original/candidate records")
    results = deepcopy(_mapping(request.get("results"), "goal results"))
    goal_ids = [goal if isinstance(goal, str) else goal["id"] for goal in mvp["goals"]]
    if set(results) != set(goal_ids):
        raise ReplicaError("Each defined observation goal needs a result")
    for value in results.values():
        _mapping(value, "goal result")
        if value.get("result") not in {"pass", "fail", "unknown"}:
            raise ReplicaError("Goal result must be pass, fail or unknown")
        if not value.get("original") or not value.get("candidate"):
            raise ReplicaError("Goal result must reference both paired observations")
        for side in ("original", "candidate"):
            reference = value[side]
            path = reference.get("path") if isinstance(reference, dict) else reference
            if not isinstance(path, str):
                raise ReplicaError("Goal observation reference needs a path or path#location")
            path = path.split("#", 1)[0]
            actual = Path(paired[side]["path"])
            referenced = Path(path)
            if referenced != actual and (store.workspace / referenced).resolve() != actual and path != actual.name:
                raise ReplicaError("Goal reference does not identify its paired observation file")
    tasks = {identifier: state["tasks"][identifier] for identifier in mvp["required_task_ids"]}
    ready = all(_delivery_current(store, state, task) for task in tasks.values())
    result = "fail" if any(value["result"] == "fail" for value in results.values()) else "pass" if ready and all(value["result"] == "pass" for value in results.values()) else "unknown"
    record = {"id": stable_id("observation", request["request_id"]), "versions": versions,
              "runtime_scope": scope, "observations": paired, "results": results, "result": result,
              "task_revisions": {identifier: task["revision"] for identifier, task in tasks.items()},
              "artifacts": [deepcopy(artifact) for task in tasks.values() if task["deliveries"]
                            for artifact in task["deliveries"][-1]["artifacts"]], "recorded_at": _now()}
    mvp["records"].append(record)
    mvp["result"] = result
    return {"mvp": mvp, "record": record, "implementation_ready": ready}


def _mvp_current(store, state, mvp):
    if not mvp["records"]:
        return "unknown"
    record = mvp["records"][-1]
    if any(state["tasks"][identifier]["revision"] != revision for identifier, revision in record["task_revisions"].items()):
        return "stale"
    try:
        for item in record["artifacts"]:
            _artifact(store, state, item)
        for item in record["observations"].values():
            _observation(store, state, item)
    except ReplicaError:
        return "stale"
    if not all(_delivery_current(store, state, state["tasks"][identifier]) for identifier in mvp["required_task_ids"]):
        return "stale"
    return record["result"]


def execute(workspace, request):
    """Apply one idempotent request; a committed receipt survives view failure."""
    request = deepcopy(_mapping(request, "request"))
    op = _text(request.get("op"), "op")
    try:
        fingerprint = hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ReplicaError(f"Request must contain finite JSON values: {exc}") from exc
    store = Store(workspace)
    with store.locked() as stored:
        if op in READS:
            if stored is None:
                raise ReplicaError("Replica workspace is not initialized")
            error = None
            try:
                store.publish(stored)
            except (OSError, ReplicaError) as exc:
                error = str(exc)
            if op == "coverage":
                data = {"coverage": (_coverage_summary if request.get("summary") else _coverage)(store, stored)}
            elif op == "recover":
                data = {"recovered": error is None}
            elif op == "inventory.get":
                data = {"object": _get(stored, "inventory", request.get("object_id"))}
            elif op == "inventory.export":
                data = {"objects": dict(stored["inventory"])}
            else:
                data = {key: deepcopy(value) for key, value in stored.items() if key not in {"receipts", "inventory"}}
                data["counts"] = {key: len(stored[key]) for key in ("inventory", "entries", "classes", "tasks", "runs")}
                data["inventory_counts_by_kind"] = dict(stored["inventory"].manifest["counts_by_kind"])
                if request.get("full") or request.get("summary") is False:
                    data["inventory"] = dict(stored["inventory"])
                for task in data["tasks"].values():
                    task["current_delivery"] = _delivery_current(store, stored, task)
                    task["current_delivery_issues"] = _delivery_issues(store, stored, task, task["deliveries"][-1]) if task["deliveries"] else []
                for mvp in data["mvps"].values():
                    mvp["current_result"] = _mvp_current(store, stored, mvp)
                data["occupied"] = sum(run["state"] != "stopped" for run in stored["runs"].values())
                data["dispatch_policy"] = deepcopy(_dispatch_policy(stored))
                data["ready_task_ids"] = [task["id"] for task in _ready_tasks(store, stored)]
                data["resumable_task_ids"] = [task["id"] for task in stored["tasks"].values()
                                               if task["state"] == "waiting" and not task["run_id"]
                                               and _continuation_prerequisites(store, stored, task)]
                data["wake_task_ids"] = [task["id"] for task in stored["tasks"].values()
                                         if task["state"] in {"delivered", "failed"} and not task["run_id"]
                                         and _pending_messages(stored, task) and _continuation_prerequisites(store, stored, task)]
                data["planning_wake_messages"] = {
                    task["id"]: messages for task in stored["tasks"].values()
                    if task["state"] in {"queued", "waiting", "delivered", "failed"} and not task["run_id"]
                    and (messages := _planning_wake_messages(stored, task))
                    and not _prerequisites(store, stored, task)
                    and _continuation_prerequisites(store, stored, task)}
            return {"ok": error is None, "revision": stored["revision"], **data,
                    **({"projection_error": error} if error else {})}
        request_id = _id(request.get("request_id"), "request_id")
        if stored is not None and request_id in stored["receipts"]:
            record = stored["receipts"][request_id]
            if record["fingerprint"] != fingerprint:
                raise ReplicaError("request_id already used for different content")
            receipt = store.receipt(record)
            response = deepcopy(receipt["response"])
            try:
                store.publish(stored)
            except (OSError, ReplicaError) as exc:
                response["projection_error"] = str(exc)
            return response
        if op == "init":
            if stored is not None:
                raise ReplicaError("Workspace already initialized")
            state = _new_state(request)
            data = {"project": state["project"], "capacity": state["capacity"]}
        else:
            if stored is None:
                raise ReplicaError("Replica workspace is not initialized")
            state = deepcopy(stored)
            data = _dispatch(store, state, request)
            _check_wait_cycles(state)
        state["revision"] += 1
        response = deepcopy({"ok": True, "committed": True, "revision": state["revision"], **data})
        state["receipts"][request_id] = {"fingerprint": fingerprint, "response": response}
        store.commit(state)
        try:
            store.publish(state)
        except (OSError, ReplicaError) as exc:
            return {**response, "projection_error": str(exc)}
        return response
