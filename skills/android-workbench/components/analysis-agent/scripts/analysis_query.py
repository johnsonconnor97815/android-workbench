#!/usr/bin/env python3
"""Strict offline original-byte index queries; registered projects use the queue."""

from __future__ import annotations

# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == '__main__':
    import sys as _awb_sys
    from pathlib import Path as _AwbPath
    for _awb_root in (_AwbPath.cwd(), *_AwbPath.cwd().parents):
        if (_awb_root / 'workbench.project.json').is_file():
            import json as _awb_json
            _awb_config = _awb_json.loads((_awb_root / 'workbench.project.json').read_text())
            _awb_runtime = (_awb_root / _awb_config.get('workbench_root', 'android-workbench')).resolve()
            if not (_awb_runtime / 'workbench/bridge.py').is_file():
                raise SystemExit('Android Workbench runtime missing; managed entry cannot run directly')
            _awb_sys.path.insert(0, str(_awb_runtime))
            from workbench.bridge import entry as _awb_entry
            _awb_entry(__file__)
            break
# END ANDROID WORKBENCH ENTRY

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


def load_engine():
    """Managed execution imports its pinned nested source, never a live fallback."""
    grant = os.environ.get('AWB_INTERNAL_GRANT')
    if grant:
        folder = Path(grant).resolve().parent
        spec = json.loads((folder / 'normalized.json').read_text())
        for index, source in enumerate(spec.get('nested_source_manifests', {})):
            if Path(source).name == 'scripts':
                path = folder / ('nested-source-' + str(index))
                if (path / 'apk_code_index.py').is_file() and (path / 'apk_navigation.py').is_file():
                    sys.path.insert(0, str(path))
                    import apk_code_index
                    return apk_code_index
        raise RuntimeError('Managed query lacks its pinned index/navigation source')
    for root in Path(__file__).resolve().parents:
        path = root / 'scripts'
        if (path / 'apk_code_index.py').is_file():
            sys.path.insert(0, str(path))
            import apk_code_index
            return apk_code_index
    raise RuntimeError('APK index query helpers are absent from this checkout')


def reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate request member: ' + key)
        value[key] = item
    return value


def parse_request(raw):
    if len(raw.encode('utf-8')) > 16384:
        raise ValueError('Request byte budget exceeded')
    return json.loads(raw, object_pairs_hook=reject_duplicate_keys,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON constant: ' + value)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--request', type=Path)
    group.add_argument('--query-json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = load_engine()
        if args.request:
            with args.request.open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('Request byte budget exceeded')
            raw = raw.decode('utf-8')
        else:
            raw = args.query_json
        request = parse_request(raw)
        result = engine.structured_query(args.db, request)
        code = 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        result = dict(schema='apk-query/v1', status='failed',
                      error=dict(code=getattr(exc, 'code', 'invalid_request'), message=str(exc),
                                 details=getattr(exc, 'details', {})))
        code = 2
    text = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n'
    if len(text.encode()) > 4 * 1024 * 1024:
        text = json.dumps(dict(schema='apk-query/v1', status='failed', error=dict(
            code='resource_limit', message='Query output byte budget exceeded'))) + '\n'
        code = 2
    if args.output:
        # Output paths cannot overwrite any selected index/sidecar or request.
        protected = {args.db.resolve(), Path(str(args.db.resolve()) + '-wal'), Path(str(args.db.resolve()) + '-shm')}
        if args.request:
            protected.add(args.request.resolve())
        if args.output.resolve() in protected:
            parser.error('--output must differ from database, sidecars and request')
        fd, temporary = tempfile.mkstemp(prefix='.' + args.output.name + '-', dir=args.output.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text); stream.flush(); os.fsync(stream.fileno())
            Path(temporary).replace(args.output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print(text, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
