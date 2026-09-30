import hashlib
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
from workbench.common import WorkbenchError, atomic_json, process_identity, rpc
from workbench.registry import normalize
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

    def test_device_management_works_without_analysis_project(self):
        unregistered = self.root / "not-an-analysis-project"
        unregistered.mkdir()
        fake_adb = self.root / "device-adb"
        fake_adb.write_text(
            "#!" + sys.executable + "\nprint('List of devices attached')\n"
            "print('visible-serial device product:fake model:fake')\n"
        )
        fake_adb.chmod(0o755)
        fake_fastboot = self.root / "device-fastboot"
        fake_fastboot.write_text(
            "#!" + sys.executable + "\nprint('fastboot-serial fastboot')\n"
        )
        fake_fastboot.chmod(0o755)
        config = json.loads((self.state / "config.json").read_text())
        config["adb"] = str(fake_adb)
        config["fastboot"] = str(fake_fastboot)
        atomic_json(self.state / "config.json", config)
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()

        client = Client(unregistered, self.state, "device-manager")
        self.assertEqual(client.mode, "device_management")
        self.assertEqual(client.project, str(self.state / "device-management"))
        discovered = client.call("devices.discover")
        self.assertEqual(
            [item["serial"] for item in discovered["devices"]],
            ["visible-serial", "fastboot-serial"],
        )
        client.call(
            "devices.register", {"id": "visible-phone", "serial": "visible-serial"}
        )
        registered = client.call("devices.list")
        self.assertIn("visible-phone", [item["id"] for item in registered])

        operations = client.call("operations.list")["operations"]
        self.assertTrue(operations)
        self.assertTrue(all(name.startswith("device_manager.") for name in operations))
        self.assertEqual(
            operations["device_manager.image_download"]["default_paths"],
            {"--cache-dir": "device-manager/images"},
        )
        self.assertEqual(
            operations["device_manager.research"]["shared_outputs"],
            ["--cache"],
        )
        self.assertEqual(
            operations["device_manager.image_download"]["shared_outputs"],
            ["--cache-dir"],
        )
        recovery = operations["device_manager.recover_image_cache"]
        self.assertEqual(
            recovery["recovery_for"],
            [
                "device_manager.image_download",
                "device_manager.recover_image_cache",
            ],
        )
        self.assertEqual(recovery["result_file"], ".")

        image = unregistered / "boot.img"
        image.write_bytes(b"boot image")
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        job = client.submit(
            operation="device_manager.image_verify",
            args=[
                "image-verify",
                "--image",
                str(image),
                "--sha256",
                digest,
                "--label",
                "boot",
            ],
            request_key="device-management-no-project",
        )
        result = client.wait(job["id"], 8)
        self.assertEqual(
            result["state"],
            "succeeded",
            (Path(result["directory"]) / "stderr.log").read_text(errors="replace"),
        )
        record = json.loads(
            (Path(result["directory"]) / "operation-result.json").read_text()
        )
        self.assertEqual(record["status"], "complete")

    def test_scene_logcat_step_is_bounded_and_clear_makes_it_mutating(self):
        directory = self.root / "normalized"
        directory.mkdir()
        config = {
            **DEFAULTS,
            "devices": {"one": {"serial": "fake-one", "adb": "fake-adb"}},
        }
        request = {
            "project": str(self.project),
            "operation": "device.scene",
            "device": "one",
            "steps": [
                {"action": "logcat", "lines": 100, "buffer": "main", "level": "E"}
            ],
            "request_key": "logcat-readonly",
        }
        spec = normalize(request, config, directory)
        self.assertTrue(spec["readonly"])
        self.assertEqual(spec["steps"][0]["lines"], 100)

        request["steps"][0]["clear"] = True
        request["request_key"] = "logcat-clear"
        spec = normalize(request, config, directory)
        self.assertFalse(spec["readonly"])

    def test_launch_component_dollar_suffix_survives_device_shell(self):
        fake_adb = self.root / "fake-adb"
        fake_adb.write_text(
            "#!"
            + sys.executable
            + "\nimport subprocess,sys\nargs=sys.argv[1:]\n"
            "if args[:3] == ['-s', 'fake-one', 'shell']:\n"
            "    command=' '.join(args[3:])\n"
            "    if command.startswith('am start'):\n"
            "        subprocess.run(['sh', '-c', 'echo component=' + command.rsplit(' ', 1)[1]])\n"
            "        raise SystemExit(0)\n"
        )
        fake_adb.chmod(0o755)
        config = json.loads((self.state / "config.json").read_text())
        config["devices"]["one"]["adb"] = str(fake_adb)
        atomic_json(self.state / "config.json", config)
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()

        job = self.a.submit(
            operation="device.scene",
            device="one",
            request_key="launch-dollar-component",
            steps=[{"action": "launch", "component": "x/.A$B"}],
        )
        result = self.a.wait(job["id"], 8)
        self.assertEqual(result["state"], "succeeded", result)
        outputs = [
            path.read_text()
            for path in Path(result["directory"]).glob("command-*.stdout")
        ]
        self.assertIn("component=x/.A$B", "".join(outputs))

    def test_device_info_is_readonly_and_routed_to_the_device(self):
        directory = self.root / "normalized-info"
        directory.mkdir()
        config = {
            **DEFAULTS,
            "devices": {"one": {"serial": "fake-one", "adb": "fake-adb"}},
        }
        request = {
            "project": str(self.project),
            "operation": "device.info",
            "device": "one",
            "request_key": "device-info",
        }
        spec = normalize(request, config, directory)
        self.assertTrue(spec["readonly"])
        self.assertFalse(spec["insertable"])
        self.assertEqual(spec["steps"], [{"action": "info"}])
        self.assertIn("device.info", self.a.call("operations.list", {})["builtin"])

    def test_devices_state_reports_latest_cached_device_info(self):
        fake_adb = self.root / "info-adb"
        fake_adb.write_text(
            "#!"
            + sys.executable
            + "\nimport sys\nargs=sys.argv[1:]\n"
            "commands={\n"
            " ('shell','getprop'):'[ro.product.model]: [Test Model]\\n"
            "[ro.build.version.release]: [15]\\n[ro.build.version.sdk]: [35]\\n',\n"
            " ('shell','wm','size'):'Physical size: 1080x2400',\n"
            " ('shell','cat','/proc/meminfo'):'MemTotal: 8192000 kB',\n"
            " ('shell','df','-k','/data'):'/data 262144 131072 131072 50% /data',\n"
            " ('shell','settings','get','global','airplane_mode_on'):'0',\n"
            " ('shell','dumpsys','connectivity'):'Active default network: 100\\n"
            "Active network type: WIFI',\n"
            " ('shell','cmd','wifi','status'):'Wi-Fi is enabled\\nSSID: test, RSSI: -52',\n"
            " ('shell','pm','list','packages','com.android.vending'):"
            "'package:com.android.vending',\n"
            " ('shell','dumpsys','account'):'type=com.google',\n"
            " ('shell','cat','/proc/sys/kernel/random/boot_id'):'boot-id',\n"
            "}\n"
            "print(commands[tuple(args[2:])])\n"
        )
        fake_adb.chmod(0o755)
        config = json.loads((self.state / "config.json").read_text())
        config["devices"]["one"]["adb"] = str(fake_adb)
        atomic_json(self.state / "config.json", config)
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()

        job = self.a.submit(
            operation="device.info",
            device="one",
            request_key="state-device-info",
        )
        result = self.a.wait(job["id"], 8)
        self.assertEqual(result["state"], "succeeded", result)
        state = self.a.call("devices.state", {"device": "one"})
        self.assertEqual(state["last_device_info"]["job"], job["id"])
        self.assertEqual(state["last_device_info"]["data"]["schema"], 2)
        self.assertEqual(
            state["last_device_info"]["data"]["system"]["android_version"], "15"
        )
        self.assertTrue(state["last_device_info"]["data"]["wifi"]["connected"])
        self.assertTrue(state["last_device_info"]["data"]["google_play"]["logged_in"])

    def test_devices_report_active_project_occupancy(self):
        other_project = self.root / "other-project"
        other_project.mkdir()
        (other_project / "workbench.project.json").write_text(
            json.dumps({"version": 1, "python": sys.executable, "operations": {}})
        )
        other = Client(other_project, self.state, "other")

        active = self.submit(seconds=0.8)
        self.wait_state(active, {"running"})
        queued = other.submit(
            operation="test.simulate",
            device="one",
            steps=[{"action": "work", "seconds": 0.1}],
        )

        state = self.a.call("devices.state", {"device": "one"})
        self.assertTrue(state["project_occupied"])
        self.assertEqual(state["occupying_projects"], [str(self.project)])
        self.assertEqual(state["queued_jobs"], [queued["id"]])
        self.assertEqual(
            state["project"]["queued"],
            [{"source": "job", "job": queued["id"], "project": str(other_project)}],
        )
        self.assertFalse(state["project"]["conflict"])
        self.assertIsNone(state["last_device_info"])

        self.assertEqual(self.a.wait(active["id"], 8)["state"], "succeeded")
        self.assertEqual(other.wait(queued["id"], 8)["state"], "succeeded")
        end = time.monotonic() + 4
        while time.monotonic() < end:
            state = self.a.call("devices.state", {"device": "one"})
            if not state["project_occupied"]:
                break
            time.sleep(0.05)
        self.assertFalse(state["project_occupied"])
        self.assertEqual(state["occupying_projects"], [])

    def test_device_project_assignment_blocks_other_projects(self):
        other_project = self.root / "assigned-other-project"
        other_project.mkdir()
        (other_project / "workbench.project.json").write_text(
            json.dumps({"version": 1, "python": sys.executable, "operations": {}})
        )
        other = Client(other_project, self.state, "other")

        assigned = self.a.call(
            "devices.project_assign",
            {"device": "one", "project": str(self.project)},
        )
        self.assertEqual(assigned["project"], str(self.project))
        state = self.a.call("devices.state", {"device": "one"})
        self.assertEqual(state["project"]["assigned"], str(self.project))
        self.assertFalse(state["project"]["conflict"])

        with self.assertRaisesRegex(
            WorkbenchError, "Device is assigned to another project"
        ):
            other.submit(
                operation="test.simulate",
                device="one",
                steps=[{"action": "work", "seconds": 0.1}],
            )

        released = self.a.call("devices.project_release", {"device": "one"})
        self.assertNotIn("project", released)

    def test_devices_report_manual_project_occupancy(self):
        acquired = self.a.call("devices.manual_acquire", {"device": "one"})
        self.assertEqual(acquired, {"status": "manual", "handed_over": True})
        state = self.a.call("devices.state", {"device": "one"})
        self.assertEqual(state["availability"], "manual")
        self.assertTrue(state["project_occupied"])
        self.assertEqual(state["occupying_projects"], [str(self.project)])

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

    def test_shared_named_output_can_be_rewritten(self):
        self.register_script(
            "session",
            "import sys\npath=sys.argv[sys.argv.index('--state')+1]\n"
            "open(path, 'a').write('x')\n",
            outputs=["--state"],
            shared_outputs=["--state"],
        )
        state = self.root / "session.txt"
        for _ in range(2):
            job = self.a.submit(operation="session", args=["--state", str(state)])
            result = self.a.wait(job["id"], 8)
            self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(state.read_text(), "xx")

    def test_artifact_name_binding_publishes_and_resolves(self):
        output = self.root / "artifact.txt"
        self.register_script(
            "artifact_producer",
            "import sys\npath=sys.argv[sys.argv.index('--output')+1]\n"
            "open(path, 'w').write('artifact-data')\n",
            outputs=["--output"],
        )
        producer = self.a.submit(
            operation="artifact_producer",
            args=["--output", str(output)],
            artifact_outputs={"data": str(output)},
        )
        producer_result = self.a.wait(producer["id"], 8)
        self.assertEqual(producer_result["state"], "succeeded", producer_result)

        artifacts = self.a.call("artifacts.list", {"name": "data"})
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["job"], producer["id"])
        self.assertEqual(artifacts[0]["path"], str(output))

        self.register_script(
            "artifact_consumer",
            "import sys\npath=sys.argv[sys.argv.index('--input')+1]\n"
            "print(path)\n",
        )
        consumer = self.a.submit(
            operation="artifact_consumer",
            artifact_inputs={"data": "--input"},
        )
        consumer_result = self.a.wait(consumer["id"], 8)
        self.assertEqual(consumer_result["state"], "succeeded", consumer_result)
        spec = json.loads(
            (Path(consumer_result["directory"]) / "normalized.json").read_text()
        )
        input_index = spec["args"].index("--input")
        self.assertEqual(spec["args"][input_index + 1], str(output))
        self.assertIn(producer["id"], spec["dependencies"])

        directory_output = self.root / "artifact-directory"
        self.register_script(
            "artifact_directory_producer",
            "import json,sys\nfrom pathlib import Path\n"
            "folder=Path(sys.argv[sys.argv.index('--output')+1]);folder.mkdir()\n"
            "(folder/'result.json').write_text(json.dumps({'value':'directory'}))\n",
            outputs=["--output"],
        )
        directory_producer = self.a.submit(
            operation="artifact_directory_producer",
            args=["--output", str(directory_output)],
            artifact_outputs={"directory_data": str(directory_output / "result.json")},
        )
        directory_result = self.a.wait(directory_producer["id"], 8)
        self.assertEqual(
            directory_result["state"], "succeeded", directory_result
        )
        artifacts = self.a.call("artifacts.list", {"name": "directory_data"})
        self.assertEqual(artifacts[0]["path"], str(directory_output / "result.json"))

    def test_missing_artifact_input_is_rejected(self):
        self.register_script(
            "artifact_consumer",
            "import sys\nprint(sys.argv)\n",
        )
        with self.assertRaisesRegex(WorkbenchError, "Artifact is not published"):
            self.a.submit(
                operation="artifact_consumer",
                artifact_inputs={"missing": "--input"},
            )

    def test_evidence_retention_preserves_index_and_references(self):
        old = self.submit(seconds=0.1)
        old_result = self.a.wait(old["id"], 8)
        self.assertEqual(old_result["state"], "succeeded", old_result)

        holder = self.submit(self.b, seconds=1)
        self.wait_state(holder, {"running"})
        queued = self.submit(
            self.b, seconds=0.1, dependencies=[old["id"]], request_key="retention-dependent"
        )
        worker_log = Path(old_result["directory"]) / "worker.log"
        expired = time.time() - 31 * 86400
        os.utime(worker_log, (expired, expired))

        referenced = self.a.call("evidence.apply_retention", {})
        self.assertGreaterEqual(referenced["deleted_files"], 0)
        self.assertTrue(worker_log.is_file())

        self.a.wait(holder["id"], 8)
        self.a.wait(queued["id"], 8)
        unreferenced = self.a.call("evidence.apply_retention", {})
        self.assertFalse(worker_log.exists())
        directory = Path(old_result["directory"])
        for name in ("normalized.json", "result.json", "artifacts.json"):
            self.assertTrue((directory / name).is_file(), name)
        record = json.loads((directory / "retention.json").read_text())
        self.assertIn(worker_log.name, [Path(x["path"]).name for x in record["deleted_files"]])
        state = self.a.call("evidence.state", {})
        self.assertEqual(state["policy"]["retention_days"], 30)
        self.assertGreaterEqual(state["bytes"], 0)

    def test_resource_usage_is_recorded_and_output_budget_is_enforced(self):
        self.register_script(
            "usage",
            "import sys,time\npath=sys.argv[sys.argv.index('--output')+1]\n"
            "open(path,'w').write('usage-result')\ntime.sleep(.1)\n",
            outputs=["--output"],
        )
        output = self.root / "usage.txt"
        job = self.a.submit(
            operation="usage", args=["--output", str(output)], request_key="usage-ok"
        )
        result = self.a.wait(job["id"], 8)
        self.assertEqual(result["state"], "succeeded", result)
        usage = result["result"]["resource_usage"]
        self.assertGreaterEqual(usage["wall_seconds"], 0)
        self.assertGreaterEqual(usage["cpu_seconds"], 0)
        self.assertGreater(usage["max_rss_bytes"], 0)
        self.assertGreater(usage["internal_evidence_bytes"], 0)
        self.assertEqual(usage["external_output_bytes"], output.stat().st_size)

        config = json.loads((self.state / "config.json").read_text())
        config["max_job_output_bytes"] = 16
        atomic_json(self.state / "config.json", config)
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()
        self.register_script(
            "too_large",
            "import sys,time\npath=sys.argv[sys.argv.index('--output')+1]\n"
            "open(path,'wb').write(b'x'*64)\ntime.sleep(.8)\n",
            outputs=["--output"],
        )
        large_output = self.root / "too-large.txt"
        bounded = self.a.submit(
            operation="too_large",
            args=["--output", str(large_output)],
            request_key="usage-too-large",
        )
        bounded_result = self.a.wait(bounded["id"], 8)
        self.assertEqual(bounded_result["state"], "failed", bounded_result)
        self.assertIn("Task output budget exceeded", bounded_result["result"]["reason"])

    def test_multiple_hook_subscribers_share_one_scene(self):
        fake_adb = self.root / "fake-adb"
        fake_adb.write_text(
            "#!"
            + sys.executable
            + "\nimport sys\nargs=sys.argv[1:]\n"
            "if args[:3] == ['-s', 'fake-one', 'shell']:\n"
            "    command=args[3:]\n"
            "    if command[:2] == ['cat', '/proc/sys/kernel/random/boot_id']:\n"
            "        print('fake-boot')\n"
            "    elif command[:3] == ['dumpsys', 'activity', 'activities']:\n"
            "        print('mResumedActivity: x/.MainActivity')\n"
            "    elif command[:2] == ['pidof', 'x']:\n"
            "        print('123')\n"
        )
        fake_adb.chmod(0o755)
        config = json.loads((self.state / "config.json").read_text())
        config["devices"]["one"]["adb"] = str(fake_adb)
        atomic_json(self.state / "config.json", config)
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()

        parent = self.a.submit(
            operation="test.simulate",
            device="one",
            app="x",
            hooks=["x"],
            steps=[
                {"action": "work", "seconds": 0.1},
                {
                    "action": "checkpoint",
                    "seconds": 1,
                    "budget": 2,
                    "max_insertions": 2,
                    "allow": ["device.observe"],
                },
            ],
        )
        self.wait_state(parent, {"running"})
        first = self.a.call(
            "hooks.subscribe",
            {
                "scene": parent["id"],
                "device": "one",
                "request_key": "hook-subscriber-1",
            },
        )
        second = self.a.call(
            "hooks.subscribe",
            {
                "scene": parent["id"],
                "device": "one",
                "request_key": "hook-subscriber-2",
            },
        )
        parent_result = self.a.wait(parent["id"], 8)
        first_result = self.a.wait(first["id"], 8)
        second_result = self.a.wait(second["id"], 8)
        self.assertEqual(parent_result["state"], "succeeded", parent_result)
        self.assertEqual(first_result["state"], "succeeded", first_result)
        self.assertEqual(second_result["state"], "succeeded", second_result)
        self.assertEqual(first_result["parent"], parent["id"])
        self.assertEqual(second_result["parent"], parent["id"])
        spec = json.loads(
            (Path(parent_result["directory"]) / "normalized.json").read_text()
        )
        self.assertEqual(len(spec.get("hook_subscribers", [])), 2)

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

    def test_registered_recovery_only_repairs_declared_operations(self):
        self.register_script("broken", "raise RuntimeError('interrupted')\n", serial=0)
        failed = self.a.submit(operation="broken", device="one", args=["fake-one"])
        self.assertFalse(self.a.wait(failed["id"], 8)["result"]["cleanup_ok"])
        code = "import json,pathlib,sys\nout=pathlib.Path(sys.argv[2]);out.mkdir();(out/'result.json').write_text(json.dumps({'pass':True}))\n"
        self.register_script(
            "repair",
            code,
            serial=0,
            recovery=True,
            recovery_for=["broken"],
            outputs=["--output"],
            result_file="result.json",
        )
        # A caller cannot label an arbitrary adapter as a recovery operation.
        with self.assertRaisesRegex(WorkbenchError, "adapter-owned"):
            self.a.submit(
                operation="broken", device="one", args=["fake-one"], recovery=True
            )
        # The reviewed repair runs even while that device needs recovery.
        # Script uses positional output; --output remains present for resource locking.
        output = self.project / "recovered"
        repaired = self.a.submit(
            operation="repair",
            device="one",
            args=["fake-one", str(output), "--output", str(output)],
        )
        done = self.a.wait(repaired["id"], 8)
        self.assertEqual(done["state"], "succeeded", done.get("result"))
        self.assertFalse(
            any(x["key"] == "device:one" for x in self.a.call("resources.state"))
        )

    def test_device_recovery_releases_dead_adb_exclusive_service_lock(self):
        self.register_script(
            "adb_failure",
            "raise SystemExit(1)\n",
            serial=0,
            adb_exclusive=True,
        )
        failed = self.a.submit(operation="adb_failure", device="one", args=["fake-one"])
        failed_result = self.a.wait(failed["id"], 8)
        self.assertFalse(failed_result["result"]["cleanup_ok"])
        states = self.a.call("resources.state")
        self.assertTrue(
            any(
                x["key"] == "service:adb" and x["status"] == "needs_recovery"
                for x in states
            )
        )

        fake_adb = self.root / "fake-adb"
        fake_adb.write_text(
            "#!" + sys.executable + "\nimport sys\nargs=sys.argv[1:]\n"
            "if args[:3] == ['-s', 'fake-one', 'shell']:\n"
            "    command=args[3:]\n"
            "    if command[:2] == ['cat', '/proc/sys/kernel/random/boot_id']:\n"
            "        print('fake-boot')\n"
            "    elif command[:3] == ['dumpsys', 'activity', 'activities']:\n"
            "        print('mResumedActivity: io.fpsystem.sample/.MainActivity')\n"
            "    elif command[:2] == ['pidof', 'io.fpsystem.sample']:\n"
            "        print('123')\n"
        )
        fake_adb.chmod(0o755)
        config = json.loads((self.state / "config.json").read_text())
        config["devices"]["one"]["adb"] = str(fake_adb)
        atomic_json(self.state / "config.json", config)
        self.proc.kill()
        self.proc.wait()
        self.proc = self.start_service()

        recovered = self.a.call(
            "devices.recover", {"device": "one", "request_key": "recover-adb-lock"}
        )
        done = self.a.wait(recovered["id"], 8)
        self.assertEqual(done["state"], "succeeded", done.get("result"))
        self.assertEqual(self.a.call("resources.state"), [])

    def test_local_recovery_repairs_declared_shared_resources(self):
        old_code = (
            "import json,pathlib,sys\n"
            "out=pathlib.Path(sys.argv[2]);out.mkdir()\n"
            "(out/'result.json').write_text(json.dumps({'pass':True,"
            "'cleanup_errors':['Task descendants still alive']}))\n"
        )
        self.register_script(
            "environment_check",
            old_code,
            mutates_environment=True,
            result_file="result.json",
            outputs=["--output"],
        )
        old_output = self.project / "environment-check"
        failed = self.a.submit(
            operation="environment_check",
            args=["--output", str(old_output)],
        )
        self.assertFalse(self.a.wait(failed["id"], 8)["result"]["cleanup_ok"])
        self.assertTrue(
            any(x["key"] == "service:adb" for x in self.a.call("resources.state"))
        )

        repair_code = (
            "import json,pathlib,sys\n"
            "out=pathlib.Path(sys.argv[2]);out.mkdir()\n"
            "(out/'result.json').write_text(json.dumps({'pass':True}))\n"
        )
        self.register_script(
            "next_environment_check", repair_code, mutates_environment=True,
            result_file="result.json", outputs=["--output"],
        )
        pending = self.a.submit(operation="next_environment_check",
                                args=["--output", str(self.project / "next-check")])
        self.assertEqual(pending["state"], "queued")
        self.register_script(
            "environment_repair",
            repair_code,
            adb_exclusive=True,
            recovery=True,
            recovery_for=["environment_check"],
            result_file="result.json",
            outputs=["--output"],
            readonly=True,
        )
        repair_output = self.project / "environment-repair"
        repaired = self.a.submit(
            operation="environment_repair",
            args=["--output", str(repair_output)],
        )
        done = self.a.wait(repaired["id"], 8)
        self.assertEqual(done["state"], "succeeded", done.get("result"))
        self.assertEqual(self.a.wait(pending["id"], 8)["state"], "succeeded")
        self.assertEqual(self.a.call("resources.state"), [])

    def test_dead_host_lease_can_recover_without_explicit_release(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_identity = process_identity(child.pid)
        child.wait()
        alive_identity = process_identity(os.getpid())

        def run_recovery(owner):
            job = self.state / "jobs" / "old-lease"
            job.mkdir(parents=True, exist_ok=True)
            atomic_json(
                job / "normalized.json",
                {
                    "operation": "host.lease",
                    "readonly": True,
                    "lease_released": False,
                    "lease_owner": owner,
                    "lease_process": owner,
                    "resources": [{"mode": "read"}],
                },
            )
            grant = self.root / "grant.json"
            atomic_json(
                grant,
                {"state": str(self.state), "spec": {"recovery_targets": ["old-lease"]}},
            )
            output = self.root / ("recovery-" + ("dead" if owner is dead_identity else "alive"))
            environment = {
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "AWB_INTERNAL_GRANT": str(grant),
            }
            return subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "recover_host_lease.py"),
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=8,
            ), json.loads((output / "result.json").read_text())

        dead_process, dead_result = run_recovery(dead_identity)
        self.assertEqual(dead_process.returncode, 0, dead_process.stderr)
        self.assertTrue(dead_result["pass"])
        self.assertEqual(dead_result["reconciled_without_release"], ["old-lease"])

        alive_process, alive_result = run_recovery(alive_identity)
        self.assertEqual(alive_process.returncode, 1)
        self.assertFalse(alive_result["pass"])
        self.assertIn(
            "Original lease owner is still alive", alive_result["cleanup_errors"][0]
        )

    def test_empty_host_lease_recovery_is_idempotent(self):
        environment = {**os.environ, "PYTHONPATH": str(ROOT)}
        for targets in ([], ["old-empty-recovery"]):
            with self.subTest(targets=targets):
                if targets:
                    folder = self.state / "jobs" / targets[0]
                    folder.mkdir(parents=True)
                    atomic_json(folder / "normalized.json", {"operation": "environment.recover_host_lease"})
                    atomic_json(folder / "result.json", {
                        "state": "failed", "cleanup_errors": ["RuntimeError: No affected host lease was identified"],
                    })
                    atomic_json(folder / "host-lease-recovery/result.json", {"pass": False, "checked_jobs": []})
                grant = self.root / "empty-grant.json"
                atomic_json(grant, {"state": str(self.state), "spec": {"recovery_targets": targets}})
                output = self.root / ("empty-recovery" if not targets else "reconcile-empty-recovery")
                run = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/recover_host_lease.py"), "--output", str(output)],
                    cwd=self.root, env={**environment, "AWB_INTERNAL_GRANT": str(grant)},
                    capture_output=True, text=True, timeout=8,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads((output / "result.json").read_text())
                self.assertTrue(result["pass"])
                self.assertEqual(result["cleanup_errors"], [])
                if not targets:
                    self.assertEqual(result["checks"], ["no_affected_leases"])

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

    def test_scene_observation_request_uses_checkpoint_friendly_estimate(self):
        child = self.a.call(
            "scenes.request_observation",
            {
                "device": "one",
                "request_key": "scene-observation-estimate",
                "not_before": time.time() + 60,
            },
        )
        try:
            normalized = json.loads(
                (Path(child["directory"]) / "normalized.json").read_text()
            )
            self.assertLessEqual(normalized["estimate"], 2)
        finally:
            self.a.call("jobs.cancel", {"id": child["id"]})

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
                ROOT / "skills/android-workbench/components/apk-export/scripts" / name,
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

    def test_dead_queued_host_lease_is_cancelled_without_recovery(self):
        from workbench.common import path_resource

        manifest = json.loads((self.project / "workbench.project.json").read_text())
        manifest["mcp_environments"] = ["environment"]
        (self.project / "workbench.project.json").write_text(json.dumps(manifest))
        blocker = self.submit(seconds=1.3, test_resources=[{
            "key": path_resource(self.project / "environment"), "mode": "write",
        }])
        self.wait_state(blocker, {"running"})
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            pending = self.b.submit(
                operation="host.lease", lease_owner=process_identity(owner.pid),
                lease_mode="environment", timeout=5,
            )
            self.assertEqual(pending["state"], "queued")
            owner.terminate()
            owner.wait(timeout=5)
            done = self.b.wait(pending["id"], 5)
            self.assertEqual(done["state"], "cancelled")
            self.assertEqual(done["result"]["reason"], "lease_owner_exited_before_grant")
            self.assertTrue(done["result"]["cleanup_ok"])
            self.assertIsNone(done["started"])
            self.assertEqual(self.a.call("resources.state"), [])
            self.assertEqual(self.a.wait(blocker["id"], 5)["state"], "succeeded")
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=5)

    def test_lease_release_updates_normalized_snapshot(self):
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
        self.a.call("leases.release", {"id": j["id"]})
        normalized = json.loads((Path(j["directory"]) / "normalized.json").read_text())
        self.assertTrue(normalized["lease_released"])

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
            # A service-wide maintenance drain must reach idle proxies even
            # when no queued environment writer is asking them to leave.
            self.a.call("service.drain")
            for process in processes:
                self.assertEqual(process.wait(timeout=6), 0)
            self.a.call("service.resume")
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
                    "confirms_cleanup": True,
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

    def test_registered_cleanup_confirmation_does_not_block_failed_adapter(self):
        script = self.project / "adapter.py"
        script.write_text(
            "import sys,json\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--record')+1]).write_text(json.dumps({'pass':False,'cleanup_errors':[],'cleanup_status':'passed'}))\nraise SystemExit(1)\n"
        )
        manifest = {
            "version": 1,
            "python": sys.executable,
            "operations": {
                "project.cleanup_adapter": {
                    "confirms_cleanup": True,
                    "script": "adapter.py",
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
                operation="project.cleanup_adapter",
                device="one",
                args=[
                    "--serial",
                    "fake-one",
                    "--record",
                    str(self.root / "adapter.json"),
                ],
            )["id"],
            8,
        )
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["result"]["cleanup_ok"])
        self.assertEqual(self.a.call("resources.state"), [])

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


class DeviceInfoTest(unittest.TestCase):
    def test_collects_bounded_reproducibility_facts(self):
        from types import SimpleNamespace
        from workbench.worker import Device

        def adb(*args, **kwargs):
            if "getprop" in args:
                return "\n".join(
                    [
                        "[ro.product.model]: [Test Model]",
                        "[ro.product.device]: [test-device]",
                        "[ro.build.id]: [TEST_BUILD]",
                        "[ro.build.version.release]: [15]",
                        "[ro.build.version.sdk]: [35]",
                        "[ro.build.version.security_patch]: [2026-09-05]",
                        "[ro.product.cpu.abilist]: [arm64-v8a]",
                        "[ignored]: [value]",
                    ]
                )
            if args == ("shell", "wm", "size"):
                return "Physical size: 1080x2400\nOverride size: 1080x2340"
            if args == ("shell", "cat", "/proc/meminfo"):
                return "MemTotal:       8192000 kB\nMemFree:         1 kB\n"
            if args == ("shell", "df", "-k", "/data"):
                return (
                    "Filesystem 1K-blocks Used Available Use% Mounted on\n"
                    "/data 262144000 131072000 131072000 50% /data\n"
                )
            if args == ("shell", "cat", "/proc/sys/kernel/random/boot_id"):
                return "boot-id"
            if args == (
                "shell",
                "settings",
                "get",
                "global",
                "airplane_mode_on",
            ):
                return "0"
            if args == ("shell", "dumpsys", "connectivity"):
                return "Active default network: 100\nActive network type: WIFI\n"
            if args == ("shell", "cmd", "wifi", "status"):
                return (
                    "Wi-Fi is enabled\n"
                    "SSID: test-network, BSSID: redacted\n"
                    "RSSI: -52\n"
                )
            if args == ("shell", "pm", "list", "packages", "com.android.vending"):
                return "package:com.android.vending\n"
            if args == ("shell", "dumpsys", "account"):
                return "Account {name=redacted, type=com.google}\n"
            raise AssertionError("unexpected adb call: " + repr(args))

        device = Device(SimpleNamespace())
        device.adb = adb
        result = device.info()
        self.assertEqual(result["boot_id"], "boot-id")
        self.assertEqual(result["properties"]["ro.product.model"], "Test Model")
        self.assertEqual(result["screen"]["size"], "1080x2340")
        self.assertEqual(result["memory"]["total_gib"], 7.81)
        self.assertEqual(result["storage"]["filesystem"], "/data")
        self.assertEqual(result["storage"]["available_gib"], 125.0)
        self.assertEqual(result["storage"]["mount"], "/data")
        self.assertEqual(result["system"]["android_version"], "15")
        self.assertEqual(result["system"]["sdk_int"], 35)
        self.assertEqual(result["system"]["security_patch"], "2026-09-05")
        self.assertFalse(result["network"]["airplane_mode"])
        self.assertEqual(result["network"]["default_network_id"], 100)
        self.assertEqual(result["network"]["type"], "WIFI")
        self.assertFalse(result["network"]["internet_probe_performed"])
        self.assertTrue(result["wifi"]["enabled"])
        self.assertTrue(result["wifi"]["connected"])
        self.assertEqual(result["wifi"]["ssid"], "test-network")
        self.assertEqual(result["wifi"]["signal_dbm"], -52)
        self.assertTrue(result["google_play"]["installed"])
        self.assertTrue(result["google_play"]["google_account_present"])
        self.assertTrue(result["google_play"]["logged_in"])


if __name__ == "__main__":
    unittest.main()
