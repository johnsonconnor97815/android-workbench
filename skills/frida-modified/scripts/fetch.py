#!/usr/bin/env python3
"""Fetch an explicitly selected upstream version; never choose one by Android major."""
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
import lzma
from pathlib import Path
import re
import urllib.request
from common import run, sha256, utc, write_json
from provenance import git

OFFICIAL = 'https://github.com/frida/frida.git'


def fetch_source(version, destination):
    run(['git', 'clone', '--branch', version, '--depth', '1', '--recurse-submodules',
         '--shallow-submodules', OFFICIAL, destination], timeout=1800)
    return {'kind': 'source', 'url': OFFICIAL, 'tag': version,
            'commit': git(destination, 'rev-parse', 'HEAD'),
            'submodules': git(destination, 'submodule', 'status', '--recursive').splitlines()}


def fetch_release(version, destination, kind, abi):
    api = f'https://api.github.com/repos/frida/frida/releases/tags/{version}'
    request = urllib.request.Request(api, headers={'User-Agent': 'frida-modified-skill'})
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)
    extension = '.so.xz' if kind == 'gadget' else '.xz'
    name = f'frida-{kind}-{version}-android-{abi}{extension}'
    asset = next((a for a in release['assets'] if a['name'] == name), None)
    if asset is None:
        raise RuntimeError('No official asset for this combination: ' + name)
    destination.mkdir(parents=True, exist_ok=False)
    compressed = destination / name
    partial = destination / (name + '.part')
    with urllib.request.urlopen(asset['browser_download_url'], timeout=120) as response, partial.open('wb') as stream:
        while block := response.read(1024 * 1024):
            stream.write(block)
    digest = sha256(partial)
    upstream_digest = asset.get('digest')
    if upstream_digest and upstream_digest != 'sha256:' + digest:
        raise RuntimeError('Official asset digest mismatch; partial file retained')
    partial.rename(compressed)
    artifact = compressed.with_suffix('')
    with lzma.open(compressed, 'rb') as source, artifact.open('wb') as target:
        while block := source.read(1024 * 1024):
            target.write(block)
    if kind == 'server':
        artifact.chmod(0o755)
    return {'kind': kind, 'version': version, 'abi': abi, 'release_url': release['html_url'],
            'url': asset['browser_download_url'], 'compressed_sha256': digest,
            'upstream_digest': upstream_digest, 'upstream_digest_verified': bool(upstream_digest),
            'artifact': str(artifact.resolve()), 'sha256': sha256(artifact)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--kind', choices=['source', 'server', 'gadget'], required=True)
    parser.add_argument('--abi', choices=['arm', 'arm64', 'x86', 'x86_64'])
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*', args.version):
        parser.error('Expected an explicit upstream release version')
    if args.destination.exists() or args.record.exists():
        parser.error('Use new destination and record paths')
    if args.kind != 'source' and not args.abi:
        parser.error('--abi is required for binary assets')
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    record = {'schema': 1, 'utc': utc(), 'version': args.version, 'status': 'failed'}
    try:
        record.update(fetch_source(args.version, args.destination) if args.kind == 'source' else
                      fetch_release(args.version, args.destination, args.kind, args.abi))
        record['status'] = 'downloaded'
    except BaseException as error:
        record['error'] = f'{type(error).__name__}: {error}'
    write_json(args.record, record)
    print(f"{record['status']}: {args.record}")
    return 0 if record['status'] == 'downloaded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
