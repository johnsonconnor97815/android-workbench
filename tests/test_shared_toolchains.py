"""Shared storage, explicit overrides and queue coverage without downloads."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from workbench.common import WorkbenchError, atomic_json, conflict, path_resource, rpc
from workbench.project import generate, update
from workbench.registry import normalize
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shared_tools_manager", ROOT / "Tools/manager.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class SharedToolchainsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-shared-tools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"ANDROID_WORKBENCH_TOOLCHAINS": str(self.root / "shared")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.projects = [self.root / name for name in ("one", "two")]
        self.docs = []
        for project in self.projects:
            project.mkdir()
            doc = generate(project, ROOT, sys.executable)
            atomic_json(project / "workbench.project.json", doc)
            self.docs.append(doc)

    def test_new_projects_resolve_the_same_shared_tool(self):
        first = manager.inventory(self.projects[0], "jadx")["tools"][0]
        second = manager.inventory(self.projects[1], "jadx")["tools"][0]
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first["origin"], "shared")
        self.assertTrue(Path(first["path"]).is_relative_to(self.root / "shared"))
        self.assertEqual(first["status"], "not_installed")

    def test_explicit_missing_override_never_falls_back(self):
        shared = Path(manager.inventory(self.projects[1], "jadx")["tools"][0]["path"])
        shared.parent.mkdir(parents=True)
        shared.write_text("shared tool")
        self.docs[0]["toolchain"]["overrides"]["jadx"] = {"workspace": "special"}
        atomic_json(self.projects[0] / "workbench.project.json", self.docs[0])
        item = manager.inventory(self.projects[0], "jadx")["tools"][0]
        self.assertEqual(item["status"], "not_installed")
        self.assertEqual(item["origin"], "project_override")
        self.assertTrue(Path(item["path"]).is_relative_to(self.projects[0] / "special"))

    def test_worker_java_path_uses_the_jdk_binding_when_other_tools_are_shared(self):
        from workbench.toolchains import worker_environment, shared_workspace
        project=self.projects[0]
        override=project/'special'
        shared=shared_workspace(ROOT)
        for root,label in ((override,'override'),(shared,'shared')):
            state=root/'.android-static/state/jdk.json'
            state.parent.mkdir(parents=True)
            atomic_json(state,{'java_home':str(root/label)})
        self.docs[0]['toolchain']['overrides']['jdk']={'workspace':str(override)}
        environment,_=worker_environment(project,self.docs[0])
        self.assertEqual(environment['JAVA_HOME'],str(override/'override'))
        paths=environment['PATH'].split(os.pathsep)
        self.assertIn(str(override/'override/bin'),paths)
        self.assertNotIn(str(shared/'shared/bin'),paths)

    def test_damaged_metadata_remains_inspectable_for_repair(self):
        package = Path(manager.inventory(self.projects[0], 'babel')['tools'][0]['path'])
        package.parent.mkdir(parents=True)
        package.write_text('[]')
        item = manager.inventory(self.projects[0], 'babel')['tools'][0]
        self.assertEqual(item['status'], 'installed')
        self.assertIn('metadata_error', item)
        moved = manager.quarantine(self.projects[0], 'babel')
        self.assertTrue(moved)
        self.assertFalse(package.exists())
        self.assertTrue(Path(moved[0]['preserved']).is_dir())

    def test_static_checks_keep_an_explicit_workspace_even_if_it_has_shared_defaults(self):
        args = manager.static_args(self.projects[1], 'jadx')
        environment = manager.static_module().Environment(args)
        self.assertEqual(environment.root, self.projects[1] / '.android-static')

    def test_shared_install_conflicts_with_other_project_reader(self):
        reader = normalize({"project": str(self.projects[0]), "operation": "tools.check", "args": ["check", "--tool", "jadx"]}, DEFAULTS, self.root / "reader")
        writer = normalize({"project": str(self.projects[1]), "operation": "tools.ensure", "args": ["ensure", "--tool", "jadx"]}, DEFAULTS, self.root / "writer")
        self.assertTrue(conflict(reader["resources"], writer["resources"]))
        self.assertTrue(any(r["key"].startswith(path_resource(self.root / "shared")) for r in writer["resources"]))

    def test_update_preserves_legacy_installation_as_explicit_override(self):
        wrapper = self.projects[0] / ".android-static/bin/jadx"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("legacy tool")
        old = {k: v for k, v in self.docs[0].items() if k != "toolchain"}
        new = update(old, self.docs[0], project_root=self.projects[0])
        self.assertEqual(new["toolchain"]["mode"], "shared")
        self.assertEqual(Path(new["toolchain"]["overrides"]["jadx"]["workspace"]), self.projects[0])
        self.assertEqual(wrapper.read_text(), "legacy tool")

    def test_ensure_rechecks_after_install_and_retains_failed_verdict(self):
        before = {"pass": False, "tools": [{"id": "babel", "status": "not_installed", "check": {"status": "not_checked"}}]}
        after = {"pass": False, "tools": [{"id": "babel", "status": "installed", "check": {"status": "failed", "error": "fixture still broken"}}]}
        with patch.object(manager, "check", side_effect=[before, after]), patch.object(manager, "install", return_value={"pass": True}) as install:
            result = manager.ensure(self.projects[0], "babel")
        self.assertFalse(result["pass"])
        self.assertEqual(result["verification"]["tools"][0]["check"]["status"], "failed")
        self.assertEqual(install.call_count, 1)

    def test_client_queues_repairs_as_dependencies_before_consumer(self):
        from workbench.client import Client
        client = object.__new__(Client)
        client.project = str(self.projects[0])
        client.directory = self.root / 'state'
        client.token = 'fixture-only'
        request = {'operation': 'tools.js_ast', 'args': ['js-ast', '--input', 'input.js'], 'request_key': 'fixture'}
        with patch('workbench.client.rpc', return_value={'id': 'prepared-fixture'}) as rpc:
            prepared = client.prepare_tools(request)
        self.assertEqual(prepared['dependencies'], ['prepared-fixture'])
        submitted = rpc.call_args.args[2]
        self.assertEqual(submitted['operation'], 'tools.ensure')
        self.assertEqual(submitted['args'], ['ensure', '--tool', 'babel'])
        self.assertEqual(request.get('dependencies'), None)

    def test_explicit_engine_argument_does_not_install_another_version(self):
        from workbench.client import Client
        client = object.__new__(Client)
        client.project = str(self.projects[0])
        request = {'operation': 'analysis.decompile', 'args': ['decompile', '--jadx', '/explicit/jadx'], 'request_key': 'fixture'}
        with patch('workbench.client.rpc') as rpc:
            self.assertEqual(client.prepare_tools(request), request)
        rpc.assert_not_called()

    def test_managed_consumer_uses_shared_binary_after_automatic_preparation(self):
        from workbench.client import Client
        wrapper = Path(manager.inventory(self.projects[0], 'jadx')['tools'][0]['path'])
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
                           'if "--version" in sys.argv: print("1.5.6")\n'
                           'else:\n'
                           ' out=Path(sys.argv[sys.argv.index("-d")+1]);out.mkdir(parents=True,exist_ok=True)\n'
                           ' (out/"Probe.java").write_text("class Probe { String value = \\\"SHARED_TOOL\\\"; }")\n')
        wrapper.chmod(0o755)
        state = self.root / 'state'
        state.mkdir()
        atomic_json(state / 'config.json', {**DEFAULTS, 'min_free_bytes': 0})
        with (self.root / 'service.log').open('wb') as log:
            proc = subprocess.Popen([sys.executable, str(ROOT / 'scripts/workbench.py'), '--state', str(state), 'serve'], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        rpc(state, 'service.capabilities')
                        break
                    except WorkbenchError:
                        time.sleep(.02)
                sample = self.projects[1] / 'fixture.dex'
                sample.write_bytes(b'dex\n035\0fixture')
                client = Client(self.projects[1], state, 'shared-integration')
                submitted = client.submit(operation='analysis.decompile', args=['decompile', '--apk', str(sample), '--output-dir', str(self.projects[1] / 'source')], request_key='shared-consumer')
                completed = client.wait(submitted['id'], timeout=12)
                self.assertEqual(completed['state'], 'succeeded', (Path(completed['directory']) / 'stderr.log').read_text())
                self.assertIn('SHARED_TOOL', (self.projects[1] / 'source/Probe.java').read_text())
                jobs = client.call('jobs.list')
                self.assertEqual({j['operation'] for j in jobs}, {'tools.ensure', 'analysis.decompile'})
                prep = next(j for j in jobs if j['operation'] == 'tools.ensure')
                self.assertEqual(prep['state'], 'succeeded')
                self.assertLessEqual(prep['finished'], completed['started'])
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_frida_probe_prepares_the_explicit_client_release(self):
        from workbench.client import Client
        client = object.__new__(Client)
        client.project = str(self.projects[0])
        client.directory = self.root / 'state'
        client.token = 'fixture-only'
        request = {'operation': 'frida.probe_native', 'args': ['--version', '17.18.0'], 'request_key': 'frida-fixture'}
        with patch('workbench.client.rpc', return_value={'id': 'prepared-client'}) as rpc:
            prepared = client.prepare_tools(request)
        self.assertEqual(prepared['dependencies'], ['prepared-client'])
        self.assertEqual(rpc.call_args.args[2]['operation'], 'frida.install_client')
        self.assertIn('14.10.4', rpc.call_args.args[2]['args'])
        with self.assertRaisesRegex(ValueError, 'compatible explicit'):
            client.prepare_tools({**request, 'args': ['--version', '18.0.0']})

    def test_pending_frida_dependency_can_provision_interpreter_before_probe(self):
        request = {'project': str(self.projects[0]), 'operation': 'frida.probe_native',
                   'device': 'fixture-phone', 'args': ['--version', '17.18.0', '--serial', 'fixture-only', '--record', str(self.root / 'record.json')],
                   'dependencies': ['a' * 32]}
        normalized = normalize(request, {**DEFAULTS, 'devices': {'fixture-phone': {'serial': 'fixture-only'}}}, self.root / 'probe-job')
        self.assertIn('frida-clients/17.18.0/venv/bin/python', normalized['python'])
        self.assertTrue(Path(normalized['python']).is_relative_to(self.root / 'shared'))

    def test_pending_native_dependency_is_resolved_before_interpreter_launch(self):
        request = {'project': str(self.projects[0]), 'operation': 'static.inspect_so',
                   'args': ['fixture.so', '--source', 'fixture'], 'dependencies': ['a' * 32]}
        normalized = normalize(request, DEFAULTS, self.root / 'native-job')
        self.assertIn('.android-static/venvs/native-python/bin/python', normalized['python'])
        self.assertTrue(Path(normalized['python']).is_relative_to(self.root / 'shared'))
