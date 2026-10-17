import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.common import WorkbenchError, file_hash
from workbench.results import readonly_package_query, verify_package_query_recovery, readonly_hal_query, verify_hal_query_recovery


class PackageQueryTest(unittest.TestCase):
    def specimen(self):
        return {
            "operation": "apkrev.devsh", "serial": "registered-serial",
            "args": ["sh", "pm path com.example.fixture", "--serial", "registered-serial"],
            "entry": {"script": "@workbench/skills/android-workbench/components/apk-reverse/scripts/devsh.py",
                      "source": "@workbench/skills/android-workbench/components/apk-reverse"},
        }

    def test_query_does_not_allow_mutations_or_unreviewed_wrappers(self):
        spec = self.specimen()
        self.assertTrue(readonly_package_query(spec))
        for command in ("pm path com.example.fixture; pm uninstall com.example.fixture",
                        "pm install /data/local/tmp/test.apk", "pm path com.example.fixture >/sdcard/query"):
            bad = copy.deepcopy(spec)
            bad["args"][1] = command
            self.assertFalse(readonly_package_query(bad))
        bad = copy.deepcopy(spec)
        bad["args"][-1] = "another-serial"
        self.assertFalse(readonly_package_query(bad))
        bad = copy.deepcopy(spec)
        bad["entry"]["script"] = "custom.py"
        self.assertFalse(readonly_package_query(bad))

    def test_historical_recovery_checks_wrapper_and_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "reviewed"
            snapshot = folder / "source"
            component = ROOT / "skills/android-workbench/components/apk-reverse"
            spec = self.specimen()
            spec.update(source=str(source), source_manifest={})
            for name in ("scripts/devsh.py", "scripts/device_shell.py"):
                for target in (source / name, snapshot / name):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(component / name, target)
                spec["source_manifest"][name] = file_hash(source / name)
            runner = Mock()
            self.assertTrue(verify_package_query_recovery(runner, folder, spec))
            runner.device.adb.assert_not_called()
            (snapshot / "scripts/devsh.py").write_text("changed")
            with self.assertRaisesRegex(WorkbenchError, "source changed"):
                verify_package_query_recovery(runner, folder, spec)

    def test_hal_listing_recovery_rejects_other_commands_and_changed_sources(self):
        spec = self.specimen()
        spec["args"] = ["--serial", spec["serial"], "sh", "lshal"]
        self.assertTrue(readonly_hal_query(spec))
        for command in ("lshal debug", "lshal --debug", "lshal > /sdcard/list", "lshal; reboot", "$(lshal)"):
            bad = copy.deepcopy(spec)
            bad["args"][-1] = command
            self.assertFalse(readonly_hal_query(bad))
        bad = copy.deepcopy(spec)
        bad["args"][1] = "another-phone"
        self.assertFalse(readonly_hal_query(bad))
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spec.update(source=str(folder / "reviewed"), source_manifest={})
            for name in ("scripts/devsh.py", "scripts/device_shell.py"):
                for base in (Path(spec["source"]), folder / "source"):
                    target = base / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / "skills/android-workbench/components/apk-reverse" / name, target)
                spec["source_manifest"][name] = file_hash(Path(spec["source"]) / name)
            runner = Mock()
            self.assertTrue(verify_hal_query_recovery(runner, folder, spec))
            (Path(spec["source"]) / "scripts/device_shell.py").write_text("changed")
            with self.assertRaisesRegex(WorkbenchError, "source changed"):
                verify_hal_query_recovery(runner, folder, spec)


if __name__ == "__main__":
    unittest.main()
