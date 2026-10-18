"""Small synchronous source checkpoints and service-owned, batched Git pushes.

Git is the snapshot store. Network work never holds the replica metadata lock,
the commit lock, or a device/project queue lease.
"""

from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import uuid

from ..common import atomic_json, state_dir
from .paths import lexical, no_symlinks, resolve_path, effective_writer_index
from .store import ReplicaError, Store

CONFIG = "coordination/git-config.json"
SAVE = "coordination/git-save.json"
PUSH = "coordination/git-push.json"
FLUSH = "coordination/git-flush.json"
INTERVAL = 5
TIMEOUT = 30
EXCLUDED_PARTS = {".git", ".gradle", ".kotlin", ".venv", "node_modules", "__pycache__",
                  "build", ".replication", ".artifacts", "artifacts", "evidence", "coordination"}
EXCLUDED_NAMES = {"local.properties", "workbench.project.json", ".env", ".envrc"}
EXCLUDED_SUFFIXES = {".apk", ".aab", ".apks", ".xapk", ".jks", ".keystore", ".pem", ".key", ".log"}


class _ExcludedHistory(ReplicaError):
    pass


def _read(store, name, default=None):
    path = store.metadata_path(name)
    return json.loads(path.read_text()) if path.exists() else default


def _git(repo, *args, data=None, allowed=(0,), timeout=TIMEOUT):
    routing = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
               "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
               "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"}
    env = {k: v for k, v in os.environ.items() if k not in routing}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # A timeout must also stop a hook/SSH child, not leave it writing later.
    proc = subprocess.Popen(["git", "--literal-pathspecs", "-C", str(repo), *args],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, start_new_session=True)
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise ReplicaError(f"Git {args[0]} timed out after {timeout}s") from exc
    if proc.returncode not in allowed:
        detail = re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", os.fsdecode(err).strip())
        raise ReplicaError(f"Git {args[0]} failed ({proc.returncode}): {detail}")
    return out


def _text(repo, *args, **kwargs):
    return os.fsdecode(_git(repo, *args, **kwargs)).strip()


def _head(repo):
    return _text(repo, "rev-parse", "--verify", "--quiet", "HEAD", allowed=(0, 1)) or None


def _common(repo):
    return Path(_text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


@contextmanager
def _lock(path, timeout=TIMEOUT):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReplicaError("Git checkpoint is busy; retry at the next stable boundary")
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def configured(workspace):
    path = Path(workspace) / CONFIG
    try:
        return path.is_file() and json.loads(path.read_text()).get("enabled", False)
    except (OSError, ValueError):
        return True  # Surface damaged configuration without blocking task release.


def _config(store, state=None):
    config = _read(store, CONFIG)
    if not config or not config.get("enabled"):
        return None
    if state is None:
        with store.locked() as state:
            repo = resolve_path(store.workspace, state, config["repo"])
    else:
        repo = resolve_path(store.workspace, state, config["repo"])
    return {**config, "repo": str(no_symlinks(repo))}


def _blocked(path, config):
    path = Path(path)
    return (bool(set(path.parts) & EXCLUDED_PARTS) or path.name in EXCLUDED_NAMES
            or path.name.startswith(".env.") or path.suffix.lower() in EXCLUDED_SUFFIXES
            or any(fnmatch.fnmatchcase(path.as_posix(), rule) for rule in config["exclude"]))


def _included(path, config):
    path = Path(path)
    return not _blocked(path, config) and any(path.is_relative_to(Path(root)) for root in config["include"])


def _files(repo, paths=()):
    return sorted(set(os.fsdecode(value) for value in
                      _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths).split(b"\0") if value))


def _check_repo(config):
    repo = Path(config["repo"])
    if _text(repo, "rev-parse", "--show-toplevel") != str(repo):
        raise ReplicaError("Git configuration must name the App repository root")
    if _text(repo, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)) != config["branch"]:
        raise ReplicaError("The App checkout is no longer on its configured replica branch")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock"):
        if Path(_text(repo, "rev-parse", "--path-format=absolute", "--git-path", name)).exists():
            raise ReplicaError(f"Git has an unfinished operation: {name}")


def _push_url(repo, remote):
    urls = _text(repo, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(urls) != 1:
        raise ReplicaError("Published history requires exactly one push destination")
    return urls[0]


def _remote_hash(repo, remote):
    return hashlib.sha256(os.fsencode(_push_url(repo, remote))).hexdigest()


def _check_history(repo, config, head, base=None):
    if not head:
        return
    published = config.get("published_history")
    if published:
        if _remote_hash(repo, config["remote"]) != published["remote_hash"]:
            raise ReplicaError("The remote changed; recheck the previously published history before pushing")
        base = base or published["commit"]
    if base:
        _git(repo, "merge-base", "--is-ancestor", base, head)
        # Names, rather than only new object IDs, also catch copies/renames of
        # existing content into an excluded path. Removing such files is fine.
        paths = [os.fsdecode(p) for p in _git(repo, "log", "--format=", "--name-only", "-z",
                 "--diff-filter=ACMRT", f"{base}..{head}").split(b"\0") if p]
    else:
        paths = [line.partition(" ")[2] for line in
                 os.fsdecode(_git(repo, "rev-list", "--objects", head)).splitlines() if " " in line]
    for path in paths:
        if _blocked(path, config):
            raise _ExcludedHistory(f"Existing Git history contains an excluded path: {path}")


def register(workspace, config):
    target = Path(config["service_state"]) / "replica-git" / (config["id"] + ".json")
    registration = {"workspace": str(Path(workspace).resolve())}
    # Enrollment owns this external write. Ordinary worker saves only need to
    # read the existing registration, including from workspace-write sandboxes.
    if target.is_file() and json.loads(target.read_text()) == registration:
        return
    atomic_json(target, registration)


def enable(workspace, *, repo, remote="origin", branch=None, include=None, exclude=None,
           push_count=5, push_seconds=600, service_state=None):
    store = Store(workspace)
    if type(push_count) is not int or push_count < 1 or type(push_seconds) is not int or push_seconds < 1:
        raise ReplicaError("Push count and seconds must be positive integers")
    roots = include or ["."]
    for root in roots:
        if not isinstance(root, str) or not root or Path(root).is_absolute() or ".." in Path(root).parts:
            raise ReplicaError("Git include paths must be relative to the App repository")
    scope = {"include": roots, "exclude": exclude or []}
    with store.locked() as state:
        if not state:
            raise ReplicaError("Initialize the replica workspace first")
        repo = no_symlinks(resolve_path(workspace, state, str(repo)))
        if repo == Path(__file__).resolve().parents[2] or Path(__file__).resolve().is_relative_to(repo):
            raise ReplicaError("Git autosave is for the App project, not the Workbench checkout")
        active = [task["id"] for task in state["tasks"].values() if task["run_id"] and
                  any(path.is_relative_to(repo) and _included(path.relative_to(repo), scope)
                      for path in (resolve_path(workspace, state, p) for p in task["outputs"]))]
        if active:
            raise ReplicaError("Enroll at a stable boundary after these engineering writers stop: " + ", ".join(active))
        identity = str(state["project"].get("id") or state["project"].get("name") or "app")
    if not repo.is_dir():
        raise ReplicaError("App repository directory does not exist")
    if repo == store.workspace:
        raise ReplicaError("Keep the App repository separate from replica coordination metadata")
    # Remote setup stays explicit; do not create a repository and then discover
    # that there is nowhere to push it.
    if not (repo / ".git").exists():
        raise ReplicaError("Initialize the App Git repository and configure its remote before git-enable")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip(".-") or hashlib.sha256(identity.encode()).hexdigest()[:12]
    branch = branch or "replica/" + slug
    if not branch.startswith("replica/"):
        raise ReplicaError("Use a dedicated replica/<project-id> branch")
    _git(repo, "check-ref-format", "refs/heads/" + branch)
    if not remote or remote.startswith("-") or remote not in _text(repo, "remote").splitlines():
        raise ReplicaError("Configure the named Git remote in the App repository first")
    old = _read(store, CONFIG, {})
    config = {"enabled": True, "id": old.get("id", uuid.uuid4().hex), "repo": str(repo),
              "branch": branch, "remote": remote, "include": roots, "exclude": exclude or [],
              "push_count": push_count, "push_seconds": push_seconds,
              "service_state": str(state_dir(service_state)), "enabled_at": time.time()}
    if old.get("enabled"):
        comparable = ("repo", "branch", "remote", "include", "exclude", "push_count", "push_seconds")
        effective = _config(store)
        if any(effective[key] != config[key] for key in comparable):
            raise ReplicaError("Disable autosave before changing its repository or policy")
        if old.get("published_history"):
            config["published_history"] = old["published_history"]
    with _lock(_common(repo) / "replica-commit.lock"):
        config["start_head"] = _head(repo)
        try:
            _check_history(repo, config, config["start_head"])
        except _ExcludedHistory:
            # Existing project repositories can already contain published logs
            # and coordination records. Reuse that exact remote baseline, never
            # approve unpublished excluded history or change its destination.
            push_url = _push_url(repo, remote)
            refs = _text(repo, "ls-remote", "--heads", push_url).splitlines()
            if not any(line.split()[0] == config["start_head"] for line in refs):
                raise
            config["published_history"] = {"commit": config["start_head"],
                                            "remote_hash": _remote_hash(repo, remote)}
        current = _text(repo, "symbolic-ref", "--short", "HEAD")
        # Refuse an existing different branch: switching its tree can replace
        # the current App files. Creating a branch at HEAD preserves them.
        if current != branch:
            exists = _text(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, allowed=(0, 1))
            if exists:
                raise ReplicaError("Replica branch already exists; explicitly select it before enrollment")
            _git(repo, "switch", "-c", branch)
        _check_repo(config)
        if old.get("enabled"):
            config.update(enabled_at=old["enabled_at"], start_head=old["start_head"])
        elif old:
            config["id"] = uuid.uuid4().hex
            for filename in (SAVE, PUSH, FLUSH):
                store.write_json(filename, {})
            registration = Path(old["service_state"]) / "replica-git" / (old["id"] + ".json")
            registration.unlink(missing_ok=True)
        store.write_json(CONFIG, config)
    result = checkpoint(store, config, [p for p in _files(repo) if _included(p, config)],
                        "enroll-" + config["id"], "initial App source", baseline=True)
    register(workspace, config)
    return {"ok": result["ok"], "git": result, "config": config}


def disable(workspace):
    store = Store(workspace)
    config = _read(store, CONFIG)
    if config:
        store.write_json(CONFIG, {**config, "enabled": False})
    return {"ok": True, "enabled": False}


def checkpoint(store, config, paths, request_id, label, *, baseline=False):
    repo = Path(config["repo"])
    started = time.monotonic()
    marker = "Replica-Checkpoint: " + hashlib.sha256((config["id"] + request_id).encode()).hexdigest()
    try:
        with _lock(_common(repo) / "replica-commit.lock"):
            _check_repo(config)
            head = _head(repo)
            previous = _text(repo, "log", "-1", "--format=%H", "--fixed-strings", "--grep=" + marker) if head else ""
            if previous:
                return {"ok": True, "state": "already_saved", "commit": previous}
            paths = sorted(set(paths))
            if not paths:
                return {"ok": True, "state": "no_engineering_files", "commit": head}
            for path in paths:
                no_symlinks(lexical(path, base=repo))
                if not _included(path, config):
                    raise ReplicaError(f"Path is outside Git source scope: {path}")
            # Preserve unrelated manual staging; never consume a partial staged
            # version of a file the worker is about to replace.
            staged = set(os.fsdecode(x) for x in _git(repo, "diff", "--cached", "--name-only", "-z", "--", *paths).split(b"\0") if x)
            if not baseline and staged.intersection(paths):
                raise ReplicaError("Checkpoint paths have staged changes; preserve them and resolve explicitly")
            known = set(_files(repo, paths))
            paths = [p for p in paths if p in known]
            if not paths:
                return {"ok": True, "state": "unchanged", "commit": head}
            tracked = set(os.fsdecode(x) for x in _git(repo, "ls-files", "--cached", "-z", "--", *paths).split(b"\0") if x)
            new = [p for p in paths if p not in tracked]
            if new:
                # Intent-to-add teaches Git the names without leaving a staged
                # content snapshot behind if a hook rejects the commit.
                _git(repo, "add", "--intent-to-add", "--pathspec-from-file=-", "--pathspec-file-nul",
                     data=b"".join(os.fsencode(p) + b"\0" for p in new))
            changed = set(os.fsdecode(x) for x in _git(repo, "diff", "HEAD", "--name-only", "-z", "--", *paths).split(b"\0") if x) if head else set(paths)
            if not changed.intersection(paths):
                return {"ok": True, "state": "unchanged", "commit": head}
            data = b"".join(os.fsencode(p) + b"\0" for p in paths)
            _git(repo, "commit", "--only", "--no-status", "-m", f"replica: {label}\n\n{marker}",
                 "--pathspec-from-file=-", "--pathspec-file-nul", data=data)
            result = {"ok": True, "state": "saved", "commit": _head(repo), "paths": paths,
                      "saved_at": time.time(), "elapsed_seconds": time.monotonic() - started}
    except (OSError, ValueError) as exc:
        result = {"ok": False, "state": "error", "error": str(exc), "attempted_at": time.time()}
    store.write_json(SAVE, result)
    return result


def save_task(workspace, state, task, request_id, paths=None):
    store = Store(workspace)
    config = _config(store, state)
    if not config:
        return {"ok": True, "state": "disabled"}
    repo = Path(config["repo"])
    assigned = {resolve_path(workspace, state, p) for p in task["outputs"]}
    selected = [resolve_path(workspace, state, p) for p in paths] if paths is not None else assigned
    writers = effective_writer_index(workspace, state)
    if any(p not in assigned or writers.get(str(p), (None, None))[1] != task["id"] for p in selected):
        raise ReplicaError("Git checkpoint contains a different writer's output")
    scoped = [p.relative_to(repo).as_posix() for p in selected if p.is_relative_to(repo)
              and _included(p.relative_to(repo), config)]
    result = checkpoint(store, config, scoped, request_id, f"{task['id']} progress")
    register(workspace, config)
    return result


def _pending(store, config):
    repo = config["repo"]
    head = _head(repo)
    pushed = _read(store, PUSH, {})
    base = pushed.get("commit") or config["start_head"]
    if not head or pushed.get("commit") == head:
        return {"count": 0, "oldest_at": None, "head": head}
    if base:
        _git(repo, "merge-base", "--is-ancestor", base, head)
    revisions = _text(repo, "log", "--format=%ct", f"{base}..{head}" if base else head).splitlines()
    oldest = min(map(int, revisions)) if revisions else config["enabled_at"]
    if not pushed.get("commit"):
        oldest = min(oldest, config["enabled_at"])
    return {"count": max(1, len(revisions)), "oldest_at": oldest, "head": head}


def status(workspace):
    store = Store(workspace)
    try:
        config = _config(store)
        if not config:
            return {"enabled": False, "state": "not_enabled"}
        _check_repo(config)
        pending = _pending(store, config)
        heartbeat = Path(config["service_state"]) / "replica-git-health.json"
        health = json.loads(heartbeat.read_text()) if heartbeat.exists() else {}
        registration = Path(config["service_state"]) / "replica-git" / (config["id"] + ".json")
        registered = json.loads(registration.read_text()) if registration.exists() else {}
        return {"enabled": True, "branch": config["branch"], "remote": config["remote"],
                "pending_commits": pending["count"], "oldest_pending_at": pending["oldest_at"],
                "save": _read(store, SAVE), "push": _read(store, PUSH),
                "timer_running": (registered.get("workspace") == str(store.workspace)
                                  and time.time() - health.get("checked_at", 0) < max(2 * TIMEOUT, 3 * INTERVAL))}
    except (OSError, ValueError) as exc:
        return {"enabled": True, "error": str(exc), "timer_running": False}


def request_push(workspace):
    store = Store(workspace)
    config = _config(store)
    if not config:
        raise ReplicaError("Git autosave is not enabled")
    store.write_json(FLUSH, {"id": uuid.uuid4().hex, "requested_at": time.time()})
    register(workspace, config)
    return {"ok": True, "state": "push_requested"}


def push_due(workspace, *, now=None):
    store = Store(workspace)
    config = _config(store)
    if not config:
        return
    repo = config["repo"]
    now = time.time() if now is None else now
    with _lock(_common(repo) / "replica-push.lock", timeout=0):
        previous = _read(store, PUSH, {})
        if now < previous.get("retry_at", 0):
            return
        flush = _read(store, FLUSH, {})
        try:
            pending = _pending(store, config)
            if not pending["count"]:
                if flush.get("id") != previous.get("flush_id"):
                    store.write_json(PUSH, {**previous, "flush_id": flush.get("id")})
                return
            due = (pending["count"] >= config["push_count"]
                   or now - pending["oldest_at"] >= config["push_seconds"]
                   or flush.get("id") != previous.get("flush_id"))
            if not due:
                return
            _check_repo(config)
            _check_history(repo, config, pending["head"], previous.get("commit") or config["start_head"])
            # A fixed existing OID lets local checkpoints continue during push.
            _git(repo, "push", "--porcelain", config["remote"],
                 pending["head"] + ":refs/heads/" + config["branch"])
            if _read(store, CONFIG)["id"] != config["id"]:
                return  # Enrollment changed while this network request ran.
            store.write_json(PUSH, {"state": "synced", "commit": pending["head"],
                                   "synced_at": now, "flush_id": flush.get("id")})
        except (OSError, ValueError) as exc:
            if _read(store, CONFIG)["id"] != config["id"]:
                return
            attempts = previous.get("attempts", 0) + 1
            store.write_json(PUSH, {**previous, "state": "error", "error": str(exc),
                                   "attempts": attempts, "retry_at": now + min(300, 5 * 2 ** min(attempts, 6))})


class GitSync:
    """One optional thread in the existing service, outside the device scheduler."""

    def __init__(self, directory, stopping):
        self.directory = Path(directory)
        self.stopping = stopping
        self.thread = threading.Thread(target=self.run, name="replica-git-push", daemon=True)

    def sweep(self):
        active = False
        for path in sorted((self.directory / "replica-git").glob("*.json")):
            if self.stopping.is_set():
                break
            try:
                workspace = Path(json.loads(path.read_text())["workspace"])
                if configured(workspace):
                    active = True
                    push_due(workspace)
            except (OSError, ValueError, KeyError):
                # Per-project status holds ordinary Git errors; one stale
                # registration must not stop other projects or device work.
                continue
        if active:
            atomic_json(self.directory / "replica-git-health.json", {"checked_at": time.time()})

    def run(self):
        while not self.stopping.is_set():
            try:
                self.sweep()
            except OSError:
                pass
            self.stopping.wait(INTERVAL)
