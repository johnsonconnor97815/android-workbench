#!/usr/bin/env python3
"""Install/check an explicitly versioned host client; never contact a device."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'check'])
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--version', required=True)
    parser.add_argument('--tools-version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if any(not re.fullmatch(r'\d+\.\d+\.\d+', value) for value in (args.version, args.tools_version)):
        parser.error('Explicit release versions are required')
    project = args.workspace.expanduser().resolve()
    config = project / 'workbench.project.json'
    if not config.is_file():
        parser.error('A registered project and managed queue are required')
    document = json.loads(config.read_text())
    sys.path.insert(0, str((project / document['workbench_root']).resolve()))
    from workbench.bridge import entry
    from workbench.common import atomic_json
    from workbench.toolchains import binding
    entry(__file__, project=project)
    if args.output.exists():
        parser.error('Use a new evidence output')
    storage = Path(binding(project, 'frida')['workspace'])
    root = storage / 'Tools/runtime/frida-clients' / args.version
    python = root / 'venv/bin/python'
    result = {'schema': 1, 'pass': False, 'version': args.version,
              'frida_tools_version': args.tools_version, 'python': str(python),
              'scope': 'Host Python imports and CLI version only; no device, server or Hook verification',
              'commands': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def run(command, timeout=120):
        completed = subprocess.run(list(map(str, command)), stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=timeout,
                                   env={**os.environ, 'PIP_DISABLE_PIP_VERSION_CHECK': '1'})
        record = {'argv': list(map(str, command)), 'returncode': completed.returncode,
                  'stdout': completed.stdout[-12000:], 'stderr': completed.stderr[-12000:]}
        result['commands'].append(record)
        if completed.returncode:
            raise RuntimeError('Client command failed: ' + record['stderr'][-2000:])
        return completed.stdout

    check = ('import frida; from importlib.metadata import version; '
             'assert frida.__version__ == ' + repr(args.version) + '; '
             'assert version("frida-tools") == ' + repr(args.tools_version) + '; '
             'print(frida.__version__, version("frida-tools"))')
    try:
        ready = False
        if python.is_file():
            try:
                run([python, '-c', check])
                ready = True
            except (OSError, RuntimeError, subprocess.SubprocessError):
                if args.action == 'install':
                    backup = root / 'repair-backups' / uuid.uuid4().hex
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    python.parent.parent.rename(backup)
                    result['preserved_venv'] = str(backup)
        if not ready and args.action == 'install':
            root.mkdir(parents=True, exist_ok=True)
            if not python.is_file():
                run([sys.executable, '-m', 'venv', root / 'venv'])
            run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--force-reinstall',
                 '--report', root / 'pip-report.json', 'frida==' + args.version,
                 'frida-tools==' + args.tools_version], timeout=1200)
            (root / 'requirements.txt').write_text(run([python, '-m', 'pip', 'freeze']))
        run([python, '-c', check])
        run([root / 'venv/bin/frida', '--version'])
        run([python, '-m', 'pip', 'check'])
        result['pass'] = True
        if args.action == 'install':
            atomic_json(root / 'client.json', {**result, 'checked_at': time.time()})
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
    atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
