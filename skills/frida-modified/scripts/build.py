#!/usr/bin/env python3
"""Build a locked Frida source tree with the explicit 17.18.0 build adapter."""
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
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from apply_profile import load_profile
from common import Blocked, digest_json, run, sha256, utc, write_json
from provenance import git, source_state
from sync_resources import synchronize


def validate_cache(directory, identity):
    lock = Path(directory) / '.frida-modified-inputs.json'
    if Path(directory).exists() and any(Path(directory).iterdir()):
        if not lock.is_file():
            raise Blocked('Build cache is unowned; use a fresh --build-dir and --prefix')
        previous = json.loads(lock.read_text())
        if previous.get('identity') != identity:
            raise Blocked('Build inputs changed; use a fresh --build-dir and --prefix')
        for artifact in previous.get('artifacts', []):
            path = Path(artifact['path'])
            if not path.is_file() or sha256(path) != artifact['sha256']:
                raise Blocked('Cached artifact changed or is missing: ' + str(path))


def execute(args):
    source, build, prefix = (p.resolve() for p in (args.source, args.build_dir, args.prefix))
    if source in build.parents or source == build or build == prefix or source in prefix.parents:
        raise Blocked('Use distinct build/prefix directories outside the source tree')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        raise Blocked('This build adapter currently supports Linux x86_64 hosts')
    if args.version != '17.18.0':
        raise Blocked('No build adapter for this version; inspect its upstream build instructions first')
    tag = git(source, 'rev-list', '-n', '1', args.version)
    if git(source, 'rev-parse', 'HEAD') != tag:
        raise Blocked('Source HEAD differs from the selected version')
    for command in ['git', 'make', 'ninja', 'cc', 'c++', 'pkg-config', 'node', 'npm']:
        if shutil.which(command) is None:
            raise Blocked('Missing build tool: ' + command)
    environment = os.environ.copy()
    tools = {'python': sys.version, 'host': [platform.system(), platform.machine()],
             'environment': {key: environment.get(key) for key in ['CC', 'CXX', 'CFLAGS', 'CXXFLAGS',
                             'CPPFLAGS', 'LDFLAGS', 'PKG_CONFIG', 'PKG_CONFIG_PATH', 'CMAKE_PREFIX_PATH']}}
    for command in ['cc', 'c++', 'make', 'ninja', 'node', 'npm', 'pkg-config']:
        tools[command] = {'version': run([command, '--version']).stdout.splitlines()[0],
                          'path': shutil.which(command), 'sha256': sha256(shutil.which(command))}
    if args.ndk:
        ndk = args.ndk.resolve()
        if not (ndk / 'source.properties').is_file():
            raise Blocked('Invalid --ndk directory')
        environment.update(ANDROID_NDK_ROOT=str(ndk), ANDROID_NDK_HOME=str(ndk))
        tools['ndk'] = (ndk / 'source.properties').read_text()
        tools['ndk_path'] = str(ndk)
        tools['ndk_clang_sha256'] = sha256(ndk / 'toolchains/llvm/prebuilt/linux-x86_64/bin/clang')
    elif args.mode == 'android':
        raise Blocked('--ndk is required for Android builds')
    profile = None
    resources = None
    if args.profile:
        profile = load_profile(args.profile, source)
        if not args.patch_record:
            raise Blocked('--patch-record is required when building a profile')
        receipt = json.loads(args.patch_record.read_text())
        if receipt.get('status') != 'applied' or receipt.get('profile_sha256') != sha256(args.profile) or receipt.get('source') != str(source):
            raise Blocked('Patch receipt does not describe this source/profile')
        for patch in receipt['patches']:
            run(['git', '-C', source / patch['repository'], 'apply', '--reverse', '--check', args.profile.resolve().parent / patch['file']])
        if 'runtime-names' in receipt['groups']:
            if not (args.sdk and args.build_tools and args.helper_api):
                raise Blocked('Helper regeneration requires --sdk, --build-tools and --helper-api')
            if not shutil.which('javac') or not shutil.which('jar'):
                raise Blocked('Missing Java build tools')
            tools['javac'] = run(['javac', '-version']).stdout.strip()
            tools['d8_sha256'] = sha256(args.sdk / 'build-tools' / args.build_tools / 'lib/d8.jar')
            tools['android_jar_sha256'] = sha256(args.sdk / 'platforms' / f'android-{args.helper_api}' / 'android.jar')
            resources = synchronize(source, args.profile, args.sdk, args.build_tools, args.helper_api)
    else:
        for name, state in source_state(source).items():
            if git(source / name, 'status', '--porcelain', '--untracked-files=normal'):
                raise Blocked('An unpatched build requires clean source: ' + name)
    options = {'version': args.version, 'mode': args.mode, 'prefix': str(prefix), 'source_path': str(source),
               'profile_sha256': sha256(args.profile) if args.profile else None,
               'helper_api': args.helper_api, 'build_tools': args.build_tools}
    inputs = {'source': source_state(source), 'tools': tools, 'options': options,
              'builder_sha256': sha256(__file__), 'resource_script_sha256': sha256(Path(__file__).with_name('sync_resources.py'))}
    identity = digest_json(inputs)
    validate_cache(build, identity)
    if prefix.exists() and any(prefix.iterdir()) and not (build / '.frida-modified-inputs.json').exists():
        raise Blocked('Install prefix is not empty and has no matching build receipt')
    record = {'schema': 1, 'utc': utc(), 'identity': identity, 'inputs': inputs, 'resources': resources,
              'status': 'preflight_passed', 'artifacts': []}
    if args.preflight:
        return record
    build.mkdir(parents=True, exist_ok=True)
    prefix.mkdir(parents=True, exist_ok=True)
    write_json(build / '.frida-modified-inputs.json', record)
    environment['MESON_BUILD_ROOT'] = str(build)
    configure = [str(source / 'configure'), '--prefix=' + str(prefix)]
    if args.mode == 'android':
        configure += ['--host=android-arm64', '--enable-portal', '--',
                      '-Dfrida-gum:devkits=gum,gumjs', '-Dfrida-core:compiler_backend=enabled', '-Dfrida-core:devkits=core']
    else:
        configure += ['--enable-frida-python', '--disable-frida-tools', '--disable-graft-tool',
                      '--', '-Dfrida-core:droidy_backend=enabled']
    log = args.record.with_suffix('.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    def command(argv):
        with log.open('a') as stream:
            stream.write(json.dumps(list(map(str, argv))) + '\n')
            stream.flush()
            subprocess.run(list(map(str, argv)), cwd=source, env=environment, stdout=stream,
                           stderr=subprocess.STDOUT, check=True, timeout=7200)
    if not (build / 'build.ninja').exists():
        command(configure)
    record['status'] = 'configured'
    write_json(build / '.frida-modified-inputs.json', record)
    if args.configure_only:
        return record
    command(['make', '-C', build, '-j' + str(args.jobs)])
    if args.mode == 'android':
        command(['make', '-C', build, 'install'])
        server = prefix / 'bin/frida-server'
        artifacts = [server]
        if profile:
            alias = prefix / 'bin' / profile['server_filename']
            shutil.copy2(server, alias)
            artifacts.append(alias)
        for component in ['agent', 'gadget']:
            for arch, relative in [('arm64', f'lib/{component}/frida-{component}.so'), ('arm', f'compat/frida-{component}.so')]:
                original = build / 'subprojects/frida-core' / relative
                target = prefix / 'lib' / arch / (profile[f'{component}_filename'] if profile else f'frida-{component}.so')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
                artifacts.append(target)
    else:
        extension = build / 'subprojects/frida-python/frida/_frida.abi3.so'
        if not extension.is_file():
            raise Blocked('Build adapter expects the Linux Python extension layout')
        wheels = prefix / 'wheels'
        wheels.mkdir(exist_ok=True)
        environment.update(FRIDA_EXTENSION=str(extension), FRIDA_VERSION=args.version)
        command([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                 '--wheel-dir', wheels, source / 'subprojects/frida-python'])
        artifacts = list(wheels.glob('frida-*.whl'))
        if not artifacts:
            raise RuntimeError('No client wheel produced')
    record['artifacts'] = [{'path': str(p), 'sha256': sha256(p)} for p in artifacts]
    record['status'] = 'built'
    write_json(build / '.frida-modified-inputs.json', record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source', 'build-dir', 'prefix', 'record']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--mode', choices=['android', 'host'], required=True)
    for name in ['profile', 'patch-record', 'ndk', 'sdk']:
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--build-tools')
    parser.add_argument('--helper-api', type=int)
    parser.add_argument('--jobs', type=int, default=4)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--configure-only', action='store_true')
    args = parser.parse_args()
    if args.record.exists() or args.jobs < 1:
        parser.error('Use a new record path and a positive job count')
    try:
        record = execute(args)
        code = 0
    except BaseException as error:
        code = 2 if isinstance(error, Blocked) else 1
        record = {'utc': utc(), 'status': 'blocked' if code == 2 else 'failed',
                  'error': f'{type(error).__name__}: {error}', 'log': str(args.record.with_suffix('.log'))}
    write_json(args.record, record)
    print(f"{record['status']}: {args.record}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
