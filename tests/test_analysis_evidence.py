"""Offline checks of declared-input evidence; no underlying-engine proof."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workbench.analysis_evidence import executable_identity, make_evidence, verify_evidence
from workbench.common import WorkbenchError, file_hash


class AnalysisEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spec = {"operation": "analysis.decompile", "directory": str(self.root),
                     "script": "adapter.py", "source_hash": "adapter-a",
                     "source_manifest": {"adapter.py": "adapter-a"}, "args": ["--mode", "a"],
                     "env": {"MODE": "a", "API_KEY": "private-a"}, "timeout": 60,
                     "input_fingerprints": {"sample.apk": "sample-a"}}
        self.result = {"state": "succeeded", "cleanup_ok": True, "uncertain": False,
                       "exit_code": 0, "reason": None}
        self.artifacts = {"job": "job-a", "files": [{"name": "stdout.log", "sha256": "raw-a", "bytes": 10}],
                          "external_outputs": []}

    def value(self, spec=None, result=None):
        return make_evidence(spec or self.spec, result or self.result, self.artifacts)

    def test_stable_content_identity_and_config_source_input_changes(self):
        original = self.value()
        self.assertEqual(self.value()["evidence_id"], original["evidence_id"])
        for key, value in (("args", ["--mode", "b"]), ("source_hash", "adapter-b"),
                           ("input_fingerprints", {"sample.apk": "sample-b"}),
                           ("nested_source_manifests", {"helpers": {"helper.py": "helper-b"}})):
            with self.subTest(key=key):
                changed = {**self.spec, key: value}
                self.assertNotEqual(self.value(changed)["execution_identity_sha256"],
                                    original["execution_identity_sha256"])

    def test_credential_values_are_not_part_of_identity_or_serialized(self):
        original = self.value()
        changed = copy.deepcopy(self.spec)
        changed["env"]["API_KEY"] = "private-b"
        self.assertEqual(original["execution_identity_sha256"], self.value(changed)["execution_identity_sha256"])
        serialized = json.dumps(self.value(changed))
        self.assertNotIn("private-a", serialized)
        self.assertNotIn("private-b", serialized)
        changed["args"] = ["--api-key", "private-b", "--token=private-c"]
        first = self.value(changed)["execution_identity_sha256"]
        changed["args"] = ["--api-key", "private-d", "--token=private-e"]
        self.assertEqual(first, self.value(changed)["execution_identity_sha256"])

    def test_partial_output_and_unknown_engine_never_claim_exact_cache(self):
        value = self.value(result={**self.result, "state": "failed", "exit_code": 2})
        self.assertTrue(value["partial_observation"])
        self.assertFalse(value["cache_reusable"])
        self.assertEqual(value["coverage"]["external_engine_identity"], "unknown")
        self.assertEqual(value["coverage"]["semantic_review"], "required")
        self.assertEqual(verify_evidence(value), value)
        value["outcome"]["state"] = "succeeded"
        with self.assertRaisesRegex(WorkbenchError, "identity differs"):
            verify_evidence(value)
        self.artifacts["files"][0]["bytes"] = 0
        self.assertFalse(self.value(result={**self.result, "state": "failed"})["partial_observation"])

    def test_explicit_environment_without_path_uses_exec_default(self):
        with mock.patch("workbench.analysis_evidence.shutil.which", return_value=None) as which:
            executable_identity("some-tool", env={"MODE": "test"}, cwd=self.root)
        self.assertEqual(which.call_args.kwargs["path"], os.defpath)

    def test_relative_executable_and_path_match_child_working_directory(self):
        directory = self.root / "tools"
        directory.mkdir()
        executable = directory / "tool"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        identity = executable_identity("tool", env={"PATH": "tools"}, cwd=self.root)
        self.assertEqual(identity["path"], str(executable))
        self.assertEqual(identity["sha256"], file_hash(executable))
        self.assertEqual(executable_identity("tools/tool", cwd=self.root)["sha256"], identity["sha256"])
        self.assertEqual(executable_identity("absent", env={"PATH": "tools"}, cwd=self.root)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
