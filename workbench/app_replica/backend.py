"""Tool-enabled Codex execution with durable, host-wide process reservations.

Linux is required for CLI execution. A small subreaper supervises the CLI and
its descendants; a CLI exit alone does not establish that its writers stopped.
Native descriptors are instructions for the host, never claimed tool execution.
"""

from __future__ import annotations

import contextlib
import ctypes
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid


class BackendError(ValueError):
    pass


class CapacityError(BackendError):
    pass


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def process_identity(pid):
    """Observe Linux boot/start time as well as PID; return None only if absent."""
    try:
        proc = Path("/proc") / str(pid)
        raw = (proc / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return {
            "pid": int(pid), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": int(fields[19]), "uid": proc.stat().st_uid,
            "ppid": int(fields[1]), "state": fields[0], "pgid": int(fields[2]),
            "session": int(fields[3]),
        }
    except FileNotFoundError:
        return None


def _same_process(expected, observed):
    keys = ("pid", "boot_id", "start_ticks", "uid")
    return bool(expected and observed and all(expected.get(k) == observed.get(k) for k in keys))


def _presence(identity):
    if not identity:
        return "unknown"
    try:
        observed = process_identity(identity["pid"])
    except (OSError, ValueError, KeyError):
        return "unknown"
    if not _same_process(identity, observed) or observed["state"] == "Z":
        return "absent"
    return "present"


def _release_proof(reservation):
    """Re-read supervisor evidence rather than trusting caller's stopped flag."""
    metadata = reservation.get("metadata", {})
    try:
        record = _read(metadata["record_path"])
        proof = _read(record["paths"]["completion"])
        if proof.get("launch_token") != metadata.get("launch_token"):
            return None
        identity = proof.get("supervisor")
        if proof.get("kind") == "launcher_failure":
            # Popen failed before a supervisor existed; recorded under run lock.
            return proof if record.get("state") == "launch_failed" and not identity else None
        if proof.get("kind") != "tree_reaped" or not proof.get("subreaper"):
            return None
        if not identity or _presence(identity) != "absent":
            return None
        return proof
    except (OSError, ValueError, KeyError, TypeError):
        return None


class HostSlots:
    """One explicit allowance shared by every workflow using this host file.

    Reservations never expire. Reconciliation requires real launch/tree proof.
    Keep this file outside any one project so separate workflows share it.
    """

    @staticmethod
    def default_path():
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        return base / "android-workbench/app-replica/host-slots.json"

    def __init__(self, path, capacity):
        if type(capacity) is not int or capacity < 1:
            raise BackendError("host capacity must be an explicit positive integer")
        self.path = Path(path).resolve()
        self.capacity = capacity
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _state(self):
        if not self.path.exists():
            return {"version": 1, "capacity": self.capacity, "reservations": {}}
        state = _read(self.path)
        if state.get("capacity") != self.capacity:
            raise CapacityError("host capacity differs from the shared host allowance")
        return state

    def reserve(self, run_id, workflow, metadata=None):
        with _lock(self.lock_path):
            state = self._state()
            existing = state["reservations"].get(run_id)
            if existing:
                if existing["workflow"] != str(workflow) or existing["metadata"] != (metadata or {}):
                    raise BackendError("run ID is already reserved with different launch data")
                return existing
            if sum(r["state"] == "occupied" for r in state["reservations"].values()) >= self.capacity:
                raise CapacityError("host execution allowance is occupied")
            record = {"run_id": run_id, "workflow": str(workflow), "metadata": metadata or {},
                      "state": "occupied", "reserved_at": time.time()}
            state["reservations"][run_id] = record
            _write(self.path, state)
            return record

    def get(self, run_id=None):
        with _lock(self.lock_path):
            state = self._state()
            return state if run_id is None else state["reservations"].get(run_id)

    def release(self, run_id, evidence=None):
        with _lock(self.lock_path):
            state = self._state()
            record = state["reservations"].get(run_id)
            if record is None:
                raise BackendError("unknown host reservation")
            if record["state"] == "released":
                return record
            proof = _release_proof(record)
            if proof is None:
                raise BackendError("no confirmed launch failure or stopped descendant tree; slot retained")
            record.update(state="released", released_at=time.time(), stop_evidence=proof)
            if evidence:
                record["observation"] = evidence
            _write(self.path, state)
            return record


def build_launch_descriptor(operation, task_file, model, effort, *, target=None,
                            message=None, task_name=None):
    """Return real host-tool arguments, without claiming the host called them."""
    path = Path(task_file).resolve()
    if not path.is_file():
        raise BackendError("native task file must exist")
    _validate_selection(model, effort)
    text = message or f"Read the assigned task and shared protocol at {path}. Submit discoveries to the shared queue."
    if str(path) not in text:
        text += f"\nTask record: {path}"
    if operation in ("spawn", "spawn_agent"):
        name = task_name or re.sub("[^a-z0-9_]", "_", path.parent.name.lower())
        if not name:
            raise BackendError("native spawn needs a task name")
        arguments = {"task_name": name, "fork_turns": "none", "model": model,
                     "reasoning_effort": effort, "message": text}
        tool = "spawn_agent"
    elif operation in ("followup", "followup_task", "message", "send_message"):
        if not target:
            raise BackendError("native continuation/notification needs the actual host target")
        tool = "followup_task" if operation in ("followup", "followup_task") else "send_message"
        arguments = {"target": target, "message": text}
    else:
        raise BackendError("unknown native host operation")
    return {"backend": "native", "tool": tool, "arguments": arguments,
            "task_file": str(path), "requested": {"model": model, "effort": effort},
            "reported": {"model": None, "effort": None}, "execution": "host_action_required",
            "limitations": (["Continuation tools cannot change model/effort; host must verify target configuration."]
                            if tool != "spawn_agent" else [])}


def _validate_selection(model, effort):
    if not isinstance(model, str) or not model.strip() or not isinstance(effort, str) or not effort.strip():
        raise BackendError("model and reasoning effort must be explicit")
    if not re.fullmatch(r"[a-z0-9_.-]+", model) or effort not in ("none", "low", "medium", "high", "xhigh", "max", "ultra"):
        raise BackendError("invalid explicit model or reasoning effort")


def _session_metadata(thread_id, created_at):
    """Read backend-written turn context, never model-written message claims."""
    if not thread_id:
        return None
    try:
        uuid.UUID(thread_id)
    except (ValueError, TypeError, AttributeError):
        return None
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if not root.is_dir():
        return None
    paths = list(root.rglob(f"*-{thread_id}.jsonl"))
    if len(paths) != 1:
        return None
    matched_session, context = False, None
    try:
        with paths[0].open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    if event.get("type") == "session_meta":
                        matched_session = payload.get("id") == thread_id
                    elif event.get("type") == "turn_context" and matched_session:
                        stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                        if stamp >= created_at:
                            context = {"path": str(paths[0]), "line": number,
                                       "model": payload.get("model"), "effort": payload.get("effort")}
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except OSError:
        return None
    return context


class CodexBackend:
    def __init__(self, workspace, records_dir, slots, executable="codex", sandbox="workspace-write"):
        self.workspace = Path(workspace).resolve()
        self.records_dir = Path(records_dir).resolve()
        self.slots = slots
        self.executable = str(executable)
        if sandbox not in ("read-only", "workspace-write"):
            raise BackendError("CLI backend requires read-only or workspace-write sandbox")
        self.sandbox = sandbox
        self._children = {}

    def _directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id) or run_id in (".", ".."):
            raise BackendError("invalid run ID")
        return self.records_dir / run_id

    def start(self, run_id, prompt, model, effort, thread_id=None):
        _validate_selection(model, effort)
        if not sys.platform.startswith("linux"):
            raise BackendError("CLI descendant supervision requires Linux /proc and subreaper support")
        if not self.workspace.is_dir() or not isinstance(prompt, str):
            raise BackendError("workspace must exist and prompt must be text")
        if thread_id is not None:
            try:
                uuid.UUID(thread_id)
            except (ValueError, TypeError, AttributeError) as exc:
                raise BackendError("resume requires the exact recorded thread UUID") from exc
        directory = self._directory(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        record_path = directory / "run.json"
        selection = {"model": model, "effort": effort}
        request = {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                   "requested": selection, "thread_id": thread_id, "workspace": str(self.workspace),
                   "executable": self.executable, "sandbox": self.sandbox}
        with _lock(directory / "launch.lock"):
            if record_path.exists():
                record = _read(record_path)
                if record["request"] != request:
                    raise BackendError("run ID reused for a different execution")
                return self.poll(run_id)
            token = uuid.uuid4().hex
            paths = {"record": str(record_path), "prompt": str(directory / "prompt.txt"),
                     "events": str(directory / "events.jsonl"), "stderr": str(directory / "stderr.log"),
                     "last_message": str(directory / "last-message.txt"),
                     "completion": str(directory / "completion.json"),
                     "supervisor": str(directory / "supervisor.json"),
                     "supervisor_log": str(directory / "supervisor.log")}
            self.slots.reserve(run_id, self.workspace, {"record_path": str(record_path), "launch_token": token})
            command = [shutil.which(self.executable) or self.executable, "exec"]
            if thread_id:
                command += ["resume"]
            command += ["--json", "--skip-git-repo-check", "--model", model, "-c", f'model_reasoning_effort="{effort}"',
                        "-c", 'approval_policy="never"', "-c", f'sandbox_mode="{self.sandbox}"',
                        "--disable", "multi_agent", "--output-last-message", paths["last_message"]]
            if thread_id:
                command += [thread_id]
            command += ["-"]
            Path(paths["prompt"]).write_text(prompt, encoding="utf-8")
            record = {"version": 1, "run_id": run_id, "request": request, "launch_token": token,
                      "paths": paths, "command": command, "state": "launch_intent",
                      "created_at": time.time(), "process": None}
            _write(record_path, record)
            environment = dict(os.environ)
            environment["AWB_APP_REPLICA_LAUNCH"] = token
            try:
                with Path(paths["supervisor_log"]).open("ab") as log:
                    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--supervise", str(record_path)],
                                             cwd=self.workspace, env=environment, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=log, start_new_session=True)
            except OSError as exc:
                record.update(state="launch_failed", launch_error=str(exc))
                _write(record_path, record)
                _write(paths["completion"], {"kind": "launcher_failure", "launch_token": token,
                                             "error": str(exc), "observed_at": time.time()})
            else:
                self._children[run_id] = child
                # Failure to acknowledge an already launched process must never
                # be mistaken for failure to launch. Its reservation stays held.
                record.update(state="launched", process=process_identity(child.pid), launched_at=time.time())
                _write(record_path, record)
        return self.poll(run_id)

    def resume(self, run_id, thread_id, prompt, model, effort):
        return self.start(run_id, prompt, model, effort, thread_id=thread_id)

    def poll(self, run_id):
        record = _read(self._directory(run_id) / "run.json")
        child = self._children.get(run_id)
        if child is not None:
            child.poll()  # Reap our supervisor if it exited.
        process = record.get("process")
        supervisor = None
        try:
            supervisor = _read(record["paths"]["supervisor"])
            if supervisor.get("launch_token") == record["launch_token"]:
                process = supervisor["process"]
            else:
                supervisor = None
        except FileNotFoundError:
            pass
        events, malformed = [], 0
        event_path = Path(record["paths"]["events"])
        if event_path.exists():
            with event_path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        malformed += 1
        thread_ids = list(dict.fromkeys(e.get("thread_id") for e in events
                                       if isinstance(e, dict) and e.get("type") == "thread.started" and e.get("thread_id")))
        expected_thread = record["request"].get("thread_id")
        thread_id = thread_ids[0] if len(thread_ids) == 1 else expected_thread
        thread_error = None
        if len(thread_ids) > 1 or (expected_thread and thread_ids and thread_ids != [expected_thread]):
            thread_error = "CLI reported unexpected thread identity"
        context = _session_metadata(thread_id, record["created_at"]) if not thread_error else None
        reported = {"model": context.get("model") if context else None,
                    "effort": context.get("effort") if context else None}
        selection_error = None
        if any(value is not None and value != record["request"]["requested"][key]
               for key, value in reported.items()):
            selection_error = "Backend-reported selection differs from the requested model/effort"
        reservation = self.slots.get(run_id)
        proof = _release_proof(reservation) if reservation else None
        state = "stopped" if proof else ("running" if _presence(process) == "present" else "unknown")
        result = {"backend": "codex", "run_id": run_id, "state": state, "confirmed_stopped": bool(proof),
                  "worker_started": bool(supervisor and supervisor.get("child_launched")),
                  "thread_id": thread_id, "thread_evidence": {"reported_ids": thread_ids, "error": thread_error},
                  "requested": record["request"]["requested"], "reported": reported,
                  "selection_error": selection_error,
                  "reported_evidence": context,
                  "paths": record["paths"], "process": process, "supervisor_evidence": supervisor,
                  "completion": proof, "exit_code": proof.get("exit_code") if proof else None,
                  "confirmed_launch_failed": bool(proof and (proof.get("launch_error") or proof.get("kind") == "launcher_failure")),
                  "launch_error": (proof.get("launch_error") or proof.get("error")) if proof else None,
                  "command": record["command"],
                  "event_count": len(events), "malformed_event_lines": malformed,
                  "turn_completed": any(isinstance(e, dict) and e.get("type") == "turn.completed" for e in events),
                  "limitations": ([] if context else ["No backend-written turn context was found; model and effort remain unknown."])}
        if proof:
            self.slots.release(run_id, {"confirmed_stopped": True, "paths": record["paths"]})
        return result

    def stop(self, run_id, force=False):
        observation = self.poll(run_id)
        if observation["confirmed_stopped"]:
            return observation
        identity = observation["process"]
        if _presence(identity) != "present":
            return observation
        # The supervisor remains alive to reap every writer. Never kill it first.
        request_path = self._directory(run_id) / "stop.json"
        _write(request_path, {"launch_token": _read(self._directory(run_id) / "run.json")["launch_token"],
                              "signal": signal.SIGKILL if force else signal.SIGTERM,
                              "requested_at": time.time()})
        return self.poll(run_id)

    def reconcile(self, run_id):
        return self.poll(run_id)


def _descendants(root_pid):
    observed = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                identity = process_identity(int(path.name))
                if identity:
                    observed[identity["pid"]] = identity
            except (OSError, ValueError):
                continue
    descendants, parents = {}, {root_pid}
    while True:
        additions = {pid: value for pid, value in observed.items()
                     if pid not in parents and value["ppid"] in parents}
        if not additions:
            return descendants
        descendants.update(additions)
        parents.update(additions)


def _supervise(record_path):
    record = _read(record_path)
    paths, token = record["paths"], record["launch_token"]
    identity = process_identity(os.getpid())
    base = {"launch_token": token, "supervisor": identity, "observed_at": time.time()}
    # PR_SET_CHILD_SUBREAPER makes orphaned descendants our children, including
    # writers that change their process group/session or clear their environment.
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot establish descendant subreaper")
    _write(paths["supervisor"], {"launch_token": token, "process": identity, "subreaper": True})
    interrupted = []
    signal.signal(signal.SIGTERM, lambda signum, frame: interrupted.append(signum))
    signal.signal(signal.SIGINT, lambda signum, frame: interrupted.append(signum))
    try:
        with Path(paths["prompt"]).open("rb") as prompt, Path(paths["events"]).open("ab") as events, Path(paths["stderr"]).open("ab") as errors:
            child = subprocess.Popen(record["command"], cwd=record["request"]["workspace"],
                                     stdin=prompt, stdout=events, stderr=errors)
    except OSError as exc:
        _write(paths["completion"], {**base, "kind": "tree_reaped", "subreaper": True,
                                     "exit_code": None, "launch_error": str(exc), "reaped": []})
        return
    root_pid = child.pid
    try:
        actual_executable = str((Path("/proc") / str(root_pid) / "exe").resolve(strict=True))
    except OSError:
        actual_executable = None
    _write(paths["supervisor"], {"launch_token": token, "process": identity, "subreaper": True,
                                 "child_launched": True,
                                 "child_process": process_identity(root_pid),
                                 "child_executable": actual_executable, "command": record["command"]})
    root_exit, reaped, identities = None, [], {}
    while True:
        identities.update(_descendants(os.getpid()))
        stop_signal = interrupted[-1] if interrupted else None
        stop_path = Path(record_path).parent / "stop.json"
        if stop_path.exists():
            try:
                stop = _read(stop_path)
                if stop.get("launch_token") == token and stop.get("signal") in (signal.SIGTERM, signal.SIGKILL):
                    stop_signal = stop["signal"]
            except (OSError, ValueError):
                pass
        if stop_signal:
            for target in identities.values():
                if _presence(target) == "present":
                    try:
                        os.kill(target["pid"], stop_signal)
                    except ProcessLookupError:
                        pass
        no_children = False
        while True:
            try:
                pid, wait_status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                no_children = True
                break
            if pid == 0:
                break
            exit_code = os.waitstatus_to_exitcode(wait_status)
            reaped.append({"pid": pid, "identity": identities.get(pid), "exit_code": exit_code})
            if pid == root_pid:
                root_exit = exit_code
                child.returncode = exit_code
        if no_children:
            _write(paths["completion"], {**base, "kind": "tree_reaped", "subreaper": True,
                                         "exit_code": root_exit, "reaped": reaped,
                                         "stop_signal": stop_signal, "finished_at": time.time()})
            return
        time.sleep(0.05)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise":
        _supervise(sys.argv[2])
    else:
        raise SystemExit("internal supervisor entry only")
