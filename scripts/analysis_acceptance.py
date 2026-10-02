#!/usr/bin/env python3
"""Exercise package → route → evidence → review → report via the shared queue."""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.client import Client
from workbench.common import atomic_json, WorkbenchError


def acceptance(client, directory, apk=None, expected_permission=None, expected_code=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = "analysis-acceptance-" + uuid.uuid4().hex
    jobs = []
    operations = client.call("operations.list")["operations"]

    def run(operation, args, successful=True):
        subcommand = operations[operation].get("subcommand")
        if subcommand:
            args = [subcommand, *args]
        job = client.submit(operation=operation, args=args, request_key=key + "-" + str(len(jobs)), timeout=120)
        record = {"id": job["id"], "operation": operation, "state": job["state"]}
        jobs.append(record)
        result = client.wait(job["id"], 150)
        record["state"] = result["state"]
        if successful and result["state"] != "succeeded":
            raise ValueError(operation + " failed: " + str(result.get("result")))
        return result

    def save(path, value):
        # Project evidence writes also participate in resource coordination.
        contents = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        code = "from pathlib import Path; p=Path(" + repr(str(path)) + "); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(" + repr(contents) + ")"
        run("analysis.python", ["--code", code, "--cwd", str(client.project), "--write-path", str(path)])

    plan = directory / "plan.json"
    real = apk is not None
    try:
        samples = [Path(apk).resolve()] if real else [directory / "base.apk", directory / "split.apk"]
        if not real:
            code = "import zipfile,json\nfrom pathlib import Path\n"
            for path, entries in zip(samples, [
                {"AndroidManifest.xml": "layout fixture, not an installable APK", "classes.dex": "layout fixture", "assets/config.json": '{"endpoint":"https://example.invalid/fixture"}'},
                {"lib/arm64-v8a/libflutter.so": "layout fixture", "assets/flutter_assets/AssetManifest.json": "{}"},
            ]):
                code += "with zipfile.ZipFile(" + repr(str(path)) + ", 'w') as z:\n"
                for name, data in entries.items():
                    code += " z.writestr(" + repr(name) + "," + repr(data) + ")\n"
            run("analysis.python", ["--code", code, "--cwd", str(client.project), "--write-path", str(directory)])
        sample_args = [value for p in samples for value in ("--apk", str(p))]
        recon = directory / "recon.json"
        run("analysis.recon", [*sample_args, "--output", str(recon)])
        question = "分析权限风险" if real else "查看安装包内配置和框架结构"
        route = directory / "route.json"
        run("analysis.route", ["--question", question, "--recon", str(recon), "--output", str(route)])
        parsed_route = json.loads(route.read_text())
        if real and "static.permission_audit" not in parsed_route["suggested_operations"]:
            raise ValueError("Permission audit was not selected")
        if not real and not parsed_route["specialist_required"]:
            raise ValueError("Framework in split APK was not recognized")
        run("analysis.plan", ["--question", question, "--recon", str(recon), "--output", str(plan)])
        initial = directory / "initial-completion.json"
        result = run("analysis.plan_check", ["--plan", str(plan), "--output", str(initial)], False)
        if result["state"] != "failed" or json.loads(initial.read_text())["complete"]:
            raise ValueError("Unreviewed plan incorrectly completed")
        if real:
            audit = directory / "permission-audit"
            result = run("static.permission_audit", [*sample_args, "--output-dir", str(audit)])
            if result["result"].get("analysis_status") != {"collection_only": True, "review_required": True}:
                raise ValueError("Collector review requirement was lost")
            inventory = json.loads((audit / "permissions-inventory.json").read_text())
            text = (audit / "permissions-evidence.md").read_text()
            if expected_permission and expected_permission not in json.dumps(inventory):
                raise ValueError("Expected permission missing")
            if expected_code and expected_code not in text:
                raise ValueError("Expected code evidence missing")
            evidence = audit / "permissions-evidence.md"
            claim = "The collector recorded the supplied fixture permission and code check; runtime behavior was not verified."
            # Without an expected assertion, this run tests collection, not app risk.
            if not expected_permission and not expected_code:
                claim = "Permission collection produced code evidence; findings still require app-specific interpretation."
        else:
            evidence = directory / "config-evidence.json"
            run("analysis.preview", ["--apk", str(samples[0]), "--entry", "assets/config.json", "--output", str(evidence)])
            if "https://example.invalid/fixture" not in evidence.read_text():
                raise ValueError("Known configuration value missing")
            claim = "The generated layout fixture contains the expected configuration endpoint and Flutter markers in its split; no app runtime behavior is inferred."
        run("analysis.plan_update", ["--plan", str(plan), "--step", "static", "--status", "completed", "--evidence", str(evidence), "--note", "Checked the known fixture assertion"])
        review = directory / "review.json"
        save(review, {"review_complete": True, "claims": [{"claim": claim, "evidence": [str(evidence), str(recon)]}], "limitations": ["No device or Hook compatibility validated by this static acceptance"]})
        run("analysis.plan_update", ["--plan", str(plan), "--step", "review", "--status", "completed", "--evidence", str(review), "--note", "Reviewed the fixture assertion and limited scope"])
        report = directory / "report.md"
        save(report, claim + "\n\nEvidence: " + str(evidence) + "\n\nScope: static acceptance only.\n")
        run("analysis.plan_update", ["--plan", str(plan), "--step", "report", "--status", "completed", "--evidence", str(report), "--note", "Final acceptance report"])
        completion = directory / "completion.json"
        run("analysis.plan_check", ["--plan", str(plan), "--output", str(completion)])
        return {"pass": True, "sample_kind": "provided_apk" if real else "generated_structure_fixture", "jobs": jobs, "completion": str(completion), "device_validation": False}
    except (ValueError, OSError, TimeoutError, WorkbenchError) as exc:
        if plan.exists():
            try:
                run("analysis.plan_update", ["--plan", str(plan), "--step", "static", "--status", "blocked", "--note", str(exc)], False)
            except Exception:
                pass
        return {"pass": False, "error": str(exc), "jobs": jobs, "device_validation": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--expected-permission")
    parser.add_argument("--expected-code")
    args = parser.parse_args()
    result = acceptance(Client(args.project, session="analysis-acceptance"), args.output_dir, args.apk, args.expected_permission, args.expected_code)
    atomic_json(args.output_dir / "acceptance.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
