"""Validate observations against a job obtained from the authenticated service.

This module accepts trusted Store records, never a caller-selected state directory.
The caller must enforce Service.shared() before passing a job here.
"""

import hashlib
import json
import math
from pathlib import Path
import re

from .common import WorkbenchError
from .analysis_evidence import verify_evidence

MAX_OBSERVATION_BYTES = 32 * 1024 * 1024
MAX_TOTAL_OBSERVATION_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024


class RuntimeResourceLimit(ValueError):
    pass


def _read_metadata(path, expected=None):
    if path.is_symlink() or not path.is_file():
        raise ValueError("runtime metadata must be a regular file")
    before = path.stat()
    if before.st_size > MAX_METADATA_BYTES:
        raise RuntimeResourceLimit("runtime metadata exceeds the 8 MiB resource_limit")
    with path.open("rb") as stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise RuntimeResourceLimit("runtime metadata exceeds the 8 MiB resource_limit")
    after = path.stat()
    if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        raise ValueError("runtime metadata changed during verification")
    if expected is not None and (len(raw) != expected.get("bytes")
                                 or hashlib.sha256(raw).hexdigest() != expected.get("sha256")):
        raise ValueError("runtime evidence sidecar differs from the trusted job reference")
    return json.loads(raw)


def _hash_observation(path, expected_bytes):
    value = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(min(1024 * 1024, expected_bytes - consumed + 1))
            if not block:
                return value.hexdigest()
            consumed += len(block)
            if consumed > expected_bytes:
                raise RuntimeResourceLimit("runtime observation grew beyond its admitted resource_limit")
            value.update(block)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _device_operation(spec):
    operation = spec.get("operation")
    if operation in {"device.screenshot", "device.observe", "device.info"}:
        return True
    if operation == "device.scene":
        return any(step.get("action") in {"observe", "screenshot", "ui_dump", "logcat", "info"}
                   for step in spec.get("steps", []) if isinstance(step, dict))
    # Only reviewed observation/probe adapters qualify. Firmware installation,
    # environment setup or arbitrary device shell jobs cannot certify runtime.
    collectors = {"frida.inspect_device", "frida.probe_native", "frida.probe_uncrackable",
                  "apkrev.coldstart", "apkrev.grab_crash", "apkrev.install_test",
                  "apkrev.lib_map", "apkrev.run_probe", "apkrev.sig_probe", "apkrev.snap"}
    return (operation in collectors
            and isinstance(spec.get("entry"), dict)
            and spec["entry"].get("serial") is not None)


def verify_runtime(job, params, manifest=None):
    """Return qualification, not a verdict about the observation's meaning."""
    result = {"schema": "1", "verified": False, "issues": [],
              "limitation": "Job and published-file checks do not establish the semantic claim."}
    try:
        if (not isinstance(params, dict)
                or set(params) != {"job_id", "device", "observation_window", "observations"}):
            raise ValueError("runtime requires job_id, device, observation_window and observations only")
        if not isinstance(job, dict) or job.get("id") != params["job_id"]:
            raise ValueError("runtime job identity differs")
        spec, outcome = job.get("spec"), job.get("result")
        if not isinstance(spec, dict) or not isinstance(outcome, dict):
            raise ValueError("runtime job has no verified final result")
        if not job.get("project") or job["project"] != spec.get("project") or not job.get("owner"):
            raise ValueError("runtime job project or execution owner is unavailable")
        if (job.get("state") != "succeeded" or outcome.get("state") != "succeeded"
                or outcome.get("cleanup_ok") is not True
                or outcome.get("uncertain") is not False
                or outcome.get("cleanup_errors") != [] or outcome.get("reason")
                or outcome.get("error") or outcome.get("errors")
                or outcome.get("exit_code") not in (None, 0)):
            raise ValueError("runtime job did not succeed with confirmed cleanup")
        if (not isinstance(params["device"], str) or not params["device"]
                or spec.get("device") != params["device"] or not spec.get("serial")
                or not _device_operation(spec)):
            raise ValueError("runtime requires the matching device and a genuine device observation operation")
        started, finished = job.get("started"), job.get("finished")
        worker_finished = outcome.get("finished_at")
        if not all(_number(value) for value in (started, finished, worker_finished)):
            raise ValueError("runtime job execution timestamps are unavailable")
        if not started <= worker_finished <= finished:
            raise ValueError("runtime job execution timestamps are inconsistent")
        window = params["observation_window"]
        if not isinstance(window, dict) or set(window) != {"started_at", "finished_at"}:
            raise ValueError("runtime observation_window requires started_at and finished_at")
        begin, end = window["started_at"], window["finished_at"]
        if not _number(begin) or not _number(end) or begin != started or end != worker_finished:
            raise ValueError("runtime window must equal full job execution; artifact capture times are unknown")
        observations = params["observations"]
        if not isinstance(observations, list) or not 1 <= len(observations) <= 1000:
            raise ValueError("runtime requires 1..1000 published observations")
        # Reject the complete request before hashing any observation. RPC callers
        # may hold the scheduler lock, so one request has a fixed I/O budget.
        total = 0
        prepared = []
        for item in observations:
            if (not isinstance(item, dict) or set(item) != {"kind", "path", "sha256"}
                    or item["kind"] not in {"ui", "log", "file", "state"}
                    or not isinstance(item["path"], str)
                    or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("runtime observation requires kind, absolute path and SHA-256")
            path = Path(item["path"])
            if path.name in {"artifacts.json", "normalized.json", "grant.json", "result.json", "analysis-evidence.json"}:
                raise ValueError("runtime publication metadata is not an observation")
            if not path.is_absolute() or path.is_symlink() or str(path.resolve()) != str(path) or not path.is_file():
                raise ValueError("runtime observation path must be a canonical absolute regular file")
            before = path.stat()
            total += before.st_size
            if before.st_size > MAX_OBSERVATION_BYTES or total > MAX_TOTAL_OBSERVATION_BYTES:
                raise RuntimeResourceLimit("runtime observations exceed 32 MiB per file or 64 MiB total resource_limit")
            prepared.append((item, path, before))
        directory = Path(spec["directory"])
        if not directory.is_absolute():
            raise ValueError("runtime trusted job directory is not absolute")
        reference = outcome.get("analysis_evidence")
        if (not isinstance(reference, dict) or reference.get("name") != "analysis-evidence.json"
                or not isinstance(reference.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])):
            raise ValueError("runtime legacy job has no trusted analysis evidence publication binding")
        evidence = verify_evidence(_read_metadata(directory / reference["name"], reference))
        if (evidence.get("evidence_id") != reference.get("evidence_id")
                or evidence.get("job_id") != job["id"] or evidence.get("operation") != spec["operation"]
                or not isinstance(evidence.get("outcome"), dict)
                or any(evidence["outcome"].get(key) != outcome.get(key) for key in
                       ("state", "functional_result", "exit_code", "cleanup_ok", "uncertain", "reason"))):
            raise ValueError("runtime bound evidence identity or final outcome differs")
        raw_results = evidence.get("raw_results")
        if not isinstance(raw_results, list) or len(raw_results) > 10000:
            raise ValueError("runtime invalid bound raw results")
        bound = {}
        for entry in raw_results:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("runtime invalid bound raw result")
            bound.setdefault(entry["path"], []).append(entry)
        if manifest is None:
            manifest = _read_metadata(directory / "artifacts.json")
        if (not isinstance(manifest, dict) or manifest.get("job") != job["id"]
                or manifest.get("operation") != spec["operation"]
                or manifest.get("device") != spec["device"] or manifest.get("quality") != "succeeded"):
            raise ValueError("runtime artifact publication does not match the job")
        published = {}
        for item in manifest.get("files", []):
            name = item.get("name")
            if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                raise ValueError("runtime invalid internal published artifact")
            path = directory / name
            published.setdefault(str(path), []).append(item)
        for output in manifest.get("external_outputs", []):
            for item in output.get("files", []):
                path = item.get("path")
                if isinstance(path, str) and Path(path).is_absolute():
                    published.setdefault(path, []).append(item)
        verified = []
        for item, path, before in prepared:
            matches = published.get(str(path), [])
            if len(matches) != 1 or matches[0].get("sha256") != item["sha256"]:
                raise ValueError("runtime observation is not uniquely published with its declared hash")
            raw = bound.get(str(path), [])
            if len(raw) != 1 or raw[0].get("sha256") != item["sha256"] or raw[0].get("bytes") != before.st_size:
                raise ValueError("runtime observation is not bound to trusted published evidence")
            actual = _hash_observation(path, before.st_size)
            after = path.stat()
            if (not path.is_file() or after.st_size == 0 or actual != item["sha256"]
                    or matches[0].get("bytes") != after.st_size
                    or (before.st_ino, before.st_mtime_ns, before.st_size)
                    != (after.st_ino, after.st_mtime_ns, after.st_size)):
                raise ValueError("runtime published observation changed or disappeared")
            verified.append(dict(item))
        result.update(verified=True, job_id=job["id"], project=job["project"],
                      owner_verified=True, device=spec["device"], operation=spec["operation"],
                      observation_window=dict(window), window_scope="job_execution", observations=verified)
    except RuntimeResourceLimit as exc:
        result.update(reason="resource_limit")
        result["issues"].append(str(exc))
    except (ValueError, OSError, KeyError, TypeError, AttributeError, WorkbenchError) as exc:
        result["issues"].append(str(exc))
    return result
