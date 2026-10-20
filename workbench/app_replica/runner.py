"""Deterministic dispatch; semantic planning remains an App agent task."""

from contextlib import contextmanager
import fcntl
from pathlib import Path
import json
import time
import uuid

from .backend import BackendError, CapacityError, CodexBackend, HostSlots, build_launch_descriptor
from .engine import execute
from .ids import stable_id
from .roles import task_prompt
from .paths import effective_project
from .store import ReplicaError, Store


def mutation(workspace, op, request_id=None, **fields):
    result = execute(workspace, {"op": op, "request_id": request_id or str(uuid.uuid4()), **fields})
    if not result.get("ok"):
        raise ReplicaError(f"Metadata operation requires recovery: {result}")
    return result


@contextmanager
def dispatch_lock(workspace):
    store = Store(workspace)
    path = store.metadata_path("coordination/dispatcher.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReplicaError("Another dispatcher is active for this workflow") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def seed_app(workspace):
    return mutation(workspace, "task.add", request_id="bootstrap-app", task={
        "id": "app-owner", "role": "app", "mode": "plan",
        "description": "Own the declared App replication from architecture inventory through entry MVPs and residual reconciliation",
        "sources": [str(Path(workspace).resolve() / "project.json")],
        "outputs": ["artifacts/app-owner/report.md"],
    })


def _resume_ready(workspace):
    snapshot = execute(workspace, {"op": "status", "summary": True})
    identifiers = list(dict.fromkeys(snapshot.get("resumable_task_ids", []) + snapshot.get("wake_task_ids", [])))
    for identifier in identifiers:
        task = snapshot["tasks"][identifier]
        mutation(workspace, "task.resume", request_id=f"ready-{identifier}-{task['revision']}",
                 task_id=identifier, reason="Recorded wait conditions or a new actionable message permit continuation",
                 evidence=[{"task_revision": task["revision"], "requires": task["requires"],
                            "message_wake": identifier in snapshot.get("wake_task_ids", []),
                            "planning_wake_message_ids": snapshot.get("planning_wake_messages", {}).get(identifier, [])}])
    return execute(workspace, {"op": "status", "summary": True}) if identifiers else snapshot


class Runner:
    def __init__(self, workspace, *, host_capacity, slots_path=None, executable="codex", backend=None):
        self.workspace = Path(workspace).resolve()
        self.store = Store(workspace)
        self._check_cli_binding()
        snapshot = execute(workspace, {"op": "status", "summary": True})
        project = snapshot["project"]
        execution_root = Path(project.get("execution_root", self.workspace)).resolve()
        # All file writes by workers must fit the configured backend sandbox.
        if not self.workspace.is_relative_to(execution_root):
            raise ReplicaError("Workflow workspace must be within project.execution_root")
        self.backend = backend or CodexBackend(execution_root, self.workspace / "coordination/backend",
                                              HostSlots(slots_path or HostSlots.default_path(), host_capacity),
                                              executable=executable)

    def _check_cli_binding(self):
        with self.store.locked() as state:
            if state and state.get("schema") == 3:
                raise ReplicaError("Relocated CLI Runner/step/run is unsupported; use native-next")

    def _launch(self, task, run, project):
        relative = f"coordination/launches/{run['id']}.json"
        path = self.store.metadata_path(relative)
        if path.exists():
            launch = json.loads(path.read_text())
        else:
            launch = {"run_id": run["id"], "task_id": task["id"],
                      "prompt": task_prompt(self.workspace, project, task, run),
                      "model": run["model"], "effort": run["effort"],
                      "thread_id": (run.get("continuation") if isinstance(run.get("continuation"), dict) else {}).get("thread_id")}
            self.store.write_json(relative, launch)
        return self.backend.start(run["id"], launch["prompt"], launch["model"], launch["effort"],
                                  thread_id=launch["thread_id"])

    def _observe(self, run, observation):
        identifier = run["id"]
        reported = observation.get("reported", {})
        # A backend handle contains paths/process evidence and the exact thread.
        if run["state"] == "reserved" and not observation.get("confirmed_launch_failed"):
            if observation.get("worker_started") or observation.get("confirmed_stopped"):
                result = mutation(self.workspace, "run.started", request_id=f"started-{identifier}",
                                  run_id=identifier, backend_handle=observation,
                                  requested_model=run["model"], requested_effort=run["effort"],
                                  reported_model=reported.get("model"), reported_effort=reported.get("effort"))
                run = result["run"]
        if observation.get("selection_error") or observation.get("thread_evidence", {}).get("error"):
            if not observation.get("confirmed_stopped"):
                self.backend.stop(identifier)
        if not observation.get("confirmed_stopped"):
            return
        snapshot = execute(self.workspace, {"op": "status", "summary": True})
        run = snapshot["runs"][identifier]
        if run["state"] == "stopped":
            return
        task = snapshot["tasks"][run["task_id"]]
        good_exit = observation.get("exit_code") == 0 and not observation.get("selection_error") and not observation.get("thread_evidence", {}).get("error")
        outcome = "failed"
        if observation.get("confirmed_launch_failed") and run["state"] == "reserved":
            outcome = "launch_failed"
        elif good_exit and task["state"] in {"delivered", "waiting"}:
            outcome = task["state"]
        result = mutation(self.workspace, "run.stopped", request_id=f"stopped-{identifier}",
                          run_id=identifier, outcome=outcome, backend_handle=observation,
                          reported_model=reported.get("model"), reported_effort=reported.get("effort"),
                          observation={"confirmed": True, "writers_stopped": True,
                                       "evidence": observation})
        mutation(self.workspace, "message.send", request_id=f"event-{identifier}",
                 message_id=f"event-{identifier}", type="task_stopped", sender="runner",
                 recipient="app-owner", task_id=task["id"],
                 payload={"outcome": outcome, "task_state": result["task"]["state"], "run_id": identifier},
                 evidence=[observation.get("paths", {})])

    def step(self):
        self._check_cli_binding()
        events, blocked = [], []
        with dispatch_lock(self.workspace):
            self._check_cli_binding()
            snapshot = execute(self.workspace, {"op": "status", "summary": True})
            # Reconcile durable launch intents before considering new work.
            for run in snapshot["runs"].values():
                if run["state"] == "stopped" or run.get("backend") != "codex":
                    continue
                try:
                    observation = self._launch(snapshot["tasks"][run["task_id"]], run, snapshot["project"]) if run["state"] == "reserved" else self.backend.poll(run["id"])
                except CapacityError as exc:
                    blocked.append({"run_id": run["id"], "reason": str(exc)})
                    continue
                self._observe(run, observation)
                events.append({"run_id": run["id"], "state": observation["state"]})
            available = _resume_ready(self.workspace)
            while True:
                if not available["ready_task_ids"] or available["occupied"] >= available["capacity"]:
                    break
                result = mutation(self.workspace, "task.claim", backend="codex", task_id=available["ready_task_ids"][0])
                if not result.get("task"):
                    break
                run, task = result["run"], result["task"]
                try:
                    observation = self._launch(task, run, snapshot["project"])
                except CapacityError as exc:
                    blocked.append({"run_id": run["id"], "reason": str(exc)})
                    break
                self._observe(run, observation)
                events.append({"run_id": run["id"], "task_id": task["id"], "state": observation["state"]})
                available = execute(self.workspace, {"op": "status", "summary": True})
            final = execute(self.workspace, {"op": "status", "summary": True})
            active = [run["id"] for run in final["runs"].values() if run["state"] != "stopped"]
            unknown = [item for item in events if item["state"] == "unknown"]
            mismatched_capacity = any("differs" in item["reason"] for item in blocked)
            state = ("attention_required" if unknown or mismatched_capacity else
                     "waiting_capacity" if blocked else "running" if active else "idle")
            return {"ok": True, "state": state, "active_run_ids": active, "events": events,
                    "blocked": blocked, "ready_task_ids": final["ready_task_ids"],
                    "task_states": {key: value["state"] for key, value in final["tasks"].items()},
                    **({"git": final["git"]} if "git" in final else {}),
                    "coverage": execute(self.workspace, {"op": "coverage", "summary": True})}

    def run(self, interval=1.0):
        if interval <= 0 or interval > 60:
            raise ReplicaError("Polling interval must be > 0 and <= 60 seconds")
        while True:
            result = self.step()
            yield result
            if result["state"] not in {"running", "waiting_capacity"}:
                return
            time.sleep(interval)


def native_next(workspace, *, external_active, task_id=None, stopped=None):
    """Record an observed stop, optionally, then reserve one host action."""
    with dispatch_lock(workspace):
        if type(external_active) is not int or external_active < 0:
            raise ReplicaError("external_active must be a nonnegative integer")
        stop_result = {}
        if stopped is not None:
            if not isinstance(stopped, dict) or stopped.get("op") != "run.stopped":
                raise ReplicaError("stopped must be one run.stopped request")
            with Store(workspace).locked() as state:
                run = state["runs"].get(stopped.get("run_id")) if state else None
                if not run or run.get("backend") != "native":
                    raise ReplicaError("native-next can only record a native run stop")
            request = dict(stopped)
            request.setdefault("request_id", stable_id("request", request))
            # Use the normal stop path, including Git saving, validation,
            # followup settlement and the durable idempotent receipt.
            stop_result["stopped"] = mutation(workspace, **request)
        snapshot = _resume_ready(workspace)
        # Recovery reoffers the existing reservation, not a second launch.
        pending = [run for run in snapshot["runs"].values()
                   if run["state"] == "reserved" and run.get("backend") == "native"]
        if pending:
            run = pending[0]
            task = snapshot["tasks"][run["task_id"]]
            recovery = True
        else:
            result = mutation(workspace, "task.claim", backend="native", external_active=external_active,
                              **({"task_id": task_id} if task_id else {}))
            if not result.get("task"):
                return {**result, **stop_result}
            run, task, recovery = result["run"], result["task"], False
        prior = run.get("continuation") if isinstance(run.get("continuation"), dict) else {}
        reuse = prior.get("target") and prior.get("requested") == {"model": run["model"], "effort": run["effort"]}
        directive = build_launch_descriptor("followup" if reuse else "spawn",
                                             Path(workspace) / "tasks" / task["id"] / "task.json",
                                             run["model"], run["effort"], target=prior.get("target") if reuse else None,
                                             task_name="replica_" + run["id"].replace("-", "_"),
                                             message=task_prompt(workspace, effective_project(workspace, snapshot), task, run))
        return {"ok": True, "task_id": task["id"], "run_id": run["id"], "directive": directive,
                **stop_result,
                "recovery": recovery,
                "next": "If recovery=true, reconcile actual host launch before repeating. Execute the host directive, then record run.started with the actual handle. Record run.stopped only after the host confirms it and any writer children stopped."}
