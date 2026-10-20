"""Lease cleanup distinguishes transport errors, dead processes and unknown trees."""

import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.common import (SchedulerUnavailable, WorkbenchError, atomic_json,
                              process_identity, process_status, process_group_status, rpc)
from workbench.leases import execute_lease, retry_lease_call, verify_lease_tree
from workbench.worker import Runner


class LeaseEvidenceTest(unittest.TestCase):
    def test_actual_socket_timeout_is_a_retryable_transport_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(Path(folder) / "service.sock"))
                server.listen()

                def delayed_reply():
                    with server.accept()[0] as connection:
                        connection.recv(4096)
                        time.sleep(.1)

                thread = threading.Thread(target=delayed_reply)
                thread.start()
                try:
                    with self.assertRaisesRegex(SchedulerUnavailable, "timed out"):
                        rpc(folder, "worker.lease_state", timeout=.02)
                finally:
                    thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_identity_validation_unknown_reads_and_pid_reuse(self):
        identity = process_identity(os.getpid())
        self.assertEqual(process_status(identity), "alive")
        for invalid in (None, {}, {**identity, "pid": True}, {**identity, "start": "invalid"}, {**identity, "boot": "invalid"}):
            self.assertEqual(process_status(invalid), "unknown")
        self.assertEqual(process_status({**identity, "start": str(int(identity["start"]) + 1)}), "dead")
        original = Path.read_text

        def unreadable(path, *args, **kwargs):
            if str(path) == f"/proc/{identity['pid']}/stat":
                raise PermissionError("fixture unreadable process")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", unreadable):
            self.assertEqual(process_status(identity), "unknown")

    def test_dead_leader_does_not_hide_live_group_member(self):
        with tempfile.TemporaryDirectory() as folder:
            child_file = Path(folder) / "child.json"
            code = "import subprocess,sys,json\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\nopen(sys.argv[1],'w').write(json.dumps({'pid':p.pid}))\n"
            leader = subprocess.Popen([sys.executable, "-c", code, str(child_file)], start_new_session=True)
            identity = process_identity(leader.pid)
            try:
                leader.wait(timeout=5)
                self.assertEqual(process_status(identity), "dead")
                self.assertEqual(process_group_status(identity), "alive")
                original = Path.read_text

                # A failed /proc scan is not an empty group.
                child_pid = json.loads(child_file.read_text())["pid"]

                def unreadable(path, *args, **kwargs):
                    if str(path) == f"/proc/{child_pid}/stat":
                        raise PermissionError("fixture unreadable group member")
                    return original(path, *args, **kwargs)

                with patch.object(Path, "read_text", unreadable):
                    self.assertEqual(process_group_status(identity), "unknown")
            finally:
                os.killpg(leader.pid, 9)
                if leader.poll() is None:
                    leader.wait(timeout=5)

    def test_only_transport_errors_are_retried(self):
        calls = []

        def transient():
            calls.append(1)
            if len(calls) < 3:
                raise SchedulerUnavailable("fixture timeout")
            return "received"

        with patch("workbench.leases.time.sleep"):
            self.assertEqual(retry_lease_call(transient), "received")
        self.assertEqual(len(calls), 3)
        with patch("workbench.leases.time.sleep") as sleep:
            with self.assertRaisesRegex(WorkbenchError, "rejected"):
                retry_lease_call(lambda: (_ for _ in ()).throw(WorkbenchError("rejected")))
            sleep.assert_not_called()

    def test_close_checkpoint_cannot_override_unknown_owner_or_live_group(self):
        identity = process_identity(os.getpid())
        spec = {"lease_owner": identity, "lease_tree_closed": True}
        with patch("workbench.leases.process_status", return_value="unknown"):
            with self.assertRaisesRegex(WorkbenchError, "unknown"):
                verify_lease_tree(spec)
        spec["lease_process"] = identity
        with patch("workbench.leases.process_group_status", return_value="alive"):
            with self.assertRaisesRegex(WorkbenchError, "tree is alive or unknown"):
                verify_lease_tree(spec)

    def test_worker_lease_poll_retries_transport_and_released_snapshot_clears_uncertainty(self):
        with tempfile.TemporaryDirectory() as folder:
            spec = {"operation": "host.lease", "directory": folder, "lease_owner": process_identity(os.getpid())}
            runner = Runner({"spec": spec, "state": folder, "job": "fixture", "token": "fixture"})
            with patch("workbench.worker.rpc", side_effect=[SchedulerUnavailable("fixture timeout"), {"released": True}]) as rpc:
                with patch("workbench.leases.time.sleep"):
                    self.assertEqual(runner.call("lease_state"), {"released": True})
                self.assertEqual(rpc.call_count, 2)
            runner.authorize = lambda: None
            runner.call = lambda method: {"released": True, "mcp_expected": True}
            self.assertEqual(execute_lease(runner), "partial")
            self.assertFalse(runner.uncertain)
            atomic_json(Path(folder) / "normalized.json", {"lease_released": True, "lease_tree_closed": True})
            runner.uncertain = True
            runner.call = lambda method: (_ for _ in ()).throw(SchedulerUnavailable("fixture persistent timeout"))
            with self.assertRaises(SchedulerUnavailable):
                execute_lease(runner)
            self.assertFalse(runner.uncertain)
            atomic_json(Path(folder) / "normalized.json", {"lease_released": False})
            with self.assertRaises(SchedulerUnavailable):
                execute_lease(runner)
            self.assertTrue(runner.uncertain)
            runner.call = lambda method: {"released": False}
            with patch("workbench.leases.process_status", return_value="unknown"):
                with self.assertRaisesRegex(WorkbenchError, "owner state is unknown"):
                    execute_lease(runner)


if __name__ == "__main__":
    unittest.main()
