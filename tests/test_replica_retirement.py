"""Retire bundled replication adapters without deleting project extensions."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.common import WorkbenchError
from workbench.project import builtin_operations, generate, update
from workbench.registry import normalize
from workbench.service import DEFAULTS


RETIRED = {
    "replication.workflow": "replication.py",
    "replication.emulator_compare": "runtime_compare.py",
}
OLD_SOURCE = "@workbench/skills/android-workbench/components/app-replication"


class ReplicaRetirementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-retirement-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.generated = generate(self.project, ROOT, sys.executable)

    def legacy_manifest(self):
        manifest = copy.deepcopy(self.generated)
        manifest["operations"].update({
            name: {"source": OLD_SOURCE, "script": OLD_SOURCE + "/scripts/" + script}
            for name, script in RETIRED.items()
        })
        manifest["operations"]["custom.inspect"] = {
            "script": "scripts/inspect.py", "source": "scripts", "readonly": True,
        }
        return manifest

    def test_fresh_manifest_has_no_retired_adapters(self):
        operations = builtin_operations()
        for name in RETIRED:
            self.assertNotIn(name, operations)
        self.assertFalse(any(OLD_SOURCE in entry["script"] for entry in operations.values()))
        self.assertIn("analysis.recon", operations)
        self.assertIn("device_manager.preflight", operations)

    def test_update_removes_old_builtins_and_preserves_custom_operations(self):
        previous = self.legacy_manifest()
        before = copy.deepcopy(previous)
        actual = update(previous, self.generated)
        for name in RETIRED:
            self.assertNotIn(name, actual["operations"])
        self.assertEqual(actual["operations"]["custom.inspect"], before["operations"]["custom.inspect"])
        self.assertEqual(previous, before)
        self.assertEqual(update(actual, self.generated), actual)

    def test_same_names_with_project_scripts_remain_custom(self):
        previous = copy.deepcopy(self.generated)
        scripts = self.project / "scripts"
        scripts.mkdir()
        (scripts / "custom.py").write_text("print('custom')\n")
        for name in RETIRED:
            previous["operations"][name] = {"source": "scripts", "script": "scripts/custom.py"}
        actual = update(previous, self.generated)
        (self.project / "workbench.project.json").write_text(json.dumps(actual))
        for name in RETIRED:
            self.assertEqual(actual["operations"][name], previous["operations"][name])
            directory = self.project / "evidence" / name
            directory.mkdir(parents=True)
            spec = normalize({"project": str(self.project), "operation": name}, DEFAULTS, directory)
            self.assertEqual(spec["script"], str(scripts / "custom.py"))

    def test_explicit_cli_update_backs_up_and_removes_stale_registrations(self):
        previous = self.legacy_manifest()
        target = self.project / "workbench.project.json"
        target.write_text(json.dumps(previous))
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/configure_project.py"), str(self.project), "--update"],
            cwd=self.project, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual = json.loads(target.read_text())
        self.assertEqual(actual["operations"]["custom.inspect"], previous["operations"]["custom.inspect"])
        backups = list((self.project / "evidence/workbench-config").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), previous)
        for name in RETIRED:
            with self.subTest(operation=name):
                self.assertNotIn(name, actual["operations"])
                directory = self.project / "evidence" / name
                directory.mkdir()
                with self.assertRaisesRegex(WorkbenchError, "Unknown registered operation"):
                    normalize({"project": str(self.project), "operation": name}, DEFAULTS, directory)


if __name__ == "__main__":
    unittest.main()
