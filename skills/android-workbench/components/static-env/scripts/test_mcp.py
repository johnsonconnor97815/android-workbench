#!/usr/bin/env python3
"""Behavioral tests for local MCP configuration and stdio probing; no network."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

import mcp_setup
from mcp_probe import Session
from mcp_smoke import is_loopback_listener


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='android mcp tests ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.specs = {'android-apktool': {'command': str(self.root / 'tool path'), 'args': []}}

    def test_actual_socket_addresses(self):
        for address in ['127.0.0.1:8650', '[::1]:8650', '[::ffff:127.0.0.1]:8650']:
            self.assertTrue(is_loopback_listener(address), address)
        for address in ['0.0.0.0:8650', '*:8650', '[::]:8650', '192.0.2.1:8650']:
            self.assertFalse(is_loopback_listener(address), address)

    def test_plan_has_no_side_effects(self):
        project = self.root / 'unused'
        proc = subprocess.run([sys.executable, str(Path(mcp_setup.__file__)), 'plan',
            '--workspace', str(project), '--servers', 'ghidra,semgrep'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(proc.stdout)['servers'], ['ghidra', 'semgrep'])
        self.assertFalse(project.exists())

    def test_toml_merge_preserves_unrelated_settings_and_is_idempotent(self):
        target = self.root / 'config.toml'
        original = '# user note\nmodel = "keep"\n[mcp_servers.existing]\ncommand = "keep"\n'
        target.write_text(original)
        mcp_setup.merge_codex(target, self.specs)
        first = target.read_text()
        self.assertTrue(first.startswith(original))
        self.assertEqual(tomllib.loads(first)['mcp_servers']['android-apktool']['command'], self.specs['android-apktool']['command'])
        mcp_setup.merge_codex(target, self.specs)
        self.assertEqual(first, target.read_text())
        self.assertEqual(len(list(self.root.glob('*.bak-*'))), 1)

    def test_config_collision_preserves_original(self):
        target = self.root / 'config.toml'
        original = '[mcp_servers.android-apktool]\ncommand = "user choice"\n'
        target.write_text(original)
        with self.assertRaisesRegex(RuntimeError, 'outside managed'):
            mcp_setup.merge_codex(target, self.specs)
        self.assertEqual(target.read_text(), original)

    def test_json_merge_preserves_other_servers(self):
        target = self.root / 'mcp.json'
        target.write_text(json.dumps({'other': True, 'servers': {'existing': {'command': 'keep'}}}))
        mcp_setup.merge_json(target, self.specs, 'vscode')
        data = json.loads(target.read_text())
        self.assertTrue(data['other'])
        self.assertEqual(data['servers']['existing']['command'], 'keep')
        self.assertIn('android-apktool', data['servers'])

    def mock_server(self, body):
        script = self.root / 'server.py'
        script.write_text(body)
        return {'command': sys.executable, 'args': [str(script)]}

    def test_protocol_handles_notification_and_paginated_tools(self):
        spec = self.mock_server('''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m['method']=='initialize': r={'serverInfo':{'name':'fixture','version':'1'},'protocolVersion':'2025-03-26','capabilities':{'tools':{}}}
 elif m['method']=='tools/list':
  r={'tools':[{'name':'second'}]} if m['params'].get('cursor') else {'tools':[{'name':'first'}],'nextCursor':'page2'}
 else: r={'content':[{'type':'text','text':'evidence'}]}
 print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{}}),flush=True)
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)
''')
        with Session(spec, self.root / 'probe', 2) as client:
            self.assertEqual(client.initialize()['tools'], ['first', 'second'])
            client.call('first', {}, 'evidence')
            with self.assertRaisesRegex(RuntimeError, 'expected evidence'):
                client.call('first', {}, 'missing')
        self.assertIsNotNone(client.process.poll())

    def test_timeout_and_tool_errors_cannot_pass(self):
        spec = self.mock_server('import sys,time\nfor line in sys.stdin: time.sleep(2)\n')
        with Session(spec, self.root / 'timeout', 0.05) as client:
            with self.assertRaises(TimeoutError):
                client.initialize()
        self.assertIsNotNone(client.process.poll())
        spec = self.mock_server('''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'isError':True,'content':[]}}),flush=True)
''')
        with Session(spec, self.root / 'error', 2) as client:
            with self.assertRaisesRegex(RuntimeError, 'isError'):
                client.request('tools/call', {})


if __name__ == '__main__':
    unittest.main()
