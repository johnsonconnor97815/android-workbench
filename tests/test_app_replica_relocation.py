"""Relocation contract tests use isolated host fixtures, never restored App state."""
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.app_replica import execute, ReplicaError
from workbench.app_replica.backend import HostSlots, process_identity
from workbench.app_replica.cli import main
from workbench.app_replica.paths import PathBindings, resolve_path, read_regular
from workbench.app_replica.relocation import relocate
from workbench.app_replica.runner import Runner, native_next
from workbench.app_replica.snapshot import Inventory
from workbench.app_replica.store import Store


def sha(content):
    return hashlib.sha256(content).hexdigest()


class RelocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='awb-relocation-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / 'origin'
        self.clone = self.root / 'clone'
        self.workspace = self.clone / 'workflow'
        self.old_workspace = self.old / 'workflow'
        self.slots = self.root / 'slots.json'
        self.addCleanup(patch.stopall)
        patch.object(HostSlots, 'default_path', return_value=self.slots).start()
        self.counter = 0
        self.call('init', project={'id': 'restored-host-fixture', 'execution_root': str(self.old),
            'output_path': str(self.old / 'output'), 'workbench_source': str(ROOT)}, capacity=4)
        self.call('inventory.add', objects=[{'id': 'obj-source', 'kind': 'class', 'name': 'Original',
            'source': {'path': str(self.old / 'original.java'), 'line': 19}}])
        self.call('task.add', task={'id': 'app-owner', 'role': 'app', 'mode': 'plan',
            'description': 'Preserved App scope', 'outputs': []})
        self.call('task.add', task={'id': 'writer', 'role': 'class', 'mode': 'code',
            'description': 'Preserved writer', 'outputs': [str(self.old / 'output/code.txt'), str(self.old / 'output/checks.json')]})
        self.before = self.disk()
        # A restored clone has old absolute claims while its files live here.
        (self.clone / 'output').mkdir()
        (self.clone / 'output/code.txt').write_text('unchanged historical bytes')
        (self.clone / 'output/checks.json').write_text(json.dumps([{'name': 'host fixture', 'result': 'pass', 'evidence': ['host-only']}]))
        self.evidence = self.root / 'snapshot-evidence.json'
        self.evidence.write_text('{"fixture":"no Android runtime proof"}\n')
        self.plan = self.make_plan()

    def call(self, op, **fields):
        self.counter += 1
        if op not in {'status', 'recover', 'coverage', 'path.resolve', 'inventory.get', 'inventory.export'}:
            fields.setdefault('request_id', f'fixture-{self.counter}')
        return execute(self.workspace, {'op': op, **fields})

    def disk(self):
        return json.loads((self.workspace / 'coordination/state.json').read_text())

    def reference(self, path):
        return {'path': str(path), 'sha256': sha(path.read_bytes())}

    def make_plan(self):
        disk = self.disk()
        observed = time.time()
        observation = self.root / 'ownership.json'
        observation.write_text(json.dumps({'workspace': str(self.workspace),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'authority_sha256': sha((self.workspace / 'coordination/state.json').read_bytes()),
            'authority_revision': disk['revision'], 'observed_at': observed, 'expires_at': observed + 300,
            'method': 'main_current_clone_takeover', 'observer': process_identity(os.getpid()),
            'dispatcher_stopped': True, 'unrecorded_writers': [],
            'recorded_run_ids': [r['id'] for r in disk['runs'].values() if r['state'] != 'stopped'],
            'ownership_basis': 'Isolated fixture created and exclusively owned by this test process.'}))
        return {'op': 'workspace.relocate', 'request_id': 'relocate-fixture', 'expected_schema': 2,
            'expected_revision': disk['revision'], 'expected_sha256': sha((self.workspace / 'coordination/state.json').read_bytes()),
            'expected_binding_revision': 0, 'origin_workspace': str(self.old_workspace), 'target_workspace': str(self.workspace),
            'bindings': [{'from': str(self.old), 'to': str(self.clone)}], 'continuation_backend': 'native',
            'actor': 'host-fixture-Main', 'reason': 'Test guarded adoption only', 'evidence': [self.reference(self.evidence)],
            'fence': {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'active_runs': [], 'local_writer_observation': self.reference(observation)}}

    def adopt(self):
        return relocate(self.workspace, self.plan)

    def active_backend(self, *, live=False):
        run = self.call('task.claim', backend='codex', task_id='app-owner')['run']
        identifier = run['id']
        directory = self.workspace / 'coordination/backend' / identifier
        directory.mkdir(parents=True)
        identity = process_identity(os.getpid()) if live else {'pid': 2147483000, 'boot_id': 'old-boot', 'start_ticks': 123, 'uid': os.getuid()}
        token = 'exact-fixture-token'
        paths = {k: str(self.old_workspace / 'coordination/backend' / identifier / name) for k, name in
                 [('record', 'run.json'), ('completion', 'completion.json'), ('supervisor', 'supervisor.json')]}
        record = {'run_id': identifier, 'launch_token': token, 'process': identity, 'paths': paths,
                  'request': {'workspace': str(self.old)}}
        supervisor = {'launch_token': token, 'process': identity, 'subreaper': True, 'child_launched': False}
        completion = {'launch_token': token, 'supervisor': identity, 'kind': 'tree_reaped', 'subreaper': True, 'exit_code': 1, 'reaped': []}
        for name, value in [('run.json', record), ('supervisor.json', supervisor), ('completion.json', completion)]:
            (directory / name).write_text(json.dumps(value))
        self.plan = self.make_plan()
        self.plan['fence']['active_runs'] = [{'run_id': identifier, **{key: self.reference(directory / name) for key, name in
                [('record', 'run.json'), ('supervisor', 'supervisor.json'), ('completion', 'completion.json')]}}]
        return run, directory

    def test_R01_guards_dry_run_and_replay_after_later_commit(self):
        before = (self.workspace / 'coordination/state.json').read_bytes()
        for key, bad in [('expected_schema', True), ('expected_schema', 3), ('expected_revision', True),
                         ('expected_revision', 999), ('expected_sha256', '0' * 64), ('expected_binding_revision', 1),
                         ('target_workspace', str(self.root / 'other'))]:
            request = deepcopy(self.plan); request[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ReplicaError):
                relocate(self.workspace, request)
            self.assertEqual(before, (self.workspace / 'coordination/state.json').read_bytes())
        dry = relocate(self.workspace, self.plan, dry_run=True)
        self.assertTrue(dry['prepared']); self.assertEqual(self.disk()['schema'], 2)
        self.assertFalse((self.workspace / 'coordination/relocations').exists())
        committed = self.adopt()
        self.call('project.update', changes={'scope_note': 'later independent commit'}, evidence=['fixture'])
        latest = (self.workspace / 'coordination/state.json').read_bytes()
        self.assertEqual(committed, self.adopt())
        self.assertEqual(latest, (self.workspace / 'coordination/state.json').read_bytes())
        changed = deepcopy(self.plan); changed['request_id'] = 'second'
        with self.assertRaisesRegex(ReplicaError, 'expected_schema'):
            relocate(self.workspace, changed)
        altered = deepcopy(self.plan); altered['reason'] = 'different raw request'
        with self.assertRaisesRegex(ReplicaError, 'different content'):
            relocate(self.workspace, altered)

    def test_R02_exact_provenance_and_blob_bytes(self):
        before = self.disk()
        blobs = {str(p.relative_to(self.workspace)): p.read_bytes() for p in (self.workspace / 'coordination').rglob('*.json')}
        raw = (self.workspace / 'coordination/state.json').read_bytes()
        self.adopt(); after = self.disk()
        for key, value in before.items():
            if key not in {'schema', 'revision', 'receipts'}:
                self.assertEqual(value, after[key], key)
        self.assertEqual(before['receipts'], {key: after['receipts'][key] for key in before['receipts']})
        for path, content in blobs.items():
            if path != 'coordination/state.json':
                self.assertEqual(content, (self.workspace / path).read_bytes(), path)
        self.assertEqual(raw, (self.workspace / after['relocation']['backup']).read_bytes())
        with Store(self.workspace).locked() as state:
            self.assertEqual(state['inventory']['obj-source']['source']['path'], str(self.old / 'original.java'))

    def test_R03_schema3_wrong_clone_and_legacy_migrate_rejected(self):
        self.adopt()
        with self.assertRaisesRegex(ReplicaError, 'already adopted'):
            Store(self.workspace).migrate(expected_revision=0, expected_sha256='0' * 64)
        import shutil
        copied = self.root / 'wrong-clone'
        shutil.copytree(self.workspace, copied)
        with self.assertRaisesRegex(ReplicaError, 'exact target'):
            execute(copied, {'op': 'status'})

    def test_repeated_relocation_preserves_old_and_intermediate_aliases(self):
        import shutil
        self.adopt()
        first_plan = deepcopy(self.plan)
        # New outputs written after the first adoption use the current root.
        self.call('task.add', task={'id': 'later-writer', 'role': 'class', 'mode': 'code',
            'description': 'New path since first adoption', 'outputs': [str(self.clone / 'output/later.txt')]})
        (self.clone / 'output/later.txt').write_text('new material')
        previous_root = self.clone
        self.clone = self.root / 'second-clone'
        shutil.copytree(previous_root, self.clone)
        self.workspace = self.clone / 'workflow'
        source = self.disk()
        self.plan = self.make_plan()
        self.plan.update(request_id='relocate-second', expected_schema=3, expected_binding_revision=1,
                         origin_workspace=str(previous_root / 'workflow'),
                         bindings=[{'from': str(previous_root), 'to': str(self.clone)}])
        before = (self.workspace / 'coordination/state.json').read_bytes()
        self.assertTrue(relocate(self.workspace, self.plan, dry_run=True)['prepared'])
        self.assertEqual(before, (self.workspace / 'coordination/state.json').read_bytes())
        result = self.adopt()
        self.assertEqual(2, result['relocation']['binding_revision'])
        self.assertEqual(source['tasks'], self.disk()['tasks'])
        for alias in (self.old, previous_root, self.clone):
            self.assertEqual(self.clone / 'output/code.txt', resolve_path(self.workspace, self.disk(), str(alias / 'output/code.txt')))
        self.assertEqual(result, self.adopt())
        self.assertTrue(relocate(self.workspace, first_plan)['committed'])
        self.assertEqual(2, self.disk()['relocation']['binding_revision'])
        self.assertEqual('new material', resolve_path(self.workspace, self.disk(), str(previous_root / 'output/later.txt')).read_text())
        self.assertTrue(self.call('status')['ok'])

    def test_second_relocation_rejects_stale_binding_and_active_native_writer(self):
        import shutil
        self.adopt()
        self.call('task.claim', backend='native', task_id='app-owner')
        previous_root = self.clone
        self.clone = self.root / 'second-clone'
        shutil.copytree(previous_root, self.clone)
        self.workspace = self.clone / 'workflow'
        self.plan = self.make_plan()
        self.plan.update(request_id='relocate-second', expected_schema=3, expected_binding_revision=0,
                         origin_workspace=str(previous_root / 'workflow'),
                         bindings=[{'from': str(previous_root), 'to': str(self.clone)}])
        with self.assertRaisesRegex(ReplicaError, 'binding revision'):
            self.adopt()
        self.plan['expected_binding_revision'] = 1
        self.plan['fence']['active_runs'] = [{'run_id': r['id']} for r in self.disk()['runs'].values() if r['state'] != 'stopped']
        with self.assertRaisesRegex(ReplicaError, 'native writer'):
            self.adopt()

    def test_R03_relative_unmoved_workspace_remains_supported(self):
        import contextlib, io
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--workspace', 'clone/workflow', 'status', '--summary']), 0)
            self.assertEqual(Store('clone/workflow').workspace, self.workspace)
        finally:
            os.chdir(previous)

    def test_R04_faults_before_and_after_switch(self):
        before = (self.workspace / 'coordination/state.json').read_bytes()
        original = Store.write_bytes
        for stage in ['backup', 'plan', 'report', 'authority']:
            def failure(store, relative, content, stage=stage):
                matches = {'backup': '/backups/' in relative, 'plan': '/plans/' in relative,
                           'report': '/reports/' in relative, 'authority': relative == 'coordination/state.json'}
                if matches[stage]:
                    raise OSError('injected ' + stage)
                return original(store, relative, content)
            with self.subTest(stage=stage), patch.object(Store, 'write_bytes', failure), self.assertRaises(OSError):
                self.adopt()
            self.assertEqual(before, (self.workspace / 'coordination/state.json').read_bytes())
        with patch.object(Store, 'publish', side_effect=OSError('projection injection')):
            result = self.adopt()
        self.assertTrue(result['committed']); self.assertIn('projection_error', result)
        self.assertEqual(self.disk()['schema'], 3)
        self.assertNotIn('projection_error', self.adopt())
        backup = self.workspace / self.disk()['relocation']['backup']
        backup.write_text('corrupt')
        with self.assertRaisesRegex(ReplicaError, 'backup SHA-256'):
            self.adopt()

    def test_R04_hard_process_crashes_at_publication_boundaries(self):
        child = r"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from workbench.app_replica.backend import HostSlots
from workbench.app_replica.store import Store
from workbench.app_replica.relocation import relocate
workspace, plan_path, slots, stage = sys.argv[1:]
plan = json.loads(Path(plan_path).read_text())
original = os.replace
def replace(source, target):
    target = str(target)
    point = ('backup' if '/backups/' in target else 'plan' if '/plans/' in target else
             'report' if '/reports/' in target else 'authority' if target.endswith('/coordination/state.json') else '')
    if stage == 'before_' + point:
        os._exit(73)
    original(source, target)
    if stage == 'after_' + point:
        os._exit(73)
def publish(store, state):
    os._exit(73)
with patch.object(HostSlots, 'default_path', return_value=Path(slots)), patch('os.replace', replace):
    if stage == 'projection':
        with patch.object(Store, 'publish', publish):
            relocate(workspace, plan)
    else:
        relocate(workspace, plan)
"""
        for stage in ['before_backup', 'after_backup', 'after_plan', 'after_report',
                      'before_authority', 'after_authority', 'projection']:
            fixture = RelocationTests('runTest'); fixture.setUp(); self.addCleanup(fixture.doCleanups)
            plan_path = fixture.root / 'plan.json'; plan_path.write_text(json.dumps(fixture.plan))
            source = (fixture.workspace / 'coordination/state.json').read_bytes()
            result = subprocess.run([sys.executable, '-c', child, str(fixture.workspace), str(plan_path), str(fixture.slots), stage],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 73, result.stderr)
            switched = stage in {'after_authority', 'projection'}
            self.assertEqual(fixture.disk()['schema'], 3 if switched else 2, stage)
            if not switched:
                self.assertEqual(source, (fixture.workspace / 'coordination/state.json').read_bytes())
            committed = fixture.adopt()
            self.assertTrue(committed['committed'])
            self.assertEqual(fixture.disk()['schema'], 3)

    def test_R04_fsync_failures_do_not_rollback_visible_commit(self):
        source = (self.workspace / 'coordination/state.json').read_bytes()
        with patch('os.fsync', side_effect=OSError('injected fsync failure')), self.assertRaises(OSError):
            self.adopt()
        self.assertEqual(source, (self.workspace / 'coordination/state.json').read_bytes())
        original = Store._fsync_dir
        def fail_after_switch(path):
            if self.disk()['schema'] == 3:
                raise OSError('injected authority directory fsync failure')
            return original(path)
        with patch.object(Store, '_fsync_dir', staticmethod(fail_after_switch)):
            result = self.adopt()
        self.assertTrue(result['committed'])
        self.assertIn('authority_error', result)
        self.assertIn('uncertain_after_authority_rename', result['durability'])
        self.assertEqual(self.disk()['schema'], 3)
        self.assertTrue(self.adopt()['committed'])

    def test_R05_actual_run_completion_and_locks(self):
        run, directory = self.active_backend()
        with (self.workspace / 'coordination/dispatcher.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ReplicaError, 'lock is occupied'):
                self.adopt()
        with (directory / 'launch.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ReplicaError, 'lock is occupied'):
                self.adopt()
        reference = self.plan['fence']['active_runs'][0]['completion']
        old = (directory / 'completion.json').read_bytes()
        for changes in [{'launch_token': 'wrong'}, {'subreaper': False}, {'kind': 'missing_tree'}]:
            value = json.loads(old); value.update(changes)
            (directory / 'completion.json').write_text(json.dumps(value))
            reference.update(self.reference(directory / 'completion.json'))
            with self.assertRaisesRegex(ReplicaError, 'proof mismatch'):
                self.adopt()
        (directory / 'completion.json').write_bytes(old)
        reference.update(self.reference(directory / 'completion.json'))
        (directory / 'completion.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.adopt()
        (directory / 'completion.json').write_bytes(old)
        self.adopt()
        self.assertEqual(self.disk()['runs'][run['id']]['state'], 'reserved')

    def test_R05_authoritative_handle_binds_tree_identity_and_token(self):
        run, directory = self.active_backend()
        record = json.loads((directory / 'run.json').read_text())
        record['request']['requested'] = {'model': run['model'], 'effort': run['effort']}
        record['command'] = ['historical-cli-arguments']
        (directory / 'run.json').write_text(json.dumps(record))
        self.plan['fence']['active_runs'][0]['record'] = self.reference(directory / 'run.json')
        handle = {'run_id': run['id'], 'process': record['process'], 'paths': record['paths'],
                  'requested': record['request']['requested'], 'command': record['command'],
                  'supervisor_evidence': {'launch_token': 'wrong-authoritative-token'}}
        self.call('run.started', run_id=run['id'], backend_handle=handle)
        self.plan = self.make_plan()
        self.plan['fence']['active_runs'] = [{'run_id': run['id'], **{key: self.reference(directory / name) for key, name in
            [('record', 'run.json'), ('supervisor', 'supervisor.json'), ('completion', 'completion.json')]}}]
        with self.assertRaisesRegex(ReplicaError, 'Authoritative supervisor launch token'):
            self.adopt()
        with Store(self.workspace).locked() as state:
            state['runs'][run['id']]['backend_handle']['supervisor_evidence']['launch_token'] = record['launch_token']
            state['runs'][run['id']]['backend_handle']['process'] = {**record['process'], 'start_ticks': 999}
            Store(self.workspace).commit(state)
        self.plan = self.make_plan()
        self.plan['fence']['active_runs'] = [{'run_id': run['id'], **{key: self.reference(directory / name) for key, name in
            [('record', 'run.json'), ('supervisor', 'supervisor.json'), ('completion', 'completion.json')]}}]
        with self.assertRaisesRegex(ReplicaError, 'Authoritative run handle'):
            self.adopt()

    def test_R05_live_supervisor_and_fresh_observation_fail_closed(self):
        self.active_backend(live=True)
        with self.assertRaisesRegex(ReplicaError, 'remains present'):
            self.adopt()
        observation = self.root / 'ownership.json'
        value = json.loads(observation.read_text()); value['observed_at'] = time.time() - 301
        observation.write_text(json.dumps(value))
        self.plan['fence']['local_writer_observation'] = self.reference(observation)
        with self.assertRaisesRegex(ReplicaError, 'expired'):
            self.adopt()

    def test_R06_slots_absence_and_unrelated_records_unchanged(self):
        relocate(self.workspace, self.plan, dry_run=True); self.assertFalse(self.slots.exists())
        self.slots.write_text(json.dumps({'capacity': 3, 'reservations': {'other': {'workflow': '/other/workflow', 'state': 'occupied'}}}))
        content = self.slots.read_bytes(); self.adopt()
        self.assertEqual(content, self.slots.read_bytes())

    def test_R06_conflicting_target_slot_is_not_released(self):
        self.slots.write_text(json.dumps({'capacity': 3, 'reservations': {'other': {'workflow': str(self.old_workspace), 'state': 'occupied'}}}))
        content = self.slots.read_bytes()
        with self.assertRaisesRegex(ReplicaError, 'reservation'):
            self.adopt()
        self.assertEqual(content, self.slots.read_bytes()); self.assertEqual(self.disk()['schema'], 2)

    def test_R07_explicit_grammar(self):
        self.adopt()
        resolver = PathBindings.from_state(self.workspace, self.disk())
        old = str(self.old / 'source name.java')
        current = str(self.clone / 'source name.java')
        for value, expected in [(old, current), (old + ':10-20:2#location', current + ':10-20:2#location')]:
            self.assertEqual(resolver.resolve('locator', value)['effective'], expected)
        source = {'path': old, 'line': 8, 'table': 'methods', 'row': 3, 'archive_entry': 'same:1'}
        self.assertEqual(resolver.resolve('locator', source)['effective'], {**source, 'path': current})
        uri = 'file://' + str(self.old) + '/source%20name.java?key=1#spot'
        self.assertEqual(resolver.resolve('file_uri', uri)['effective'], 'file://' + str(self.clone) + '/source%20name.java?key=1#spot')
        for opaque in ['https://example.test/a', 'content://a/b', 'apk:/carrier', 'dex:/signature', 'apk:sha256!classes.dex', 'Lsample/Thing;->run()V']:
            self.assertEqual(resolver.resolve('locator', opaque)['effective'], opaque)
        self.assertEqual(resolver.resolve('path', 'artifacts/result')['local_path'], str(self.workspace / 'artifacts/result'))
        with self.assertRaisesRegex(ReplicaError, 'Unmapped'):
            resolver.resolve('path', str(self.old) + '-similar/file')
        for value in ['file://remote/a', 'file:///a.jar!/entry']:
            with self.assertRaisesRegex(ReplicaError, 'Unsupported'):
                resolver.resolve('file_uri', value)
        for value in [old + ' && echo x', old + ' | cat', old + '; rm x']:
            with self.assertRaisesRegex(ReplicaError, 'Opaque'):
                resolver.resolve('locator', value)
        literal = self.clone / 'name:10'; literal.write_text('literal')
        with self.assertRaisesRegex(ReplicaError, 'Ambiguous'):
            resolver.resolve('locator', str(self.old / 'name:10'))
        self.assertEqual(resolver.resolve('path', str(self.old / 'name:10'))['local_path'], str(literal))

    def test_R08_symlinks_traversal_metadata_and_conflicting_rules(self):
        link = self.root / 'link'; link.symlink_to(self.clone, target_is_directory=True)
        with self.assertRaisesRegex(ReplicaError, 'Symlink'):
            Store(link / 'workflow')
        bad = deepcopy(self.plan); bad['bindings'].append({'from': str(self.old / 'nested'), 'to': str(self.clone / 'output')})
        with self.assertRaisesRegex(ReplicaError, 'Conflicting'):
            relocate(self.workspace, bad)
        bad = deepcopy(self.plan); bad['bindings'][0]['to'] = str(self.old / 'nested')
        (self.old / 'nested').mkdir(parents=True)
        with self.assertRaisesRegex(ReplicaError, 'chains'):
            relocate(self.workspace, bad)
        self.adopt()
        (self.clone / 'output/link').symlink_to(self.clone / 'output', target_is_directory=True)
        for path in [str(self.old / 'output/../file'), str(self.old / 'output/link/file'), str(self.old_workspace / 'coordination/state.json')]:
            with self.assertRaises(ReplicaError):
                self.call('task.add', task={'id': 'writer', 'role': 'class', 'mode': 'code', 'outputs': [path]})

    def test_R08_mapped_foreign_writer_collision_blocks(self):
        with Store(self.workspace).locked() as state:
            state['writers'][str(self.clone / 'output/code.txt')] = 'foreign'
            Store(self.workspace).commit(state)
        self.plan = self.make_plan()
        with self.assertRaisesRegex(ReplicaError, 'binding alias'):
            self.adopt()
        self.assertEqual(self.disk()['schema'], 2)

    def test_R08_nested_external_destination_is_supported(self):
        external = self.clone / 'external/source-apks'
        external.mkdir(parents=True)
        self.plan['bindings'].append({'from': str(self.root / 'old-external'), 'to': str(external)})
        self.adopt()
        resolver = PathBindings.from_state(self.workspace, self.disk())
        self.assertEqual(resolver.path(str(self.root / 'old-external/base.apk')), external / 'base.apk')

    def test_R09_alias_claim_revision_submit_and_foreign_owner(self):
        self.adopt(); revision = self.disk()['tasks']['writer']['revision']
        for value in [str(self.old / 'output/code.txt'), str(self.clone / 'output/code.txt')]:
            self.call('task.add', task={'id': 'writer', 'role': 'class', 'mode': 'code', 'outputs': [value]})
            self.assertEqual(self.disk()['tasks']['writer']['revision'], revision)
        with self.assertRaisesRegex(ReplicaError, 'logical writer'):
            self.call('task.add', task={'id': 'other', 'role': 'analysis', 'mode': 'inspect', 'description': 'conflict', 'outputs': [str(self.clone / 'output/code.txt')]})
        self.call('task.add', task={'id': 'writer', 'role': 'class', 'mode': 'code', 'outputs': [str(self.old / 'output/new.txt')]})
        self.assertIn(str(self.clone / 'output/new.txt'), self.disk()['writers'])
        (self.clone / 'output/new.txt').write_text('new')
        run = self.call('task.claim', backend='native', task_id='writer', external_active=1)['run']
        self.call('run.started', run_id=run['id'], backend_handle={'target': 'fixture-worker'})
        outputs = self.disk()['tasks']['writer']['outputs']
        artifacts = [{'path': path, 'sha256': sha(resolve_path(self.workspace, self.disk(), path).read_bytes())} for path in outputs]
        result = self.call('task.submit', task_id='writer', run_id=run['id'], artifacts=artifacts,
            self_checks=[{'name': 'fixture', 'result': 'pass', 'evidence': ['host only']}], unresolved=[])
        self.assertTrue(result['delivery']['accepted'], result['delivery'])
        self.assertEqual(outputs, self.disk()['tasks']['writer']['outputs'])

    def test_delivery_tokens_followups_and_inputs_use_adopted_paths(self):
        self.adopt()
        self.call('task.add', task={'id': 'assembly', 'role': 'class', 'mode': 'code',
            'description': 'Downstream fixture writer', 'outputs': []})
        run = self.call('task.claim', backend='native', task_id='writer', external_active=1)['run']
        self.call('run.started', run_id=run['id'], backend_handle={'target': 'fixture-worker'})
        artifacts = [{'path': path, 'sha256': sha(resolve_path(self.workspace, self.disk(), path).read_bytes())}
                     for path in self.disk()['tasks']['writer']['outputs']]
        result = self.call('task.submit', task_id='writer', run_id=run['id'], artifacts=artifacts,
            self_checks=[{'name': 'source', 'result': 'pass', 'evidence': ['host fixture']}], unresolved=[],
            scope='source', inputs=[artifacts[1]], followups=[{'id': 'compile', 'stage': 'build',
                'task_id': 'assembly', 'description': 'Compile adopted source', 'evidence': [artifacts[1]]}])
        self.assertTrue(result['delivery']['accepted'])
        self.assertEqual(result['delivery']['inputs'][0]['path'], str(self.clone / 'output/checks.json'))
        self.call('run.stopped', run_id=run['id'], outcome='delivered',
                  observation={'confirmed': True, 'writers_stopped': True, 'evidence': ['host fixture']})
        status = self.call('status')
        self.assertIsNotNone(status['tasks']['writer']['delivery_token'])
        self.assertEqual(status['followup_counts']['open'], 1)
        (self.clone / 'output/checks.json').write_text('changed check proof')
        self.assertIsNone(self.call('status')['tasks']['writer']['delivery_token'])

    def test_R09_directory_alias_correction_retains_child_owner(self):
        directory = self.clone / 'output/directory'
        directory.mkdir(parents=True)
        child = directory / 'child.txt'; child.write_text('child')
        evidence = self.clone / 'output/evidence.json'; evidence.write_text('{}')
        self.call('task.add', task={'id': 'writer', 'role': 'class', 'mode': 'code',
                  'outputs': [str(self.old / 'output/directory'),
                              str(self.old / 'output/directory/child.txt'),
                              str(self.old / 'output/evidence.json')]})
        self.plan = self.make_plan(); self.adopt()
        state = self.disk(); task = state['tasks']['writer']
        result = self.call('task.outputs.remove_directories', task_id='writer', owner='writer', expected_revision=task['revision'],
             directories=[str(directory)], reason='Remove directory registration; retain child files',
             evidence=[{'path': str(evidence), 'sha256': sha(evidence.read_bytes())}])
        self.assertNotIn(str(self.old / 'output/directory'), self.disk()['writers'])
        self.assertEqual(self.disk()['writers'][str(self.old / 'output/directory/child.txt')], 'writer')

    def test_R10_historical_stale_delivery_is_preserved(self):
        store = Store(self.workspace)
        with store.locked() as state:
            task = state['tasks']['writer']
            artifact = {'path': str(self.old / 'output/code.txt'), 'sha256': '0' * 64}
            task['deliveries'].append({'id': 'old-stale', 'run_id': 'history', 'artifacts': [artifact], 'accepted': True})
            store.commit(state)
        self.plan = self.make_plan()
        dry = relocate(self.workspace, self.plan, dry_run=True)
        self.assertEqual(dry['validation']['artifacts'][0]['result'], 'changed_since_delivery')
        before = self.disk()['tasks']['writer']['deliveries']
        self.adopt()
        self.assertEqual(before, self.disk()['tasks']['writer']['deliveries'])

    def test_R10_MVP_uses_effective_observation_identity_and_stale_bytes(self):
        self.adopt()
        run = self.call('task.claim', backend='native', task_id='writer')['run']
        self.call('run.started', run_id=run['id'], backend_handle={'target': 'host-fixture'})
        artifacts = [{'path': raw, 'sha256': sha(resolve_path(self.workspace, self.disk(), raw).read_bytes())}
                     for raw in self.disk()['tasks']['writer']['outputs']]
        self.call('task.submit', task_id='writer', run_id=run['id'], artifacts=artifacts,
                  self_checks=[{'name': 'host fixture', 'result': 'pass', 'evidence': ['fixture']}], unresolved=[])
        self.call('run.stopped', run_id=run['id'], outcome='delivered',
                  observation={'confirmed': True, 'writers_stopped': True, 'evidence': ['host-fixture-only']})
        self.call('entry.add', entry={'id': 'fixture-entry', 'trigger': 'host-fixture', 'evidence': ['fixture'], 'root_object_ids': ['obj-source']})
        self.call('mvp.define', mvp={'id': 'paired-host-fixture', 'entry_ids': ['fixture-entry'], 'required_task_ids': ['writer'],
            'target_versions': {'original': 'old', 'candidate': 'new'}, 'conditions': {'kind': 'host-only'}, 'goals': ['same']})
        observations = {}
        for side in ('original', 'candidate'):
            path = self.clone / 'output' / (side + '.txt'); path.write_text(side)
            observations[side] = {'path': str(self.old / 'output' / (side + '.txt')), 'sha256': sha(path.read_bytes())}
        args = {'mvp_id': 'paired-host-fixture', 'original_version': 'old', 'candidate_version': 'new',
                'runtime_scope': {'kind': 'host_fixture', 'conditions': {'kind': 'host-only'}},
                'observations': observations, 'results': {'same': {'result': 'pass',
                'original': str(self.clone / 'output/original.txt') + '#line1',
                'candidate': str(self.old / 'output/candidate.txt') + ':1'}}}
        result = self.call('mvp.record', **args)
        self.assertEqual(result['record']['result'], 'pass')
        args['observations']['candidate'] = {**observations['original'], 'path': str(self.clone / 'output/original.txt')}
        with self.assertRaisesRegex(ReplicaError, 'distinct original/candidate'):
            self.call('mvp.record', **args)
        (self.clone / 'output/candidate.txt').write_text('changed')
        self.assertEqual(self.call('status', summary=True)['mvps']['paired-host-fixture']['current_result'], 'stale')

    def test_R10_required_source_requires_trusted_hash(self):
        source = self.clone / 'source.apk'; source.write_bytes(b'original sample')
        store = Store(self.workspace)
        with store.locked() as state:
            state['project']['source_paths'] = [str(self.old / 'source.apk')]
            store.commit(state)
        self.plan = self.make_plan()
        with self.assertRaisesRegex(ReplicaError, 'trusted snapshot hash'):
            self.adopt()
        self.plan['evidence'].append(self.reference(source))
        self.adopt()
        self.assertEqual(self.disk()['project']['source_paths'], [str(self.old / 'source.apk')])

    def test_R11_CLI_alias_operands_checks_and_projections(self):
        self.adopt()
        run = self.call('task.claim', backend='native', task_id='writer', external_active=1)['run']
        self.call('run.started', run_id=run['id'], backend_handle={'target': 'fixture'})
        import contextlib, io
        stdout, stderr = io.StringIO(), io.StringIO()
        revisions = self.clone / 'revisions.json'; revisions.write_text('{}')
        unresolved = self.clone / 'unresolved.json'; unresolved.write_text('[]')
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(['--workspace', str(self.workspace), 'submit', '--task', 'writer', '--run', run['id'],
                '--artifact', str(self.old / 'output/code.txt'), '--checks', str(self.old / 'output/checks.json'),
                '--class-revisions', str(self.old / 'revisions.json'), '--dependency-revisions', str(self.old / 'revisions.json'),
                '--unresolved', str(self.old / 'unresolved.json')])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())['delivery']['accepted'])
        projected = json.loads((self.workspace / 'tasks/writer/task.json').read_text())
        self.assertEqual(projected['outputs'], self.before['tasks']['writer']['outputs'])
        self.assertEqual(projected['_projection']['path_bindings'], 'coordination/path-bindings.json')
        command = self.clone / 'request.json'; raw = {'op': 'project.update', 'request_id': 'raw-request', 'changes': {'note': str(self.old / 'opaque')}, 'evidence': ['fixture']}
        command.write_text(json.dumps(raw))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--workspace', str(self.workspace), 'apply', '--file', str(self.old / 'request.json')]), 0)
        self.assertEqual(self.disk()['project']['note'], str(self.old / 'opaque'))
        app = self.call('task.claim', backend='native', task_id='app-owner')['run']
        self.call('run.started', run_id=app['id'], backend_handle={'target': 'fixture-app'})
        checkpoint = self.clone / 'checkpoint.md'; checkpoint.write_text('host fixture checkpoint')
        requirements = self.clone / 'requires.json'; requirements.write_text(json.dumps([{'task_id': 'writer', 'when': 'stopped'}]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--workspace', str(self.workspace), 'wait', '--task', 'app-owner', '--run', app['id'],
                '--requires', str(self.old / 'requires.json'), '--checkpoint', str(self.old / 'checkpoint.md')]), 0)
        self.assertEqual(self.disk()['tasks']['app-owner']['checkpoint'], self.reference(checkpoint))
        with Store(self.workspace).locked() as state:
            receipt = Store(self.workspace).receipt(state['receipts']['raw-request'])
            from workbench.app_replica.ids import canonical_json
            self.assertEqual(receipt['fingerprint'], sha(canonical_json(raw).encode()))

    def test_combined_scope_and_stop_next_resolve_adopted_paths(self):
        import contextlib, io
        self.adopt()
        task = self.call('task.discover', task_id='app-owner', target='obj-source',
                         source={'path': str(self.old / 'original.java'), 'line': 19})['task']
        run = self.call('task.claim', backend='native', task_id=task['id'])['run']
        self.call('run.started', run_id=run['id'], backend_handle={'target': 'fixture-scope'})
        evidence = {**self.reference(self.clone / 'output/code.txt'),
                    'path': str(self.old / 'output/code.txt')}
        published = self.call('scope.class.publish', task_id=task['id'], run_id=run['id'],
            expected_revision=0, reason='Synthetic complete declaration',
            structure={'kind': 'interface', 'name': 'Original', 'members': []}, evidence=[evidence])
        self.assertEqual(published['class']['sources'][0]['path'], str(self.clone / 'output/code.txt'))
        self.call('task.submit', task_id=task['id'], run_id=run['id'], artifacts=[],
            class_revisions={'obj-source': 1}, unresolved=[],
            self_checks=[{'name': 'host declaration fixture', 'result': 'pass', 'evidence': ['host only']}])
        writer = self.call('task.add', task={'role': 'class', 'mode': 'code', 'object_id': 'obj-source',
            'class_id': 'obj-source', 'description': 'Adopted fixture implementation',
            'outputs': [str(self.old / 'output/Original.java')]})['task']
        stop = self.clone / 'stop.json'
        stop.write_text(json.dumps({'op': 'run.stopped', 'run_id': run['id'], 'outcome': 'delivered',
            'observation': {'confirmed': True, 'writers_stopped': True, 'evidence': ['synthetic host fixture']}}))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(['--workspace', str(self.workspace), 'native-next', '--external-active', '1',
                           '--task', writer['id'], '--stopped-file', str(self.old / 'stop.json')])
        self.assertEqual(result, 0, stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())['task_id'], writer['id'])
        self.assertTrue(json.loads(stdout.getvalue())['stopped']['released'])
        self.assertIsNotNone(self.call('status', task_id=task['id'])['task']['delivery_token'])
        (self.clone / 'output/code.txt').write_text('changed structural input')
        self.assertIsNone(self.call('status', task_id=task['id'])['task']['delivery_token'])

    def test_R12_fresh_native_nine_message_handoff_preserves_old_inbox_handle(self):
        messages = []
        for i in range(9):
            messages.append(self.call('message.send', type='correction', sender='main', recipient='app-owner', task_id='app-owner',
                wake=True, payload={'original': str(self.old)})['message']['id'])
        old = self.call('task.claim', backend='codex', task_id='app-owner')['run']
        handle = {'thread_id': '01a141f7-7816-7412-8eda-ded7e78bde18', 'requested': {'model': 'gpt-6-astra', 'effort': 'xhigh'}}
        self.call('run.started', run_id=old['id'], backend_handle=handle)
        self.call('run.stopped', run_id=old['id'], outcome='failed', observation={'confirmed': True, 'writers_stopped': True, 'evidence': ['fixture-only stopped']})
        preserved = deepcopy(self.disk()['runs'][old['id']])
        self.plan = self.make_plan(); self.adopt()
        checkpoint = self.clone / 'checkpoint.md'; checkpoint.write_text('Original scope checkpoint')
        handoff = self.call('message.send', type='host_adoption', sender='main', recipient='app-owner', task_id='app-owner', wake=True,
            payload={'mode': 'fresh_native_context', 'prior_run_id': old['id'], 'checkpoint': self.reference(checkpoint), 'read_inherited_message_ids': messages})
        with patch('workbench.app_replica.backend.CodexBackend.start', side_effect=AssertionError('no codex')), patch('workbench.app_replica.backend.CodexBackend.poll', side_effect=AssertionError('no codex')):
            descriptor = native_next(self.workspace, external_active=1, task_id='app-owner')
        self.assertEqual(descriptor['directive']['tool'], 'spawn_agent')
        self.assertEqual(descriptor['directive']['requested'], {'model': 'gpt-6-astra', 'effort': 'xhigh'})
        prompt = descriptor['directive']['arguments']['message']
        self.assertIn('Old embedded source/script/prose paths are not rewritten', prompt)
        self.assertIn(str(self.clone / 'output'), prompt)
        self.assertEqual(self.disk()['runs'][old['id']], preserved)
        current = self.disk()['runs'][descriptor['run_id']]
        self.assertEqual(current['inbox_message_ids'], [handoff['message']['id']])
        self.assertEqual(preserved['inbox_message_ids'], sorted(messages))
        self.call('run.started', run_id=current['id'], backend_handle={'target': 'actual-fixture-native', 'requested': {'model': current['model'], 'effort': current['effort']}})
        self.call('run.stopped', run_id=current['id'], outcome='failed', observation={'confirmed': True, 'writers_stopped': True, 'evidence': ['fixture stopped']})
        self.call('message.send', type='authorized_next', sender='main', recipient='app-owner', task_id='app-owner', wake=True, payload='new concrete work')
        next_run = native_next(self.workspace, external_active=1, task_id='app-owner')
        self.assertEqual(next_run['directive']['tool'], 'followup_task')
        self.assertEqual(next_run['directive']['arguments']['target'], 'actual-fixture-native')

    def test_R13_unsupported_CLI_fails_before_publication_reservation_or_launch(self):
        self.adopt(); before = (self.workspace / 'coordination/state.json').read_bytes()
        with patch.object(Store, 'publish', side_effect=AssertionError('no publication')), patch('workbench.app_replica.runner.CodexBackend', side_effect=AssertionError('no backend')):
            with self.assertRaisesRegex(ReplicaError, 'CLI Runner'):
                Runner(self.workspace, host_capacity=3)
            with self.assertRaisesRegex(ReplicaError, 'native claims only'):
                self.call('task.claim', backend='codex', task_id='app-owner')
        self.assertEqual(before, (self.workspace / 'coordination/state.json').read_bytes())
        blocked = self.call('task.claim', backend='native', task_id='app-owner', external_active=4)
        self.assertEqual(blocked['reason'], 'capacity'); self.assertIsNone(blocked['run'])
        first = self.call('task.claim', backend='native', task_id='app-owner', external_active=1)['run']
        again = self.call('task.claim', backend='native', task_id='app-owner', external_active=1)
        self.assertIsNone(again['run']); self.assertEqual(self.disk()['tasks']['app-owner']['run_id'], first['id'])

    def test_R14_lazy_ordinary_operations_and_reimport_rejection(self):
        self.adopt()
        with patch.object(Inventory, '_shard', side_effect=AssertionError('ordinary action scanned inventory')):
            self.call('status', summary=True)
            self.call('project.update', changes={'ordinary': 'lazy'}, evidence=['fixture'])
            self.call('task.add', task={'id': 'writer', 'role': 'class', 'mode': 'code', 'outputs': [str(self.clone / 'output/code.txt')]})
        with self.assertRaisesRegex(ReplicaError, 'reimport'):
            self.call('inventory.import', objects=[], entries=[])
        exported = self.call('inventory.export')['objects']['obj-source']
        self.assertEqual(exported['source']['path'], str(self.old / 'original.java'))


if __name__ == '__main__':
    unittest.main()
