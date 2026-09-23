#!/usr/bin/env python3
"""Replace an explicitly named standalone Android server, verify injection, or restore it."""
# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == '__main__':
    import sys as _awb_sys
    from pathlib import Path as _AwbPath
    _awb_project = None
    for _awb_origin in (_AwbPath.cwd(),):
        for _awb_root in (_awb_origin, *_awb_origin.parents):
            if (_awb_root / 'workbench.project.json').is_file():
                _awb_project = _awb_root
                break
        if _awb_project is not None:
            break
    if _awb_project is not None:
        import json as _awb_json
        _awb_config = _awb_json.loads((_awb_project / 'workbench.project.json').read_text())
        _awb_runtime = (_awb_project / _awb_config.get('workbench_root', 'android-workbench')).resolve()
        if not (_awb_runtime / 'workbench/bridge.py').is_file():
            raise SystemExit('Android Workbench runtime missing; managed entry cannot run directly')
        _awb_sys.path.insert(0, str(_awb_runtime))
        from workbench.bridge import entry as _awb_entry
        _awb_entry(__file__)
# END ANDROID WORKBENCH ENTRY

import argparse
import json
from pathlib import Path, PurePosixPath
import shlex
import sys
import time
import uuid
from common import Adb, Blocked, cleanup, device_lock, run, select_device, sha256, utc, write_json

PROBE = Path(__file__).with_name('probe_native.py')


def probe_python(python, args, version, port, output):
    # Called while the parent holds the device lock. Use the importable probe function
    # in this child, instead of invoking its lock-taking CLI entrypoint.
    config = {'serial': args.serial, 'version': version, 'record': str(output), 'adb': args.adb,
              'endpoint': None, 'server_port': port, 'arch': args.arch, 'runtime': 'qjs',
              'target': '/system/bin/sleep', 'spawn': False, 'no_root': False,
              'gadget': None, 'harness': None, 'gadget_port': 27165}
    program = ('import json,sys; from pathlib import Path; from types import SimpleNamespace; '
               'sys.path.insert(0,sys.argv[1]); from probe_native import probe; '
               'a=json.loads(sys.argv[2]); a["record"]=Path(a["record"]); '
               'code,record=probe(SimpleNamespace(**a)); sys.exit(code)')
    result = run([python, '-c', program, PROBE.parent, json.dumps(config)], timeout=90, check=False)
    if not output.is_file():
        raise RuntimeError('Probe produced no result: ' + (result.stderr or result.stdout))
    record = json.loads(output.read_text())
    if result.returncode or record.get('pass') is not True:
        raise RuntimeError('Injection/cleanup validation failed: ' + str(output) + ': ' + str(record.get('error') or record.get('cleanup_errors')))
    return {'record': str(output), 'sha256': sha256(output), 'pass': True}


def find_servers(adb, remote):
    command = ('for p in /proc/[0-9]*; do e=$(readlink "$p/exe" 2>/dev/null) || continue; '
               'if [ "$e" = ' + shlex.quote(remote) + ' ]; then echo "${p##*/}"; fi; done')
    return [int(value) for value in adb.shell_text(command).split()]


def launch(adb, argv, log, uid, cwd='/'):
    command = 'cd ' + shlex.quote(cwd) + ' || exit 1\n' + shlex.join(['/system/bin/nohup', *argv])
    command += ' >' + shlex.quote(log) + ' 2>&1 </dev/null & echo $!'
    return int(adb.shell_text(command, root=uid == 0))


def verify(adb, pid, digest, uid, argv):
    identity = adb.identity(pid)
    if identity is None or identity['uid'] != uid:
        raise RuntimeError('Server is absent or its UID changed')
    if adb.file_hash(f'/proc/{pid}/exe') != digest:
        raise RuntimeError('Running server hash mismatch')
    actual = adb.shell('cat', f'/proc/{pid}/cmdline').rstrip('\0').split('\0')
    if actual != argv:
        raise RuntimeError('Server launch arguments differ from expected arguments')
    return {**identity, 'argv': actual, 'sha256': digest,
            'context': adb.shell('cat', f'/proc/{pid}/attr/current').rstrip('\0')}


def deploy(args):
    adb = Adb(args.serial, args.adb, root=True)
    remote = args.remote
    token = uuid.uuid4().hex
    backup = '/data/local/tmp/frida-skill-deploy-' + token
    staged = remote + '.stage-' + token
    expected = sha256(args.binary)
    record = {'schema': 1, 'utc': utc(), 'device': args.serial, 'remote': remote,
              'binary_sha256': expected, 'version': args.version, 'phase': 'preflight', 'pass': False,
              'backup': backup, 'staged': staged, 'rollback': 'not_needed',
              'limitations': ['Root server startup may change SELinux policy; binary rollback does not restore that policy.']}
    previous = None
    stopped = replaced = backup_created = staged_attempted = False
    had_binary = False
    old_hash = old_version = None
    old_args = [remote]
    old_cwd = '/'
    old_uid = None
    def save(phase):
        record['phase'] = phase
        write_json(args.record, record)
    def evidence(name):
        return args.record.with_name(args.record.stem + '-' + name + '.json')
    try:
        if adb.shell('id', '-u') != '0':
            raise Blocked('Standalone server deployment requires root')
        if adb.shell_text('if test -L ' + shlex.quote(remote) + '; then echo symlink; fi'):
            raise Blocked('Destination is a symlink; choose a regular standalone server path')
        pids = find_servers(adb, remote)
        if len(pids) > 1:
            raise Blocked('Multiple servers use this path; resolve ownership before deployment')
        had_binary = adb.exists(remote)
        if had_binary:
            old_hash = adb.file_hash(remote)
            record['previous_file_sha256'] = old_hash
        if pids:
            previous = adb.identity(pids[0])
            if previous is None or not had_binary:
                raise Blocked('Cannot recover the current running artifact')
            old_uid = previous['uid']
            if old_uid == 2000 and adb.call('shell', 'id -u') == '0':
                raise Blocked('Cannot recreate a shell UID from a root adbd without a dedicated launcher')
            if old_uid not in (0, 2000):
                raise Blocked('Automatic restoration only supports UID 0 or UID 2000 standalone processes')
            old_args = adb.shell('cat', f'/proc/{pids[0]}/cmdline').rstrip('\0').split('\0')
            if not old_args or old_args[0] != remote or any(a.startswith(('--token', '--certificate')) for a in old_args):
                raise Blocked('Authenticated or nonstandard launch configuration requires a dedicated deployment adapter')
            old_cwd = adb.shell('readlink', f'/proc/{pids[0]}/cwd')
            previous = verify(adb, pids[0], old_hash, old_uid, old_args)
            old_version = adb.shell(remote, '--version')
            record['previous'] = {**previous, 'cwd': old_cwd, 'version': old_version}
            record['previous_functional'] = probe_python(args.previous_python or args.python, args,
                                                       old_version, args.previous_port or args.port, evidence('before'))
        record['selinux_before'] = adb.shell('getenforce')
        adb.shell('mkdir', backup)
        backup_created = True
        adb.shell('chmod', '755', backup)
        if had_binary:
            adb.shell('cp', '-p', remote, backup + '/previous-server')
            if adb.file_hash(backup + '/previous-server') != old_hash:
                raise RuntimeError('Backup hash mismatch')
        if old_uid == 2000:
            adb.shell('touch', backup + '/rollback.log')
            adb.shell('chown', '2000:2000', backup + '/rollback.log')
        staged_attempted = True
        save('uploading')
        adb.call('push', str(args.binary), staged)
        adb.shell('chmod', '755', staged)
        if adb.file_hash(staged) != expected:
            raise RuntimeError('Uploaded binary hash mismatch')
        if adb.shell(staged, '--version') != args.version:
            raise Blocked('Binary version does not match the selected Frida version')
        new_args = [remote, *args.server_arg] if args.server_arg else old_args
        record['new_argv'] = new_args
        if previous:
            stopped = True
            save('stopping_previous')
            adb.stop_owned(previous)
        replaced = True
        save('replacing_binary')
        adb.shell('mv', staged, remote)
        save('starting_candidate')
        pid = launch(adb, new_args, backup + '/start.log', 0)
        record['candidate_pid'] = pid
        save('verifying_candidate')
        time.sleep(0.5)
        adb.identity(pid)
        record['candidate'] = verify(adb, pid, expected, 0, new_args)
        record['functional'] = probe_python(args.python, args, args.version, args.port, evidence('candidate'))
        record['pass'] = True
        record['phase'] = 'deployed'
    except BaseException as error:
        record['error'] = f'{type(error).__name__}: {error}'
        record['blocked'] = isinstance(error, Blocked)
        if stopped or replaced:
            record['rollback'] = 'in_progress'
            save('restoring')
            try:
                # A failed launch/ADB call may have started a process before returning.
                for pid in find_servers(adb, remote):
                    identity = adb.identity(pid)
                    if previous and identity and identity['starttime'] == previous['starttime'] and pid == previous['pid']:
                        continue
                    if identity and adb.file_hash(f'/proc/{pid}/exe') == expected:
                        adb.stop_owned(identity)
                    elif identity:
                        raise RuntimeError('Unexpected process owns the deployment path; restoration paused')
                if had_binary:
                    temporary = remote + '.restore-' + token
                    adb.shell('cp', '-p', backup + '/previous-server', temporary)
                    adb.shell('mv', temporary, remote)
                    if adb.file_hash(remote) != old_hash:
                        raise RuntimeError('Restored file hash mismatch')
                    if previous:
                        alive = adb.identity(previous['pid'])
                        if alive and alive['starttime'] == previous['starttime']:
                            restored_pid = previous['pid']
                        else:
                            restored_pid = launch(adb, old_args, backup + '/rollback.log', old_uid, old_cwd)
                            time.sleep(0.5)
                        restored = verify(adb, restored_pid, old_hash, old_uid, old_args)
                        if restored['context'] != previous['context']:
                            raise RuntimeError('Restored process SELinux context differs from baseline')
                        record['restored'] = restored
                        record['restored_functional'] = probe_python(args.previous_python or args.python, args,
                                                                    old_version, args.previous_port or args.port, evidence('restored'))
                else:
                    adb.shell('rm', '-f', remote)
                    if adb.exists(remote):
                        raise RuntimeError('New deployment file remains after rollback')
                record['rollback'] = 'verified'
            except BaseException as recovery_error:
                record['rollback'] = 'failed'
                record['rollback_error'] = f'{type(recovery_error).__name__}: {recovery_error}'
        record['pass'] = False
        record['phase'] = 'failed'
    finally:
        actions = []
        if staged_attempted:
            actions.append(('remove staged upload', lambda: adb.shell('rm', '-f', staged)))
        errors = cleanup(actions)
        if backup_created:
            record['retained_backup'] = backup
        record['cleanup_errors'] = errors
        record['pass'] = record['pass'] and not errors
        record['completed_utc'] = utc()
        record['status'] = 'passed' if record['pass'] else ('blocked' if record.get('blocked') else 'failed')
        write_json(args.record, record)
    return (0 if record['pass'] else 2 if record.get('blocked') else 1), record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--previous-python')
    parser.add_argument('--port', type=int, default=27042)
    parser.add_argument('--previous-port', type=int)
    parser.add_argument('--server-arg', action='append', default=[])
    parser.add_argument('--arch', choices=['arm', 'arm64', 'ia32', 'x64'], required=True)
    args = parser.parse_args()
    path = PurePosixPath(args.remote)
    if not path.is_absolute() or '..' in path.parts or len(path.parts) < 3:
        parser.error('--remote must name an absolute device file')
    if args.record.exists():
        parser.error('Use a new record path')
    try:
        select_device(args.serial, args.adb)
        with device_lock(args.serial):
            code, record = deploy(args)
    except BaseException as error:
        record = {'utc': utc(), 'status': 'blocked', 'pass': False, 'error': str(error)}
        write_json(args.record, record)
        code = 2
    print(f"{record['status']}: {args.record}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
