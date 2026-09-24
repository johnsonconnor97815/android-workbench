from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import uuid

TERMINAL = {"succeeded", "partial", "failed", "cancelled", "expired"}
MAX_MESSAGE = 2 * 1024 * 1024


class WorkbenchError(RuntimeError):
    pass


def state_dir(value=None):
    return (
        Path(
            value
            or os.environ.get("ANDROID_WORKBENCH_STATE")
            or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
            / "android-workbench"
        )
        .expanduser()
        .resolve()
    )


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            stream.write(encode(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def lock_file(path, shared=False, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(
            fd,
            (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            | (0 if blocking else fcntl.LOCK_NB),
        )
    except BaseException:
        os.close(fd)
        raise
    return fd


def process_identity(pid):
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {
            "pid": int(pid),
            "start": stat[19],
            "group": int(stat[2]),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, ValueError):
        return None


def same_process(identity):
    return bool(identity) and process_identity(identity["pid"]) == identity


def is_adb_server(identity):
    if not identity:
        return False
    try:
        argv = (
            Path(f"/proc/{int(identity['pid'])}/cmdline")
            .read_bytes()
            .split(b"\0")
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return b"adb" in argv and b"fork-server" in argv and b"server" in argv


def stop_group(proc, grace=2):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=grace)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=grace)


def rpc(directory, method, params=None, token=None, timeout=10):
    request = {"protocol": 1, "method": method, "params": params or {}, "token": token}
    raw = (encode(request) + "\n").encode()
    if len(raw) > MAX_MESSAGE:
        raise WorkbenchError("Request exceeds message limit")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        try:
            client.connect(str(Path(directory) / "service.sock"))
            client.sendall(raw)
            with client.makefile("rb") as stream:
                data = stream.readline(MAX_MESSAGE + 1)
        except OSError as error:
            raise WorkbenchError(f"Scheduler unavailable: {error}") from error
    if not data or len(data) > MAX_MESSAGE:
        raise WorkbenchError("Invalid scheduler response")
    result = json.loads(data)
    if "error" in result:
        raise WorkbenchError(result["error"])
    return result["result"]


def path_resource(path):
    return "path:" + str(Path(path).expanduser().resolve())


def overlap(a, b):
    if a == b:
        return True
    if a.startswith("path:") and b.startswith("path:"):
        x, y = Path(a[5:]), Path(b[5:])
        return x.is_relative_to(y) or y.is_relative_to(x)
    return False


def conflict(a, b):
    return any(
        overlap(x["key"], y["key"]) and ("write" in (x["mode"], y["mode"]))
        for x in a
        for y in b
    )


def resource(key, mode="write"):
    return {"key": key, "mode": mode}


def descendants(pid):
    """Identify only processes descended from this known supervisor."""
    processes = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                processes[int(path.name)] = int(fields[1])
        except (OSError, ValueError):
            continue
    owned = {pid}
    while True:
        extra = {
            child for child, parent in processes.items() if parent in owned
        } - owned
        if not extra:
            break
        owned.update(extra)
    remaining = []
    for child in owned - {pid}:
        identity = process_identity(child)
        if identity and not is_adb_server(identity):
            remaining.append(identity)
    return remaining


def become_subreaper():
    # Orphaned grandchildren stay attached to the worker rather than escaping its cleanup check.
    import ctypes

    if (
        ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0
    ):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot supervise descendant processes")
