import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import apply_profile
import build
import common
import deploy_server
import inspect_device
import probe_native
import provenance


class EvidenceTests(unittest.TestCase):
    def test_cleanup_failure_keeps_functional_result_but_fails_task(self):
        record = {'functional_pass': True}
        self.assertEqual(common.finish(record, ['forward removal failed']), 1)
        self.assertTrue(record['functional_pass'])
        self.assertFalse(record['pass'])

    def test_primary_error_survives_multiple_cleanup_failures(self):
        record = {'functional_pass': False, 'error': 'original injection failure'}
        def fail():
            raise RuntimeError('cleanup failure')
        completed = []
        errors = common.cleanup([('first', fail), ('second', lambda: completed.append(True)), ('third', fail)])
        common.finish(record, errors)
        self.assertEqual(record['error'], 'original injection failure')
        self.assertEqual(len(errors), 2)
        self.assertEqual(completed, [True])

    def test_multiple_devices_require_explicit_selection(self):
        result = subprocess.CompletedProcess([], 0, 'List of devices attached\nA\tdevice\nB\tdevice\n', '')
        with patch.object(common, 'run', return_value=result):
            with self.assertRaises(common.Blocked):
                common.select_device()
            self.assertEqual(common.select_device('B'), 'B')

    def test_unknown_android_environment_is_incomplete(self):
        fake = types.SimpleNamespace(serial='test', shell=lambda *a, **kw: '')
        with patch.object(inspect_device, 'select_device', return_value='test'), patch.object(inspect_device, 'Adb', return_value=fake):
            result = inspect_device.inspect('test')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['selection'], 'not_selected')
        self.assertIn('android', result['unavailable'])

    def test_unowned_and_changed_build_cache_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'build.ninja').write_text('stale')
            with self.assertRaises(common.Blocked):
                build.validate_cache(path, 'new')
            common.write_json(path / '.frida-modified-inputs.json', {'identity': 'old'})
            with self.assertRaises(common.Blocked):
                build.validate_cache(path, 'new')
            build.validate_cache(path, 'old')

    def test_changed_cached_artifact_is_rejected_even_when_inputs_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            artifact = path / 'server'
            artifact.write_bytes(b'expected build output')
            receipt = {'identity': 'same', 'artifacts': [{'path': str(artifact), 'sha256': common.sha256(artifact)}]}
            common.write_json(path / '.frida-modified-inputs.json', receipt)
            build.validate_cache(path, 'same')
            artifact.write_bytes(b'corrupted output')
            with self.assertRaises(common.Blocked):
                build.validate_cache(path, 'same')

    def test_device_lock_excludes_another_task(self):
        with common.device_lock('unit-test-lock'):
            with self.assertRaises(common.Blocked):
                with common.device_lock('unit-test-lock'):
                    self.fail('Second task obtained the device lock')

    def test_process_exit_during_identity_read_is_not_a_cleanup_failure(self):
        adb = common.Adb('test')
        with patch.object(adb, 'exists', side_effect=[True, False]), patch.object(adb, 'shell', side_effect=RuntimeError('proc entry disappeared')):
            self.assertIsNone(adb.identity(7))

    def test_identity_read_error_on_live_process_is_preserved(self):
        adb = common.Adb('test')
        with patch.object(adb, 'exists', return_value=True), patch.object(adb, 'shell', side_effect=RuntimeError('permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                adb.identity(7)

    def test_reused_pid_is_never_signalled(self):
        adb = common.Adb('test')
        with patch.object(adb, 'identity', return_value={'pid': 7, 'starttime': 'new', 'exe': '/other'}), patch.object(adb, 'shell') as shell:
            adb.stop_owned({'pid': 7, 'starttime': 'old', 'exe': '/owned'})
            shell.assert_not_called()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        def git(*args):
            return common.run(['git', '-C', self.source, *args]).stdout.strip()
        self.git = git
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'answer.txt').write_text('old\n')
        git('add', 'answer.txt')
        git('commit', '-qm', 'fixture')
        commit = git('rev-parse', 'HEAD')
        (self.source / 'answer.txt').write_text('new\n')
        patch_file = self.root / 'change.patch'
        patch_file.write_text(git('diff') + '\n')
        git('restore', 'answer.txt')
        self.profile = self.root / 'profile.json'
        self.manifest = {'schema': 1, 'version': 'fixture', 'commits': {'.': commit},
                         'groups': [{'name': 'fixture', 'patches': [{'repository': '.', 'file': patch_file.name,
                                                                  'sha256': common.sha256(patch_file)}]}]}
        common.write_json(self.profile, self.manifest)

    def test_check_is_read_only_then_patch_applies(self):
        before = provenance.fingerprint(self.source)
        result = apply_profile.apply(self.source, self.profile, check=True)
        self.assertEqual(result['status'], 'checked')
        self.assertEqual(provenance.fingerprint(self.source), before)
        result = apply_profile.apply(self.source, self.profile)
        self.assertEqual((self.source / 'answer.txt').read_text(), 'new\n')
        self.assertNotEqual(provenance.fingerprint(self.source)['identity'], before['identity'])
        self.assertEqual(result['status'], 'applied')

    def test_wrong_commit_cannot_be_overridden_by_dirty_source(self):
        self.manifest['commits']['.'] = '0' * 40
        common.write_json(self.profile, self.manifest)
        (self.source / 'answer.txt').write_text('user edit\n')
        with self.assertRaises(common.Blocked):
            apply_profile.apply(self.source, self.profile)
        self.assertEqual((self.source / 'answer.txt').read_text(), 'user edit\n')

    def test_corrupt_patch_fails_without_modification(self):
        (self.root / 'change.patch').write_text('corrupt')
        with self.assertRaises(common.Blocked):
            apply_profile.apply(self.source, self.profile)
        self.assertEqual((self.source / 'answer.txt').read_text(), 'old\n')

    def test_untracked_input_changes_fingerprint(self):
        before = provenance.fingerprint(self.source)
        (self.source / '额外构建输入.c').write_text('new build input')
        self.assertNotEqual(provenance.fingerprint(self.source)['identity'], before['identity'])


class NativeProbeTests(unittest.TestCase):
    def execute(self, cleanup_failure=False, rpc_failure=False, version='17.18.0'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        client = directory / 'client.so'
        client.write_bytes(b'fixture client')
        args = argparse.Namespace(serial='test', version=version, record=directory / 'result.json', adb='adb',
                                  no_root=False, gadget=None, harness=None, endpoint=None, server_port=27042,
                                  spawn=False, target='/fixture/sleep', runtime='qjs', arch='arm64')
        calls = []
        class FakeAdb:
            def __init__(self, *a, **kw): pass
            def call(self, *argv):
                calls.append(argv)
                return '45001'
            def launch(self, argv): return 42
            def identity(self, pid): return {'pid': pid, 'uid': 0, 'exe': '/fixture/sleep', 'starttime': '1'}
            def stop_owned(self, identity):
                if cleanup_failure: raise RuntimeError('Cannot clean up owned process')
        def rpc():
            if rpc_failure: raise RuntimeError('Original RPC failure')
            return {'pid': 42, 'nativePid': 42, 'hits': 1, 'statusBytes': 100, 'runtime': 'QJS', 'arch': 'arm64'}
        script = types.SimpleNamespace(load=lambda: None, unload=lambda: None, exports_sync=types.SimpleNamespace(probe=rpc))
        session = types.SimpleNamespace(create_script=lambda *a, **kw: script, detach=lambda: None)
        device = types.SimpleNamespace(attach=lambda p: session, enumerate_processes=lambda: [types.SimpleNamespace(pid=42)])
        manager = types.SimpleNamespace(add_remote_device=lambda e: device, remove_remote_device=lambda e: None)
        frida = types.SimpleNamespace(__version__='17.18.0', _frida=types.SimpleNamespace(__file__=str(client)), get_device_manager=lambda: manager)
        with patch.dict(sys.modules, {'frida': frida}), patch.object(probe_native, 'Adb', FakeAdb), patch.object(probe_native, 'deadline', lambda: nullcontext()):
            code, result = probe_native.probe(args)
        self.assertEqual(json.loads(args.record.read_text()), result)
        return code, result, calls

    def test_success_checks_and_cleans_owned_forward(self):
        code, result, calls = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(result['capabilities']['native-hook'], 'passed')
        self.assertIn(('forward', '--remove', 'tcp:45001'), calls)

    def test_cleanup_failure_cannot_report_pass(self):
        code, result, calls = self.execute(cleanup_failure=True)
        self.assertEqual(code, 1)
        self.assertTrue(result['functional_pass'])
        self.assertFalse(result['pass'])
        self.assertIn(('forward', '--remove', 'tcp:45001'), calls)

    def test_rpc_error_and_cleanup_error_are_both_retained(self):
        code, result, _ = self.execute(cleanup_failure=True, rpc_failure=True)
        self.assertEqual(code, 1)
        self.assertIn('Original RPC failure', result['error'])
        self.assertTrue(result['cleanup_errors'])

    def test_client_mismatch_is_blocked_before_device_mutation(self):
        code, result, calls = self.execute(version='16.7.19')
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(calls, [])


class AppCleanupTests(unittest.TestCase):
    def test_detach_is_attempted_after_unload_failure(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('app_cleanup_fixture', SCRIPTS / 'probe_uncrackable.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'frida': types.SimpleNamespace()}):
            spec.loader.exec_module(module)
        probe = module.Test.__new__(module.Test)
        calls = []
        def unload():
            raise RuntimeError('script already destroyed')
        probe.script = types.SimpleNamespace(unload=unload)
        probe.session = types.SimpleNamespace(detach=lambda: calls.append('detach'))
        with patch.object(module, 'deadline', lambda: nullcontext()):
            with self.assertRaisesRegex(RuntimeError, 'script already destroyed'):
                probe.release()
        self.assertEqual(calls, ['detach'])
        self.assertIsNone(probe.session)


class DeploymentTests(unittest.TestCase):
    def exercise(self, candidate_fails=False, cleanup_fails=False, previous_uid=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        binary = directory / 'server'
        binary.write_bytes(b'candidate server')
        remote = '/data/local/tmp/fixture-server'
        expected = common.sha256(binary)
        old_hash = hashlib.sha256(b'old server').hexdigest()
        old_args = [remote, '--listen=127.0.0.1:27123']
        processes = {7: {'pid': 7, 'uid': previous_uid, 'starttime': '7', 'exe': remote,
                         'sha256': old_hash, 'argv': old_args, 'context': 'u:r:fixture:s0'}}
        files = {remote: old_hash}
        stopped = []
        class FakeAdb:
            def __init__(self, *a, **kw): pass
            def exists(self, path): return path in files
            def identity(self, pid):
                p = processes.get(pid)
                return {k:p[k] for k in ['pid','uid','starttime','exe']} if p else None
            def file_hash(self, path):
                if path.startswith('/proc/'):
                    return processes[int(path.split('/')[2])]['sha256']
                return files[path]
            def shell_text(self, command, **kw): return ''
            def shell(self, *argv):
                command = argv[0]
                if argv == ('id', '-u'): return '0'
                if command == 'getenforce': return 'Enforcing'
                if command == 'readlink': return '/'
                if command == 'cat':
                    p = processes[int(argv[1].split('/')[2])]
                    return '\0'.join(p['argv']) + '\0' if argv[1].endswith('/cmdline') else p['context']
                if command == 'cp': files[argv[-1]] = files[argv[-2]]
                elif command == 'mv': files[argv[-1]] = files.pop(argv[-2])
                elif command == 'rm':
                    if cleanup_fails and '.stage-' in argv[-1]: raise RuntimeError('Stage cleanup failed')
                    files.pop(argv[-1], None)
                elif len(argv) > 1 and argv[1] == '--version': return '17.18.0'
                return ''
            def call(self, *argv):
                if argv[0] == 'push': files[argv[2]] = expected
                return ''
            def stop_owned(self, identity):
                stopped.append(identity['pid'])
                processes.pop(identity['pid'], None)
        counter = [20]
        def start(adb, argv, log, uid, cwd='/'):
            counter[0] += 1
            pid = counter[0]
            processes[pid] = {'pid': pid, 'uid': uid, 'starttime': str(pid), 'exe': remote,
                              'sha256': files[remote], 'argv': argv, 'context': 'u:r:fixture:s0'}
            return pid
        probes = []
        def check(python, args, version, port, output):
            probes.append(output.name)
            if candidate_fails and 'candidate' in output.name:
                raise RuntimeError('Candidate injection failed')
            return {'pass': True}
        args = argparse.Namespace(serial='test', adb='adb', binary=binary, remote=remote,
                                  version='17.18.0', record=directory / 'deploy.json', python=sys.executable,
                                  previous_python=None, previous_port=None, port=27123, arch='arm64', server_arg=[])
        with patch.object(deploy_server, 'Adb', FakeAdb), patch.object(deploy_server, 'find_servers', lambda *a: list(processes)), patch.object(deploy_server, 'launch', start), patch.object(deploy_server, 'probe_python', check), patch.object(deploy_server.time, 'sleep'):
            code, result = deploy_server.deploy(args)
        self.assertEqual(json.loads(args.record.read_text()), result)
        return code, result, files, processes, probes, stopped, old_hash, expected

    def test_failed_candidate_restores_binary_arguments_and_function(self):
        code, result, files, processes, probes, _, old, _ = self.exercise(candidate_fails=True)
        self.assertEqual(code, 1)
        self.assertFalse(result['pass'])
        self.assertIn('Candidate injection failed', result['error'])
        self.assertEqual(result['rollback'], 'verified')
        self.assertEqual(files[result['remote']], old)
        self.assertEqual(len(processes), 1)
        self.assertEqual(next(iter(processes.values()))['argv'][1], '--listen=127.0.0.1:27123')
        self.assertTrue(any('restored' in p for p in probes))

    def test_success_uses_uploaded_artifact_and_preserves_custom_arguments(self):
        code, result, files, processes, _, _, _, new = self.exercise()
        self.assertEqual(code, 0)
        self.assertEqual(files[result['remote']], new)
        self.assertEqual(next(iter(processes.values()))['argv'][1], '--listen=127.0.0.1:27123')

    def test_cleanup_failure_is_reported_after_functional_success(self):
        code, result, *_ = self.exercise(cleanup_fails=True)
        self.assertEqual(code, 1)
        self.assertTrue(result['functional']['pass'])
        self.assertFalse(result['pass'])
        self.assertIn('Stage cleanup failed', result['cleanup_errors'][0])

    def test_unsupported_previous_uid_does_not_stop_service(self):
        code, result, _, processes, _, stopped, _, _ = self.exercise(previous_uid=1234)
        self.assertEqual(code, 2)
        self.assertEqual(stopped, [])
        self.assertIn(7, processes)


if __name__ == '__main__':
    unittest.main()
