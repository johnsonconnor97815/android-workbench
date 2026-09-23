#!/usr/bin/env python3
"""Local installer invariants; no network or system package changes."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

import setup


class InstallerInvariants(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='android static tests ')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.args = argparse.Namespace(workspace=str(self.project), cache_dir=None, only='androguard,apkid', profile='full', with_mobsf=False, action='install', install_system_deps=False, accept_sdk_licenses=False, mobsf_image=None)
        self.env = setup.Environment(self.args)

    def test_plan_has_no_side_effects(self):
        result = subprocess.run([sys.executable, str(Path(setup.__file__)), 'plan', '--workspace', str(self.project)], capture_output=True, text=True, check=True)
        self.assertIn('ghidra', json.loads(result.stdout)['tools'])
        self.assertIn('droidasc', json.loads(result.stdout)['tools'])
        self.assertFalse(self.project.exists())

    def test_droidasc_is_full_profile_only(self):
        args = argparse.Namespace(workspace=str(self.project), cache_dir=None, only=None, profile='core', with_mobsf=False, action='plan', install_system_deps=False, accept_sdk_licenses=False, mobsf_image=None)
        env = setup.Environment(args)
        self.assertNotIn('droidasc', env.selected)
        env.args.profile = 'full'
        env.selected = setup.select_tools(env.args)
        self.assertIn('droidasc', env.selected)

    def test_unmanaged_install_directory_is_preserved(self):
        self.env.root.mkdir(parents=True)
        sentinel = self.env.root / 'existing'
        sentinel.write_text('preserve')
        with self.assertRaisesRegex(RuntimeError, 'not owned'):
            self.env.prepare()
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_corrupt_cache_cannot_be_installed(self):
        self.env.prepare()
        cached = self.env.cache / 'test-tool.zip'
        cached.write_bytes(b'corrupt')
        recipe = {'url': 'https://example.invalid/tool.zip', 'checksum': 'sha256:' + hashlib.sha256(b'expected').hexdigest()}
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            self.env.fetch('test', recipe)
        self.assertNotIn('test', self.env.artifacts)
        self.assertEqual(cached.read_bytes(), b'corrupt')

    def test_archive_traversal_is_rejected(self):
        self.env.prepare()
        archive = Path(self.temp.name) / 'malicious.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../outside', 'must not write')
        with self.assertRaisesRegex(RuntimeError, 'Unsafe ZIP'):
            self.env.extract(archive, Path(self.temp.name) / 'zip-output')
        self.assertFalse((Path(self.temp.name) / 'outside').exists())
        archive = Path(self.temp.name) / 'malicious.tar'
        with tarfile.open(archive, 'w') as t:
            item = tarfile.TarInfo('../outside')
            item.size = 1
            t.addfile(item, io.BytesIO(b'x'))
        with self.assertRaises(tarfile.OutsideDestinationError):
            self.env.extract(archive, Path(self.temp.name) / 'tar-output')
        self.assertFalse((Path(self.temp.name) / 'outside').exists())

    def test_partial_failure_is_nonzero_and_keeps_existing_envrc(self):
        self.project.mkdir()
        config = self.project / '.envrc'
        config.write_text('export PROJECT_SENTINEL=original\n')
        visited = []
        def install(name):
            visited.append(name)
            if name == 'androguard':
                raise RuntimeError('simulated dependency failure')
        self.env.install_one = install
        self.env.checks = lambda name: [{'test_double': True}]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.env.execute(), 1)
        self.assertEqual(visited, ['androguard', 'apkid'])
        self.assertEqual(config.read_text(), 'export PROJECT_SENTINEL=original\n')
        result = json.loads((self.env.root / 'manifest.json').read_text())['results']
        self.assertEqual(result['androguard']['status'], 'failed')
        self.assertEqual(result['apkid']['status'], 'verified')

    def test_activation_is_safe_with_spaces_and_idempotent(self):
        self.env.prepare()
        self.env.activation()
        script = '. "$1"; first_path=$PATH; . "$1"; test "$first_path" = "$PATH"; command -v sh'
        for shell in ['bash', 'sh']:
            subprocess.run([shell, '-c', script, shell, str(self.env.root / 'env.sh')], stdout=subprocess.DEVNULL, check=True)


if __name__ == '__main__':
    unittest.main()
