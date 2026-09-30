"""Durable, device-bound preflight facts and authorization invalidation."""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.service import Service
from workbench.store import Store


class DeviceStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service.__new__(Service)
        self.service.config = {"devices": {"one": {"serial": "fixture-one"},
                                           "two": {"serial": "fixture-two"}}}
        self.service.store = Store(self.root / "state.sqlite3")
        self.service.directory = self.root
        self.service.generation = "fixture"
        self.record_count = 0

    def tearDown(self):
        self.service.store.db.close()
        self.temp.cleanup()

    def record(self, *, status="granted", serial="fixture-one", dry_run=False,
               observations=None, access=None, state="succeeded", started=True):
        self.record_count += 1
        path = self.root / "preflight.json"
        path.write_text(json.dumps({
            "device": serial, "mode": "android", "status": "complete",
            "observations": {"boot_id": "boot-1", "build_fingerprint": "build-1",
                             "adb_authorization": "authorized", **(observations or {})},
            "root_access": {"status": status, "caller_uid": 2000,
                            "su_path": "/system/bin/su", "su_version": "30.7:MAGISKSU",
                            "checked_at": f"check-{self.record_count}", **(access or {})},
            "unavailable": {"root": "root access was not requested"} if status == "not_checked" else {},
        }))
        job = {"id": f"preflight-{self.record_count}", "state": state, "finished": time.time(),
               "started": time.time() if started else None,
               "spec": {"operation": "device_manager.preflight", "device": "one",
                        "outputs": [str(path)], "args": ["--dry-run"] if dry_run else []}}
        self.service.record_device_status(job)
        return path

    def record_info(self, *, boot="boot-1", build="build-1", serial="fixture-one"):
        path = self.root / "device-info.json"
        path.write_text(json.dumps({"boot_id": boot,
                                    "properties": {"ro.build.fingerprint": build}}))
        self.service.record_device_status({
            "id": "info-job", "state": "succeeded", "finished": time.time(),
            "spec": {"operation": "device.info", "device": "one", "serial": serial,
                     "directory": str(self.root)},
        })
        return path

    def test_authorization_survives_restart_and_evidence_removal(self):
        self.record().unlink()
        self.service.store.db.close()
        self.service.store = Store(self.root / "state.sqlite3")
        data = self.service.device_status("one")
        self.assertEqual(data["root_access"]["status"], "granted")
        self.assertEqual(data["root_access"]["caller_uid"], 2000)
        self.assertEqual(data["root_access"]["verified_job"], "preflight-1")
        self.assertGreaterEqual(data["age_seconds"], 0)
        self.assertFalse(data["stale"])
        self.assertIsNone(self.service.device_status("two"))

    def test_latest_denial_replaces_previous_grant(self):
        self.record()
        self.record(status="denied")
        self.assertEqual(self.service.device_status("one")["root_access"]["status"], "denied")

    def test_passive_check_reuses_matching_grant_without_renewing_verification(self):
        self.record()
        original = self.service.device_status("one")
        self.record(status="not_checked")
        data = self.service.device_status("one")
        self.assertEqual(data["job"], "preflight-2")
        self.assertEqual(data["root_access"]["status"], "granted")
        self.assertTrue(data["root_access"]["cached"])
        self.assertEqual(data["root_access"]["checked_at"], original["root_access"]["checked_at"])
        self.assertEqual(data["root_access"]["verified_job"], original["job"])
        self.assertNotIn("root", data["unavailable"])
        self.record(status="not_checked")
        self.assertEqual(self.service.device_status("one")["root_access"], data["root_access"])

    def test_passive_check_cannot_reuse_changed_or_missing_context(self):
        for observations, access in (({"boot_id": "boot-2"}, {}),
                                     ({"build_fingerprint": "build-2"}, {}),
                                     ({"boot_id": None}, {}),
                                     ({}, {"caller_uid": 12345}),
                                     ({}, {"su_path": "/other/su"}),
                                     ({}, {"su_version": "new-version"}),
                                     ({}, {"su_version": None})):
            with self.subTest(observations=observations, access=access):
                self.record()
                self.record(status="not_checked", observations=observations, access=access)
                root = self.service.device_status("one")["root_access"]
                self.assertEqual(root["status"], "not_checked")
                self.assertNotIn("cached", root)

    def test_passive_check_does_not_mint_or_restore_invalidated_grant(self):
        self.record(status="not_checked")
        self.assertEqual(self.service.device_status("one")["root_access"]["status"], "not_checked")
        self.record()
        self.service.invalidate_device_status("one", "manual_control")
        self.record(status="not_checked")
        self.assertFalse(self.service.device_status("one")["stale"])
        self.assertEqual(self.service.device_status("one")["root_access"]["status"], "not_checked")

    def test_cancelled_preflight_cannot_record_grant(self):
        self.record(state="cancelled")
        self.assertIsNone(self.service.device_status("one"))

    def test_preflight_cancelled_before_start_keeps_previous_grant(self):
        self.record()
        original = self.service.device_status("one")
        self.record(state="cancelled", started=False)
        self.assertEqual(self.service.device_status("one")["root_access"], original["root_access"])
        self.assertFalse(self.service.device_status("one")["stale"])

    def test_malformed_preflight_cannot_replace_previous_grant(self):
        self.record()
        path = self.root / "malformed-preflight.json"
        path.write_text(json.dumps({"device": "fixture-one", "status": "complete",
                                    "root_access": {"status": "granted"}, "observations": None}))
        self.service.record_device_status({
            "id": "malformed-job", "state": "succeeded", "started": time.time(),
            "spec": {"operation": "device_manager.preflight", "device": "one",
                     "outputs": [str(path)]},
        })
        self.assertTrue(self.service.device_status("one")["stale"])
        self.assertEqual(self.service.device_status("one")["root_access"]["verified_job"], "preflight-1")

    def test_info_survives_restart_and_evidence_cleanup_without_preflight(self):
        self.record_info().unlink()
        self.service.store.db.close()
        self.service.store = Store(self.root / "state.sqlite3")
        info = self.service.last_device_info("one")
        self.assertEqual(info["data"]["boot_id"], "boot-1")
        self.assertFalse(info["stale"])
        self.assertGreaterEqual(info["age_seconds"], 0)
        self.assertIsNone(self.service.device_status("one"))
        self.assertIsNone(self.service.last_device_info("two"))
        self.service.config["devices"]["one"]["serial"] = "replacement-phone"
        self.assertIsNone(self.service.last_device_info("one"))

    def test_wrong_phone_info_cannot_be_saved(self):
        self.record_info(serial="fixture-two")
        self.assertIsNone(self.service.last_device_info("one"))

    def test_legacy_info_is_saved_on_read_and_bound_to_original_phone(self):
        path = self.root / "device-info.json"
        path.write_text(json.dumps({"boot_id": "legacy-boot"}))
        job = {"id": "legacy-job", "state": "succeeded", "finished": time.time(),
               "spec": {"operation": "device.info", "device": "one", "serial": "fixture-one",
                        "directory": str(self.root)}}
        with patch.object(self.service.store, "jobs", return_value=[job]):
            self.assertEqual(self.service.last_device_info("one")["data"]["boot_id"], "legacy-boot")
        path.unlink()
        self.assertEqual(self.service.last_device_info("one")["data"]["boot_id"], "legacy-boot")
        self.service.config["devices"]["one"]["serial"] = "replacement-phone"
        with patch.object(self.service.store, "jobs", return_value=[job]):
            self.assertIsNone(self.service.last_device_info("one"))

    def test_preflight_does_not_make_old_general_info_fresh(self):
        self.record_info()
        self.service.invalidate_device_status("one", "device_manager.flash", "flash-job")
        self.record()
        self.assertFalse(self.service.device_status("one")["stale"])
        info = self.service.last_device_info("one")
        self.assertTrue(info["stale"])
        self.assertEqual(info["invalidation_reason"], "device_manager.flash")
        self.record_info()
        self.assertFalse(self.service.last_device_info("one")["stale"])

    def test_preflight_detects_general_info_from_an_older_boot_or_build(self):
        for change in ({"boot_id": "boot-2"}, {"build_fingerprint": "build-2"}):
            with self.subTest(change=change):
                self.record_info()
                self.record(status="not_checked", observations=change)
                info = self.service.last_device_info("one")
                self.assertTrue(info["stale"])
                self.assertEqual(info["invalidation_reason"], "boot_or_build_changed")
                self.assertFalse(self.service.device_status("one")["stale"])

    def test_only_started_failed_root_install_invalidates_condition(self):
        for started in (None, time.time()):
            with self.subTest(started=started):
                self.record()
                self.record_info()
                self.service.record_device_status({
                    "id": "frida-job", "state": "cancelled", "started": started,
                    "spec": {"operation": "device_manager.install_frida", "device": "one"},
                })
                self.assertEqual(self.service.device_status("one")["stale"], started is not None)
                self.assertEqual(self.service.last_device_info("one")["stale"], started is not None)

    def test_dry_run_cannot_grant_authorization(self):
        self.record(dry_run=True)
        self.assertIsNone(self.service.device_status("one"))

    def test_wrong_serial_cannot_replace_authorization(self):
        self.record()
        self.record(serial="fixture-two")
        data = self.service.device_status("one")
        self.assertEqual(data["serial"], "fixture-one")
        self.assertTrue(data["stale"])
        self.service.config["devices"]["one"]["serial"] = "replacement-phone"
        self.assertIsNone(self.service.device_status("one"))

    def test_invalidation_preserves_history_until_new_preflight(self):
        self.record()
        self.service.invalidate_device_status("one", "manual_control")
        data = self.service.device_status("one")
        self.assertTrue(data["stale"])
        self.assertEqual(data["root_access"]["status"], "granted")
        self.assertEqual(data["invalidation_reason"], "manual_control")
        self.record()
        self.assertFalse(self.service.device_status("one")["stale"])

    def test_changed_boot_or_build_invalidates_cached_grant(self):
        for boot, build, changed in (("boot-1", "build-1", False),
                                     ("boot-2", "build-1", True),
                                     ("boot-1", "build-2", True)):
            with self.subTest(boot=boot, build=build):
                self.record()
                self.record_info(boot=boot, build=build)
                self.assertEqual(self.service.device_status("one")["stale"], changed)
                self.assertFalse(self.service.last_device_info("one")["stale"])

    def test_real_flash_invalidates_before_launch_but_dry_run_does_not(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                self.record()
                job = {"id": "flash-job", "spec": {
                    "device": "one", "operation": "device_manager.flash",
                    "args": ["--dry-run"] if dry_run else [], "directory": str(self.root),
                }}
                with patch("workbench.service.atomic_json", side_effect=RuntimeError("stop before launch")):
                    with self.assertRaisesRegex(RuntimeError, "stop before launch"):
                        self.service.grant(job)
                self.assertEqual(self.service.device_status("one")["stale"], not dry_run)


if __name__ == "__main__":
    unittest.main()
