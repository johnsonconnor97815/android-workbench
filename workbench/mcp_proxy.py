"""Relay analysis MCPs while coordinating live environments and queued tool calls."""

from collections import deque
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

    def input_reader():
        for line in sys.stdin.buffer:
            messages.put(("input", line))
        messages.put(("eof", None))

    def output_reader():
        for line in proc.stdout:
            messages.put(("output", line))
        messages.put(("upstream_eof", None))

    def release(job):
        client.call("leases.release", {"id": job["id"]})

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
            if kind in ("eof", "upstream_eof"):
                break
            message = json.loads(raw)
            if kind == "input":
                if message.get("method") == "tools/call":
                    incoming.append((message, raw))
                else:
                    send(raw)
            else:
                job = (
                    pending.pop(encode(message.get("id")), None)
                    if "method" not in message
                    else None
                )
                if job:
                    release(job)
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
    finally:
        if proc:
            stop_group(proc)
        if waiting:
            client.call("jobs.cancel", {"id": waiting[0]["id"]})
        if not descendants(os.getpid()):
            for job in pending.values():
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
