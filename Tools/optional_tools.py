"""Project-local installers and harmless checks; invoked only by managed manager.py."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import urllib.request


def lock(source):
    return json.loads((source / 'optional.lock.json').read_text())


def extras(source=None):
    import importlib.util
    path = (Path(source) if source else Path(__file__).resolve().parent) / 'static_extras.py'
    spec = importlib.util.spec_from_file_location('awb_static_extras', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def path_for(root, name):
    if name in extras().NAMES: return extras().path_for(root, name)
    if name in ('evmole', 'pwntools'):
        return root / name / 'bin/python'
    if name == 'mitmproxy':
        return root / name / 'bin/mitmdump'
    if name == 'ilspy':
        return root / 'ilspy/ilspycmd'
    return root / 'gdb-pwndbg/pwndbg/bin/pwndbg'


def download(item, destination):
    algorithm = item.get('algorithm', 'sha256')
    def digest(path):
        h = hashlib.new(algorithm)
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
        return h.hexdigest()
    if destination.is_file() and digest(destination) == item['hash']:
        return
    partial = destination.with_suffix(destination.suffix + '.partial')
    with urllib.request.urlopen(item['url'], timeout=60) as response, partial.open('wb') as target:
        shutil.copyfileobj(response, target)
    if digest(partial) != item['hash']:
        raise RuntimeError('Official archive checksum mismatch: ' + item['url'])
    partial.replace(destination)


def extract(item, target, root):
    archive = root / (target.name + '-archive.tar')
    download(item, archive)
    staged = root / (target.name + '-staging')
    if staged.exists(): shutil.rmtree(staged)
    staged.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(staged, filter='data')
    if target.exists():
        raise RuntimeError('Existing incomplete archive environment preserved: ' + str(target))
    staged.replace(target)


def dotnet_env(root):
    return {**os.environ, 'DOTNET_ROOT': str(root / 'dotnet'),
            'DOTNET_CLI_HOME': str(root / 'dotnet-home'),
            'NUGET_PACKAGES': str(root / 'nuget-packages'),
            'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_SKIP_FIRST_TIME_EXPERIENCE': '1',
            'DOTNET_NOLOGO': '1', 'DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE': 'true'}


def install(root, item, source, run, commands):
    name = item['id']
    if name in extras(source).NAMES:
        import types
        api = types.SimpleNamespace(lock=lock, download=download, dotnet_env=dotnet_env)
        return extras(source).install(root, item, source, run, commands, api)
    if name in ('evmole', 'pwntools', 'mitmproxy'):
        target = root / name
        python = target / 'bin/python'
        if not python.exists():
            import sys
            commands.append(run([sys.executable, '-m', 'venv', target], root))
        commands.append(run([python, '-m', 'pip', 'install', '--disable-pip-version-check',
                             '--report', root / (name + '-pip-report.json'),
                             name + '==' + item['version']], root, timeout=900))
        commands.append(run([python, '-m', 'pip', 'freeze'], root))
        (root / (name + '-requirements.txt')).write_text(commands[-1]['stdout'])
    elif name == 'gdb-pwndbg':
        target = root / name
        if not path_for(root, name).exists():
            extract(lock(source)[name], target, root)
    elif name == 'ilspy':
        sdk = root / 'dotnet/dotnet'
        if not sdk.exists(): extract(lock(source)['dotnet'], root / 'dotnet', root)
        environment = dotnet_env(root)
        identity = run([sdk, '--version'], root, env=environment)
        commands.append(identity)
        if identity['stdout'].strip() != lock(source)['dotnet']['version']:
            raise RuntimeError('Existing .NET SDK differs from pinned version; preserved')
        if not path_for(root, name).exists():
            commands.append(run([sdk, 'tool', 'install', 'ilspycmd', '--version', item['version'],
                                 '--tool-path', root / 'ilspy', '--no-cache'], root, timeout=900, env=environment))
    else:
        raise ValueError('Unknown optional tool: ' + name)
    evidence = check(root, item, source, run, root)
    commands.extend(evidence['commands'])
    return {'checks': commands, 'observed_version': evidence['version'],
            'verification_scope': evidence['scope'], 'source_lock_sha256': hashlib.sha256((source/'optional.lock.json').read_bytes()).hexdigest()}


def check(root, item, source, run, cwd):
    name = item['id']
    if name in extras(source).NAMES:
        import types
        return extras(source).check(root,item,source,run,cwd,types.SimpleNamespace(dotnet_env=dotnet_env,lock=lock))
    env = {**os.environ, 'TERM': 'dumb', 'PWNLIB_NOTERM': '1',
           'PWNLIB_UPDATE_CHECK_INTERVAL': 'never', 'DEBUGINFOD_URLS': ''}
    version_code = "import importlib.metadata as m; print(m.version(%r))" % name
    commands = []
    scope = 'CLI/import and fixed harmless synthetic function check; no target, device or network capture'
    if name in ('evmole', 'pwntools', 'mitmproxy'):
        python = root / name / 'bin/python'
        commands.append(run([python, '-c', version_code], cwd, env=env))
        version = commands[0]['stdout'].strip()
        code = {
            'evmole': "import evmole; assert evmole.contract_info('0x00', selectors=True).functions == []; print('empty STOP selectors: passed')",
            'pwntools': "from pwnlib.util.packing import p32,u32; assert u32(p32(42)) == 42; from pwnlib.util.cyclic import cyclic,cyclic_find; assert cyclic_find(cyclic(32)[12:16]) == 12; print('packing/cyclic: passed')",
            'mitmproxy': "from mitmproxy import http; r=http.Request.make('GET','https://example.invalid/'); assert r.host=='example.invalid'; print('offline HTTP request: passed')",
        }[name]
        commands.append(run([python, '-c', code], cwd, env=env))
        if name == 'mitmproxy': commands.append(run([path_for(root, name), '--version'], cwd, env=env))
    elif name == 'gdb-pwndbg':
        commands.append(run([path_for(root,name), '-nx', '-batch', '-ex', 'set debuginfod enabled off',
                             '-ex', 'show version', '-ex', 'version'], cwd, env=env))
        text = commands[0]['stdout'] + commands[0]['stderr']
        if item['version'] not in text or 'Pwndbg' not in text:
            raise RuntimeError('Pwndbg startup/version command did not confirm pinned version')
        version = item['version']
        scope = 'Bundled GDB and Pwndbg batch startup/version; no inferior, attach, core or device'
    else:
        env = dotnet_env(root)
        commands.append(run([path_for(root, name), '--version'], cwd, env=env))
        text = commands[0]['stdout']
        if item['version'] not in text: raise RuntimeError('ILSpy version differs from pinned version')
        version = item['version']
        scope = 'ILSpy CLI and .NET startup only; no real .NET assembly compatibility claim'
    if version != item['version']: raise RuntimeError('Installed version differs from pinned version')
    return {'commands': commands, 'version': version, 'scope': scope}
