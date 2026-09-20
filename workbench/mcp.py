"""MCP stdio transport; each connection is only a client of the shared service."""

import json
import os
import sys
import uuid
from .client import Client
from .common import encode, rpc, MAX_MESSAGE

METHODS = {
    "sessions_select": (
        "sessions.select",
        {"session": {"type": "string"}},
        ["session"],
    ),
    "devices_list": ("devices.list", {}, []),
    "devices_state": ("devices.state", {"device": {"type": "string"}}, ["device"]),
    "jobs_submit": ("jobs.submit", {"request": {"type": "object"}}, ["request"]),
    "jobs_list": ("jobs.list", {}, []),
    "jobs_status": ("jobs.status", {"id": {"type": "string"}}, ["id"]),
    "jobs_explain": ("jobs.explain", {"id": {"type": "string"}}, ["id"]),
    "jobs_cancel": ("jobs.cancel", {"id": {"type": "string"}}, ["id"]),
    "jobs_set_priority": (
        "jobs.set_priority",
        {
            "id": {"type": "string"},
            "priority": {"type": "integer", "minimum": -5, "maximum": 5},
        },
        ["id", "priority"],
    ),
    "jobs_artifacts": ("jobs.artifacts", {"id": {"type": "string"}}, ["id"]),
    "jobs_subscribe": (
        "jobs.subscribe",
        {"id": {"type": "string"}, "after": {"type": "integer"}},
        ["id"],
    ),
    "operations_list": ("operations.list", {}, []),
    "scenes_request_observation": (
        "scenes.request_observation",
        {"request": {"type": "object"}},
        ["request"],
    ),
    "devices_recover": (
        "devices.recover",
        {"device": {"type": "string"}, "request_key": {"type": "string"}},
        ["device", "request_key"],
    ),
    "devices_manual_acquire": (
        "devices.manual_acquire",
        {"device": {"type": "string"}},
        ["device"],
    ),
    "devices_manual_release": (
        "devices.manual_release",
        {"device": {"type": "string"}},
        ["device"],
    ),
    "resources_state": ("resources.state", {}, []),
    "service_capabilities": ("service.capabilities", {}, []),
}
DESCRIPTIONS = {
    "sessions_select": "Select or resume this connection's persistent Session name before submitting/retrying tasks. Use a stable per-conversation ID; do not reuse another conversation's name.",
    "jobs_submit": "Submit a registered Android operation. Requires operation and request_key; device operations also require device. Returns immediately with a job id. Use operations_list for existing script adapters.",
    "scenes_request_observation": "Queue a screenshot for a bound scene: include scene, device, request_key, expected app/activity and accept_hooks when appropriate. Never bypass the current controller.",
    "jobs_subscribe": "Read persisted progress events after a cursor; this does not start or stop capture.",
    "devices_manual_acquire": "Request a manual handover; only handed_over=true means the device has been released.",
}


def main(project, directory, session=None):
    client = None
    session = (
        session
        or os.environ.get("ANDROID_WORKBENCH_SESSION")
        or os.environ.get("CODEX_THREAD_ID")
        or "mcp-" + uuid.uuid4().hex
    )
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_MESSAGE:
            continue
        request = None
        try:
            request = json.loads(raw)
            method = request.get("method")
            ident = request.get("id")
            if method == "initialize":
                version = request.get("params", {}).get("protocolVersion", "2024-11-05")
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "android-workbench", "version": "0.1.0"},
                    "instructions": "All device jobs share one local scheduler. Do not issue raw device commands while a managed scene owns the phone.",
                }
            elif method.startswith("notifications/"):
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": name,
                            "description": DESCRIPTIONS.get(
                                name, method_name + " via the shared scheduler"
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": props,
                                "required": required,
                                "additionalProperties": False,
                            },
                        }
                        for name, (method_name, props, required) in METHODS.items()
                    ]
                }
            elif method == "tools/call":
                params = request["params"]
                name = params["name"]
                method_name = METHODS[name][0]
                args = params.get("arguments", {})
                if "request" in args:
                    args = args["request"]
                try:
                    if name == "sessions_select":
                        chosen = args["session"]
                        if not isinstance(chosen, str) or not 1 <= len(chosen) <= 200:
                            raise ValueError("Session name must be 1..200 characters")
                        session = chosen
                        client = Client(project, directory, session)
                        value = {"client_session": session, "project": client.project}
                    elif name == "service_capabilities":
                        value = {
                            **rpc(directory, method_name, args),
                            "client_session": session,
                        }
                    else:
                        if client is None:
                            client = Client(project, directory, session)
                        value = client.call(method_name, args)
                    result = {
                        "content": [{"type": "text", "text": encode(value)}],
                        "isError": False,
                    }
                except Exception as error:
                    result = {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    }
            else:
                raise ValueError("Method not supported")
            response = {"jsonrpc": "2.0", "id": ident, "result": result}
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32602, "message": str(error)},
            }
        print(encode(response), flush=True)
