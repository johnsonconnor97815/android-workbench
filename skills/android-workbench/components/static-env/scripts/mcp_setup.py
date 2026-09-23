#!/usr/bin/env python3
"""Install, configure and probe project-local Android static-analysis MCP servers."""
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
import fcntl
import hashlib
import difflib
import zipfile
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tomllib
import uuid

import setup
from mcp_probe import Session

LOCK = setup.COMPONENT / 'assets/mcp.lock.json'
DEPENDENCIES = {'jadx': ['jdk', 'jadx'], 'apktool': ['jdk', 'apktool'],
                'ghidra': ['jdk', 'ghidra'], 'semgrep': ['semgrep']}
BEGIN = '# BEGIN android-static-env MCP\n'
END = '# END android-static-env MCP\n'


def select(value):
    names = list(dict.fromkeys(value.split(',')))
    if not names or set(names) - DEPENDENCIES.keys():
        raise ValueError('Use comma-separated server names: jadx,apktool,ghidra,semgrep')
    return names


def base_args(args):
    dependencies = {dep for name in select(args.servers) for dep in DEPENDENCIES[name]}
    return argparse.Namespace(workspace=args.workspace, cache_dir=args.cache_dir,
        only=','.join(n for n in setup.FULL if n in dependencies), profile='full',
        with_mobsf=False, action='install' if args.action == 'install' else 'check',
        install_system_deps=False, accept_sdk_licenses=False, mobsf_image=None)


def backup_write(path, content):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing to replace symlink: ' + str(path))
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.bak-' + uuid.uuid4().hex[:10]))
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(content)
    temp.replace(path)


def codex_text(specs, enable_jadx=False):
    lines = [BEGIN.rstrip()]
    for name, spec in specs.items():
        lines += ['', '[mcp_servers.' + json.dumps(name) + ']']
        for key in ['command', 'args', 'cwd']:
            if key in spec:
                lines.append(key + ' = ' + json.dumps(spec[key], ensure_ascii=True))
        lines += ['startup_timeout_sec = 120', 'tool_timeout_sec = 300',
                  'enabled = ' + ('false' if name == 'android-jadx' and not enable_jadx else 'true')]
        if spec.get('env'):
            lines.append('env = { ' + ', '.join(json.dumps(k) + ' = ' + json.dumps(v)
                         for k, v in spec['env'].items()) + ' }')
    return '\n'.join(lines) + '\n' + END


def merge_codex(path, specs, enable_jadx=False):
    original = path.read_text() if path.exists() else ''
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise RuntimeError('Malformed managed MCP block; preserve and review ' + str(path))
    outside = original
    if BEGIN in original:
        start, stop = original.index(BEGIN), original.index(END) + len(END)
        if stop < start:
            raise RuntimeError('Malformed managed MCP block')
        outside = original[:start] + original[stop:]
    existing = tomllib.loads(outside).get('mcp_servers', {})
    if set(existing) & specs.keys():
        raise RuntimeError('MCP server name already configured outside managed block')
    block = codex_text(specs, enable_jadx)
    if BEGIN in original:
        result = original[:start] + block + original[stop:]
    else:
        result = original + ('\n' if original and not original.endswith('\n\n') else '') + block
    tomllib.loads(result)
    backup_write(path, result)


def merge_json(path, specs, client, enable_jadx=False):
    data = json.loads(path.read_text()) if path.exists() else {}
    key = 'servers' if client == 'vscode' else 'mcpServers'
    servers = data.setdefault(key, {})
    for name, spec in specs.items():
        if name == 'android-jadx' and not enable_jadx:
            continue
        entry = {k: v for k, v in spec.items() if k in ['command', 'args', 'env']}
        if name in servers and servers[name] != entry:
            raise RuntimeError('Conflicting MCP configuration: ' + name)
        servers[name] = entry
    backup_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


class MCPEnvironment(setup.Environment):
    def __init__(self, args):
        super().__init__(base_args(args))
        self.options = args
        self.mcp_lock = setup.read_json(LOCK)
        self.catalog['artifacts'].update(self.mcp_lock['artifacts'])
        self.catalog['python'].update(self.mcp_lock['python'])
        self.names = select(args.servers)

    def runtime_env(self):
        env = self.env()
        keys = ['JAVA_HOME', 'PATH', 'PYTHONNOUSERSITE', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                'SEMGREP_SEND_METRICS', 'SEMGREP_ENABLE_VERSION_CHECK']
        selected = {k: env[k] for k in keys if k in env}
        selected.update(JADX_CONFIG_DIR=str(self.root / 'config/jadx'),
                        JADX_CACHE_DIR=str(self.root / 'cache/jadx'),
                        XDG_DATA_HOME=str(self.root / 'data'),
                        ANONYMIZED_TELEMETRY='False', OTEL_SDK_DISABLED='true',
                        JAVA_TOOL_OPTIONS='"-Djava.util.prefs.userRoot=' + str(self.root / 'config/java-prefs') + '"')
        return selected

    def launch(self, name, command, extra=None):
        env = {**self.runtime_env(), **(extra or {})}
        target = self.root / 'bin' / ('mcp-' + name)
        cwd = self.root / 'mcp/work'
        cwd.mkdir(parents=True, exist_ok=True)
        script = '#!/bin/sh\nunset PYTHONPATH PYTHONHOME\n'
        script += '\n'.join('export ' + k + '=' + shlex.quote(v) for k, v in env.items()) + '\n'
        if not name.endswith('-gui'):
            script += 'cd ' + shlex.quote(str(cwd)) + '\n'
        script += 'exec ' + shlex.join(list(map(str, command))) + ' "$@"\n'
        target.write_text(script)
        target.chmod(0o755)
        return self.managed_spec({'command': str(target), 'args': [], 'cwd': str(cwd)})

    def managed_spec(self, spec):
        if not (self.workspace / 'workbench.project.json').is_file():
            return spec
        manifest = json.loads((self.workspace / 'workbench.project.json').read_text())
        sys.path.insert(0, str((self.workspace / manifest.get('workbench_root', 'android-workbench')).resolve()))
        from workbench.bridge import managed_mcp_spec
        return managed_mcp_spec(self.workspace, spec)

    def loopback_plugin(self, original):
        recipe = self.mcp_lock['artifacts']['mcp-jadx-plugin-source']
        source = self.fetch('mcp-jadx-plugin-source', recipe)
        raw = source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        if blob != recipe['git_blob_sha1']:
            raise RuntimeError('JADX source differs from the pinned upstream Git blob')
        text = raw.decode()
        before = '}).start(port);'
        after = '}).start("127.0.0.1", port);'
        if text.count(before) != 1:
            raise RuntimeError('Unexpected JADX PluginServer source; review the loopback patch')
        # The release JAR shades Javalin; compile against that exact bundled dependency.
        relocated = 'com.zin.jadxaimcp.deps.javalin.Javalin'
        if text.count('import io.javalin.Javalin;') != 1:
            raise RuntimeError('Unexpected Javalin import in pinned source')
        patched = text.replace(before, after).replace('import io.javalin.Javalin;', 'import ' + relocated + ';')
        directory = self.root / 'tools/mcp-jadx-loopback'
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / 'jadx-ai-mcp-loopback.jar'
        record_path = directory / 'build.json'
        record = setup.read_json(record_path, {})
        source_hash = hashlib.sha256(patched.encode()).hexdigest()
        if target.exists() and record.get('original_jar_sha256') == setup.digest(original) and record.get('patched_source_sha256') == source_hash and record.get('derived_jar_sha256') == setup.digest(target):
            return target
        patched_source = directory / 'src/com/zin/jadxaimcp/server/PluginServer.java'
        patched_source.parent.mkdir(parents=True, exist_ok=True)
        patched_source.write_text(patched)
        (directory / 'loopback.patch').write_text(''.join(difflib.unified_diff(text.splitlines(True), patched.splitlines(True), fromfile='upstream/PluginServer.java', tofile='derived/PluginServer.java')))
        classes = directory / 'classes'
        classes.mkdir(exist_ok=True)
        self.run(['javac', '--release', '17', '-encoding', 'UTF-8', '-cp', str(original) + os.pathsep + str(self.root / 'tools/jadx/lib/*'), '-d', classes, patched_source])
        class_path = 'com/zin/jadxaimcp/server/PluginServer.class'
        new_class = (classes / class_path).read_bytes()
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(target, 'w') as dst:
            if any(n.upper().endswith(('.SF', '.RSA', '.DSA')) for n in src.namelist()):
                raise RuntimeError('Signed JAR needs separate review; original is preserved')
            for info in src.infolist():
                dst.writestr(info, new_class if info.filename == class_path else src.read(info.filename))
        setup.write_json(record_path, {'original_jar_sha256': setup.digest(original),
            'source_url': recipe['url'], 'source_git_blob_sha1': blob,
            'patched_source_sha256': source_hash, 'derived_jar_sha256': setup.digest(target),
            'changed_class': class_path, 'patch': str(directory / 'loopback.patch'),
            'note': 'Locally derived plugin; only HTTP listen address changed to 127.0.0.1'})
        return target

    def install_server(self, name):
        if name != 'semgrep':
            self.install_python('mcp-' + name)
        if name in ['jadx', 'apktool']:
            folder = self.unpack('mcp-' + name + '-server')
            script = self.find_one(folder, '**/' + name + '_mcp_server.py')
            command = [self.root / 'venvs' / ('mcp-' + name) / 'bin/python', script]
            if name == 'apktool':
                command += ['--workspace', self.root / 'mcp/apktool-projects']
            else:
                command += ['--jadx-host', '127.0.0.1', '--jadx-port', str(self.options.jadx_port)]
                plugin = self.fetch('mcp-jadx-plugin', self.mcp_lock['artifacts']['mcp-jadx-plugin'])
                plugin = self.loopback_plugin(plugin)
                # A dedicated config directory avoids changing the user's existing GUI plugins.
                gui_env = self.runtime_env()
                result = subprocess.run([str(self.root / 'bin/jadx'), 'plugins', '--install-jar', str(plugin)],
                    env={**self.env(), **gui_env}, capture_output=True, text=True, timeout=120)
                (self.run_dir / 'jadx-plugin-install.log').write_text(result.stdout + result.stderr)
                if result.returncode:
                    raise RuntimeError('JADX plugin registration failed; see ' + str(self.run_dir))
                installed = list((self.root / 'config/jadx').rglob('*.jar'))
                if not any(setup.digest(p) == setup.digest(plugin) for p in installed):
                    raise RuntimeError('Plugin was not registered in the isolated JADX config directory')
                self.launch('jadx-gui', [self.root / 'bin/jadx-gui'])
            return self.launch(name, command)
        if name == 'ghidra':
            ghidra = self.find_one(self.root / 'tools/ghidra', '*/ghidraRun').parent
            project = Path(self.options.ghidra_project_dir).expanduser().resolve() if self.options.ghidra_project_dir else self.workspace / 'evidence/android-mcp/ghidra-projects'
            if any(part.startswith('.') for part in project.parts):
                raise RuntimeError('Ghidra rejects hidden path elements; set --ghidra-project-dir to a path without dot-prefixed components')
            project.mkdir(parents=True, exist_ok=True)
            model = self.unpack('mcp-ghidra-embedding-model')
            entry = self.root / 'mcp/ghidra_mcp_entry.py'
            entry.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(setup.COMPONENT / 'scripts/ghidra_mcp_entry.py', entry)
            return self.launch(name, [self.root / 'venvs/mcp-ghidra/bin/python', entry, '--model-cache', model,
                '--transport', 'stdio', '--project-root', project,
                '--project-name', 'android-workbench'], {'GHIDRA_INSTALL_DIR': str(ghidra)})
        # Keep Semgrep on local, explicit-rule operations in this static-analysis profile.
        entry = self.root / 'mcp/semgrep_mcp_entry.py'
        entry.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(setup.COMPONENT / 'scripts/semgrep_mcp_entry.py', entry)
        return self.launch(name, [self.root / 'venvs/semgrep/bin/python', entry, '-t', 'stdio'], {
            'SEMGREP_FINDINGS_DISABLED': 'true', 'SEMGREP_RULE_SCHEMA_DISABLED': 'true',
            'SEMGREP_SCAN_REMOTE_DISABLED': 'true', 'SEMGREP_SCAN_DISABLED': 'true',
            'SEMGREP_SCAN_SUPPLY_CHAIN_DISABLED': 'true'})

    def templates(self, specs):
        folder = self.root / 'mcp'
        specs = {name: self.managed_spec(spec) for name, spec in specs.items()}
        setup.write_json(folder / 'servers.json', specs)
        (folder / 'codex.toml').write_text(codex_text(specs))
        entries = {n: {k: v for k, v in s.items() if k in ['command', 'args', 'env']}
                   for n, s in specs.items() if n != 'android-jadx'}
        setup.write_json(folder / 'mcp.json', {'mcpServers': entries})
        setup.write_json(folder / 'vscode.json', {'servers': entries})
        if 'android-jadx' in specs:
            setup.write_json(folder / 'jadx-optional.json', {'mcpServers': {'android-jadx': {
                'command': specs['android-jadx']['command'], 'args': specs['android-jadx'].get('args', [])}}})

    def execute_mcp(self):
        specs_path = self.root / 'mcp/servers.json'
        if self.options.action == 'install':
            base = setup.Environment(base_args(self.options))
            base.execute()
            base_results = setup.read_json(base.root / 'manifest.json')['results']
        elif not (self.root / 'owner.json').exists():
            raise RuntimeError('No managed installation; run mcp_setup.py install first')
        self.prepare()
        results = {}
        with (self.root / 'install.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            specs = setup.read_json(specs_path, {})
            for name in self.names:
                try:
                    if self.options.action == 'install':
                        if any(base_results.get(d, {}).get('status') != 'verified' for d in DEPENDENCIES[name]):
                            raise RuntimeError('Required base tool failed: ' + ', '.join(DEPENDENCIES[name]))
                        specs['android-' + name] = self.install_server(name)
                        results[name] = {'status': 'installed', 'analysis_verified': False}
                    else:
                        spec = self.managed_spec(specs['android-' + name])
                        with Session(spec, self.run_dir / name, self.options.timeout) as client:
                            protocol = client.initialize()
                        results[name] = {'status': 'protocol-verified', **protocol, 'analysis_verified': False}
                        if name == 'jadx':
                            results[name]['requires'] = 'Running JADX GUI with the matching plugin and an open sample'
                    print('OK ' + name + ': ' + results[name]['status'], flush=True)
                except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    results[name] = {'status': 'failed', 'error': str(error)}
                    print('FAIL ' + name + ': ' + str(error), file=sys.stderr, flush=True)
            if self.options.action == 'install':
                self.templates(specs)
            manifest = {'schema': 1, 'timestamp_utc': setup.stamp(), 'workspace': str(self.workspace),
                'action': self.options.action, 'mcp_catalog_sha256': setup.digest(LOCK),
                'results': results, 'downloads': self.artifacts,
                'scope': 'Installation or MCP initialize/tools/list only; actual analysis needs a separate sample test'}
            setup.write_json(self.run_dir / 'mcp-manifest.json', manifest)
            setup.write_json(self.root / 'mcp/manifest.json', manifest)
            setup.write_json(self.workspace / 'docs/android-mcp-manifest.json', manifest)
        print('Evidence: ' + str(self.run_dir))
        print('Configurations: ' + str(self.root / 'mcp'))
        return int(any(r['status'] == 'failed' for r in results.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'install', 'check', 'configure'])
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--servers', default='jadx,apktool,ghidra,semgrep')
    parser.add_argument('--cache-dir')
    parser.add_argument('--jadx-port', type=int, default=8650)
    parser.add_argument('--ghidra-project-dir', help='Default: workspace/evidence/android-mcp/ghidra-projects; Ghidra rejects hidden path components')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--client', choices=['codex', 'claude', 'cursor', 'vscode'], default='codex')
    parser.add_argument('--enable-jadx', action='store_true', help='GUI must be running with an open sample')
    args = parser.parse_args()
    if sys.version_info < (3, 12) or platform.system() != 'Linux' or platform.machine() not in ['x86_64', 'AMD64']:
        parser.error('Bundled installer requires Linux x86_64 and Python 3.12+')
    if not 1 <= args.jadx_port <= 65535 or args.timeout <= 0:
        parser.error('Invalid port or timeout')
    try:
        env = MCPEnvironment(args)
        if args.action == 'plan':
            print(json.dumps({'servers': env.names, 'base_tools': env.selected,
                'root': str(env.root), 'catalog': str(LOCK), 'transport': 'stdio',
                'gui_required': ['jadx'] if 'jadx' in env.names else [],
                'note': 'No files changed; install generates templates; configure merges project client settings.'}, indent=2))
            return 0
        if args.action == 'configure':
            all_specs = setup.read_json(env.root / 'mcp/servers.json', {})
            specs = {f'android-{n}': env.managed_spec(all_specs[f'android-{n}']) for n in env.names}
            path = env.workspace / {'codex': '.codex/config.toml', 'claude': '.mcp.json',
                                    'cursor': '.cursor/mcp.json', 'vscode': '.vscode/mcp.json'}[args.client]
            if args.client == 'codex':
                merge_codex(path, specs, args.enable_jadx)
            else:
                merge_json(path, specs, args.client, args.enable_jadx)
            print('Configured: ' + str(path))
            return 0
        return env.execute_mcp()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
