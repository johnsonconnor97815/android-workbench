#!/usr/bin/env python3
"""Check modified Frida against the pinned OWASP UnCrackable Level 1 APK."""
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
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import traceback
import xml.etree.ElementTree as ET

import frida
import uuid
from common import Adb, Blocked, device_lock, select_device

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = 'owasp.mstg.uncrackable1'
APK_SHA256 = '1da8bf57d266109f9a07c01bf7111a1975ce01f190b9d914bcd3ae3dbef96f21'
INPUT = 'frida-functional-negative'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def deadline(seconds=25):
    cancellable = frida.Cancellable()
    timer = threading.Timer(seconds, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        with cancellable:
            yield
    finally:
        timer.cancel()


class Test:
    def __init__(self, args):
        self.args = args
        self.out = args.output.resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.record = {'utc': datetime.now(timezone.utc).isoformat(),
                       'device': args.serial, 'package': PACKAGE, 'input': INPUT,
                       'client_version': frida.__version__, 'pass': False,
                       'checks': {}, 'rounds': [], 'commands': [], 'messages': []}
        self.xml_path = '/data/local/tmp/frida-skill-ui-' + uuid.uuid4().hex + '.xml'
        self.forward = self.device = self.session = self.script = None
        self.label = 'setup'
        self.owned_app = False
        self.bridge = args.java_bridge
        self.agent = ROOT / 'assets/uncrackable-probe.js'
        self.root_adb = Adb(args.serial, root=True)

    def adb(self, *args, binary=False, check=True, timeout=25):
        command = ['adb', '-s', self.args.serial, *args]
        started = time.monotonic()
        try:
            p = subprocess.run(command, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.record['commands'].append({'argv': command, 'timeout': timeout})
            raise
        stdout = p.stdout if binary else p.stdout.decode(errors='replace').strip()
        stderr = p.stderr.decode(errors='replace').strip()
        self.record['commands'].append({
            'argv': command, 'returncode': p.returncode,
            'seconds': round(time.monotonic() - started, 3),
            'stdout': '<binary: %d bytes>' % len(stdout) if binary else stdout,
            'stderr': stderr})
        if check and p.returncode:
            raise RuntimeError(f'{command}: {stderr or stdout}')
        return stdout

    def require(self, name, condition, details=None):
        key = self.label + '/' + name
        self.record['checks'][key] = {'pass': bool(condition), 'details': details}
        if not condition:
            raise AssertionError(key + ': ' + repr(details))

    def stage(self, label):
        self.label = label
        print(label, flush=True)

    def snapshot(self, name, screenshot=False):
        self.adb('shell', 'uiautomator', 'dump', self.xml_path)
        xml = self.adb('exec-out', 'cat', self.xml_path)
        (self.out / (name + '.xml')).write_text(xml + '\n')
        root = ET.fromstring(xml)
        self.require('target_ui_' + name,
                     any(n.get('package') == PACKAGE for n in root.iter('node')))
        if screenshot:
            png = self.adb('exec-out', 'screencap', '-p', binary=True)
            self.require('png_' + name, png.startswith(b'\x89PNG\r\n\x1a\n'))
            (self.out / (name + '.png')).write_bytes(png)
        return root

    def tap(self, root, predicate):
        node = next(n for n in root.iter('node')
                    if n.get('package') == PACKAGE and predicate(n))
        bounds = list(map(int, re.findall(r'\d+', node.attrib['bounds'])))
        x1, y1, x2, y2 = bounds
        self.adb('shell', 'input', 'tap', str((x1 + x2) // 2), str((y1 + y2) // 2))

    def verify_ui(self, name, expected):
        root = self.snapshot(name + '-before')
        edit = next(n for n in root.iter('node')
                    if n.get('package') == PACKAGE and n.get('class') == 'android.widget.EditText')
        self.require('unchanged_input_' + name, edit.get('text') == INPUT, edit.get('text'))
        self.tap(root, lambda n: n.get('text', '').upper() == 'VERIFY')
        time.sleep(0.3)
        root = self.snapshot(name, screenshot=True)
        titles = [n.get('text') for n in root.iter('node') if n.get('package') == PACKAGE]
        self.require('ui_' + name, expected in titles, titles)
        self.tap(root, lambda n: n.get('text') == 'OK')
        return {'expected': expected, 'observed': expected, 'xml': name + '.xml', 'screenshot': name + '.png'}

    def connect(self, pid, runtime, hooks):
        with deadline():
            self.session = self.device.attach(pid)
            source = ('const TEST_OPTIONS = ' + json.dumps({'hooks': hooks,
                       'deoptimize': self.args.deoptimize}) + ';\n' +
                      (self.bridge.read_text() if self.bridge else '') + '\n' + self.agent.read_text())
            self.script = self.session.create_script(source, name='app-regression', runtime=runtime)
            label = self.label

            def on_message(message, data):
                event = {'phase': label, 'message': message}
                if data is not None:
                    event['binary_hex'] = data.hex()
                self.record['messages'].append(event)

            self.script.on('message', on_message)
            self.script.load()

    def rpc(self, method, *args):
        with deadline():
            return getattr(self.script.exports_sync, method)(*args)

    def release(self):
        errors = []
        for attribute, method in [('script', 'unload'), ('session', 'detach')]:
            resource = getattr(self, attribute)
            if resource is None:
                continue
            try:
                with deadline():
                    getattr(resource, method)()
                setattr(self, attribute, None)
            except BaseException as error:
                errors.append(f'{method}: {type(error).__name__}: {error}')
        if errors:
            raise RuntimeError('; '.join(errors))

    def probes(self, pid, runtime):
        ready = self.rpc('ready')
        self.require('java_ready', ready['ready'] and ready['javaAvailable'], ready)
        self.require('runtime', ready['runtime'] == runtime.upper(), ready['runtime'])
        java = self.rpc('javaprobe', INPUT)
        self.require('java_original', java['originalResult'] is False and
                     java['package'] == PACKAGE and java['checkerClassFound'] and
                     java['activityClassFound'], java)
        native = self.rpc('nativeprobe')
        length = native['strlen']
        self.require('native_hook', native['pid'] == native['nativePid'] == pid and
                     native['pidHits'] > 0 and length['hits'] == 1 and
                     length['before'] == length['restored'] == length['expected'] and
                     length['hooked'] == length['expected'] + 7, native)
        self.require('memory_file_modules', native['memory'] == [222, 173, 190, 239, 1] and
                     native['fileCmdline'] == PACKAGE and native['statusBytes'] > 0 and
                     native['artFound'], native)
        self.require('binary_message', any(
            m['phase'] == self.label and m.get('binary_hex') == 'deadbeef01'
            for m in self.record['messages']))
        return {'ready': ready, 'java': java, 'native': native}

    def run(self):
        try:
            self.stage('setup')
            if frida.__version__ != self.args.version:
                raise Blocked('Client version differs from selected Frida version')
            if self.bridge is None and int(frida.__version__.split('.')[0]) >= 17:
                try:
                    import frida_tools
                    candidate = Path(frida_tools.__file__).parent / 'bridges/java.js'
                    if candidate.is_file():
                        self.bridge = candidate
                except ImportError:
                    pass
                if self.bridge is None:
                    raise Blocked('Java bridge unavailable; provide --java-bridge with a compatible bundled bridge')
            if self.bridge is not None and not self.bridge.is_file():
                raise Blocked('Java bridge file is missing')
            self.require('apk_sha256', sha256(self.args.apk) == APK_SHA256)
            self.record['artifacts'] = [
                {'path': str(p.resolve()), 'sha256': sha256(p)} for p in
                ([self.args.apk, self.agent, Path(__file__),
                  Path(frida._frida.__file__), self.args.server] + ([self.bridge] if self.bridge else []))]
            source_dir = self.out / 'source'
            source_dir.mkdir()
            for path in [Path(__file__), self.agent]:
                (source_dir / path.name).write_bytes(path.read_bytes())
            installed = self.adb('shell', 'pm', 'path', PACKAGE)
            paths = [l.removeprefix('package:') for l in installed.splitlines() if l.startswith('package:')]
            self.require('single_apk', len(paths) == 1, paths)
            installed_hash = self.adb('shell', 'sha256sum', paths[0]).split()[0]
            self.require('installed_sha256', installed_hash == APK_SHA256, installed_hash)
            self.server_pid = self.args.server_pid
            self.server_identity = self.root_adb.identity(self.server_pid)
            if self.server_identity is None:
                raise Blocked('Selected server PID is not running')
            command = 'sha256sum /proc/%d/exe' % self.server_pid
            server_hash = self.root_adb.shell_text(command).split()[0]
            self.require('running_server_artifact', server_hash == sha256(self.args.server), server_hash)
            self.record['server'] = {'pid': self.server_pid, 'sha256': server_hash}
            self.record['environment'] = {
                key: self.adb('shell', 'getprop', prop) for key, prop in {
                    'fingerprint': 'ro.build.fingerprint', 'android': 'ro.build.version.release',
                    'api': 'ro.build.version.sdk', 'abis': 'ro.product.cpu.abilist'}.items()}
            self.record['forwards_before'] = self.adb('forward', '--list')
            self.forward = int(self.adb('forward', 'tcp:0', 'tcp:' + str(self.args.server_port)))
            self.endpoint = '127.0.0.1:' + str(self.forward)
            with deadline():
                self.device = frida.get_device_manager().add_remote_device(self.endpoint)
                apps = self.device.enumerate_applications(identifiers=[PACKAGE])
            self.require('enumerate_app', len(apps) == 1 and apps[0].identifier == PACKAGE)
            self.adb('shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
            self.adb('shell', 'wm', 'dismiss-keyguard')
            for number in range(1, self.args.rounds + 1):
                prefix = f'round-{number}'
                self.stage(prefix + '/spawn-qjs')
                self.owned_app = True
                self.adb('shell', 'am', 'force-stop', PACKAGE)
                with deadline():
                    pid = self.device.spawn([PACKAGE])
                result = {'round': number, 'pid': pid}
                self.record['rounds'].append(result)
                self.connect(pid, 'qjs', True)
                with deadline():
                    self.device.resume(pid)
                self.rpc('ready')
                self.snapshot(prefix + '-initial')
                result['spawn'] = self.probes(pid, 'qjs')
                # Set text through the widget; an installed IME may transform adb keystrokes.
                result['input_result'] = self.rpc('setinput', INPUT)
                result['state_after_input'] = self.rpc('snapshot')
                self.snapshot(prefix + '-input')
                self.require('java_main_thread_set_text', result['input_result'] == INPUT,
                             result['input_result'])
                result['baseline'] = self.verify_ui(prefix + '-baseline', 'Nope...')
                self.require('enable_rewrite', self.rpc('setrewrite', True) is True)
                result['hooked'] = self.verify_ui(prefix + '-hooked', 'Success!')
                result['spawn_state'] = self.rpc('snapshot')
                state = result['spawn_state']
                self.require('early_java_hook', state['onCreateHits'] == 1 and state['rootHits'] >= 3, state)
                self.require('java_app_calls',
                             any(c['input'] == INPUT and c['original'] is False and c['result'] is False
                                 for c in state['verifyCalls']) and
                             any(c['input'] == INPUT and c['original'] is False and c['result'] is True
                                 for c in state['verifyCalls']) and state['cryptoCalls'] >= 2, state)
                self.release()
                self.require('survived_detach', self.adb('shell', 'pidof', PACKAGE) == str(pid))
                result['restored'] = self.verify_ui(prefix + '-restored', 'Nope...')
                self.stage(prefix + '/attach-v8')
                self.connect(pid, 'v8', True)
                result['attach'] = self.probes(pid, 'v8')
                self.rpc('setrewrite', True)
                result['attach_hooked'] = self.verify_ui(prefix + '-attach-hooked', 'Success!')
                result['attach_state'] = self.rpc('snapshot')
                self.require('attach_hook_hit', any(c['original'] is False and c['result'] is True
                             for c in result['attach_state']['verifyCalls']), result['attach_state'])
                self.release()
                self.require('survived_reattach_detach', self.adb('shell', 'pidof', PACKAGE) == str(pid))
                result['attach_restored'] = self.verify_ui(prefix + '-attach-restored', 'Nope...')
                result['pass'] = True
                print(prefix + ': PASS', flush=True)
            self.require('no_script_errors', not any(m['message'].get('type') == 'error'
                         for m in self.record['messages']))
            self.record['pass'] = True
        except BaseException as error:
            self.record['error'] = type(error).__name__ + ': ' + str(error)
            self.record['blocked'] = isinstance(error, Blocked) or 'PROBE_UNSUPPORTED:' in str(error)
            self.record['traceback'] = traceback.format_exc()
            print(self.record['error'], flush=True)
        finally:
            self.stage('cleanup')
            self.record['functional_pass'] = self.record['pass']
            errors = []
            for action in [self.release,
                           lambda: self.adb('shell', 'am', 'force-stop', PACKAGE) if self.owned_app else None,
                           lambda: self.adb('shell', 'rm', '-f', self.xml_path),
                           lambda: frida.get_device_manager().remove_remote_device(self.endpoint)
                           if self.device is not None else None,
                           lambda: self.adb('forward', '--remove', 'tcp:' + str(self.forward))
                           if self.forward else None]:
                try:
                    with deadline():
                        action()
                except BaseException as error:
                    errors.append(type(error).__name__ + ': ' + str(error))
            try:
                self.record['forwards_after'] = self.adb('forward', '--list')
                if 'forwards_before' in self.record:
                    self.require('forwards_restored', self.record['forwards_after'] == self.record['forwards_before'])
                if 'server' in self.record:
                    after = self.root_adb.file_hash('/proc/%d/exe' % self.server_pid)
                    self.require('server_identity_preserved', self.root_adb.identity(self.server_pid) == self.server_identity)
                    self.require('server_preserved', after == self.record['server']['sha256'], after)
                if self.owned_app:
                    self.require('app_stopped', not self.adb('shell', 'pidof', PACKAGE, check=False))
            except BaseException as error:
                errors.append(type(error).__name__ + ': ' + str(error))
            self.record['cleanup_errors'] = errors
            self.record['pass'] = self.record['pass'] and not errors
            self.record['status'] = 'passed' if self.record['pass'] else ('blocked' if self.record.get('blocked') else 'failed')
            self.record['capabilities'] = {name: 'passed' for name in ['android-app-spawn', 'java-hook', 'native-hook', 'rpc', 'qjs', 'v8', 'app-unload-restore']} if self.record['functional_pass'] else {}
            self.record['completed_utc'] = datetime.now(timezone.utc).isoformat()
            (self.out / 'results.json').write_text(json.dumps(self.record, indent=2) + '\n')
            print(json.dumps({'pass': self.record['pass'], 'checks': len(self.record['checks']),
                              'record': str(self.out / 'results.json'), 'cleanup_errors': errors}), flush=True)
        return 0 if self.record['pass'] else 2 if self.record.get('blocked') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--server-pid', type=int, required=True)
    parser.add_argument('--server-port', type=int, default=27042)
    parser.add_argument('--java-bridge', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--deoptimize', action='store_true',
                        help='Run Java.deoptimizeEverything() before installing Java hooks')
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    select_device(args.serial)
    with device_lock(args.serial):
        return Test(args).run()


if __name__ == '__main__':
    raise SystemExit(main())
