"""Exact selectors, stable paging and bounded raw-index graph regressions."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from scripts import apk_code_index as index


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = module_at('raw_index_fixture', ROOT / 'tests/test_apk_code_index.py')
ADAPTER_PATH = ROOT / 'skills/android-workbench/components/analysis-agent/scripts/analysis_query.py'
ADAPTER = module_at('query_adapter', ADAPTER_PATH)


class AnalysisQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='awb-query-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'analysis'; self.root.mkdir()
        (self.root / 'inventory').mkdir(); (self.root / 'native').mkdir()
        self.raw = FIXTURE.fixture_dex(); (self.root / 'input.dex').write_bytes(self.raw)
        self.unit = dict(dex_id='fixture-dex', sha256=index.digest(self.raw), entry_name='classes.dex', extracted_path='input.dex')
        (self.root / 'inventory/dex.jsonl').write_text(json.dumps(self.unit) + '\n')
        (self.root / 'native/libraries.jsonl').write_text('')
        args = SimpleNamespace(root=self.root, native_focus=None, app_prefix=['Lsample/'])
        with contextlib.redirect_stdout(io.StringIO()):
            index.build(args); index.extend_fields(args)
        self.path = self.root / 'code-index/code-index.sqlite3'

    def request(self, kind, selector=None, **extra):
        return dict(schema=index.QUERY_SCHEMA, kind=kind, selector=selector or {}, **extra)

    def query(self, kind, selector=None, **extra):
        return index.structured_query(self.path, self.request(kind, selector, **extra))

    def add_duplicate_dex(self):
        raw = bytearray(self.raw); raw[raw.index(b'x\xc0\x80')] = ord('y')
        raw[12:32] = hashlib.sha1(raw[32:]).digest(); struct.pack_into('<I', raw, 8, zlib.adler32(raw[12:]) & 0xffffffff)
        (self.root / 'other.dex').write_bytes(raw)
        with sqlite3.connect(self.path) as db:
            index.build_dex(db, self.root, dict(dex_id='second-dex', sha256=index.digest(raw), entry_name='classes2.dex', extracted_path='other.dex'))

    def test_exact_methods_callers_fields_constants_and_raw_offsets(self):
        selected = {'descriptor': 'Lsample/A;->show()V', 'artifact': 'fixture-dex', 'method_idx': 1}
        method = self.query('methods', selected)['items'][0]
        self.assertEqual(method['raw_idx'], 1); self.assertEqual(method['artifact_sha256'], self.unit['sha256'])
        self.assertEqual(method['code_offset'], 0x400)
        call = self.query('callees', selected)['items'][0]
        self.assertEqual((call['pc'], call['file_offset'], call['kind']), (6, 0x41c, 'invoke-static'))
        self.assertEqual(call['ref_idx'], 0)
        callers = self.query('callers', {'descriptor': 'Llib/C;->call()V'})['items']
        self.assertEqual(callers[0]['source_method_idx'], 1)
        field = self.query('field_sites', {'descriptor': 'Lsample/A;->text:Ljava/lang/String;'})['items'][0]
        self.assertEqual((field['pc'], field['operation']), (4, 'write'))
        site = self.query('string_sites', {'value': 'x\0😀'})['items'][0]
        self.assertEqual((site['pool_idx'], site['pc'], site['file_offset']), (7, 0, 0x410))
        self.assertFalse(self.query('methods')['whole_apk_complete'])

    def test_duplicate_descriptor_and_short_name_require_explicit_identity(self):
        self.add_duplicate_dex()
        for selector in ({'descriptor': 'Lsample/A;->show()V'}, {'name': 'show'}):
            with self.subTest(selector=selector), self.assertRaises(index.QueryError) as caught:
                self.query('callees', selector)
            self.assertEqual(caught.exception.code, 'ambiguous_selector')
            self.assertEqual(len(caught.exception.details['candidates']), 2)
        with self.assertRaises(index.QueryError) as caught:
            self.query('methods', {'name': 'show'})
        self.assertEqual(caught.exception.code, 'ambiguous_selector')
        page = self.query('callees', {'artifact': 'second-dex', 'method_idx': 1})
        self.assertEqual(page['selected']['entry'], 'classes2.dex')
        # Raw caller lookup stays in the selected DEX method-id pool.
        self.assertEqual(len(self.query('callers', {'artifact': 'second-dex', 'method_idx': 0})['items']), 1)

    def test_pages_have_stable_identity_no_gaps_or_duplicates(self):
        expected = self.query('constants')['items']; actual = []; cursor = None
        while True:
            page = self.query('constants', page_size=3, **({'cursor': cursor} if cursor else {}))
            actual.extend(page['items']); cursor = page['pagination']['next_cursor']
            if not cursor:
                self.assertTrue(page['pagination']['indexed_query_exhausted']); break
        self.assertEqual(actual, expected)
        self.assertEqual(len({(x['artifact'], x['pool_idx']) for x in actual}), len(expected))
        self.assertEqual(self.query('constants', page_size=3), self.query('constants', page_size=3))

    def test_cursor_rejects_changed_query_page_size_kind_and_database(self):
        cursor = self.query('constants', page_size=2)['pagination']['next_cursor']
        for kind, selector, extra in [('constants', {'value': 'V'}, {}), ('methods', {}, {}), ('constants', {}, {'page_size': 3})]:
            with self.subTest(kind=kind, selector=selector), self.assertRaises(index.QueryError) as caught:
                self.query(kind, selector, page_size=extra.get('page_size', 2), cursor=cursor)
            self.assertEqual(caught.exception.code, 'stale_cursor')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE constants SET value='different' WHERE pool_idx=7")
        with self.assertRaises(index.QueryError) as caught:
            self.query('constants', page_size=2, cursor=cursor)
        self.assertEqual(caught.exception.code, 'stale_cursor')

    def test_wal_changes_invalidate_cursor_and_mutation_during_read_is_rejected(self):
        db = sqlite3.connect(self.path); self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA wal_autocheckpoint=0')
        cursor = self.query('constants', page_size=2)['pagination']['next_cursor']
        db.execute("UPDATE constants SET value='wal-change' WHERE pool_idx=6"); db.commit()
        with self.assertRaises(index.QueryError) as caught:
            self.query('constants', page_size=2, cursor=cursor)
        self.assertEqual(caught.exception.code, 'stale_cursor')
        original = index.database_identity; calls = 0
        def mutate_after_read(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                db.execute("UPDATE constants SET value='second-change' WHERE pool_idx=6"); db.commit()
            return original(path)
        with mock.patch.object(index, 'database_identity', side_effect=mutate_after_read), self.assertRaises(index.QueryError) as caught:
            self.query('constants')
        self.assertEqual(caught.exception.code, 'database_changed')

    def test_native_abi_ambiguity_exact_entry_and_same_pc_are_separate(self):
        with sqlite3.connect(self.path) as db:
            for art, entry in [('arm64', 'lib/arm64-v8a/libfoo.so'), ('x86', 'lib/x86/libfoo.so')]:
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', (art, 'elf', art + '-hash', 'synthetic.so', entry))
                db.execute('INSERT INTO native_functions VALUES(?,?,?,?,?,?)', (art, 0x100, 0x180, 'symbol', 0x20, art + '-bytes'))
                db.execute('INSERT INTO native_sites VALUES(?,?,?,?,?,?,?,?,?,?)', (art, 0x100, 0x104, 0x24, 'bl', '#0x200', 0x200, '', '00000000', 'decoded_direct'))
                db.execute('INSERT INTO jni_bindings VALUES(?,?,?,?,?,?,?,?)', (art, 0x100, 'Lsample/A;->show()V', 'export', 'candidate', 'synthetic.so', art + '-hash', '{}'))
        with self.assertRaises(index.QueryError) as caught:
            self.query('native_sites', {'name': 'libfoo.so', 'address': '0x104'})
        self.assertEqual(caught.exception.code, 'ambiguous_selector')
        arm = self.query('native_sites', {'entry': 'lib/arm64-v8a/libfoo.so', 'address': 0x104})['items'][0]
        x86 = self.query('native_sites', {'artifact': 'x86', 'address': 0x104})['items'][0]
        self.assertEqual(arm['pc'], x86['pc']); self.assertNotEqual(arm['artifact'], x86['artifact'])
        outgoing = self.query('native_sites', {'artifact': 'arm64', 'address': 0x110}, direction='outgoing')['items']
        self.assertEqual(outgoing[0]['pc'], 0x104)
        bindings = self.query('jni_bindings', {'descriptor': 'Lsample/A;->show()V'})['items']
        self.assertEqual(len(bindings), 2); self.assertTrue(all(b['status'] == 'candidate' for b in bindings))

    def test_cycle_and_fanout_obey_global_graph_budgets(self):
        with sqlite3.connect(self.path) as db:
            for number in range(2, 8):
                mid = self.unit['sha256'] + ':m:' + str(number)
                db.execute('INSERT INTO dex_methods VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (mid, 'fixture-dex', number, f'Lsample/C{number};->call()V', f'Lsample/C{number};', 'call', '()V', 1, 9, 16, 1, 0, 'decoded', 'unclassified'))
                db.execute('INSERT INTO dex_calls VALUES(?,?,?,?,?,?,?,?)', (mid, 0, 16, 0x71, 1, 'Lsample/A;->show()V', '710001000000', 'invoke-static'))
            # Complete a cycle: show -> call, call -> show.
            db.execute('INSERT INTO dex_calls VALUES(?,?,?,?,?,?,?,?)', (self.unit['sha256'] + ':m:0', 0, 16, 0x71, 1, 'Lsample/A;->show()V', '710001000000', 'invoke-static'))
        graph = self.query('dex_graph', {'artifact': 'fixture-dex', 'method_idx': 1}, depth=8, max_nodes=3, max_edges=2)['graph']
        self.assertLessEqual(len(graph['nodes']), 3); self.assertLessEqual(len(graph['edges']), 2)
        self.assertIn('edge_budget', graph['budget_reasons']); self.assertFalse(graph['exhaustive'])
        complete = self.query('dex_graph', {'artifact': 'fixture-dex', 'method_idx': 1}, depth=8)['graph']
        self.assertEqual(len(complete['nodes']), 8); self.assertFalse(complete['truncated'])
        self.assertEqual(len({n['id'] for n in complete['nodes']}), 8)
        shallow = self.query('dex_graph', {'artifact': 'fixture-dex', 'method_idx': 1}, depth=0)['graph']
        self.assertEqual(shallow['budget_reasons'], ['depth_budget'])

    def test_missing_database_strict_requests_and_bounds(self):
        missing = Path(self.temp.name) / 'missing.db'
        with self.assertRaises(index.QueryError) as caught:
            index.structured_query(missing, self.request('methods'))
        self.assertEqual(caught.exception.code, 'database_missing'); self.assertFalse(missing.exists())
        for request in [dict(self.request('methods'), sql='DROP TABLE artifacts'), self.request('methods', {'method_idx': 1}), self.request('methods', {'descriptor': 'show'}), self.request('constants', page_size=True), self.request('dex_graph', {'name': 'show'}, cursor='bad'), self.request('native_sites', {'artifact': 'elf', 'address': 1}, direction=[])]:
            with self.subTest(request=request), self.assertRaises(index.QueryError):
                index.structured_query(self.path, request)
        with mock.patch.object(index, 'QUERY_MAX_ROWS', 3):
            value = self.query('constants', page_size=2)
            self.assertTrue(value['pagination']['budget_exhausted'])
            final = self.query('constants', page_size=2, cursor=value['pagination']['next_cursor'])
            self.assertIsNone(final['pagination']['next_cursor']); self.assertFalse(final['pagination']['indexed_query_exhausted'])
        with mock.patch.object(index, 'QUERY_MAX_OUTPUT_BYTES', 100), self.assertRaises(index.QueryError) as caught:
            self.query('constants')
        self.assertEqual(caught.exception.code, 'resource_limit')
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE field_sites')
        with self.assertRaises(index.QueryError) as caught:
            self.query('field_sites')
        self.assertEqual(caught.exception.code, 'index_capability_missing')

    def test_native_graph_preserves_reviewed_boundaries_and_bounds_edges(self):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', ('elf', 'elf', 'synthetic-hash', 'input.so', 'lib/arm64-v8a/native.so'))
            for address in (0x100, 0x200, 0x300):
                db.execute('INSERT INTO native_functions VALUES(?,?,?,?,?,?)', ('elf', address, address + 0x40, 'FDE', address, 'synthetic-bytes'))
            db.execute('INSERT INTO native_sites VALUES(?,?,?,?,?,?,?,?,?,?)', ('elf', 0x100, 0x104, 0x20, 'bl', '#0x200', 0x200, '', '00000000', 'decoded_direct'))
            db.execute('INSERT INTO reviewed_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('pthread_entry', 'elf', 0x200, 0x210, 0x300, None, 'create returns zero', 'bounded_static', 'review.json', 'review-hash', '{}'))
        query = {'artifact': 'elf', 'address': 0x310}
        graph = self.query('native_graph', query, depth=3)['graph']
        self.assertEqual({n['address'] for n in graph['nodes']}, {0x100, 0x200, 0x300})
        self.assertEqual(graph['edges'][0]['condition'], 'create returns zero')
        self.assertEqual(graph['edges'][1]['file_offset'], 0x20)
        self.assertFalse(graph['exhaustive']); self.assertFalse(graph['truncated'])
        limited = self.query('native_graph', query, depth=3, max_edges=1)['graph']
        self.assertEqual(len(limited['edges']), 1); self.assertIn('edge_budget', limited['budget_reasons'])

    def test_managed_adapter_imports_pinned_nested_helpers(self):
        folder = Path(self.temp.name) / 'job'; folder.mkdir()
        nested = folder / 'nested-source-0'; nested.mkdir()
        (nested / 'apk_code_index.py').write_text('PINNED=True\n')
        (nested / 'apk_navigation.py').write_text('# pinned helper\n')
        (folder / 'normalized.json').write_text(json.dumps(dict(nested_source_manifests={str(ROOT / 'scripts'): {}})))
        (folder / 'grant.json').write_text('{}')
        code = ('import importlib.util; s=importlib.util.spec_from_file_location("adapter", ' + repr(str(ADAPTER_PATH)) + '); '
                'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.load_engine().PINNED)')
        with mock.patch.dict('os.environ', {'AWB_INTERNAL_GRANT': str(folder / 'grant.json')}):
            result = subprocess.run([sys.executable, '-c', code], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(result.stdout.strip(), 'True')

    def test_standalone_cli_nonrepo_cwd_duplicate_members_and_output_protection(self):
        before = index.database_identity(self.path)
        output = Path(self.temp.name) / 'result.json'
        request = self.request('callers', {'descriptor': 'Llib/C;->call()V'})
        result = subprocess.run([sys.executable, str(ADAPTER_PATH), '--db', str(self.path), '--query-json', json.dumps(request), '--output', str(output)], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text()), json.loads(result.stdout))
        self.assertEqual(before, index.database_identity(self.path))
        with self.assertRaises(ValueError):
            ADAPTER.parse_request('{"schema":"apk-query/v1","kind":"methods","kind":"constants"}')
        result = subprocess.run([sys.executable, str(ADAPTER_PATH), '--db', str(self.path), '--query-json', json.dumps(request), '--output', str(self.path)], cwd=self.temp.name, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0); self.assertEqual(before, index.database_identity(self.path))


if __name__ == '__main__':
    unittest.main()
