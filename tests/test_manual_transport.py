"""Offline contract checks for an OTA prepare record used in manual handover.

All phones, images and reviews here are synthetic. Passing these tests does not
establish transport support or flashing success on a real device.
"""

import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.common import WorkbenchError, file_hash
from workbench import results
from workbench.registry import normalize
from workbench.worker import Runner
import test_flash_safety as safety
from flash_review_fixture import approve


class ManualOtaPrepareRecordTest(unittest.TestCase):
    def setUp(self):
        safety.FlashSafetyTest.setUp(self)
        self.research["decisions"].update(scope="ota", data="keep", reboot="yes")
        self.research["flash_plan"].update(mode="recovery", target_slot="b")
        self.research["flash_plan"].pop("partition_writes", None)
        self.research["flash_plan"]["images"] = {"ota": file_hash(self.image)}
        approve(self)
        self.folder = self.root / ("a" * 32)
        self.folder.mkdir()
        self.record_path = self.root / "synthetic-ota-prepare.json"
        self.spec = {
            "operation": "device.recover",
            "serial": "test-phone",
            "device": "offline-fixture-phone",
            "project": str(self.root),
            "transport": "sideload",
            "prepared_job": self.folder.name,
        }
        self.normalized = {
            "operation": "device_manager.install_ota",
            "serial": self.spec["serial"],
            "device": self.spec["device"],
            "project": self.spec["project"],
            "args": ["install-ota", "--serial", "test-phone", "--stage", "prepare"],
            "outputs": [str(self.record_path)],
        }
        self.outcome = {
            "state": "succeeded",
            "functional_result": "succeeded",
            "cleanup_ok": True,
            "cleanup_errors": [],
            "uncertain": False,
            "exit_code": 0,
        }
        self.record = {
            "status": "complete",
            "command": "install-ota",
            "stage": "prepare",
            "flash_phase": "awaiting_sideload",
            "device": "test-phone",
            "research": copy.deepcopy(self.research),
            "flash_plan": copy.deepcopy(self.research["flash_plan"]),
        }
        self.publish_record()
        self.write_json("normalized.json", self.normalized)
        self.write_json("result.json", self.outcome)

    def write_json(self, name, document):
        (self.folder / name).write_text(json.dumps(document))

    def publish_record(self):
        self.record_path.write_text(json.dumps(self.record))
        self.artifacts = {
            "external_outputs": [{
                "path": str(self.record_path),
                "files": [{"path": str(self.record_path), "sha256": file_hash(self.record_path)}],
            }],
        }
        self.write_json("artifacts.json", self.artifacts)

    def verify(self):
        return results.verify_ota_prepare_handover(self.folder, self.spec)

    def test_successful_reviewed_prepare_record_requires_no_device_commands(self):
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            identity = self.verify()
        self.assertIsInstance(identity, dict)
        self.assertTrue(identity)
        run.assert_not_called()
        popen.assert_not_called()

    def test_other_operation_serial_project_or_registered_device_is_rejected(self):
        original = copy.deepcopy(self.normalized)
        for field, wrong in (
            ("operation", "device.recover"),
            ("serial", "different-phone"),
            ("project", str(self.root / "other-project")),
            ("device", "different-registration"),
        ):
            with self.subTest(field=field):
                bad = {**original, field: wrong}
                self.write_json("normalized.json", bad)
                with self.assertRaises(WorkbenchError):
                    self.verify()
        self.write_json("normalized.json", original)

    def test_failed_or_unclean_prepare_cannot_authorize_handover(self):
        for change in ({"state": "failed"}, {"state": "running"}, {"cleanup_ok": False}):
            with self.subTest(change=change):
                self.write_json("result.json", {**self.outcome, **change})
                with self.assertRaises(WorkbenchError):
                    self.verify()
        self.write_json("result.json", self.outcome)

    def test_modified_record_or_wrong_published_digest_is_rejected(self):
        original = self.record_path.read_text()
        self.record_path.write_text(original + " ")
        with self.assertRaises(WorkbenchError):
            self.verify()
        self.record_path.write_text(original)
        self.artifacts["external_outputs"][0]["files"][0]["sha256"] = "0" * 64
        self.write_json("artifacts.json", self.artifacts)
        with self.assertRaises(WorkbenchError):
            self.verify()

    def test_unpublished_record_and_digest_for_another_file_are_rejected(self):
        for files in ([], [{"path": str(self.root / "other-record.json"),
                            "sha256": file_hash(self.record_path)}]):
            with self.subTest(files=files):
                artifacts = copy.deepcopy(self.artifacts)
                artifacts["external_outputs"][0]["files"] = files
                self.write_json("artifacts.json", artifacts)
                with self.assertRaises(WorkbenchError):
                    self.verify()

    def test_record_must_be_complete_prepare_awaiting_sideload_on_this_serial(self):
        original = copy.deepcopy(self.record)
        for field, wrong in (
            ("status", "dry_run"),
            ("command", "install-rom"),
            ("stage", "sideload"),
            ("flash_phase", "awaiting_verification"),
            ("device", "another-phone"),
        ):
            with self.subTest(field=field):
                self.record = {**copy.deepcopy(original), field: wrong}
                self.publish_record()
                with self.assertRaises(WorkbenchError):
                    self.verify()

    def test_research_plan_must_name_this_serial_and_recovery_mode(self):
        original = copy.deepcopy(self.record)
        for field, wrong in (("serial", "another-phone"), ("mode", "fastboot")):
            with self.subTest(field=field):
                self.record = copy.deepcopy(original)
                self.record["research"]["flash_plan"][field] = wrong
                # Keep the synthetic review valid so rejection proves the
                # serial/mode gate, rather than merely a stale review digest.
                self.research = self.record["research"]
                approve(self)
                self.record["flash_plan"] = copy.deepcopy(self.research["flash_plan"])
                self.publish_record()
                with self.assertRaises(WorkbenchError):
                    self.verify()

    def test_review_must_be_approved_resolved_current_and_bound(self):
        original = copy.deepcopy(self.record)
        for change in ("unapproved", "unresolved", "expired", "binding"):
            with self.subTest(change=change):
                self.record = copy.deepcopy(original)
                review = self.record["research"]["flash_plan"]["device_review"]
                if change == "unapproved":
                    review["status"] = "draft"
                elif change == "unresolved":
                    review["unresolved"] = ["Synthetic unanswered safety question"]
                elif change == "expired":
                    review["expires_at"] = "2020-01-01T00:00:00+00:00"
                else:
                    review["research_sha256"] = "0" * 64
                self.record["flash_plan"] = copy.deepcopy(self.record["research"]["flash_plan"])
                self.publish_record()
                with self.assertRaises(WorkbenchError):
                    self.verify()


class ManualTransportWorkerTest(unittest.TestCase):
    """Exercise only the recovery branch with a mocked device interface."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "return-job"
        self.directory.mkdir()
        self.identity = {
            "prepared_job": "a" * 32,
            "record_path": str(self.root / "synthetic-prepare.json"),
            "record_sha256": "1" * 64,
            "research_sha256": "2" * 64,
        }
        self.spec = {
            "operation": "device.recover",
            "serial": "offline-serial",
            "device": "offline-device",
            "project": str(self.root),
            "manual_return": {"transport": "sideload", **self.identity},
            "recovery_targets": [],
        }
        self.device = Mock()
        self.device.adb.return_value = "sideload"
        self.device.observe.side_effect = AssertionError("Android observation is forbidden in sideload")
        self.runner = SimpleNamespace(
            spec=self.spec, device=self.device, grant={"state": str(self.root)},
            directory=self.directory, recovered_resources=[], reconciled_jobs=[],
        )

    def run_recovery(self, **helper_options):
        with patch("workbench.results.verify_ota_prepare_handover",
                   **({"return_value": self.identity} | helper_options)) as verify:
            Runner.scene(self.runner)
        return verify

    def old_job(self, *, active=False, residual=False):
        identifier = "b" * 32
        folder = self.root / "jobs" / identifier
        folder.mkdir(parents=True)
        (folder / "normalized.json").write_text(json.dumps({
            "operation": "unknown.device.mutation", "device": self.spec["device"],
            "readonly": False, "resources": [],
        }))
        (folder / "result.json").write_text(json.dumps({"cleanup_ok": False}))
        process = {"pid": 999999, "starttime": "synthetic-process"}
        if active:
            (folder / "active-process.json").write_text(json.dumps({"identity": process}))
        if residual:
            (folder / "residual-processes.json").write_text(json.dumps([process]))
        self.spec["recovery_targets"] = [identifier]
        return process

    def test_sideload_return_only_checks_transport_and_does_not_observe_android(self):
        verify = self.run_recovery()
        verify.assert_called_once_with(
            self.root / "jobs" / self.identity["prepared_job"], self.spec)
        self.device.adb.assert_called_once_with("get-state")
        self.device.observe.assert_not_called()
        observation = json.loads((self.directory / "observation.json").read_text())
        self.assertEqual(observation["transport"], "sideload")
        self.assertEqual(observation["serial"], "offline-serial")
        self.assertFalse(observation["system_verified"])

    def test_unknown_selected_transport_is_rejected_before_device_access(self):
        self.spec["manual_return"]["transport"] = "recovery"
        with self.assertRaisesRegex(WorkbenchError, "sideload mode"):
            self.run_recovery()
        self.device.adb.assert_not_called()
        self.device.observe.assert_not_called()
        self.assertFalse((self.directory / "observation.json").exists())

    def test_unexpected_observed_states_never_publish_success(self):
        for state in ("recovery", "device", "offline", "", "unauthorized"):
            with self.subTest(state=state):
                self.device.adb.reset_mock()
                self.device.adb.return_value = state
                with self.assertRaisesRegex(WorkbenchError, "sideload mode"):
                    self.run_recovery()
                self.device.adb.assert_called_once_with("get-state")
                self.device.observe.assert_not_called()
                self.assertFalse((self.directory / "observation.json").exists())

    def test_binding_changed_between_acceptance_and_execution_is_rejected(self):
        for key in ("record_sha256", "research_sha256", "record_path"):
            with self.subTest(key=key):
                changed = {**self.identity, key: "changed-since-release"}
                with self.assertRaisesRegex(WorkbenchError, "changed after manual release"):
                    self.run_recovery(return_value=changed)
                self.device.adb.assert_not_called()
                self.device.observe.assert_not_called()
                self.assertFalse((self.directory / "observation.json").exists())

    def test_record_that_no_longer_validates_at_execution_is_rejected(self):
        with self.assertRaisesRegex(WorkbenchError, "published hash"):
            self.run_recovery(side_effect=WorkbenchError("record differs from published hash"))
        self.device.adb.assert_not_called()
        self.device.observe.assert_not_called()
        self.assertFalse((self.directory / "observation.json").exists())

    def test_valid_transport_does_not_clear_unknown_mutations(self):
        self.old_job()
        with self.assertRaisesRegex(WorkbenchError, "registered recovery procedure"):
            self.run_recovery()
        self.assertEqual(self.runner.reconciled_jobs, [])
        self.assertEqual(self.runner.recovered_resources, [])
        self.device.observe.assert_not_called()
        self.assertFalse((self.directory / "observation.json").exists())

    def test_valid_transport_does_not_ignore_a_live_old_command(self):
        process = self.old_job(active=True)
        with patch("workbench.worker.same_process", return_value=True) as alive:
            with self.assertRaisesRegex(WorkbenchError, "old command process is still alive"):
                self.run_recovery()
        alive.assert_called_once_with(process)
        self.assertEqual(self.runner.reconciled_jobs, [])
        self.device.observe.assert_not_called()
        self.assertFalse((self.directory / "observation.json").exists())

    def test_valid_transport_does_not_ignore_live_residual_descendants(self):
        process = self.old_job(residual=True)
        with patch("workbench.worker.same_process", return_value=True) as alive:
            with self.assertRaisesRegex(WorkbenchError, "descendants are still alive"):
                self.run_recovery()
        alive.assert_called_once_with(process)
        self.assertEqual(self.runner.reconciled_jobs, [])
        self.assertFalse((self.directory / "observation.json").exists())


class ManualTransportRequestBoundaryTest(unittest.TestCase):
    def test_jobs_submit_cannot_supply_internal_manual_return_metadata(self):
        request = {
            "project": str(ROOT), "operation": "device.recover",
            "device": "offline-device", "request_key": "synthetic-replay",
            "manual_return": {"transport": "sideload", "prepared_job": "a" * 32},
        }
        with patch("workbench.registry.load_project") as load:
            with self.assertRaisesRegex(WorkbenchError, "Unknown or adapter-owned.*manual_return"):
                normalize(request, {}, ROOT / "unused-offline-folder")
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
