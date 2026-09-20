from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid

from . import PROTOCOL, __version__
from .common import (
    MAX_MESSAGE,
    TERMINAL,
    WorkbenchError,
    atomic_json,
    conflict,
    digest,
    encode,
    lock_file,
    overlap,
    process_identity,
    same_process,
)
from .policy import Policy
from .registry import normalize
from .store import Store

DEFAULTS = {
    "devices": {},
    "llm": {"backend": "none"},
    "max_queued": 500,
    "max_session_queued": 100,
    "max_workers": 8,
    "max_compute": 2,
    "fair_after": 120,
    "candidate_limit": 16,
    "min_free_bytes": 100 * 1024 * 1024,
    "max_job_log_bytes": 32 * 1024 * 1024,
    "cleanup_grace": 20,
}
ACTIVE = {"running", "paused", "verifying", "cleaning", "needs_recovery"}


class Service:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.instance_lock = lock_file(self.directory / "service.lock")
        self.config = {
            **DEFAULTS,
            **json.loads((self.directory / "config.json").read_text()),
        }
        self.store = Store(self.directory / "queue.sqlite3")
        self.lock = threading.RLock()
        self.policy = Policy(self.config)
        self.generation = uuid.uuid4().hex
        self.stopping = threading.Event()
        self.draining = False
        self.selecting = set()
        self.procs = {}
        self.stop_deadlines = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="decision"
        )
        self.contexts = {}
        with self.lock:
            for job in self.store.jobs():
                if job["state"] in ACTIVE:
                    self.store.update(
                        job["id"],
                        state="needs_recovery",
                        reason="service_restarted_outcome_unconfirmed",
                    )
                    self.store.event(
                        job["id"], "recovery_required", {"reason": "service_restart"}
                    )
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def public(self, job):
        return {
            k: v
            for k, v in job.items()
            if k not in ("owner", "request_hash", "generation", "worker", "spec")
        } | {
            "operation": job["spec"]["operation"],
            "device": job["spec"].get("device"),
            "purpose": job["spec"].get("purpose"),
            "resources": job["spec"]["resources"],
            "directory": job["spec"]["directory"],
            "timeout": job["spec"]["timeout"],
        }

    def authenticate(self, token):
        if not isinstance(token, str):
            raise WorkbenchError("Session token required")
        rows = self.store.rows("SELECT * FROM sessions WHERE token=?", (digest(token),))
        if not rows:
            raise WorkbenchError("Invalid session identity")
        return rows[0]

    def own(self, ident, session):
        job = self.store.job(ident)
        if job["owner"] != session["token"]:
            raise WorkbenchError("Task belongs to a different session")
        return job

    def shared(self, ident, session):
        job = self.store.job(ident)
        if job["project"] != session["project"]:
            raise WorkbenchError("Task belongs to a different project")
        return job

    def request(self, method, p, token):
        if method.startswith("worker."):
            return self.worker_request(method, p, token)
        with self.lock:
            if method == "service.capabilities":
                return {
                    "protocol": PROTOCOL,
                    "version": __version__,
                    "pid": os.getpid(),
                    "state_directory": str(self.directory),
                    "llm": self.config.get("llm", {}).get("backend", "none"),
                    "llm_configured": self.config.get("llm", {}).get("backend", "none")
                    != "none",
                    "draining": self.draining,
                    "insertion_depth": 1,
                    "limits": {
                        k: self.config[k]
                        for k in DEFAULTS
                        if k not in ("devices", "llm")
                    },
                    "managed_boundary": "same Linux account; registered operations only",
                    "runtime_source": str(Path(__file__).resolve().parents[1]),
                }
            if method == "sessions.open":
                project = Path(p["project"]).resolve()
                if not (project / "workbench.project.json").is_file():
                    raise WorkbenchError("Unregistered project")
                new_token = secrets.token_urlsafe(32)
                self.store.db.execute(
                    "INSERT INTO sessions VALUES(?,?,?)",
                    (
                        digest(new_token),
                        str(p.get("name", "session"))[:100],
                        str(project),
                    ),
                )
                return {"token": new_token, "project": str(project)}
            session = self.authenticate(token)
            if method in ("service.drain", "service.resume"):
                self.draining = method == "service.drain"
                active = [
                    j["id"]
                    for j in self.store.jobs()
                    if j["state"] in ACTIVE or j["state"] == "queued"
                ]
                return {
                    "draining": self.draining,
                    "active": active,
                    "can_stop": not active,
                }
            if method == "operations.list":
                manifest = json.loads(
                    (Path(session["project"]) / "workbench.project.json").read_text()
                )
                return {
                    "builtin": [
                        "device.scene",
                        "device.screenshot",
                        "device.observe",
                        "device.recover",
                    ],
                    "operations": manifest.get("operations", {}),
                }
            if method == "jobs.submit" or method == "scenes.request_observation":
                if method == "scenes.request_observation":
                    p = {**p, "operation": "device.screenshot"}
                return self.submit(p, session)
            if method in ("jobs.status", "jobs.explain"):
                job = self.shared(p["id"], session)
                value = self.public(job)
                if method == "jobs.explain":
                    rows = self.store.rows(
                        "SELECT * FROM events WHERE job=? AND kind IN ('decision','waiting','inserted') ORDER BY seq DESC LIMIT 12",
                        (job["id"],),
                    )
                    value["decisions"] = [
                        {**r, "data": json.loads(r["data"])} for r in rows
                    ]
                return value
            if method == "jobs.list":
                return [
                    self.public(j)
                    for j in self.store.jobs()
                    if j["project"] == session["project"]
                ][-200:]
            if method == "jobs.cancel":
                job = self.own(p["id"], session)
                if job["state"] in TERMINAL:
                    return self.public(job)
                if (
                    job["spec"]["operation"] == "host.lease"
                    and job["state"] == "running"
                ):
                    spec = job["spec"]
                    spec["drain_requested"] = True
                    self.store.update(
                        job["id"], spec=spec, reason="external_tool_drain_requested"
                    )
                    return self.public(self.store.job(job["id"]))
                if job["state"] == "queued":
                    self.finish(
                        job,
                        {
                            "state": "cancelled",
                            "cleanup_ok": True,
                            "reason": "cancelled_before_start",
                        },
                    )
                else:
                    self.store.update(
                        job["id"],
                        cancel=1,
                        reason="cancellation_requested",
                        revision=job["revision"] + 1,
                    )
                    self.store.event(job["id"], "cancel_requested", {})
                return self.public(self.store.job(job["id"]))
            if method in ("leases.release", "leases.state", "leases.track"):
                job = self.own(p["id"], session)
                if job["spec"]["operation"] != "host.lease":
                    raise WorkbenchError("Not a lease job")
                if method == "leases.release":
                    spec = job["spec"]
                    tracked = spec.get("lease_process")
                    if tracked and same_process(tracked):
                        raise WorkbenchError(
                            "Upstream process is still alive; stop it before release"
                        )
                    spec["lease_released"] = True
                    self.store.update(job["id"], spec=spec)
                    return {"released": True}
                if method == "leases.track":
                    if not same_process(p["identity"]):
                        raise WorkbenchError("Upstream process not alive")
                    spec = job["spec"]
                    spec["lease_process"] = p["identity"]
                    self.store.update(job["id"], spec=spec)
                    return {"tracked": True}
                pending = any(
                    x["state"] == "queued"
                    and self.dependencies_ready(x)
                    and conflict(x["spec"]["resources"], job["spec"]["resources"])
                    for x in self.store.jobs()
                )
                return {
                    "draining": pending
                    or job["spec"].get("drain_requested", False)
                    or job["state"] != "running",
                    "state": job["state"],
                }
            if method == "jobs.set_priority":
                job = self.own(p["id"], session)
                if job["state"] != "queued":
                    raise WorkbenchError("Only queued task priority can change")
                value = p["priority"]
                if type(value) != int or not -5 <= value <= 5:
                    raise WorkbenchError("priority must be an integer -5..5")
                self.store.update(
                    job["id"], priority=value, revision=job["revision"] + 1
                )
                return self.public(self.store.job(job["id"]))
            if method in ("jobs.subscribe", "jobs.artifacts"):
                job = self.shared(p["id"], session)
                if method == "jobs.subscribe":
                    rows = self.store.rows(
                        "SELECT * FROM events WHERE job=? AND seq>? ORDER BY seq LIMIT 200",
                        (job["id"], int(p.get("after", 0))),
                    )
                    return {
                        "events": [{**r, "data": json.loads(r["data"])} for r in rows],
                        "state": job["state"],
                        "gap": False,
                    }
                path = Path(job["spec"]["directory"]) / "artifacts.json"
                return {
                    "published": json.loads(path.read_text())
                    if path.is_file()
                    else None,
                    "state": job["state"],
                }
            if method in ("devices.list", "devices.state"):
                rows = []
                for ident, entry in self.config["devices"].items():
                    users = [
                        j["id"]
                        for j in self.store.jobs()
                        if j["state"] in ACTIVE
                        and j["spec"].get("device") == ident
                        and not j["spec"].get("device_released")
                    ]
                    health = self.store.rows(
                        "SELECT * FROM resources WHERE key=?", ("device:" + ident,)
                    )
                    rows.append(
                        {
                            "id": ident,
                            **entry,
                            "jobs": users,
                            "availability": health[0]["status"]
                            if health
                            else ("occupied" if users else "available"),
                            "observation": self.contexts.get(ident),
                            "cached": True,
                        }
                    )
                return (
                    rows
                    if method == "devices.list"
                    else next((x for x in rows if x["id"] == p["device"]), None)
                )
            if method == "devices.register":
                ident = p["id"]
                serial = p["serial"]
                if (
                    not isinstance(ident, str)
                    or not ident
                    or len(ident) > 100
                    or not isinstance(serial, str)
                    or not serial
                ):
                    raise WorkbenchError("Device id and serial required")
                if (
                    ident in self.config["devices"]
                    and self.config["devices"][ident]["serial"] != serial
                ):
                    raise WorkbenchError(
                        "Changing a registered connection requires a drained service configuration update"
                    )
                if any(
                    x["serial"] == serial
                    for k, x in self.config["devices"].items()
                    if k != ident
                ):
                    raise WorkbenchError(
                        "Connection already registered under another device id"
                    )
                self.config["devices"][ident] = {"serial": serial}
                atomic_json(self.directory / "config.json", self.config)
                return self.config["devices"][ident]
            if method == "resources.state":
                return [
                    {**r, "detail": json.loads(r["detail"])}
                    for r in self.store.rows("SELECT * FROM resources")
                ]
            if method in ("devices.manual_acquire", "devices.manual_release"):
                key = "device:" + p["device"]
                if p["device"] not in self.config["devices"]:
                    raise WorkbenchError("Unknown device")
                if method.endswith("acquire"):
                    health = self.store.rows(
                        "SELECT * FROM resources WHERE key=?", (key,)
                    )
                    if (
                        health
                        and json.loads(health[0]["detail"]).get("owner")
                        != session["token"]
                    ):
                        raise WorkbenchError(
                            "Device requires recovery or is held by another session"
                        )
                    active = any(
                        j["state"] in ACTIVE
                        and j["spec"].get("device") == p["device"]
                        and not j["spec"].get("device_released")
                        for j in self.store.jobs()
                    )
                    status = "manual_pending" if active else "manual"
                    self.store.health(key, status, {"owner": session["token"]})
                    return {"status": status, "handed_over": not active}
                rows = self.store.rows("SELECT * FROM resources WHERE key=?", (key,))
                if (
                    not rows
                    or json.loads(rows[0]["detail"]).get("owner") != session["token"]
                ):
                    raise WorkbenchError("Not manual owner")
                self.store.health(key, "needs_recovery", {"reason": "manual_returned"})
                return self.submit(
                    {
                        "operation": "device.recover",
                        "device": p["device"],
                        "request_key": uuid.uuid4().hex,
                    },
                    session,
                )
            if method in ("devices.recover", "resources.recover"):
                if not p.get("device"):
                    raise WorkbenchError(
                        "Non-device recovery requires a registered repair operation with recovery=true"
                    )
                return self.submit({**p, "operation": "device.recover"}, session)
            raise WorkbenchError("Unknown method: " + method)

    def submit(self, request, session):
        if self.stopping.is_set() or self.draining:
            raise WorkbenchError("Service draining")
        request = dict(request)
        request["project"] = session["project"]
        key = request.get("request_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise WorkbenchError("request_key is required (1..200 chars)")
        fingerprint = digest(request)
        prior = self.store.rows(
            "SELECT id,request_hash FROM jobs WHERE owner=? AND project=? AND request_key=?",
            (session["token"], session["project"], key),
        )
        if prior:
            if prior[0]["request_hash"] != fingerprint:
                raise WorkbenchError(
                    "Idempotency key conflicts with a different request"
                )
            return self.public(self.store.job(prior[0]["id"]))
        queued = [j for j in self.store.jobs() if j["state"] == "queued"]
        if (
            len(queued) >= self.config["max_queued"]
            or sum(j["owner"] == session["token"] for j in queued)
            >= self.config["max_session_queued"]
        ):
            raise WorkbenchError("Queue capacity exceeded")
        import shutil

        if shutil.disk_usage(self.directory).free < self.config["min_free_bytes"]:
            raise WorkbenchError("Insufficient evidence disk capacity")
        ident = uuid.uuid4().hex
        folder = self.directory / "jobs" / ident
        folder.mkdir(parents=True, mode=0o700)
        config_snapshot = json.loads(encode(self.config))
        # Source snapshots and large input hashes must not block cancel/status handlers.
        self.lock.release()
        try:
            spec = normalize(request, config_snapshot, folder)
        finally:
            self.lock.acquire()
        prior = self.store.rows(
            "SELECT id,request_hash FROM jobs WHERE owner=? AND project=? AND request_key=?",
            (session["token"], session["project"], key),
        )
        if prior:
            if prior[0]["request_hash"] != fingerprint:
                raise WorkbenchError(
                    "Idempotency key conflicts with a different request"
                )
            return self.public(self.store.job(prior[0]["id"]))
        queued = [j for j in self.store.jobs() if j["state"] == "queued"]
        if (
            len(queued) >= self.config["max_queued"]
            or sum(j["owner"] == session["token"] for j in queued)
            >= self.config["max_session_queued"]
        ):
            raise WorkbenchError("Queue capacity exceeded during preparation")
        for dep in spec["dependencies"]:
            self.shared(dep if isinstance(dep, str) else dep["id"], session)
        if spec.get("scene"):
            parent = self.shared(spec["scene"], session)
            if parent["spec"].get("device") != spec.get("device"):
                raise WorkbenchError("Scene and observation device differ")
        outputs = spec.get("outputs", [])
        for output in outputs:
            if any(
                overlap("path:" + output, "path:" + r["path"])
                for r in self.store.rows("SELECT path FROM outputs")
            ):
                raise WorkbenchError(
                    "Output already reserved by another task: " + output
                )
        now = time.time()
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.db.execute(
                "INSERT INTO jobs(id,owner,project,request_key,request_hash,state,spec,created,updated,priority) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    session["token"],
                    session["project"],
                    key,
                    fingerprint,
                    "queued",
                    encode(spec),
                    now,
                    now,
                    spec["priority"],
                ),
            )
            for output in outputs:
                self.store.db.execute(
                    "INSERT INTO outputs VALUES(?,?)", (output, ident)
                )
            self.store.event(ident, "accepted", {"operation": spec["operation"]})
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        if spec.get("try_only"):
            job = self.store.job(ident)
            if self.available(job)[0]:
                self.grant(job)
            else:
                self.finish(
                    job,
                    {
                        "state": "failed",
                        "cleanup_ok": True,
                        "reason": "resource_busy_no_action",
                    },
                )
        return self.public(self.store.job(ident))

    def dependencies_ready(self, job):
        for dep in job["spec"]["dependencies"]:
            accept = (
                dep.get("accept_partial", False) if isinstance(dep, dict) else False
            )
            upstream = self.store.job(dep if isinstance(dep, str) else dep["id"])
            if upstream["state"] in TERMINAL and upstream["state"] not in (
                {"succeeded", "partial"} if accept else {"succeeded"}
            ):
                self.finish(
                    job,
                    {
                        "state": "failed",
                        "cleanup_ok": True,
                        "reason": "dependency_failed",
                        "dependency": upstream["id"],
                    },
                )
                return False
            if upstream["state"] not in (
                {"succeeded", "partial"} if accept else {"succeeded"}
            ):
                return False
        return True

    def available(self, job, parent=None, fairness=True):
        spec = job["spec"]
        if (
            job["cancel"]
            or job["state"] != "queued"
            or not self.dependencies_ready(job)
        ):
            return False, "waiting_dependency_or_cancelled"
        if spec.get("scene") and (not parent or spec["scene"] != parent["id"]):
            scene = self.store.job(spec["scene"])
            if scene["state"] in TERMINAL or scene["state"] == "needs_recovery":
                self.finish(
                    job,
                    {"state": "expired", "cleanup_ok": True, "reason": "scene_expired"},
                )
            return False, "waiting_bound_scene"
        if spec.get("not_before", 0) > time.time():
            return False, "waiting_start_time"
        health = self.store.rows("SELECT * FROM resources")
        for h in health:
            if any(overlap(h["key"], x["key"]) for x in spec["resources"]):
                if (
                    spec.get("recovery")
                    and h["status"] == "needs_recovery"
                    and h["key"] == "device:" + spec.get("device", "")
                ):
                    continue
                return False, h["status"]
        all_jobs = self.store.jobs()
        active = [j for j in all_jobs if j["state"] in ACTIVE]
        for current in active:
            if parent and current["id"] == parent["id"]:
                continue
            if (
                spec.get("recovery")
                and current["state"] == "needs_recovery"
                and current["spec"].get("device") == spec.get("device")
            ):
                if current.get("worker") and same_process(current["worker"]):
                    return False, "old_worker_alive"
                continue
            if conflict(spec["resources"], current["spec"]["resources"]):
                return False, "waiting_resource:" + current["id"]
        if (
            not parent
            and spec["operation"] != "host.lease"
            and len(
                [
                    j
                    for j in active
                    if j["state"] != "needs_recovery"
                    and j["spec"]["operation"] != "host.lease"
                ]
            )
            >= self.config["max_workers"]
        ):
            return False, "waiting_capacity"
        if (
            spec["operation"] == "host.lease"
            and sum(j["spec"]["operation"] == "host.lease" for j in active) >= 128
        ):
            return False, "waiting_lease_capacity"
        if (
            spec.get("compute")
            and sum(bool(j["spec"].get("compute")) for j in active)
            >= self.config["max_compute"]
        ):
            return False, "waiting_compute"
        if fairness:
            for earlier in all_jobs:
                if (
                    earlier["id"] == job["id"]
                    or earlier["state"] != "queued"
                    or earlier["created"] >= job["created"]
                ):
                    continue
                if (
                    not self.dependencies_ready(earlier)
                    or earlier["spec"].get("not_before", 0) > time.time()
                ):
                    continue
                # Pending exclusive users prevent a stream of new readers from starving them.
                writes = [
                    r
                    for r in earlier["spec"]["resources"]
                    if r["mode"] == "write" and not r["key"].startswith("device:")
                ]
                if conflict(writes, spec["resources"]):
                    return False, "waiting_earlier_writer:" + earlier["id"]
        return True, None

    def grant(self, job, parent=None):
        token = secrets.token_urlsafe(32)
        spec = job["spec"]
        if spec.get("recovery"):
            affected = {
                j["id"]
                for j in self.store.jobs()
                if j["state"] == "needs_recovery"
                and j["spec"].get("device") == spec.get("device")
            }
            for record in self.store.rows("SELECT * FROM resources"):
                if record["key"] == "device:" + spec.get("device", ""):
                    source = json.loads(record["detail"]).get("job")
                    if source:
                        affected.add(source)
            spec["recovery_targets"] = sorted(affected)
            self.store.update(job["id"], spec=spec)
        internal = {
            "job": job["id"],
            "token": token,
            "generation": self.generation,
            "state": str(self.directory),
            "spec": spec,
        }
        atomic_json(Path(spec["directory"]) / "grant.json", internal)
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.update(
                job["id"],
                state="running",
                started=time.time(),
                generation=digest(token),
                parent=parent["id"] if parent else None,
                reason=None,
            )
            self.store.event(
                job["id"], "started", {"parent": parent["id"] if parent else None}
            )
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        if parent:
            return internal
        log = (Path(spec["directory"]) / "worker.log").open("ab")
        try:
            python = (
                spec.get("python", sys.executable)
                if any(s.get("action") == "hook_attach" for s in spec.get("steps", []))
                else sys.executable
            )
            omitted = {
                "OPENAI_API_KEY",
                "CODEX_API_KEY",
                self.config.get("llm", {}).get("api_key_env", "OPENAI_API_KEY"),
            }
            env = {k: v for k, v in os.environ.items() if k not in omitted}
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            proc = subprocess.Popen(
                [
                    python,
                    "-m",
                    "workbench.worker",
                    str(Path(spec["directory"]) / "grant.json"),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=env,
            )
            self.procs[job["id"]] = proc
            self.store.update(job["id"], worker=process_identity(proc.pid))
        except BaseException as error:
            self.finish(
                self.store.job(job["id"]),
                {
                    "state": "failed",
                    "reason": "worker_start_failed",
                    "cleanup_ok": True,
                    "error": str(error),
                },
            )
        finally:
            log.close()

    def finish(self, job, result):
        if job["state"] in TERMINAL:
            return
        state = result.get("state", "failed")
        if state not in TERMINAL:
            state = "failed"
        cleanup = result.get("cleanup_ok", False)
        if not cleanup:
            state = "failed"
            for r in job["spec"]["resources"]:
                if (
                    r["mode"] == "write"
                    or r["key"].startswith("device:")
                    or job["spec"]["operation"] == "host.lease"
                ):
                    self.store.health(
                        r["key"],
                        "needs_recovery",
                        {"job": job["id"], "reason": "cleanup_or_outcome_unconfirmed"},
                    )
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.update(
                job["id"],
                state=state,
                result=result,
                finished=time.time(),
                reason=result.get("reason"),
            )
            self.store.event(
                job["id"], "finished", {"state": state, "cleanup_ok": cleanup}
            )
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        if job["spec"].get("recovery") and state == "succeeded" and cleanup:
            device = job["spec"]["device"]
            self.store.db.execute(
                "DELETE FROM resources WHERE key=?", ("device:" + device,)
            )
            for row in self.store.rows("SELECT * FROM resources"):
                if json.loads(row["detail"]).get("job") in result.get(
                    "reconciled_jobs", []
                ):
                    if any(
                        r["key"] == row["key"]
                        for r in result.get("recovered_resources", [])
                    ):
                        self.store.db.execute(
                            "DELETE FROM resources WHERE key=?", (row["key"],)
                        )
            for old in self.store.jobs():
                if (
                    old["id"] != job["id"]
                    and old["state"] == "needs_recovery"
                    and old["spec"].get("device") == device
                ):
                    self.finish(
                        old,
                        {
                            "state": "failed",
                            "cleanup_ok": True,
                            "reason": "reconciled_by_recovery",
                            "recovery_job": job["id"],
                        },
                    )

    def select(self, lane, candidates, context):
        try:
            chosen, reason, detail = self.policy.choose(candidates, context)
            with self.lock:
                if self.stopping.is_set():
                    return
                job = self.store.job(chosen)
                original = next(x for x in candidates if x["id"] == chosen)
                legal, _ = self.available(job)
                # Revalidate changed candidates without starting another slow model request.
                fresh = [
                    j
                    for j in self.store.jobs()
                    if j["state"] == "queued"
                    and (
                        "device:" + j["spec"]["device"]
                        if j["spec"].get("device")
                        else "local"
                    )
                    == lane
                    and self.available(j)[0]
                ]
                if not legal or job["revision"] != original["revision"]:
                    if not fresh:
                        return
                    job = min(
                        fresh, key=lambda j: (-j["priority"], j["created"], j["id"])
                    )
                    reason = "model_snapshot_invalidated"
                    detail = {"mode": "fallback"}
                aged = [
                    j
                    for j in fresh
                    if time.time() - j["created"] >= self.config["fair_after"]
                ]
                if (
                    aged
                    and chosen != min(aged, key=lambda j: (j["created"], j["id"]))["id"]
                ):
                    job = min(aged, key=lambda j: (j["created"], j["id"]))
                    reason = "fairness_revalidated"
                    detail = {"mode": "fairness"}
                elif fresh and job["priority"] < max(j["priority"] for j in fresh):
                    job = max(fresh, key=lambda j: (j["priority"], -j["created"]))
                    reason = "priority_revalidated"
                    detail = {"mode": "deterministic"}
                self.store.event(job["id"], "decision", {"reason": reason, **detail})
                self.grant(job)
        finally:
            with self.lock:
                self.selecting.discard(lane)

    def worker_request(self, method, p, token):
        with self.lock:
            job = self.store.job(p["id"])
            if not isinstance(token, str) or job["generation"] != digest(token):
                raise WorkbenchError("Invalid execution token")
            if method == "worker.finished":
                path = Path(job["spec"]["directory"]) / "result.json"
                result = json.loads(path.read_text())
                if job["parent"]:
                    self.finish(job, result)
                # Top-level grants are released by tick only after this worker exits.
                return {"accepted": True}
            if method in (
                "worker.release_device_request",
                "worker.release_device_state",
                "worker.release_device_ack",
            ):
                if (
                    job["spec"]["operation"] != "apk.pull"
                    or job["parent"]
                    or job["state"] != "running"
                ):
                    raise WorkbenchError("Operation has no releasable device phase")
                spec = job["spec"]
                if method == "worker.release_device_request":
                    spec["release_device_requested"] = True
                    self.store.update(job["id"], spec=spec)
                elif method == "worker.release_device_ack":
                    if not spec.get("release_device_requested"):
                        raise WorkbenchError("No device release requested")
                    spec["device_released"] = True
                    spec["resources"] = [
                        r
                        for r in spec["resources"]
                        if not r["key"].startswith("device:")
                        and r["key"] != "service:adb"
                    ]
                    self.store.update(job["id"], spec=spec)
                    self.store.event(job["id"], "device_released", {})
                return {"released": spec.get("device_released", False)}
            if method == "worker.lease_state":
                return {"released": job["spec"].get("lease_released", False)}
            if method in ("worker.validate_entry", "worker.validate_mcp"):
                ident = process_identity(p["pid"])
                ancestor = p["pid"]
                owned = False
                while ancestor > 1:
                    if job["worker"] and ancestor == job["worker"]["pid"]:
                        owned = True
                        break
                    try:
                        ancestor = int(
                            Path(f"/proc/{ancestor}/stat")
                            .read_text()
                            .rsplit(")", 1)[1]
                            .split()[1]
                        )
                    except (OSError, ValueError):
                        break
                if (
                    not ident
                    or not owned
                    or job["state"] not in {"running", "cleaning"}
                ):
                    raise WorkbenchError("Entry is not a live descendant of this task")
                if method == "worker.validate_mcp":
                    if not job["spec"].get("entry", {}).get("uses_mcp"):
                        raise WorkbenchError("Operation has no nested MCP grant")
                    if str(Path(p["project"]).resolve()) != job["project"]:
                        raise WorkbenchError(
                            "Nested MCP belongs to a different project"
                        )
                    return {"managed": True}
                source = Path(job["spec"].get("source", job["spec"]["project"]))
                sources = [
                    source,
                    *map(Path, job["spec"].get("nested_source_manifests", {})),
                ]
                if not any(
                    Path(p["script"]).resolve().is_relative_to(root) for root in sources
                ):
                    raise WorkbenchError(
                        "Nested entry is outside the pinned operation source"
                    )
                return {"managed": True}
            if method == "worker.ready":
                ident = process_identity(p["pid"])
                if not ident:
                    raise WorkbenchError("Worker identity not live")
                self.store.update(job["id"], worker=ident)
                return {"ready": True}
            if method == "worker.event":
                self.store.event(job["id"], p["kind"], p.get("data", {}))
                return {"accepted": True}
            if method == "worker.authorize":
                cleanup = bool(p.get("cleanup"))
                if job["state"] not in ACTIVE or (
                    job["state"] in {"paused", "needs_recovery"} and not cleanup
                ):
                    raise WorkbenchError("Execution is not current")
                if job["cancel"] and not cleanup:
                    return {"cancel": True}
                if job["parent"]:
                    parent = self.store.job(job["parent"])
                    if parent["state"] != "paused":
                        raise WorkbenchError("Borrowed scene no longer active")
                expired = (
                    job["started"]
                    and time.time() - job["started"] > job["spec"]["timeout"]
                )
                return {
                    "cancel": False,
                    "expired": bool(expired) and not cleanup,
                    "release_device": job["spec"].get("release_device_requested", False)
                    and not job["spec"].get("device_released", False),
                }
            if method == "worker.observation":
                if job["state"] not in ACTIVE:
                    raise WorkbenchError("Inactive observation")
                self.contexts[job["spec"]["device"]] = {
                    **p["observation"],
                    "observed_at": time.time(),
                    "scene": job["id"],
                }
                self.store.event(job["id"], "observation", p["observation"])
                return {"accepted": True}
            if method == "worker.checkpoint":
                if job["state"] != "running" or job["cancel"]:
                    return {"insert": None}
                if job["parent"]:
                    return {"insert": None}
                cp = p["checkpoint"]
                # The checkpoint must be present in the pinned adapter plan.
                if (
                    cp not in job["spec"].get("steps", [])
                    or cp.get("action") != "checkpoint"
                ):
                    raise WorkbenchError("Unregistered checkpoint")
                self.store.update(job["id"], state="paused")
                self.store.event(job["id"], "checkpoint", {"checkpoint": cp})
            elif method == "worker.resumed":
                if job["state"] != "paused":
                    raise WorkbenchError("Not paused")
                self.store.update(job["id"], state="running")
                self.store.event(job["id"], "resumed", {})
                return {"resumed": True}
            else:
                raise WorkbenchError("Unknown worker method")
        # Model latency never holds the database/control lock.
        return self.choose_insertion(
            job, cp, p.get("observation", {}), p.get("remaining_budget", cp["budget"])
        )

    def choose_insertion(self, parent, cp, observation, remaining_budget):
        started = time.monotonic()
        budget = min(cp["budget"], max(0, remaining_budget))
        with self.lock:
            candidates = []
            for job in self.store.jobs():
                s = job["spec"]
                allowed = s["operation"] in cp.get("allow", []) or (
                    self.config.get("test_mode") and s.get("insertable")
                )
                if (
                    job["state"] != "queued"
                    or not s.get("insertable")
                    or not allowed
                    or s.get("device") != parent["spec"].get("device")
                ):
                    continue
                if s.get("app") and s["app"] != observation.get("app"):
                    continue
                if s.get("activity") and s["activity"] != observation.get("activity"):
                    continue
                if s.get("expected_boot") and s["expected_boot"] != observation.get(
                    "boot_id"
                ):
                    continue
                if observation.get("hooks") and not s.get("accept_hooks", False):
                    continue
                if s["estimate"] > budget or s.get("baseline"):
                    continue
                if self.available(job, parent)[0]:
                    candidates.append(job)
        result = None
        if candidates:
            chosen, reason, detail = self.policy.choose(
                candidates,
                {**observation, "decision_budget_seconds": max(0.1, budget / 2)},
            )
            with self.lock:
                latest = self.store.job(parent["id"])
                child = self.store.job(chosen)
                if (
                    not latest["cancel"]
                    and latest["state"] == "paused"
                    and time.monotonic() - started < budget
                    and self.available(child, latest)[0]
                ):
                    child["spec"]["timeout"] = min(
                        child["spec"]["timeout"],
                        max(0.1, budget - (time.monotonic() - started)),
                    )
                    self.store.update(child["id"], spec=child["spec"])
                    self.store.event(
                        child["id"],
                        "inserted",
                        {"parent": parent["id"], "reason": reason, **detail},
                    )
                    result = self.grant(child, latest)
        return {"insert": result}

    def tick(self):
        lanes = {}
        with self.lock:
            for ident, proc in list(self.procs.items()):
                if proc.poll() is not None:
                    del self.procs[ident]
            for job in self.store.jobs():
                if job["state"] in ACTIVE and not job["parent"]:
                    result_path = Path(job["spec"]["directory"]) / "result.json"
                    alive = same_process(job["worker"])
                    if (
                        alive
                        and job["spec"]["operation"] != "host.lease"
                        and (
                            job["cancel"]
                            or time.time() - job["started"] > job["spec"]["timeout"]
                            or job["state"] == "needs_recovery"
                        )
                    ):
                        deadline = self.stop_deadlines.setdefault(
                            job["id"], time.monotonic() + self.config["cleanup_grace"]
                        )
                        if time.monotonic() >= deadline:
                            # Keep grants blocked; terminating a controller never proves remote cleanup.
                            os.kill(job["worker"]["pid"], signal.SIGKILL)
                            self.store.event(
                                job["id"], "controller_deadline_exceeded", {}
                            )
                    if result_path.is_file() and not alive:
                        self.finish(job, json.loads(result_path.read_text()))
                    elif (
                        job["worker"]
                        and not alive
                        and not result_path.is_file()
                        and job["state"] != "needs_recovery"
                    ):
                        self.store.update(
                            job["id"],
                            state="needs_recovery",
                            reason="worker_died_outcome_unconfirmed",
                        )
                        self.store.event(
                            job["id"], "recovery_required", {"reason": "worker_died"}
                        )
                        for child in self.store.jobs():
                            if (
                                child["parent"] == job["id"]
                                and child["state"] not in TERMINAL
                            ):
                                saved = Path(child["spec"]["directory"]) / "result.json"
                                if saved.is_file():
                                    self.finish(child, json.loads(saved.read_text()))
                                else:
                                    self.store.update(
                                        child["id"],
                                        state="needs_recovery",
                                        reason="parent_controller_died",
                                    )
                if job["state"] != "queued":
                    continue
                if time.time() - job["created"] > job["spec"]["queue_timeout"]:
                    self.finish(
                        job,
                        {
                            "state": "expired",
                            "cleanup_ok": True,
                            "reason": "queue_timeout",
                        },
                    )
                    continue
                legal, reason = self.available(job)
                if not legal:
                    if (
                        job["reason"] != reason
                        and self.store.job(job["id"])["state"] == "queued"
                    ):
                        self.store.update(job["id"], reason=reason)
                    continue
                lane = (
                    "device:" + job["spec"]["device"]
                    if job["spec"].get("device")
                    else "local"
                )
                lanes.setdefault(lane, []).append(job)
            for h in self.store.rows(
                "SELECT * FROM resources WHERE status='manual_pending'"
            ):
                active = any(
                    j["state"] in ACTIVE
                    and any(overlap(h["key"], r["key"]) for r in j["spec"]["resources"])
                    for j in self.store.jobs()
                )
                if not active:
                    self.store.health(h["key"], "manual", json.loads(h["detail"]))
            for lane, jobs in lanes.items():
                if lane not in self.selecting:
                    self.selecting.add(lane)
                    self.pool.submit(
                        self.select,
                        lane,
                        jobs,
                        self.contexts.get(lane.removeprefix("device:"), {}),
                    )

    def loop(self):
        while not self.stopping.wait(0.1):
            try:
                self.tick()
            except Exception as error:
                print(
                    "scheduler tick:",
                    type(error).__name__,
                    str(error),
                    file=sys.stderr,
                    flush=True,
                )


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(130)
        try:
            pid, uid, gid = struct.unpack(
                "3i",
                self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
            )
            if uid != os.getuid():
                raise WorkbenchError("Different operating-system user")
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE:
                raise WorkbenchError("Request too large")
            request = json.loads(raw)
            if request.get("protocol") != PROTOCOL:
                raise WorkbenchError("Protocol mismatch")
            result = self.server.service.request(
                request["method"], request.get("params", {}), request.get("token")
            )
            response = {"result": result}
        except Exception as error:
            response = {"error": str(error)}
        try:
            self.wfile.write((encode(response) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(directory):
    service = Service(directory)
    path = Path(directory) / "service.sock"
    path.unlink(missing_ok=True)  # Single-instance lock is already held.
    server = Server(str(path), Handler)
    server.service = service
    os.chmod(path, 0o600)

    def stop(signum, frame):
        service.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    service.thread.start()
    atomic_json(
        Path(directory) / "service.json",
        {
            "pid": os.getpid(),
            "identity": process_identity(os.getpid()),
            "version": __version__,
        },
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        service.stopping.set()
        service.thread.join(timeout=2)
        server.server_close()
        service.pool.shutdown(wait=True)
        path.unlink(missing_ok=True)
        os.close(service.instance_lock)
