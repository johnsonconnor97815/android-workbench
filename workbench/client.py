import json
import os
from pathlib import Path
import secrets
import time
from .common import TERMINAL, atomic_json, digest, lock_file, rpc, state_dir


class Client:
    def __init__(self, project, directory=None, session=None):
        self.directory = state_dir(directory)
        self.requested_project = str(Path(project).expanduser().resolve())
        self.project = self.requested_project
        self.mode = "project"
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
                    {
                        "project": self.requested_project,
                        "name": self.name,
                        "mode": (
                            "project"
                            if (
                                Path(self.requested_project)
                                / "workbench.project.json"
                            ).is_file()
                            else "device_management"
                        ),
                    },
                )
                self.project = opened["project"]
                self.mode = opened["mode"]
                self.token = opened["token"]
                atomic_json(self.identity, opened)
        finally:
            os.close(fd)

    def call(self, method, params=None):
        if method == "jobs.submit" and params and not os.environ.get("AWB_INTERNAL_GRANT"):
            params = self.prepare_tools(params)
        return rpc(self.directory, method, params, self.token)

    def prepare_tools(self, request):
        """Queue write-locked prerequisites before a tool consumer gets any lock.

        This is also used by MCP jobs_submit and compatibility entrypoints. A
        failed repair fails its dependent operation without starting analysis.
        Inventory/check remain read-only and never trigger downloads.
        """
        path = Path(self.project) / "workbench.project.json"
        document = json.loads(path.read_text())
        if document.get("toolchain", {}).get("auto_install") is not True:
            return request
        entry = document.get("operations", {}).get(request.get("operation"), {})
        requirements = entry.get("requires_tools", [])
        frida_versions = entry.get('requires_frida_client', {})
        if frida_versions:
            arguments = request.get('args', [])
            version = next((arguments[i + 1] for i, x in enumerate(arguments[:-1]) if x == '--version'), None)
            version = next((x.split('=', 1)[1] for x in arguments if x.startswith('--version=')), version)
            tools_version = document.get('toolchain', {}).get('frida_tools_versions', {}).get(version, frida_versions.get(version))
            if not tools_version:
                raise ValueError('Choose a compatible explicit frida-tools version in toolchain.frida_tools_versions before preparing this Frida release')
            key = request.get('request_key')
            if not isinstance(key, str) or not key:
                raise ValueError('request_key is required before preparing Frida')
            prepared = rpc(self.directory, 'jobs.submit', {
                'operation': 'frida.install_client',
                'args': ['install', '--version', version, '--tools-version', tools_version],
                'timeout': 1800, 'request_key': 'auto-frida-' + digest([key, version, tools_version]),
            }, self.token)
            return {**request, 'dependencies': [*request.get('dependencies', []), prepared['id']]}
        if not requirements:
            return request
        if "tools.ensure" not in document.get("operations", {}):
            raise ValueError("Automatic tool preparation requires tools.ensure; update project registration")
        arguments = request.get("args", [])
        selected = []
        for name in requirements:
            explicit = entry.get("tool_flags", {}).get(name)
            if explicit and any(x == explicit or x.startswith(explicit + "=") for x in arguments):
                continue
            if name == "$tool":
                name = next((arguments[i + 1] for i, x in enumerate(arguments[:-1]) if x == "--tool"), None)
                name = next((x.split("=", 1)[1] for x in arguments if x.startswith("--tool=")), name)
                if name not in {"binwalk", "unblob"}:
                    raise ValueError("Firmware scan requires --tool binwalk or unblob")
            selected.append(name)
        if not selected:
            return request
        key = request.get("request_key")
        if not isinstance(key, str) or not key:
            raise ValueError("request_key is required before preparing tools")
        dependencies = list(request.get("dependencies", []))
        for name in dict.fromkeys(selected):
            prepared = rpc(self.directory, "jobs.submit", {
                "operation": "tools.ensure", "args": ["ensure", "--tool", name],
                "timeout": 7200, "request_key": "auto-tools-" + digest([key, name]),
                "purpose": "Prepare " + name + " for " + str(request["operation"]),
            }, self.token)
            if prepared["id"] not in dependencies:
                dependencies.append(prepared["id"])
        return {**request, "dependencies": dependencies}

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
