"""Fake Codex protocol fixtures plus actual Linux subprocess/tree lifecycle tests.

These tests make no model calls and establish no Android/device behavior.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.app_replica.backend import (
    BackendError, CapacityError, CodexBackend, HostSlots, build_launch_descriptor,
    process_identity, _same_process, _write,
)


THREAD = "12345678-1234-4234-8234-123456789abc"

# This executable is deliberately fake. Only its process lifecycle is real.
FAKE_CODEX = r'''
import datetime,json,os,pathlib,signal,subprocess,sys,time
args=sys.argv[1:]
data=json.loads(sys.stdin.read())
thread=args[-2] if args[:2]==['exec','resume'] else '12345678-1234-4234-8234-123456789abc'
print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True)
print(json.dumps({'type':'fixture.arguments','args':args,'cwd':os.getcwd(),'pid':os.getpid(),
                  'inherited':os.environ.get('AWB_TEST_INHERITED')}),flush=True)
print('fixture stderr evidence',file=sys.stderr,flush=True)
if data.get('metadata'):
 root=pathlib.Path(os.environ['CODEX_HOME'])/'sessions'/'fixture'
 root.mkdir(parents=True,exist_ok=True)
 stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
 with (root/('rollout-fixture-'+thread+'.jsonl')).open('a') as f:
  f.write(json.dumps({'type':'session_meta','payload':{'id':thread}})+'\n')
  f.write(json.dumps({'type':'turn_context','timestamp':stamp,
    'payload':{'model':data.get('reported_model','gpt-6.1-sol'),'effort':'high'}})+'\n')
if data.get('detached'):
 code="import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()));time.sleep(0.7);pathlib.Path(sys.argv[2]).write_text('descendant completed')"
 subprocess.Popen([sys.executable,'-c',code,data['ready'],data['marker']],
                  env={},start_new_session=True,stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if data.get('hold'):
 if data.get('ignore_term'): signal.signal(signal.SIGTERM,signal.SIG_IGN)
 pathlib.Path(data['ready']).write_text(str(os.getpid()))
 while True: time.sleep(0.1)
if data.get('malformed'): print('deliberately malformed fixture event',flush=True)
last=args[args.index('--output-last-message')+1]
pathlib.Path(last).write_text('fake model reply')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}),flush=True)
sys.exit(data.get('exit_code',0))
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process supervision")
class BackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        self.fake = self.root / "fake codex"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
        self.fake.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "fake-codex-home"),
            "AWB_TEST_INHERITED": "preserved",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.slots = HostSlots(self.root / "host-slots.json", 1)
        self.backend = CodexBackend(self.workspace, self.root / "runs", self.slots, self.fake)
        self.runs = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for run_id in self.runs:
            try:
                self.backend.stop(run_id, force=True)
                self.wait_stopped(run_id)
            except (BackendError, OSError, AssertionError):
                pass

    def start(self, run_id="run", data=None, **kwargs):
        self.runs.append(run_id)
        return self.backend.start(run_id, json.dumps(data or {}), "gpt-6.1-sol", "high", **kwargs)

    def wait_for(self, condition, message="fixture did not become ready"):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.02)
        self.fail(message)

    def wait_stopped(self, run_id):
        return self.wait_for(lambda: (value if (value := self.backend.poll(run_id))["confirmed_stopped"] else None),
                             f"fixture {run_id} did not stop")

    def events(self, observation):
        return [json.loads(line) for line in Path(observation["paths"]["events"]).read_text().splitlines()
                if line.startswith("{")]

    def test_tool_enabled_command_exact_thread_resume_and_persistent_evidence(self):
        self.start(data={"malformed": True})
        observed = self.wait_stopped("run")
        self.assertEqual(observed["exit_code"], 0)
        self.assertTrue(observed["worker_started"])
        self.assertEqual(observed["thread_id"], THREAD)
        self.assertTrue(observed["turn_completed"])
        self.assertEqual(observed["malformed_event_lines"], 1)
        self.assertEqual(observed["reported"], {"model": None, "effort": None})
        fixture = self.events(observed)[1]
        self.assertEqual(fixture["cwd"], str(self.workspace))
        self.assertEqual(fixture["inherited"], "preserved")
        command = fixture["args"]
        for forbidden in ("--output-schema", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                          "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"):
            self.assertNotIn(forbidden, command)
        self.assertEqual(command[command.index("--disable") + 1], "multi_agent")
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(Path(observed["paths"]["stderr"]).read_text(), "fixture stderr evidence\n")
        self.assertEqual(Path(observed["paths"]["last_message"]).read_text(), "fake model reply")
        self.assertTrue(observed["completion"]["subreaper"])
        self.assertEqual(self.slots.get("run")["state"], "released")
        self.runs.append("continued")
        self.backend.resume("continued", THREAD, "{}", "gpt-6-astra", "xhigh")
        continued = self.wait_stopped("continued")
        args = self.events(continued)[1]["args"]
        self.assertEqual(args[:2], ["exec", "resume"])
        self.assertEqual(args[-2:], [THREAD, "-"])
        self.assertNotIn("-C", args)
        self.assertIn("--skip-git-repo-check", args)
        self.assertEqual(continued["requested"], {"model": "gpt-6-astra", "effort": "xhigh"})

    def test_idempotent_start_never_relaunches_and_rejects_different_request(self):
        self.start()
        first = self.wait_stopped("run")
        repeated = self.backend.start("run", "{}", "gpt-6.1-sol", "high")
        self.assertEqual(repeated["process"], first["process"])
        self.assertEqual(len(self.events(repeated)), 3)
        with self.assertRaises(BackendError):
            self.backend.start("run", "different", "gpt-6.1-sol", "high")

    def test_detached_environment_cleared_writer_keeps_slot_until_reaped(self):
        ready, marker = self.root / "descendant.ready", self.root / "descendant.done"
        self.start(data={"detached": True, "ready": str(ready), "marker": str(marker)})
        self.wait_for(ready.exists)
        value = self.backend.poll("run")
        self.assertFalse(value["confirmed_stopped"])
        self.assertEqual(self.slots.get("run")["state"], "occupied")
        with self.assertRaises(CapacityError):
            HostSlots(self.slots.path, 1).reserve("other-workflow", "/different")
        self.backend.stop("run")
        stopped = self.wait_stopped("run")
        self.assertFalse(marker.exists())
        self.assertGreaterEqual(len(stopped["completion"]["reaped"]), 2)

    def test_natural_descendant_completion_releases_after_actual_write(self):
        ready, marker = self.root / "ready", self.root / "done"
        self.start(data={"detached": True, "ready": str(ready), "marker": str(marker)})
        self.wait_for(ready.exists)
        self.assertFalse(self.backend.poll("run")["confirmed_stopped"])
        self.wait_stopped("run")
        self.assertEqual(marker.read_text(), "descendant completed")

    def test_force_stop_reaps_writer_ignoring_graceful_stop(self):
        ready = self.root / "ready"
        self.start(data={"hold": True, "ignore_term": True, "ready": str(ready)})
        self.wait_for(ready.exists)
        self.backend.stop("run")
        time.sleep(0.12)
        self.assertFalse(self.backend.poll("run")["confirmed_stopped"])
        self.assertEqual(self.slots.get("run")["state"], "occupied")
        self.backend.stop("run", force=True)
        value = self.wait_stopped("run")
        self.assertEqual(value["exit_code"], -signal.SIGKILL)

    def test_fresh_backend_reconciles_durable_process_and_thread(self):
        ready = self.root / "ready"
        self.start(data={"hold": True, "ready": str(ready)})
        self.wait_for(ready.exists)
        recovered = CodexBackend(self.workspace, self.root / "runs", HostSlots(self.slots.path, 1), self.fake)
        observation = recovered.reconcile("run")
        self.assertEqual(observation["state"], "running")
        self.assertEqual(observation["thread_id"], THREAD)
        recovered.stop("run")
        self.wait_stopped("run")

    def test_missing_executable_is_confirmed_launch_failure(self):
        backend = CodexBackend(self.workspace, self.root / "runs", self.slots, self.root / "does-not-exist")
        self.runs.append("missing")
        backend.start("missing", "{}", "gpt-6.1-sol", "high")
        result = self.wait_stopped("missing")
        backend.poll("missing")  # Reap the supervisor owned by this backend instance.
        self.assertTrue(result["confirmed_launch_failed"])
        self.assertFalse(result["worker_started"])
        self.assertIn("No such file", result["launch_error"])
        self.assertEqual(self.slots.get("missing")["state"], "released")

    def test_reported_selection_requires_current_backend_turn_context(self):
        self.start(data={"metadata": True, "reported_model": "gpt-6-astra"})
        value = self.wait_stopped("run")
        self.assertEqual(value["requested"]["model"], "gpt-6.1-sol")
        self.assertEqual(value["reported"], {"model": "gpt-6-astra", "effort": "high"})
        self.assertIsNotNone(value["selection_error"])
        evidence = value["reported_evidence"]
        self.assertEqual(evidence["line"], 2)
        self.assertTrue(Path(evidence["path"]).is_file())

    def test_uncertain_launch_never_expires_or_releases_on_claimed_flag(self):
        directory = self.root / "uncertain"
        directory.mkdir()
        record = directory / "run.json"
        _write(record, {"state": "launch_intent", "paths": {"completion": str(directory / "none")}})
        self.slots.reserve("uncertain", self.workspace, {"record_path": str(record), "launch_token": "token"})
        with self.assertRaises(BackendError):
            self.slots.release("uncertain", {"confirmed_stopped": True, "timeout": True})
        self.assertEqual(self.slots.get("uncertain")["state"], "occupied")
        with self.assertRaises(CapacityError):
            self.slots.reserve("replacement", self.workspace)

    def test_pid_reuse_is_not_identity(self):
        observed = process_identity(os.getpid())
        self.assertTrue(_same_process(observed, process_identity(os.getpid())))
        reused = {**observed, "start_ticks": observed["start_ticks"] + 1}
        self.assertFalse(_same_process(reused, observed))

    def test_slots_are_locked_across_workflows_and_capacity_is_not_silently_expanded(self):
        shared = self.root / "concurrent-host.json"
        def claim(number):
            try:
                return HostSlots(shared, 2).reserve(str(number), f"/workflow/{number}")["run_id"]
            except CapacityError:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(claim, range(8)))
        self.assertEqual(sum(value is not None for value in results), 2)
        self.assertEqual(len(HostSlots(shared, 2).get()["reservations"]), 2)
        with self.assertRaises(CapacityError):
            HostSlots(shared, 3).get()

    def test_separate_host_processes_share_the_same_capacity(self):
        shared = self.root / "multiprocess-host.json"
        code = ("import sys; from workbench.app_replica.backend import HostSlots, CapacityError\n"
                "try: HostSlots(sys.argv[1],2).reserve(sys.argv[2],'/workflow/'+sys.argv[2]); print('reserved')\n"
                "except CapacityError: print('occupied')\n")
        children = [subprocess.Popen([sys.executable, "-c", code, str(shared), str(number)],
                                     cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for number in range(6)]
        results = [child.communicate(timeout=8) for child in children]
        self.assertTrue(all(child.returncode == 0 for child in children), results)
        self.assertEqual(sum(output.strip() == "reserved" for output, error in results), 2)
        self.assertEqual(len(HostSlots(shared, 2).get()["reservations"]), 2)

    def test_interruption_between_launch_and_ack_retains_slot_and_recovers(self):
        ready = self.root / "ready"
        from workbench.app_replica import backend as module
        original = module._write
        def fail_ack(path, value):
            if Path(path).name == "run.json" and value.get("state") == "launched":
                raise OSError("fixture lost launch acknowledgment")
            return original(path, value)
        self.runs.append("lost-ack")
        with mock.patch.object(module, "_write", side_effect=fail_ack):
            with self.assertRaisesRegex(OSError, "lost launch acknowledgment"):
                self.backend.start("lost-ack", json.dumps({"hold": True, "ready": str(ready)}),
                                   "gpt-6.1-sol", "high")
        self.wait_for(ready.exists)
        self.assertEqual(self.slots.get("lost-ack")["state"], "occupied")
        recovered = self.backend.reconcile("lost-ack")
        self.assertEqual(recovered["state"], "running")
        self.assertFalse(recovered["confirmed_launch_failed"])
        self.backend.stop("lost-ack")
        self.wait_stopped("lost-ack")

    def test_native_descriptors_distinguish_start_resume_notification(self):
        task = self.root / "task.json"
        task.write_text("{}")
        spawn = build_launch_descriptor("spawn", task, "gpt-6-astra", "xhigh", task_name="class_structure")
        self.assertEqual(spawn["tool"], "spawn_agent")
        self.assertEqual(spawn["arguments"]["model"], "gpt-6-astra")
        self.assertEqual(spawn["arguments"]["fork_turns"], "none")
        self.assertEqual(spawn["execution"], "host_action_required")
        follow = build_launch_descriptor("followup", task, "gpt-6.1-sol", "high", target="actual-handle")
        notify = build_launch_descriptor("message", task, "gpt-6.1-sol", "high", target="actual-handle")
        self.assertEqual(follow["tool"], "followup_task")
        self.assertEqual(notify["tool"], "send_message")
        self.assertTrue(follow["limitations"])
        with self.assertRaises(BackendError):
            build_launch_descriptor("followup", task, "gpt-6.1-sol", "high")


if __name__ == "__main__":
    unittest.main()
