import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from scripts.audit_behavior_report import SCHEMA, audit


class BehaviorReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "code.txt").write_text("invoke startActivity\nfinish\n")
        self.fact = {"fact_id": "popup", "value": "Explicit Activity launch", "status": "decoded"}
        self.source = {"id": "s1", "artifact": "code.txt", "sha256": hashlib.sha256((self.root / "code.txt").read_bytes()).hexdigest(),
                       "locator": {"kind": "lines", "start": 1, "end": 1}}
        self.claim = {"id": "popup", "assertion": self.fact["value"], "status": "decoded", "sources": [self.source],
                      "code": {"state": "reviewed", "summary": "Constructs Intent and launches Activity", "source_ids": ["s1"]},
                      "trigger": {"state": "reviewed", "summary": "Caller invokes show after gate", "source_ids": ["s1"]},
                      "flow": {"state": "reviewed", "summary": "show -> Activity", "source_ids": ["s1"],
                               "nodes": [{"id": "show", "source_ids": ["s1"]}, {"id": "activity", "source_ids": ["s1"]}],
                               "edges": [{"from": "show", "to": "activity", "kind": "ICC", "status": "decoded", "summary": "explicit Intent", "source_ids": ["s1"]}]},
                      "review": {"state": "reviewed", "summary": "Checked original instructions", "source_ids": ["s1"]},
                      "runtime": {"state": "pending", "summary": "Natural trigger not exercised"},
                      "limits": ["No display receipt"], "temporal_scope": "fixed sample"}
        self.report = {"schema": SCHEMA, "completion": "partial", "claims": [self.claim]}

    def run_audit(self, declarations=()):
        return audit(self.root, self.report, [self.fact], declarations)

    def test_valid_static_contract_does_not_require_runtime_or_promote_it(self):
        result = self.run_audit()
        self.assertEqual(result["status"], "pass")
        self.assertIn("no automatic semantic", result["scope"])

    def test_source_drift_and_bad_line_are_rejected(self):
        (self.root / "code.txt").write_text("different\n")
        self.source["locator"]["end"] = 2
        result = self.run_audit()
        self.assertEqual(len(result["errors"]), 2)

    def test_missing_edge_provenance_is_rejected(self):
        self.claim["flow"]["edges"][0]["source_ids"] = []
        self.assertEqual(self.run_audit()["status"], "invalid")

    def test_pending_fields_cannot_be_complete(self):
        self.claim["trigger"] = {"state": "pending", "summary": "Caller not found"}
        self.assertEqual(self.run_audit()["status"], "partial")
        self.report["completion"] = "complete"
        self.assertEqual(self.run_audit()["status"], "invalid")

    def test_observation_promotion_requires_explicit_fact_revision(self):
        self.claim["status"] = "observed"
        self.assertEqual(self.run_audit()["status"], "invalid")

    def test_negative_observation_requires_bounded_window(self):
        self.fact["status"] = self.claim["status"] = "observed"
        self.claim["runtime"] = {"state": "observed", "summary": "No restart sampled", "source_ids": ["s1"],
                                 "conditions": {"home": "other launcher"}, "trigger_mode": "controlled_force_stop", "negative_observation": True}
        self.assertEqual(self.run_audit()["status"], "invalid")
        for window in (0, -1, True, "10000", float("nan"), float("inf")):
            with self.subTest(window=window):
                self.claim["runtime"]["window_ms"] = window
                self.assertEqual(self.run_audit()["status"], "invalid")
        self.claim["runtime"]["window_ms"] = 10000
        self.assertEqual(self.run_audit()["status"], "pass")

    def test_json_pointer_and_path_containment(self):
        p = self.root / "result.json"
        p.write_text(json.dumps({"a/b": [True]}))
        self.source.update(artifact=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest(), locator={"kind": "json_pointer", "pointer": "/a~1b/0"})
        self.assertEqual(self.run_audit()["status"], "pass")
        self.source["locator"]["pointer"] = "/missing"
        self.assertEqual(self.run_audit()["status"], "invalid")
        self.source["artifact"] = "../escape"
        self.assertEqual(self.run_audit()["status"], "invalid")

    def test_dex_identity_offset_hash_and_case_scope(self):
        data = bytearray(64)
        struct.pack_into("<I", data, 28, 2)
        data[32:36] = b"\x01\x00\x0e\x00"
        p = self.root / "original.dex"
        p.write_bytes(data)
        m = {"dex_id": "dex_a", "class": "Lsample/A;", "name": "show", "descriptor": "()V", "code_offset": 16,
             "instruction_bytes_sha256": hashlib.sha256(data[32:36]).hexdigest()}
        declaration = {k: v for k, v in m.items() if k != "instruction_bytes_sha256"}
        declaration.update(source=p.name, source_sha256=hashlib.sha256(data).hexdigest())
        self.source["locator"] = {"kind": "bytecode", "method": m, "scope": "method"}
        self.assertEqual(self.run_audit([declaration])["status"], "pass")
        m["dex_id"] = "dex_b"
        self.assertEqual(self.run_audit([declaration])["status"], "invalid")
        m["dex_id"] = "dex_a"
        m["code_offset"] = 18
        self.assertEqual(self.run_audit([declaration])["status"], "invalid")
        m["code_offset"] = 16
        m["instruction_bytes_sha256"] = "0" * 64
        self.assertEqual(self.run_audit([declaration])["status"], "invalid")
        m["instruction_bytes_sha256"] = hashlib.sha256(data[32:36]).hexdigest()
        self.source["locator"].update(scope="switch_case")
        self.assertEqual(self.run_audit([declaration])["status"], "invalid")
        self.source["locator"].update(selector="tag=12; default", dispatch_pc="0008", target_pc="000b", payload_source_ids=["s1"])
        self.assertEqual(self.run_audit([declaration])["status"], "pass")

    def test_entire_fact_registry_must_be_covered(self):
        self.report["claims"] = []
        self.assertEqual(self.run_audit()["status"], "invalid")

    def test_valid_unrelated_file_cannot_replace_canonical_source(self):
        self.fact["sources"] = [{"artifact": self.source["artifact"], "sha256": self.source["sha256"]}]
        replacement = self.root / "unrelated.txt"
        replacement.write_text("unrelated but hash-valid\n")
        self.source.update(artifact=replacement.name, sha256=hashlib.sha256(replacement.read_bytes()).hexdigest())
        result = self.run_audit()
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("canonical source" in error["message"] for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
