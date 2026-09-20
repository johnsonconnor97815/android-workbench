import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import (
    AAPT2_SCRIPT,
    ADB_SCRIPT,
    APKSIGNER_SCRIPT,
    PACKAGE,
    SIGNER_B,
    create_apk,
    dumpsys,
    write_executable,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/apk_pull.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("apk_pull", SCRIPT)
APK = importlib.util.module_from_spec(spec)
spec.loader.exec_module(APK)
from android_tools import ApkError, canonical_bytes, parse_signers, sha256_bytes  # noqa: E402


class ApkPullTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apk pull tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name, body in (
            ("adb", ADB_SCRIPT),
            ("aapt2", AAPT2_SCRIPT),
            ("apksigner", APKSIGNER_SCRIPT),
        ):
            write_executable(
                self.tools / name,
                body.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1),
            )
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.output = self.root / "export"
        self.base = self.root / "base.apk"
        self.split = self.root / "split_config.zh.apk"
        create_apk(self.base)
        create_apk(
            self.split,
            split_name="config.zh",
            version_name=None,
            include_dex=False,
            include_resources=True,
        )
        self.remote_base = "/data/app/com.example.target-abc/base.apk"
        self.remote_split = "/data/app/com.example.target-abc/split_config.zh.apk"
        self.config = {
            "devices": "List of devices attached\nserial-one device product:test model:Test device:test transport_id:1\n",
            "allowedSerials": ["serial-one"],
            "getprop": "[ro.product.model]: [Test]\n[ro.build.version.sdk]: [35]\n[ro.build.version.release]: [15]\n[ro.build.fingerprint]: [test/product/device:15/id:user/test-keys]\n[ro.product.cpu.abilist]: [arm64-v8a]\n",
            "pmPaths": [f"package:{self.remote_base}\npackage:{self.remote_split}\n"],
            "dumpsys": [dumpsys(["base", "config.zh"])],
            "remoteFiles": {
                self.remote_base: str(self.base),
                self.remote_split: str(self.split),
            },
        }

    def cli(self, *args, expected=0, isolated=False):
        self.config_path.write_text(json.dumps(self.config))
        env = os.environ.copy()
        env.update(
            FAKE_ADB_CONFIG=str(self.config_path), FAKE_ADB_STATE=str(self.state_path)
        )
        result = subprocess.run(
            [
                sys.executable,
                *(["-S"] if isolated else []),
                str(SCRIPT),
                *map(str, args),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.root,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def tool_args(self, adb=True):
        names = ["adb", "aapt2", "apksigner"] if adb else ["aapt2", "apksigner"]
        return [v for name in names for v in (f"--{name}", str(self.tools / name))]

    def pull(self, *extra, expected=0, isolated=False):
        return self.cli(
            "pull",
            "--package",
            PACKAGE,
            "--output",
            self.output,
            *self.tool_args(),
            *extra,
            expected=expected,
            isolated=isolated,
        )

    def verify(self, expected=0):
        return self.cli(
            "verify", self.output, *self.tool_args(adb=False), expected=expected
        )

    def content(self):
        return json.loads((self.output / "manifest.json").read_text())["content"]

    def rewrite(self, content):
        APK.save_json(
            self.output / "manifest.json",
            {
                "content": content,
                "contentSha256": sha256_bytes(canonical_bytes(content)),
            },
        )

    def test_complete_split_export_and_offline_verify_without_site_packages(self):
        result = self.pull(isolated=True)
        self.assertEqual(json.loads(result.stdout)["apkCount"], 2)
        self.assertEqual(
            (self.output / "base.apk").read_bytes(), self.base.read_bytes()
        )
        self.assertEqual(self.content()["status"], "complete")
        self.verify()
        commands = json.loads(self.state_path.read_text())["commands"]
        for command in commands:
            self.assertTrue(
                command[1] == "pull"
                or command[1:3]
                in (["shell", "getprop"], ["shell", "pm"], ["shell", "dumpsys"]),
                command,
            )

    def test_single_base_export(self):
        self.config["pmPaths"] = [f"package:{self.remote_base}\n"]
        self.config["dumpsys"] = [dumpsys(["base"])]
        self.pull()
        self.assertEqual(len(self.content()["artifacts"]), 1)

    def test_multiple_devices_require_explicit_serial(self):
        self.config["devices"] += "serial-two device model:Other transport_id:2\n"
        self.pull(expected=2)
        self.assertFalse(self.output.exists())
        self.pull("--serial", "serial-one")

    def test_offline_unauthorized_unknown_and_no_devices_fail(self):
        for state in ("offline", "unauthorized"):
            with self.subTest(state=state):
                self.config["devices"] = (
                    f"List of devices attached\nserial-one {state}\n"
                )
                self.pull("--serial", "serial-one", expected=2)
        self.config["devices"] = "List of devices attached\n"
        self.pull(expected=2)
        self.assertFalse(self.output.exists())

    def test_devices_reports_package_presence(self):
        result = self.cli("devices", "--adb", self.tools / "adb", "--package", PACKAGE)
        self.assertTrue(json.loads(result.stdout)["devices"][0]["packageInstalled"])
        self.config["pmPaths"] = [""]
        result = self.cli("devices", "--adb", self.tools / "adb", "--package", PACKAGE)
        self.assertFalse(json.loads(result.stdout)["devices"][0]["packageInstalled"])

    def test_missing_package_creates_no_output(self):
        self.config["pmPaths"] = [""]
        self.pull(expected=2)
        self.assertFalse(self.output.exists())

    def test_missing_package_exit_one_is_reported_as_absent(self):
        self.config["pmPaths"] = [""]
        self.config["pmExitCode"] = 1
        result = self.cli("devices", "--adb", self.tools / "adb", "--package", PACKAGE)
        self.assertFalse(json.loads(result.stdout)["devices"][0]["packageInstalled"])
        result = self.pull(expected=2)
        self.assertIn("is not installed", result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_tool_fails_before_device_queries(self):
        (self.tools / "apksigner").unlink()
        self.pull(expected=2)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state_path.exists())

    def test_doctor_needs_no_python_packages_or_device(self):
        result = self.cli("doctor", *self.tool_args(), isolated=True)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        self.assertFalse(self.state_path.exists())

    def test_failed_split_is_incomplete_and_cannot_be_promoted(self):
        self.config["failPull"] = [self.remote_split]
        self.pull(expected=1)
        content = self.content()
        self.assertTrue(any(i["code"] == "missing-apk" for i in content["issues"]))
        self.assertFalse((self.output / self.split.name).exists())
        self.verify(expected=1)
        content["status"], content["issues"] = "complete", []
        self.rewrite(content)
        self.verify(expected=2)

    def test_failed_pull_retries_are_bounded_and_transient_error_recovers(self):
        self.config["failOnce"] = [self.remote_split]
        self.pull()
        attempts = json.loads(self.state_path.read_text())["pullAttempts"]
        self.assertEqual(attempts[self.remote_split], 2)

    def test_pull_timeout_is_incomplete_not_success(self):
        self.config["slowPull"] = [self.remote_split]
        self.pull("--pull-timeout", "1", "--retries", "0", expected=1)
        self.assertEqual(len(self.content()["artifacts"]), 1)

    def test_package_update_during_pull_is_incomplete(self):
        self.config["dumpsys"].append(dumpsys(["base", "config.zh"], version="8"))
        self.pull(expected=1)
        self.assertIn(
            "package-changed-during-pull", [i["code"] for i in self.content()["issues"]]
        )
        self.verify(expected=1)

    def test_irrelevant_dumpsys_changes_do_not_invalidate_export(self):
        self.config["dumpsys"].append(
            self.config["dumpsys"][0] + "    lastUseTime=999\n"
        )
        self.pull()

    def test_missing_pm_identity_or_split_inventory_blocks_completion(self):
        for field in ("heading", "version", "splits"):
            with self.subTest(field=field):
                self.output = self.root / field
                self.config["dumpsys"] = [
                    dumpsys(
                        None if field == "splits" else ["base", "config.zh"],
                        version=None if field == "version" else "7",
                        include_package_heading=field != "heading",
                    )
                ]
                self.pull(expected=1)
                self.verify(expected=1)

    def test_absent_version_name_is_supported(self):
        create_apk(self.base, version_name=None)
        self.config["dumpsys"] = [dumpsys(["base", "config.zh"], version_name=None)]
        self.pull()
        self.assertIsNone(self.content()["packageIdentity"]["versionName"])

    def test_package_version_signer_mismatch_and_unsigned_apks(self):
        for label, options in (
            ("package", {"package": "com.other.app"}),
            ("version", {"version_code": "9"}),
            ("signer", {"signer": SIGNER_B}),
            ("unsigned", {"signer": ""}),
        ):
            with self.subTest(label=label):
                self.output = self.root / label
                create_apk(self.split, split_name="config.zh", **options)
                self.pull(expected=1)
                self.verify(expected=1)

    def test_manifest_and_artifact_tampering_are_rejected(self):
        self.pull()
        data = (self.output / "base.apk").read_bytes()
        (self.output / "base.apk").write_bytes(data + b"tamper")
        self.verify(expected=2)
        (self.output / "base.apk").write_bytes(data)
        content = self.content()
        content["packageIdentity"]["versionCode"] = "888"
        self.rewrite(content)
        self.verify(expected=2)

    def test_evidence_and_serial_forgery_are_rejected(self):
        self.pull()
        content = self.content()
        content["serial"] = "wrong-device"
        self.rewrite(content)
        self.verify(expected=2)

    def test_symlink_traversal_and_unrecorded_apks_are_rejected(self):
        self.pull()
        original = self.content()
        content = json.loads(json.dumps(original))
        content["artifacts"][0]["file"] = "../base.apk"
        self.rewrite(content)
        self.verify(expected=2)
        self.rewrite(original)
        apk_path = self.output / "base.apk"
        apk_path.unlink()
        apk_path.symlink_to(self.base)
        self.verify(expected=2)
        apk_path.unlink()
        shutil.copyfile(self.base, apk_path)
        shutil.copyfile(self.base, self.output / "extra.apk")
        self.verify(expected=2)

    def test_output_and_symlink_are_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep")
        self.pull(expected=2)
        self.assertEqual(marker.read_text(), "keep")
        self.output = self.root / "link"
        self.output.symlink_to(self.root / "export", target_is_directory=True)
        self.pull(expected=2)
        self.assertEqual(marker.read_text(), "keep")

    def test_malformed_remote_paths_and_duplicate_names_fail(self):
        for paths in (
            "package:/data/../base.apk\n",
            f"package:{self.remote_base}\npackage:/data/elsewhere/base.apk\n",
            "package:/data/not-an-apk.txt\n",
        ):
            with self.subTest(paths=paths):
                self.config["pmPaths"] = [paths]
                self.pull(expected=2)
                self.assertFalse(self.output.exists())

    def test_source_stamp_is_not_a_package_signer(self):
        text = (
            "Number of signers: 1\nV3.0 Signer: certificate SHA-256 digest: "
            + "a" * 64
            + "\nSource Stamp Signer certificate SHA-256 digest: "
            + "b" * 64
        )
        result = parse_signers(text)
        self.assertEqual([x["certificateSha256"] for x in result], ["a" * 64])
        with self.assertRaises(ApkError):
            parse_signers("Source Stamp Signer certificate SHA-256 digest: " + "b" * 64)

    def test_checksum_list_tampering_is_rejected(self):
        self.pull()
        (self.output / "SHA256SUMS").write_text("")
        self.verify(expected=2)

    def test_copied_skill_runs_outside_source_repository(self):
        installed = self.root / "isolated skill"
        shutil.copytree(
            ROOT / "scripts",
            installed / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.config_path.write_text(json.dumps(self.config))
        env = os.environ.copy()
        env.update(
            FAKE_ADB_CONFIG=str(self.config_path),
            FAKE_ADB_STATE=str(self.state_path),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                str(installed / "scripts/apk_pull.py"),
                "pull",
                "--package",
                PACKAGE,
                "--output",
                str(self.output),
                *self.tool_args(),
            ],
            env=env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "complete")
        self.assertEqual(json.loads(result.stdout)["apkCount"], 2)


if __name__ == "__main__":
    unittest.main()
