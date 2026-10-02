"""A version number alone must not hide an installed payload mismatch."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_plugin
from scripts.check_installation import compare_installation


class InstallationTest(unittest.TestCase):
    def test_same_version_changed_file_and_stale_extra_are_detected(self):
        with tempfile.TemporaryDirectory(prefix="awb-installed-") as folder:
            installed = Path(folder)
            paths = build_plugin.validate()
            for path in paths:
                target = installed / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            (installed / "release-manifest.json").write_text(json.dumps(build_plugin.release_manifest(paths)))
            self.assertTrue(compare_installation(installed)["pass"])
            relative = "skills/android-workbench/components/analysis-agent/scripts/analysis.py"
            (installed / relative).write_text("stale content, same manifest version")
            result = compare_installation(installed)
            self.assertFalse(result["pass"])
            self.assertIn(relative, result["changed_files"])
            shutil.copy2(ROOT / relative, installed / relative)
            stale = installed / "skills/old-entry/SKILL.md"
            stale.parent.mkdir()
            stale.write_text("Old extra entry")
            result = compare_installation(installed)
            self.assertFalse(result["pass"])
            self.assertIn("skills/old-entry/SKILL.md", result["changed_files"])


if __name__ == "__main__":
    unittest.main()
