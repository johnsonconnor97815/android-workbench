"""Execute known fixture analysis through a real isolated scheduler."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analysis_acceptance import acceptance
from workbench.client import Client
from workbench.common import WorkbenchError, atomic_json, rpc
from workbench.project import generate
from workbench.service import DEFAULTS


class AnalysisEndToEndTest(unittest.TestCase):
    def test_known_split_fixture_reaches_reviewed_report_through_queue(self):
        with tempfile.TemporaryDirectory(prefix="awb-e2e-") as folder:
            root = Path(folder)
            project, state = root / "project", root / "state"
            project.mkdir()
            state.mkdir()
            atomic_json(project / "workbench.project.json", generate(project, ROOT, sys.executable))
            atomic_json(state / "config.json", {**DEFAULTS, "min_free_bytes": 0})
            environment = {k: v for k, v in os.environ.items() if k not in {"AWB_INTERNAL_GRANT", "PYTHONHOME", "PYTHONPATH"}}
            environment["ANDROID_WORKBENCH_STATE"] = str(state)
            with (root / "service.log").open("wb") as log:
                process = subprocess.Popen([sys.executable, str(ROOT / "scripts/workbench.py"), "--state", str(state), "serve"], env=environment, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        try:
                            rpc(state, "service.capabilities")
                            break
                        except WorkbenchError:
                            time.sleep(0.03)
                    client = Client(project, state, "end-to-end")
                    result = acceptance(client, project / "evidence")
                    self.assertTrue(result["pass"], result)
                    self.assertFalse(result["device_validation"])
                    self.assertTrue(json.loads(Path(result["completion"]).read_text())["complete"])
                    self.assertIn("failed", [j["state"] for j in result["jobs"]])
                    last = result["jobs"][-1]
                    status = client.call("jobs.status", {"id": last["id"]})
                    samples = json.loads((project / "evidence/recon.json").read_text())["samples"]
                    resources = json.dumps(status["resources"])
                    for sample in samples:
                        self.assertIn(sample["path"], resources)
                    routed = next(j for j in result["jobs"] if j["operation"] == "analysis.route")
                    route_status = client.call("jobs.status", {"id": routed["id"]})
                    for sample in samples:
                        self.assertIn(sample["path"], json.dumps(route_status["resources"]))
                finally:
                    process.terminate()
                    process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
