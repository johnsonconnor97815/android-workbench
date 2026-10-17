"""Runner integration with fake model output and real Linux process lifecycles.

No model, host-agent tool or Android/device calls are made. Native host
acknowledgments below are explicit protocol fixtures, not runtime evidence.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.app_replica.backend import HostSlots
from workbench.app_replica.engine import execute
from workbench.app_replica.runner import Runner, dispatch_lock, native_next, seed_app
from workbench.app_replica.store import ReplicaError


# This is a deterministic fake Codex executable, not a model implementation.
# Its subprocesses, writes, API submissions, exits and resume arguments are real.
FAKE_RUNNER_CODEX = r'''
import hashlib,json,os,pathlib,re,subprocess,sys,time,uuid
sys.path.insert(0,WORKBENCH_ROOT)
from workbench.app_replica.engine import execute
prompt=sys.stdin.read()
task_file=pathlib.Path(re.search(r'^Assigned task: (.+)$',prompt,re.M).group(1))
workspace=pathlib.Path(re.search(r'^Workflow workspace: (.+)$',prompt,re.M).group(1))
run_id=re.search(r'^Current run ID: (.+)$',prompt,re.M).group(1)
task=json.loads(task_file.read_text())
try: behavior=json.loads(task['description'])
except ValueError: behavior={}
args=sys.argv[1:]
resumed=args[:2]==['exec','resume']
thread=args[-2] if resumed else str(uuid.uuid5(uuid.NAMESPACE_URL,task['id']))
print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True)
print(json.dumps({'type':'fixture.launch','args':args,'resumed':resumed,
                  'run_id':run_id,'pid':os.getpid()}),flush=True)
deadline=time.monotonic()+8
while True:
 snapshot=execute(workspace,{'op':'status'})
 if snapshot['runs'][run_id]['state']=='running': break
 if time.monotonic()>deadline: raise RuntimeError('fixture did not observe run.started')
 time.sleep(0.01)
def call(op,**fields):
 return execute(workspace,{'op':op,'request_id':'fixture-'+run_id+'-'+op,**fields})
if behavior.get('wait_once') and not resumed:
 call('task.wait',task_id=task['id'],run_id=run_id,
      requires=[{'message_id':'dependency-ready'}],checkpoint={'fixture':'waiting for reply'})
 print(json.dumps({'type':'fixture.wait_recorded'}),flush=True)
 if behavior.get('hold_wait'):
  release=workspace/'artifacts'/'release-wait'
  while not release.exists():
   if time.monotonic()>deadline: raise RuntimeError('fixture wait release missing')
   time.sleep(0.01)
else:
 artifacts=[]
 for output in task['outputs']:
  path=pathlib.Path(output)
  path.parent.mkdir(parents=True,exist_ok=True)
  path.write_text('fake Codex delivery for '+task['id'])
  artifacts.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
 call('task.submit',task_id=task['id'],run_id=run_id,artifacts=artifacts,
      self_checks=[{'name':'explicit fake Codex fixture','result':'pass','evidence':['host fixture only']}],
      unresolved=[],class_revisions={},dependency_revisions={})
 print(json.dumps({'type':'fixture.delivery_recorded'}),flush=True)
 if behavior.get('detached_writer'):
  ready=workspace/'artifacts'/'writer-ready'
  release=workspace/'artifacts'/'release-writer'
  done=workspace/'artifacts'/'writer-done'
  code="import pathlib,sys,time;ready,release,done=map(pathlib.Path,sys.argv[1:]);ready.write_text(str(__import__('os').getpid()));deadline=time.monotonic()+8\nwhile not release.exists():\n if time.monotonic()>deadline: raise RuntimeError('fixture writer release missing')\n time.sleep(0.01)\ndone.write_text('actual detached fixture write')"
  subprocess.Popen([sys.executable,'-c',code,str(ready),str(release),str(done)],
    env={},start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
last=args[args.index('--output-last-message')+1]
pathlib.Path(last).write_text('fake fixture completed')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux subprocess lifecycle integration")
class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-replica-runner-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workflow with spaces"
        self.workspace.mkdir()
        (self.workspace / "artifacts").mkdir()
        self.fake = self.root / "fake codex"
        self.fake.write_text(f"#!{sys.executable}\nWORKBENCH_ROOT={str(ROOT)!r}\n" + FAKE_RUNNER_CODEX)
        self.fake.chmod(0o700)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "fake-codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.counter = 0
        self.call("init", project={"id": "runner-fixture", "workbench_source": str(ROOT),
                                   "execution_root": str(self.root)}, capacity=1)
        self.slots_path = self.root / "host-slots.json"
        self.runner = Runner(self.workspace, host_capacity=1, slots_path=self.slots_path, executable=self.fake)
        self.addCleanup(self.cleanup_processes)

    def call(self, op, **fields):
        self.counter += 1
        if op not in {"status", "recover", "coverage"}:
            fields.setdefault("request_id", f"fixture-request-{self.counter}")
        return execute(self.workspace, {"op": op, **fields})

    def add_task(self, identifier="worker", **behavior):
        return self.call("task.add", task={"id": identifier, "role": "analysis", "mode": "inspect",
                                          "description": json.dumps(behavior),
                                          "outputs": [f"artifacts/{identifier}/result.txt"]})["task"]

    def wait_for(self, condition, message="runner fixture did not reach expected state"):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.02)
        snapshot = self.call("status")
        logs = {}
        for run in snapshot["runs"].values():
            path = self.workspace / "coordination/backend" / run["id"] / "stderr.log"
            if path.exists():
                logs[run["id"]] = path.read_text()
        self.fail(f"{message}; task states: {[(key, value['state']) for key, value in snapshot['tasks'].items()]}; stderr: {logs}")

    def drive_until_idle(self):
        return self.wait_for(lambda: (value if (value := self.runner.step())["state"] == "idle" else None))

    def pump_until(self, condition):
        def advance():
            self.runner.step()
            return condition()
        return self.wait_for(advance)

    def cleanup_processes(self):
        snapshot = self.call("status")
        for run in snapshot["runs"].values():
            if run.get("backend") == "codex":
                try:
                    self.runner.backend.stop(run["id"], force=True)
                    self.wait_for(lambda: self.runner.backend.poll(run["id"])["confirmed_stopped"])
                except (OSError, ValueError, AssertionError):
                    pass

    def launch_events(self, run):
        path = Path(run["backend_handle"]["paths"]["events"])
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_delivery_is_not_free_capacity_until_detached_writer_really_exits(self):
        self.add_task(detached_writer=True)
        self.runner.step()
        ready = self.workspace / "artifacts/writer-ready"
        self.pump_until(ready.exists)
        snapshot = self.call("status")
        task = snapshot["tasks"]["worker"]
        run_id = task["run_id"]
        self.assertEqual(task["state"], "delivered")
        self.assertEqual(snapshot["runs"][run_id]["state"], "delivery_pending_stop")
        self.assertEqual(HostSlots(self.slots_path, 1).get(run_id)["state"], "occupied")
        self.add_task("second")
        observation = self.runner.step()
        self.assertEqual(observation["state"], "running")
        self.assertEqual(len(self.call("status")["runs"]), 1)
        (self.workspace / "artifacts/release-writer").touch()
        self.drive_until_idle()
        snapshot = self.call("status")
        self.assertEqual((self.workspace / "artifacts/writer-done").read_text(), "actual detached fixture write")
        self.assertEqual(len(snapshot["runs"]), 2)
        self.assertTrue(all(run["state"] == "stopped" for run in snapshot["runs"].values()))
        self.assertTrue(all(task["deliveries"][-1]["accepted"] for task in snapshot["tasks"].values()))
        self.assertTrue(all(slot["state"] == "released" for slot in HostSlots(self.slots_path, 1).get()["reservations"].values()))

    def test_wait_keeps_slot_until_stop_then_reply_resumes_exact_thread(self):
        self.add_task(wait_once=True, hold_wait=True)
        self.runner.step()
        self.pump_until(lambda: self.call("status")["tasks"]["worker"]["state"] == "waiting")
        snapshot = self.call("status")
        first_run_id = snapshot["tasks"]["worker"]["run_id"]
        self.assertEqual(snapshot["runs"][first_run_id]["state"], "waiting_pending_stop")
        self.assertEqual(HostSlots(self.slots_path, 1).get(first_run_id)["state"], "occupied")
        with self.assertRaisesRegex(ReplicaError, "still live"):
            self.call("task.resume", task_id="worker", reason="fixture premature resume", evidence=["fixture"])
        (self.workspace / "artifacts/release-wait").touch()
        self.drive_until_idle()
        waiting = self.call("status")
        self.assertIsNone(waiting["tasks"]["worker"]["run_id"])
        self.assertEqual(waiting["tasks"]["worker"]["state"], "waiting")
        self.assertEqual(waiting["runs"][first_run_id]["outcome"], "waiting")
        self.runner = Runner(self.workspace, host_capacity=1, slots_path=self.slots_path, executable=self.fake)
        self.call("message.send", message_id="dependency-ready", type="answer", sender="fixture-analysis",
                  recipient="worker", task_id="worker", payload={"fixture": "ready"}, evidence=["fixture reply"])
        self.drive_until_idle()
        final = self.call("status")
        self.assertEqual(len(final["tasks"]["worker"]["runs"]), 2)
        first, second = [final["runs"][identifier] for identifier in final["tasks"]["worker"]["runs"]]
        exact_thread = first["backend_handle"]["thread_id"]
        self.assertEqual(second["backend_handle"]["thread_id"], exact_thread)
        launch = next(event for event in self.launch_events(second) if event["type"] == "fixture.launch")
        self.assertTrue(launch["resumed"])
        self.assertEqual(launch["args"][-2:], [exact_thread, "-"])
        self.assertEqual(second["outcome"], "delivered")
        self.assertTrue(final["tasks"]["worker"]["deliveries"][-1]["accepted"])

    def test_run_generator_drives_real_process_to_idle(self):
        self.add_task()
        observations = list(self.runner.run(interval=0.01))
        self.assertEqual(observations[-1]["state"], "idle")
        snapshot = self.call("status")
        self.assertEqual(snapshot["tasks"]["worker"]["state"], "delivered")
        self.assertTrue(snapshot["tasks"]["worker"]["deliveries"][-1]["accepted"])
        run = next(iter(snapshot["runs"].values()))
        self.assertEqual(run["state"], "stopped")
        self.assertTrue(run["stop_observation"]["writers_stopped"])

    def test_host_capacity_wait_continues_until_other_workflow_releases(self):
        external = self.root / "external-workflow"
        (external / "artifacts").mkdir(parents=True)
        execute(external, {"op": "init", "request_id": "external-init", "capacity": 1,
                           "project": {"id": "external-fixture", "workbench_source": str(ROOT),
                                       "execution_root": str(self.root)}})
        execute(external, {"op": "task.add", "request_id": "external-task", "task": {
            "id": "external-worker", "role": "analysis", "mode": "inspect",
            "description": json.dumps({"wait_once": True, "hold_wait": True}),
            "outputs": ["artifacts/external.txt"]}})
        external_runner = Runner(external, host_capacity=1, slots_path=self.slots_path, executable=self.fake)
        def cleanup_external():
            for run in execute(external, {"op": "status"})["runs"].values():
                external_runner.backend.stop(run["id"], force=True)
                self.wait_for(lambda: external_runner.backend.poll(run["id"])["confirmed_stopped"])
        self.addCleanup(cleanup_external)
        def start_external():
            external_runner.step()
            return execute(external, {"op": "status"})["tasks"]["external-worker"]["state"] == "waiting"
        self.wait_for(start_external)
        self.add_task()
        polling = self.runner.run(interval=0.01)
        first = next(polling)
        self.assertEqual(first["state"], "waiting_capacity")
        self.assertEqual(len(self.call("status")["runs"]), 1)
        (external / "artifacts/release-wait").touch()
        self.wait_for(lambda: external_runner.step()["state"] == "idle")
        remaining = list(polling)
        self.assertEqual(remaining[-1]["state"], "idle")
        snapshot = self.call("status")
        self.assertTrue(snapshot["tasks"]["worker"]["deliveries"][-1]["accepted"])
        self.assertEqual(len(snapshot["runs"]), 1)

    def test_confirmed_launch_failure_becomes_failed_without_fabricated_start(self):
        self.add_task()
        self.runner = Runner(self.workspace, host_capacity=1, slots_path=self.slots_path,
                             executable=self.root / "missing-fake-codex")
        self.drive_until_idle()
        snapshot = self.call("status")
        run = next(iter(snapshot["runs"].values()))
        self.assertEqual(run["outcome"], "launch_failed")
        self.assertNotIn("started_at", run)
        self.assertEqual(snapshot["tasks"]["worker"]["state"], "failed")
        self.assertTrue(run["stop_observation"]["evidence"]["confirmed_launch_failed"])
        self.assertEqual(HostSlots(self.slots_path, 1).get(run["id"])["state"], "released")

    def test_seed_app_is_idempotent_and_dispatch_lock_prevents_competing_runner(self):
        first, repeated = seed_app(self.workspace), seed_app(self.workspace)
        self.assertEqual(first, repeated)
        self.assertEqual(list(self.call("status")["tasks"]), ["app-owner"])
        with dispatch_lock(self.workspace):
            with self.assertRaisesRegex(ReplicaError, "Another dispatcher"):
                self.runner.step()

    def test_native_directives_require_host_execution_and_recovery_reuses_reservation(self):
        self.add_task(wait_once=True)
        full = native_next(self.workspace, external_active=1)
        self.assertIsNone(full["task"])
        initial = native_next(self.workspace, external_active=0)
        recovered = native_next(self.workspace, external_active=0)
        self.assertEqual(initial["run_id"], recovered["run_id"])
        self.assertFalse(initial["recovery"])
        self.assertTrue(recovered["recovery"])
        self.assertEqual(initial["directive"]["tool"], "spawn_agent")
        self.assertEqual(initial["directive"]["execution"], "host_action_required")
        self.assertEqual(self.call("status")["runs"][initial["run_id"]]["state"], "reserved")
        # Explicit synthetic host acknowledgments exercise the protocol only.
        handle = {"target": "fixture-host-handle", "requested": {"model": "gpt-6-astra", "effort": "xhigh"}}
        self.call("run.started", run_id=initial["run_id"], backend_handle=handle)
        self.call("task.wait", task_id="worker", run_id=initial["run_id"],
                  requires=[{"message_id": "dependency-ready"}], checkpoint={"fixture": "native wait"})
        self.call("run.stopped", run_id=initial["run_id"], outcome="waiting",
                  observation={"confirmed": True, "writers_stopped": True, "evidence": ["synthetic host fixture only"]})
        self.call("message.send", message_id="dependency-ready", type="answer", sender="fixture", recipient="worker",
                  task_id="worker", payload={"fixture": "ready"}, evidence=["synthetic fixture"])
        continued = native_next(self.workspace, external_active=0)
        self.assertEqual(continued["directive"]["tool"], "followup_task")
        self.assertEqual(continued["directive"]["arguments"]["target"], "fixture-host-handle")
        self.assertNotEqual(continued["run_id"], initial["run_id"])


if __name__ == "__main__":
    unittest.main()
