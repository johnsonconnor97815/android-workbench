"""Central tool boundaries; no dependency download, system install or device operation."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import time
import io
import hashlib
import tarfile
import unittest
from unittest.mock import patch

from scripts import build_plugin
from workbench.common import WorkbenchError, atomic_json, path_resource, rpc
from workbench.client import Client
from workbench.project import builtin_operations, generate, update
from workbench.registry import normalize, snapshot_tree
from workbench.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('awb_tools_test', ROOT/'Tools/manager.py')
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)
rust_spec=importlib.util.spec_from_file_location('awb_rust_test',ROOT/'Tools/rust_toolchain.py')
rust=importlib.util.module_from_spec(rust_spec);rust_spec.loader.exec_module(rust)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='awb-tools-test-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)/'project'
        self.project.mkdir()
        self.doc = {'version':1,'workbench_root':str(ROOT),'python':sys.executable,'operations':builtin_operations()}
        atomic_json(self.project/'workbench.project.json',self.doc)

    def normalized(self, operation, args):
        return normalize({'project':str(self.project),'operation':operation,'args':args},
                         {**DEFAULTS,'min_free_bytes':0},Path(self.temp.name)/'job')

    def test_registration_defaults_and_old_manifest_lock_coverage(self):
        for operation in ['inventory','check','install','js_ast','js_deobfuscate','binary_layout','firmware_scan']:
            self.assertIn('tools.'+operation,builtin_operations())
        spec=self.normalized('tools.js_ast',['js-ast','--input','app.js'])
        output=Path(spec['outputs'][0])
        self.assertIn(str(output)+'.artifacts',spec['outputs'])
        self.assertIn({'key':path_resource(self.project/'Tools/runtime'),'mode':'read'},spec['resources'])
        self.assertIn('adapters/js_ast.cjs',spec['source_manifest'])

    def test_workspace_cannot_escape_registered_project_locks(self):
        with self.assertRaisesRegex(WorkbenchError,'workspace must match'):
            self.normalized('tools.install',['install','--tool','babel','--workspace','../other'])
        with self.assertRaisesRegex(WorkbenchError,'workspace must match'):
            self.normalized('tools.check',['check','--workspace','.','--workspace','.'])

    def test_update_preserves_custom_environments_and_path(self):
        generated=generate(self.project,ROOT,sys.executable)
        old={**self.doc,'environments':['custom','Tools/runtime'],'mcp_environments':['other'],'env':{'PATH':'original'}}
        updated=update(old,generated)
        self.assertEqual(updated['environments'],['custom','Tools/runtime'])
        self.assertEqual(updated['mcp_environments'],['other','Tools/runtime'])
        self.assertEqual(updated['env'],old['env'])

    def test_runtime_binaries_symlinks_excluded_source_kept_after_arbitrary_clone(self):
        fake=Path(self.temp.name)/'clone'
        (fake/'Tools/runtime/node').mkdir(parents=True)
        (fake/'Tools/manager.py').write_text('pass\n')
        (fake/'Tools/runtime/node/generated.json').write_text('{}')
        (fake/'Tools/runtime/binary').symlink_to('/bin/false')
        with patch.object(build_plugin,'ROOT',fake),patch.object(build_plugin,'FILES',()),patch.object(build_plugin,'DIRECTORIES',('Tools',)):
            self.assertEqual([x.relative_to(fake).as_posix() for x in build_plugin.payload()],['Tools/manager.py'])
        manifest=snapshot_tree(fake/'Tools',fake/'snapshot')
        self.assertEqual(set(manifest),{'manager.py'})

    def test_inventory_distinguishes_missing_installed_and_unsupported(self):
        missing=tools.inventory(self.project,'babel')['tools'][0]
        self.assertEqual(missing['status'],'not_installed')
        package=self.project/'Tools/runtime/node/node_modules/@babel/parser/package.json'
        package.parent.mkdir(parents=True);package.write_text('{"version":"7.28.4"}')
        installed=tools.inventory(self.project,'babel')['tools'][0]
        self.assertEqual(installed['status'],'installed')
        self.assertEqual(installed['observed_version'],'7.28.4')
        self.assertEqual(installed['check']['status'],'not_checked')
        self.assertEqual(tools.inventory(self.project,'ida')['tools'][0]['status'],'unsupported')

    def test_elf_magic_failure_preserves_input_and_evidence(self):
        source=self.project/'fake.so';source.write_bytes(b'not ELF')
        output=self.project/'result.json'
        result=tools.analyze(self.project,'binary-layout',source,output)
        self.assertFalse(result['pass']);self.assertIn('ELF magic',result['error'])
        self.assertEqual(source.read_bytes(),b'not ELF')
        self.assertEqual(json.loads(output.read_text()),result)

    def test_original_and_existing_output_cannot_be_overwritten(self):
        source=self.project/'app.js';source.write_text('const x=1;')
        with self.assertRaisesRegex(ValueError,'overlaps'):
            tools.analyze(self.project,'js-ast',source,source)
        output=self.project/'old.json';output.write_text('old evidence')
        with self.assertRaisesRegex(ValueError,'must be new'):
            tools.analyze(self.project,'js-ast',source,output)
        self.assertEqual(output.read_text(),'old evidence')

    def test_firmware_fixed_scan_flags_stage_original_and_json_result(self):
        source=self.project/'firmware.bin';source.write_bytes(b'original bytes')
        commands=[]
        def fake_run(argv,cwd,**kw):
            commands.append([str(x) for x in argv])
            if '--version' not in argv:
                report=Path(argv[argv.index('--report')+1]);report.write_text('{"format":"gzip"}')
            return {'argv':commands[-1],'exit_code':0,'stdout':'26.6.4','stderr':''}
        executable=self.project/'Tools/runtime/unblob/bin/unblob';executable.parent.mkdir(parents=True);executable.write_bytes(b'fixture adapter')
        with patch.object(tools,'checked_run',side_effect=fake_run):
            result=tools.analyze(self.project,'firmware-scan',source,self.project/'scan.json','unblob')
        self.assertTrue(result['pass'])
        argv=commands[0];self.assertIn('--skip-extraction',argv);self.assertNotIn('--no-sandbox',argv);self.assertNotIn('--force',argv)
        self.assertEqual(argv[argv.index('--depth')+1],'1')
        self.assertNotEqual(Path(argv[-1]),source)
        self.assertEqual(source.read_bytes(),b'original bytes')
        self.assertEqual(result['analysis']['observed_version'],'26.6.4')

    def test_node_lock_has_fixed_versions_integrity_and_no_install_scripts(self):
        lock=json.loads((ROOT/'Tools/node/package-lock.json').read_text())
        for name in ('@babel/parser','@babel/traverse'):
            self.assertEqual(lock['packages']['node_modules/'+name]['version'],'7.28.4')
        self.assertEqual(lock['packages']['node_modules/@wakaru/cli-linux-x64']['version'],'1.13.0')
        for name,record in lock['packages'].items():
            if name:
                self.assertIn('integrity',record)
                self.assertFalse(record.get('hasInstallScript'))

    def start_temporary_service(self):
        state=Path(self.temp.name)/"service-state";state.mkdir()
        atomic_json(state/"config.json",{**DEFAULTS,"min_free_bytes":0})
        log=(Path(self.temp.name)/"service.log").open("wb")
        proc=subprocess.Popen([sys.executable,str(ROOT/"scripts/workbench.py"),"--state",str(state),"serve"],stdout=log,stderr=log)
        def stop():
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
            log.close()
        self.addCleanup(stop)
        for _ in range(100):
            try: rpc(state,"service.capabilities");break
            except WorkbenchError: time.sleep(.03)
        else: self.fail("Temporary managed service failed to start")
        return Client(self.project,state,"tools-test")

    def test_managed_inventory_works_without_dependencies_and_publishes_result(self):
        client=self.start_temporary_service()
        job=client.submit(operation="tools.inventory",args=["inventory","--tool","babel"])
        result=client.wait(job["id"],timeout=15)
        self.assertEqual(result["state"],"succeeded",result)
        record=Path(result["directory"])/"tool-result.json"
        evidence=json.loads(record.read_text())
        self.assertTrue(evidence["pass"])
        self.assertEqual(evidence["tools"][0]["status"],"not_installed")

    def test_managed_check_missing_tool_fails_without_raw_fallback(self):
        client=self.start_temporary_service()
        job=client.submit(operation="tools.check",args=["check","--tool","babel"])
        result=client.wait(job["id"],timeout=15)
        self.assertEqual(result["state"],"failed",result)
        record=json.loads((Path(result["directory"])/"tool-result.json").read_text())
        self.assertFalse(record["pass"])
        self.assertEqual(record["status"],"partial")
        self.assertFalse((self.project/"Tools/runtime").exists())

    def test_managed_static_check_resolves_sibling_source_and_existing_wrapper(self):
        wrapper=self.project/".android-static/bin/jadx";wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!"+sys.executable+"\nprint('fixture jadx version 1.5.6')\n")
        wrapper.chmod(0o755)
        client=self.start_temporary_service()
        job=client.submit(operation="tools.check",args=["check","--tool","jadx"])
        result=client.wait(job["id"],timeout=15)
        self.assertEqual(result["state"],"succeeded",result)
        record=json.loads((Path(result["directory"])/"tool-result.json").read_text())
        self.assertEqual(record["tools"][0]["check"]["status"],"passed")
        self.assertIn("CLI startup",record["tools"][0]["check"]["scope"])

    def test_command_output_is_bounded_and_truncation_cannot_pass(self):
        with self.assertRaisesRegex(RuntimeError,"output truncated|output limit") as context:
            tools.checked_run([sys.executable,"-c","import sys;sys.stdout.write('x'*(3*1024*1024))"],self.project)
        record=context.exception.record
        self.assertTrue(record["stdout_truncated"])
        self.assertLessEqual(len(record["stdout"]),1024*1024)
        self.assertTrue(Path(record["raw_output"]["stdout"]).is_file())

    def test_frida_source_presence_remains_separate_from_runtime_and_install_inputs(self):
        (self.project/".tools/frida-src").mkdir(parents=True)
        record=tools.inventory(self.project,"frida")["tools"][0]
        self.assertEqual(record["status"],"installed")
        self.assertIn("Source checkout only",record["presence_scope"])
        self.assertEqual(record["install"]["operation"],"frida.fetch")
        with self.assertRaisesRegex(ValueError,"explicit --version"):
            tools.install(self.project,"frida")

    def test_rust_lock_has_official_fixed_component_hashes(self):
        lock=json.loads((ROOT/"Tools/rust.lock.json").read_text())
        self.assertEqual(lock["version"],"1.90.0")
        self.assertEqual(set(lock["components"]),{"rustc","cargo","rust-std"})
        for item in lock["components"].values():
            self.assertTrue(item["url"].startswith("https://static.rust-lang.org/dist/2025-09-18/"))
            self.assertRegex(item["sha256"],r"^[0-9a-f]{64}$")

    def test_rust_download_checksum_mismatch_never_becomes_verified_cache(self):
        target=self.project/"cache/archive.tar.xz"
        item={"url":"https://static.rust-lang.org/dist/test.tar.xz","sha256":"0"*64}
        with patch.object(rust.urllib.request,"urlopen",return_value=io.BytesIO(b"bad archive")):
            with self.assertRaisesRegex(RuntimeError,"SHA-256 mismatch"):
                rust.download(item,target)
        self.assertFalse(target.exists())
        self.assertTrue(target.with_suffix(".xz.partial").exists())

    def test_rust_archive_traversal_is_rejected(self):
        archive=self.project/"evil.tar.xz"
        with tarfile.open(archive,"w:xz") as output:
            item=tarfile.TarInfo("../escape/install.sh");data=b"malicious";item.size=len(data)
            output.addfile(item,io.BytesIO(data))
        with self.assertRaises(tarfile.FilterError):
            rust.unpack(archive,self.project/"unpack")
        self.assertFalse((self.project/"escape/install.sh").exists())

    def test_managed_rust_receipt_reuses_verified_executables_without_download(self):
        runtime=self.project/"Tools/runtime";prefix=runtime/"rust/1.90.0"
        (prefix/"bin").mkdir(parents=True)
        for name in ("cargo","rustc"): (prefix/"bin"/name).write_bytes(name.encode())
        receipt={"pass":True,"prefix":str(prefix),"lock_sha256":rust.digest(ROOT/"Tools/rust.lock.json"),
            "executables":{name:rust.digest(prefix/"bin"/name) for name in ("cargo","rustc")}}
        tools.write_json(runtime/"rust-install.json",receipt)
        def fake_run(argv,cwd,**kw):
            return {"argv":[str(x) for x in argv],"exit_code":0,"stdout":"fixture 1.90.0","stderr":""}
        with patch.object(rust,"download",side_effect=AssertionError("must reuse")):
            found,record=rust.ensure(runtime,ROOT/"Tools/rust.lock.json",fake_run,tools.write_json)
        self.assertEqual(found,prefix);self.assertTrue(record["pass"]);self.assertTrue(record["reused_existing"])

    def test_binwalk_install_provisions_rust_and_confines_cargo_cache(self):
        runtime=self.project/"Tools/runtime";prefix=runtime/"rust/1.90.0"
        calls=[]
        def fake_run(argv,cwd,**kw):
            calls.append(([str(x) for x in argv],kw))
            return {"exit_code":0,"stdout":"fixture build succeeded","stderr":""}
        with patch.object(tools,"ensure_rust",return_value=(prefix,{"pass":True})),patch.object(tools,"checked_run",side_effect=fake_run):
            result=tools.install(self.project,"binwalk")
        self.assertTrue(result["pass"])
        argv,options=calls[0]
        self.assertEqual(argv[0],str(prefix/"bin/cargo"));self.assertIn("--locked",argv)
        self.assertEqual(options["env"]["CARGO_HOME"],str(runtime/"cargo-home"))
        self.assertEqual(options["env"]["RUSTC"],str(prefix/"bin/rustc"))
        self.assertEqual(options["env"]["CARGO_BUILD_JOBS"],"2")

    def test_jdk_inventory_uses_installer_state_not_wrapper_or_global_java(self):
        # The managed JDK is an extracted tree, not .android-static/bin/java.
        home=self.project/".android-static/tools/jdk/extracted-jdk"
        java=home/"bin/java";java.parent.mkdir(parents=True);java.write_bytes(b"fixture java")
        state=self.project/".android-static/state/jdk.json";state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"java_home":str(home)}))
        item=tools.inventory(self.project,"jdk")["tools"][0]
        self.assertEqual(item["status"],"installed")
        self.assertEqual(item["path"],str(java))
        state.unlink()
        self.assertEqual(tools.inventory(self.project,"jdk")["tools"][0]["status"],"not_installed")
        for malformed in ("broken JSON", "[]", '{"java_home":null}', '{"java_home":"relative"}'):
            state.write_text(malformed)
            item=tools.inventory(self.project,"jdk")["tools"][0]
            self.assertEqual(item["status"],"not_installed")
            self.assertIsNone(item["path"])
        state.write_text(json.dumps({"java_home":str(home)}));java.unlink()
        self.assertEqual(tools.inventory(self.project,"jdk")["tools"][0]["status"],"not_installed")

    def test_optional_paths_and_platform_limits(self):
        for name in ('ilspy', 'evmole', 'pwntools', 'gdb-pwndbg', 'mitmproxy'):
            item = tools.inventory(self.project, name)['tools'][0]
            self.assertEqual(item['status'], 'not_installed')
            self.assertTrue(Path(item['path']).is_relative_to(self.project/'Tools/runtime'))
        for name in ('hopper', 'macos-runtime'):
            self.assertEqual(tools.inventory(self.project, name)['tools'][0]['status'], 'unsupported')

    def test_optional_archive_checksum_and_traversal_rejected(self):
        optional = tools.optional_module()
        target = self.project/'archive.tar'
        with patch.object(optional.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                optional.download({'url':'https://example.invalid/archive','hash':'0'*64}, target)
        self.assertFalse(target.exists())
        archive = self.project/'escape.tar'
        with tarfile.open(archive, 'w') as output:
            item = tarfile.TarInfo('../escape'); item.size=3
            output.addfile(item, io.BytesIO(b'bad'))
        root = self.project/'runtime'; root.mkdir()
        def copy_download(item, destination): shutil.copyfile(archive, destination)
        import shutil
        with patch.object(optional, 'download', side_effect=copy_download):
            with self.assertRaises(tarfile.FilterError):
                optional.extract({}, root/'gdb-pwndbg', root)
        self.assertFalse((self.project/'escape').exists())

    def test_optional_check_rejects_wrong_version_and_capture_not_started(self):
        optional = tools.optional_module()
        calls=[]
        def fake_run(argv, cwd, **kw):
            calls.append([str(x) for x in argv])
            return {'stdout':'12.2.3', 'stderr':'', 'exit_code':0}
        item={'id':'mitmproxy','version':'12.2.3'}
        result=optional.check(self.project/'Tools/runtime', item, ROOT/'Tools', fake_run, self.project)
        self.assertEqual(result['version'], '12.2.3')
        self.assertEqual(calls[-1][-1], '--version')
        self.assertNotIn('--listen-port', str(calls))
        item['version']='wrong'
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            optional.check(self.project/'Tools/runtime', item, ROOT/'Tools', fake_run, self.project)

    def test_catalog_matches_existing_locked_versions(self):
        lock=json.loads((ROOT/'skills/android-workbench/components/static-env/assets/toolchain.lock.json').read_text())
        entries={x['id']:x for x in tools.catalog()}
        for name in ('jadx','apktool','ghidra','rizin'):
            self.assertEqual(entries[name]['version'],lock['artifacts'][name]['version'])
        self.assertEqual(entries['native-python']['version'],lock['python']['native-python']['version'])
        self.assertEqual(entries['babel']['run_operation'],'tools.js_ast')


if __name__=='__main__':
    unittest.main()
