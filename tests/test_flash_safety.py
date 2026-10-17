"""Offline regressions for flash intent, image binding and final verification."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_device_manager as fixtures
from flash_review_fixture import approve, rule


class FlashSafetyTest(unittest.TestCase):
    def setUp(self):
        self.module = fixtures.load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "boot.img"
        self.image.write_bytes(b"reviewed test image")
        self.research_path = fixtures.DeviceManagerDryRunTest().write_research(self.root)
        self.research = json.loads(self.research_path.read_text())
        self.research["decisions"]["reboot"] = "no"
        self.research["flash_plan"] = {
            "serial": "test-phone", "fastboot_serial": "test-phone",
            "slot": "a", "target_slot": "a", "mode": "fastboot", "bootloader_version": "reviewed-loader",
            "target_fingerprint": "test/target/build",
            "source_fingerprint": "test/source/build", "minimum_battery": 30,
            "acceptance_checks": ["wifi"],
            "anti_rollback": {"compatible": True, "source": "https://example.com/device-guide"},
            "images": {"boot": hashlib.sha256(self.image.read_bytes()).hexdigest()},
        }

        self.research["flash_plan"]["hardware_checks"] = {"android": {"ro.product.device": "husky"}, "fastboot": {"product": "husky"}}
        self.research["flash_plan"]["partition_writes"] = [rule("boot")]
        approve(self)

    def validate(self, research=None, dry_run=False):
        return self.module.validate_flash_plan(
            research or self.research, [("boot", self.image)], serial="test-phone",
            fastboot_serial="test-phone", dry_run=dry_run)

    def test_keep_data_blocks_both_userdata_and_encryption_metadata(self):
        self.research["decisions"]["scope"] = "custom"
        for partition in ("userdata", "userdata_a", "metadata", "metadata_b"):
            with self.subTest(partition=partition), self.assertRaisesRegex(ValueError, "keep data"):
                self.module.validate_flash_decisions(
                    self.research, [(partition, self.image)], no_reboot=True)

    def test_generic_flash_rejects_locked_device_and_unimplemented_wipe(self):
        for change, message in (({"bootloader": "locked"}, "unlocked"),
                                ({"decisions": {**self.research["decisions"], "data": "wipe"}}, "wipe")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.module.validate_flash_decisions(
                    {**self.research, **change}, [("boot", self.image)], no_reboot=True)

    def test_changed_file_and_wrong_phone_block_before_device_access(self):
        self.assertEqual(self.validate()["offline_checks"], "passed")
        self.image.write_bytes(b"changed after review")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.validate()
        self.research["flash_plan"]["serial"] = "another-phone"
        with self.assertRaisesRegex(ValueError, "serial"):
            self.validate()

    def test_missing_plan_is_only_allowed_as_incomplete_offline_preview(self):
        self.research.pop("flash_plan")
        self.assertFalse(self.validate(dry_run=True)["execution_ready"])
        with self.assertRaisesRegex(ValueError, "requires a reviewed flash_plan"):
            self.validate()

    def test_rollback_review_and_explicit_device_facts_are_required(self):
        for field in ("slot", "target_slot", "mode", "bootloader_version", "target_fingerprint", "anti_rollback"):
            research = copy.deepcopy(self.research)
            research["flash_plan"].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(research)

    def test_live_guard_rejects_stale_loader_slot_locked_or_different_device(self):
        args = SimpleNamespace(adb="fake-adb", fastboot="fake-fastboot",
                               serial="test-phone", timeout=30)
        baseline = {"is-userspace": "no", "product": "husky", "unlocked": "yes",
                    "version-bootloader": "reviewed-loader", "current-slot": "a"}
        for variable, wrong in (("product", "other"), ("unlocked", "no"),
                                ("version-bootloader", "new-loader"), ("current-slot", "b")):
            values = {**baseline, variable: wrong}
            def answer(command, **kwargs):
                return {"stdout": "", "stderr": f"{command[-1]}: {values[command[-1]]}", "returncode": 0}
            with self.subTest(variable=variable), patch.object(self.module, "ensure_bootloader"), \
                    patch.object(self.module, "run_command", side_effect=answer), \
                    self.assertRaisesRegex(ValueError, "mismatch"):
                self.module.check_flash_device(args, self.research, fastboot_serial="test-phone", records=[])

    def test_wrong_hash_main_never_calls_a_device_command(self):
        self.research["flash_plan"]["images"]["boot"] = "0" * 64
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / "result.json"
        argv = ["device_manager.py", "flash", "--serial", "test-phone", "--confirm",
                "--image", "boot=" + str(self.image), "--research", str(self.research_path),
                "--output", str(output), "--no-reboot"]
        with patch.object(sys, "argv", argv), patch.object(self.module, "run_command") as device:
            self.assertEqual(self.module.main(), 2)
            device.assert_not_called()
        self.assertIn("SHA-256 mismatch", json.loads(output.read_text())["error"])

    def test_completed_write_remains_unverified(self):
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / "result.json"
        argv = ["device_manager.py", "flash", "--serial", "test-phone", "--confirm",
                "--image", "boot=" + str(self.image), "--research", str(self.research_path),
                "--output", str(output), "--no-reboot"]
        with patch.object(sys, "argv", argv), patch.object(self.module, "check_flash_device"), \
                patch.object(self.module, "execute_plan"), patch.object(self.module, "check_partition_slot"):
            self.assertEqual(self.module.main(), 0)
        result = json.loads(output.read_text())
        self.assertEqual(result["flash_phase"], "awaiting_verification")
        self.assertFalse(result["workflow_complete"])

    def test_recovery_image_destination_is_explicit(self):
        commands = self.module.install_rom_prepare_commands(
            "fake-fastboot", "test-phone", self.image, partition="vendor_boot")
        self.assertEqual(commands[0][-2], "vendor_boot")
        with self.assertRaises(ValueError):
            self.module.install_rom_prepare_commands("fake", "test-phone", self.image, partition="userdata")

    def test_final_verification_rejects_old_build_and_preparation_only(self):
        record = self.root / "flash-result.json"
        previous = {"device": "test-phone", "command": "flash", "status": "complete",
                    "flash_phase": "awaiting_verification", "research": self.research,
                    "flash_plan": self.research["flash_plan"]}
        record.write_text(json.dumps(previous))
        args = SimpleNamespace(flash_record=record, serial="test-phone", adb="fake", timeout=30)
        health = self.root / "health.json"
        health.write_text(json.dumps({"flash_record_sha256": hashlib.sha256(record.read_bytes()).hexdigest(), "serial": "test-phone", "target_fingerprint": "test/target/build",
            "checks": {"wifi": {"status": "passed", "evidence": [{"path": str(self.image),
                "sha256": hashlib.sha256(self.image.read_bytes()).hexdigest()}]}}}))
        args.health_record = health
        with patch.object(self.module, "wait_for_boot"), patch.object(self.module, "run_command") as command:
            command.side_effect = [{"stdout": "husky"}, {"stdout": "test/old/build"}]
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                self.module.verify_flashed_system(args, [])
            command.side_effect = [{"stdout": "husky"}, {"stdout": "test/target/build"}, {"stdout": "_b"}]
            with self.assertRaisesRegex(ValueError, "slot_suffix"):
                self.module.verify_flashed_system(args, [])
            command.side_effect = [{"stdout": "husky"}, {"stdout": "test/target/build"}, {"stdout": "_a"}]
            self.assertTrue(self.module.verify_flashed_system(args, [])["workflow_complete"])
        previous["flash_phase"] = "awaiting_sideload"
        record.write_text(json.dumps(previous))
        with self.assertRaisesRegex(ValueError, "completed write record"):
            self.module.verify_flashed_system(args, [])

    def test_progress_is_saved_before_starting_each_external_command(self):
        output = self.root / "progress.json"
        self.module.FLASH_PROGRESS = lambda step: self.module.write_result(
            output, {"status": "in_progress", "active_step": step, "workflow_complete": False})
        def execute(*args, **kwargs):
            self.assertEqual(json.loads(output.read_text())["active_step"]["state"], "running")
            raise subprocess.TimeoutExpired(args[0], 1)
        with patch.object(self.module.subprocess, "run", side_effect=execute), self.assertRaises(RuntimeError):
            self.module.run_command(["fake-fastboot"], label="write test image", timeout=1, records=[])
        self.assertEqual(json.loads(output.read_text())["active_step"]["returncode"], 124)

    def test_research_cache_preserves_flash_plan(self):
        self.assertEqual(self.module.validate_research(self.research)["flash_plan"], self.research["flash_plan"])

    def test_sideload_requires_the_matching_preparation_before_device_access(self):
        self.research["decisions"].update(scope="custom", data="wipe")
        self.research["flash_plan"]["images"]["rom"] = self.research["flash_plan"]["images"]["boot"]
        self.research["flash_plan"]["rollback_images"] = {"boot": self.research["flash_plan"]["images"]["boot"]}
        backup = self.module.prepare_backup(self.research, [("boot", self.image)], self.root / "originals",
                                          discard_reason="No data to preserve on the test fixture")
        self.research["flash_plan"]["backup_record"] = {
            "path": backup["backup_manifest"], "sha256": backup["backup_manifest_sha256"]}
        approve(self)
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / "result.json"
        argv = ["device_manager.py", "install-rom", "--stage", "sideload", "--serial", "test-phone",
                "--confirm", "--rom", str(self.image), "--research", str(self.research_path),
                "--output", str(output), "--no-reboot"]
        with patch.object(sys, "argv", argv), patch.object(self.module, "run_command") as device:
            self.assertEqual(self.module.main(), 2)
            device.assert_not_called()
        self.assertIn("prepared-record", json.loads(output.read_text())["error"])

    def test_image_paths_are_reserved_and_fingerprinted_by_the_scheduler(self):
        from workbench.project import builtin_operations
        from workbench.registry import normalize
        project = self.root / "project"
        project.mkdir()
        (project / "workbench.project.json").write_text(json.dumps({
            "version": 1, "python": sys.executable,
            "workbench_root": str(Path(__file__).resolve().parents[1]),
            "operations": builtin_operations(),
        }))
        for operation, args in (
            ("flash", ["--image", "boot=" + str(self.image)]),
            ("flash", ["--image=boot=" + str(self.image)]),
            ("install_rom", ["--rom", str(self.image), "--recovery-boot", str(self.image)]),
            ("flash_verify", ["--flash-record", str(self.image)]),
        ):
            with self.subTest(operation=operation, args=args):
                folder = Path(tempfile.mkdtemp(dir=self.root))
                spec = normalize({
                    "project": str(project), "operation": "device_manager." + operation,
                    "device": "phone", "request_key": folder.name,
                    "args": [operation.replace("_", "-"), "--serial", "test-phone", *args],
                }, {"devices": {"phone": {"serial": "test-phone"}}}, folder)
                self.assertEqual(spec["input_fingerprints"][str(self.image)],
                                 hashlib.sha256(self.image.read_bytes()).hexdigest())
                self.assertIn({"key": "path:" + str(self.image), "mode": "read"}, spec["resources"])

    def test_partition_recovery_only_reconciles_the_selected_operation_and_transport(self):
        script = (Path(__file__).resolve().parents[1] /
                  "skills/android-workbench/components/device-manager/scripts/recover_flash.py")
        target = self.root / "jobs" / "target"
        target.mkdir(parents=True)
        normalized = target / "normalized.json"
        normalized.write_text(json.dumps({"operation": "device_manager.flash",
                                           "args": ["--fastboot-serial", "bootloader-phone"]}))
        fastboot = self.root / "fastboot"
        fastboot.write_text("#!/bin/sh\nprintf 'bootloader-phone fastboot\\n'\n")
        fastboot.chmod(0o700)
        grant = self.root / "grant.json"
        grant.write_text(json.dumps({"state": str(self.root), "spec": {
            "serial": "test-phone", "recovery_targets": ["target"]}}))
        import os
        env = {**os.environ, "AWB_INTERNAL_GRANT": str(grant), "PATH": str(self.root)}
        for original_operation, success in (("device_manager.flash", True), ("device_manager.root", False)):
            normalized.write_text(json.dumps({"operation": original_operation,
                                               "args": ["--fastboot-serial", "bootloader-phone"]}))
            output = self.root / (original_operation + ".json")
            result = subprocess.run([sys.executable, str(script), "--serial", "test-phone",
                                     "--fastboot-serial", "bootloader-phone", "--output", str(output)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["pass"], success)
            self.assertFalse(record["system_verified"])
            self.assertFalse(record["safe_to_retry"])

    def test_rom_prepare_recovery_checks_bootloader_and_rejects_wrong_stage(self):
        import os
        script = (Path(__file__).resolve().parents[1] /
                  "skills/android-workbench/components/device-manager/scripts/recover_install_rom.py")
        target = self.root / "jobs" / "target"
        target.mkdir(parents=True)
        (target / "normalized.json").write_text(json.dumps({
            "operation": "device_manager.install_rom", "args": ["--stage", "prepare"]}))
        fastboot = self.root / "fastboot"
        fastboot.write_text("#!/bin/sh\nprintf 'test-phone fastboot\\n'\n")
        fastboot.chmod(0o700)
        grant = self.root / "grant.json"
        grant.write_text(json.dumps({"state": str(self.root), "spec": {
            "serial": "test-phone", "adb": "must-not-run", "recovery_targets": ["target"]}}))
        for stage, expected in (("prepare", 0), ("sideload", 1)):
            output = self.root / (stage + ".json")
            result = subprocess.run([sys.executable, str(script), "--serial", "test-phone",
                                     "--stage", stage, "--output", str(output)],
                                    env={**os.environ, "AWB_INTERNAL_GRANT": str(grant), "PATH": str(self.root)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["pass"], expected == 0)
            self.assertFalse(record["system_verified"])


if __name__ == "__main__":
    unittest.main()
