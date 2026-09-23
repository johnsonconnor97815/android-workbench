#!/usr/bin/env python3
"""Prepare a project-local Android static-analysis toolchain (Linux x86_64)."""
# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == '__main__':
    import sys as _awb_sys
    from pathlib import Path as _AwbPath
    _awb_project = None
    for _awb_origin in (_AwbPath.cwd(),):
        for _awb_root in (_awb_origin, *_awb_origin.parents):
            if (_awb_root / 'workbench.project.json').is_file():
                _awb_project = _awb_root
                break
        if _awb_project is not None:
            break
    if _awb_project is not None:
        import json as _awb_json
        _awb_config = _awb_json.loads((_awb_project / 'workbench.project.json').read_text())
        _awb_runtime = (_awb_project / _awb_config.get('workbench_root', 'android-workbench')).resolve()
        if not (_awb_runtime / 'workbench/bridge.py').is_file():
            raise SystemExit('Android Workbench runtime missing; managed entry cannot run directly')
        _awb_sys.path.insert(0, str(_awb_runtime))
        from workbench.bridge import entry as _awb_entry
        _awb_entry(__file__)
# END ANDROID WORKBENCH ENTRY

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

COMPONENT = Path(__file__).resolve().parents[1]
CATALOG = COMPONENT / 'assets/toolchain.lock.json'
CORE = ['jdk', 'sdk', 'jadx', 'apktool', 'smali', 'bundletool', 'androguard']
FULL = CORE + ['droidasc', 'dex2jar', 'apkid', 'quark', 'semgrep', 'rizin', 'ghidra', 'native-python', 'utilities']
DEPS = {name: ['jdk'] for name in ['sdk', 'jadx', 'apktool', 'smali', 'bundletool', 'dex2jar', 'ghidra']}
UTILITY_CHECKS = {
    'git': ['--version'], 'curl': ['--version'], 'unzip': ['-v'], 'zip': ['-v'],
    '7z': [], 'jq': ['--version'], 'rg': ['--version'], 'file': ['--version'],
    'readelf': ['--version'], 'objdump': ['--version'], 'strings': ['--version'],
    'llvm-objdump': ['--version'], 'llvm-readobj': ['--version'], 'nm': ['--version'], 'c++filt': ['--version'], 'checksec': ['--version'], 'openssl': ['version'], 'yara': ['--version'], 'dot': ['-V'],
}


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def read_json(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def select_tools(args):
    selected = args.only.split(',') if args.only else list(CORE if args.profile == 'core' else FULL)
    if args.with_mobsf:
        selected.append('mobsf')
    valid = set(FULL + ['mobsf'])
    if not set(selected) <= valid:
        raise ValueError('Unknown tools: ' + ', '.join(sorted(set(selected) - valid)))
    for name in list(selected):
        selected.extend(DEPS.get(name, []))
    return [name for name in FULL + ['mobsf'] if name in selected]


class Environment:
    def __init__(self, args):
        self.args = args
        self.catalog = read_json(CATALOG)
        self.workspace = Path(args.workspace).expanduser().resolve()
        self.root = self.workspace / '.android-static'
        self.cache = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else self.root / 'cache'
        self.selected = select_tools(args)
        self.system_packages = self.catalog['system_packages'] if 'utilities' in self.selected else self.catalog['base_system_packages']
        self.artifacts = {}
        self.run_dir = self.root / 'logs' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
        self.log_index = 0

    def prepare(self):
        marker = self.root / 'owner.json'
        if self.root.is_symlink():
            raise RuntimeError(f'Refusing a symlink install root: {self.root}')
        if self.root.exists() and not marker.exists():
            raise RuntimeError(f'Existing directory is not owned by this installer: {self.root}')
        if marker.exists() and read_json(marker).get('skill') != 'android-static-env':
            raise RuntimeError(f'Unexpected ownership marker: {marker}')
        self.root.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            write_json(marker, {'skill': 'android-static-env', 'created_at': stamp()})
        for name in ['bin', 'tools', 'venvs', 'state', 'locks', 'sdk']:
            (self.root / name).mkdir(exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True)
        self.artifacts = read_json(self.root / 'locks/downloads.json', {})

    def env(self):
        env = dict(os.environ)
        paths = [self.root / 'bin']
        java = read_json(self.root / 'state/jdk.json', {})
        if java.get('java_home'):
            env['JAVA_HOME'] = java['java_home']
            paths.append(Path(java['java_home']) / 'bin')
        sdk_bin = self.root / 'sdk/cmdline-tools/21.0/bin'
        paths += [sdk_bin, self.root / 'sdk/build-tools' / self.catalog['sdk_build_tools']]
        env.update(ANDROID_HOME=str(self.root / 'sdk'), ANDROID_SDK_ROOT=str(self.root / 'sdk'),
                   PATH=os.pathsep.join(map(str, paths)) + os.pathsep + env.get('PATH', ''),
                   SEMGREP_SEND_METRICS='off', SEMGREP_ENABLE_VERSION_CHECK='0',
                   PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONNOUSERSITE='1',
                   XDG_CONFIG_HOME=str(self.root / 'config'), XDG_CACHE_HOME=str(self.root / 'cache/xdg'))
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        return env

    def run(self, argv, *, input_text=None, timeout=180, ok=(0,)):
        self.log_index += 1
        log = self.run_dir / f'{self.log_index:03d}-{Path(str(argv[0])).name}.log'
        with log.open('w') as output:
            output.write('$ ' + shlex.join(map(str, argv)) + '\n')
            output.flush()
            result = subprocess.run(list(map(str, argv)), env=self.env(), cwd=self.workspace,
                                    input=input_text, text=True, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout, stdin=None if input_text is not None else subprocess.DEVNULL)
        text = log.read_text(errors='replace')
        if result.returncode not in ok:
            raise RuntimeError(f'exit {result.returncode}: {shlex.join(map(str, argv))}\n{text[-2200:]}\nLog: {log}')
        return {'argv': list(map(str, argv)), 'exit_code': result.returncode, 'log': str(log), 'output': text[-3000:]}

    def fetch(self, name, recipe):
        url = recipe['url']
        if not url.startswith('https://'):
            raise RuntimeError(f'HTTPS is required: {url}')
        filename = url.rsplit('/', 1)[-1]
        target = self.cache / (name + '-' + filename)
        previous = self.artifacts.get(name)
        if previous and previous['url'] != url:
            raise RuntimeError(f'{name}: download lock differs; use a new workspace or review the lock explicitly')
        checksum = recipe.get('checksum')
        if not target.exists():
            print(f'DOWNLOAD {name}: {url}', flush=True)
            part = target.with_name(target.name + '.part')
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'android-static-env/1'})
                    with urllib.request.urlopen(req, timeout=60) as response, part.open('wb') as output:
                        shutil.copyfileobj(response, output, 1024 * 1024)
                    part.replace(target)
                    break
                except (OSError, urllib.error.URLError):
                    part.unlink(missing_ok=True)
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
        if recipe.get('size') and target.stat().st_size != recipe['size']:
            raise RuntimeError(f'{name}: size mismatch; cached file preserved for inspection: {target}')
        if checksum:
            algorithm, expected = checksum.split(':', 1)
            if digest(target, algorithm) != expected:
                raise RuntimeError(f'{name}: {algorithm} mismatch: {target}')
        sha = digest(target)
        if previous and previous['sha256'] != sha:
            raise RuntimeError(f'{name}: SHA-256 differs from the recorded download lock: {target}')
        self.artifacts[name] = {
            'url': url, 'version': recipe.get('version'), 'sha256': sha,
            'upstream_checksum': checksum, 'source': recipe.get('source'),
            'verification': 'upstream-checksum' if checksum else 'first-download-hash-only',
            'size': target.stat().st_size, 'path': str(target),
        }
        write_json(self.root / 'locks/downloads.json', self.artifacts)
        return target

    def extract(self, archive, destination):
        destination.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                for entry in z.infolist():
                    path = destination / entry.filename
                    if not path.resolve().is_relative_to(destination.resolve()):
                        raise RuntimeError(f'Unsafe ZIP entry: {entry.filename}')
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise RuntimeError(f'ZIP symlink rejected: {entry.filename}')
                    z.extract(entry, destination)
                    if mode & 0o111 and path.is_file():
                        path.chmod(0o755)
        else:
            with tarfile.open(archive) as t:
                t.extractall(destination, filter='data')

    def unpack(self, name, recipe=None):
        recipe = recipe or self.catalog['artifacts'][name]
        archive = self.fetch(name, recipe)
        target = self.root / 'tools' / name
        marker = target / '.artifact.json'
        if marker.exists():
            if read_json(marker)['sha256'] != self.artifacts[name]['sha256']:
                raise RuntimeError(f'{name}: installed artifact differs from the lock')
            return target
        if target.exists():
            raise RuntimeError(f'Incomplete or unmanaged install: {target}; inspect it before retrying')
        with tempfile.TemporaryDirectory(prefix='unpack-', dir=self.root / 'tools') as stage:
            stage = Path(stage)
            if archive.suffix == '.jar':
                shutil.copy2(archive, stage / 'tool.jar')
            else:
                self.extract(archive, stage)
            write_json(stage / '.artifact.json', self.artifacts[name])
            stage.rename(target)
        return target

    def wrapper(self, name, command):
        target = self.root / 'bin' / name
        target.write_text('#!/bin/sh\n. ' + shlex.quote(str(self.root / 'env.sh')) + '\nexec ' + shlex.join(list(map(str, command))) + ' "$@"\n')
        target.chmod(0o755)

    def find_one(self, root, pattern):
        hits = list(root.glob(pattern))
        if len(hits) != 1:
            raise RuntimeError(f'Expected one {pattern} in {root}, found {len(hits)}')
        return hits[0]

    def activation(self):
        env = self.env()
        variables = ['ANDROID_HOME', 'ANDROID_SDK_ROOT', 'SEMGREP_SEND_METRICS', 'SEMGREP_ENABLE_VERSION_CHECK']
        if env.get('JAVA_HOME'):
            variables.append('JAVA_HOME')
        prefix = env['PATH'][:-(len(os.environ.get('PATH', '')) + 1)]
        lines = ['# Generated by android-static-env. Source after any existing project .envrc.']
        lines += ['export ' + name + '=' + shlex.quote(env[name]) for name in variables]
        lines += ['case ":${PATH}:" in', '  *' + shlex.quote(':' + prefix + ':') + '*) ;;',
                  '  *) export PATH=' + shlex.quote(prefix) + ':"$PATH" ;;', 'esac']
        (self.root / 'env.sh').write_text('\n'.join(lines) + '\n')

    def install_python(self, name):
        spec = self.catalog['python'][name]
        venv = self.root / 'venvs' / name
        py = venv / 'bin/python'
        lock = self.root / 'locks' / (name + '-requirements.txt')
        complete = self.root / 'state' / (name + '.json')
        if complete.exists() and read_json(complete).get('version') != spec['version']:
            raise RuntimeError(f'{name}: version change requires a new environment, preserve the existing lock')
        needs_install = not complete.exists() or not py.exists()
        if not py.exists():
            self.run([sys.executable, '-m', 'venv', venv], timeout=120)
        if needs_install:
            command = [py, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', '--retries', '2', '--timeout', '45', '--report', self.root / 'locks' / (name + '-pip-report.json')]
            command += ['--require-hashes', '-r', lock] if lock.exists() else [spec['package'] + '==' + spec['version']] + [p + '==' + v for p, v in spec.get('extra_packages', {}).items()]
            self.run(command, timeout=1200)
            result = self.run([py, '-m', 'pip', 'freeze'], timeout=60)
            (self.root / 'locks' / (name + '-freeze.txt')).write_text(Path(result['log']).read_text().split('\n', 1)[1])
            report = read_json(self.root / 'locks' / (name + '-pip-report.json'))
            requirements = []
            for item in report['install']:
                info = item['metadata']
                sha = item['download_info']['archive_info']['hashes']['sha256']
                requirements.append(info['name'] + '==' + info['version'] + ' --hash=sha256:' + sha)
            if requirements:
                lock.write_text('\n'.join(sorted(requirements)) + '\n')
            write_json(complete, {'version': spec['version'], 'package': spec['package'], 'created_at': stamp()})
        self.run([py, '-m', 'pip', 'check'])
        cli = {'quark': 'quark'}.get(name, name)
        if name == 'native-python':
            self.wrapper('native-python', [py])
            self.wrapper('so-info', [py, COMPONENT / 'scripts/inspect_so.py'])
        else:
            self.wrapper(cli, [venv / 'bin' / cli])
        if name == 'androguard':
            self.wrapper('androguard-python', [py])
        if name == 'quark':
            rules = self.unpack('quark-rules')
            rules_path = self.find_one(rules, 'quark-rules-*')
            (self.root / 'quark-rules.path').write_text(str(rules_path) + '\n')

    def install_one(self, name):
        print('INSTALL ' + name, flush=True)
        if name in self.catalog['python']:
            self.install_python(name)
        elif name == 'utilities':
            missing = [x for x in UTILITY_CHECKS if not shutil.which(x, path=self.env()['PATH'])]
            if missing:
                raise RuntimeError('Missing system utilities: ' + ', '.join(missing) + '; rerun with --install-system-deps on Ubuntu/Debian')
        elif name == 'mobsf':
            self.install_mobsf()
        elif name == 'smali':
            jars = []
            for index, spec in enumerate(self.catalog['smali']['artifacts']):
                jars.append(self.fetch('smali-' + str(index), spec))
            cp = os.pathsep.join(map(str, jars))
            for command in ['smali', 'baksmali']:
                self.wrapper(command, ['java', '-cp', cp, 'com.android.tools.smali.' + command + '.Main'])
        else:
            folder = self.unpack(name)
            if name == 'jdk':
                java = self.find_one(folder, '*/bin/java')
                write_json(self.root / 'state/jdk.json', {'java_home': str(java.parent.parent)})
            elif name == 'sdk':
                target = self.root / 'sdk/cmdline-tools/21.0'
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    if target.resolve() != (folder / 'cmdline-tools').resolve():
                        raise RuntimeError('Unexpected SDK directory symlink: ' + str(target))
                    target.unlink()
                if not target.exists():
                    shutil.copytree(folder / 'cmdline-tools', target)
                for cli, main in [('sdkmanager', 'com.android.sdklib.tool.sdkmanager.SdkManagerCli'), ('apkanalyzer', 'com.android.tools.apk.analyzer.ApkAnalyzerCli')]:
                    command = ['java', '-Dcom.android.sdklib.toolsdir=' + str(target), '-classpath', target / 'lib' / (cli + '-classpath.jar'), main]
                    if cli == 'sdkmanager':
                        command += ['--sdk_root=' + str(self.root / 'sdk')]
                    self.wrapper(cli, command)
                sdkmanager = self.root / 'bin/sdkmanager'
                build = self.root / 'sdk/build-tools' / self.catalog['sdk_build_tools']
                if not (build / 'source.properties').exists():
                    if not self.args.accept_sdk_licenses and not (self.root / 'sdk/licenses/android-sdk-license').exists():
                        raise RuntimeError('SDK package license acceptance needed: review https://developer.android.com/studio/terms and pass --accept-sdk-licenses, or run the generated sdkmanager --licenses interactively')
                    if self.args.accept_sdk_licenses:
                        self.run([sdkmanager, '--licenses'], input_text='y\n' * 100, timeout=300)
                    self.run([sdkmanager, '--install', 'build-tools;' + self.catalog['sdk_build_tools']], input_text='y\n' * 100 if self.args.accept_sdk_licenses else None, timeout=900)
                    if not (build / 'source.properties').exists():
                        raise RuntimeError('Build-Tools installation incomplete; inspect SDK license and install logs')
            elif name in ['apktool', 'bundletool']:
                self.wrapper(name, ['java', '-jar', folder / 'tool.jar'])
            elif name == 'jadx':
                for cli in ['jadx', 'jadx-gui']:
                    self.wrapper(cli, [folder / 'bin' / cli])
            elif name == 'dex2jar':
                for script in folder.glob('**/d2j-*.sh'):
                    script.chmod(0o755)
                    self.wrapper(script.stem, [script])
            elif name == 'rizin':
                for cli in ['rizin', 'rz-bin', 'rz-asm', 'rz-diff', 'rz-hash', 'rz-find']:
                    executable = self.find_one(folder, '**/bin/' + cli)
                    self.wrapper(cli, [executable])
            elif name == 'ghidra':
                self.wrapper('ghidraRun', [self.find_one(folder, '*/ghidraRun')])
                self.wrapper('analyzeHeadless', [self.find_one(folder, '*/support/analyzeHeadless')])
        self.activation()

    def install_mobsf(self):
        self.run(['docker', 'info'], timeout=60)
        lock = self.root / 'locks/mobsf-image.json'
        image = read_json(lock, {}).get('digest') or self.args.mobsf_image
        if not image:
            raise RuntimeError('--with-mobsf requires --mobsf-image with an explicit upstream tag or digest')
        if not image.startswith(('opensecurity/mobile-security-framework-mobsf:', 'opensecurity/mobile-security-framework-mobsf@sha256:')):
            raise RuntimeError('Use the official opensecurity/mobile-security-framework-mobsf image')
        if image.endswith(':latest') or not (':' in image or '@sha256:' in image):
            raise RuntimeError('Use a version tag or digest for MobSF, not latest')
        self.run(['docker', 'pull', image], timeout=1800)
        result = self.run(['docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', image])
        digests = json.loads(Path(result['log']).read_text().split('\n', 1)[1])
        immutable = next((x for x in digests if x.startswith('opensecurity/mobile-security-framework-mobsf@sha256:')), None)
        if not immutable:
            raise RuntimeError('Could not record the official MobSF image digest')
        write_json(lock, {'requested': image, 'digest': immutable, 'recorded_at': stamp()})
        (self.root / 'mobsf.compose.yaml').write_text('services:\n  mobsf:\n    image: ' + json.dumps(immutable) + '\n    ports:\n      - "127.0.0.1:8000:8000"\n    volumes:\n      - mobsf-data:/home/mobsf/.MobSF\nvolumes:\n  mobsf-data: {}\n')

    def checks(self, name):
        root = self.root
        commands = {
            'jdk': [['java', '-version'], ['javac', '-version']],
            'sdk': [['sdkmanager', '--version'], ['apkanalyzer', '--help'], ['aapt', 'version'], ['aapt2', 'version'], ['apksigner', '--version'], ['zipalign', '-h'], ['dexdump']],
            'jadx': [['jadx', '--version']], 'apktool': [['apktool', '--version']],
            'smali': [['smali', '--version'], ['baksmali', '--version']],
            'bundletool': [['bundletool', 'version']], 'dex2jar': [['d2j-dex2jar', '--help']],
            'androguard': [['androguard', '--version'], ['androguard-python', '-c', 'from androguard.core.apk import APK; from androguard.core.dex import DEX; print("APK and DEX imports OK")']],
            'droidasc': [['droidasc', '--help']],
            'apkid': [['apkid', '--help'], [root / 'venvs/apkid/bin/python', '-c', 'import yara; yara.compile(source=\'import "dex" rule dex_module_smoke { condition: true }\'); print("YARA dex module OK")']],
            'native-python': [['native-python', '-c', 'import lief, capstone; from elftools.elf.elffile import ELFFile; from importlib.metadata import version; print({p: version(p) for p in ["lief", "pyelftools", "capstone"]}); assert capstone.cs_support(capstone.CS_ARCH_ARM) and capstone.cs_support(capstone.CS_ARCH_ARM64)']],
            'quark': [['quark', '--help']], 'semgrep': [['semgrep', '--version']],
            'rizin': [['rizin', '-v'], ['rz-bin', '-v']],
            'ghidra': [['analyzeHeadless']], 'utilities': [[k] + v for k, v in UTILITY_CHECKS.items()],
            'mobsf': [['docker', 'compose', '-f', root / 'mobsf.compose.yaml', 'config', '--quiet']],
        }
        results = []
        for command in commands[name]:
            executable = shutil.which(str(command[0]), path=self.env()['PATH'])
            if not executable:
                raise RuntimeError(f'Missing command: {command[0]}')
            if name not in ['utilities', 'mobsf'] and not Path(executable).absolute().is_relative_to(root):
                raise RuntimeError(f'Command resolves outside this installation: {executable}')
            # These CLIs use nonzero for documented usage output; check the content too.
            usage = str(command[0]) in ['zipalign', 'dexdump', 'analyzeHeadless']
            result = self.run(command, timeout=180, ok=(0, 1, 2) if usage else (0,))
            if usage and not any(x in result['output'].lower() for x in ['usage', 'headlessusage', 'dexfile...']):
                raise RuntimeError('CLI did not produce usage text: ' + result['log'])
            results.append(result)
        if name in self.catalog['python']:
            py = root / 'venvs' / name / 'bin/python'
            spec = self.catalog['python'][name]
            expected = {spec['package']: spec['version'], **spec.get('extra_packages', {})}
            code = 'import json,sys; from importlib.metadata import version; expected=json.loads(sys.argv[1]); actual={p:version(p) for p in expected}; print(actual); assert actual == expected, (actual, expected)'
            results.append(self.run([py, '-c', code, json.dumps(expected)]))
            results.append(self.run([py, '-m', 'pip', 'check']))
        if name == 'mobsf':
            image = read_json(root / 'locks/mobsf-image.json')['digest']
            results.append(self.run(['docker', 'image', 'inspect', image]))
        if name == 'quark':
            rules_path = Path((root / 'quark-rules.path').read_text().strip())
            if not any(rules_path.glob('**/*.json')):
                raise RuntimeError('Quark rule snapshot contains no JSON rules')
        return results

    def install_system_deps(self):
        release = platform.freedesktop_os_release()
        if release.get('ID') not in ['ubuntu', 'debian']:
            raise RuntimeError('--install-system-deps is supported on Ubuntu/Debian only')
        prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
        self.run(prefix + ['apt-get', 'update'], timeout=600)
        self.run(prefix + ['apt-get', 'install', '-y', '--no-install-recommends'] + self.system_packages, timeout=1200)

    def execute(self):
        if self.args.action == 'check' and not (self.root / 'owner.json').exists():
            raise RuntimeError('No managed installation exists here; run install first')
        self.prepare()
        with (self.root / 'install.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.activation()
            if self.args.action == 'install' and self.args.install_system_deps:
                self.install_system_deps()
            results = {}
            for name in self.selected:
                try:
                    failed_deps = [x for x in DEPS.get(name, []) if results.get(x, {}).get('status') != 'verified']
                    if failed_deps:
                        raise RuntimeError('Dependencies failed: ' + ', '.join(failed_deps))
                    if self.args.action == 'install':
                        self.install_one(name)
                    checks = self.checks(name)
                    results[name] = {'status': 'verified', 'checks': checks}
                    print('OK ' + name, flush=True)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
                    results[name] = {'status': 'failed', 'error': str(error)}
                    print('FAIL ' + name + ': ' + str(error), file=sys.stderr, flush=True)
            manifest = {'schema': 1, 'timestamp_utc': stamp(), 'workspace': str(self.workspace),
                        'host': {'system': platform.system(), 'machine': platform.machine(), 'python': sys.version, 'platform': platform.platform()},
                        'catalog_sha256': digest(CATALOG), 'action': self.args.action,
                        'selected': self.selected, 'results': results, 'downloads': self.artifacts,
                        'validation_scope': 'CLI startup and dependency checks; sample smoke tests are separate',
                        'sdk_license_flag': self.args.accept_sdk_licenses}
            manifest['sdk_metadata'] = {str(p.relative_to(self.root)): {'sha256': digest(p), 'content': p.read_text()} for p in (self.root / 'sdk').glob('**/source.properties')}
            write_json(self.run_dir / 'manifest.json', manifest)
            write_json(self.root / 'manifest.json', manifest)
            write_json(self.workspace / 'docs/android-static-manifest.json', manifest)
            if not (self.workspace / '.envrc').exists():
                (self.workspace / '.envrc').write_text('source ' + shlex.quote(str(self.root / 'env.sh')) + '\n')
            print('Manifest: ' + str(self.root / 'manifest.json'))
            print('Activate: source ' + shlex.quote(str(self.root / 'env.sh')))
            return int(any(x['status'] != 'verified' for x in results.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'install', 'check'])
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--profile', choices=['full', 'core'], default='full')
    parser.add_argument('--only', help='Comma-separated tool names; Java dependencies are included')
    parser.add_argument('--cache-dir', help='Dedicated download cache; files are verified before reuse')
    parser.add_argument('--install-system-deps', action='store_true', help='Install the recorded apt packages (Ubuntu/Debian; sudo -n if non-root)')
    parser.add_argument('--accept-sdk-licenses', action='store_true', help='Accept Android SDK licenses for this project SDK')
    parser.add_argument('--with-mobsf', action='store_true', help='Pull MobSF and generate local Compose config; does not start a service')
    parser.add_argument('--mobsf-image', help='Official MobSF image with explicit version tag or digest')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12+ required for the bundled installer')
    if platform.system() != 'Linux' or platform.machine() not in ['x86_64', 'AMD64']:
        parser.error('Bundled artifacts target Linux x86_64 (including WSL2); see references/platforms.md')
    try:
        env = Environment(args)
        if args.action == 'plan':
            print(json.dumps({'workspace': str(env.workspace), 'install_root': str(env.root), 'tools': env.selected,
                              'system_packages': env.system_packages, 'catalog': str(CATALOG),
                              'with_mobsf': args.with_mobsf, 'sdk_licenses': args.accept_sdk_licenses,
                              'note': 'No files changed. Default full includes native analysis and rule scanners.'}, indent=2))
            return 0
        return env.execute()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
