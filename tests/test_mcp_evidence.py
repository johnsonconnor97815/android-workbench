"""Managed MCP call receipts, bounded transport retention and lease provenance."""

import base64
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

from workbench.client import Client
from workbench.common import WorkbenchError, atomic_json, file_hash, process_identity, rpc
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
RETAINED_BYTES = 262144


def call_record(response=None, outcome="success", truncated=False):
    response = response if response is not None else {
        "jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "fixture result"}]}}
    raw = json.dumps(response, separators=(",", ":")).encode() + b"\n"
    prefix = raw[:RETAINED_BYTES] if truncated else raw
    return {"schema": "workbench.mcp-call/v1",
            "request": {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                        "params": {"name": "fixture_lookup", "arguments": {"query": "fixture"}}},
            "provider": {"direct_executable": {"command": sys.executable, "path": sys.executable,
                                                "status": "captured_before_launch", "version": None},
                         "server_info": {"name": "synthetic-provider", "version": "1.0"},
                         "identity_status": "proxy_reported"},
            "response": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                         "retained_base64": base64.b64encode(prefix).decode(), "truncated": truncated,
                         "outcome": outcome}}


class McpEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-mcp-receipt-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        from workbench.mcp_evidence import record_mcp_call
        self.record = record_mcp_call

    def test_success_receipt_retains_response_and_provider_identity(self):
        record = call_record()
        receipt = self.record(self.directory, record)
        path = Path(receipt["path"])
        self.assertEqual(path, self.directory / "mcp-call.json")
        self.assertEqual(receipt["sha256"], file_hash(path))
        self.assertEqual(receipt["bytes"], path.stat().st_size)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(receipt["provider"]["server_info"], record["provider"]["server_info"])
        self.assertEqual(len(receipt["request_identity_sha256"]), 64)
        raw = (self.directory / "mcp-response.bin").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["response"]["sha256"])
        self.assertEqual(len(raw), record["response"]["bytes"])

    def test_tool_and_rpc_errors_are_retained_as_distinct_outcomes(self):
        responses = [
            ({"jsonrpc": "2.0", "id": 7, "result": {"content": [], "isError": True}}, "tool_error"),
            ({"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "fixture error"}}, "rpc_error"),
        ]
        for response, outcome in responses:
            with self.subTest(outcome=outcome):
                folder = self.directory / outcome
                folder.mkdir()
                receipt = self.record(folder, call_record(response, outcome))
                self.assertEqual(receipt["outcome"], "partial")
                retained = json.loads(Path(receipt["path"]).read_text())
                self.assertEqual(retained["response"]["outcome"], outcome)
                self.assertTrue((folder / "mcp-response.bin").is_file())

    def test_truncation_retains_prefix_but_full_response_identity(self):
        response = {"jsonrpc": "2.0", "id": 7,
                    "result": {"content": [{"type": "text", "text": "x" * (RETAINED_BYTES + 100)}]}}
        record = call_record(response, truncated=True)
        receipt = self.record(self.directory, record)
        self.assertEqual(receipt["outcome"], "partial")
        self.assertEqual((self.directory / "mcp-response.bin").stat().st_size, RETAINED_BYTES)
        self.assertGreater(record["response"]["bytes"], RETAINED_BYTES)
        self.assertNotEqual(file_hash(self.directory / "mcp-response.bin"), record["response"]["sha256"])

    def test_invalid_schema_and_response_shapes_fail_closed(self):
        alterations = [
            lambda value: value.update(schema="other/v1"),
            lambda value: value["response"].update(retained_base64="not base64!"),
            lambda value: value["response"].update(bytes=1),
            lambda value: value["response"].update(sha256="0" * 64),
            lambda value: value["response"].update(outcome="invented"),
            lambda value: value["response"].update(truncated="yes"),
        ]
        for index, alter in enumerate(alterations):
            with self.subTest(index=index):
                folder = self.directory / str(index)
                folder.mkdir()
                record = call_record()
                alter(record)
                with self.assertRaises((ValueError, WorkbenchError)):
                    self.record(folder, record)
                self.assertFalse((folder / "mcp-call.json").exists())

    def test_oversize_retained_response_is_rejected(self):
        record = call_record({"jsonrpc": "2.0", "id": 7, "result": {"content": [
            {"type": "text", "text": "x" * (RETAINED_BYTES + 10)}]}})
        with self.assertRaises((ValueError, WorkbenchError)):
            self.record(self.directory, record)

    def test_false_success_cannot_hide_tool_error_or_wrong_response_id(self):
        for response in (
            {"jsonrpc": "2.0", "id": 7, "result": {"isError": True, "content": []}},
            {"jsonrpc": "2.0", "id": 8, "result": {"content": []}},
        ):
            with self.subTest(response=response):
                with self.assertRaises((ValueError, WorkbenchError)):
                    self.record(self.directory, call_record(response, outcome="success"))

    def test_absent_response_is_explicit_incomplete_and_never_success(self):
        record = call_record()
        record["response"].update(bytes=0, sha256=hashlib.sha256(b"").hexdigest(),
                                  retained_base64="", truncated=False, outcome="incomplete")
        receipt = self.record(self.directory, record)
        self.assertEqual(receipt["outcome"], "partial")
        self.assertEqual((self.directory / "mcp-response.bin").read_bytes(), b"")

    def test_request_credential_values_are_omitted_from_identity_and_receipt(self):
        record = call_record()
        record["request"]["params"]["arguments"].update(api_key="private-first", password="private-password")
        record["request"]["params"]["arguments"].update(
            apiKey="private-camel-api", accessToken="private-access-token", headers={
                "Authorization": "private-auth-header", "Proxy-Authorization": "private-proxy-auth"})
        record["provider"]["apiKey"] = "private-provider-key"
        first = self.record(self.directory, record)
        folder = self.directory / "second"
        folder.mkdir()
        record["request"]["params"]["arguments"]["api_key"] = "private-second"
        record["request"]["params"]["arguments"].update(apiKey="rotated-camel-api", accessToken="rotated-access-token",
            headers={"Authorization": "rotated-auth-header", "Proxy-Authorization": "rotated-proxy-auth"})
        record["provider"]["apiKey"] = "rotated-provider-key"
        second = self.record(folder, record)
        self.assertEqual(first["request_identity_sha256"], second["request_identity_sha256"])
        for path in (self.directory / "mcp-call.json", folder / "mcp-call.json"):
            text = path.read_text()
            self.assertNotIn("private-first", text)
            self.assertNotIn("private-second", text)
            self.assertNotIn("private-password", text)
            for secret in ("private-camel-api", "private-access-token", "private-auth-header", "private-proxy-auth",
                           "private-provider-key", "rotated-camel-api", "rotated-access-token", "rotated-auth-header",
                           "rotated-proxy-auth", "rotated-provider-key"):
                self.assertNotIn(secret, text)
        self.assertNotIn("private-provider-key", json.dumps(first))
        self.assertNotIn("rotated-provider-key", json.dumps(second))


class McpReceiptServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="awb-mcp-service-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "workbench.project.json").write_text(json.dumps(
            {"version": 1, "python": sys.executable, "operations": {}}))
        self.state = self.root / "state"
        self.state.mkdir()
        atomic_json(self.state / "config.json", {**DEFAULTS, "test_mode": True,
                                                 "min_free_bytes": 0, "devices": {}})
        self.log = (self.root / "service.log").open("wb")
        self.process = subprocess.Popen([sys.executable, str(ROOT / "scripts/workbench.py"),
                                         "--state", str(self.state), "serve"], stdout=self.log, stderr=self.log)
        self.leases = []
        self.addCleanup(self.stop_service)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                rpc(self.state, "service.capabilities")
                break
            except WorkbenchError:
                time.sleep(0.03)
        else:
            self.fail("Temporary MCP receipt service did not start")
        self.owner = Client(self.project, self.state, "owner")
        self.other = Client(self.project, self.state, "other")

    def stop_service(self):
        for identifier in self.leases:
            try:
                self.owner.call("leases.release", {"id": identifier})
                self.owner.wait(identifier, 5)
            except (WorkbenchError, TimeoutError):
                pass
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()
        self.temporary.cleanup()

    def lease(self, mode="analysis"):
        job = self.owner.submit(operation="host.lease", lease_owner=process_identity(os.getpid()),
                                lease_mode=mode, timeout=20)
        self.leases.append(job["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.owner.call("jobs.status", {"id": job["id"]})["state"] == "running":
                return job
            time.sleep(0.03)
        self.fail("MCP fixture lease did not start")

    def test_receipt_requires_owned_running_analysis_lease(self):
        job = self.lease()
        params = {"id": job["id"], "record": call_record()}
        with self.assertRaises(WorkbenchError):
            self.other.call("leases.record_mcp", params)
        receipt = self.owner.call("leases.record_mcp", params)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.owner.call("leases.release", {"id": job["id"]})
        done = self.owner.wait(job["id"], 5)
        self.assertEqual(done["state"], "succeeded", done)
        with self.assertRaises(WorkbenchError):
            self.owner.call("leases.record_mcp", params)
        environment = self.lease("environment")
        with self.assertRaises(WorkbenchError):
            self.owner.call("leases.record_mcp", {"id": environment["id"], "record": call_record()})

    def test_tool_error_retains_raw_results_and_marks_partial(self):
        job = self.lease()
        record = call_record({"jsonrpc": "2.0", "id": 7,
                              "result": {"content": [], "isError": True}}, "tool_error")
        self.owner.call("leases.record_mcp", {"id": job["id"], "record": record})
        self.owner.call("leases.release", {"id": job["id"]})
        done = self.owner.wait(job["id"], 5)
        self.assertEqual(done["state"], "partial", done)
        folder = self.state / "jobs" / job["id"]
        evidence = json.loads((folder / "analysis-evidence.json").read_text())
        names = {Path(item["path"]).name for item in evidence["raw_results"]}
        self.assertTrue({"mcp-call.json", "mcp-response.bin"}.issubset(names))
        self.assertFalse(evidence["cache_reusable"])
        self.assertEqual(evidence["coverage"]["semantic_review"], "required")

    def test_modified_receipt_cannot_pass_on_lease_completion(self):
        job = self.lease()
        self.owner.call("leases.record_mcp", {"id": job["id"], "record": call_record()})
        folder = self.state / "jobs" / job["id"]
        (folder / "mcp-response.bin").write_bytes(b"substituted response")
        self.owner.call("leases.release", {"id": job["id"]})
        done = self.owner.wait(job["id"], 5)
        self.assertNotEqual(done["state"], "succeeded", done)

    def test_actual_proxy_provider_and_service_publish_bound_call_results(self):
        provider = self.project / "fake_provider.py"
        provider.write_text('''import json, sys
for line in sys.stdin:
    value = json.loads(line)
    if value.get("method") == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fixture-provider", "version": "1.2"}}
    elif value.get("method") == "tools/call":
        mode = value["params"]["name"]
        if mode == "disconnect":
            break
        result = {"content": [{"type": "text", "text": "x" * 262244 if mode == "large" else "fixture result"}]}
        if mode == "error":
            result["isError"] = True
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": value["id"], "result": result}), flush=True)
''')
        for mode, expected, response_outcome in (("success", "succeeded", "success"),
                                                 ("error", "partial", "tool_error"),
                                                 ("disconnect", "partial", "incomplete"),
                                                 ("large", "partial", "success")):
            with self.subTest(mode=mode):
                before = {job["id"] for job in self.owner.call("jobs.list")}
                proxy = subprocess.Popen([sys.executable, str(ROOT / "scripts/workbench.py"),
                                          "--state", str(self.state), "--project", str(self.project),
                                          "mcp-proxy", "--", sys.executable, str(provider)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                    proxy.stdin.write(json.dumps(initialize).encode() + b"\n")
                    proxy.stdin.flush()
                    self.assertTrue(select.select([proxy.stdout], [], [], 10)[0], "Proxy initialize timed out")
                    self.assertEqual(json.loads(proxy.stdout.readline())["result"]["serverInfo"]["version"], "1.2")
                    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": mode, "arguments": {"query": "fixture"}}}
                    proxy.stdin.write(json.dumps(request).encode() + b"\n")
                    proxy.stdin.flush()
                    self.assertTrue(select.select([proxy.stdout], [], [], 10)[0], "Proxy tools/call timed out")
                    response = proxy.stdout.readline()
                    if mode == "disconnect":
                        self.assertEqual(response, b"")
                    else:
                        self.assertEqual(json.loads(response)["id"], 7)
                    proxy.stdin.close()
                    proxy.wait(timeout=10)
                    self.assertEqual(proxy.returncode, 0, proxy.stderr.read().decode())
                finally:
                    if proxy.poll() is None:
                        if not proxy.stdin.closed:
                            proxy.stdin.close()
                        try:
                            proxy.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proxy.terminate()
                            proxy.wait(timeout=3)
                    proxy.stdout.close()
                    proxy.stderr.close()
                jobs = [job for job in self.owner.call("jobs.list") if job["id"] not in before
                        and job["operation"] == "host.lease" and "Analysis MCP tool" in job["purpose"]]
                self.assertEqual(len(jobs), 1, jobs)
                done = self.owner.wait(jobs[0]["id"], 5)
                self.assertEqual(done["state"], expected, done)
                folder = self.state / "jobs" / done["id"]
                receipt = json.loads((folder / "mcp-call.json").read_text())
                self.assertEqual(receipt["response"]["outcome"], response_outcome)
                self.assertEqual(receipt["provider"]["server_info"], {"name": "fixture-provider", "version": "1.2"})
                self.assertLessEqual((folder / "mcp-response.bin").stat().st_size, RETAINED_BYTES)
                evidence = json.loads((folder / "analysis-evidence.json").read_text())
                results = {Path(item["path"]).name: item for item in evidence["raw_results"]}
                self.assertTrue({"mcp-call.json", "mcp-response.bin"}.issubset(results))
                for name in ("mcp-call.json", "mcp-response.bin"):
                    self.assertEqual(results[name]["sha256"], file_hash(folder / name))
                self.assertEqual(evidence["execution_identity"]["external_call"]["request_identity_sha256"], receipt["request_identity_sha256"])
                self.assertFalse(evidence["cache_reusable"])


if __name__ == "__main__":
    unittest.main()
