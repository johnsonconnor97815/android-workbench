#!/usr/bin/env python3
"""Regenerate the 17.18.0 helper and synchronize its known embedded socket fields."""
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
import os
from pathlib import Path
from apply_profile import load_profile
from common import Blocked, run, sha256, write_json


def synchronize(source, profile_path, sdk, build_tools, helper_api):
    source, sdk = Path(source).resolve(), Path(sdk).resolve()
    profile = load_profile(profile_path, source)
    if profile['version'] != '17.18.0':
        raise Blocked('This resource adapter is specific to 17.18.0')
    core = source / 'subprojects/frida-core'
    field = profile['socket_field']
    old = field['old'].encode().ljust(field['size'], b'\0')
    new = field['new'].encode().ljust(field['size'], b'\0')
    if len(old) != field['size'] or len(new) != field['size']:
        raise Blocked('Socket field length changed')
    content = (core / 'src/linux/helpers/zymbiote.c').read_text()
    if f'char socket_path[{field["size"]}];' not in content or field['new'] not in content:
        raise Blocked('Apply runtime-names before regenerating resources')
    prepared = []
    for entry in profile['generated_resources']:
        path = (core / entry['path']).resolve()
        path.relative_to(core)
        before = path.read_bytes()
        actual = hashlib.sha256(before).hexdigest()
        if actual == entry['before_sha256'] and before.count(old) == 1 and before.count(new) == 0:
            after = before.replace(old, new, 1)
        elif actual == entry['after_sha256'] and before.count(new) == 1 and before.count(old) == 0:
            after = before
        else:
            raise Blocked('Embedded payload does not match known input: ' + entry['path'])
        if not before.startswith(b'\x7fELF') or len(after) != len(before) or hashlib.sha256(after).hexdigest() != entry['after_sha256']:
            raise Blocked('Embedded payload verification failed: ' + entry['path'])
        prepared.append((path, after))
    d8 = sdk / 'build-tools' / build_tools / 'd8'
    android_jar = sdk / 'platforms' / f'android-{helper_api}' / 'android.jar'
    if not d8.is_file() or not android_jar.is_file():
        raise Blocked('Required d8 or android.jar is missing')
    environment = os.environ.copy()
    environment.update(ANDROID_HOME=str(sdk), ANDROID_SDK_ROOT=str(sdk))
    environment['PATH'] = str(d8.parent) + os.pathsep + environment['PATH']
    helper = core / 'src/android-helper'
    run(['make', '-B', '-C', helper, f'ANDROID_API_LEVEL={helper_api}'], env=environment, timeout=120)
    dex = helper / 'helper.dex'
    if not dex.read_bytes().startswith(b'dex\n'):
        raise RuntimeError('Helper generation did not produce a DEX')
    for path, data in prepared:
        path.write_bytes(data)
    return {'helper_sha256': sha256(dex), 'payloads': {str(p.relative_to(core)): sha256(p) for p, _ in prepared}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['source', 'profile', 'sdk', 'record']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--build-tools', required=True)
    parser.add_argument('--helper-api', type=int, required=True)
    args = parser.parse_args()
    if args.record.exists():
        parser.error('Use a new record path')
    record = synchronize(args.source, args.profile, args.sdk, args.build_tools, args.helper_api)
    write_json(args.record, record)
    print(args.record)


if __name__ == '__main__':
    main()
