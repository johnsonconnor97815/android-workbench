#!/usr/bin/env python3
"""Compare FIFO, an App-reuse rule, and real Codex on the same simulated device queue."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.client import Client
from workbench.common import atomic_json, rpc, WorkbenchError
from workbench.service import DEFAULTS


def trial(mode, root):
    root.mkdir()
    state = root / "state"
    state.mkdir()
    project = root / "project"
    project.mkdir()
    adb = project / "adb.py"
    adb.write_text(
        "#!"
        + sys.executable
        + '\nimport sys\na=sys.argv[1:]\nif "activities" in a:print("mResumedActivity: com.example.x/.Home")\nelif "pidof" in a:print("123")\nelse:print("test-boot")\n'
    )
    adb.chmod(0o755)
    rule = project / "rule.py"
    rule.write_text(
        "import sys,json\np=json.loads(sys.stdin.read().split('\\n')[-1]); candidates=p['candidates']; current=p['context'].get('app')\nx=min(candidates,key=lambda j:(j.get('app')!=current,j['estimate_seconds'],-j['wait_seconds']))\nprint(json.dumps({'job_id':x['job_id'],'reason':'current-app then shorter task rule'}))\n"
    )
    model = (
        {"backend": "none"}
        if mode == "fifo"
        else (
            {"backend": "command", "argv": [sys.executable, str(rule)], "timeout": 2}
            if mode == "state_rule"
            else {"backend": "codex", "timeout": 15, "calls_per_hour": 30}
        )
    )
    atomic_json(
        state / "config.json",
        {
            **DEFAULTS,
            "test_mode": True,
            "min_free_bytes": 0,
            "llm": model,
            "adb": str(adb),
            "devices": {"phone": {"serial": "fake"}},
        },
    )
    atomic_json(
        project / "workbench.project.json",
        {"version": 1, "python": sys.executable, "operations": {}},
    )
    with (root / "service.log").open("w") as log:
        service = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/workbench.py"),
                "--state",
                str(state),
                "serve",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                try:
                    rpc(state, "service.capabilities")
                    break
                except WorkbenchError:
                    time.sleep(0.03)
            a = Client(project, state, "a")
            b = Client(project, state, "b")
            observation = a.wait(
                a.submit(operation="device.observe", device="phone")["id"], 10
            )
            assert observation["state"] == "succeeded", observation
            ready = time.time() + 0.5
            switch = a.submit(
                operation="test.simulate",
                device="phone",
                app="com.example.y",
                estimate=30,
                not_before=ready,
                purpose="Switch to App Y and collect a separate observation",
                steps=[{"action": "work", "seconds": 0.1}],
            )
            reuse = b.submit(
                operation="test.simulate",
                device="phone",
                app="com.example.x",
                estimate=2,
                not_before=ready,
                purpose="Quick observation in the currently open App X",
                steps=[{"action": "work", "seconds": 0.1}],
            )
            results = [a.wait(j["id"], 45) for j in (switch, reuse)]
            assert all(j["state"] == "succeeded" for j in results), results
            ordered = sorted(results, key=lambda j: j["started"])
            roles = {switch["id"]: "switch", reuse["id"]: "reuse"}
            events = a.call("jobs.explain", {"id": ordered[0]["id"]})["decisions"]
            return {
                "mode": mode,
                "order": [roles[j["id"]] for j in ordered],
                "queue_ready_to_first_start_seconds": ordered[0]["started"] - ready,
                "queue_ready_to_all_finished_seconds": max(
                    j["finished"] for j in results
                )
                - ready,
                "decisions": events,
                "jobs": results,
            }
        finally:
            service.terminate()
            service.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Use a new output file")
    with tempfile.TemporaryDirectory(prefix="awb-real-model-") as folder:
        results = [
            trial(mode, Path(folder) / mode) for mode in ("fifo", "state_rule", "codex")
        ]
    record = {
        "scope": "real policy backends with identical simulated device/queue; no real App timing claim",
        "measured_at": time.time(),
        "trials": results,
    }
    atomic_json(args.output, record)
    print(
        json.dumps(
            [
                {
                    k: r[k]
                    for k in (
                        "mode",
                        "order",
                        "queue_ready_to_first_start_seconds",
                        "queue_ready_to_all_finished_seconds",
                    )
                }
                for r in results
            ],
            indent=2,
        )
    )
    if results[2]["order"] != ["reuse", "switch"]:
        raise SystemExit(
            "Real model did not select App reuse; inspect decisions including fallback"
        )


if __name__ == "__main__":
    main()
