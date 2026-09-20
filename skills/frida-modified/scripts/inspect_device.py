#!/usr/bin/env python3
"""Read target/host facts without selecting a Frida version or changing services."""
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
import platform
from pathlib import Path
import sys
from common import Adb, Blocked, select_device, utc, write_json


def inspect(serial, adb='adb', check_root=False, package=None):
    device = Adb(select_device(serial, adb), adb)
    record = {'schema': 1, 'utc': utc(), 'device': device.serial,
              'host': {'system': platform.system(), 'arch': platform.machine(), 'python': platform.python_version()},
              'observations': {}, 'unavailable': {}}
    properties = {'android': 'ro.build.version.release', 'api': 'ro.build.version.sdk',
                  'fingerprint': 'ro.build.fingerprint', 'abis': 'ro.product.cpu.abilist',
                  'primary_abi': 'ro.product.cpu.abi', 'model': 'ro.product.model',
                  'art_library': 'persist.sys.dalvik.vm.lib.2'}
    for key, prop in properties.items():
        value = device.shell('getprop', prop)
        if value:
            record['observations'][key] = value
        else:
            record['unavailable'][key] = 'property empty'
    for key, argv in {'kernel': ['uname', '-a'], 'selinux': ['getenforce'], 'shell_uid': ['id', '-u']}.items():
        try:
            record['observations'][key] = device.shell(*argv)
        except RuntimeError as error:
            record['unavailable'][key] = str(error)
    if check_root:
        try:
            record['observations']['root_uid'] = device.shell('id', '-u', root=True)
        except RuntimeError as error:
            record['unavailable']['root'] = str(error)
    if package:
        record['package'] = package
        dump = device.shell('dumpsys', 'package', package)
        record['observations']['app_abis'] = [line.strip() for line in dump.splitlines()
                                             if 'primaryCpuAbi=' in line or 'secondaryCpuAbi=' in line]
        record['observations']['apk_paths'] = device.shell('pm', 'path', package).splitlines()
    record['selection'] = 'not_selected'
    record['status'] = 'complete' if all(k in record['observations'] for k in ['android', 'api', 'primary_abi']) else 'incomplete'
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--check-root', action='store_true')
    parser.add_argument('--package')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new output path; existing device evidence is retained')
    try:
        record = inspect(args.serial, args.adb, args.check_root, args.package)
        code = 0 if record['status'] == 'complete' else 2
    except (Blocked, RuntimeError) as error:
        record = {'utc': utc(), 'status': 'blocked', 'error': str(error), 'selection': 'not_selected'}
        code = 2
    write_json(args.output, record)
    print(f"{record['status']}: {args.output}")
    return code


if __name__ == '__main__':
    sys.exit(main())
