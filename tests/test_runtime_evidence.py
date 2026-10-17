"""Synthetic trusted-job qualification; no device execution."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workbench.runtime_evidence import verify_runtime, MAX_OBSERVATION_BYTES, MAX_TOTAL_OBSERVATION_BYTES, MAX_METADATA_BYTES
from workbench.analysis_evidence import make_evidence


class RuntimeEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.image = self.directory / "screen.png"
        self.image.write_bytes(b"synthetic observation")
        self.sha = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.job = {"id": "trusted-job", "owner": "private-owner-token-hash", "project": "/project",
                    "state": "succeeded", "started": 10.0, "finished": 20.0,
                    "spec": {"project": "/project", "operation": "device.screenshot", "device": "phone",
                             "serial": "test-only", "directory": str(self.directory)},
                    "result": {"state": "succeeded", "cleanup_ok": True, "cleanup_errors": [],
                               "uncertain": False, "reason": None, "exit_code": None, "finished_at": 19.0}}
        self.manifest = {"job": "trusted-job", "operation": "device.screenshot", "device": "phone",
                         "quality": "succeeded", "files": [{"name": "screen.png", "sha256": self.sha,
                                                               "bytes": self.image.stat().st_size}], "external_outputs": []}
        self.params = {"job_id": "trusted-job", "device": "phone",
                       "observation_window": {"started_at": 10.0, "finished_at": 19.0},
                       "observations": [{"kind": "ui", "path": str(self.image), "sha256": self.sha}]}
        self.bind()

    def bind(self):
        evidence = make_evidence(self.job["spec"], self.job["result"], self.manifest)
        path = self.directory / "analysis-evidence.json"
        path.write_text(json.dumps(evidence))
        self.job["result"]["analysis_evidence"] = {
            "name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size, "evidence_id": evidence["evidence_id"]}

    def test_internal_and_external_published_files_pass_without_owner_secret(self):
        result = verify_runtime(self.job, self.params, self.manifest)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["window_scope"], "job_execution")
        self.assertNotIn(self.job["owner"], json.dumps(result))
        self.manifest["external_outputs"] = [{"path": str(self.directory), "files": [
            {"path": str(self.image), "sha256": self.sha, "bytes": self.image.stat().st_size}]}]
        self.manifest["files"] = []
        self.assertTrue(verify_runtime(self.job, self.params, self.manifest)["verified"])

    def test_false_identity_device_and_project_are_rejected(self):
        for change in ({"job_id": "invented"}, {"device": "other"}, {"project": "/other"}):
            with self.subTest(change=change):
                params = {**self.params, **change}
                self.assertFalse(verify_runtime(self.job, params, self.manifest)["verified"])
        self.job["spec"]["project"] = "/other"
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])

    def test_nonfinal_failed_partial_and_cleanup_unknown_are_rejected(self):
        for state in ("running", "failed", "cancelled", "partial", "needs_recovery"):
            with self.subTest(state=state):
                job = {**self.job, "state": state}
                self.assertFalse(verify_runtime(job, self.params, self.manifest)["verified"])
        for change in ({"cleanup_ok": False}, {"cleanup_ok": None}, {"uncertain": True},
                       {"uncertain": None}, {"cleanup_errors": ["unload failed"]},
                       {"reason": "failure"}, {"state": "partial"}, {"exit_code": 2}):
            with self.subTest(change=change):
                job = {**self.job, "result": {**self.job["result"], **change}}
                self.assertFalse(verify_runtime(job, self.params, self.manifest)["verified"])

    def test_static_host_and_unobserved_scene_are_rejected(self):
        for operation in ("analysis.exec", "static.snapshot", "host.lease", "test.simulate", "device.recover", "analysis.decompile"):
            with self.subTest(operation=operation):
                job = copy.deepcopy(self.job)
                job["spec"].update(operation=operation, entry={"serial": "--serial"})
                self.assertFalse(verify_runtime(job, self.params, self.manifest)["verified"])
        self.job["spec"].update(operation="device.scene", steps=[{"action": "wait"}])
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])

    def test_window_must_be_finite_and_inside_actual_execution(self):
        for begin, end in ((9, 18), (11, 19.1), (18, 11), (True, 18), (11, float("nan")), (10, 10.1), (11, 18)):
            with self.subTest(begin=begin, end=end):
                params = {**self.params, "observation_window": {"started_at": begin, "finished_at": end}}
                self.assertFalse(verify_runtime(self.job, params, self.manifest)["verified"])
        self.job["started"] = None
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])

    def test_missing_changed_and_duplicate_publication_are_rejected(self):
        self.assertFalse(verify_runtime(self.job, self.params, {**self.manifest, "files": []})["verified"])
        self.manifest["files"].append(dict(self.manifest["files"][0]))
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])
        self.manifest["files"].pop()
        self.image.write_bytes(b"changed")
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])
        self.image.unlink()
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])

    def test_relative_symlink_and_missing_sha_are_rejected(self):
        for path in ("screen.png", str(self.directory / ".." / self.directory.name / "screen.png")):
            params = copy.deepcopy(self.params)
            params["observations"][0]["path"] = path
            self.assertFalse(verify_runtime(self.job, params, self.manifest)["verified"])
        link = self.directory / "link.png"
        link.symlink_to(self.image)
        self.params["observations"][0]["path"] = str(link)
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])
        del self.params["observations"][0]["sha256"]
        self.assertFalse(verify_runtime(self.job, self.params, self.manifest)["verified"])

    def test_reads_publication_only_from_trusted_job_directory(self):
        (self.directory / "artifacts.json").write_text(json.dumps(self.manifest))
        self.assertTrue(verify_runtime(self.job, self.params)["verified"])
        self.params["state_directory"] = str(self.directory)
        self.assertFalse(verify_runtime(self.job, self.params)["verified"])

    def test_added_manifest_observation_cannot_create_trusted_binding(self):
        fake = self.directory / "unrelated.txt"
        fake.write_bytes(b"not produced by the trusted job")
        sha = hashlib.sha256(fake.read_bytes()).hexdigest()
        self.manifest["external_outputs"] = [{"path": str(fake), "files": [
            {"path": str(fake), "sha256": sha, "bytes": fake.stat().st_size}]}]
        self.params["observations"] = [{"kind": "state", "path": str(fake), "sha256": sha}]
        result = verify_runtime(self.job, self.params, self.manifest)
        self.assertFalse(result["verified"])
        self.assertIn("not bound", result["issues"][0])

    def test_sidecar_substitution_and_legacy_job_are_rejected(self):
        path = self.directory / "analysis-evidence.json"
        data = json.loads(path.read_text())
        data["job_id"] = "different-job"
        path.write_text(json.dumps(data))
        result = verify_runtime(self.job, self.params, self.manifest)
        self.assertFalse(result["verified"])
        self.assertIn("trusted job reference", result["issues"][0])
        del self.job["result"]["analysis_evidence"]
        self.assertIn("legacy", verify_runtime(self.job, self.params, self.manifest)["issues"][0])

    def test_overbudget_rejects_before_any_hash_without_partial_pass(self):
        with self.image.open("r+b") as stream:
            stream.truncate(MAX_OBSERVATION_BYTES + 1)
        with mock.patch("workbench.runtime_evidence._hash_observation") as hashed:
            result = verify_runtime(self.job, self.params, self.manifest)
            hashed.assert_not_called()
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "resource_limit")
        self.assertNotIn("observations", result)

    def test_total_budget_and_metadata_budget_are_bounded(self):
        observations = []
        for ordinal in range(3):
            path = self.directory / ("large-" + str(ordinal))
            with path.open("wb") as stream:
                stream.truncate(MAX_TOTAL_OBSERVATION_BYTES // 3 + 1)
            observations.append({"kind": "file", "path": str(path), "sha256": self.sha})
        self.params["observations"] = observations
        with mock.patch("workbench.runtime_evidence._hash_observation") as hashed:
            result = verify_runtime(self.job, self.params, self.manifest)
            hashed.assert_not_called()
        self.assertEqual(result["reason"], "resource_limit")
        self.params["observations"] = [{"kind": "ui", "path": str(self.image), "sha256": self.sha}]
        with (self.directory / "analysis-evidence.json").open("wb") as stream:
            stream.truncate(MAX_METADATA_BYTES + 1)
        self.assertEqual(verify_runtime(self.job, self.params, self.manifest)["reason"], "resource_limit")

    def test_metadata_cannot_substitute_for_observation(self):
        reference = self.job["result"]["analysis_evidence"]
        self.params["observations"] = [{"kind": "state", "path": str(self.directory / reference["name"]),
                                         "sha256": reference["sha256"]}]
        self.assertIn("not an observation", verify_runtime(self.job, self.params, self.manifest)["issues"][0])


if __name__ == "__main__":
    unittest.main()
