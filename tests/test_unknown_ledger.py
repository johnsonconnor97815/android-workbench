"""Unknown question revisions, concurrent writers and changed resolution files."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/android-workbench/components/analysis-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
from analysis_ledger import check_unknowns, record_unknown, update_unknown
from analysis_workflow import check_plan, create_plan, update_plan


class UnknownLedgerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / "unknowns.json"
        self.evidence = self.root / "evidence.txt"
        self.evidence.write_text("controlled fixture evidence")

    def test_stable_id_history_and_required_scope(self):
        first = record_unknown(self.ledger, "  Does  the guard run? ", True)
        same = record_unknown(self.ledger, "Does the guard run?", True)
        self.assertEqual(first, same)
        record_unknown(self.ledger, "Optional follow-up", False)
        self.assertFalse(check_unknowns(self.ledger)["complete"])
        resolved = update_unknown(self.ledger, first["id"], 1, "resolved", "Fixture supports the reviewed answer", [self.evidence])
        self.assertEqual(resolved["revision"], 2)
        self.assertEqual([item["revision"] for item in resolved["history"]], [1, 2])
        self.assertEqual(resolved["history"][1]["previous_revision_digest"], resolved["history"][0]["revision_digest"])
        self.assertTrue(check_unknowns(self.ledger)["complete"])

    def test_resolution_requires_reason_and_evidence(self):
        first = record_unknown(self.ledger, "Question", True)
        for reason, paths in (("", [self.evidence]), ("Resolved", [])):
            with self.assertRaises(ValueError):
                update_unknown(self.ledger, first["id"], 1, "resolved", reason, paths)
        self.assertEqual(json.loads(self.ledger.read_text())["questions"][0]["revision"], 1)

    def test_changed_deleted_evidence_marks_resolved_stale(self):
        first = record_unknown(self.ledger, "Question", True)
        update_unknown(self.ledger, first["id"], 1, "resolved", "Reviewed", [self.evidence])
        self.evidence.write_text("replacement evidence")
        result = check_unknowns(self.ledger)
        self.assertFalse(result["complete"])
        self.assertEqual(result["questions"][0]["effective_status"], "stale")
        self.evidence.unlink()
        self.assertFalse(check_unknowns(self.ledger)["complete"])

    def test_two_processes_from_same_revision_have_one_winner(self):
        first = record_unknown(self.ledger, "Question", True)
        command = [sys.executable, str(SCRIPTS / "analysis_ledger.py"), "unknown-update", "--ledger", str(self.ledger),
                   "--id", first["id"], "--expected-revision", "1", "--status", "resolved", "--reason", "Reviewed",
                   "--evidence", str(self.evidence)]
        writers = [subprocess.Popen([*command, "--output", str(self.root / (str(index) + ".json"))],
                                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for index in range(2)]
        outputs = [writer.communicate(timeout=15) for writer in writers]
        self.assertEqual(sorted(writer.returncode for writer in writers), [0, 1], outputs)
        self.assertTrue(any("revision conflict" in stdout for stdout, _ in outputs))
        self.assertEqual(len(json.loads(self.ledger.read_text())["questions"][0]["history"]), 2)

    def test_tampered_history_is_rejected(self):
        record_unknown(self.ledger, "Question", True)
        data = json.loads(self.ledger.read_text())
        data["questions"][0]["history"][0]["reason"] = "changed"
        self.ledger.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "history"):
            check_unknowns(self.ledger)

    def test_plan_only_blocks_required_unknown_and_old_plan_still_works(self):
        # A static plan without APK is invalid; this minimal runtime plan uses an
        # authenticated-verifier fixture, then exercises only ledger integration.
        recon = {"schema": "1", "samples": []}
        plan = create_plan("Observe fixture", recon, {"mode": "device_runtime"})
        record = self.root / "runtime.json"
        record.write_text(json.dumps({"runtime_verified": True, "device": "fixture", "job_id": "fixture-job",
                                      "observation_window": {"started_at": 1, "finished_at": 2},
                                      "observations": [{"kind": "file", "path": str(self.evidence),
                                                        "sha256": __import__("hashlib").sha256(self.evidence.read_bytes()).hexdigest()}]}))
        verifier = lambda params: {"verified": True}
        update_plan(plan, "runtime", "completed", [record], "fixture", verifier)
        review = self.root / "review.json"
        review.write_text(json.dumps({"review_complete": True, "claims": [{"claim": "Fixture retained", "evidence": [str(self.evidence)]}], "limitations": []}))
        update_plan(plan, "review", "completed", [review], "Reviewed", verifier)
        report = self.root / "report.md"
        report.write_text("Fixture only")
        update_plan(plan, "report", "completed", [report], "Reported", verifier)
        self.assertTrue(check_plan(plan, verifier)["complete"])
        record_unknown(self.ledger, "Optional", False)
        plan["unknown_ledger"] = {"path": str(self.ledger)}
        self.assertTrue(check_plan(plan, verifier)["complete"])
        first = record_unknown(self.ledger, "Required", True)
        self.assertFalse(check_plan(plan, verifier)["complete"])
        update_unknown(self.ledger, first["id"], 1, "resolved", "Reviewed", [self.evidence])
        self.assertTrue(check_plan(plan, verifier)["complete"])

    def test_cli_cannot_overwrite_ledger_as_output(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "analysis_ledger.py"), "unknown-record", "--ledger", str(self.ledger),
                                 "--question", "Question", "--output", str(self.ledger)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
