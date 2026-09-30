"""Offline tests for the device-manager component."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = (
    ROOT
    / "skills/android-workbench/components/device-manager/scripts/device_manager.py"
)
RECOVERY_SCRIPT = (
    ROOT
    / "skills/android-workbench/components/device-manager/scripts/recover_image_cache.py"
)
ROM_RECOVERY_SCRIPT = (
    ROOT
    / "skills/android-workbench/components/device-manager/scripts/recover_install_rom.py"
)
FACTORY_RECOVERY_SCRIPT = (
    ROOT
    / "skills/android-workbench/components/device-manager/scripts/recover_flash_factory.py"
)
PARTITION_RECOVERY_SCRIPT = (
    ROOT
    / "skills/android-workbench/components/device-manager/scripts/recover_partition_repair.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("device_manager", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DeviceManagerCommandTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_root_access_distinguishes_grant_denial_missing_su_and_timeout(self):
        for expected in ("granted", "denied", "su_missing", "unknown"):
            records = []

            def probe(command, *, label, records, **kwargs):
                record = {"name": label, "returncode": 0, "stdout": "", "stderr": ""}
                if label == "root caller uid":
                    record["stdout"] = "2000"
                elif label == "su path":
                    record["stdout"] = "/system/bin/su" if expected != "su_missing" else ""
                    record["returncode"] = 1 if expected == "su_missing" else 0
                elif label == "su version":
                    record["stdout"] = "30.7:MAGISKSU"
                elif label == "root id":
                    if expected == "granted":
                        record["stdout"] = "uid=0(root) gid=0(root)"
                    elif expected == "denied":
                        record.update(returncode=1, stderr="Permission denied")
                    else:
                        record["returncode"] = 124
                        records.append(record)
                        raise RuntimeError("root id timed out")
                records.append(record)
                return record

            with self.subTest(status=expected), patch.object(self.module, "run_command", side_effect=probe):
                access = self.module.inspect_root_access("adb", "fixture", timeout=15,
                                                         records=records, check_root=True)
                self.assertEqual(access["status"], expected)
                self.assertEqual(access["caller_uid"], 2000)
                self.assertIn("checked_at", access)
                if expected == "su_missing":
                    self.assertNotIn("root id", [step["name"] for step in records])
                elif expected == "unknown":
                    self.assertIn("unconfirmed", access["reason"])

    def test_default_root_inspection_does_not_request_privileges(self):
        with patch.object(self.module, "run_command", side_effect=[
            {"returncode": 0, "stdout": "2000", "stderr": ""},
            {"returncode": 0, "stdout": "/system/bin/su", "stderr": ""},
            {"returncode": 0, "stdout": "30.7:MAGISKSU", "stderr": ""},
        ]) as command:
            access = self.module.inspect_root_access("adb", "fixture", timeout=15, records=[])
        self.assertEqual(access["status"], "not_checked")
        self.assertEqual(access["su_version"], "30.7:MAGISKSU")
        self.assertEqual(command.call_count, 3)
        self.assertNotIn("su -c id", [call.args[0][-1] for call in command.call_args_list])
        parser = self.module.build_parser()
        self.assertFalse(parser.parse_args(["preflight", "--serial", "fixture"]).check_root)
        self.assertTrue(parser.parse_args(["preflight", "--serial", "fixture", "--check-root"]).check_root)

    def test_optional_su_version_timeout_does_not_block_root_inspection(self):
        records = []

        def probe(command, *, label, records, **kwargs):
            record = {"name": label, "returncode": 0, "stdout": "", "stderr": ""}
            record["stdout"] = {"root caller uid": "2000", "su path": "/system/bin/su",
                                "root id": "uid=0(root)"}.get(label, "")
            if label == "su version":
                record["returncode"] = 124
            records.append(record)
            if label == "su version":
                raise RuntimeError("su version timed out")
            return record

        with patch.object(self.module, "run_command", side_effect=probe):
            access = self.module.inspect_root_access("adb", "fixture", timeout=15,
                                                     records=records, check_root=True)
        self.assertEqual(access["status"], "granted")
        self.assertIsNone(access["su_version"])

    def test_unauthorized_phone_is_incomplete_and_does_not_request_root(self):
        with patch.object(self.module, "detect_mode", return_value="unauthorized"), patch.object(self.module, "inspect_root_access") as root:
            result = self.module.preflight(adb_path="adb", fastboot_path="fastboot", serial="fixture",
                                           fastboot_serial="fixture", timeout=15)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["observations"]["adb_authorization"], "unauthorized")
        root.assert_not_called()

    def test_adb_failure_is_not_recorded_as_missing_su(self):
        with patch.object(self.module, "run_command", side_effect=[
            {"returncode": 0, "stdout": "2000", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": "error: device offline"},
        ]) as command:
            access = self.module.inspect_root_access("adb", "fixture", timeout=15, records=[])
        self.assertEqual(access["status"], "unknown")
        self.assertIn("offline", access["reason"])
        self.assertEqual(command.call_count, 2)

    def test_global_arguments_are_accepted_before_or_after_subcommand(self):
        parser = self.module.build_parser()
        after = parser.parse_args(
            ["--output", "after.json", "preflight", "--serial", "after-serial"]
        )
        self.assertEqual(after.output, Path("after.json"))
        self.assertEqual(after.serial, "after-serial")
        before = parser.parse_args(
            ["--serial", "before-serial", "--output", "before.json", "preflight"]
        )
        self.assertEqual(before.output, Path("before.json"))
        self.assertEqual(before.serial, "before-serial")

    def test_install_rom_allows_long_sideload_transfers(self):
        parser = self.module.build_parser()
        arguments = parser.parse_args(["install-rom", "--stage", "sideload"])
        self.assertEqual(arguments.timeout, 1800.0)

    def test_partition_inspect_dry_run_writes_read_only_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "partition-inspect",
                    "--partition",
                    "system_a",
                    "--partition",
                    "product_a",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["mode"], "dry_run")
            self.assertEqual(record["partitions_requested"], ["system_a", "product_a"])
            self.assertEqual(record["plan"][3][-1], "is-logical:system_a")
            self.assertEqual(record["plan"][6][-1], "partition-size:product_a")

    def test_image_download_accepts_configurable_chunks(self):
        parser = self.module.build_parser()
        arguments = parser.parse_args(
            [
                "image-download",
                "--url",
                "https://example.com/image.img",
                "--sha256",
                "0" * 64,
                "--label",
                "image",
                "--cache-dir",
                "/cache",
            ]
        )
        self.assertEqual(arguments.chunks, 8)
        arguments = parser.parse_args(
            [
                "image-download",
                "--url",
                "https://example.com/image.img",
                "--sha256",
                "0" * 64,
                "--label",
                "image",
                "--cache-dir",
                "/cache",
                "--chunks",
                "32",
            ]
        )
        self.assertEqual(arguments.chunks, 32)

    def test_parse_images_requires_partition_and_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "boot.img"
            image.write_bytes(b"boot")
            parsed = self.module.parse_images([f"boot={image}"])
            self.assertEqual(parsed, [("boot", image.resolve())])
        with self.assertRaises(ValueError):
            self.module.parse_images([f"boot;invalid={image}"])
        with self.assertRaises(ValueError):
            self.module.parse_images([f"boot={image}.missing"])

    def test_command_output_combines_stdout_and_stderr(self):
        self.assertEqual(
            self.module.command_output(
                {"stdout": "device\n", "stderr": "unlocked: yes\n"}
            ),
            "device\nunlocked: yes",
        )

    def test_flash_commands_include_partition_and_reboot(self):
        commands = self.module.flash_commands(
            "fastboot",
            "serial",
            [("boot", Path("/images/boot.img"))],
            reboot=True,
        )
        self.assertEqual(
            commands,
            [
                ["fastboot", "-s", "serial", "flash", "boot", "/images/boot.img"],
                ["fastboot", "-s", "serial", "reboot"],
            ],
        )

    def test_root_commands_use_requested_slot(self):
        commands = self.module.root_commands(
            "fastboot",
            "serial",
            Path("/images/patched-boot.img"),
            partition="boot",
            slot="a",
            reboot=False,
        )
        self.assertEqual(
            commands,
            [
                [
                    "fastboot",
                    "-s",
                    "serial",
                    "flash",
                    "boot_a",
                    "/images/patched-boot.img",
                ],
            ],
        )

    def test_root_commands_support_both_slots(self):
        commands = self.module.root_commands(
            "fastboot",
            "serial",
            Path("/images/patched-boot.img"),
            partition="boot",
            slot="both",
            reboot=False,
        )
        self.assertEqual(
            [command[4] for command in commands],
            ["boot_a", "boot_b"],
        )

    def test_root_planner_refuses_other_partitions(self):
        for partition in ("init_boot", "recovery", "vendor_boot"):
            with self.subTest(partition=partition), self.assertRaisesRegex(ValueError, "only.*boot"):
                self.module.root_commands(
                    "fastboot", "serial", Path("/images/patched.img"),
                    partition=partition, slot="a", reboot=False,
                )

    def test_install_frida_commands_push_chmod_and_start(self):
        commands = self.module.install_frida_commands(
            "adb",
            "serial",
            Path("/downloads/frida-server"),
            "/data/local/tmp/frida-server",
            start=True,
        )
        self.assertEqual(commands[0], ["adb", "-s", "serial", "shell", "su -c id"])
        self.assertEqual(
            commands[1],
            [
                "adb",
                "-s",
                "serial",
                "push",
                "/downloads/frida-server",
                "/data/local/tmp/frida-server",
            ],
        )
        self.assertIn(
            "su -c 'chmod 755 /data/local/tmp/frida-server'",
            commands[2][-1],
        )
        self.assertIn(
            "su -c 'nohup /data/local/tmp/frida-server >/dev/null 2>&1 &'",
            commands[3][-1],
        )

    def test_research_validation_requires_complete_dossier(self):
        dossier = {
            "schema": 1,
            "model": "Pixel 8",
            "device": "husky",
            "build": "AP4A.250105.002",
            "bootloader": "unlocked",
            "method": "fastboot",
            "decisions": {
                "scope": "boot_only",
                "root": "no",
                "data": "keep",
                "reboot": "yes",
                "frida": "no",
            },
            "sources": [
                {
                    "title": "Google Pixel factory image page",
                    "url": "https://developers.google.com/android/images",
                }
            ],
            "pitfalls": ["Do not flash an image for another device."],
            "rollback": "Reflash the previous official image.",
        }
        validated = self.module.validate_research(dossier)
        self.assertEqual(validated["status"], "complete")
        incomplete = dict(dossier)
        incomplete.pop("rollback")
        with self.assertRaises(ValueError):
            self.module.validate_research(incomplete)
        invalid_decisions = dict(dossier)
        invalid_decisions["decisions"] = {
            **dossier["decisions"],
            "root": "maybe",
        }
        with self.assertRaises(ValueError):
            self.module.validate_research(invalid_decisions)

    def test_image_cache_path_preserves_zip_suffix(self):
        path = self.module.image_cache_path(
            Path("/cache"),
            "a" * 64,
            "https://example.com/lineage-blueline.zip",
        )
        self.assertEqual(path, Path("/cache") / ("a" * 64 + ".zip"))

    def test_image_cache_path_preserves_apk_suffix(self):
        path = self.module.image_cache_path(
            Path("/cache"), "b" * 64, "https://example.com/Magisk-v30.7.apk"
        )
        self.assertEqual(path, Path("/cache") / ("b" * 64 + ".apk"))

    def test_chunk_ranges_cover_file_exactly(self):
        self.assertEqual(
            self.module.chunk_ranges(10, 3),
            [(0, 2), (3, 5), (6, 9)],
        )

    def test_factory_flash_wrapper_pins_fastboot_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_directory = root / "bin"
            bin_directory.mkdir()
            fastboot = bin_directory / "fastboot"
            fastboot.write_text("#!/bin/sh\nexit 0\n")
            fastboot.chmod(0o700)
            original_path = os.environ["PATH"]
            os.environ["PATH"] = f"{bin_directory}:{original_path}"
            factory = root / "factory.zip"
            image_archive = root / "image-blueline.zip"
            with zipfile.ZipFile(image_archive, "w") as nested:
                nested.writestr(
                    "android-info.txt", "require board=crosshatch|blueline\n"
                )
            with zipfile.ZipFile(factory, "w") as archive:
                archive.writestr("blueline/flash-all.sh", "#!/bin/sh\ntrue\n")
                archive.write(
                    image_archive, "blueline/image-blueline.zip"
                )
            extracted = root / "extracted"
            extracted.mkdir()
            try:
                script, wrapper_directory, working_directory, _super_empty = (
                    self.module.prepare_factory_flash(
                        factory, "fastboot", "8ARXS06LU", extracted
                    )
                )
            finally:
                os.environ["PATH"] = original_path
            self.assertEqual(script.parent, working_directory)
            self.assertEqual(
                (wrapper_directory / "fastboot").read_text(),
                "#!/bin/sh\nexec "
                + __import__("shlex").quote(str(fastboot.resolve()))
                + " -s 8ARXS06LU \"$@\"\n",
            )

    def test_factory_flash_extracts_super_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_directory = root / "bin"
            bin_directory.mkdir()
            fastboot = bin_directory / "fastboot"
            fastboot.write_text("#!/bin/sh\nexit 0\n")
            fastboot.chmod(0o700)
            image_archive = root / "image-blueline.zip"
            with zipfile.ZipFile(image_archive, "w") as nested:
                nested.writestr(
                    "android-info.txt", "require board=crosshatch|blueline\n"
                )
                nested.writestr("super_empty.img", "super metadata")
            factory = root / "factory.zip"
            with zipfile.ZipFile(factory, "w") as archive:
                archive.writestr("blueline/flash-all.sh", "#!/bin/sh\ntrue\n")
                archive.write(image_archive, "blueline/image-blueline.zip")
            extracted = root / "extracted"
            extracted.mkdir()
            original_path = os.environ["PATH"]
            os.environ["PATH"] = f"{bin_directory}:{original_path}"
            try:
                _script, _wrapper, _working, super_empty = (
                    self.module.prepare_factory_flash(
                        factory,
                        "fastboot",
                        "8ARXS06LU",
                        extracted,
                        require_super_empty=True,
                    )
                )
            finally:
                os.environ["PATH"] = original_path
            self.assertEqual(super_empty, extracted / "super_empty.img")
            self.assertEqual(super_empty.read_text(), "super metadata")

    def test_install_rom_decisions_require_custom_scope_and_wipe(self):
        research = self.module.validate_research(
            {
                "model": "Pixel 3",
                "device": "blueline",
                "build": "lineage-22.2-20260928",
                "bootloader": "unlocked",
                "method": "LineageOS recovery sideload",
                "decisions": {
                    "scope": "custom",
                    "root": "no",
                    "data": "wipe",
                    "reboot": "yes",
                    "frida": "no",
                },
                "sources": [
                    {
                        "title": "LineageOS install guide",
                        "url": "https://wiki.lineageos.org/devices/blueline/install",
                    }
                ],
                "pitfalls": ["Follow the device-specific recovery guide."],
                "rollback": "Reflash Google Android 12 factory image SP1A.210812.016.C2.",
            }
        )
        self.module.validate_install_rom_decisions(research, no_reboot=False)
        locked = {**research, "bootloader": "locked"}
        with self.assertRaises(ValueError):
            self.module.validate_install_rom_decisions(locked, no_reboot=False)
        keep_data = {
            **research,
            "decisions": {**research["decisions"], "data": "keep"},
        }
        with self.assertRaises(ValueError):
            self.module.validate_install_rom_decisions(keep_data, no_reboot=False)


class DeviceManagerDryRunTest(unittest.TestCase):
    def write_research(
        self,
        root: Path,
        *,
        root_decision: str = "no",
        frida_decision: str = "no",
        reboot_decision: str = "yes",
    ) -> Path:
        research = root / "device-research.json"
        research.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "model": "Pixel 8",
                    "device": "husky",
                    "build": "AP4A.250105.002",
                    "bootloader": "unlocked",
                    "method": "fastboot",
                    "root_partition": "boot",
                    "decisions": {
                        "scope": "boot_only",
                        "root": root_decision,
                        "data": "keep",
                        "reboot": reboot_decision,
                        "frida": frida_decision,
                    },
                    "sources": [
                        {
                            "title": "Google Pixel factory image page",
                            "url": "https://developers.google.com/android/images",
                        }
                    ],
                    "pitfalls": ["Do not flash an image for another device."],
                    "rollback": "Reflash the previous official image.",
                }
            )
        )
        return research

    def write_custom_rom_research(self, root: Path) -> Path:
        research = root / "custom-rom-research.json"
        research.write_text(
            json.dumps(
                {
                    "model": "Pixel 3",
                    "device": "blueline",
                    "build": "lineage-22.2-20260928",
                    "bootloader": "unlocked",
                    "method": "LineageOS recovery sideload",
                    "decisions": {
                        "scope": "custom",
                        "root": "no",
                        "data": "wipe",
                        "reboot": "yes",
                        "frida": "no",
                    },
                    "sources": [
                        {
                            "title": "LineageOS install guide",
                            "url": "https://wiki.lineageos.org/devices/blueline/install",
                        }
                    ],
                    "pitfalls": ["Follow the device-specific recovery guide."],
                    "rollback": "Reflash Google Android 12 factory image SP1A.210812.016.C2.",
                }
            )
        )
        return research

    def write_official_rollback_research(self, root: Path) -> Path:
        research = root / "official-rollback-research.json"
        research.write_text(
            json.dumps(
                {
                    "model": "Pixel 3",
                    "device": "blueline",
                    "build": "SP1A.210812.016.C2",
                    "bootloader": "unlocked",
                    "method": "Run the official Google factory image flash-all.sh with fastboot pinned to the device serial.",
                    "root_partition": "boot",
                    "decisions": {
                        "scope": "full_image",
                        "root": "yes",
                        "data": "wipe",
                        "reboot": "yes",
                        "frida": "no",
                    },
                    "sources": [
                        {
                            "title": "Google Pixel factory images",
                            "url": "https://developers.google.com/android/images",
                        },
                        {
                            "title": "Official Pixel 3 factory image",
                            "url": "https://dl.google.com/dl/android/aosp/blueline-sp1a.210812.016.c2-factory-fa981d87.zip",
                        },
                    ],
                    "pitfalls": [
                        "The factory image erases data.",
                        "Use the blueline image only.",
                    ],
                    "rollback": "Repeat the official factory image installation.",
                }
            )
        )
        return research

    def write_factory_zip(self, root: Path, *, super_empty: bool = False) -> Path:
        factory = root / "blueline-factory.zip"
        image_archive = root / "image-blueline.zip"
        if super_empty:
            with zipfile.ZipFile(image_archive, "w") as nested:
                nested.writestr(
                    "android-info.txt",
                    "require board=crosshatch|blueline\n"
                    "require partition-exists=product\n",
                )
                nested.writestr("super_empty.img", "super metadata")
                nested.writestr("product.img", "product")
                nested.writestr("system_ext.img", "system ext")
            image_bytes = image_archive.read_bytes()
        else:
            with zipfile.ZipFile(image_archive, "w") as nested:
                nested.writestr(
                    "android-info.txt",
                    "require board=crosshatch|blueline\n"
                    "require partition-exists=product\n",
                )
            image_bytes = image_archive.read_bytes()
        with zipfile.ZipFile(factory, "w") as archive:
            archive.writestr("blueline/flash-all.sh", "#!/bin/sh\nfastboot getvar product\n")
            archive.writestr("blueline/image-blueline.zip", image_bytes)
            archive.writestr("blueline/bootloader-blueline.img", "bootloader")
            archive.writestr("blueline/radio-blueline.img", "radio")
        return factory

    def test_install_rom_prepare_dry_run_writes_plan_without_contacting_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boot = root / "boot.img"
            boot.write_bytes(b"boot")
            research = self.write_custom_rom_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "install-rom",
                    "--stage",
                    "prepare",
                    "--recovery-boot",
                    str(boot),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["stage"], "prepare")
            self.assertIn("Recovery mode", record["next_action"])
            self.assertIn("Factory reset", record["next_action"])
            self.assertEqual(
                record["plan"],
                [
                    ["missing-fastboot", "-s", "serial", "flash", "boot", str(boot)],
                ],
            )

    def test_install_rom_sideload_dry_run_writes_plan_without_contacting_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rom = root / "lineage.zip"
            rom.write_bytes(b"rom")
            research = self.write_custom_rom_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "install-rom",
                    "--stage",
                    "sideload",
                    "--rom",
                    str(rom),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["stage"], "sideload")
            self.assertEqual(
                record["plan"],
                [
                    ["missing-adb", "-s", "serial", "sideload", str(rom)],
                    ["missing-adb", "-s", "serial", "reboot"],
                ],
            )

    def test_flash_dry_run_writes_plan_without_contacting_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "boot.img"
            image.write_bytes(b"boot")
            research = self.write_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash",
                    "--image",
                    f"boot={image}",
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")

    def test_flash_factory_dry_run_requires_official_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = self.write_factory_zip(root)
            research = self.write_official_rollback_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash-factory",
                    "--factory-zip",
                    str(factory),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["factory_script"], "blueline/flash-all.sh")
            self.assertEqual(record["factory_image_zip"], "blueline/image-blueline.zip")
            self.assertEqual(
                record["model_consistency"]["factory_allowed_boards"],
                ["crosshatch", "blueline"],
            )
            self.assertEqual(record["model_consistency"]["device_check"], "pending")

    def test_flash_factory_rejects_research_device_not_supported_by_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = self.write_factory_zip(root)
            research = self.write_official_rollback_research(root)
            research_data = json.loads(research.read_text())
            research_data["device"] = "crosshatch-typo"
            research.write_text(json.dumps(research_data))
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash-factory",
                    "--factory-zip",
                    str(factory),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertIn("does not match the factory image", record["error"])

    def test_partition_repair_dry_run_writes_safe_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = self.write_official_rollback_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "partition-repair",
                    "--research",
                    str(research),
                    "--delete-partition",
                    "product_a",
                    "--delete-partition",
                    "product_b",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["delete_partitions"], ["product_a", "product_b"])
            self.assertEqual(
                record["plan"][0][3:], ["delete-logical-partition", "product_a"]
            )
            self.assertEqual(
                record["plan"][1][3:], ["delete-logical-partition", "product_b"]
            )
            self.assertTrue(record["mode_transition"]["conditional"])

    def test_flash_factory_reset_super_dry_run_writes_safe_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = self.write_factory_zip(root, super_empty=True)
            research = self.write_official_rollback_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash-factory",
                    "--factory-zip",
                    str(factory),
                    "--research",
                    str(research),
                    "--reset-super",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertTrue(record["reset_super"])
            self.assertEqual(record["super_empty"], "super_empty.img")
            self.assertEqual(record["plan"][0][3:6], ["--slot", "all", "wipe-super"])
            self.assertEqual(record["plan"][1][4], "bootloader")

    def test_flash_factory_repair_requires_reset_super(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = self.write_factory_zip(root, super_empty=True)
            research = self.write_official_rollback_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash-factory",
                    "--factory-zip",
                    str(factory),
                    "--research",
                    str(research),
                    "--repair-dynamic-partitions",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertIn(
                "--repair-dynamic-partitions requires --reset-super", record["error"]
            )

    def test_flash_factory_repair_dry_run_writes_staged_safe_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = self.write_factory_zip(root, super_empty=True)
            research = self.write_official_rollback_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash-factory",
                    "--factory-zip",
                    str(factory),
                    "--research",
                    str(research),
                    "--reset-super",
                    "--repair-dynamic-partitions",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(output.read_text())
            self.assertTrue(record["repair_dynamic_partitions"])
            self.assertEqual(
                record["supported_dynamic_partitions"], ["product", "system_ext"]
            )
            commands = [step["command"] for step in record["steps"]]
            self.assertIn(
                ["missing-fastboot", "-s", "serial", "delete-logical-partition", "product_a"],
                commands,
            )
            self.assertIn(
                ["missing-fastboot", "-s", "serial", "delete-logical-partition", "product_b"],
                commands,
            )
            self.assertEqual(record["steps"][0]["conditional"], True)

    def test_flash_factory_reset_super_requires_super_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = self.write_factory_zip(root)
            research = self.write_official_rollback_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash-factory",
                    "--factory-zip",
                    str(factory),
                    "--research",
                    str(research),
                    "--reset-super",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertIn("super_empty.img", record["error"])

    def test_root_prepare_and_collect_dry_runs_write_safe_plans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock_boot = root / "boot.img"
            stock_boot.write_bytes(b"stock boot")
            magisk = root / "Magisk.apk"
            magisk.write_bytes(b"apk")
            research = self.write_official_rollback_research(root)
            prepare_output = root / "prepare.json"
            prepare = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(prepare_output),
                    "root-prepare",
                    "--stock-boot",
                    str(stock_boot),
                    "--magisk-apk",
                    str(magisk),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(prepare.returncode, 0, prepare.stderr)
            prepare_record = json.loads(prepare_output.read_text())
            self.assertEqual(prepare_record["status"], "dry_run")
            self.assertEqual(
                prepare_record["plan"],
                [
                    ["missing-adb", "-s", "serial", "install", "-r", str(magisk)],
                    ["missing-adb", "-s", "serial", "push", str(stock_boot), "/sdcard/Download/boot.img"],
                ],
            )

            destination = root / "magisk_patched.img"
            collect_output = root / "collect.json"
            collect = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(collect_output),
                    "root-collect",
                    "--destination",
                    str(destination),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(collect.returncode, 0, collect.stderr)
            collect_record = json.loads(collect_output.read_text())
            self.assertEqual(collect_record["status"], "dry_run")
            self.assertEqual(
                collect_record["plan"],
                [
                    [
                        "missing-adb",
                        "-s",
                        "serial",
                        "pull",
                        "/sdcard/Download/magisk_patched.img",
                        str(destination),
                    ]
                ],
            )

    def test_root_operations_require_researched_boot_partition_before_device_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "patched.img"
            image.write_bytes(b"offline image")
            magisk = root / "Magisk.apk"
            magisk.write_bytes(b"offline manager")
            research = self.write_research(root, root_decision="yes")
            original = json.loads(research.read_text())
            cases = {
                "root": ["--patched-boot", str(image)],
                "root-prepare": ["--stock-boot", str(image), "--magisk-apk", str(magisk)],
                "root-collect": ["--destination", str(root / "collected.img")],
            }
            for partition in (None, "init_boot", "recovery"):
                dossier = {**original, "root_partition": partition}
                research.write_text(json.dumps(dossier))
                for command, args in cases.items():
                    with self.subTest(partition=partition, command=command):
                        output = root / f"blocked-{command}-{partition}.json"
                        result = subprocess.run(
                            [sys.executable, str(SCRIPT), "--serial", "serial",
                             "--adb", "missing-adb", "--fastboot", "missing-fastboot",
                             "--output", str(output), command, *args,
                             "--research", str(research), "--dry-run"],
                            capture_output=True, text=True, timeout=20,
                        )
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        record = json.loads(output.read_text())
                        self.assertIn("root_partition", record["error"])
                        self.assertEqual(record["steps"], [])
            self.assertEqual(load_module().validate_research(original)["root_partition"], "boot")

    def test_root_dry_run_accepts_explicit_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched_boot = root / "patched-boot.img"
            patched_boot.write_bytes(b"patched")
            research = self.write_research(root, root_decision="yes")
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "root",
                    "--patched-boot",
                    str(patched_boot),
                    "--research",
                    str(research),
                    "--slot",
                    "a",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["slot"], "a")
            self.assertEqual(record["plan"][0][4], "boot_a")

    def test_root_dry_run_installs_magisk_before_flashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched_boot = root / "patched-boot.img"
            patched_boot.write_bytes(b"patched")
            magisk = root / "Magisk.apk"
            magisk.write_bytes(b"apk")
            research = self.write_research(root, root_decision="yes")
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "root",
                    "--patched-boot",
                    str(patched_boot),
                    "--magisk-apk",
                    str(magisk),
                    "--research",
                    str(research),
                    "--slot",
                    "a",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(
                record["plan"],
                [
                    ["missing-adb", "-s", "serial", "install", "-r", str(magisk)],
                    [
                        "missing-fastboot",
                        "-s",
                        "serial",
                        "flash",
                        "boot_a",
                        str(patched_boot),
                    ],
                    ["missing-fastboot", "-s", "serial", "reboot"],
                ],
            )

    def test_install_frida_dry_run_requires_server_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "frida-server"
            server.write_bytes(b"server")
            research = self.write_research(
                root,
                root_decision="yes",
                frida_decision="yes",
            )
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "install-frida",
                    "--server",
                    str(server),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "dry_run")
            self.assertEqual(record["plan"][1][4], str(server.resolve()))

    def test_flash_dry_run_requires_research_dossier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "boot.img"
            image.write_bytes(b"boot")
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash",
                    "--image",
                    f"boot={image}",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("research", record["error"])

    def test_flash_rejects_partition_outside_selected_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "system.img"
            image.write_bytes(b"system")
            research = self.write_research(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "flash",
                    "--image",
                    f"system={image}",
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("boot_only", record["error"])

    def test_root_dry_run_requires_research_dossier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched_boot = root / "patched-boot.img"
            patched_boot.write_bytes(b"patched")
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "root",
                    "--patched-boot",
                    str(patched_boot),
                    "--slot",
                    "a",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("research", record["error"])

    def test_root_is_blocked_when_user_chose_not_to_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched_boot = root / "patched-boot.img"
            patched_boot.write_bytes(b"patched")
            research = self.write_research(root, root_decision="no")
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "root",
                    "--patched-boot",
                    str(patched_boot),
                    "--research",
                    str(research),
                    "--slot",
                    "a",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("not to root", record["error"])

    def test_root_is_blocked_when_bootloader_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched_boot = root / "patched-boot.img"
            patched_boot.write_bytes(b"patched")
            research = self.write_research(root, root_decision="yes")
            document = json.loads(research.read_text())
            document["bootloader"] = "locked"
            research.write_text(json.dumps(document))
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "root",
                    "--patched-boot",
                    str(patched_boot),
                    "--research",
                    str(research),
                    "--slot",
                    "a",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("unlocked bootloader", record["error"])

    def test_root_prepare_and_collect_ignore_reboot_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock_boot = root / "boot.img"
            stock_boot.write_bytes(b"stock boot")
            magisk = root / "Magisk.apk"
            magisk.write_bytes(b"apk")
            research = self.write_research(
                root,
                root_decision="yes",
                reboot_decision="no",
            )
            prepare_output = root / "prepare.json"
            prepare = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(prepare_output),
                    "root-prepare",
                    "--stock-boot",
                    str(stock_boot),
                    "--magisk-apk",
                    str(magisk),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(prepare.returncode, 0, prepare.stderr)

            destination = root / "magisk_patched.img"
            collect_output = root / "collect.json"
            collect = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(collect_output),
                    "root-collect",
                    "--destination",
                    str(destination),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(collect.returncode, 0, collect.stderr)

    def test_install_frida_is_blocked_when_user_chose_not_to_install_frida(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "frida-server"
            server.write_bytes(b"server")
            research = self.write_research(
                root,
                root_decision="yes",
                frida_decision="no",
            )
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "install-frida",
                    "--server",
                    str(server),
                    "--research",
                    str(research),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("not to install Frida", record["error"])

    def test_research_cache_reuses_identical_dossier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossier = self.write_research(root)
            cache = root / "research-cache.json"
            first_output = root / "first.json"
            second_output = root / "second.json"
            base_command = [
                sys.executable,
                str(SCRIPT),
                "--serial",
                "serial",
                "--adb",
                "missing-adb",
                "--fastboot",
                "missing-fastboot",
            ]
            first = subprocess.run(
                [
                    *base_command,
                    "--output",
                    str(first_output),
                    "research",
                    "--dossier",
                    str(dossier),
                    "--cache",
                    str(cache),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            second = subprocess.run(
                [
                    *base_command,
                    "--output",
                    str(second_output),
                    "research",
                    "--dossier",
                    str(dossier),
                    "--cache",
                    str(cache),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            first_record = json.loads(first_output.read_text())
            second_record = json.loads(second_output.read_text())
            self.assertFalse(first_record["cached"])
            self.assertTrue(second_record["cached"])
            self.assertEqual(first_record["cache_key"], second_record["cache_key"])
            self.assertEqual(first_record["cached_at"], second_record["cached_at"])

    def test_research_cache_refresh_and_decision_change_create_new_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossier = self.write_research(root, root_decision="yes")
            cache = root / "research-cache.json"
            output = root / "result.json"
            command = [
                sys.executable,
                str(SCRIPT),
                "--serial",
                "serial",
                "--adb",
                "missing-adb",
                "--fastboot",
                "missing-fastboot",
                "--output",
                str(output),
                "research",
                "--dossier",
                str(dossier),
                "--cache",
                str(cache),
            ]
            first = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_key = json.loads(output.read_text())["cache_key"]
            refresh_output = root / "refresh.json"
            refreshed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(refresh_output),
                    "research",
                    "--dossier",
                    str(dossier),
                    "--cache",
                    str(cache),
                    "--refresh",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(refreshed.returncode, 0, refreshed.stdout + refreshed.stderr)
            refreshed_record = json.loads(refresh_output.read_text())
            self.assertFalse(refreshed_record["cached"])
            self.assertEqual(refreshed_record["cache_key"], first_key)

            changed = json.loads(dossier.read_text())
            changed["decisions"]["root"] = "no"
            dossier.write_text(json.dumps(changed))
            changed_output = root / "changed.json"
            changed_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(changed_output),
                    "research",
                    "--dossier",
                    str(dossier),
                    "--cache",
                    str(cache),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(changed_result.returncode, 0, changed_result.stdout + changed_result.stderr)
            changed_record = json.loads(changed_output.read_text())
            self.assertFalse(changed_record["cached"])
            self.assertNotEqual(changed_record["cache_key"], first_key)


class DeviceManagerImageCacheTest(unittest.TestCase):
    def write_source_image(self, root: Path) -> tuple[Path, str]:
        source = root / "source.img"
        source.write_bytes(b"factory image")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return source, digest

    def test_image_download_reuses_matching_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, digest = self.write_source_image(root)
            cache_dir = root / "images"
            first_output = root / "first.json"
            second_output = root / "second.json"
            base_command = [
                sys.executable,
                str(SCRIPT),
                "--serial",
                "serial",
                "--adb",
                "missing-adb",
                "--fastboot",
                "missing-fastboot",
            ]
            first = subprocess.run(
                [
                    *base_command,
                    "--output",
                    str(first_output),
                    "image-download",
                    "--url",
                    source.as_uri(),
                    "--sha256",
                    digest,
                    "--label",
                    "factory-boot",
                    "--partition",
                    "boot",
                    "--cache-dir",
                    str(cache_dir),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            second = subprocess.run(
                [
                    *base_command,
                    "--output",
                    str(second_output),
                    "image-download",
                    "--url",
                    source.as_uri(),
                    "--sha256",
                    digest,
                    "--label",
                    "factory-boot",
                    "--partition",
                    "boot",
                    "--cache-dir",
                    str(cache_dir),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            first_record = json.loads(first_output.read_text())
            second_record = json.loads(second_output.read_text())
            self.assertFalse(first_record["cached"])
            self.assertTrue(second_record["cached"])
            self.assertEqual(first_record["sha256"], digest)
            self.assertEqual(second_record["sha256"], digest)
            self.assertTrue((cache_dir / f"{digest}.img").is_file())

    def test_image_download_resumes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, digest = self.write_source_image(root)
            cache_dir = root / "images"
            cache_dir.mkdir()
            partial = cache_dir / f"{digest}.img.part"
            partial.write_bytes(b"fact")
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output",
                    str(output),
                    "image-download",
                    "--url",
                    source.as_uri(),
                    "--sha256",
                    digest,
                    "--label",
                    "factory-boot",
                    "--partition",
                    "boot",
                    "--cache-dir",
                    str(cache_dir),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertTrue(record["resumed"])
            self.assertEqual(
                (cache_dir / f"{digest}.img").read_bytes(),
                source.read_bytes(),
            )

    def test_image_download_blocks_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, digest = self.write_source_image(root)
            wrong_digest = "0" * 64
            cache_dir = root / "images"
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "image-download",
                    "--url",
                    source.as_uri(),
                    "--sha256",
                    wrong_digest,
                    "--label",
                    "factory-boot",
                    "--partition",
                    "boot",
                    "--cache-dir",
                    str(cache_dir),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "blocked")
            self.assertIn("sha256 mismatch", record["error"])
            self.assertFalse((cache_dir / f"{wrong_digest}.img").exists())
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_image_verify_writes_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, digest = self.write_source_image(root)
            output = root / "verify.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--serial",
                    "serial",
                    "--adb",
                    "missing-adb",
                    "--fastboot",
                    "missing-fastboot",
                    "--output",
                    str(output),
                    "image-verify",
                    "--image",
                    str(source),
                    "--sha256",
                    digest,
                    "--label",
                    "factory-boot",
                    "--partition",
                    "boot",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["sha256"], digest)
            self.assertEqual(record["label"], "factory-boot")


class DeviceManagerRegistrationTest(unittest.TestCase):
    def test_builtin_operations_are_registered(self):
        from workbench.project import builtin_operations

        operations = builtin_operations()
        expected = {
            "device_manager.preflight": "preflight",
            "device_manager.partition_inspect": "partition-inspect",
            "device_manager.partition_repair": "partition-repair",
            "device_manager.bootloader_state": "bootloader-state",
            "device_manager.research": "research",
            "device_manager.image_download": "image-download",
            "device_manager.image_verify": "image-verify",
            "device_manager.flash": "flash",
            "device_manager.flash_factory": "flash-factory",
            "device_manager.install_rom": "install-rom",
            "device_manager.root": "root",
            "device_manager.root_prepare": "root-prepare",
            "device_manager.root_collect": "root-collect",
            "device_manager.install_frida": "install-frida",
        }
        for name, subcommand in expected.items():
            with self.subTest(name=name):
                entry = operations[name]
                self.assertEqual(entry["subcommand"], subcommand)
                if name not in {
                    "device_manager.research",
                    "device_manager.image_download",
                    "device_manager.image_verify",
                }:
                    self.assertEqual(entry["serial"], "--serial")
                self.assertEqual(
                    entry["outputs"],
                    ["--output", "--cache"]
                    if name == "device_manager.research"
                    else ["--output", "--cache-dir"]
                    if name == "device_manager.image_download"
                    else ["--output", "--destination"]
                    if name == "device_manager.root_collect"
                    else ["--output"],
                )
                self.assertIn("components/device-manager", entry["script"])
                self.assertEqual(
                    entry.get("readonly", False),
                    name in {
                        "device_manager.preflight",
                        "device_manager.partition_inspect",
                        "device_manager.image_verify",
                    },
                )
        recovery = operations["device_manager.recover_image_cache"]
        self.assertTrue(recovery["script"].endswith("recover_image_cache.py"))
        self.assertEqual(
            recovery["recovery_for"],
            ["device_manager.image_download", "device_manager.recover_image_cache"],
        )
        rom_recovery = operations["device_manager.recover_install_rom"]
        self.assertTrue(rom_recovery["script"].endswith("recover_install_rom.py"))
        self.assertEqual(
            rom_recovery["recovery_for"],
            [
                "device_manager.install_rom",
                "device_manager.recover_install_rom",
            ],
        )
        factory_recovery = operations["device_manager.recover_flash_factory"]
        self.assertTrue(factory_recovery["script"].endswith("recover_flash_factory.py"))
        self.assertEqual(
            factory_recovery["recovery_for"],
            [
                "device_manager.flash_factory",
                "device_manager.recover_flash_factory",
            ],
        )
        partition_recovery = operations["device_manager.recover_partition_repair"]
        self.assertTrue(
            partition_recovery["script"].endswith("recover_partition_repair.py")
        )
        self.assertEqual(
            partition_recovery["recovery_for"],
            [
                "device_manager.partition_repair",
                "device_manager.recover_partition_repair",
            ],
        )


class DeviceManagerImageCacheRecoveryTest(unittest.TestCase):
    def test_recovery_removes_only_partial_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "device-manager" / "images"
            images.mkdir(parents=True)
            partial = images / "pending.zip.part"
            complete = images / "verified.img"
            partial.write_bytes(b"partial")
            complete.write_bytes(b"complete")
            job = root / "state" / "jobs" / "old-job"
            job.mkdir(parents=True)
            (job / "normalized.json").write_text(
                json.dumps(
                    {
                        "operation": "device_manager.image_download",
                        "resources": [
                            {"key": "path:" + str(images), "mode": "write"}
                        ],
                    }
                )
            )
            grant = root / "grant.json"
            grant.write_text(
                json.dumps(
                    {"state": str(root / "state"), "spec": {"recovery_targets": ["old-job"]}}
                )
            )
            output = root / "recovery-result.json"
            result = subprocess.run(
                [sys.executable, str(RECOVERY_SCRIPT), "--output", str(output)],
                env={**os.environ, "AWB_INTERNAL_GRANT": str(grant)},
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(output.read_text())
            self.assertTrue(record["pass"])
        self.assertEqual(record["recovered_parts"], [str(partial)])


class DeviceManagerRomRecoveryTest(unittest.TestCase):
    def test_recovery_requires_sideload_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "jobs" / "target"
            target.mkdir(parents=True)
            (target / "normalized.json").write_text(
                json.dumps({"operation": "device_manager.install_rom"})
            )
            adb = root / "adb"
            adb.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-s\" ] && [ \"$2\" = \"serial\" ] "
                "&& [ \"$3\" = \"get-state\" ]; then\n"
                "  echo sideload\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n"
            )
            adb.chmod(0o755)
            grant = root / "grant.json"
            grant.write_text(
                json.dumps(
                    {
                        "state": str(root),
                        "spec": {
                            "adb": str(adb),
                            "serial": "serial",
                            "recovery_targets": ["target"],
                        },
                    }
                )
            )
            output = root / "recovery-result.json"
            environment = {**os.environ, "AWB_INTERNAL_GRANT": str(grant)}
            success = subprocess.run(
                [
                    sys.executable,
                    str(ROM_RECOVERY_SCRIPT),
                    "--serial",
                    "serial",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            record = json.loads(output.read_text())
            self.assertTrue(record["pass"])
            self.assertEqual(record["adb_state"], "sideload")
            self.assertEqual(record["recovery_targets"], ["target"])


class DeviceManagerFactoryRecoveryTest(unittest.TestCase):
    def test_recovery_accepts_fastbootd_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "jobs" / "target"
            target.mkdir(parents=True)
            (target / "normalized.json").write_text(
                json.dumps({"operation": "device_manager.flash_factory"})
            )
            fastboot = root / "fastboot"
            fastboot.write_text("#!/bin/sh\necho 'serial\\tfastbootd'\n")
            fastboot.chmod(0o755)
            grant = root / "grant.json"
            grant.write_text(
                json.dumps(
                    {
                        "state": str(root),
                        "spec": {
                            "serial": "serial",
                            "recovery_targets": ["target"],
                        },
                    }
                )
            )
            output = root / "recovery-result.json"
            environment = {
                **os.environ,
                "AWB_INTERNAL_GRANT": str(grant),
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
            }
            success = subprocess.run(
                [
                    sys.executable,
                    str(FACTORY_RECOVERY_SCRIPT),
                    "--serial",
                    "serial",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            record = json.loads(output.read_text())
            self.assertTrue(record["pass"])
            self.assertEqual(record["fastboot_state"], "fastbootd")
            self.assertEqual(record["recovery_targets"], ["target"])


class DeviceManagerPartitionRecoveryTest(unittest.TestCase):
    def test_recovery_accepts_fastboot_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "jobs" / "target"
            target.mkdir(parents=True)
            (target / "normalized.json").write_text(
                json.dumps({"operation": "device_manager.partition_repair"})
            )
            fastboot = root / "fastboot"
            fastboot.write_text("#!/bin/sh\necho 'serial\\tfastboot'\n")
            fastboot.chmod(0o755)
            grant = root / "grant.json"
            grant.write_text(
                json.dumps(
                    {
                        "state": str(root),
                        "spec": {
                            "serial": "serial",
                            "recovery_targets": ["target"],
                        },
                    }
                )
            )
            output = root / "recovery-result.json"
            environment = {
                **os.environ,
                "AWB_INTERNAL_GRANT": str(grant),
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
            }
            success = subprocess.run(
                [
                    sys.executable,
                    str(PARTITION_RECOVERY_SCRIPT),
                    "--serial",
                    "serial",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            record = json.loads(output.read_text())
            self.assertTrue(record["pass"])
            self.assertEqual(record["fastboot_state"], "fastboot")
            self.assertEqual(record["recovery_targets"], ["target"])


if __name__ == "__main__":
    unittest.main()
