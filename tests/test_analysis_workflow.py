"""Known package layouts and evidence-backed analysis completion, without a phone."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/android-workbench/components/analysis-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("workflow_analysis", SCRIPTS / "analysis.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AnalysisWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-analysis-flow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.apk = self.package("base.apk", {"classes.dex": b"dex\n035\0", "AndroidManifest.xml": b"manifest"})

    def package(self, name, entries):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, data in entries.items():
                archive.writestr(entry, data)
        return path

    def evidence(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def test_routes_permissions_privacy_and_observation_without_unrelated_actions(self):
        for question in ("分析这个 App 的权限风险", "分析这个 App 是否收集并上传位置"):
            result = analysis.route_request(question, True, [])
            self.assertIn("static.permission_audit", result["suggested_operations"])
            self.assertIn("components/permission-audit/README.md", result["suggested_references"])
            self.assertFalse(result["device_runtime_tools_enabled"])
        result = analysis.route_request("只截当前页面，不安装，不启动 App", False, [])
        self.assertEqual(result["mode"], "device_runtime")
        self.assertIn("device.screenshot", result["suggested_operations"])
        self.assertNotIn("apkrev.install_test", result["suggested_operations"])
        self.assertNotIn("apkrev.run_probe", result["suggested_operations"])
        self.assertNotIn("apkrev.usb_net_proxy", result["suggested_operations"])

    def test_split_framework_evidence_changes_tool_selection(self):
        split = self.package("split.apk", {"lib/arm64-v8a/libflutter.so": b"ELF", "assets/flutter_assets/AssetManifest.json": b"{}"})
        recon = analysis.reconnaissance([self.apk, split])
        self.assertIn("flutter", recon["layers"])
        self.assertEqual(len(recon["samples"]), 2)
        result = analysis.route_request("分析登录逻辑，只看源码", True, [], recon)
        self.assertIn("components/apk-reverse/references/framework-runtimes.md", result["suggested_references"])
        self.assertTrue(result["specialist_required"])
        self.assertFalse(result["device_runtime_tools_enabled"])

    def test_protection_absence_is_not_a_claim_of_readable_code(self):
        recon = analysis.reconnaissance([self.apk])
        self.assertEqual(recon["protection"]["status"], "not_detected")
        self.assertTrue(recon["protection"]["uncertain"])
        packed = self.package("packed.apk", {"classes.dex": b"dex\n035\0", "lib/arm64-v8a/libjiagu.so": b"ELF"})
        result = analysis.route_request("分析真实代码", True, [], analysis.reconnaissance([packed]))
        self.assertIn("components/apk-reverse/references/recon.md", result["suggested_references"])
        self.assertTrue(result["specialist_required"])

    def test_no_jadx_route_is_presented_as_complete_flutter_coverage(self):
        result = analysis.route_request("分析 Flutter App 的登录逻辑，只看源码", True, [])
        self.assertTrue(result["specialist_required"])
        self.assertIn("components/apk-reverse/references/framework-runtimes.md", result["suggested_references"])

    def plan(self):
        recon = analysis.reconnaissance([self.apk])
        return analysis.create_plan("分析权限风险", recon, analysis.route_request("分析权限风险", True, [], recon))

    def test_collector_success_is_not_review_or_analysis_completion(self):
        plan = self.plan()
        collector = self.evidence("collection.json", {"pass": True, "collection_only": True, "review_required": True})
        analysis.update_plan(plan, "static", "completed", [collector], "Permissions collected")
        with self.assertRaisesRegex(ValueError, "review"):
            analysis.update_plan(plan, "review", "completed", [collector], "Review")
        self.assertFalse(analysis.check_plan(plan)["complete"])

    def test_known_evidence_chain_completes_then_detects_changed_evidence(self):
        plan = self.plan()
        code = self.evidence("code.json", {"permission": "CAMERA", "location": "Guard.java:8"})
        analysis.update_plan(plan, "static", "completed", [code], "Located check")
        review = self.evidence("review.json", {"review_complete": True, "claims": [{"claim": "CAMERA is checked", "evidence": [str(code)]}], "limitations": ["No device verification requested"]})
        analysis.update_plan(plan, "review", "completed", [review], "Reviewed actual call")
        report = self.root / "report.md"
        report.write_text("CAMERA is checked at Guard.java:8. Static evidence only.\n")
        analysis.update_plan(plan, "report", "completed", [report], "Final report")
        self.assertTrue(analysis.check_plan(plan)["complete"])
        code.write_text("changed")
        self.assertFalse(analysis.check_plan(plan)["complete"])

    def test_sample_change_and_out_of_order_completion_are_rejected(self):
        plan = self.plan()
        report = self.root / "report.md"
        report.write_text("Report")
        with self.assertRaisesRegex(ValueError, "previous"):
            analysis.update_plan(plan, "report", "completed", [report], "Done")
        self.apk.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            analysis.update_plan(plan, "static", "completed", [report], "Done")

    def test_runtime_completion_requires_observation_evidence(self):
        recon = analysis.reconnaissance([self.apk])
        route = analysis.route_request("安装并截图", True, [], recon)
        plan = analysis.create_plan("安装并截图", recon, route)
        for step in plan["steps"]:
            if step["id"] == "static":
                step["status"] = "completed"
                step["evidence"] = [{"path": str(self.apk), "sha256": analysis.sha256_file(self.apk)}]
        exit_record = self.evidence("exit.json", {"exit_code": 0})
        with self.assertRaisesRegex(ValueError, "runtime"):
            analysis.update_plan(plan, "runtime", "completed", [exit_record], "Command succeeded")

    def test_missing_manifest_tool_is_an_explicit_gap(self):
        output = self.root / "recon.json"
        result = subprocess.run([sys.executable, str(SCRIPTS / "analysis.py"), "recon", "--apk", str(self.apk), "--aapt2", str(self.root / "missing-aapt2"), "--output", str(output)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(output.read_text())["manifests"][0]
        self.assertFalse(manifest["available"])
        self.assertTrue(manifest["uncertain"])

    def test_removed_stage_and_changed_review_cannot_complete(self):
        plan = self.plan()
        plan["steps"] = plan["steps"][:2]
        result = analysis.check_plan(plan)
        self.assertFalse(result["complete"])
        self.assertIn("Required analysis stages", result["issues"][0])

    def test_runtime_observation_can_be_reviewed_and_completed(self):
        plan = analysis.create_plan("只截当前页面，不安装", analysis.reconnaissance([]), analysis.route_request("只截当前页面，不安装", False, []))
        screenshot = self.root / "ui.png"
        screenshot.write_bytes(b"explicit simulated screenshot evidence")
        record = self.evidence("runtime.json", {"runtime_verified": True, "device": "simulated", "job_id": "simulated-job", "observations": [{"kind": "ui", "path": "ui.png"}]})
        analysis.update_plan(plan, "runtime", "completed", [record], "Observed fixture UI")
        review = self.evidence("review.json", {"review_complete": True, "claims": [{"claim": "Simulated fixture observation is retained", "evidence": [str(screenshot)]}], "limitations": ["Not a real device test"]})
        analysis.update_plan(plan, "review", "completed", [review], "Reviewed fixture")
        report = self.root / "report.md"
        report.write_text("Simulated fixture only")
        analysis.update_plan(plan, "report", "completed", [report], "Report")
        self.assertTrue(analysis.check_plan(plan)["complete"])
        analysis.update_plan(plan, "runtime", "blocked", [], "Observation expired")
        self.assertFalse(analysis.check_plan(plan)["complete"])
        self.assertEqual(plan["steps"][-1]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
