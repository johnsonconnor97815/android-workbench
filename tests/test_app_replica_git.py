"""Real Git/local bare-remote tests; no App, device, model or external network."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.app_replica import execute, ReplicaError
from workbench.app_replica import git_sync
from workbench.app_replica.store import Store
from workbench.app_replica.cli import main
from workbench.common import rpc, WorkbenchError
from scripts import replica_git_notify


class ReplicaGitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awb-git-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "app"
        self.repo.mkdir()
        self.workspace = self.root / "workflow"
        self.service = self.root / "service"
        self.remote = self.root / "remote.git"
        self.counter = 0
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        self.git("init", "-q", "-b", "dev")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("init", "-q", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.write("A.kt", "base A\n")
        self.write("B.kt", "base B\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.call("init", project={"id": "fixture", "output_path": str(self.repo)}, capacity=3)

    def git(self, *args, repo=None, allowed=(0,)):
        result = subprocess.run(["git", "--literal-pathspecs", "-C", str(repo or self.repo), *args],
                                text=True, capture_output=True, env=self.env)
        self.assertIn(result.returncode, allowed, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def write(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def call(self, op, **fields):
        self.counter += 1
        fields.setdefault("request_id", f"event-{self.counter}")
        return execute(self.workspace, {"op": op, **fields})

    def enable(self, **fields):
        return git_sync.enable(self.workspace, repo=self.repo, service_state=self.service, **fields)

    def writer(self, identifier="writer", paths=("A.kt",)):
        task = self.call("task.add", task={"id": identifier, "role": "class", "mode": "code",
                         "description": "Assemble fixture source", "outputs": [str(self.repo / p) for p in paths]})["task"]
        run = self.call("task.claim", task_id=task["id"], backend="native")["run"]
        self.call("run.started", run_id=run["id"], backend_handle={"fixture": True})
        return task["id"], run["id"]

    def save(self, writer, **fields):
        return self.call("git.checkpoint", task_id=writer[0], run_id=writer[1], **fields)["git"]

    def test_resume_preserves_history_and_waits_for_a_writer_checkpoint(self):
        self.enable()
        writer = self.writer()
        before = self.git('rev-parse', 'HEAD')
        identity = json.loads((self.workspace / git_sync.CONFIG).read_text())['id']
        git_sync.disable(self.workspace)
        self.assertEqual('disabled', git_sync.status(self.workspace)['state'])
        self.write('A.kt', 'active writer has not reached a checkpoint\n')
        result = git_sync.resume(self.workspace)
        self.assertTrue(result['ok'])
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertEqual(identity, json.loads((self.workspace / git_sync.CONFIG).read_text())['id'])
        self.assertEqual('saved', self.save(writer)['state'])
        git_sync.request_push(self.workspace)
        git_sync.push_due(self.workspace)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'refs/heads/replica/fixture', repo=self.remote))

    def test_monitor_notifies_about_stalled_changes_without_touching_git(self):
        self.enable()
        git_sync.request_push(self.workspace)
        git_sync.push_due(self.workspace)
        (self.service / 'replica-git-health.json').write_text(json.dumps({'checked_at': time.time()}))
        self.write('A.kt', 'pending source\n')
        self.write('B.kt', 'manually staged\n')
        self.git('add', 'B.kt')
        self.write('evidence/private.apk', 'not source')
        before = (self.git('rev-parse', 'HEAD'), self.git('diff', '--cached'), self.git('diff'))
        notices = []
        send = lambda thread, prompt: notices.append((thread, prompt)) or 'accepted'
        initial = replica_git_notify.check(self.workspace, 'reviewer', now=100, sender=send)
        self.assertEqual([], initial['issues'])
        result = replica_git_notify.check(self.workspace, 'reviewer', now=701, sender=send)
        self.assertEqual(['source_changes_waiting'], result['issues'])
        self.assertEqual({'A.kt', 'B.kt'}, {p['path'] for p in result['changes']})
        self.assertEqual(1, len(notices))
        self.assertEqual(before, (self.git('rev-parse', 'HEAD'), self.git('diff', '--cached'), self.git('diff')))

    def test_monitor_disabled_notification_retry_and_rate_limit(self):
        self.enable()
        git_sync.disable(self.workspace)
        before = (self.workspace / git_sync.CONFIG).read_bytes()
        def failed(thread, prompt):
            raise RuntimeError('queue unavailable')
        first = replica_git_notify.check(self.workspace, 'reviewer', now=100, sender=failed)
        self.assertIn('notification_error', first)
        notices = []
        send = lambda thread, prompt: notices.append(thread) or 'accepted'
        result = replica_git_notify.check(self.workspace, 'reviewer', now=160, sender=send)
        self.assertIn('autosave_disabled', result['issues'])
        replica_git_notify.check(self.workspace, 'reviewer', now=220, sender=send)
        self.assertEqual(1, len(notices))
        replica_git_notify.check(self.workspace, 'reviewer', now=1961, sender=send)
        self.assertEqual(2, len(notices))
        self.assertEqual(before, (self.workspace / git_sync.CONFIG).read_bytes())

    def test_monitor_due_push_is_only_a_notification(self):
        self.enable(push_count=1)
        git_sync.request_push(self.workspace)
        git_sync.push_due(self.workspace)
        old_remote = self.git('rev-parse', 'refs/heads/replica/fixture', repo=self.remote)
        writer = self.writer()
        self.write('A.kt', 'saved but not pushed\n')
        self.save(writer)
        notices = []
        result = replica_git_notify.check(self.workspace, 'reviewer', sender=lambda *args: notices.append(args))
        self.assertIn('push_due_based_on_local_refs', result['issues'])
        self.assertEqual(1, len(notices))
        self.assertEqual(old_remote, self.git('rev-parse', 'refs/heads/replica/fixture', repo=self.remote))

    def test_baseline_excludes_artifacts_and_keeps_existing_source_together(self):
        self.write("A.kt", "existing edits\n")
        self.write("resources/icon.png", "fixture bitmap")
        self.write("build/output.apk", "generated")
        self.write("evidence/run.json", "private evidence")
        self.write("local.properties", "sdk.dir=/private")
        self.write(".env", "SECRET=fixture")
        result = self.enable()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.git("branch", "--show-current"), "replica/fixture")
        self.assertEqual(set(self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()),
                         {"A.kt", "B.kt", "resources/icon.png"})
        self.assertEqual(self.git("show", "HEAD:A.kt"), "existing edits")

    def test_stable_paths_add_delete_mode_and_manual_staging_are_isolated(self):
        self.enable()
        writer = self.writer(paths=("A.kt", "new[1].kt", "B.kt"))
        self.write("A.kt", "stable A")
        self.write("new[1].kt", "new source").chmod(0o755)
        (self.repo / "B.kt").unlink()
        self.write("manual.txt", "human staging")
        self.git("add", "manual.txt")
        self.write("in-progress.txt", "other writer")
        before = self.call("status", summary=True)["tasks"][writer[0]]["revision"]
        result = self.save(writer)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "manual.txt")
        self.assertEqual(self.git("ls-tree", "HEAD", "new[1].kt").split()[0], "100755")
        self.assertNotIn("B.kt", self.git("ls-tree", "--name-only", "HEAD").splitlines())
        self.assertNotIn("in-progress.txt", self.git("ls-tree", "--name-only", "HEAD").splitlines())
        self.assertEqual(self.call("status", summary=True)["tasks"][writer[0]]["revision"], before)

    def test_replay_and_empty_checkpoint_do_not_duplicate_or_capture_later_edits(self):
        self.enable()
        writer = self.writer()
        self.write("A.kt", "first save")
        first = self.save(writer, request_id="stable-event")
        self.write("A.kt", "later edit")
        replay = self.save(writer, request_id="stable-event")
        self.assertEqual(first["commit"], replay["commit"])
        self.assertEqual(self.git("show", "HEAD:A.kt"), "first save")
        self.save(writer)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.save(writer)["state"], "unchanged")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_enrolled_worker_does_not_rewrite_external_service_registration(self):
        self.enable()
        writer = self.writer()
        self.write("A.kt", "worker save")
        with patch.object(git_sync, "atomic_json", side_effect=PermissionError("external state is read-only")):
            self.assertTrue(self.save(writer)["ok"])
            self.assertTrue(git_sync.request_push(self.workspace)["ok"])

    def test_concurrent_writers_only_commit_their_own_files(self):
        self.enable()
        first = self.writer("one", ("A.kt",))
        second = self.writer("two", ("B.kt",))
        self.write("A.kt", "one")
        self.write("B.kt", "two")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, self.workspace, {"op": "git.checkpoint", "request_id": key,
                       "task_id": writer[0], "run_id": writer[1]}) for key, writer in (("one-save", first), ("two-save", second))]
            results = [future.result() for future in futures]
        for result, name in zip(results, ("A.kt", "B.kt")):
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", result["git"]["commit"]), name)

    def test_hook_failure_keeps_files_and_can_retry_without_index_repair(self):
        self.enable()
        writer = self.writer(paths=("A.kt", "New.kt"))
        self.write("A.kt", "not yet compilable")
        self.write("New.kt", "new source")
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.save(writer)
        self.assertFalse(result["ok"])
        self.assertEqual((self.repo / "A.kt").read_text(), "not yet compilable")
        self.assertEqual(self.git("show", "HEAD:A.kt"), "base A")
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.assertTrue(self.save(writer)["ok"])

    def test_submit_and_stop_use_common_boundary_without_holding_metadata_lock(self):
        self.enable()
        writer = self.writer()
        content = "source at delivery"
        self.write("A.kt", content)
        submitted = self.call("task.submit", task_id=writer[0], run_id=writer[1],
                              artifacts=[{"path": str(self.repo / "A.kt"), "sha256": hashlib.sha256(content.encode()).hexdigest()}],
                              self_checks=[{"name": "compile", "result": "fail", "evidence": ["incomplete fixture"]}], unresolved=[])
        self.assertEqual(submitted["git"]["state"], "saved")
        self.assertFalse(submitted["delivery"]["accepted"])
        self.write("A.kt", "final correction")
        original = git_sync._git
        def unlocked_git(*args, **kwargs):
            # Another thread can acquire the metadata lock during Git I/O.
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: self.call("coverage", summary=True))
                self.assertTrue(future.result(timeout=2)["ok"])
            return original(*args, **kwargs)
        with patch.object(git_sync, "_git", side_effect=unlocked_git):
            stopped = self.call("run.stopped", run_id=writer[1], outcome="delivered",
                                observation={"confirmed": True, "writers_stopped": True, "evidence": ["fixture stopped"]})
        self.assertTrue(stopped["released"])
        self.assertEqual(self.git("show", "HEAD:A.kt"), "final correction")

    def test_wait_saves_source_without_claiming_build_success(self):
        self.enable()
        writer = self.writer()
        self.call("task.add", task={"id": "dependency", "role": "analysis", "mode": "inspect",
                                    "description": "Recover missing signature"})
        self.write("A.kt", "partial source")
        result = self.call("task.wait", task_id=writer[0], run_id=writer[1],
                           requires=[{"task_id": "dependency"}], checkpoint={"note": "await signature"})
        self.assertTrue(result["stop_pending"])
        self.assertEqual(result["git"]["state"], "saved")
        self.assertEqual(self.git("show", "HEAD:A.kt"), "partial source")

    def test_failed_save_does_not_prevent_actual_stop(self):
        self.enable()
        writer = self.writer()
        self.write("A.kt", "keep file")
        self.git("add", "A.kt")
        stopped = self.call("run.stopped", run_id=writer[1], outcome="failed",
                            observation={"confirmed": True, "writers_stopped": True, "evidence": ["fixture"]})
        self.assertTrue(stopped["released"])
        self.assertFalse(stopped["git"]["ok"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "A.kt")

    def test_quantity_and_oldest_time_trigger_independently_and_restart_preserves_wait(self):
        self.enable(push_count=2, push_seconds=600)
        writer = self.writer()
        self.write("A.kt", "one")
        self.save(writer)
        oldest = git_sync.status(self.workspace)["oldest_pending_at"]
        git_sync.push_due(self.workspace, now=oldest + 1)
        self.assertFalse((self.workspace / git_sync.PUSH).exists())
        self.write("A.kt", "two")
        self.save(writer)
        git_sync.push_due(self.workspace, now=oldest + 2)
        self.assertEqual(self.git("rev-parse", "refs/heads/replica/fixture", repo=self.remote), self.git("rev-parse", "HEAD"))
        self.write("A.kt", "three")
        self.save(writer)
        pending = git_sync.status(self.workspace)
        self.assertEqual(pending["pending_commits"], 1)
        # A newly constructed service worker uses persisted Git history/time.
        worker = git_sync.GitSync(self.service, threading.Event())
        with patch.object(git_sync.time, "time", return_value=pending["oldest_pending_at"] + 601):
            worker.sweep()
        self.assertEqual(git_sync.status(self.workspace)["pending_commits"], 0)
        with patch.object(git_sync, "_git", wraps=git_sync._git) as git:
            worker.sweep()
        self.assertFalse(any(call.args[1] == "push" for call in git.call_args_list))

    def test_push_failure_retries_and_never_blocks_new_local_commits(self):
        self.enable(push_count=1)
        writer = self.writer()
        self.write("A.kt", "offline")
        self.save(writer)
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        git_sync.push_due(self.workspace)
        status = git_sync.status(self.workspace)
        self.assertEqual(status["push"]["state"], "error")
        self.write("A.kt", "still working")
        self.assertTrue(self.save(writer)["ok"])
        self.git("remote", "set-url", "origin", str(self.remote))
        git_sync.push_due(self.workspace, now=status["push"]["retry_at"] + 1)
        self.assertEqual(git_sync.status(self.workspace)["pending_commits"], 0)

    def test_slow_push_does_not_hold_commit_or_device_scheduler_thread(self):
        self.enable(push_count=1)
        writer = self.writer()
        self.write("A.kt", "before push")
        self.save(writer)
        entered, release = threading.Event(), threading.Event()
        original = git_sync._git
        def slow(repo, *args, **kwargs):
            if args[0] == "push":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture push not released")
            return original(repo, *args, **kwargs)
        worker = git_sync.GitSync(self.service, threading.Event())
        with patch.object(git_sync, "_git", side_effect=slow):
            thread = threading.Thread(target=worker.sweep)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.write("A.kt", "while push waits")
                self.assertTrue(self.save(writer)["ok"])
                self.assertTrue(self.call("status", summary=True)["ok"])
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(git_sync.status(self.workspace)["pending_commits"], 1)

    def test_invalid_owner_or_stop_does_not_create_commit(self):
        self.enable()
        writer = self.writer()
        self.write("A.kt", "still writing")
        head = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ReplicaError, "different writer"):
            self.save(writer, paths=[str(self.repo / "B.kt")])
        with self.assertRaisesRegex(ReplicaError, "confirmed"):
            self.call("run.stopped", run_id=writer[1], outcome="failed", observation={"confirmed": False})
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_tracked_excluded_history_is_not_silently_pushed(self):
        self.write("secret.keystore", "fixture key")
        self.git("add", "secret.keystore")
        self.git("commit", "-qm", "existing forbidden history")
        with self.assertRaisesRegex(ReplicaError, "excluded path"):
            self.enable()
        self.assertFalse((self.workspace / git_sync.CONFIG).exists())
        self.assertEqual(self.git("branch", "--show-current"), "dev")

    def test_unconfigured_status_is_explicit(self):
        self.assertEqual(self.call("status", summary=True)["git"],
                         {"enabled": False, "state": "not_enabled"})

    def test_existing_project_reuses_only_published_history_and_scopes_writers(self):
        self.write("replica-app/Main.kt", "old source")
        self.write("analysis/old.log", "already published fixture log")
        self.git("add", ".")
        self.git("commit", "-qm", "existing project")
        baseline = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        nested_workspace = self.repo / "workflow"
        shutil.move(self.workspace, nested_workspace)
        self.workspace = nested_workspace
        self.writer("planner", ("workflow/notes.md",))
        self.write("workflow/notes.md", "live planning")
        self.write("replica-app/Main.kt", "accumulated source")
        result = self.enable(include=["replica-app"], push_count=1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["config"]["published_history"]["commit"], baseline)
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         "replica-app/Main.kt")
        writer = self.writer("code", ("replica-app/Main.kt",))
        self.write("replica-app/Main.kt", "next stable source")
        self.assertTrue(self.save(writer)["ok"])
        git_sync.push_due(self.workspace)
        self.assertEqual(git_sync.status(self.workspace)["pending_commits"], 0)
        self.assertEqual(self.git("rev-parse", "main", repo=self.remote), baseline)
        # Renaming existing bytes must not bypass the incremental history check.
        self.git("mv", "analysis/old.log", "analysis/new.log")
        self.git("commit", "-qm", "new excluded path")
        git_sync.push_due(self.workspace)
        self.assertIn("excluded path", git_sync.status(self.workspace)["push"]["error"])

    def test_published_history_cannot_be_redirected_to_another_remote(self):
        self.write("analysis/old.log", "already published")
        self.git("add", ".")
        self.git("commit", "-qm", "old log")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.enable(push_count=1)
        other = self.root / "other.git"
        self.git("init", "-q", "--bare", str(other))
        self.git("remote", "set-url", "origin", str(other))
        git_sync.push_due(self.workspace)
        self.assertIn("remote changed", git_sync.status(self.workspace)["push"]["error"])
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", repo=other), "")

    def test_remote_divergence_is_reported_without_force_or_checkout_changes(self):
        self.enable(push_count=1)
        git_sync.push_due(self.workspace)
        other = self.root / "other"
        self.git("clone", "-q", "--branch", "replica/fixture", str(self.remote), str(other))
        self.git("config", "user.name", "Other", repo=other)
        self.git("config", "user.email", "other@example.invalid", repo=other)
        self.git("config", "commit.gpgsign", "false", repo=other)
        (other / "A.kt").write_text("other development")
        self.git("commit", "-qam", "other change", repo=other)
        self.git("push", "origin", "HEAD", repo=other)
        remote_head = self.git("rev-parse", "HEAD", repo=other)
        writer = self.writer()
        self.write("A.kt", "our source")
        saved = self.save(writer)
        git_sync.push_due(self.workspace)
        self.assertEqual(git_sync.status(self.workspace)["push"]["state"], "error")
        self.assertEqual(self.git("rev-parse", "refs/heads/replica/fixture", repo=self.remote), remote_head)
        self.assertEqual(self.git("rev-parse", "HEAD"), saved["commit"])
        self.assertEqual((self.repo / "A.kt").read_text(), "our source")

    def test_lost_push_response_recovers_with_ordinary_retry(self):
        self.enable(push_count=1)
        original = git_sync._git
        def lose_response(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[0] == "push":
                raise ReplicaError("fixture: successful remote write, lost response")
            return result
        with patch.object(git_sync, "_git", side_effect=lose_response):
            git_sync.push_due(self.workspace)
        before = self.git("rev-parse", "HEAD")
        git_sync.push_due(self.workspace, now=git_sync.status(self.workspace)["push"]["retry_at"] + 1)
        self.assertEqual(git_sync.status(self.workspace)["pending_commits"], 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_unborn_repository_can_save_its_initial_source(self):
        fresh = self.root / "new-app"
        fresh.mkdir()
        self.git("init", "-q", "-b", "dev", repo=fresh)
        self.git("config", "user.name", "Fixture", repo=fresh)
        self.git("config", "user.email", "fixture@example.invalid", repo=fresh)
        self.git("config", "commit.gpgsign", "false", repo=fresh)
        self.git("remote", "add", "origin", str(self.remote), repo=fresh)
        (fresh / "Main.kt").write_text("initial source")
        result = git_sync.enable(self.workspace, repo=fresh, service_state=self.service)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.git("show", "HEAD:Main.kt", repo=fresh), "initial source")

    def test_bound_paths_save_only_in_adopted_checkout(self):
        self.enable()
        writer = self.writer()
        adopted = self.root / "adopted"
        new_repo, new_workspace = adopted / "app", adopted / "workflow"
        shutil.copytree(self.repo, new_repo)
        shutil.copytree(self.workspace, new_workspace)
        store = Store(new_workspace)
        # Exercise path bindings, not the separate process-adoption protocol.
        with store.locked() as state:
            state["schema"] = 3
            state["relocation"] = {"format": 1, "binding_revision": 1,
                                   "target_workspace": str(new_workspace), "continuation_backend": "native",
                                   "bindings": [{"from": str(self.repo), "to": str(new_repo)},
                                                {"from": str(self.workspace), "to": str(new_workspace)}]}
            store.commit(state)
        (new_repo / "A.kt").write_text("adopted edit")
        before = self.git("rev-parse", "HEAD")
        result = execute(new_workspace, {"op": "git.checkpoint", "request_id": "adopted-save",
                                         "task_id": writer[0], "run_id": writer[1]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.git("show", "HEAD:A.kt", repo=new_repo), "adopted edit")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_existing_service_pushes_without_another_replica_call(self):
        self.enable(push_count=99, push_seconds=1)
        self.service.mkdir(exist_ok=True)
        (self.service / "config.json").write_text("{}")
        log = (self.root / "service.log").open("w+")
        self.addCleanup(log.close)
        process = subprocess.Popen([sys.executable, "-m", "workbench.cli", "--state", str(self.service), "serve"],
                                   env={**self.env, "PYTHONPATH": str(ROOT)}, stdout=log, stderr=log)
        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.seek(0)
                self.fail(log.read())
            try:
                capabilities = rpc(self.service, "service.capabilities")
                self.assertTrue(capabilities["replica_git_sync"])
                break
            except WorkbenchError:
                time.sleep(0.05)
        else:
            self.fail("fixture service did not start")
        # Only inspect the bare remote; no replica operation triggers the push.
        while time.monotonic() < deadline:
            ref = self.git("rev-parse", "--verify", "--quiet", "refs/heads/replica/fixture",
                           repo=self.remote, allowed=(0, 1))
            if ref:
                self.assertEqual(ref, self.git("rev-parse", "HEAD"))
                break
            time.sleep(0.05)
        else:
            log.seek(0)
            self.fail("timer did not push: " + log.read())

    def test_cli_enrollment_checkpoint_flush_and_disable(self):
        def cli(*args):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--workspace", str(self.workspace), *args])
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())
        with patch("workbench.cli.start", return_value={"replica_git_sync": True}):
            enabled = cli("git-enable", "--repo", str(self.repo), "--service-state", str(self.service))
        self.assertEqual(enabled["timer"], "starting")
        writer = self.writer()
        self.write("A.kt", "CLI save")
        result = cli("git-checkpoint", "--task", writer[0], "--run", writer[1], "--request-id", "cli-save")
        self.assertEqual(result["git"]["state"], "saved")
        self.assertIn("git", cli("status", "--summary"))
        self.assertEqual(cli("git-flush")["state"], "push_requested")
        cli("git-disable")
        self.assertFalse(cli("git-status")["git"]["enabled"])


if __name__ == "__main__":
    unittest.main()
