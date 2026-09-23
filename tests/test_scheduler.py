import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.client import Client
from workbench.common import WorkbenchError, atomic_json, rpc
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-test-")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "workbench.project.json").write_text(
            json.dumps({"version": 1, "python": sys.executable, "operations": {}})
        )
        config = {
            **DEFAULTS,
            "test_mode": True,
            "min_free_bytes": 0,
            "devices": {"one": {"serial": "fake-one"}, "two": {"serial": "fake-two"}},
        }
        atomic_json(self.state / "config.json", config)
        self.log = (self.root / "service.log").open("wb")
        self.proc = self.start_service()
        self.a = Client(self.project, self.state, "a")
        self.b = Client(self.project, self.state, "b")

    def start_service(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/workbench.py"),
                "--state",
                str(self.state),
                "serve",
            ],
            stdout=self.log,
            stderr=self.log,
        )
        for _ in range(100):
            try:
                rpc(self.state, "service.capabilities")
                return proc
            except WorkbenchError:
                time.sleep(0.03)
        self.fail("No service")

    def tearDown(self):
        try:
            for j in self.a.call("jobs.list"):
                if j["state"] not in {
                    "succeeded",
                    "failed",
                    "partial",
                    "cancelled",
                    "expired",
                    "needs_recovery",
                }:
                    owner = (
                        self.a
                        if self.a.call("jobs.status", {"id": j["id"]})
                        else self.b
                    )
                    try:
                        owner.call("jobs.cancel", {"id": j["id"]})
                    except WorkbenchError:
                        try:
                            self.b.call("jobs.cancel", {"id": j["id"]})
                        except WorkbenchError:
                            pass
            time.sleep(0.25)
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.log.close()
            self.temp.cleanup()

    def submit(self, client=None, device="one", seconds=0.3, **kw):
        return (client or self.a).submit(
            operation="test.simulate",
            device=device,
            steps=[{"action": "work", "seconds": seconds}],
            **kw,
        )

    def wait_state(self, job, states, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = self.a.call("jobs.status", {"id": job["id"]})
            if value["state"] in states:
                return value
            time.sleep(0.03)
        self.fail(f"Timeout: {value}")

    def events(self, j):
        return self.a.call("jobs.subscribe", {"id": j["id"]})["events"]

    def test_same_device_serial_other_device_parallel(self):
        a = self.submit(seconds=0.8)
        self.wait_state(a, {"running"})
        b = self.submit(self.b)
        c = self.submit(device="two")
        results = [self.a.wait(j["id"], 8) for j in (a, b, c)]
        self.assertEqual([j["state"] for j in results], ["succeeded"] * 3)
        self.assertGreaterEqual(results[1]["started"], results[0]["finished"])
        self.assertLess(results[2]["started"], results[0]["finished"])

    def test_idempotency_and_session_ownership(self):
        key = uuid.uuid4().hex
        a = self.submit(request_key=key)
        b = self.submit(request_key=key)
        self.assertEqual(a["id"], b["id"])
        with self.assertRaisesRegex(WorkbenchError, "conflicts"):
            self.submit(request_key=key, seconds=0.6)
        with self.assertRaisesRegex(WorkbenchError, "different session"):
            self.b.call("jobs.cancel", {"id": a["id"]})
        self.a.wait(a["id"], 8)

    def test_checkpoint_inserts_compatible_child_and_resumes(self):
        a = self.a.submit(
            operation="test.simulate",
            device="one",
            app="x",
            steps=[
                {"action": "work", "seconds": 0.15},
                {
                    "action": "checkpoint",
                    "seconds": 1,
                    "budget": 2,
                    "max_insertions": 1,
                    "allow": ["test.simulate"],
                },
                {"action": "work", "seconds": 0.15},
            ],
        )
        self.wait_state(a, {"running"})
        incompatible = self.submit(self.b, app="y", seconds=0.1)
        b = self.submit(
            self.b, app="x", seconds=0.05, estimate=0.05, test_insertable=True
        )
        ar = self.a.wait(a["id"], 8)
        br = self.a.wait(b["id"], 8)
        cr = self.a.wait(incompatible["id"], 8)
        self.assertEqual((ar["state"], br["state"], cr["state"]), ("succeeded",) * 3)
        self.assertEqual(br["parent"], a["id"])
        self.assertLess(br["finished"], ar["finished"])
        self.assertGreaterEqual(cr["started"], ar["finished"])

    def test_cancellation_is_responsive(self):
        a = self.submit(seconds=3)
        self.wait_state(a, {"running"})
        b = self.submit(self.b)
        started = time.monotonic()
        cancelled = self.b.call("jobs.cancel", {"id": b["id"]})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertLess(time.monotonic() - started, 0.8)
        self.a.call("jobs.cancel", {"id": a["id"]})
        self.assertEqual(self.a.wait(a["id"], 8)["state"], "cancelled")

    def test_crash_blocks_device_without_replay(self):
        a = self.a.submit(
            operation="test.simulate", device="one", steps=[{"action": "crash"}]
        )
        self.wait_state(a, {"needs_recovery"})
        b = self.submit(self.b)
        time.sleep(0.3)
        self.assertEqual(self.a.call("jobs.status", {"id": b["id"]})["state"], "queued")
        self.assertEqual(sum(e["kind"] == "started" for e in self.events(a)), 1)

    def test_environment_writer_prevents_new_readers(self):
        r = {"key": "path:/example/env", "mode": "read"}
        w = {**r, "mode": "write"}
        a = self.submit(seconds=0.8, test_resources=[r])
        self.wait_state(a, {"running"})
        b = self.submit(self.b, device="two", seconds=0.2, test_resources=[w])
        c = self.submit(device="two", seconds=0.05, test_resources=[r])
        ar = self.a.wait(a["id"], 8)
        br = self.a.wait(b["id"], 8)
        cr = self.a.wait(c["id"], 8)
        self.assertGreaterEqual(br["started"], ar["finished"])
        self.assertGreaterEqual(cr["started"], br["finished"])

    def test_invalid_model_does_not_grant_unknown_job(self):
        # Policy validation is exercised through the service with a real subprocess backend.
        from workbench.policy import Policy

        script = self.root / "bad.py"
        script.write_text(
            'print(\'{"job_id":"made-up","reason":"ignore constraints"}\')'
        )
        policy = Policy(
            {"llm": {"backend": "command", "argv": [sys.executable, str(script)]}}
        )
        jobs = [
            {
                "id": x,
                "created": time.time(),
                "priority": 0,
                "revision": 1,
                "spec": {"purpose": x, "estimate": 1, "operation": "test"},
            }
            for x in ("a", "b")
        ]
        selected, _, info = policy.choose(jobs, {})
        self.assertEqual(selected, "a")
        self.assertEqual(info["mode"], "fallback")

    def register_script(self, name, body, **entry):
        source = self.project / "operations"
        source.mkdir(exist_ok=True)
        script = source / (name + ".py")
        script.write_text(body)
        path = self.project / "workbench.project.json"
        manifest = json.loads(path.read_text())
        manifest["operations"][name] = {
            "script": str(script.relative_to(self.project)),
            "source": "operations",
            **entry,
        }
        path.write_text(json.dumps(manifest))
        return script

    def test_registered_operation_preserves_private_python_environment(self):
        private = self.project / "private-python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(private)], check=True
        )
        self.register_script(
            "private",
            "import sys; print(sys.prefix)\n",
            readonly=True,
            python="private-python/bin/python",
        )
        job = self.a.submit(operation="private", args=[])
        result = self.a.wait(job["id"], 8)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(
            (Path(result["directory"]) / "stdout.log").read_text().strip(), str(private)
        )
        self.register_script(
            "missing-python",
            "raise AssertionError('must not run')\n",
            readonly=True,
            python="missing/bin/python",
        )
        with self.assertRaisesRegex(WorkbenchError, "Registered Python is missing"):
            self.a.submit(operation="missing-python", args=[])

    def test_legacy_adapter_and_partial_result(self):
        self.register_script(
            "partial",
            "import json,sys\nfrom pathlib import Path\np=Path(sys.argv[2]);p.mkdir()\n(p/'summary.json').write_text(json.dumps({'status':'partial'}))\nraise SystemExit(1)\n",
            readonly=True,
            partial_codes=[1],
            outputs=["--output"],
        )
        output = self.project / "result"
        j = self.a.submit(operation="partial", args=["--output", str(output)])
        result = self.a.wait(j["id"], 8)
        self.assertEqual(result["state"], "partial", result)
        self.assertEqual(result["result"]["exit_code"], 1)
        self.assertTrue(output.joinpath("summary.json").exists())

    def test_queued_source_change_never_runs_new_code(self):
        script = self.register_script("pinned", "print('old')\n", readonly=True)
        running = self.submit(seconds=0.7)
        self.wait_state(running, {"running"})
        # Use a device legacy adapter so the script waits for the occupied device.
        manifest = json.loads((self.project / "workbench.project.json").read_text())
        manifest["operations"]["pinned"]["serial"] = 0
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        j = self.a.submit(operation="pinned", device="one", args=["fake-one"])
        script.write_text("raise RuntimeError('NEW CODE MUST NOT RUN')\n")
        result = self.a.wait(j["id"], 8)
        self.assertEqual(result["state"], "failed")
        self.assertIn("Source changed", result["result"]["reason"])

    def test_service_restart_does_not_replay_active_task(self):
        a = self.submit(seconds=2)
        self.wait_state(a, {"running"})
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()
        recovered = self.wait_state(a, {"needs_recovery", "failed"}, timeout=8)
        self.assertNotEqual(recovered["state"], "succeeded")
        self.assertEqual(sum(e["kind"] == "started" for e in self.events(a)), 1)

    def test_unknown_adapter_fields_cannot_change_lock_mode(self):
        with self.assertRaisesRegex(WorkbenchError, "adapter-owned"):
            self.a.submit(
                operation="test.simulate", device="one", recovery=True, resources=[]
            )

    def test_dependency_does_not_hold_phone_while_waiting(self):
        upstream = self.submit(device="two", seconds=0.5)
        downstream = self.submit(seconds=0.1, dependencies=[upstream["id"]])
        other = self.submit(self.b, seconds=0.1)
        other_result = self.a.wait(other["id"], 8)
        downstream_result = self.a.wait(downstream["id"], 8)
        self.assertLess(other_result["finished"], downstream_result["started"])

    def test_background_descendant_blocks_release(self):
        self.register_script(
            "background",
            "import subprocess,sys\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(0.8)'],start_new_session=True)\n",
            serial=0,
        )
        j = self.a.submit(operation="background", device="one", args=["fake-one"])
        result = self.a.wait(j["id"], 8)
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["result"]["cleanup_ok"])
        states = self.a.call("resources.state")
        self.assertTrue(
            any(
                x["key"] == "device:one" and x["status"] == "needs_recovery"
                for x in states
            )
        )
        time.sleep(0.9)

    def test_bound_scene_expiry_does_not_capture_another_scene(self):
        a = self.submit(seconds=0.15)
        self.a.wait(a["id"], 8)
        b = self.submit(self.b, scene=a["id"], test_insertable=True)
        self.assertEqual(self.a.wait(b["id"], 8)["state"], "expired")

    def test_apk_device_release_precedes_local_validation(self):
        import shutil

        sys.path.insert(
            0,
            str(ROOT / "skills/android-workbench/components/apk-export/tests"),
        )
        from fixtures import (
            ADB_SCRIPT,
            AAPT2_SCRIPT,
            APKSIGNER_SCRIPT,
            PACKAGE,
            create_apk,
            dumpsys,
            write_executable,
        )

        tools = self.project / "tools"
        tools.mkdir()
        for name, body in [
            ("adb", ADB_SCRIPT),
            ("aapt2", AAPT2_SCRIPT),
            ("apksigner", APKSIGNER_SCRIPT),
        ]:
            if name == "apksigner":
                body = body.replace(
                    "import ", "import time;time.sleep(.35)\nimport ", 1
                )
            write_executable(
                tools / name,
                body.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1),
            )
        base = self.project / "base.apk"
        create_apk(base)
        remote = "/data/app/com.example.target-abc/base.apk"
        fake = self.project / "fake.json"
        fake.write_text(
            json.dumps(
                {
                    "devices": "List of devices attached\nfake-one device model:Test transport_id:1\n",
                    "allowedSerials": ["fake-one"],
                    "getprop": "[ro.product.model]: [Test]\n[ro.build.version.sdk]: [35]\n[ro.build.version.release]: [15]\n[ro.build.fingerprint]: [test/product/device:15/id:user/test-keys]\n[ro.product.cpu.abilist]: [arm64-v8a]\n",
                    "pmPaths": ["package:" + remote + "\n"],
                    "dumpsys": [dumpsys(["base"])],
                    "remoteFiles": {remote: str(base)},
                }
            )
        )
        source = self.project / "apk"
        source.mkdir()
        for name in ("apk_pull.py", "android_tools.py"):
            shutil.copyfile(
                ROOT
                / "skills/android-workbench/components/apk-export/scripts"
                / name,
                source / name,
            )
        (self.project / "android-workbench").symlink_to(ROOT, target_is_directory=True)
        manifest = {
            "version": 1,
            "python": sys.executable,
            "env": {
                "FAKE_ADB_CONFIG": str(fake),
                "FAKE_ADB_STATE": str(self.project / "fake-state.json"),
            },
            "operations": {
                "apk.pull": {
                    "script": "apk/apk_pull.py",
                    "source": "apk",
                    "serial": "--serial",
                    "subcommand": "pull",
                    "readonly": True,
                    "outputs": ["--output"],
                }
            },
        }
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        j = self.a.submit(
            operation="apk.pull",
            device="one",
            args=[
                "pull",
                "--package",
                PACKAGE,
                "--serial",
                "fake-one",
                "--output",
                str(self.project / "export"),
                "--adb",
                str(tools / "adb"),
                "--aapt2",
                str(tools / "aapt2"),
                "--apksigner",
                str(tools / "apksigner"),
            ],
        )
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if any(e["kind"] == "device_released" for e in self.events(j)):
                break
            status = self.a.call("jobs.status", {"id": j["id"]})
            if status["state"] == "failed":
                self.fail(str(status))
            time.sleep(0.03)
        else:
            self.fail("Device phase did not release")
        other = self.submit(self.b, seconds=0.05)
        later = self.a.wait(other["id"], 8)
        export = self.a.wait(j["id"], 10)
        self.assertEqual(export["state"], "succeeded", export)
        self.assertLess(later["started"], export["finished"])
        self.assertTrue((self.project / "export/manifest.json").is_file())

    def test_live_external_lease_is_not_freed_by_cancel(self):
        from workbench.common import process_identity

        manifest = json.loads((self.project / "workbench.project.json").read_text())
        manifest["mcp_environments"] = ["environment"]
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        j = self.a.submit(
            operation="host.lease",
            lease_owner=process_identity(os.getpid()),
            lease_mode="environment",
            timeout=5,
        )
        self.wait_state(j, {"running"})
        response = self.a.call("jobs.cancel", {"id": j["id"]})
        self.assertEqual(response["state"], "running")
        self.assertTrue(self.a.call("leases.state", {"id": j["id"]})["draining"])
        self.a.call("leases.release", {"id": j["id"]})
        self.assertEqual(self.a.wait(j["id"], 8)["state"], "succeeded")

    def test_analysis_mcp_proxies_queue_shared_workspace(self):
        script = self.project / "fake_mcp.py"
        script.write_text(
            "import json,sys,time\nfor line in sys.stdin:\n r=json.loads(line)\n if r.get('method')=='tools/call':time.sleep(.25)\n if 'id' in r: print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'content':[{'type':'text','text':'done'}]}}),flush=True)\n"
        )
        processes = []
        try:
            for _ in range(2):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "scripts/workbench.py"),
                        "--state",
                        str(self.state),
                        "--project",
                        str(self.project),
                        "mcp-proxy",
                        "--",
                        sys.executable,
                        str(script),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append(process)
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())["id"], 1)
            for process in processes:
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "test", "arguments": {}},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
            for process in processes:
                self.assertEqual(json.loads(process.stdout.readline())["id"], 2)
            time.sleep(0.3)
            jobs = [
                x
                for x in self.a.call("jobs.list")
                if x["purpose"].startswith("Analysis MCP tool")
            ]
            self.assertEqual(len(jobs), 2)
            jobs.sort(key=lambda x: x["started"])
            self.assertGreaterEqual(jobs[1]["started"], jobs[0]["finished"])
        finally:
            for process in processes:
                if process.stdin:
                    process.stdin.close()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
            end = time.monotonic() + 4
            while time.monotonic() < end:
                leases = [
                    x
                    for x in self.a.call("jobs.list")
                    if x["operation"] == "host.lease"
                ]
                if all(x["state"] == "succeeded" for x in leases):
                    break
                time.sleep(0.1)
            self.assertTrue(all(x["state"] == "succeeded" for x in leases), leases)

    def test_nested_mcp_check_reuses_environment_grant(self):
        upstream = self.project / "echo.py"
        upstream.write_text(
            "import sys\nfor line in sys.stdin: print(line,flush=True)\n"
        )
        entry = self.project / "probe.py"
        command = [
            sys.executable,
            str(ROOT / "scripts/workbench.py"),
            "--state",
            str(self.state),
            "--project",
            str(self.project),
            "mcp-proxy",
            "--",
            sys.executable,
            str(upstream),
        ]
        entry.write_text(
            "import subprocess\nr=subprocess.run("
            + repr(command)
            + ",input='nested-ok\\n',capture_output=True,text=True,timeout=3)\nassert r.returncode==0,r.stderr\nassert 'nested-ok' in r.stdout,r.stdout\n"
        )
        manifest = {
            "version": 1,
            "python": sys.executable,
            "environments": ["env"],
            "operations": {
                "nested": {
                    "script": "probe.py",
                    "source": ".",
                    "mutates_environment": True,
                    "uses_mcp": True,
                }
            },
        }
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        result = self.a.wait(self.a.submit(operation="nested")["id"], 8)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertFalse(
            any(j["operation"] == "host.lease" for j in self.a.call("jobs.list"))
        )

    def test_cleanup_error_blocks_even_when_legacy_exits_zero(self):
        script = self.project / "legacy.py"
        script.write_text(
            "import sys,json\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--record')+1]).write_text(json.dumps({'pass':True,'cleanup_errors':['detach failed']}))\n"
        )
        manifest = {
            "version": 1,
            "python": sys.executable,
            "operations": {
                "cleanup": {
                    "script": "legacy.py",
                    "source": ".",
                    "serial": "--serial",
                    "outputs": ["--record"],
                    "result_file": ".",
                }
            },
        }
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        result = self.a.wait(
            self.a.submit(
                operation="cleanup",
                device="one",
                args=[
                    "--serial",
                    "fake-one",
                    "--record",
                    str(self.root / "record.json"),
                ],
            )["id"],
            8,
        )
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["result"]["cleanup_ok"])
        queued = self.submit(self.b)
        time.sleep(0.25)
        self.assertEqual(
            self.a.call("jobs.status", {"id": queued["id"]})["state"], "queued"
        )
        self.assertTrue(self.a.call("resources.state"))

    def test_hook_observation_requires_explicit_acceptance(self):
        a = self.a.submit(
            operation="test.simulate",
            device="one",
            app="x",
            hooks=["x"],
            steps=[
                {"action": "work", "seconds": 0.1},
                {
                    "action": "checkpoint",
                    "seconds": 0.7,
                    "budget": 1,
                    "max_insertions": 1,
                    "allow": ["test.simulate"],
                },
            ],
        )
        self.wait_state(a, {"running"})
        baseline = self.submit(
            self.b, app="x", seconds=0.05, estimate=0.05, test_insertable=True
        )
        compatible = self.submit(
            self.b,
            app="x",
            seconds=0.05,
            estimate=0.05,
            test_insertable=True,
            accept_hooks=True,
        )
        ar = self.a.wait(a["id"], 8)
        br = self.a.wait(baseline["id"], 8)
        cr = self.a.wait(compatible["id"], 8)
        self.assertEqual(cr["parent"], a["id"])
        self.assertIsNone(br["parent"])
        self.assertGreaterEqual(br["started"], ar["finished"])

    def test_native_probe_failure_with_verified_cleanup_does_not_block(self):
        script = self.project / "probe.py"
        script.write_text(
            "import sys,json\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--record')+1]).write_text(json.dumps({'pass':False,'cleanup_errors':[],'cleanup_status':'passed'}))\nraise SystemExit(1)\n"
        )
        manifest = {
            "version": 1,
            "python": sys.executable,
            "operations": {
                "frida.probe_native": {
                    "script": "probe.py",
                    "source": ".",
                    "serial": "--serial",
                    "outputs": ["--record"],
                    "result_file": ".",
                }
            },
        }
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        result = self.a.wait(
            self.a.submit(
                operation="frida.probe_native",
                device="one",
                args=[
                    "--serial",
                    "fake-one",
                    "--record",
                    str(self.root / "probe.json"),
                ],
            )["id"],
            8,
        )
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["result"]["cleanup_ok"])
        self.assertEqual(
            self.a.wait(self.submit(self.b)["id"], 8)["state"], "succeeded"
        )

    def test_invalid_start_time_cannot_poison_the_queue(self):
        with self.assertRaisesRegex(WorkbenchError, "not_before"):
            self.submit(not_before="tomorrow")
        with self.assertRaisesRegex(WorkbenchError, "boolean"):
            self.submit(accept_hooks="false")
        self.assertEqual(self.a.wait(self.submit()["id"], 8)["state"], "succeeded")

    def test_mcp_config_wrapper_is_stable_and_idempotent(self):
        from workbench.bridge import managed_mcp_spec

        manifest = self.project / "workbench.project.json"
        doc = json.loads(manifest.read_text())
        doc["workbench_root"] = str(ROOT)
        manifest.write_text(json.dumps(doc))
        raw = {"command": "/existing/mcp", "args": ["--stdio"], "cwd": "/work"}
        wrapped = managed_mcp_spec(self.project, raw)
        self.assertEqual(wrapped, managed_mcp_spec(self.project, wrapped))
        self.assertEqual(wrapped["args"][-2:], ["/existing/mcp", "--stdio"])
        self.assertEqual(wrapped["args"].count("mcp-proxy"), 1)

    def test_mcp_clients_connect_to_same_service(self):
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "service_capabilities", "arguments": {}},
            },
        ]
        p = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/workbench.py"),
                "--state",
                str(self.state),
                "--project",
                str(self.project),
                "mcp",
            ],
            input="\n".join(json.dumps(x) for x in requests) + "\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        value = json.loads(
            json.loads(p.stdout.splitlines()[1])["result"]["content"][0]["text"]
        )
        self.assertEqual(value["pid"], self.proc.pid)


class PageValidationTest(unittest.TestCase):
    def test_page_changed_after_capture_retains_failed_evidence(self):
        from types import SimpleNamespace
        from workbench.worker import Device

        with tempfile.TemporaryDirectory() as folder:
            runner = SimpleNamespace(
                spec={
                    "operation": "device.screenshot",
                    "ui_expect": [{"text": "expected"}],
                },
                grant={"job": "fixture"},
                directory=Path(folder),
                cleanup_errors=[],
            )
            device = Device(runner)
            device.observe = lambda: {
                "boot_id": "boot",
                "activity": "app/Home",
                "app": "app",
                "pid": "1",
            }
            calls = []

            def adb(*args, **kwargs):
                calls.append(args)
                if "screencap" in args:
                    return b"\x89PNG\r\n\x1a\nfixture"
                if args[:2] == ("exec-out", "cat"):
                    return (
                        b'<hierarchy><node text="expected"/></hierarchy>'
                        if "before" in args[2]
                        else b'<hierarchy><node text="changed"/></hierarchy>'
                    )
                return ""

            device.adb = adb
            with self.assertRaisesRegex(WorkbenchError, "UI condition"):
                device.screenshot()
            self.assertTrue((Path(folder) / "screenshot.png").is_file())
            self.assertTrue(any(args[:3] == ("shell", "rm", "-f") for args in calls))


if __name__ == "__main__":
    unittest.main()
