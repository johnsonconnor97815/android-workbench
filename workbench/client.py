import json
import os
from pathlib import Path
import secrets
import time
from .common import TERMINAL, atomic_json, digest, lock_file, rpc, state_dir


class Client:
    def __init__(self, project, directory=None, session=None):
        self.directory = state_dir(directory)
        self.project = str(Path(project).resolve())
        self.name = (
            session
            or os.environ.get("ANDROID_WORKBENCH_SESSION")
            or os.environ.get("CODEX_THREAD_ID")
            or "cli"
        )
        self.identity = (
            self.directory / "clients" / (digest([self.project, self.name]) + ".json")
        )
        fd = lock_file(self.identity.with_suffix(".lock"), blocking=True)
        try:
            if self.identity.exists():
                self.token = json.loads(self.identity.read_text())["token"]
            else:
                opened = rpc(
                    self.directory,
                    "sessions.open",
                    {"project": self.project, "name": self.name},
                )
                self.token = opened["token"]
                atomic_json(self.identity, opened)
        finally:
            os.close(fd)

    def call(self, method, params=None):
        return rpc(self.directory, method, params, self.token)

    def submit(self, **spec):
        spec.setdefault("request_key", secrets.token_hex(16))
        return self.call("jobs.submit", spec)

    def wait(self, identifier, timeout=None):
        start = time.monotonic()
        while True:
            job = self.call("jobs.status", {"id": identifier})
            if job["state"] in TERMINAL or job["state"] == "needs_recovery":
                return job
            if timeout is not None and time.monotonic() - start > timeout:
                raise TimeoutError("Client wait timed out; task remains managed")
            time.sleep(0.2)
