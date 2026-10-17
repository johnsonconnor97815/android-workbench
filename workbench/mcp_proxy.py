"""Relay analysis MCPs while coordinating live environments and queued tool calls."""

from collections import deque
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from .client import Client
from .analysis_evidence import executable_identity
from .mcp_evidence import SCHEMA as MCP_CALL_SCHEMA, MAX_RETAINED_RESPONSE, redact_request
from .common import (
    WorkbenchError,
    encode,
    process_identity,
    stop_group,
    descendants,
    become_subreaper,
    rpc,
)


def proxy(project, directory, command):
    become_subreaper()
    if os.environ.get("AWB_INTERNAL_GRANT"):
        return nested_proxy(project, command)
    client = Client(project, directory, "mcp-proxy-" + uuid.uuid4().hex)
    owner = process_identity(os.getpid())
    environment = client.submit(
        operation="host.lease",
        lease_owner=owner,
        lease_mode="environment",
        timeout=86400,
        purpose="Live analysis MCP environment",
    )
    while True:
        status = client.call("jobs.status", {"id": environment["id"]})
        if status["state"] == "running":
            break
        if status["state"] in {"failed", "cancelled", "expired", "needs_recovery"}:
            raise WorkbenchError("Cannot acquire analysis environment")
        time.sleep(0.1)
    proc = None
    incoming = deque()
    pending = {}
    waiting = None
    messages = queue.Queue()
    draining = False
    provider = {"direct_executable": executable_identity(command[0], cwd=Path.cwd()),
                "server_info": None, "identity_status": "proxy_reported"}
    initialize_ids = set()

    def input_reader():
        # An idle daemon must not hold a buffered-stdin lock when maintenance
        # ends the proxy while the client still has its input pipe open.
        buffered = b""
        while True:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                if buffered:
                    messages.put(("input", buffered))
                break
            buffered += chunk
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                messages.put(("input", line + b"\n"))
        messages.put(("eof", None))

    def output_reader():
        while True:
            line = proc.stdout.readline(32 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 32 * 1024 * 1024:
                messages.put(("upstream_limit", line))
                return
            messages.put(("output", line))
        messages.put(("upstream_eof", None))

    def release(job):
        client.call("leases.release", {"id": job["id"]})

    def record_call(job, raw=b"", outcome="incomplete"):
        if job.get("_mcp_recorded"):
            return
        record = {
            "schema": MCP_CALL_SCHEMA, "request": job["_mcp_request"], "provider": provider,
            "response": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                         "retained_base64": base64.b64encode(raw[:MAX_RETAINED_RESPONSE]).decode("ascii"),
                         "truncated": len(raw) > MAX_RETAINED_RESPONSE, "outcome": outcome},
        }
        client.call("leases.record_mcp", {"id": job["id"], "record": record})
        job["_mcp_recorded"] = True

    def respond_busy(message):
        print(
            encode(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Analysis environment is draining for maintenance; no tool action was executed. Reconnect and retry.",
                            }
                        ],
                        "isError": True,
                    },
                }
            ),
            flush=True,
        )

    def send(raw):
        proc.stdin.write(raw)
        proc.stdin.flush()

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            start_new_session=True,
        )
        client.call(
            "leases.track",
            {"id": environment["id"], "identity": process_identity(proc.pid)},
        )
        threading.Thread(target=input_reader, daemon=True).start()
        threading.Thread(target=output_reader, daemon=True).start()
        last_poll = 0
        while True:
            if time.monotonic() - last_poll > 0.2:
                last_poll = time.monotonic()
                environment_state = client.call(
                    "leases.state", {"id": environment["id"]}
                )
                draining = environment_state.get("draining")
                if environment_state["state"] not in ("running", "queued"):
                    raise WorkbenchError(
                        "Analysis environment lease ended unexpectedly"
                    )
                for active in pending.values():
                    state = client.call("leases.state", {"id": active["id"]})
                    if state["state"] != "running":
                        raise WorkbenchError(
                            "Analysis call exceeded its lease; upstream will be stopped"
                        )
                if waiting:
                    job, message, raw = waiting
                    if draining:
                        client.call("jobs.cancel", {"id": job["id"]})
                        respond_busy(message)
                        waiting = None
                    else:
                        state = client.call("jobs.status", {"id": job["id"]})["state"]
                        if state == "running":
                            job["_mcp_request"] = redact_request(message)
                            pending[encode(message.get("id"))] = job
                            send(raw)
                            waiting = None
                        elif state in {
                            "failed",
                            "cancelled",
                            "expired",
                            "needs_recovery",
                        }:
                            respond_busy(message)
                            waiting = None
                if draining and not pending:
                    for message, raw in incoming:
                        respond_busy(message)
                    break
            if incoming and not pending and not waiting and not draining:
                message, raw = incoming.popleft()
                job = client.submit(
                    operation="host.lease",
                    lease_owner=owner,
                    lease_mode="analysis",
                    timeout=600,
                    purpose="Analysis MCP tool " + message["params"]["name"],
                )
                waiting = (job, message, raw)
            try:
                kind, raw = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "upstream_limit":
                # A bounded transport sample is incomplete, even if its prefix
                # resembles success. Stop upstream rather than growing memory.
                for job in pending.values():
                    record_call(job, raw, "incomplete")
                raise WorkbenchError("MCP response exceeded its 32 MiB transport budget; a bounded prefix was retained")
            if kind in ("eof", "upstream_eof"):
                break
            message = json.loads(raw)
            if kind == "input":
                if message.get("method") == "tools/call":
                    incoming.append((message, raw))
                else:
                    if message.get("method") == "initialize":
                        initialize_ids.add(encode(message.get("id")))
                    send(raw)
            else:
                job = (
                    pending.get(encode(message.get("id")), None)
                    if "method" not in message
                    else None
                )
                if job:
                    outcome = ("rpc_error" if "error" in message else
                               "tool_error" if isinstance(message.get("result"), dict) and message["result"].get("isError") is True
                               else "success" if "result" in message else "incomplete")
                    record_call(job, raw, outcome)
                    release(job)
                    pending.pop(encode(message.get("id")), None)
                elif encode(message.get("id")) in initialize_ids:
                    result = message.get("result")
                    info = result.get("serverInfo") if isinstance(result, dict) else None
                    if isinstance(info, dict):
                        provider["server_info"] = {key: str(info[key])[:1000] for key in ("name", "version") if key in info}
                    initialize_ids.discard(encode(message.get("id")))
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
    finally:
        if proc:
            stop_group(proc)
        if waiting:
            client.call("jobs.cancel", {"id": waiting[0]["id"]})
        if not descendants(os.getpid()):
            for job in pending.values():
                record_call(job)
                release(job)
            release(environment)
        # If descendants survive, owner death leaves a recovery block instead of a false release.


def nested_proxy(project, command):
    """Environment checks already own their full interval; never enqueue a child lease."""
    grant = json.loads(Path(os.environ["AWB_INTERNAL_GRANT"]).read_text())
    params = {"id": grant["job"], "pid": os.getpid(), "project": str(project)}
    rpc(grant["state"], "worker.validate_mcp", params, grant["token"])
    proc = subprocess.Popen(command, start_new_session=True)
    try:
        while proc.poll() is None:
            state = rpc(
                grant["state"], "worker.authorize", {"id": grant["job"]}, grant["token"]
            )
            if state.get("cancel") or state.get("expired"):
                raise WorkbenchError("Parent task cancelled or expired")
            time.sleep(0.1)
        return proc.returncode
    finally:
        stop_group(proc)
