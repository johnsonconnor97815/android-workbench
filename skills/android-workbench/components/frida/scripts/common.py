"""Shared process, evidence and device-ownership helpers (Python 3.10+)."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time


class Blocked(RuntimeError):
    """A missing prerequisite prevents a compatibility conclusion."""


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(argv, *, cwd=None, env=None, timeout=60, check=True):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{shlex.join(list(map(str, argv)))}: {result.stderr.strip() or result.stdout.strip()}')
    return result


@contextmanager
def device_lock(serial):
    directory = Path(tempfile.gettempdir()) / ('frida-modified-locks-' + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256(serial.encode()).hexdigest()
    with (directory / key).open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Blocked('Another frida-modified task owns this device') from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def deadline(seconds=25):
    import frida
    cancellable = frida.Cancellable()
    timer = threading.Timer(seconds, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        with cancellable:
            yield
    finally:
        timer.cancel()


class Adb:
    def __init__(self, serial, executable='adb', root=False):
        self.serial, self.executable, self.root = serial, executable, root
        self.use_su = None

    def call(self, *argv, check=True, timeout=25):
        return run([self.executable, '-s', self.serial, *argv], check=check, timeout=timeout).stdout.strip()

    def shell(self, *argv, root=None):
        return self.shell_text(shlex.join(list(map(str, argv))), root=root)

    def shell_text(self, command, root=None):
        root = self.root if root is None else root
        if root:
            if self.use_su is None:
                self.use_su = self.call('shell', 'id -u') != '0'
            if self.use_su:
                command = 'su -c ' + shlex.quote(command)
        return self.call('shell', command)

    def exists(self, path):
        return self.shell_text('if test -e ' + shlex.quote(str(path)) + '; then echo present; else echo absent; fi') == 'present'

    def identity(self, pid):
        if not self.exists(f'/proc/{pid}'):
            return None
        try:
            stat = self.shell('cat', f'/proc/{pid}/stat')
            if stat.rsplit(')', 1)[1].split()[0] == 'Z':
                return None
            status = self.shell('cat', f'/proc/{pid}/status')
            return {
                'pid': pid, 'starttime': stat.rsplit(')', 1)[1].split()[19],
                'exe': self.shell('readlink', f'/proc/{pid}/exe'),
                'uid': int(next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1]),
            }
        except RuntimeError:
            # A process may exit between exists(), stat, status and readlink.
            # A successful second ADB check distinguishes that from lost transport.
            if not self.exists(f'/proc/{pid}'):
                return None
            raise

    def launch(self, argv, log='/dev/null', root=None):
        command = shlex.join(['/system/bin/nohup', *map(str, argv)])
        command += ' >' + shlex.quote(log) + ' 2>&1 </dev/null & echo $!'
        return int(self.shell_text(command, root=root))

    def stop_owned(self, identity, signal='TERM'):
        if identity is None:
            return
        pid = identity['pid']
        current = self.identity(pid)
        if current is None or current['starttime'] != identity['starttime']:
            return
        if current['exe'].removesuffix(' (deleted)') != identity['exe'].removesuffix(' (deleted)'):
            raise RuntimeError('PID executable changed; refusing to signal it')
        try:
            self.shell('kill', '-' + signal, str(pid))
        except RuntimeError:
            if self.identity(pid) is None:
                return
            raise
        for _ in range(50):
            current = self.identity(pid)
            if current is None or current['starttime'] != identity['starttime']:
                return
            time.sleep(0.1)
        raise RuntimeError('Owned process did not terminate')

    def file_hash(self, path):
        value = self.shell('sha256sum', str(path)).split()[0]
        if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise RuntimeError('Invalid device SHA-256 response')
        return value


def finish(record, cleanup_errors):
    record['cleanup_errors'] = cleanup_errors
    record['cleanup_status'] = 'failed' if cleanup_errors else 'passed'
    record['pass'] = bool(record.get('functional_pass')) and not cleanup_errors and not record.get('error')
    record['status'] = 'passed' if record['pass'] else ('blocked' if record.get('blocked') else 'failed')
    record['completed_utc'] = utc()
    return 0 if record['pass'] else (2 if record.get('blocked') else 1)


def cleanup(actions):
    errors = []
    for label, action in actions:
        try:
            action()
        except BaseException as error:
            errors.append(f'{label}: {type(error).__name__}: {error}')
    return errors


def select_device(serial=None, executable='adb'):
    devices = []
    for line in run([executable, 'devices']).stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == 'device':
            devices.append(fields[0])
    if serial is not None:
        if serial not in devices:
            raise Blocked('Specified device is not online: ' + serial)
        return serial
    if len(devices) != 1:
        raise Blocked('Specify --serial: expected exactly one online Android device')
    return devices[0]
