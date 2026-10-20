"""Verified launch snapshots; these never replace current output ownership."""

import hashlib
import json
import re

from .paths import PathBindings, read_regular
from .snapshot import encoded
from .store import ReplicaError, Store


NAMESPACE = "prompt-manifests"


def _index_path(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,249}", run_id):
        raise ReplicaError("Invalid prompt manifest run ID")
    return f"coordination/{NAMESPACE}/runs/{run_id}.json"


def _read_index(store, task_id, run_id):
    path = store.metadata_path(_index_path(run_id))
    try:
        index = json.loads(read_regular(path))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        raise ReplicaError(f"Cannot read prompt manifest index: {path}: {exc}") from exc
    if (not isinstance(index, dict) or type(index.get("format")) is not int or index["format"] != 1
            or index.get("task_id") != task_id or index.get("run_id") != run_id
            or not isinstance(index.get("references"), list) or not index["references"]):
        raise ReplicaError("Invalid prompt manifest index")
    manifest = None
    for reference in index["references"]:
        if not isinstance(reference, dict) or type(reference.get("version")) is not int or reference["version"] != 1:
            raise ReplicaError("Unsupported prompt manifest reference")
        manifest = store.read_blob(reference, NAMESPACE)
        if (not isinstance(manifest, dict) or type(manifest.get("format")) is not int or manifest["format"] != 1
                or manifest.get("task_id") != task_id or manifest.get("run_id") != run_id
                or type(manifest.get("task_revision")) is not int
                or type(manifest.get("binding_revision")) is not int
                or not isinstance(manifest.get("outputs"), list)
                or any(not isinstance(item, dict) or not isinstance(item.get("raw"), str)
                       or not isinstance(item.get("effective"), str) for item in manifest["outputs"])):
            raise ReplicaError("Invalid prompt manifest contents")
        raw = [item["raw"] for item in manifest["outputs"]]
        if len(raw) != len(set(raw)):
            raise ReplicaError("Duplicate prompt manifest output identity")
    return index["references"], manifest


def output_prompt(workspace, project, task, run):
    """Publish the full current list and expand only initial, added or focused paths.

    A per-run index retains every immutable snapshot generated for that run.
    Reoffering a reservation may have a newer task or path binding; its paths
    must be resolved again. A preceding run without an index is a legacy
    record, never evidence that all of today's outputs are newly assigned.
    """
    store = Store(workspace)
    run_ids = task.get("runs", [])
    if run_ids.count(run["id"]) != 1:
        raise ReplicaError("Prompt run must occur exactly once in task history")
    position = run_ids.index(run["id"])
    prior_id = run_ids[position - 1] if position else None
    references, _ = _read_index(store, task["id"], run["id"])
    prior_references, prior = _read_index(store, task["id"], prior_id) if prior_id else ([], None)
    binding = project.get("_path_binding", {})
    resolver = PathBindings(workspace, binding.get("bindings", ()), binding.get("revision", 0))
    raw_outputs = task["outputs"]
    if (not isinstance(raw_outputs, list) or any(not isinstance(value, str) or not value for value in raw_outputs)
            or len(raw_outputs) != len(set(raw_outputs))):
        raise ReplicaError("Invalid prompt output identities")
    focus = run.get("focus_outputs", [])
    if (not isinstance(focus, list) or any(not isinstance(value, str) or value not in raw_outputs for value in focus)
            or len(focus) != len(set(focus))):
        raise ReplicaError("Prompt focus_outputs must be exact assigned output identities")
    outputs = [{"raw": value, "effective": str(resolver.path(value))} for value in raw_outputs]
    prior_raw = {item["raw"] for item in prior["outputs"]} if prior else set()
    added = [value for value in raw_outputs if value not in prior_raw] if prior else []
    removed = [item["raw"] for item in prior["outputs"] if item["raw"] not in raw_outputs] if prior else []
    status = "verified" if prior else ("unavailable" if prior_id else "initial_run")
    manifest = {"format": 1, "task_id": task["id"], "task_revision": task["revision"],
                "run_id": run["id"], "run_revision": run.get("revision"),
                "run_record_sha256": hashlib.sha256(encoded(run)).hexdigest(),
                "binding_revision": resolver.revision, "outputs": outputs, "focus_outputs": focus,
                "baseline_status": status, "baseline_run_id": prior_id,
                "baseline_reference": prior_references[-1] if prior_references else None,
                "added_outputs": added, "removed_outputs": removed}
    reference = {**store.write_blob(NAMESPACE, manifest), "version": 1}
    # Verify the selected immutable bytes, including on the first publication.
    store.read_blob(reference, NAMESPACE)
    if not references or references[-1] != reference:
        store.write_json(_index_path(run["id"]), {"format": 1, "task_id": task["id"],
                         "run_id": run["id"], "references": [*references, reference]})
    selected = set(raw_outputs if not prior_id else [*focus, *added])
    expanded = [item["effective"] for item in outputs if item["raw"] in selected]
    if status == "verified":
        baseline_note = (f"Verified delta baseline: run {prior_id}; "
                         f"reference {json.dumps(prior_references[-1], ensure_ascii=False)}. "
                         f"Added identities: {len(added)}; removed identities: {len(removed)}. "
                         f"Baseline binding revision: {prior['binding_revision']}; current: {resolver.revision}.")
    elif prior_id:
        baseline_note = f"No verified delta baseline: prior run {prior_id} has no prompt manifest. No historical output is classified as new."
    else:
        baseline_note = "Initial task run: all assigned outputs are expanded below."
    label = "Effective assigned outputs (initial run)" if not prior_id else "Current focus/new assigned outputs"
    return f"""Full assigned-output manifest (relative to the current workflow workspace): {json.dumps(reference, ensure_ascii=False)}
Manifest verified before launch: version 1, task revision {task['revision']}, binding revision {resolver.revision}, {len(outputs)} outputs. It contains every raw identity and its current effective path.
When reading a manifest, verify its byte count and SHA-256 against its reference; reject a mismatch. Manifest blobs and run indexes are program-generated metadata; do not edit them.
{baseline_note}
{label}: {json.dumps(expanded, ensure_ascii=False)}
This launch snapshot and the inline selection do not narrow or expand permissions. Current task/state ownership and the current path resolver remain authoritative, including paths reserved later by task.add. Read the full verified manifest whenever needed; do not assume a resumed or new thread remembers any earlier list. Compare raw identities and resolve them with the current binding; old effective paths are historical only.
Read the current checkpoint in tasks/{task['id']}/task.json, relevant delivery/history records, and this run's assigned messages before continuing; inbox assignment alone does not prove handling.
"""
