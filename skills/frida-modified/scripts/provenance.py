"""Fingerprint actual source and inputs, including submodule worktree changes."""
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
import hashlib
import json
import os
from pathlib import Path
import subprocess
from common import Blocked, digest_json, run, sha256, write_json


def git(source, *args):
    return run(['git', '-C', str(source), *args]).stdout.strip()


def repository_state(source):
    source = Path(source)
    diff = subprocess.check_output(['git', '-C', str(source), 'diff', '--binary', 'HEAD', '--'])
    untracked = {}
    names = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z', '--others', '--exclude-standard'])
    for raw_name in names.split(b'\0'):
        if not raw_name:
            continue
        name = os.fsdecode(raw_name)
        path = source / name
        if path.is_symlink():
            untracked[name] = {'symlink': str(path.readlink())}
        elif path.is_file():
            untracked[name] = sha256(path)
    return {'commit': git(source, 'rev-parse', 'HEAD'),
            'diff_sha256': hashlib.sha256(diff).hexdigest(), 'untracked': untracked}


def source_state(source):
    source = Path(source)
    result = {'.': repository_state(source)}
    for line in git(source, 'submodule', 'status', '--recursive').splitlines():
        if not line:
            continue
        if line[0] == 'U':
            raise Blocked('Conflicted submodule: ' + line)
        fields = line.lstrip(' +-').split()
        name = fields[1]
        if line[0] == '-':
            result[name] = {'commit': fields[0], 'initialized': False}
        else:
            result[name] = repository_state(source / name)
    return result


def fingerprint(source, files=(), options=None):
    value = {'source': source_state(source),
             'files': {str(Path(p).resolve()): sha256(p) for p in files}, 'options': options or {}}
    return {'identity': digest_json(value), 'inputs': value}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--file', type=Path, action='append', default=[])
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    current = fingerprint(args.source, args.file)
    if args.verify:
        previous = json.loads(args.record.read_text())
        if current != previous:
            raise SystemExit('Inputs changed; invalidate affected stages and use a fresh build directory')
        print('Input identity verified')
    else:
        if args.record.exists():
            raise SystemExit('Record exists; use a new evidence path')
        write_json(args.record, current)
        print(current['identity'])


if __name__ == '__main__':
    main()
