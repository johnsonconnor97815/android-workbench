#!/usr/bin/env python3
"""Check native attach/spawn or Gadget harness; Android App spawn is a separate test."""
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
from pathlib import Path
import time
import uuid
from common import Adb, Blocked, cleanup, deadline, device_lock, finish, select_device, sha256, utc, write_json

ASSETS = Path(__file__).resolve().parent.parent / 'assets'


def probe(args):
    record = {'schema': 1, 'utc': utc(), 'device': args.serial, 'functional_pass': False,
              'mode': 'gadget-harness' if args.gadget else ('native-spawn' if args.spawn else 'native-attach'),
              'runtime': args.runtime, 'capabilities': {}, 'probe_sha256': sha256(ASSETS / 'native-probe.js')}
    adb = Adb(args.serial, args.adb, root=not args.no_root)
    session = script = device = owned = forwarding = None
    remote = None
    actions = []
    try:
        import frida
        record['client_version'] = frida.__version__
        record['client_sha256'] = sha256(frida._frida.__file__)
        if frida.__version__ != args.version:
            raise Blocked(f'Client version {frida.__version__} differs from selected version {args.version}')
        endpoint = args.endpoint
        if args.gadget:
            remote = '/data/local/tmp/frida-skill-probe-' + uuid.uuid4().hex
            adb.shell('mkdir', remote)
            actions.append(('remove harness directory', lambda: adb.shell('rmdir', remote)))
            config = args.record.with_suffix('.gadget-config.json')
            write_json(config, {'interaction': {'type': 'listen', 'address': '127.0.0.1',
                       'port': args.gadget_port, 'on_load': 'resume'}, 'teardown': 'full'})
            for source, name in [(args.gadget, 'probe.so'), (args.harness, 'harness'), (config, 'probe.config')]:
                adb.call('push', str(source), remote + '/' + name)
                actions.insert(0, ('remove ' + name, lambda n=name: adb.shell('rm', '-f', remote + '/' + n)))
                if adb.file_hash(remote + '/' + name) != sha256(source):
                    raise RuntimeError('Uploaded artifact hash mismatch: ' + name)
            record['gadget_sha256'] = sha256(args.gadget)
            record['harness_sha256'] = sha256(args.harness)
            adb.shell('chmod', '755', remote + '/harness')
            actions.insert(0, ('remove harness log', lambda: adb.shell('rm', '-f', remote + '/run.log')))
            pid = adb.launch([remote + '/harness', remote + '/probe.so', '120'], remote + '/run.log')
            record['target_pid'] = pid
            owned = adb.identity(pid)
            time.sleep(0.5)
        if not endpoint:
            port = args.gadget_port if args.gadget else args.server_port
            forwarding = int(adb.call('forward', 'tcp:0', 'tcp:' + str(port)))
            endpoint = '127.0.0.1:' + str(forwarding)
        record['endpoint'] = endpoint
        with deadline():
            device = frida.get_device_manager().add_remote_device(endpoint)
            if args.gadget:
                pids = [p.pid for p in device.enumerate_processes()]
                if pids != [pid]:
                    raise RuntimeError('Gadget endpoint does not belong to the created harness')
            else:
                argv = [args.target, '120']
                pid = device.spawn(argv) if args.spawn else adb.launch(argv)
                record['target_pid'] = pid
                owned = adb.identity(pid)
            if owned is None:
                raise RuntimeError('Created probe process exited before attach')
            record['owned_identity'] = owned
            session = device.attach(pid)
            script = session.create_script((ASSETS / 'native-probe.js').read_text(), runtime=args.runtime)
            script.load()
            if args.spawn and not args.gadget:
                device.resume(pid)
            rpc = script.exports_sync.probe()
            record['rpc'] = rpc
            if not (rpc['pid'] == rpc['nativePid'] == pid and rpc['hits'] > 0 and rpc['statusBytes'] > 0):
                raise RuntimeError('Native Hook/RPC/file check failed')
            if args.arch and rpc['arch'] != args.arch:
                raise RuntimeError('Target ABI differs from expected ABI')
            if rpc['runtime'].lower() != args.runtime:
                raise RuntimeError('JavaScript runtime differs from requested runtime')
            script.unload()
            script = None
            session.detach()
            session = None
            if pid not in [p.pid for p in device.enumerate_processes()]:
                raise RuntimeError('Target exited after detach')
        if args.gadget:
            adb.stop_owned(owned)
            record['device_log'] = adb.shell('cat', remote + '/run.log')
            if 'unloaded' not in record['device_log']:
                raise RuntimeError('Gadget full dlclose was not observed')
        record['capabilities'] = {name: 'passed' for name in [record['mode'], 'native-hook', 'rpc', 'file-io', 'unload-detach', args.runtime]}
        record['functional_pass'] = True
    except BaseException as error:
        record['error'] = f'{type(error).__name__}: {error}'
        record['blocked'] = isinstance(error, (Blocked, ModuleNotFoundError)) or 'PROBE_UNSUPPORTED:' in str(error)
    finally:
        def release(resource, method):
            if resource is not None:
                with deadline():
                    getattr(resource, method)()
        release_actions = [('unload script', lambda: release(script, 'unload')),
                           ('detach session', lambda: release(session, 'detach'))]
        if owned is not None:
            release_actions.append(('stop owned probe', lambda: adb.stop_owned(owned)))
        if device is not None:
            release_actions.append(('remove Frida endpoint', lambda: frida.get_device_manager().remove_remote_device(endpoint)))
        if forwarding is not None:
            release_actions.append(('remove owned forward', lambda: adb.call('forward', '--remove', 'tcp:' + str(forwarding))))
        errors = cleanup(release_actions + actions)
        if record.get('target_pid') and owned is None:
            errors.append('Probe process ownership was not captured; inspect target_pid before retrying')
        code = finish(record, errors)
        write_json(args.record, record)
    return code, record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--endpoint')
    parser.add_argument('--server-port', type=int, default=27042)
    parser.add_argument('--arch', choices=['arm', 'arm64', 'ia32', 'x64'])
    parser.add_argument('--runtime', choices=['qjs', 'v8'], default='qjs')
    parser.add_argument('--target', default='/system/bin/sleep')
    parser.add_argument('--spawn', action='store_true')
    parser.add_argument('--no-root', action='store_true')
    parser.add_argument('--gadget', type=Path)
    parser.add_argument('--harness', type=Path)
    parser.add_argument('--gadget-port', type=int, default=27165)
    args = parser.parse_args()
    if args.record.exists():
        parser.error('Use a new record path')
    if args.gadget and (not args.harness or args.spawn or args.endpoint):
        parser.error('Gadget requires --harness and its own endpoint; omit --spawn and --endpoint')
    try:
        select_device(args.serial, args.adb)
        with device_lock(args.serial):
            code, record = probe(args)
    except BaseException as error:
        record = {'utc': utc(), 'status': 'blocked', 'pass': False, 'error': str(error)}
        write_json(args.record, record)
        code = 2
    print(f"{record['status']}: {args.record}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
