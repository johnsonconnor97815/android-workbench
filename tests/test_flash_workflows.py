"""Offline evidence for backup coverage, full OTA and staged ROM installation."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import test_flash_safety as safety
from flash_review_fixture import approve, rule


class FlashWorkflowTest(unittest.TestCase):
    setUp = safety.FlashSafetyTest.setUp

    def backup(self):
        self.research["flash_plan"]["rollback_images"] = {"boot": hashlib.sha256(self.image.read_bytes()).hexdigest()}
        backup = self.module.prepare_backup(self.research, [("boot", self.image)], self.root / "originals",
                                          discard_reason="The owner explicitly chose not to keep test data")
        path = Path(backup["backup_manifest"])
        self.research["flash_plan"]["backup_record"] = {"path": str(path), "sha256": backup["backup_manifest_sha256"]}
        return path

    def run_main(self, command, *args):
        plan = self.research["flash_plan"]
        if command == "install-rom" and plan.get("recovery_partition"):
            parts = plan.get("prepare_order", [plan["recovery_partition"]])
            plan["partition_writes"] = [rule("recovery" if part == plan["recovery_partition"] else part,
                part, plan.get("partition_modes", {}).get(part, plan["mode"])) for part in parts]
        approve(self)
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / ("result-" + str(len(list(self.root.glob("result-*")))) + ".json")
        argv = ["device_manager.py", command, "--serial", "test-phone", "--research", str(self.research_path),
                "--output", str(output), *map(str, args)]
        with patch.object(sys, "argv", argv):
            code = self.module.main()
        return code, json.loads(output.read_text())

    def ota(self, **fields):
        self.research["bootloader"] = "locked"
        self.research["decisions"].update(scope="ota", data="keep")
        self.research["flash_plan"].update(mode="recovery", target_slot="b")
        image = self.root / "ota.zip"
        metadata = {"pre-device": "husky", "post-build": "test/target/build", "ota-type": "AB", **fields}
        with zipfile.ZipFile(image, "w") as archive:
            archive.writestr("META-INF/com/android/metadata", "\n".join(key + "=" + value for key, value in metadata.items()))
        self.research["flash_plan"]["images"] = {"ota": hashlib.sha256(image.read_bytes()).hexdigest()}
        return image

    def test_backup_covers_reviewed_originals_and_detects_changed_copy(self):
        path = self.backup()
        self.assertTrue(self.module.verify_backup(path, self.research)["requires_fresh_restore_research"])
        (path.parent / "boot.img").write_bytes(b"modified original")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.module.verify_backup(path, self.research)

    def test_backup_rejects_incomplete_originals_before_creating_directory(self):
        self.research["flash_plan"]["rollback_images"] = {"boot": "0" * 64, "vendor_boot": "1" * 64}
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.module.prepare_backup(self.research, [("boot", self.image)], self.root / "originals", discard_reason="discard")
        self.assertFalse((self.root / "originals").exists())

    def test_data_receipt_requires_correct_source_and_existing_hashed_files(self):
        self.research["flash_plan"]["rollback_images"] = {"boot": hashlib.sha256(self.image.read_bytes()).hexdigest()}
        record = self.root / "data.json"
        receipt = {"status": "complete", "serial": "test-phone", "source_fingerprint": "old-build",
                   "coverage": "user_selected_shared_storage", "files": [{"path": str(self.image), "sha256": "0" * 64}]}
        for build, expected in (("old-build", "different source"), ("test/source/build", "missing or changed")):
            receipt["source_fingerprint"] = build
            record.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, expected):
                self.module.prepare_backup(self.research, [("boot", self.image)], self.root / "originals", data_record=record)
        self.assertFalse((self.root / "originals").exists())

    def test_shared_storage_receipt_is_retained_beside_data_and_can_prepare_original_backup(self):
        retained = self.root / "retained-data"
        def pull(command, **kwargs):
            (Path(command[-1]) / "fixture.txt").write_bytes(b"exported fixture data")
            return {"stdout": "pulled", "returncode": 0}
        with patch.object(self.module, "check_android_source", return_value={}), \
                patch.object(self.module, "run_command", side_effect=pull):
            code, result = self.run_main("backup-data", "--backup-dir", retained, "--remote-path", "/sdcard/Documents")
        self.assertEqual(code, 0, result)
        receipt = Path(result["data_manifest"])
        self.assertEqual(receipt.parent, retained)
        data = json.loads(receipt.read_text())
        self.assertFalse(data["includes_private_app_data"])
        self.assertEqual(len(data["files"]), 1)
        self.research["flash_plan"]["rollback_images"] = {"boot": hashlib.sha256(self.image.read_bytes()).hexdigest()}
        original = self.module.prepare_backup(self.research, [("boot", self.image)], self.root / "originals", data_record=receipt)
        manifest = self.module.verify_backup(original["backup_manifest"], self.research)
        self.assertEqual(manifest["data"]["coverage"], "user_selected_shared_storage")

    def test_wipe_without_backup_blocks_all_device_access(self):
        self.research["decisions"].update(scope="custom", data="wipe")
        self.research["flash_plan"].update(recovery_partition="boot", recovery_steps=["Reset data using the guide"])
        self.research["flash_plan"]["images"]["recovery"] = hashlib.sha256(self.image.read_bytes()).hexdigest()
        with patch.object(self.module, "run_command") as phone:
            code, result = self.run_main("install-rom", "--stage", "prepare", "--confirm", "--no-reboot", "--recovery-boot", self.image)
        self.assertEqual(code, 2)
        self.assertIn("backup_record", result["error"])
        phone.assert_not_called()

    def test_full_ota_rejects_wrong_model_incremental_wipe_or_target_before_device(self):
        for fields in ({"pre-device": "other"}, {"pre-build": "old"}, {"ota-wipe": "yes"}, {"post-build": "different"}):
            with self.subTest(fields=fields):
                image = self.ota(**fields)
                with patch.object(self.module, "run_command") as phone:
                    code, result = self.run_main("install-ota", "--stage", "prepare", "--ota", image, "--confirm")
                self.assertEqual(code, 2)
                phone.assert_not_called()

    def test_locked_phone_full_ota_uses_only_stock_recovery(self):
        image = self.ota()
        observed = {"ro.product.device": "husky", "ro.build.fingerprint": "test/source/build",
                    "ro.bootloader": "reviewed-loader", "ro.boot.slot_suffix": "_a", "battery": "Current Battery Service state:\nlevel: 75\nhealth: 2"}
        def answer(command, **kwargs):
            return {"stdout": observed[command[-1]], "stderr": "", "returncode": 0}
        with patch.object(self.module, "run_command", side_effect=answer), patch.object(self.module, "execute_plan") as write:
            code, result = self.run_main("install-ota", "--stage", "prepare", "--ota", image, "--confirm")
        self.assertEqual(code, 0, result)
        self.assertEqual(write.call_args.args[0], [["adb", "-s", "test-phone", "reboot", "recovery"]])
        self.assertEqual(result["flash_phase"], "awaiting_sideload")
        self.assertFalse(result["workflow_complete"])

    def test_ota_sideload_requires_matching_prepare_receipt(self):
        image = self.ota()
        with patch.object(self.module, "run_command") as phone:
            code, result = self.run_main("install-ota", "--stage", "sideload", "--ota", image, "--confirm")
        self.assertEqual(code, 2)
        self.assertIn("prepared-record", result["error"])
        phone.assert_not_called()

    def test_source_and_battery_guards_stop_before_mode_change(self):
        args = SimpleNamespace(serial="test-phone", adb="adb", timeout=30)
        baseline = ["husky", "test/source/build", "reviewed-loader", "_a", "level: 15"]
        with patch.object(self.module, "run_command", side_effect=[{"stdout": value} for value in baseline]), \
                self.assertRaisesRegex(ValueError, "battery"):
            self.module.check_android_source(args, self.research, [], require_baseline=True)
        self.research["flash_plan"].pop("source_fingerprint")
        with patch.object(self.module, "run_command") as phone, self.assertRaisesRegex(ValueError, "source_fingerprint"):
            self.module.check_android_source(args, self.research, [], require_baseline=True)
        phone.assert_not_called()

    def test_abnormal_or_frozen_battery_stops_ota_before_reboot(self):
        image = self.ota()
        for battery in (
            "Current Battery Service state:\nlevel: 75\nhealth: 3\ntemperature: 269",
            "Current Battery Service state:\nlevel: 75\nhealth: 2\n(UPDATES STOPPED -- use 'reset' to restart)",
            "Current Battery Service state:\nlevel: 75",
            "level: 75\nhealth: 2",
        ):
            with self.subTest(battery=battery), patch.object(self.module, "run_command", side_effect=[
                {"stdout": value, "returncode": 0} for value in
                ["husky", "test/source/build", "reviewed-loader", "_a", battery]
            ]), patch.object(self.module, "execute_plan") as change_mode:
                code, result = self.run_main("install-ota", "--stage", "prepare", "--ota", image, "--confirm")
            self.assertEqual(code, 2, result)
            self.assertIn("battery", result["error"])
            change_mode.assert_not_called()

    def test_multiple_preparation_images_require_exact_order_and_mode(self):
        plan = self.research["flash_plan"]
        images = [("vendor_boot", self.image), ("dtbo", self.image)]
        with self.assertRaisesRegex(ValueError, "prepare_order"):
            self.module.validate_installation_steps(plan, images, recovery_partition="vendor_boot")
        plan.update(prepare_order=["dtbo", "vendor_boot"], partition_modes={"dtbo": "fastbootd"})
        steps = self.module.validate_installation_steps(plan, images, recovery_partition="vendor_boot")
        self.assertEqual([(part, mode) for part, path, mode in steps], [("dtbo", "fastbootd"), ("vendor_boot", "fastboot")])
        plan["prepare_order"] = ["dtbo", "dtbo"]
        with self.assertRaises(ValueError):
            self.module.validate_installation_steps(plan, images, recovery_partition="vendor_boot")

    def test_reviewed_addons_cannot_be_omitted_or_rebooted_early(self):
        self.research["decisions"].update(scope="custom", data="wipe", reboot="yes")
        self.research["flash_plan"]["images"].update(rom=hashlib.sha256(self.image.read_bytes()).hexdigest(),
                                                    **{"addon:1": hashlib.sha256(self.image.read_bytes()).hexdigest()})
        for flags, error in (([], "every reviewed"), (["--addon", self.image], "do not reboot")):
            with patch.object(self.module, "run_command") as phone:
                code, result = self.run_main("install-rom", "--stage", "sideload", "--rom", self.image, "--confirm", *flags)
            self.assertEqual(code, 2)
            self.assertIn(error, result["error"])
            phone.assert_not_called()

    def test_stage_receipt_rejects_skipped_addon_or_changed_plan(self):
        record = self.root / "preceding.json"
        previous = {"device": "test-phone", "status": "complete", "command": "install-rom",
                    "flash_plan": copy.deepcopy(self.research["flash_plan"]), "flash_phase": "awaiting_addon", "package_index": 0}
        record.write_text(json.dumps(previous))
        self.module.require_prepared_record(record, self.research, "test-phone", command="install-rom", phase="awaiting_addon", package_index=0)
        with self.assertRaisesRegex(ValueError, "preceding stage"):
            self.module.require_prepared_record(record, self.research, "test-phone", command="install-rom", phase="awaiting_addon", package_index=1)
        self.research["flash_plan"]["target_fingerprint"] = "changed"
        with self.assertRaises(ValueError):
            self.module.require_prepared_record(record, self.research, "test-phone", command="install-rom", phase="awaiting_addon", package_index=0)

    def test_rom_extra_images_then_all_addons_reach_verification_in_order(self):
        self.research["decisions"].update(scope="custom", data="wipe", reboot="no")
        digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        plan = self.research["flash_plan"]
        plan.update(recovery_partition="vendor_boot", prepare_order=["dtbo", "vendor_boot"],
                    recovery_steps=["Use the device guide to reset data and enter sideload"],
                    partition_modes={"dtbo": "fastbootd"})
        plan["images"].update(recovery=digest, dtbo=digest, rom=digest, **{"addon:1": digest})
        self.backup()
        with patch.object(self.module, "check_android_source", return_value={}), \
                patch.object(self.module, "check_flash_device") as check, \
                patch.object(self.module, "check_partition_slot"), \
                patch.object(self.module, "execute_plan") as write:
            code, prepared = self.run_main("install-rom", "--stage", "prepare", "--confirm",
                "--recovery-boot", self.image, "--prepare-image", "dtbo=" + str(self.image), "--addon", self.image)
        self.assertEqual(code, 0, prepared)
        self.assertEqual([call.args[0][0][-2] for call in write.call_args_list], ["dtbo", "vendor_boot"])
        self.assertEqual([call.args[1]["flash_plan"]["mode"] for call in check.call_args_list], ["fastbootd", "fastboot"])
        preceding = self.root / "result-0.json"
        for index, phase in ((0, "awaiting_addon"), (1, "awaiting_verification")):
            with patch.object(self.module, "wait_for_adb_state"), patch.object(self.module, "execute_plan") as write:
                code, record = self.run_main("install-rom", "--stage", "sideload", "--confirm", "--no-reboot",
                    "--rom", self.image, "--addon", self.image, "--package-index", index, "--prepared-record", preceding)
            self.assertEqual(code, 0, record)
            self.assertEqual(record["flash_phase"], phase)
            self.assertFalse(record["workflow_complete"])
            self.assertEqual(write.call_args.args[0], [["adb", "-s", "test-phone", "sideload", str(self.image)]])
            preceding = self.root / ("result-" + str(index + 1) + ".json")

    def test_functional_acceptance_needs_hashed_evidence_for_correct_build(self):
        plan = self.research["flash_plan"]
        plan["acceptance_checks"] = ["wifi", "camera"]
        with self.assertRaisesRegex(ValueError, "health-record"):
            self.module.validate_health_record(None, plan, "test-phone", flash_record_sha256="1" * 64)
        record = self.root / "health.json"
        evidence = {"path": str(self.image), "sha256": hashlib.sha256(self.image.read_bytes()).hexdigest()}
        receipt = {"flash_record_sha256": "1" * 64, "serial": "test-phone", "target_fingerprint": plan["target_fingerprint"],
                   "checks": {name: {"status": "passed", "evidence": [evidence]} for name in plan["acceptance_checks"]}}
        record.write_text(json.dumps(receipt))
        self.assertEqual(self.module.validate_health_record(record, plan, "test-phone", flash_record_sha256="1" * 64)["health_checks"], "passed")
        self.image.write_bytes(b"changed evidence")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.module.validate_health_record(record, plan, "test-phone", flash_record_sha256="1" * 64)

    def test_nested_backup_files_are_reserved_and_hashed(self):
        from workbench.project import builtin_operations
        from workbench.registry import normalize
        self.backup()
        self.research_path.write_text(json.dumps(self.research))
        project = self.root / "project"
        project.mkdir()
        (project / "workbench.project.json").write_text(json.dumps({"version": 1, "python": sys.executable,
            "workbench_root": str(Path(__file__).resolve().parents[1]), "operations": builtin_operations()}))
        folder = self.root / "job"
        folder.mkdir()
        spec = normalize({"project": str(project), "operation": "device_manager.backup_verify", "request_key": "nested",
            "args": ["backup-verify", "--research", str(self.research_path), "--backup-record", self.research["flash_plan"]["backup_record"]["path"]]}, {}, folder)
        original = self.root / "originals/boot.img"
        self.assertIn(str(original), spec["input_fingerprints"])
        self.assertIn({"key": "path:" + str(original), "mode": "read"}, spec["resources"])

    def test_managed_backup_cannot_silently_use_a_retention_cleaned_job_directory(self):
        from workbench.project import builtin_operations
        from workbench.registry import normalize
        project = self.root / "backup-project"
        project.mkdir()
        self.research_path.write_text(json.dumps(self.research))
        (project / "workbench.project.json").write_text(json.dumps({"version": 1, "python": sys.executable,
            "workbench_root": str(Path(__file__).resolve().parents[1]), "operations": builtin_operations()}))
        folder = self.root / "temporary-job"
        folder.mkdir()
        spec = normalize({"project": str(project), "operation": "device_manager.backup_prepare", "request_key": "retention",
            "args": ["backup-prepare", "--research", str(self.research_path), "--image", "boot=" + str(self.image),
                     "--discard-data-reason", "No data in this fixture"]}, {}, folder)
        with self.assertRaises(SystemExit) as blocked:
            self.module.build_parser().parse_args(spec["args"])
        self.assertEqual(blocked.exception.code, 2)
        self.assertFalse((folder / "original-images").exists())

    def test_ota_recovery_reconciles_only_its_own_stage_and_retains_prior_evidence(self):
        scripts = Path(__file__).resolve().parents[1] / "skills/android-workbench/components/device-manager/scripts"
        target = self.root / "jobs/interrupted"
        target.mkdir(parents=True)
        adb = self.root / "adb"
        adb.write_text("#!" + sys.executable + "\nprint('recovery')\n")
        adb.chmod(0o700)
        grant = self.root / "grant.json"
        grant.write_text(json.dumps({"state": str(self.root), "spec": {"serial": "test-phone",
            "adb": str(adb), "recovery_targets": ["interrupted"]}}))
        env = {**os.environ, "AWB_INTERNAL_GRANT": str(grant)}
        for index, (operation, stage, success) in enumerate((
            ("device_manager.install_ota", "prepare", True),
            ("device_manager.install_ota", "sideload", False),
            ("device_manager.install_rom", "prepare", False))):
            (target / "normalized.json").write_text(json.dumps({"operation": operation, "args": ["--stage", stage]}))
            output = self.root / ("recovery-" + str(index) + ".json")
            result = subprocess.run([sys.executable, str(scripts / "recover_ota.py"), "--serial", "test-phone",
                "--stage", "prepare", "--output", str(output)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt["system_verified"])
            self.assertFalse(receipt["safe_to_retry"])
        output = self.root / "keep-evidence.json"
        for script in ("recover_ota.py", "recover_flash.py", "recover_flash_factory.py", "recover_install_rom.py"):
            output.write_text("retained evidence")
            result = subprocess.run([sys.executable, str(scripts / script), "--serial", "test-phone", "--output", str(output)],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "retained evidence")


if __name__ == "__main__":
    unittest.main()
