"""Finite offline fixtures: lexical positions, source-map VLQ and HAR identities."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/android-workbench/components/analysis-agent/scripts/analysis_offline.py'
SPEC = importlib.util.spec_from_file_location('offline_analysis', SCRIPT)
offline = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(offline)


def encoded(value):
    return json.dumps(value).encode()


def map_fixture(**changes):
    value = dict(version=3, sources=['source.js'], names=[], mappings='AAAA,KAAC;AACA', sourcesContent=['original source'])
    value.update(changes); return value


def har_entry(body=b'payload', status=200, url='https://user:password@example.com/api?token=secret&token=second#private'):
    return dict(startedDateTime='2026-10-09T10:00:00.000Z', time=12.5,
                request=dict(method='GET', url=url, headers=[dict(name='Authorization', value='private-header')], cookies=[dict(name='session', value='private-cookie')]),
                response=dict(status=status, headers=[dict(name='Set-Cookie', value='private-response-cookie')], content=dict(text=base64.b64encode(body).decode(), encoding='base64')))


def har_fixture(entries):
    return dict(log=dict(version='1.2', entries=entries))


class OfflineAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.budgets = dict(offline.DEFAULT_BUDGETS)
        self.temp = tempfile.TemporaryDirectory(prefix='awb-offline-'); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_js_literals_ignore_comments_preserve_offsets_and_redact_urls(self):
        raw = ('// import "ignored"\nconst title = "😀";\nimport "module-a";\n'
               'const x = require("module-b");\nReactNativeWebView.postMessage("https://u:p@host.test/a?key=hidden");').encode()
        value = offline.javascript(raw, self.budgets)
        modules = [x for x in value['findings'] if x['kind'] == 'module_literal']
        self.assertEqual([x['value'] for x in modules], ['module-a', 'module-b'])
        self.assertEqual((modules[0]['byte_offset'], modules[0]['line'], modules[0]['column']), (raw.index(b'"module-a"'), 3, 7))
        urls = [x for x in value['findings'] if x['kind'] == 'url_literal']
        self.assertEqual(len(urls), 1); self.assertNotIn('hidden', urls[0]['value']); self.assertNotIn('u:p', urls[0]['value'])
        self.assertTrue(any(x['kind'] == 'bridge_lexical_clue' for x in value['findings']))
        self.assertTrue(value['lexical_scan_complete']); self.assertIn('Lexical clues only', value['limits'][0])
        prefix = 'const title = "😀"; import '
        unicode_value = offline.javascript((prefix + '"after";').encode(), self.budgets)['findings'][0]
        self.assertEqual(unicode_value['byte_offset'], len(prefix.encode()))
        self.assertEqual(unicode_value['column'], len(prefix.encode('utf-16le')) // 2)

    def test_js_unsupported_syntax_token_and_finding_budgets_are_partial(self):
        for raw in (b'const x = `dynamic ${value}`;', b'const p = /pattern/;', b'const x = "unterminated', b'import "\\q";'):
            with self.subTest(raw=raw):
                value = offline.javascript(raw, self.budgets)
                self.assertEqual(value['status'], 'partial'); self.assertFalse(value['lexical_scan_complete'])
        budget = dict(self.budgets, max_items=1)
        value = offline.javascript(b'import "one"; import "two";', budget)
        self.assertEqual(value['status'], 'partial'); self.assertEqual(len(value['findings']), 1)
        value = offline.javascript(b'/* comment */' * 100, budget)
        self.assertFalse(value['lexical_scan_complete'])
        with self.assertRaises(offline.OfflineError):
            offline.javascript(b'\xffhbc', self.budgets)

    def test_legacy_octal_and_decimal_escapes_are_unknown_not_wrong_modules(self):
        for spelling in (r'\012', r'\08', r'\01', r'\09'):
            with self.subTest(spelling=spelling):
                value = offline.javascript(('require("' + spelling + '");').encode(), self.budgets)
                self.assertEqual(value['status'], 'partial')
                self.assertFalse(value['lexical_scan_complete'])
                self.assertEqual(value['findings'], [])
                self.assertIn('Unsupported string escape spelling', value['unknowns'])
        # Escaped backslash followed by literal digits is a different spelling.
        self.assertEqual(offline.string_value(r'"\\012"'), r'\012')
        self.assertEqual(offline.string_value(r'"\0x"'), '\0x')

    def test_unterminated_repeated_comment_openers_are_consumed_once(self):
        class AuditedText(str):
            searches = 0
            def find(self, *args):
                self.searches += 1
                return super().find(*args)
        text = AuditedText('/*a' * 200000 + ' import "inside-comment";')
        # The malformed comment is one terminal span. The ordinary regex must
        # never be asked to scan/retry the repeated openers or enclosed code.
        with mock.patch.object(offline, 'TOKEN') as token:
            token.match.side_effect = AssertionError('Unclosed comment entered ordinary token matcher')
            spans = list(offline.lexical_tokens(text))
        self.assertEqual(text.searches, 1)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][:3], ('unterminated_comment', 0, len(text)))
        value = offline.javascript(text.encode(), self.budgets)
        self.assertEqual(value['status'], 'partial'); self.assertEqual(value['findings'], [])

    def test_source_map_vlq_lookup_exact_known_positions_and_missing_sources(self):
        value = offline.source_map(encoded(map_fixture()), self.budgets, line=1, column=7)
        self.assertEqual([(x['generated_line'], x['generated_column'], x['original_line'], x['original_column']) for x in value['mappings']], [(1, 0, 1, 0), (1, 5, 1, 1), (2, 0, 2, 1)])
        self.assertEqual(value['lookup']['status'], 'mapped'); self.assertEqual(value['lookup']['mapping']['generated_column'], 5)
        self.assertEqual(value['sources'][0]['content_sha256'], hashlib.sha256(b'original source').hexdigest())
        missing = offline.source_map(encoded(map_fixture(sources=['../../private.js'], sourcesContent=None)), self.budgets)
        self.assertEqual(missing['sources'][0]['content_status'], 'external_reference_not_read')
        self.assertEqual(missing['missing_source_contents'], 1)
        unmapped = offline.source_map(encoded(map_fixture(mappings='A')), self.budgets, 1, 0)
        self.assertEqual(unmapped['lookup']['status'], 'unknown')

    def test_source_maps_reject_bad_vlq_indexes_extensions_and_limits(self):
        for value in [map_fixture(version=2), map_fixture(sections=[]), map_fixture(mappings='g'), map_fixture(mappings='AA'), map_fixture(mappings='ACAA'), map_fixture(mappings='AAAA,,CAAC'), map_fixture(mappings='AAAA,AAAA'), map_fixture(sourcesContent=['a', 'b']), map_fixture(sourceRoot={})]:
            with self.subTest(value=value), self.assertRaises(offline.OfflineError):
                offline.source_map(encoded(value), self.budgets)
        limited = offline.source_map(encoded(map_fixture()), dict(self.budgets, max_items=1), 1, 0)
        self.assertEqual(limited['status'], 'partial'); self.assertEqual(limited['lookup']['status'], 'unknown')
        with self.assertRaises(offline.OfflineError) as caught:
            offline.source_map(encoded(map_fixture()), dict(self.budgets, max_content_bytes=2))
        self.assertEqual(caught.exception.code, 'resource_limit')

    def test_embedded_explicit_and_absent_maps_never_open_declared_sources(self):
        mapping = encoded(map_fixture(sources=['https://host.test/source.js?key=private'], sourcesContent=None))
        raw = b'import "a";\n//# sourceMappingURL=data:application/json;base64,' + base64.b64encode(mapping)
        with mock.patch('builtins.open', side_effect=AssertionError('Implicit read forbidden')):
            value = offline.javascript(raw, self.budgets)
        self.assertEqual(value['source_map_status'], 'embedded_map')
        self.assertEqual(value['source_map']['missing_source_contents'], 1)
        self.assertNotIn('private', value['source_map']['sources'][0]['source'])
        absent = offline.javascript(b'//# sourceMappingURL=../../private.map', self.budgets)
        self.assertEqual(absent['source_map_status'], 'declared_map_not_supplied')
        explicit = offline.javascript(b'import "a";', self.budgets, encoded(map_fixture()))
        self.assertEqual(explicit['source_map_status'], 'explicit_map')
        with self.assertRaises(offline.OfflineError):
            offline.javascript(b'//# sourceMappingURL=data:application/json;base64,%%%bad', self.budgets)
        with self.assertRaises(offline.OfflineError):
            offline.javascript(b'//# sourceMappingURL=data:application/json,{}', self.budgets)

    def test_har_base64_duplicate_records_and_sensitive_values_are_not_displayed(self):
        entries = [har_entry(b'binary\0\xff'), har_entry(b'other')]
        value = offline.har(encoded(har_fixture(entries)), self.budgets)
        self.assertEqual(value['status'], 'succeeded'); self.assertEqual([x['ordinal'] for x in value['records']], [0, 1])
        self.assertEqual(value['records'][0]['content']['sha256'], hashlib.sha256(b'binary\0\xff').hexdigest())
        self.assertNotEqual(value['records'][0]['record_sha256'], value['records'][1]['record_sha256'])
        self.assertEqual(value['records'][0]['url_sha256'], value['records'][1]['url_sha256'])
        text = offline.canonical(value)
        for secret in ('password', 'secret', 'second', 'private-header', 'private-cookie', 'private-response-cookie', 'binary', 'other'):
            self.assertNotIn(secret, text)

    def test_har_missing_content_encoding_truncation_and_invalid_records(self):
        for content, reason in [({}, 'not_recorded'), ({'text': '%%', 'encoding': 'base64'}, 'invalid_base64'), ({'text': 'abc', 'encoding': 'gzip'}, 'unsupported_encoding')]:
            entry = har_entry(); entry['response']['content'] = content
            value = offline.har(encoded(har_fixture([entry])), self.budgets)
            self.assertEqual(value['status'], 'partial'); self.assertIn(reason, value['unknowns'])
        limited = offline.har(encoded(har_fixture([har_entry(), har_entry()])), dict(self.budgets, max_items=1))
        self.assertTrue(limited['truncated']); self.assertEqual(limited['declared_records'], 2)
        value = offline.har(encoded(har_fixture([har_entry(b'long payload')])), dict(self.budgets, max_content_bytes=2))
        self.assertIn('budget_exhausted', value['unknowns'])
        for change in ('status', 'time', 'date'):
            entry = har_entry()
            if change == 'status': entry['response']['status'] = '200'
            elif change == 'time': entry['time'] = -10
            else: entry['startedDateTime'] = 'invalid'
            with self.subTest(change=change), self.assertRaises(offline.OfflineError):
                offline.har(encoded(har_fixture([entry])), self.budgets)
        for raw in (b'{"log":{},"log":{}}', b'{"a":1e999}', b'{"a":NaN}'):
            with self.assertRaises(offline.OfflineError): offline.read_json(raw)

    def test_comparison_known_changes_duplicates_and_unknown_empty_or_partial(self):
        analyze = lambda entries: offline.har(encoded(har_fixture(entries)), self.budgets)
        left = analyze([har_entry(), har_entry()]); right = analyze([har_entry(), har_entry(status=404)])
        self.assertEqual(offline.comparison(left, left, 'har')['comparison'], 'same_observed_subset')
        self.assertEqual(offline.comparison(left, right, 'har')['comparison'], 'different_observed_subset')
        self.assertEqual(offline.comparison(left, analyze([har_entry()]), 'har')['comparison'], 'different_observed_subset')
        empty = analyze([])
        self.assertEqual(offline.comparison(empty, empty, 'har')['comparison'], 'unknown')
        partial = offline.javascript(b'import "a"; import "b";', dict(self.budgets, max_items=1))
        self.assertEqual(offline.comparison(partial, partial, 'js')['comparison'], 'unknown')
        left_js = offline.javascript(b'import "a";', self.budgets); right_js = offline.javascript(b'import "b";', self.budgets)
        self.assertEqual(offline.comparison(left_js, right_js, 'js')['comparison'], 'different_observed_subset')

    def test_cli_nonrepo_cwd_no_js_execution_atomic_output_and_input_protection(self):
        input_path = self.folder / 'input.js'; sentinel = self.folder / 'should-not-exist'
        input_path.write_text('require("fs").writeFileSync(' + json.dumps(str(sentinel)) + ', "bad");')
        output = self.folder / 'result.json'; output.write_text('previous')
        before = input_path.read_bytes()
        command = [sys.executable, str(SCRIPT), 'js', '--input', str(input_path), '--output', str(output)]
        value = subprocess.run(command, cwd=self.folder, capture_output=True, text=True)
        self.assertEqual(value.returncode, 0, value.stderr); self.assertFalse(sentinel.exists())
        self.assertEqual(json.loads(value.stdout), json.loads(output.read_text()))
        self.assertEqual(input_path.read_bytes(), before); self.assertEqual(list(self.folder.glob('.result.json-*')), [])
        rejected = subprocess.run(command[:-1] + [str(input_path)], cwd=self.folder, capture_output=True, text=True)
        self.assertNotEqual(rejected.returncode, 0); self.assertEqual(input_path.read_bytes(), before)
        bound = subprocess.run(command + ['--max-bytes', '2'], cwd=self.folder, capture_output=True, text=True)
        self.assertEqual(json.loads(bound.stdout)['error']['code'], 'resource_limit')
        self.assertEqual(input_path.read_bytes(), before)

    def test_cli_source_map_position_and_comparison_are_explicit(self):
        path = self.folder / 'input.map'; path.write_bytes(encoded(map_fixture()))
        command = [sys.executable, str(SCRIPT), 'source-map', '--input', str(path), '--line', '1', '--column', '7']
        process = subprocess.run(command, cwd=self.folder, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['lookup']['mapping']['generated_column'], 5)
        process = subprocess.run(command[:-2], cwd=self.folder, capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        path_a, path_b = self.folder / 'a.har', self.folder / 'b.har'
        path_a.write_bytes(encoded(har_fixture([har_entry()]))); path_b.write_bytes(encoded(har_fixture([har_entry(status=404)])))
        process = subprocess.run([sys.executable, str(SCRIPT), 'compare', '--kind', 'har', '--baseline', str(path_a), '--candidate', str(path_b)], cwd=self.folder, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout); self.assertEqual(value['comparison'], 'different_observed_subset')
        self.assertEqual(len(value['inputs']), 2); self.assertFalse(value['semantic_complete'])


if __name__ == '__main__':
    unittest.main()
