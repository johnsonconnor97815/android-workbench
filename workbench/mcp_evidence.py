"""Bounded, proxy-reported MCP call receipts, separate from semantic proof."""

import base64
import hashlib
import json
from pathlib import Path

from .analysis_evidence import _credential_key
from .common import WorkbenchError, atomic_json, digest, file_hash

SCHEMA = "workbench.mcp-call/v1"
MAX_RETAINED_RESPONSE = 256 * 1024


def redact_request(value):
    if isinstance(value, dict):
        return {key: "[credential omitted]" if _credential_key(key) else redact_request(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_request(item) for item in value]
    return value


def record_mcp_call(directory, record):
    """Validate the local receipt and write only its bounded raw response prefix."""
    if not isinstance(record, dict) or set(record) != {"schema", "request", "provider", "response"} or record.get("schema") != SCHEMA:
        raise WorkbenchError("Invalid MCP call receipt schema")
    request, provider, response = (record[key] for key in ("request", "provider", "response"))
    if (not isinstance(request, dict) or request.get("method") != "tools/call"
            or not isinstance(request.get("params"), dict)
            or not isinstance(request["params"].get("name"), str)
            or not request["params"]["name"] or len(request["params"]["name"]) > 1000):
        raise WorkbenchError("MCP receipt requires a named tools/call request")
    safe_request = redact_request(request)
    if len(json.dumps(safe_request).encode()) > 64 * 1024:
        # Preserve the tool/id only when the source request exceeds the budget.
        safe_request = {"method": "tools/call", "id": request.get("id"),
                        "params": {"name": request["params"]["name"]}, "arguments_omitted": "request_budget"}
    if not isinstance(provider, dict) or len(json.dumps(provider).encode()) > 16384:
        raise WorkbenchError("MCP provider metadata exceeds its budget")
    if not isinstance(response, dict) or set(response) != {"bytes", "sha256", "retained_base64", "truncated", "outcome"}:
        raise WorkbenchError("Invalid MCP response receipt")
    size = response["bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 32 * 1024 * 1024 + 1:
        raise WorkbenchError("MCP response length exceeds its transport budget")
    if (response["outcome"] not in {"success", "tool_error", "rpc_error", "incomplete"}
            or not isinstance(response["truncated"], bool)
            or not isinstance(response["retained_base64"], str)
            or len(response["retained_base64"]) > ((MAX_RETAINED_RESPONSE + 2) // 3) * 4):
        raise WorkbenchError("Invalid or oversized MCP retained response")
    try:
        retained = base64.b64decode(response["retained_base64"], validate=True)
    except ValueError as exc:
        raise WorkbenchError("Invalid MCP response base64") from exc
    if (len(retained) > MAX_RETAINED_RESPONSE or size < len(retained)
            or response["truncated"] != (size > len(retained))):
        raise WorkbenchError("MCP response truncation differs from retained bytes")
    sha = response["sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise WorkbenchError("MCP response SHA-256 is invalid")
    if not response["truncated"]:
        if hashlib.sha256(retained).hexdigest() != sha:
            raise WorkbenchError("MCP response digest differs from retained bytes")
        if retained:
            try:
                payload = json.loads(retained)
            except ValueError as exc:
                raise WorkbenchError("MCP response is not JSON-RPC") from exc
            if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
                    or payload.get("id") != request.get("id")):
                raise WorkbenchError("MCP response does not match its request")
            actual = ("rpc_error" if "error" in payload else
                      "tool_error" if isinstance(payload.get("result"), dict) and payload["result"].get("isError") is True
                      else "success" if "result" in payload else "incomplete")
            if actual != response["outcome"]:
                raise WorkbenchError("MCP response outcome differs from its payload")
        elif response["outcome"] != "incomplete":
            raise WorkbenchError("An absent MCP response must be incomplete")
    folder = Path(directory)
    raw_path = folder / "mcp-response.bin"
    receipt_path = folder / "mcp-call.json"
    if receipt_path.exists():
        raise WorkbenchError("MCP call receipt already recorded")
    # The service owns these private, fixed paths; callers cannot select them.
    with raw_path.open("xb") as stream:
        stream.write(retained)
    value = {"schema": SCHEMA, "tool_name": request["params"]["name"],
             "request_identity_sha256": digest(safe_request),
             "request_scope": "credential_redacted_and_bounded",
             "provider": redact_request(provider), "provider_identity_scope": "proxy_reported",
             "response": {**{key: response[key] for key in ("bytes", "sha256", "truncated", "outcome")},
                          "length_scope": "observed_transport_sample" if response["outcome"] == "incomplete" else "complete_response"},
             "retained_response": {"path": str(raw_path), "sha256": file_hash(raw_path), "bytes": len(retained)},
             "limitations": ["A proxy receipt binds retained bytes to a call; it does not verify the tool's semantic claim.",
                             "Truncated responses retain a prefix; complete-response hash and provider identity are proxy-reported."]}
    atomic_json(receipt_path, value)
    return {"path": str(receipt_path), "sha256": file_hash(receipt_path), "bytes": receipt_path.stat().st_size,
            "outcome": "partial" if response["truncated"] or response["outcome"] != "success" else "succeeded",
            "request_identity_sha256": value["request_identity_sha256"], "provider": value["provider"],
            "tool_name": value["tool_name"]}
