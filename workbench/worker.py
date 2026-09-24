"""One supervised process per protected interval; insertion runs in this controller."""

from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import threading
import traceback
import xml.etree.ElementTree as ET

from .common import (
    WorkbenchError,
    atomic_json,
    encode,
    file_hash,
    lock_file,
    process_identity,
    rpc,
    same_process,
    is_adb_server,
    stop_group,
    descendants,
    become_subreaper,
)


class Cancelled(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class Device:
    def __init__(self, runner):
        self.runner = runner
        self.hooks = []
        self.forward = None
        self.frida_device = None

    def adb(self, *args, timeout=30, binary=False, cleanup=False):
        spec = self.runner.spec
        data = self.runner.command(
            [spec["adb"], "-s", spec["serial"], *map(str, args)],
            timeout=timeout,
            cleanup=cleanup,
        )
        return data if binary else data.decode(errors="replace").strip()

    def observe(self):
        boot = self.adb("shell", "cat", "/proc/sys/kernel/random/boot_id")
        activity = self.adb("shell", "dumpsys", "activity", "activities")
        focus = next(
            (
                x.strip()
                for x in activity.splitlines()
                if "mResumedActivity" in x or "topResumedActivity" in x
            ),
            "",
        )
        match = re.search(r"([A-Za-z0-9_.$]+)/([A-Za-z0-9_.$]+)", focus)
        app = match.group(1) if match else None
        pid = self.adb("shell", "pidof", app, timeout=10) if app else ""
        value = {
            "boot_id": boot,
            "activity": match.group(0) if match else None,
            "app": app,
            "pid": pid,
            "hooks": [x["package"] for x in self.hooks],
            "observed_at": time.time(),
        }
        self.runner.call("observation", observation=value)
        return value

    def info(self):
        properties = {}
        for line in self.adb("shell", "getprop").splitlines():
            match = re.fullmatch(r"\[([^\]]+)\]: \[([^\]]*)\]", line.strip())
            if match and match.group(1) in {
                "ro.product.brand",
                "ro.product.manufacturer",
                "ro.product.model",
                "ro.product.name",
                "ro.product.device",
                "ro.build.id",
                "ro.build.version.release",
                "ro.build.version.sdk",
                "ro.build.version.security_patch",
                "ro.build.fingerprint",
                "ro.product.cpu.abilist",
            }:
                properties[match.group(1)] = match.group(2)

        size_output = self.adb("shell", "wm", "size")
        size = None
        for line in size_output.splitlines():
            match = re.search(r"(?:Override|Physical) size:\s*(\d+x\d+)", line)
            if match:
                size = match.group(1)

        meminfo = self.adb("shell", "cat", "/proc/meminfo")
        memory = None
        for line in meminfo.splitlines():
            match = re.fullmatch(r"MemTotal:\s*(\d+)\s*kB", line.strip())
            if match:
                total_kib = int(match.group(1))
                memory = {
                    "total_kib": total_kib,
                    "total_gib": round(total_kib / (1024 * 1024), 2),
                }
                break

        storage = None
        data_lines = [
            line for line in self.adb("shell", "df", "-k", "/data").splitlines() if line.strip()
        ]
        if data_lines:
            columns = data_lines[-1].split()
            if len(columns) >= 4:
                total_kib = int(columns[1])
                available_kib = int(columns[3])
                storage = {
                    "filesystem": columns[0],
                    "total_kib": total_kib,
                    "available_kib": available_kib,
                    "total_gib": round(total_kib / (1024 * 1024), 2),
                    "available_gib": round(available_kib / (1024 * 1024), 2),
                    "mount": columns[-1],
                }

        return {
            "schema": 1,
            "boot_id": self.adb("shell", "cat", "/proc/sys/kernel/random/boot_id"),
            "properties": properties,
            "screen": {"size": size},
            "memory": memory,
            "storage": storage,
            "observed_at": time.time(),
        }

    def validate_ui(self, suffix):
        expected = self.runner.spec.get("ui_expect", [])
        if not expected:
            return None
        remote = f"/data/local/tmp/awb-{self.runner.grant['job']}-{suffix}.xml"
        try:
            self.adb("shell", "uiautomator", "dump", remote)
            data = self.adb("exec-out", "cat", remote, binary=True)
            path = self.runner.directory / ("page-" + suffix + ".xml")
            path.write_bytes(data)
            root = ET.fromstring(data)
            for condition in expected:
                if not any(
                    all(
                        node.attrib.get(key) == value
                        for key, value in condition.items()
                    )
                    for node in root.iter()
                ):
                    raise WorkbenchError(
                        "Requested UI condition is no longer present: "
                        + encode(condition)
                    )
            return file_hash(path)
        finally:
            try:
                self.adb("shell", "rm", "-f", remote, cleanup=True)
            except BaseException as error:
                self.runner.cleanup_errors.append(str(error))
                raise

    def screenshot(self, name="screenshot.png"):
        before = self.observe()
        expected_boot = self.runner.spec.get("expected_boot")
        if expected_boot and before["boot_id"] != expected_boot:
            raise WorkbenchError("Device boot identity changed")
        requested = self.runner.spec.get("app")
        if requested and before["app"] != requested:
            raise WorkbenchError("Requested app is no longer foreground")
        expected = self.runner.spec.get("activity")
        if expected and before["activity"] != expected:
            raise WorkbenchError("Requested activity is no longer foreground")
        if (
            self.hooks
            and not self.runner.spec.get("accept_hooks", False)
            and self.runner.spec["operation"] == "device.screenshot"
        ):
            raise WorkbenchError("Observation does not accept active hooks")
        ui_before = self.validate_ui(name + "-before")
        data = self.adb("exec-out", "screencap", "-p", binary=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise WorkbenchError("Device did not return PNG data")
        path = self.runner.directory / name
        path.write_bytes(data)
        ui_after = self.validate_ui(name + "-after")
        after = self.observe()
        atomic_json(
            path.with_suffix(".json"),
            {
                "before": before,
                "after": after,
                "ui_before_sha256": ui_before,
                "ui_after_sha256": ui_after,
                "ui_expect": self.runner.spec.get("ui_expect", []),
                "page_validation": "activity/process and any requested UI conditions before/after; screenshot pixels still require review",
            },
        )
        if any(before[k] != after[k] for k in ("boot_id", "activity", "pid")):
            raise WorkbenchError(
                "Scene changed during screenshot; evidence retained without success"
            )

    def attach(self, step):
        import frida

        if self.frida_device is None:
            self.forward = self.adb(
                "forward", "tcp:0", f"tcp:{int(step.get('port', 27042))}"
            )
            self.frida_device = self.runner.frida_call(
                "add_remote_device",
                lambda: frida.get_device_manager().add_remote_device(
                    "127.0.0.1:" + self.forward
                ),
            )
        self.runner.authorize()
        pid = int(self.adb("shell", "pidof", "-s", step["package"]))
        session = self.runner.frida_call(
            "attach", lambda: self.frida_device.attach(pid)
        )
        owned = {
            "package": step["package"],
            "pid": pid,
            "session": session,
            "script": None,
        }
        self.hooks.append(owned)
        source = Path(step["script"]).read_text()
        script = self.runner.frida_call(
            "create_script",
            lambda: session.create_script(source, runtime=step.get("runtime", "qjs")),
        )
        owned["script"] = script
        script.on("message", lambda message, data: self.runner.log_hook(message, data))
        self.runner.authorize()
        self.runner.frida_call("load", script.load)
        self.runner.call(
            "event", kind="hook_attached", data={"package": step["package"], "pid": pid}
        )

    def unload(self):
        errors = []
        for owned in reversed(self.hooks):
            for obj, method in (
                (owned["script"], "unload"),
                (owned["session"], "detach"),
            ):
                if obj:
                    try:
                        self.runner.authorize(cleanup=True)
                        self.runner.frida_call(
                            method, getattr(obj, method), cleanup=True, timeout=5
                        )
                    except Exception as error:
                        errors.append(str(error))
        self.hooks = []
        if self.frida_device:
            try:
                import frida

                self.runner.frida_call(
                    "remove_remote_device",
                    lambda: frida.get_device_manager().remove_remote_device(
                        "127.0.0.1:" + self.forward
                    ),
                    cleanup=True,
                    timeout=5,
                )
            except Exception as error:
                errors.append(str(error))
            self.frida_device = None
        if self.forward:
            try:
                self.adb("forward", "--remove", "tcp:" + self.forward, cleanup=True)
            except Exception as error:
                errors.append(str(error))
            self.forward = None
        if errors:
            raise WorkbenchError("Hook cleanup failed: " + "; ".join(errors))


class Runner:
    def __init__(self, grant, device=None):
        self.grant = grant
        self.spec = grant["spec"]
        self.directory = Path(self.spec["directory"])
        self.started = time.monotonic()
        self.cleanup_errors = []
        self.uncertain = False
        self.active = None
        self.device = device or Device(self)
        self.lock_fd = None
        self.signal_cancel = False
        self.log_failed = False
        self.recovered_resources = []
        self.reconciled_jobs = []

    def call(self, method, **values):
        return rpc(
            self.grant["state"],
            "worker." + method,
            {"id": self.grant["job"], **values},
            self.grant["token"],
            timeout=125 if method == "checkpoint" else 10,
        )

    def authorize(self, cleanup=False):
        if self.signal_cancel and not cleanup:
            raise Cancelled("Worker stopping")
        reply = self.call("authorize", cleanup=cleanup)
        if reply.get("release_device") and self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
            self.call("release_device_ack")
            self.spec["device_released"] = True
        if reply.get("cancel"):
            raise Cancelled("Task cancelled")
        if reply.get("expired") or (
            not cleanup and time.monotonic() - self.started > self.spec["timeout"]
        ):
            raise BudgetExceeded("Task execution budget exceeded")
        if self.log_failed and not cleanup:
            raise WorkbenchError("Evidence writing failed")

    def frida_call(self, name, function, cleanup=False, timeout=15):
        import frida

        self.authorize(cleanup)
        done = threading.Event()
        errors = []
        cancellable = frida.Cancellable()

        def monitor():
            end = time.monotonic() + timeout
            while not done.wait(0.1):
                try:
                    self.authorize(cleanup)
                    if time.monotonic() >= end:
                        raise BudgetExceeded("Frida " + name + " timed out")
                except BaseException as exc:
                    errors.append(exc)
                    cancellable.cancel()
                    return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        self.call("event", kind="frida_prepared", data={"operation": name})
        try:
            with cancellable:
                value = function()
            if errors:
                raise errors[0]
            self.call("event", kind="frida_confirmed", data={"operation": name})
            return value
        except BaseException:
            self.uncertain = True
            if errors:
                raise errors[0]
            raise
        finally:
            done.set()
            watcher.join(timeout=1)

    def log_hook(self, message, data):
        try:
            path = self.directory / "hook.jsonl"
            if path.exists() and path.stat().st_size > 32 * 1024 * 1024:
                raise WorkbenchError("Hook log budget exceeded")
            with path.open("a") as stream:
                stream.write(
                    encode(
                        {
                            "time": time.time(),
                            "message": message,
                            "data_hex": data.hex() if data else None,
                        }
                    )
                    + "\n"
                )
        except Exception:
            self.log_failed = True

    def command(self, argv, timeout=30, cleanup=False, env=None, cwd=None):
        self.authorize(cleanup)
        operation = {"argv": argv, "prepared_at": time.time(), "confirmed": False}
        self.call("event", kind="command_prepared", data=operation)
        cmd_id = str(time.time_ns())
        out = self.directory / ("command-" + cmd_id + ".stdout")
        err = self.directory / ("command-" + cmd_id + ".stderr")
        with out.open("wb") as stdout, err.open("wb") as stderr:
            proc = subprocess.Popen(
                argv,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
            self.active = proc
            atomic_json(
                self.directory / "active-process.json",
                {"identity": process_identity(proc.pid), "argv": argv},
            )
            started = time.monotonic()
            try:
                while proc.poll() is None:
                    if time.monotonic() - started > timeout:
                        self.uncertain = True
                        raise BudgetExceeded(
                            "Command timeout; outcome requires reconciliation"
                        )
                    self.authorize(cleanup)
                    if out.stat().st_size + err.stat().st_size > 32 * 1024 * 1024:
                        raise WorkbenchError("Command output budget exceeded")
                    time.sleep(0.08)
                code = proc.wait()
            except BaseException:
                self.uncertain = True
                stop_group(proc)
                raise
            finally:
                self.active = None
        self.call(
            "event",
            kind="command_confirmed",
            data={
                "argv": argv,
                "exit_code": code,
                "stdout": out.name,
                "stderr": err.name,
            },
        )
        if code:
            raise WorkbenchError(
                f"Command failed ({code}): {err.read_text(errors='replace')[-1000:]}"
            )
        return out.read_bytes()

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.authorize()
            time.sleep(min(0.1, max(0, end - time.monotonic())))

    def checkpoint(self, step, observation):
        self.device.validate_ui("checkpoint-before")
        total = 0
        end = time.monotonic() + step.get("seconds", 0)
        budget = step.get("budget", 10)
        while True:
            self.authorize()
            if total < step.get("max_insertions", 2) and budget > 0:
                before = time.monotonic()
                reply = self.call(
                    "checkpoint",
                    checkpoint=step,
                    observation=observation,
                    remaining_budget=budget,
                )
                child = reply.get("insert")
                if child:
                    self.call(
                        "event", kind="insertion_begin", data={"child": child["job"]}
                    )
                    nested = Runner(child, self.device)
                    old_runner = self.device.runner
                    self.device.runner = nested
                    try:
                        # Parent process and device lock stay alive throughout this call.
                        result = nested.execute(borrowed=True)
                    finally:
                        self.device.runner = old_runner
                    total += 1
                    if not result.get("cleanup_ok"):
                        self.uncertain = True
                        raise WorkbenchError("Inserted task did not clean up")
                self.call("resumed")
                if self.spec["operation"] != "test.simulate":
                    try:
                        self.device.validate_ui("checkpoint-after")
                    except BaseException:
                        self.uncertain = True
                        raise
                    fresh = self.device.observe()
                    if any(
                        fresh.get(k) != observation.get(k)
                        for k in ("boot_id", "activity", "pid", "hooks")
                    ):
                        self.uncertain = True
                        raise WorkbenchError("Parent continuation conditions changed")
                budget -= time.monotonic() - before
            if time.monotonic() >= end:
                return
            self.sleep(min(0.1, end - time.monotonic()))

    def scene(self):
        spec = self.spec
        if spec["operation"] == "device.recover":
            # Reinspection does not claim to undo unknown remote mutations.
            observation = self.device.observe()
            previous = []
            for identifier in spec.get("recovery_targets", []):
                folder = Path(self.grant["state"]) / "jobs" / identifier
                normalized = folder / "normalized.json"
                if not normalized.is_file() or folder == self.directory:
                    continue
                old = json.loads(normalized.read_text())
                if old.get("device") != spec["device"]:
                    continue
                residual = folder / "residual-processes.json"
                if residual.is_file() and any(
                    same_process(x) for x in json.loads(residual.read_text())
                ):
                    raise WorkbenchError("Old task descendants are still alive")
                active = folder / "active-process.json"
                if active.is_file() and same_process(
                    json.loads(active.read_text()).get("identity")
                ):
                    raise WorkbenchError("An old command process is still alive")
                result = folder / "result.json"
                outcome = json.loads(result.read_text()) if result.is_file() else {}
                if not outcome.get("cleanup_ok") and not old.get("readonly", False):
                    from .results import verify_native_probe_recovery

                    if old[
                        "operation"
                    ] == "frida.probe_native" and verify_native_probe_recovery(
                        self, folder, old
                    ):
                        self.recovered_resources.extend(old.get("resources", []))
                        self.reconciled_jobs.append(folder.name)
                    else:
                        previous.append(folder.name)
            if previous:
                raise WorkbenchError(
                    "Uncertain mutating operations require their registered recovery procedure: "
                    + ",".join(previous)
                )
            atomic_json(self.directory / "observation.json", observation)
            return
        if spec["operation"] == "device.info":
            atomic_json(self.directory / "device-info.json", self.device.info())
            return
        if spec.get("scene") and spec.get("expected_boot"):
            if self.device.observe()["boot_id"] != spec["expected_boot"]:
                raise WorkbenchError("Bound scene boot identity expired")
        for index, step in enumerate(spec["steps"]):
            self.authorize()
            action = step["action"]
            if spec["operation"] == "test.simulate":
                self.call(
                    "event",
                    kind="simulated_step",
                    data={"action": action, "index": index},
                )
                if action == "checkpoint":
                    self.checkpoint(
                        step, {"app": spec.get("app"), "hooks": spec.get("hooks", [])}
                    )
                elif action == "fail":
                    raise WorkbenchError("Simulated failure")
                elif action == "crash":
                    os._exit(79)
                else:
                    self.sleep(step.get("seconds", 0))
                continue
            if action == "observe":
                atomic_json(
                    self.directory / f"observation-{index}.json", self.device.observe()
                )
            elif action == "screenshot":
                self.device.screenshot(f"screenshot-{index}.png")
            elif action == "ui_dump":
                remote = f"/data/local/tmp/awb-{self.grant['job']}.xml"
                try:
                    self.device.adb("shell", "uiautomator", "dump", remote)
                    (self.directory / f"ui-{index}.xml").write_bytes(
                        self.device.adb("exec-out", "cat", remote, binary=True)
                    )
                finally:
                    self.device.adb("shell", "rm", "-f", remote, cleanup=True)
            elif action == "logcat":
                buffer = step.get("buffer", "main")
                if step.get("clear"):
                    self.device.adb("shell", "logcat", "-b", buffer, "-c")
                arguments = [
                    "logcat", "-d", "-b", buffer, "-t", str(step["lines"])
                ]
                if step.get("level"):
                    arguments.append("*:" + step["level"])
                (self.directory / f"logcat-{index}.log").write_text(
                    self.device.adb(
                        "shell", *arguments, timeout=min(60, max(5, step["lines"] / 100))
                    ),
                    encoding="utf-8",
                    errors="replace",
                )
            elif action == "launch":
                self.device.adb("shell", "am", "start", "-W", "-n", step["component"])
            elif action == "tap":
                self.device.adb("shell", "input", "tap", step["x"], step["y"])
            elif action == "swipe":
                self.device.adb(
                    "shell",
                    "input",
                    "swipe",
                    *[step[x] for x in ("x1", "y1", "x2", "y2", "duration")],
                )
            elif action == "input":
                self.device.adb("shell", "input text " + shlex.quote(step["text"]))
            elif action == "keyevent":
                self.device.adb("shell", "input", "keyevent", step["code"])
            elif action == "wait":
                self.sleep(step.get("seconds", 0))
            elif action == "checkpoint":
                self.checkpoint(step, self.device.observe())
            elif action == "hook_attach":
                self.device.attach(step)
            elif action == "hook_unload":
                self.device.unload()

    def legacy(self):
        entry = self.spec["entry"]
        if self.spec.get("recovery"):
            # Only a reviewed, registered adapter may repair the named operations.
            for identifier in self.spec.get("recovery_targets", []):
                folder = Path(self.grant["state"]) / "jobs" / identifier
                old = json.loads((folder / "normalized.json").read_text())
                if old.get("operation") not in entry["recovery_for"]:
                    raise WorkbenchError("Recovery adapter does not cover: " + old["operation"])
                for name in ("active-process.json", "residual-processes.json"):
                    path = folder / name
                    if path.is_file():
                        data = json.loads(path.read_text())
                        identities = data if isinstance(data, list) else [data.get("identity")]
                        if any(
                            same_process(x)
                            for x in identities
                            if x and not is_adb_server(x)
                        ):
                            raise WorkbenchError("Old adapter process is still alive")
        # Snapshot Python code while preserving original __file__-relative project defaults.
        for relative, expected in self.spec.get("source_manifest", {}).items():
            original = Path(self.spec["source"]) / relative
            if not original.is_file() or file_hash(original) != expected:
                raise WorkbenchError(
                    "Source changed after submission; submit a new task"
                )
        for source, manifest in self.spec.get("nested_source_manifests", {}).items():
            for relative, expected in manifest.items():
                original = Path(source) / relative
                if not original.is_file() or file_hash(original) != expected:
                    raise WorkbenchError("Nested source changed after submission")
        for path, expected in self.spec.get("input_fingerprints", {}).items():
            if not Path(path).is_file() or file_hash(path) != expected:
                raise WorkbenchError("Input changed after submission: " + path)
        env = {**os.environ, **self.spec.get("env", {})}
        env["AWB_INTERNAL_GRANT"] = str(self.directory / "grant.json")
        env["PYTHONPATH"] = (
            str(Path(__file__).resolve().parents[1])
            + os.pathsep
            + env.get("PYTHONPATH", "")
        )
        argv = [
            self.spec["python"],
            "-m",
            "workbench.legacy_runner",
            str(self.directory / "normalized.json"),
        ]
        if self.spec["script"].endswith(".sh"):
            argv = ["bash", self.spec["script"], *self.spec.get("args", [])]
        self.authorize()
        out = self.directory / "stdout.log"
        err = self.directory / "stderr.log"
        with out.open("wb") as stdout, err.open("wb") as stderr:
            proc = subprocess.Popen(
                argv,
                cwd=self.spec["project"],
                env=env,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.active = proc
            atomic_json(
                self.directory / "active-process.json",
                {"identity": process_identity(proc.pid), "argv": argv},
            )
            try:
                while proc.poll() is None:
                    self.authorize()
                    if out.stat().st_size + err.stat().st_size > 32 * 1024 * 1024:
                        raise WorkbenchError("Task log budget exceeded")
                    time.sleep(0.1)
                code = proc.wait()
            except BaseException:
                self.uncertain = not self.spec["readonly"]
                # SIGINT gives Python adapters their existing finally/rollback path.
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                    proc.wait(timeout=5)
                except Exception:
                    stop_group(proc)
                raise
            finally:
                self.active = None
        cleanup_verified = False
        result_file = entry.get("result_file")
        if result_file:
            outputs = self.spec.get("outputs", [])
            record = (
                Path(outputs[0]) / result_file
                if outputs and result_file != "."
                else (Path(outputs[0]) if outputs else None)
            )
            if record is None or not record.is_file():
                self.uncertain = bool(self.spec.get("device")) or bool(
                    entry.get("mutates_environment")
                )
                raise WorkbenchError("Adapter result record is missing")
            try:
                data = json.loads(record.read_text())
                if not isinstance(data, dict):
                    raise ValueError("Result must be an object")
            except (ValueError, OSError):
                self.uncertain = True
                raise WorkbenchError("Adapter result record cannot be verified")
            from .results import confirmed_legacy_cleanup

            cleanup_verified = confirmed_legacy_cleanup(self.spec, data)
            errors = data.get("cleanup_errors") or []
            if not isinstance(errors, list):
                errors = [str(errors)]
            if data.get("cleanup_error"):
                errors.append(str(data["cleanup_error"]))
            if data.get("rollback_error"):
                errors.append(str(data["rollback_error"]))
            if errors:
                self.cleanup_errors.extend(errors)
                self.uncertain = True
            if code == 0 and data.get("pass") is not True:
                raise WorkbenchError(
                    "Adapter exited successfully without a passing result record"
                )
        if code == 0:
            if self.spec.get("recovery"):
                for identifier in self.spec.get("recovery_targets", []):
                    folder = Path(self.grant["state"]) / "jobs" / identifier
                    old = json.loads((folder / "normalized.json").read_text())
                    self.reconciled_jobs.append(identifier)
                    self.recovered_resources.extend(old.get("resources", []))
            return "succeeded", code
        if code in entry.get("partial_codes", []):
            output = self.spec.get("outputs", [])
            summary = Path(output[0]) / "summary.json" if output else None
            if summary and summary.is_file():
                return "partial", code
        if (
            self.spec.get("device")
            and not self.spec["readonly"]
            and not cleanup_verified
        ):
            self.uncertain = True
        return "failed", code

    def publish(self, result):
        files = []
        for path in sorted(self.directory.iterdir()):
            if path.is_file() and path.name not in {
                "grant.json",
                "normalized.json",
                "result.json",
                "artifacts.json",
                "worker.log",
            }:
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                files.append(
                    {
                        "name": path.name,
                        "sha256": file_hash(path),
                        "bytes": path.stat().st_size,
                    }
                )
        external = []
        for output in self.spec.get("outputs", []):
            root = Path(output)
            record = {"path": output, "exists": root.exists(), "files": []}
            paths = sorted(root.rglob("*")) if root.is_dir() else [root]
            for path in paths:
                if path.is_symlink():
                    record["files"].append(
                        {"path": str(path), "quality": "symlink_not_published"}
                    )
                elif path.is_file():
                    before = path.stat()
                    hashed = file_hash(path)
                    after = path.stat()
                    if (before.st_ino, before.st_mtime_ns, before.st_size) != (
                        after.st_ino,
                        after.st_mtime_ns,
                        after.st_size,
                    ):
                        raise WorkbenchError(
                            "Output changed during publication: " + str(path)
                        )
                    record["files"].append(
                        {"path": str(path), "sha256": hashed, "bytes": after.st_size}
                    )
            external.append(record)
        atomic_json(
            self.directory / "artifacts.json",
            {
                "job": self.grant["job"],
                "operation": self.spec["operation"],
                "device": self.spec.get("device"),
                "created_at": time.time(),
                "quality": result["state"],
                "files": files,
                "external_outputs": external,
            },
        )
        atomic_json(self.directory / "result.json", result)

    def execute(self, borrowed=False):
        outcome = "failed"
        code = None
        error = None
        try:
            if not borrowed:
                if self.spec.get("device"):
                    self.lock_fd = lock_file(
                        Path(self.grant["state"])
                        / "devices"
                        / (self.spec["device"].encode().hex() + ".lock")
                    )
                self.call("ready", pid=os.getpid())
            self.authorize()
            for path, expected in self.spec["snapshot_hashes"].items():
                if file_hash(path) != expected:
                    raise WorkbenchError("Pinned input modified")
            if self.spec["operation"] == "host.lease":
                from .leases import execute_lease

                execute_lease(self)
                outcome = "succeeded"
            elif "entry" in self.spec:
                outcome, code = self.legacy()
            else:
                self.scene()
                outcome = "succeeded"
        except Cancelled as exc:
            outcome = "cancelled"
            error = str(exc)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if not borrowed:
                try:
                    self.device.unload()
                except BaseException as exc:
                    self.cleanup_errors.append(str(exc))
        remaining = descendants(os.getpid()) if not borrowed else []
        if remaining:
            self.uncertain = True
            atomic_json(self.directory / "residual-processes.json", remaining)
            self.cleanup_errors.append("Task descendants still alive")
        cleanup = not self.cleanup_errors and not self.uncertain and not self.log_failed
        result = {
            "state": outcome if cleanup else "failed",
            "functional_result": outcome,
            "exit_code": code,
            "cleanup_ok": cleanup,
            "cleanup_errors": self.cleanup_errors,
            "reason": error,
            "uncertain": self.uncertain,
            "finished_at": time.time(),
            "reconciled_jobs": self.reconciled_jobs,
            "recovered_resources": self.recovered_resources,
        }
        try:
            self.publish(result)
            self.call("finished")
        except BaseException:
            # No success receipt if durable evidence/IPC failed. Restart reconciliation reads result.json.
            traceback.print_exc(file=sys.stderr)
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
        return result


def main():
    become_subreaper()
    grant = json.loads(Path(sys.argv[1]).read_text())
    runner = Runner(grant)

    def stop(signum, frame):
        runner.signal_cancel = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    result = runner.execute()
    raise SystemExit(0 if result["state"] in ("succeeded", "partial") else 1)


if __name__ == "__main__":
    main()
