"""Evidence guards for the historical read-only recovery refusal."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from workbench.common import atomic_json, file_hash, process_identity

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host_recovery", ROOT / "scripts/recover_host_lease.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ReadonlyRefusalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.copied = self.folder / "source/recover_host_lease.py"
        self.copied.parent.mkdir()
        self.copied.write_text("# isolated source fixture\n")
        self.hash = file_hash(self.copied)
        self.patch = patch.object(helper, "READONLY_REFUSAL_SOURCE", self.hash)
        self.patch.start()
        output = self.folder / "output/result.json"
        atomic_json(output, {"pass": False, "checked_jobs": [],
                            "cleanup_errors": [helper.TREE_CLOSURE_REFUSAL],
                            "reconciled_without_release": ["ancestor"]})
        self.output = output
        self.publish()
        identity = {**process_identity(os.getpid()), "boot": str(uuid.uuid4()), "group": os.getpid()}
        atomic_json(self.folder / "active-process.json", {"identity": identity})
        self.normalized = {
            "readonly": True, "recovery": True, "source_hash": self.hash,
            "script_copy": str(self.copied), "source_manifest": {"recover_host_lease.py": self.hash},
            "outputs": [str(output.parent)], "entry": {"result_file": "result.json"},
            "recovery_targets": ["ancestor"],
        }
        self.result = {"exit_code": 1, "functional_result": "failed", "reason": None,
                       "cleanup_errors": [helper.TREE_CLOSURE_REFUSAL]}

    def publish(self):
        atomic_json(self.folder / "artifacts.json", {"external_outputs": [{"files": [
            {"path": str(self.output), "sha256": file_hash(self.output)}]}]})

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def verify(self):
        helper.verify_readonly_refusal(self.folder, self.normalized, self.result)

    def test_verified_refusal_is_recognized(self):
        self.verify()

    def test_changed_source_is_rejected(self):
        self.copied.write_text("raise Exception('changed')\n")
        with self.assertRaisesRegex(RuntimeError, "verified read-only checker"):
            self.verify()

    def test_changed_other_snapshot_file_is_rejected(self):
        other = self.copied.parent / "other.py"
        other.write_text("old\n")
        self.normalized["source_manifest"]["other.py"] = file_hash(other)
        other.write_text("new\n")
        with self.assertRaisesRegex(RuntimeError, "source snapshot changed"):
            self.verify()

    def test_changed_published_result_is_rejected(self):
        self.output.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "published evidence"):
            self.verify()

    def test_partial_work_is_rejected_even_with_matching_published_hash(self):
        data = json.loads(self.output.read_text())
        data["checked_jobs"] = ["ancestor"]
        atomic_json(self.output, data)
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "inconsistent"):
            self.verify()

    def test_live_or_unknown_process_group_is_rejected(self):
        for identity in (process_identity(os.getpid()), None):
            with self.subTest(identity=identity):
                atomic_json(self.folder / "active-process.json", {"identity": identity})
                with self.assertRaisesRegex(RuntimeError, "alive or unknown"):
                    self.verify()

    def test_interrupted_refusal_is_rejected(self):
        self.result["exit_code"] = -15
        with self.assertRaisesRegex(RuntimeError, "verified refusal"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
