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
    file_hash,
    is_adb_server,
    lock_file,
    overlap,
    path_resource,
    process_identity,
    resource,
    same_process,
)
from .policy import Policy
from .project import device_management_manifest
from .registry import normalize
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
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
    "max_job_output_bytes": 2 * 1024 * 1024 * 1024,
    "evidence_retention_days": 30,
    "max_internal_evidence_bytes": 2 * 1024 * 1024 * 1024,
    "retention_interval_seconds": 3600,
    "cleanup_grace": 20,
}
ACTIVE = {"running", "paused", "verifying", "cleaning", "needs_recovery"}
RETENTION_PRESERVED = {
    "normalized.json",
    "result.json",
    "artifacts.json",
    "retention.json",
}


class Service:
    def source_root(self):
        candidates = []
        if os.environ.get("ANDROID_WORKBENCH_SOURCE"):
            candidates.append(Path(os.environ["ANDROID_WORKBENCH_SOURCE"]))
        pointer = self.directory / "current-runtime.json"
        if pointer.is_file():
            try:
                source = json.loads(pointer.read_text()).get("source")
                if source:
                    candidates.append(Path(source))
            except ValueError:
                pass
        candidates.append(ROOT)
        for candidate in candidates:
            if (
                candidate
                / "skills/android-workbench/components/device-manager/scripts/device_manager.py"
            ).is_file():
                return candidate.resolve()
        raise WorkbenchError(
            "Workbench source is missing; reinstall the shared runtime"
        )

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.instance_lock = lock_file(self.directory / "service.lock")
        self.config = {
            **DEFAULTS,
            **json.loads((self.directory / "config.json").read_text()),
        }
        self.device_management_root = self.directory / "device-management"
        self.device_management_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(
            self.device_management_root / "workbench.project.json",
            device_management_manifest(self.device_management_root, self.source_root()),
        )
        self.store = Store(self.directory / "queue.sqlite3")
        self.lock = threading.RLock()
        self.policy = Policy(self.config)
        self.generation = uuid.uuid4().hex
        self.stopping = threading.Event()
        self.draining = False
        self.selecting = set()
        self.procs = {}
        self.stop_deadlines = {}
        self.next_retention = time.time()
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
            self.enforce_retention()
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
            "artifact_outputs": job["spec"].get("artifact_outputs", {}),
            "artifact_inputs": job["spec"].get("artifact_inputs", {}),
        }

    def last_device_info(self, ident):
        saved = self.store.device_record(ident, "info")
        if saved:
            if saved.get("serial") == self.config["devices"][ident]["serial"]:
                return self.device_info_condition(ident, saved)
            return None
        jobs = [
            job
            for job in self.store.jobs()
            if job["state"] == "succeeded"
            and job["spec"].get("operation") == "device.info"
            and job["spec"].get("device") == ident
            and job["spec"].get("serial") == self.config["devices"][ident]["serial"]
        ]
        for job in sorted(jobs, key=lambda value: value["finished"] or 0, reverse=True):
            path = Path(job["spec"]["directory"]) / "device-info.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            finished = job["finished"] or 0
            saved = {"serial": job["spec"]["serial"], "job": job["id"],
                     "finished_at": finished, "data": data}
            self.store.save_device_record(ident, "info", saved)
            return self.device_info_condition(ident, saved)
        return None

    def device_info_condition(self, ident, saved):
        data = {key: value for key, value in saved.items() if key != "serial"}
        invalidation = self.store.device_record(ident, "invalidation")
        stale = bool(
            invalidation
            and invalidation.get("serial") == self.config["devices"][ident]["serial"]
            and invalidation["at"] > saved["finished_at"]
        )
        data.update(age_seconds=max(0, round(time.time() - saved["finished_at"], 3)), stale=stale)
        if stale:
            data.update(invalidated_at=invalidation["at"],
                        invalidation_reason=invalidation["reason"])
        return data

    def device_status(self, ident):
        data = self.store.device_status(ident)
        if not data or data.get("serial") != self.config["devices"][ident]["serial"]:
            return None
        return {**data, "age_seconds": max(0, round(time.time() - data["updated_at"], 3))}

    def invalidate_device_status(self, ident, reason, job=None, *, invalidate_info=True):
        if invalidate_info:
            self.store.save_device_record(ident, "invalidation", {
                "serial": self.config["devices"][ident]["serial"], "at": time.time(),
                "reason": reason, "job": job,
            })
        data = self.device_status(ident)
        if data:
            data.pop("age_seconds", None)
            data.update(stale=True, invalidated_at=time.time(), invalidation_reason=reason,
                        invalidating_job=job)
            self.store.save_device_status(ident, data)

    def record_device_status(self, job):
        spec = job["spec"]
        ident = spec.get("device")
        if not ident or ident not in self.config["devices"] or "--dry-run" in spec.get("args", []):
            return
        if spec["operation"] == "device.info":
            cached = self.device_status(ident)
            if job["state"] != "succeeded" or spec.get("serial") != self.config["devices"][ident]["serial"]:
                return
            path = Path(spec["directory"]) / "device-info.json"
        elif spec["operation"] == "device_manager.preflight":
            if job["state"] != "succeeded" and job.get("started") is None:
                return
            outputs = spec.get("outputs", [])
            if not outputs:
                self.invalidate_device_status(ident, "preflight_unconfirmed", job["id"])
                return
            path = Path(outputs[0])
        else:
            if job["state"] != "succeeded" and job.get("started") is not None and spec["operation"] == "device_manager.install_frida":
                self.invalidate_device_status(ident, "root_operation_failed", job["id"])
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            if spec["operation"] == "device_manager.preflight":
                self.invalidate_device_status(ident, "preflight_unconfirmed", job["id"])
            return
        if not isinstance(data, dict):
            if spec["operation"] == "device_manager.preflight":
                self.invalidate_device_status(ident, "preflight_unconfirmed", job["id"])
            return
        if spec["operation"] == "device.info":
            self.store.save_device_record(ident, "info", {
                "serial": self.config["devices"][ident]["serial"], "job": job["id"],
                "finished_at": job.get("finished") or time.time(), "data": data,
            })
            if cached:
                observations = cached["observations"]
                current = {"boot_id": data.get("boot_id"),
                           "build_fingerprint": data.get("properties", {}).get("ro.build.fingerprint")}
                if any(current[key] and observations.get(key) != current[key] for key in current):
                    self.invalidate_device_status(ident, "boot_or_build_changed", job["id"], invalidate_info=False)
            return
        if data.get("device") != self.config["devices"][ident]["serial"] or data.get("status") not in {"complete", "incomplete"}:
            self.invalidate_device_status(ident, "preflight_unconfirmed", job["id"])
            return
        if job["state"] != "succeeded" and not (
            job["state"] == "failed" and data["status"] == "incomplete"
            and job.get("result", {}).get("cleanup_ok") is True
        ):
            self.invalidate_device_status(ident, "preflight_unconfirmed", job["id"])
            return
        access = data.get("root_access", {"status": "unknown", "reason": "not checked"})
        observations = data.get("observations", {})
        unavailable = data.get("unavailable", {})
        if not all(isinstance(value, dict) for value in (access, observations, unavailable)):
            self.invalidate_device_status(ident, "preflight_unconfirmed", job["id"])
            return
        info = self.last_device_info(ident)
        if info and not info["stale"]:
            previous_info = {
                "boot_id": info["data"].get("boot_id"),
                "build_fingerprint": info["data"].get("properties", {}).get("ro.build.fingerprint"),
            }
            if any(observations.get(key) and previous_info[key]
                   and observations[key] != previous_info[key] for key in previous_info):
                self.invalidate_device_status(ident, "boot_or_build_changed", job["id"])
        cached = self.device_status(ident)
        if access.get("status") == "not_checked" and cached and not cached["stale"]:
            previous = cached["root_access"]
            same_boot = all(observations.get(key) and observations[key] == cached["observations"].get(key)
                            for key in ("boot_id", "build_fingerprint"))
            same_caller = all(access.get(key) is not None and access[key] == previous.get(key)
                              for key in ("caller_uid", "su_path", "su_version"))
            if previous.get("status") == "granted" and same_boot and same_caller:
                access = {**previous, "cached": True}
                observations["root"] = "available"
                unavailable.pop("root", None)
        if access.get("status") == "granted" and access.get("cached") is not True:
            access = {**access, "verified_job": job["id"]}
        self.store.save_device_status(ident, {
            "schema": 1, "serial": data["device"], "job": job["id"],
            "updated_at": job.get("finished") or time.time(), "stale": False,
            "mode": data.get("mode", "unknown"), "observations": observations,
            "unavailable": unavailable, "root_access": access,
        })

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

    def artifact_rows(self, project, name=None):
        if name is None:
            return self.store.rows(
                "SELECT * FROM artifacts WHERE project=? ORDER BY created DESC, id DESC LIMIT 500",
                (project,),
            )
        return self.store.rows(
            "SELECT * FROM artifacts WHERE project=? AND name=? ORDER BY created DESC, id DESC LIMIT 500",
            (project, name),
        )

    def evidence_inventory(self, jobs=None):
        jobs = jobs or self.store.jobs()
        files = 0
        total = 0
        directories = 0
        for job in jobs:
            directory = Path(job["spec"]["directory"])
            if not directory.is_dir():
                continue
            directories += 1
            for path in directory.rglob("*"):
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    files += 1
                    total += path.stat().st_size
                except OSError:
                    continue
        return {
            "jobs": directories,
            "files": files,
            "bytes": total,
            "named_artifacts": len(self.store.rows("SELECT id FROM artifacts")),
        }

    def evidence_state(self):
        record_path = self.directory / "retention.json"
        record = json.loads(record_path.read_text()) if record_path.is_file() else None
        return {
            "policy": {
                "retention_days": self.config["evidence_retention_days"],
                "max_internal_evidence_bytes": self.config[
                    "max_internal_evidence_bytes"
                ],
                "interval_seconds": self.config["retention_interval_seconds"],
            },
            **self.evidence_inventory(),
            "last_run": record,
        }

    def enforce_retention(self):
        jobs = self.store.jobs()
        referenced = {
            dependency
            for job in jobs
            if job["state"] not in TERMINAL
            for dependency in job["spec"].get("dependencies", [])
        }
        artifact_paths = {row["path"] for row in self.store.rows("SELECT path FROM artifacts")}
        entries = []
        inventory = self.evidence_inventory(jobs)
        total = inventory["bytes"]
        for job in jobs:
            if job["state"] not in TERMINAL or job["id"] in referenced:
                continue
            directory = Path(job["spec"]["directory"])
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                if path.parent == directory and path.name in RETENTION_PRESERVED:
                    continue
                if str(path) in artifact_paths:
                    continue
                entries.append(
                    {
                        "job": job["id"],
                        "path": str(path),
                        "mtime": stat.st_mtime,
                        "bytes": stat.st_size,
                    }
                )
        cutoff = time.time() - self.config["evidence_retention_days"] * 86400
        deleted = []
        deleted_bytes = 0
        deleted_paths = set()

        def remove(entry):
            nonlocal deleted_bytes
            try:
                Path(entry["path"]).unlink()
            except FileNotFoundError:
                return
            deleted.append(entry)
            deleted_paths.add(entry["path"])
            deleted_bytes += entry["bytes"]

        for entry in entries:
            if entry["mtime"] <= cutoff:
                remove(entry)
        remaining_budget = total - deleted_bytes
        if remaining_budget > self.config["max_internal_evidence_bytes"]:
            for entry in sorted(
                (entry for entry in entries if entry["path"] not in deleted_paths),
                key=lambda entry: (entry["mtime"], entry["path"]),
            ):
                if remaining_budget <= self.config["max_internal_evidence_bytes"]:
                    break
                remove(entry)
                remaining_budget -= entry["bytes"]
        by_job = {}
        for entry in deleted:
            by_job.setdefault(entry["job"], []).append(
                {"path": entry["path"], "bytes": entry["bytes"]}
            )
        now = time.time()
        for job, files in by_job.items():
            directory = Path(
                next(item["spec"]["directory"] for item in jobs if item["id"] == job)
            )
            atomic_json(
                directory / "retention.json",
                {
                    "applied_at": now,
                    "policy": {
                        "retention_days": self.config["evidence_retention_days"],
                        "max_internal_evidence_bytes": self.config[
                            "max_internal_evidence_bytes"
                        ],
                    },
                    "deleted_files": files,
                },
            )
        result = {
            "applied_at": now,
            "policy": {
                "retention_days": self.config["evidence_retention_days"],
                "max_internal_evidence_bytes": self.config[
                    "max_internal_evidence_bytes"
                ],
            },
            "deleted_files": len(deleted),
            "deleted_bytes": deleted_bytes,
            **self.evidence_inventory(),
        }
        atomic_json(self.directory / "retention.json", result)
        self.next_retention = (
            now + self.config["retention_interval_seconds"]
        )
        return result

    def resolve_artifact_inputs(self, spec, session):
        for name, flag in spec.get("artifact_inputs", {}).items():
            rows = self.artifact_rows(session["project"], name)
            if not rows:
                raise WorkbenchError("Artifact is not published: " + name)
            row = rows[0]
            producer = self.store.job(row["job"])
            if producer["state"] != "succeeded":
                raise WorkbenchError("Artifact producer did not succeed: " + name)
            path = Path(row["path"])
            if not path.is_file() or file_hash(path) != row["sha256"]:
                raise WorkbenchError("Artifact changed or is missing: " + name)
            spec["args"] = [*spec.get("args", []), flag, str(path)]
            if row["job"] not in spec["dependencies"]:
                spec["dependencies"] = [*spec["dependencies"], row["job"]]
            spec["input_fingerprints"][str(path)] = row["sha256"]
            spec["resources"].append(resource(path_resource(path), "read"))

    def prepare_artifacts(self, job):
        records = []
        for name, value in job["spec"].get("artifact_outputs", {}).items():
            path = Path(value)
            if not path.is_file():
                raise WorkbenchError("Artifact output is missing: " + name)
            records.append(
                {
                    "project": job["project"],
                    "name": name,
                    "job": job["id"],
                    "path": str(path),
                    "sha256": file_hash(path),
                    "created": time.time(),
                }
            )
        return records

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
                requested_project = Path(p["project"]).resolve()
                mode = "project"
                project = requested_project
                if not (project / "workbench.project.json").is_file():
                    if p.get("mode") == "device_management" or (
                        project / "workbench" / "service.py"
                    ).is_file():
                        project = self.device_management_root
                        mode = "device_management"
                    else:
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
                return {
                    "token": new_token,
                    "project": str(project),
                    "requested_project": str(requested_project),
                    "mode": mode,
                }
            session = self.authenticate(token)
            if method in ("service.drain", "service.resume"):
                self.draining = method == "service.drain"
                queued = [j["id"] for j in self.store.jobs() if j["state"] == "queued"]
                active = [
                    j["id"]
                    for j in self.store.jobs()
                    if j["state"] in ACTIVE or j["state"] == "queued"
                ]
                return {
                    "draining": self.draining,
                    "active": active,
                    "queued": queued,
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
                        "device.info",
                        "device.recover",
                    ],
                    "operations": manifest.get("operations", {}),
                }
            if method == "jobs.submit" or method == "scenes.request_observation":
                if method == "scenes.request_observation":
                    p = {
                        **p,
                        "operation": "device.screenshot",
                        "estimate": p.get("estimate", 2),
                    }
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
                    atomic_json(
                        Path(spec["directory"]) / "normalized.json",
                        spec,
                    )
                    return {"released": True}
                if method == "leases.track":
                    if not same_process(p["identity"]):
                        raise WorkbenchError("Upstream process not alive")
                    spec = job["spec"]
                    spec["lease_process"] = p["identity"]
                    self.store.update(job["id"], spec=spec)
                    atomic_json(
                        Path(spec["directory"]) / "normalized.json",
                        spec,
                    )
                    return {"tracked": True}
                pending = any(
                    x["state"] == "queued"
                    and self.dependencies_ready(x)
                    and conflict(x["spec"]["resources"], job["spec"]["resources"])
                    for x in self.store.jobs()
                )
                return {
                    "draining": self.draining
                    or pending
                    or job["spec"].get("drain_requested", False)
                    or job["state"] != "running",
                    "state": job["state"],
                }
            if method == "jobs.set_priority":
                job = self.own(p["id"], session)
                if job["state"] != "queued":
                    raise WorkbenchError("Only queued task priority can change")
                value = p["priority"]
                if type(value) is not int or not -5 <= value <= 5:
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
            if method == "artifacts.list":
                return self.artifact_rows(session["project"], p.get("name"))
            if method == "evidence.state":
                return self.evidence_state()
            if method == "evidence.apply_retention":
                return self.enforce_retention()
            if method == "hooks.subscribe":
                scene = self.shared(p["scene"], session)
                has_hook = any(
                    step.get("action") == "hook_attach"
                    for step in scene["spec"].get("steps", [])
                ) or (
                    self.config.get("test_mode")
                    and bool(scene["spec"].get("hooks"))
                )
                if not has_hook:
                    raise WorkbenchError("Scene has no managed Hook")
                child = self.submit(
                {
                    **p,
                    "operation": "device.observe",
                    "accept_hooks": True,
                    "estimate": p.get("estimate", 1),
                },
                    session,
                )
                spec = scene["spec"]
                spec["hook_subscribers"] = [
                    *spec.get("hook_subscribers", []),
                    {
                        "job": child["id"],
                        "session": session["token"],
                        "requested_at": time.time(),
                    },
                ]
                self.store.update(scene["id"], spec=spec)
                atomic_json(Path(spec["directory"]) / "normalized.json", spec)
                return child
            if method == "hooks.list":
                rows = []
                for job in self.store.jobs():
                    if job["project"] != session["project"]:
                        continue
                    has_hook = any(
                        step.get("action") == "hook_attach"
                        for step in job["spec"].get("steps", [])
                    ) or (
                        self.config.get("test_mode")
                        and bool(job["spec"].get("hooks"))
                    )
                    if not has_hook:
                        continue
                    rows.append(
                        {
                            "job": job["id"],
                            "state": job["state"],
                            "device": job["spec"].get("device"),
                            "subscribers": job["spec"].get("hook_subscribers", []),
                        }
                    )
                return rows
            if method in ("devices.list", "devices.state"):
                rows = []
                for ident, entry in self.config["devices"].items():
                    active_jobs = [
                        j
                        for j in self.store.jobs()
                        if j["state"] in ACTIVE
                        and j["spec"].get("device") == ident
                        and not j["spec"].get("device_released")
                    ]
                    queued_jobs = [
                        j
                        for j in self.store.jobs()
                        if j["state"] == "queued"
                        and j["spec"].get("device") == ident
                    ]
                    users = [j["id"] for j in active_jobs]
                    health = self.store.rows(
                        "SELECT * FROM resources WHERE key=?", ("device:" + ident,)
                    )
                    occupancies = [
                        {
                            "source": "job",
                            "job": job["id"],
                            "project": job["project"],
                        }
                        for job in active_jobs
                    ]
                    if health and health[0]["status"] == "manual":
                        owner = json.loads(health[0]["detail"]).get("owner")
                        if owner:
                            owner_sessions = self.store.rows(
                                "SELECT project FROM sessions WHERE token=?", (owner,)
                            )
                            if owner_sessions:
                                occupancies.append(
                                    {
                                        "source": "manual",
                                        "project": owner_sessions[0]["project"],
                                    }
                                )
                    occupying_projects = sorted(
                        {item["project"] for item in occupancies}
                    )
                    queued = [
                        {
                            "source": "job",
                            "job": job["id"],
                            "project": job["project"],
                        }
                        for job in queued_jobs
                    ]
                    assigned_project = entry.get("project")
                    other_project = next(
                        (
                            item["project"]
                            for item in [*occupancies, *queued]
                            if assigned_project is not None
                            and item["project"] != assigned_project
                        ),
                        None,
                    )
                    rows.append(
                        {
                            "id": ident,
                            **entry,
                            "jobs": users,
                            "queued_jobs": [job["id"] for job in queued_jobs],
                            "availability": health[0]["status"]
                            if health
                            else ("occupied" if users else "available"),
                            "project_occupied": bool(occupying_projects),
                            "occupying_projects": occupying_projects,
                            "project": {
                                "assigned": assigned_project,
                                "occupancies": occupancies,
                                "queued": queued,
                                "conflict": other_project is not None,
                                "conflicting_project": other_project,
                            },
                            "last_device_info": self.last_device_info(ident),
                            "device_status": self.device_status(ident),
                            "observation": self.contexts.get(ident),
                            "cached": True,
                        }
                    )
                return (
                    rows
                    if method == "devices.list"
                    else next((x for x in rows if x["id"] == p["device"]), None)
                )
            if method == "devices.discover":
                def probe(command, label):
                    try:
                        completed = subprocess.run(
                            command,
                            capture_output=True,
                            text=True,
                            errors="replace",
                            timeout=5,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired) as error:
                        return {"stdout": "", "error": f"{label}: {error}"}
                    if completed.returncode != 0:
                        detail = completed.stderr.strip() or completed.stdout.strip()
                        return {
                            "stdout": completed.stdout,
                            "error": f"{label} failed: {detail}",
                        }
                    return {"stdout": completed.stdout, "error": None}

                probes = {
                    "adb": probe([self.config.get("adb", "adb"), "devices", "-l"], "adb devices"),
                    "fastboot": probe(
                        [self.config.get("fastboot", "fastboot"), "devices"],
                        "fastboot devices",
                    ),
                }
                discovered = []
                registered = {
                    entry["serial"]: identifier
                    for identifier, entry in self.config["devices"].items()
                }
                for transport, probe_result in probes.items():
                    for line in probe_result["stdout"].splitlines():
                        fields = line.split()
                        if len(fields) < 2 or fields[0] == "List":
                            continue
                        serial, state = fields[:2]
                        discovered.append(
                            {
                                "serial": serial,
                                "state": state,
                                "transport": transport,
                                "description": line,
                                "registered_id": registered.get(serial),
                            }
                        )
                return {
                    "devices": discovered,
                    "errors": {
                        transport: result["error"]
                        for transport, result in probes.items()
                        if result["error"]
                    },
                }
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
                entry = self.config["devices"].get(ident, {})
                self.config["devices"][ident] = {
                    **entry,
                    "serial": serial,
                }
                atomic_json(self.directory / "config.json", self.config)
                return self.config["devices"][ident]
            if method in ("devices.project_assign", "devices.project_release"):
                ident = p["device"]
                if ident not in self.config["devices"]:
                    raise WorkbenchError("Unknown device")
                entry = self.config["devices"][ident]
                jobs = [
                    job
                    for job in self.store.jobs()
                    if (job["state"] in ACTIVE or job["state"] == "queued")
                    and job["spec"].get("device") == ident
                    and not job["spec"].get("device_released")
                ]
                if method == "devices.project_assign":
                    project = p.get("project")
                    if not isinstance(project, str) or not project:
                        raise WorkbenchError("Project required")
                    assigned_path = Path(project).expanduser().resolve()
                    if not (assigned_path / "workbench.project.json").is_file():
                        raise WorkbenchError("Project is not registered")
                    assigned = str(assigned_path)
                    if any(job["project"] != assigned for job in jobs):
                        raise WorkbenchError(
                            "Device has queued or active tasks from another project"
                        )
                    key = "device:" + ident
                    health = self.store.rows(
                        "SELECT * FROM resources WHERE key=?", (key,)
                    )
                    if health and health[0]["status"] == "manual":
                        owner = json.loads(health[0]["detail"]).get("owner")
                        owner_sessions = self.store.rows(
                            "SELECT project FROM sessions WHERE token=?", (owner,)
                        )
                        if owner_sessions and owner_sessions[0]["project"] != assigned:
                            raise WorkbenchError(
                                "Device is manually held by another project"
                            )
                    entry["project"] = assigned
                else:
                    assigned = entry.get("project")
                    if not assigned:
                        raise WorkbenchError("Device is not assigned to a project")
                    if jobs:
                        raise WorkbenchError("Device has queued or active tasks")
                    if session["project"] not in {assigned, str(self.device_management_root)}:
                        raise WorkbenchError(
                            "Only the assigned project or device management can release the assignment"
                        )
                    entry.pop("project", None)
                self.config["devices"][ident] = entry
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
                    self.invalidate_device_status(p["device"], "manual_control")
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
        if spec.get("artifact_inputs"):
            self.resolve_artifact_inputs(spec, session)
            atomic_json(Path(spec["directory"]) / "normalized.json", spec)
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

    def adb_lock_recoverable(self, old):
        if not old or old["spec"].get("entry", {}).get("adb_exclusive") is not True:
            return False
        if old.get("worker") and same_process(old["worker"]):
            return False
        folder = Path(old["spec"]["directory"])
        for name in ("active-process.json", "residual-processes.json"):
            path = folder / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                return False
            if isinstance(data, list):
                identities = data
            elif isinstance(data, dict):
                identities = [data.get("identity")]
            else:
                identities = []
            if any(
                identity and not is_adb_server(identity) and same_process(identity)
                for identity in identities
            ):
                return False
        return True

    def available(self, job, parent=None, fairness=True):
        spec = job["spec"]
        repairs = (
            set(spec.get("entry", {}).get("recovery_for", []))
            if spec.get("recovery")
            else set()
        )
        if (
            job["cancel"]
            or job["state"] != "queued"
            or not self.dependencies_ready(job)
        ):
            return False, "waiting_dependency_or_cancelled"
        if spec["operation"] == "host.lease" and not same_process(spec.get("lease_owner")):
            # No lease has been granted yet, so the abandoned request owns no
            # resources or upstream process tree and needs no recovery action.
            self.finish(job, {
                "state": "cancelled",
                "cleanup_ok": True,
                "reason": "lease_owner_exited_before_grant",
            })
            return False, "lease_owner_exited_before_grant"
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
        all_jobs = self.store.jobs()
        health = self.store.rows("SELECT * FROM resources")
        for h in health:
            if any(overlap(h["key"], x["key"]) for x in spec["resources"]):
                if (
                    spec.get("recovery")
                    and h["status"] == "needs_recovery"
                    and h["key"] == "device:" + spec.get("device", "")
                ):
                    continue
                if spec.get("recovery") and h["status"] == "needs_recovery":
                    source = json.loads(h["detail"]).get("job")
                    old = next((x for x in all_jobs if x["id"] == source), None)
                    if (
                        spec.get("operation") == "device.recover"
                        and h["key"] == "service:adb"
                        and self.adb_lock_recoverable(old)
                    ):
                        continue
                    if (
                        old
                        and old["spec"]["operation"] in repairs
                        and not (old.get("worker") and same_process(old["worker"]))
                    ):
                        continue
                return False, h["status"]
        active = [j for j in all_jobs if j["state"] in ACTIVE]
        for current in active:
            if parent and current["id"] == parent["id"]:
                continue
            if spec.get("recovery") and current["state"] == "needs_recovery":
                if current["spec"]["operation"] not in repairs or (
                    current.get("worker") and same_process(current["worker"])
                ):
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
        if fairness and not spec.get("recovery"):
            # A registered repair must pass ordinary writers that cannot run
            # until this repair clears their resource-health block.
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
        if spec.get("device") and "--dry-run" not in spec.get("args", []) and spec["operation"] in {
            "device_manager.flash", "device_manager.flash_factory", "device_manager.install_rom",
            "device_manager.root", "device_manager.root_prepare", "device_manager.partition_repair",
            "device_manager.bootloader_state",
        }:
            self.invalidate_device_status(spec["device"], spec["operation"], job["id"])
        if spec.get("recovery"):
            repairs = set(spec.get("entry", {}).get("recovery_for", []))
            affected = {
                j["id"]
                for j in self.store.jobs()
                if j["state"] == "needs_recovery"
                and j["spec"].get("device") == spec.get("device")
            }
            for record in self.store.rows("SELECT * FROM resources"):
                source = json.loads(record["detail"]).get("job")
                old = next((j for j in self.store.jobs() if j["id"] == source), None)
                if old and old["spec"]["operation"] in repairs:
                    affected.add(source)
            spec["recovery_targets"] = sorted(affected)
            if spec.get("operation") == "device.recover":
                adb_targets = set()
                for record in self.store.rows("SELECT * FROM resources"):
                    if record["key"] != "service:adb":
                        continue
                    source = json.loads(record["detail"]).get("job")
                    old = next(
                        (x for x in self.store.jobs() if x["id"] == source), None
                    )
                    if self.adb_lock_recoverable(old):
                        adb_targets.add(source)
                spec["adb_recovery_targets"] = sorted(adb_targets)
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
        artifact_records = []
        if state == "succeeded" and job["spec"].get("artifact_outputs"):
            try:
                artifact_records = self.prepare_artifacts(job)
            except WorkbenchError as error:
                state = "failed"
                result = {
                    **result,
                    "state": "failed",
                    "reason": "artifact_publication_failed",
                    "error": str(error),
                }
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
            if job["spec"].get("recovery") and state == "succeeded" and cleanup:
                device = job["spec"].get("device")
                if device:
                    self.store.db.execute(
                        "DELETE FROM resources WHERE key=?", ("device:" + device,)
                    )
                for row in self.store.rows("SELECT * FROM resources"):
                    if row["key"] == "service:adb" and json.loads(row["detail"]).get(
                        "job"
                    ) in job["spec"].get("adb_recovery_targets", []):
                        self.store.db.execute(
                            "DELETE FROM resources WHERE key=?", (row["key"],)
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
            self.store.update(
                job["id"],
                state=state,
                result=result,
                finished=time.time(),
                reason=result.get("reason"),
            )
            self.record_device_status(self.store.job(job["id"]))
            for record in artifact_records:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO artifacts(project,name,job,path,sha256,created) VALUES(?,?,?,?,?,?)",
                    (
                        record["project"],
                        record["name"],
                        record["job"],
                        record["path"],
                        record["sha256"],
                        record["created"],
                    ),
                )
            self.store.event(
                job["id"], "finished", {"state": state, "cleanup_ok": cleanup}
            )
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        if job["spec"].get("recovery") and state == "succeeded" and cleanup:
            device = job["spec"].get("device")
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
                    self.invalidate_device_status(h["key"].removeprefix("device:"), "manual_control")
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
            if time.time() >= self.next_retention:
                self.enforce_retention()

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
