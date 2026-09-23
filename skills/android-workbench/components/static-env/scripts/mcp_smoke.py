#!/usr/bin/env python3
"""Verify actual MCP analysis with identified fixtures; never execute input samples."""
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
import copy
import datetime as dt
import json
import ipaddress
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import setup
from mcp_probe import Session
from mcp_setup import select


def payload(result):
    if result.get('structuredContent') is not None:
        return result['structuredContent']
    for item in result.get('content', []):
        if item.get('type') == 'text':
            try:
                return json.loads(item['text'])
            except json.JSONDecodeError:
                continue
    raise RuntimeError('No structured result returned')


def is_loopback_listener(address):
    host = address.rsplit(':', 1)[0].strip('[]')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, 'ipv4_mapped', None)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


def stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--servers', default='apktool,ghidra,semgrep')
    parser.add_argument('--apk', type=Path)
    parser.add_argument('--apk-source')
    parser.add_argument('--class-name')
    parser.add_argument('--package-name')
    parser.add_argument('--apk-marker')
    parser.add_argument('--so', type=Path)
    parser.add_argument('--so-source')
    parser.add_argument('--so-symbol')
    parser.add_argument('--so-marker')
    parser.add_argument('--start-jadx-gui', action='store_true')
    parser.add_argument('--xvfb', action='store_true', help='Start the test GUI on a virtual display')
    parser.add_argument('--jadx-port', type=int, default=8650)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    names = select(args.servers)
    if set(names) & {'jadx', 'apktool'} and not all([args.apk, args.apk_source, args.class_name, args.package_name, args.apk_marker]):
        parser.error('APK checks require --apk, --apk-source, --class-name, --package-name and --apk-marker')
    if 'ghidra' in names and not all([args.so, args.so_source, args.so_symbol, args.so_marker]):
        parser.error('Ghidra check requires --so, --so-source, --so-symbol and --so-marker')
    if args.xvfb and not args.start_jadx_gui:
        parser.error('--xvfb requires --start-jadx-gui')
    workspace = Path(args.workspace).expanduser().resolve()
    root = workspace / '.android-static'
    specs = setup.read_json(root / 'mcp/servers.json')
    out = workspace / 'evidence/android-mcp' / ('smoke-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    report = {'timestamp_utc': setup.stamp(), 'scope': 'sample functional MCP calls', 'inputs': {}, 'results': {}}
    for name in ['apk', 'so']:
        path = getattr(args, name)
        if path:
            path = path.expanduser().resolve(strict=True)
            setattr(args, name, path)
            report['inputs'][name] = {'path': str(path), 'source': getattr(args, name + '_source'), 'sha256': setup.digest(path)}
    setup.write_json(out / 'inputs.json', report['inputs'])

    for name in names:
        gui, gui_log = None, None
        try:
            spec = copy.deepcopy(specs['android-' + name])
            if name == 'ghidra':
                # Wait for analysis, and use the actual returned program name (which includes a suffix).
                project = out / 'ghidra-projects'
                if any(p.startswith('.') for p in project.parts):
                    raise RuntimeError('Ghidra smoke workspace must not contain hidden path components')
                spec['args'] += ['--project-path', str(project), '--project-name', 'fixture',
                                 '--wait-for-analysis', str(args.so)]
            if name == 'jadx' and args.start_jadx_gui:
                with socket.socket() as sock:
                    if sock.connect_ex(('127.0.0.1', args.jadx_port)) == 0:
                        raise RuntimeError('JADX port is already in use; refusing to test an unidentified GUI')
                gui_log = (out / 'jadx-gui.log').open('w')
                command = [str(root / 'bin/mcp-jadx-gui'), str(args.apk)]
                if args.xvfb:
                    command = ['xvfb-run', '-a', *command]
                gui = subprocess.Popen(command, stdout=gui_log, stderr=subprocess.STDOUT, start_new_session=True)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                url = f'http://127.0.0.1:{args.jadx_port}/class-source?' + urllib.parse.urlencode({'class_name': args.class_name})
                deadline = time.monotonic() + min(args.timeout, 90)
                while True:
                    if gui.poll() is not None:
                        raise RuntimeError('JADX GUI exited; see jadx-gui.log')
                    try:
                        with opener.open(url, timeout=2) as response:
                            if args.apk_marker in response.read().decode():
                                break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError('JADX GUI did not expose the expected class; see jadx-gui.log')
                    time.sleep(0.5)
            calls = []
            with Session(spec, out / name, args.timeout) as client:
                protocol = client.initialize()
                if name == 'jadx' and args.start_jadx_gui:
                    listeners = subprocess.run(['ss', '-H', '-ltn'], capture_output=True, text=True, check=True).stdout
                    rows = [line for line in listeners.splitlines() if line.split()[3].endswith(':' + str(args.jadx_port))]
                    (out / 'jadx-listeners.txt').write_text('\n'.join(rows) + '\n')
                    if not rows or any(not is_loopback_listener(line.split()[3]) for line in rows):
                        raise RuntimeError('JADX plugin is not bound exclusively to loopback')
                def call(tool, arguments=None, expect=None):
                    result = client.call(tool, arguments or {}, expect)
                    calls.append(tool)
                    return result
                if name == 'apktool':
                    project = root / 'mcp/apktool-projects' / out.name
                    decoded = payload(call('decode_apk', {'apk_path': str(args.apk), 'output_dir': str(project), 'force': False}))
                    if decoded.get('has_manifest') is not True or decoded.get('success') is not True:
                        raise RuntimeError('APK decode did not produce a manifest')
                    call('get_manifest', {'project_dir': str(project)}, args.package_name)
                    call('get_smali_file', {'project_dir': str(project), 'class_name': args.class_name}, args.apk_marker)
                elif name == 'jadx':
                    call('get_android_manifest', expect=args.package_name)
                    call('get_class_source', {'class_name': args.class_name}, args.apk_marker)
                elif name == 'ghidra':
                    data = payload(call('list_project_binaries'))
                    programs = [p for p in data['programs'] if p.get('file_path') == str(args.so)]
                    if len(programs) != 1 or not programs[0]['analysis_complete']:
                        raise RuntimeError('Expected one analyzed fixture program: ' + json.dumps(data))
                    binary = programs[0]['name']
                    call('list_project_binary_metadata', {'binary_name': binary})
                    call('list_exports', {'binary_name': binary}, args.so_symbol)
                    call('decompile_function', {'binary_name': binary, 'name_or_address': args.so_symbol}, args.so_marker)
                    deadline = time.monotonic() + args.timeout
                    while True:
                        status = payload(call('list_project_binaries'))
                        ready = next(p for p in status['programs'] if p['name'] == binary)
                        if ready['code_indexed'] and ready['strings_indexed']:
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Ghidra MCP indexing did not complete')
                        time.sleep(1)
                    call('search_code', {'binary_name': binary, 'query': args.so_marker, 'search_mode': 'semantic'}, args.so_marker)
                else:
                    data = payload(call('semgrep_scan_with_custom_rule', {
                        'code_files': [{'path': 'Probe.java', 'content': 'class Probe { void call() { System.out.println("ANDROID_MCP_SMOKE"); } }'}],
                        'rule': 'rules:\n  - id: android-mcp-smoke\n    languages: [java]\n    message: Android MCP smoke fixture\n    severity: INFO\n    pattern: System.out.println(...)\n'}))
                    if data.get('errors') or not any(r['check_id'] == 'android-mcp-smoke' for r in data.get('results', [])):
                        raise RuntimeError('Expected Semgrep finding is missing or scan has errors')
                report['results'][name] = {'status': 'passed', 'protocol': protocol, 'calls': calls}
            print('OK functional ' + name, flush=True)
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
            report['results'][name] = {'status': 'failed', 'error': str(error)}
            print('FAIL functional ' + name + ': ' + str(error), file=sys.stderr, flush=True)
        finally:
            if gui:
                stop_process(gui)
            if gui_log:
                gui_log.close()
            setup.write_json(out / 'result.json', report)
    report['input_hashes_unchanged'] = all(setup.digest(v['path']) == v['sha256'] for v in report['inputs'].values())
    setup.write_json(out / 'result.json', report)
    print('Evidence: ' + str(out))
    return int(not report['input_hashes_unchanged'] or any(r['status'] != 'passed' for r in report['results'].values()))


if __name__ == '__main__':
    sys.exit(main())
