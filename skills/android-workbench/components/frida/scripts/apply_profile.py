#!/usr/bin/env python3
"""Apply a reviewed, commit-bound profile to an isolated clean source tree."""
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
from pathlib import Path
from common import Blocked, run, sha256, utc, write_json
from provenance import git


def load_profile(path, source):
    profile = json.loads(Path(path).read_text())
    if profile.get('schema') != 1:
        raise Blocked('Unsupported profile schema')
    source = Path(source).resolve()
    for name, expected in profile['commits'].items():
        repository = (source / name).resolve()
        repository.relative_to(source)
        actual = git(repository, 'rev-parse', 'HEAD')
        if actual != expected:
            raise Blocked(f'Profile commit mismatch: {name}: expected {expected}, found {actual}')
    for group in profile['groups']:
        for patch in group['patches']:
            file = (Path(path).parent / patch['file']).resolve()
            file.relative_to(Path(path).parent.resolve())
            if sha256(file) != patch['sha256']:
                raise Blocked('Patch SHA-256 mismatch: ' + patch['file'])
    return profile


def apply(source, profile_path, groups=None, check=False):
    source, profile_path = Path(source).resolve(), Path(profile_path).resolve()
    profile = load_profile(profile_path, source)
    available = {group['name'] for group in profile['groups']}
    wanted = set(groups) if groups else available
    if not wanted or not wanted <= available:
        raise Blocked('Unknown or empty patch group selection')
    patches = [patch for group in profile['groups'] if group['name'] in wanted for patch in group['patches']]
    # A new application starts clean. Resume uses the saved receipt; never reapply blindly.
    for name in profile['commits']:
        if git(source / name, 'status', '--porcelain', '--untracked-files=normal'):
            raise Blocked('Source is not clean: ' + name)
    for patch in patches:
        repository = (source / patch['repository']).resolve()
        repository.relative_to(source)
        run(['git', '-C', repository, 'apply', '--check', profile_path.parent / patch['file']])
    applied = []
    try:
        if not check:
            for patch in patches:
                run(['git', '-C', source / patch['repository'], 'apply', profile_path.parent / patch['file']])
                applied.append(patch)
    except BaseException as error:
        recovery = []
        for patch in reversed(applied):
            try:
                run(['git', '-C', source / patch['repository'], 'apply', '--reverse', profile_path.parent / patch['file']])
            except BaseException as rollback_error:
                recovery.append(str(rollback_error))
        if recovery:
            raise RuntimeError(f'{error}; patch rollback incomplete: {recovery}') from error
        raise
    return {'schema': 1, 'utc': utc(), 'status': 'checked' if check else 'applied',
            'profile_sha256': sha256(profile_path), 'source': str(source),
            'version': profile['version'], 'groups': sorted(wanted), 'commits': profile['commits'],
            'patches': patches, 'resources': 'pending' if 'runtime-names' in wanted else 'not_required'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--group', action='append')
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.record.exists():
        parser.error('Use a new record path; existing evidence is retained')
    try:
        record = apply(args.source, args.profile, args.group, args.check)
        code = 0
    except BaseException as error:
        code = 2 if isinstance(error, Blocked) else 1
        record = {'utc': utc(), 'status': 'blocked' if code == 2 else 'failed', 'error': str(error)}
    write_json(args.record, record)
    print(f"{record['status']}: {args.record}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
