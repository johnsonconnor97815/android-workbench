#!/usr/bin/env python3
"""Fixed offline tool adapters. Every CLI action requires the shared Workbench queue."""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parent
STATIC = SOURCE.parent / 'skills/android-workbench/components/static-env'
MAX_INPUT = 64 * 1024 * 1024


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def catalog():
    return json.loads((SOURCE / 'catalog.json').read_text())['tools']


def checked_run(argv, cwd, timeout=120, limited=False, env=None):
    argv = [str(x) for x in argv]
    if limited:
        limiter = shutil.which('prlimit')
        if not limiter:
            raise RuntimeError('prlimit missing; bounded tool execution cannot proceed')
        argv = [limiter, '--as=4294967296', '--cpu=90', '--fsize=134217728', '--nofile=256', '--', *argv]
    # Spool directly to disk; never collect arbitrary stdout in Python memory.
    # Files remain in the declared artifact/runtime directory for failure review.
    cwd = Path(cwd)
    stdout = tempfile.NamedTemporaryFile(prefix="command-", suffix=".stdout.txt", dir=cwd, delete=False)
    stderr = tempfile.NamedTemporaryFile(prefix="command-", suffix=".stderr.txt", dir=cwd, delete=False)
    failure = None
    proc = None
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                failure = "Tool wall-clock limit exceeded"
                break
            if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > 2 * 1024 * 1024:
                failure = "Tool output limit exceeded"
                break
            time.sleep(.02)
        if failure:
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        else:
            proc.wait()
    finally:
        stdout.close(); stderr.close()
    output_paths = {"stdout": Path(stdout.name), "stderr": Path(stderr.name)}
    record = {"argv": argv, "exit_code": proc.returncode,
              "raw_output": {name: str(path) for name, path in output_paths.items()}}
    for name, path in output_paths.items():
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        record[name] = data[:1024 * 1024].decode("utf-8", errors="replace")
        record[name + "_truncated"] = len(data) > 1024 * 1024
        record[name + "_bytes"] = path.stat().st_size
    if failure or proc.returncode or record["stdout_truncated"] or record["stderr_truncated"]:
        error = RuntimeError(failure or "Tool command failed or output truncated: " + record["stderr"][-2000:])
        error.record = record
        error.cleanup_unknown = bool(failure)
        raise error
    return record


def runtime(workspace):
    return Path(workspace) / 'Tools/runtime'


def static_module():
    spec = importlib.util.spec_from_file_location('awb_static_setup', STATIC / 'scripts/setup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def static_args(workspace, tool, action='check', accept_sdk_licenses=False):
    return argparse.Namespace(action=action, workspace=str(workspace), profile='full', only=tool,
        with_mobsf=False, mobsf_image=None, cache_dir=None, install_system_deps=False,
        accept_sdk_licenses=accept_sdk_licenses)


def path_for(workspace, item):
    name, kind = item['id'], item['kind']
    root = Path(workspace)
    if kind == 'optional':
        return optional_module().path_for(runtime(root), name)
    if kind == 'node':
        return runtime(root) / 'node/node_modules' / ('@babel/parser/package.json' if name == 'babel' else '@wakaru/cli-linux-x64/wakaru')
    if kind == 'cargo':
        return runtime(root) / 'binwalk/bin/binwalk'
    if kind == 'rust':
        return runtime(root) / 'rust/1.90.0/bin/rustc'
    if kind == 'python':
        return runtime(root) / 'unblob/bin/unblob'
    if kind == 'frida':
        return root / '.tools/frida-src'
    if kind == 'catalog-only':
        return None
    if name == 'jdk':
        state = root / '.android-static/state/jdk.json'
        try:
            data = json.loads(state.read_text())
            home = data.get('java_home') if isinstance(data, dict) else None
            if not isinstance(home, str) or not home:
                return None
            java_home = Path(home).expanduser()
            if not java_home.is_absolute():
                return None
            return java_home / 'bin/java'
        except (OSError, ValueError):
            return None
    if name == 'sdk':
        return root / '.android-static/sdk'
    if name == 'utilities':
        return Path(shutil.which('file') or '/nonexistent')
    return root / '.android-static/bin' / {'ghidra':'analyzeHeadless','dex2jar':'d2j-dex2jar','native-python':'native-python','jdk':'java'}.get(name, name)


def inventory(workspace, only=None):
    static_manifest = Path(workspace) / '.android-static/manifest.json'
    previous = json.loads(static_manifest.read_text()) if static_manifest.is_file() else {}
    records = []
    for item in catalog():
        if only and item['id'] != only:
            continue
        location = path_for(workspace, item)
        supported = item['kind'] != 'catalog-only' and platform.system() == 'Linux' and platform.machine() in ('x86_64','AMD64')
        present = location is not None and location.exists()
        record = {**item, 'status': 'installed' if present and supported else 'not_installed' if supported else 'unsupported',
            'path': str(location) if location else None, 'observed_version': 'unknown', 'check': {'status':'not_checked'},
            'install': {'operation':'tools.install','args':['install','--tool',item['id']]} if supported else None}
        if item['kind'] == 'node' and present:
            package = runtime(workspace) / 'node/node_modules' / ('@babel/parser/package.json' if item['id']=='babel' else '@wakaru/cli/package.json')
            if package.is_file(): record['observed_version'] = json.loads(package.read_text()).get('version','unknown')
        if item['kind'] == 'frida':
            record['install'] = {'operation':'frida.fetch','required_args':['--version','--kind source|server|gadget','--destination','--record','--abi (binary only)']}
            record['presence_scope'] = 'Source checkout only; no host/device runtime verification.'
        if item['kind'] == 'static-env':
            record['previous_install_check'] = previous.get('results',{}).get(item['id'])
            record['previous_install_check_at'] = previous.get('timestamp_utc')
            record['previous_install_check_scope'] = 'Historical startup checks; not a current functional verification.'
        records.append(record)
    if only and not records:
        raise ValueError('Unknown fixed tool: ' + only)
    return {'schema':1, 'pass':True, 'workspace':str(Path(workspace).resolve()), 'tools':records,
        'storage': {'new':str(runtime(workspace)), 'reused':[str(Path(workspace)/'.android-static'),str(Path(workspace)/'.tools')]},
        'scope':'Presence inventory only. Installed does not mean verified functional.'}


def input_file(value, output):
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise ValueError('Input must be an existing regular file of at most 64 MiB')
    if output and (path == Path(output).resolve() or path.is_relative_to(Path(str(output)+'.artifacts').resolve())):
        raise ValueError('Input overlaps output; original input must remain read-only')
    return path


def analyze(workspace, action, value, output, tool=None):
    source = input_file(value, output)
    if not output:
        raise ValueError('--output required for analysis')
    out = Path(output).expanduser().resolve()
    artifacts = Path(str(out) + '.artifacts')
    if artifacts.exists() or out.exists():
        raise ValueError('Output and artifact directory must be new; existing evidence is never overwritten')
    out.parent.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir()
    result = {'schema':1,'status':'failed','operation':'tools.'+action.replace('-','_'),
        'input':{'path':str(source),'sha256':sha(source),'size':source.stat().st_size},
        'artifacts':str(artifacts),'checks':[],'cleanup':{'status':'complete','scope':'No target process or device was started.'}}
    try:
        if action == 'js-ast':
            if source.suffix.lower() not in {'.js','.mjs','.cjs','.jsx','.ts','.tsx'}:
                raise ValueError('JS AST accepts JS/JSX/TypeScript text, not Hermes bytecode')
            command = [shutil.which('node') or 'node', SOURCE/'adapters/js_ast.cjs',runtime(workspace)/'node',source]
            record = checked_run(command, artifacts, limited=True)
            result['analysis'] = json.loads(record['stdout'])
        elif action == 'js-deobfuscate':
            if source.suffix.lower() not in {'.js','.mjs','.cjs'}:
                raise ValueError('Wakaru adapter accepts .js/.mjs/.cjs only')
            executable = runtime(workspace)/'node/node_modules/@wakaru/cli-linux-x64/wakaru'
            recovered = artifacts/'recovered.js'
            record = checked_run([executable,source,'-o',recovered,'--json'],artifacts,limited=True)
            if not recovered.is_file() or not recovered.stat().st_size:
                raise RuntimeError('Wakaru did not produce readable source')
            result['analysis'] = {'tool':'wakaru','version':package_version(workspace,'wakaru'),
                'executable_sha256':sha(executable),'recovered_source':str(recovered),'sha256':sha(recovered),
                'limitations':['Readable rewrites do not prove original source recovery or behavioral equivalence.']}
        elif action == 'binary-layout':
            if source.read_bytes()[:4] != b'\x7fELF':
                raise ValueError('Binary layout requires ELF magic; filename alone is not evidence')
            python = Path(workspace)/'.android-static/venvs/native-python/bin/python'
            record = checked_run([python,SOURCE/'adapters/elf_layout.py',source],artifacts,limited=True)
            result['analysis'] = json.loads(record['stdout'])
        elif action == 'firmware-scan':
            if tool not in {'binwalk','unblob'}:
                raise ValueError('--tool must be binwalk or unblob')
            # Stage the bytes so upstream carving/symlinks cannot touch the original input.
            staged = artifacts/'input.bin'
            shutil.copyfile(source, staged)
            staged.chmod(0o400)
            report = artifacts/'upstream.json'
            if tool == 'binwalk':
                argv = [runtime(workspace)/'binwalk/bin/binwalk','--threads','1','--log',report,staged]
            else:
                argv = [runtime(workspace)/'unblob/bin/unblob','--skip-extraction','--depth','1','--process-num','1',
                    '--randomness-depth','0','--extract-dir',artifacts/'carved','--report',report,'--log',artifacts/'unblob.log',staged]
            record = checked_run(argv,artifacts,limited=True)
            if not report.is_file(): raise RuntimeError('Scanner did not produce JSON report')
            data = json.loads(report.read_text())
            identity = checked_run([argv[0],'--version'],artifacts)
            result['checks'].append(identity)
            result['analysis'] = {'tool':tool,'observed_version':identity['stdout'].strip(),'executable_sha256':sha(argv[0]),'report':str(report),'report_sha256':sha(report),'result':data,
                'limits':{'input_bytes':MAX_INPUT,'depth':1,'workers':1,'file_bytes':134217728,'address_bytes':4294967296,'cpu_seconds':90,'wall_seconds':120},
                'limitations':['Signature recognition is not full firmware reconstruction.', 'Unblob may carve bytes; external extraction and target execution are disabled.']}
        else:
            raise ValueError('Unknown fixed analysis action')
        result['checks'].append(record)
        if sha(source) != result['input']['sha256']:
            raise RuntimeError('Original input changed during analysis')
        result['status'] = 'success'
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        result['error'] = str(error)
        if hasattr(error,'record'): result['checks'].append(error.record)
        result['cleanup']['status'] = 'unknown' if isinstance(error,subprocess.TimeoutExpired) or getattr(error,'cleanup_unknown',False) else 'complete'
    result['pass'] = result['status']=='success'
    write_json(out,result)
    return result


def package_version(workspace, tool):
    package = runtime(workspace)/'node/node_modules' / ('@babel/parser/package.json' if tool=='babel' else '@wakaru/cli/package.json')
    return json.loads(package.read_text())['version'] if package.is_file() else 'unknown'


def optional_module():
    spec = importlib.util.spec_from_file_location('awb_optional_tools', SOURCE / 'optional_tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_rust(workspace):
    spec = importlib.util.spec_from_file_location('awb_rust_toolchain', SOURCE / 'rust_toolchain.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ensure(runtime(workspace), SOURCE / 'rust.lock.json', checked_run, write_json)


def install(workspace, name, accept_sdk_licenses=False):
    item = next((x for x in catalog() if x['id']==name),None)
    if item is None: raise ValueError('Unknown fixed tool: '+name)
    if item['kind']=='frida': raise ValueError('Use registered frida.fetch with explicit --version --kind --destination --record and --abi for binaries; Tools does not guess these inputs')
    if item['kind']=='catalog-only': raise ValueError('Unsupported in this profile: '+name)
    if platform.system()!='Linux' or platform.machine() not in ('x86_64','AMD64'):
        raise ValueError('Installer profile supports Linux x86_64 only')
    root = runtime(workspace)
    root.mkdir(parents=True,exist_ok=True)
    result = {'schema':1,'tool':name,'requested_version':item['version'],'status':'failed','checks':[]}
    try:
        if item['kind']=='optional':
            result.update(optional_module().install(root, item, SOURCE, checked_run, result['checks']))
        elif item['kind']=='node':
            target = root/'node'
            target.mkdir(exist_ok=True)
            expected = (SOURCE/'node/package-lock.json').read_bytes()
            if (target/'package-lock.json').is_file() and (target/'package-lock.json').read_bytes()!=expected:
                raise RuntimeError('Existing Node lock differs; preserve and review before upgrading')
            for file in ('package.json','package-lock.json'): shutil.copyfile(SOURCE/'node'/file,target/file)
            if package_version(workspace,'babel')=='7.28.4' and package_version(workspace,'wakaru')=='1.13.0' and (target/'node_modules/@wakaru/cli-linux-x64/wakaru').is_file():
                result['reused_existing'] = True
            else:
                result['checks'].append(checked_run(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],target,timeout=900))
            result['lock_sha256']=sha(target/'package-lock.json')
        elif item['kind']=='rust':
            prefix,receipt=ensure_rust(workspace)
            result['dependency_receipt']=receipt
        elif item['kind']=='cargo':
            prefix,receipt=ensure_rust(workspace)
            result['dependency_receipt']=receipt
            cargo_env={**os.environ,'PATH':str(prefix/'bin')+os.pathsep+os.environ.get('PATH',os.defpath),
                'CARGO_HOME':str(root/'cargo-home'),'RUSTC':str(prefix/'bin/rustc'),'CARGO_BUILD_JOBS':'2'}
            result['checks'].append(checked_run([prefix/'bin/cargo','install','binwalk','--version',item['version'],'--locked','--root',root/'binwalk'],root,timeout=3600,env=cargo_env))
        elif item['kind']=='python':
            target=root/'unblob'
            if not (target/'bin/python').is_file():
                result['checks'].append(checked_run([sys.executable,'-m','venv',target],root))
            result['checks'].append(checked_run([target/'bin/python','-m','pip','install','--disable-pip-version-check','--report',root/'unblob-pip-report.json','unblob=='+item['version']],root,timeout=900))
            result['checks'].append(checked_run([target/'bin/python','-m','pip','freeze'],root))
            (root/'unblob-requirements.txt').write_text(result['checks'][-1]['stdout'])
        else:
            module=static_module()
            # Reuse its pinned downloads, ownership checks and manifests; never apt or accept licenses implicitly.
            stream=io.StringIO()
            with contextlib.redirect_stdout(stream):
                code=module.Environment(static_args(workspace,name,'install',accept_sdk_licenses)).execute()
            result['checks'].append({'adapter':'existing-static-installer','exit_code':code,'stdout':stream.getvalue()})
            if code: raise RuntimeError('Existing static installer failed; inspect its manifest/logs')
        result['status']='success'
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        result['error']=str(error)
        if hasattr(error,'record'): result['checks'].append(error.record)
    result['pass'] = result['status']=='success'
    write_json(root/(name+'-install.json'),result)
    return result


def check(workspace, only=None, output=None):
    result=inventory(workspace,only)
    with tempfile.TemporaryDirectory(prefix='awb-tools-check-') as tmp:
        for item in result['tools']:
            if item['status']!='installed': continue
            name=item['id']
            try:
                if name in ('babel','wakaru','native-python','binwalk','unblob'):
                    out=Path(tmp)/(name+'.json')
                    if name=='babel': action,value,tool='js-ast',SOURCE/'fixtures/scope.js',None
                    elif name=='wakaru': action,value,tool='js-deobfuscate',SOURCE/'fixtures/minified.js',None
                    elif name=='native-python': action,value,tool='binary-layout',Path(sys.executable).resolve(),None
                    else:
                        import gzip
                        value=Path(tmp)/(name+'.bin'); value.write_bytes(b'FIXTURE'+gzip.compress(b'Workbench signature fixture\n',mtime=0))
                        action,tool='firmware-scan',name
                    evidence=analyze(workspace,action,value,out,tool)
                    if evidence['status']!='success': raise RuntimeError(evidence.get('error','Functional check failed'))
                    if name=='babel':
                        analysis=evidence['analysis']
                        if not {'load','nested'} <= {f['name'] for f in analysis['functions']} or len([b for b in analysis['bindings'] if b['name']=='value'])!=2:
                            raise RuntimeError('AST fixture lost function/binding scope')
                    if name in ('binwalk','unblob') and 'gzip' not in json.dumps(evidence['analysis']['result']).lower():
                        raise RuntimeError('Scanner did not recognize fixed gzip fixture')
                    item['check']={'status':'passed','scope':'Fixed synthetic sample function test','evidence':evidence}
                    # Scratch paths no longer exist after check; persist source/check content, not claims of retained artifacts.
                    item['check']['evidence_retention']='Embedded JSON retained; scratch files removed after check.'
                    if name=='native-python': item['observed_version']=evidence['analysis']['version']
                elif item['kind']=='optional':
                    evidence=optional_module().check(runtime(workspace),item,SOURCE,checked_run,tmp)
                    item['observed_version']=evidence['version']
                    item['check']={'status':'passed','scope':evidence['scope'],'commands':evidence['commands']}
                elif item['kind']=='rust':
                    prefix=runtime(workspace)/'rust'/item['version']
                    fixture=Path(tmp)/'fixture.rs';fixture.write_text('fn main() { let _value: u64 = 42; }\n')
                    records=[checked_run([prefix/'bin/rustc','--version'],tmp),checked_run([prefix/'bin/cargo','--version'],tmp),
                        checked_run([prefix/'bin/rustc',fixture,'-o',Path(tmp)/'fixture'],tmp)]
                    item['observed_version']=records[0]['stdout'].strip()
                    item['check']={'status':'passed','scope':'Fixed Rust source compile; resulting executable was not run','commands':records}
                elif item['kind']=='frida':
                    record=checked_run(['git','-C',Path(item['path']),'rev-parse','HEAD'],tmp)
                    item['observed_commit']=record['stdout'].strip()
                    item['check']={'status':'passed','scope':'Source commit provenance only; no host/device runtime test','commands':[record]}
                else:
                    module=static_module()
                    env=module.Environment(static_args(workspace,name))
                    env.run_dir=Path(tmp)/('static-'+name); env.run_dir.mkdir()
                    records=env.checks(name)
                    item['check']={'status':'passed','scope':'CLI startup/import only; use static.smoke for sample function checks','commands':records}
            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
                item['check']={'status':'failed','error':str(error)}
    result['status']='success' if all(x['status']=='installed' and x['check']['status']=='passed' for x in result['tools']) else 'partial'
    result['pass'] = result['status']=='success'
    if output: write_json(output,result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['inventory','check','install','js-ast','js-deobfuscate','binary-layout','firmware-scan'])
    parser.add_argument('--workspace',default='.')
    parser.add_argument('--tool')
    parser.add_argument('--input')
    parser.add_argument('--output')
    parser.add_argument('--accept-sdk-licenses',action='store_true')
    args=parser.parse_args()
    workspace=Path(args.workspace).expanduser().resolve()
    config=workspace/'workbench.project.json'
    if not config.is_file(): parser.error('Workspace is not registered; use configure_project.py first. No raw fallback.')
    if args.output and Path(args.output).expanduser().resolve().exists():
        parser.error('Explicit result output already exists; use a new evidence path')
    doc=json.loads(config.read_text()); checkout=(workspace/doc.get('workbench_root','android-workbench')).resolve()
    sys.path.insert(0,str(checkout))
    os.chdir(workspace)
    from workbench.bridge import entry
    entry(__file__)
    try:
        if args.action=='inventory':
            result=inventory(workspace,args.tool)
            if args.output: write_json(args.output,result)
        elif args.action=='check': result=check(workspace,args.tool,args.output)
        elif args.action=='install':
            if not args.tool: raise ValueError('--tool required')
            result=install(workspace,args.tool,args.accept_sdk_licenses)
            if args.output: write_json(args.output,result)
        else:
            if not args.input: raise ValueError('--input required')
            result=analyze(workspace,args.action,args.input,args.output,args.tool)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result.get('status') not in ('failed','partial') else 1
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        result={'schema':1,'status':'failed','error':str(error)}
        if args.output: write_json(args.output,result)
        print(json.dumps(result,ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
