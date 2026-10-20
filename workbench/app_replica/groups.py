"""Related class tasks can share a native thread, never ownership or acceptance."""

from copy import deepcopy

from .store import ReplicaError


def selection(state, task):
    key = "class.code" if (task["role"], task["mode"]) == ("class", "code") else task["role"]
    if (task["role"], task["mode"]) == ("analysis", "extract"):
        key = "analysis.extract"
    return {field: task.get(field) or state["models"][key][field] for field in ("model", "effort")}


def for_task(state, identifier):
    return next((group for group in state.get("task_groups", {}).values()
                 if identifier in group["task_ids"]), None)


def active(state, group):
    return [run for run in state["runs"].values()
            if run["task_id"] in group["task_ids"] and run["state"] != "stopped"]


def register(store, state, request):
    # Local import keeps the public validators and evidence rules in one place.
    from .engine import _array, _get, _id, _mapping, _requirement_evidence, _text

    identifier = _id(request.get("group_id"), "group id")
    if identifier in state.get("task_groups", {}):
        raise ReplicaError("Task group is immutable; retry the original request_id")
    members = [_id(value, "group task id") for value in _array(request.get("task_ids"), "group task_ids")]
    if not members or len(set(members)) != len(members):
        raise ReplicaError("Task group needs distinct explicit tasks")
    tasks = [_get(state, "tasks", value) for value in members]
    profile, kind = selection(state, tasks[0]), (tasks[0]["role"], tasks[0]["mode"])
    if kind not in {("analysis", "scope"), ("class", "structure"), ("class", "code")}:
        raise ReplicaError("Only class scope, structure or code tasks can share a group")
    for task in tasks:
        if (task["role"], task["mode"]) != kind or selection(state, task) != profile:
            raise ReplicaError("Group must preserve one exact role/model/effort")
        if task["state"] != "queued" or task["run_id"] or task["runs"]:
            raise ReplicaError("Group registration requires unstarted queued tasks")
        if for_task(state, task["id"]):
            raise ReplicaError("Task already belongs to a group")
        obj = _get(state, "inventory", task.get("object_id") or task.get("class_id"))
        if obj["kind"] != "class":
            raise ReplicaError("Group members require their own original class mapping")
    relationships, connected = [], {members[0]}
    for raw in _array(request.get("relationships"), "group relationships"):
        item = _mapping(raw, "group relationship")
        if item.get("from") not in members or item.get("to") not in members or item["from"] == item["to"]:
            raise ReplicaError("Group relationship must join two member tasks")
        evidence = [_requirement_evidence(store, state, value)
                    for value in _array(item.get("evidence"), "relationship evidence")]
        if not evidence:
            raise ReplicaError("Related classes need hashed source evidence")
        relationships.append({"from": item["from"], "to": item["to"],
                              "relation": _text(item.get("relation"), "relationship"), "evidence": evidence})
    while True:
        before = len(connected)
        for item in relationships:
            if connected.intersection((item["from"], item["to"])):
                connected.update((item["from"], item["to"]))
        if len(connected) == before:
            break
    if connected != set(members):
        raise ReplicaError("Every grouped class must have an evidenced relationship to the group")
    group = {"id": identifier, "revision": 1, "task_ids": members,
             "role": kind[0], "mode": kind[1], **profile,
             "reason": _text(request.get("reason"), "group reason"),
             "relationships": relationships, "request_id": request["request_id"]}
    state.setdefault("task_groups", {})[identifier] = group
    # No task, class, writer, prerequisite or delivery is changed here.
    return {"group": group}


def claim_context(store, state, task, backend, profile):
    from .engine import _requirement_evidence

    group = for_task(state, task["id"])
    if not group:
        return {}
    if backend != "native":
        raise ReplicaError("Grouped context reuse currently requires the native backend")
    if active(state, group):
        raise ReplicaError("Previous group worker has not actually stopped")
    if profile != {field: group[field] for field in ("model", "effort")}:
        raise ReplicaError("Group model configuration changed; no silent substitution")
    for item in group["relationships"]:
        for evidence in item["evidence"]:
            _requirement_evidence(store, state, evidence)
    result = {"group_id": group["id"], "group_context": deepcopy(group)}
    prior = [run for run in state["runs"].values()
             if run.get("group_id") == group["id"] and run["state"] == "stopped"
             and run.get("backend") == "native" and run.get("stop_observation", {}).get("writers_stopped")]
    if prior:
        previous = max(prior, key=lambda run: run["stopped_at"])
        handle = previous.get("backend_handle") or {}
        if (handle.get("target") and handle.get("requested") == profile
                and all(previous.get("reported_" + field) in {None, profile[field]} for field in profile)):
            if any(run["state"] != "stopped" and (run.get("backend_handle") or {}).get("target") == handle["target"]
                   for run in state["runs"].values()):
                raise ReplicaError("Group host thread is still active")
            result.update(continuation=deepcopy(handle), group_continuation_from_run=previous["id"])
    return result


def prompt_note(workspace, run):
    if not run.get("group_id"):
        return ""
    return f"""
Related class group: {run['group_id']} (record: {workspace}/coordination/task-groups.json).
This turn owns ONLY the assigned task/run above. Other members retain their own class owners, files,
dependencies and acceptance results. Main may reuse this host thread after your actual confirmed stop;
group membership does not grant another task's outputs or permit concurrent group writers.
Reuse applicable original evidence and the same declaration/source comparison checklist across related
members. Record each original mapping, class revision, consumed input hashes and its actual checks.
A shared read-only evidence reference need not be copied into another report merely because a task starts.
Use one cohesive per-class report/check artifact when sufficient; there is no required artifact count.
Unknown declarations, native behavior or candidate emission remain explicit; templates never imply pass.
Finish this task's public submit/wait and end. Do not self-dispatch, poll for other members, or infer another
member's acceptance from this result. Read the new task and current manifest even on a reused thread.
"""
