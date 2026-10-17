#!/usr/bin/env python3
"""Finite offline JS lexical clues, flat source-map v3 mappings and HAR evidence."""

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
import base64
import binascii
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA = 'analysis-offline/v1'
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_BUDGETS = dict(max_bytes=8 * 1024 * 1024, max_items=1000, max_content_bytes=1024 * 1024)
BASE64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'


class OfflineError(ValueError):
    def __init__(self, code, message):
        super().__init__(message); self.code = code


def fail(code, message):
    raise OfflineError(code, message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail('invalid_json', 'Duplicate JSON member: ' + key)
        value[key] = item
    return value


def read_json(raw):
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            fail('invalid_json', 'Non-finite JSON number')
        return parsed
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=duplicate_keys,
                          parse_constant=lambda x: fail('invalid_json', 'Non-finite JSON constant'), parse_float=finite_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        fail('invalid_json', str(exc))


def artifact(path, budgets):
    path = Path(path).resolve()
    if not path.is_file():
        fail('input_missing', 'Explicit input must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(budgets['max_bytes'] + 1)
    if len(raw) > budgets['max_bytes']:
        fail('resource_limit', 'Input byte budget exceeded')
    return raw, dict(path=str(path), size=len(raw), sha256=sha(raw))


def safe_url(value):
    """Display URL identity without userinfo, query values or fragments."""
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return '<non-http-url>'
        host = parts.hostname
        if ':' in host:
            host = '[' + host + ']'
        if parts.port is not None:
            host += ':' + str(parts.port)
        query = urlencode([(key, '<redacted>') for key, _ in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=1000)])
        return urlunsplit((parts.scheme, host, parts.path, query, ''))
    except ValueError:
        return '<invalid-url>'


def result(kind, inputs, budgets, **fields):
    return dict(schema=SCHEMA, kind=kind, inputs=inputs, configuration=budgets,
                status='succeeded', semantic_complete=False, **fields)


def vlq(segment):
    values = []; value = shift = 0
    for char in segment:
        digit = BASE64.find(char)
        if digit < 0 or shift > 30:
            fail('invalid_source_map', 'Invalid or oversized VLQ segment')
        value |= (digit & 31) << shift
        if digit & 32:
            shift += 5
        else:
            values.append(-(value >> 1) if value & 1 else value >> 1)
            value = shift = 0
    if shift:
        fail('invalid_source_map', 'Truncated VLQ segment')
    if len(values) not in (1, 4, 5):
        fail('unsupported_source_map', 'Only 1/4/5-field flat v3 segments are supported')
    return values


def source_map(raw, budgets, line=None, column=None):
    mapping = read_json(raw)
    if not isinstance(mapping, dict) or mapping.get('version') != 3 or type(mapping.get('version')) is not int:
        fail('unsupported_source_map', 'Only source-map version 3 is supported')
    if mapping.keys() - {'version', 'file', 'sourceRoot', 'sources', 'names', 'mappings', 'sourcesContent'}:
        fail('unsupported_source_map', 'Indexed maps and unknown map extensions are unsupported')
    sources, names, encoded = mapping.get('sources'), mapping.get('names'), mapping.get('mappings')
    if not isinstance(sources, list) or not all(isinstance(x, str) for x in sources) or not isinstance(names, list) or not all(isinstance(x, str) for x in names) or not isinstance(encoded, str):
        fail('invalid_source_map', 'sources/names must be string arrays and mappings a string')
    if any(key in mapping and not isinstance(mapping[key], str) for key in ('file', 'sourceRoot')):
        fail('invalid_source_map', 'file/sourceRoot must be strings')
    if encoded.startswith(',') or re.search(r',(?:[,;]|$)|;,', encoded):
        fail('invalid_source_map', 'Empty mapping segments are invalid')
    if len(sources) + len(names) > budgets['max_items']:
        fail('resource_limit', 'Source-map source/name budget exceeded')
    contents = mapping.get('sourcesContent')
    if contents is not None and (not isinstance(contents, list) or len(contents) != len(sources) or any(x is not None and not isinstance(x, str) for x in contents)):
        fail('invalid_source_map', 'sourcesContent must match sources and contain text or null')
    content_bytes = sum(len(x.encode('utf-8')) for x in (contents or []) if x is not None)
    if content_bytes > budgets['max_content_bytes']:
        fail('resource_limit', 'Embedded source content budget exceeded')
    source_records = []
    for index, source in enumerate(sources):
        content = contents[index] if contents is not None else None
        external = bool(urlsplit(source).scheme or source.startswith(('/', '\\')) or '..' in source.replace('\\', '/').split('/'))
        source_records.append(dict(index=index, source=safe_url(source) if urlsplit(source).scheme else source,
                                   source_sha256=sha(source.encode()),
                                   content_sha256=sha(content.encode()) if content is not None else None,
                                   content_status='embedded' if content is not None else 'external_reference_not_read' if external else 'source_not_supplied'))
    decoded = []; source_index = original_line = original_column = name_index = 0; truncated = False
    # Regex iteration avoids allocating an unbounded array of split segments.
    generated_line = 1; generated_column = 0; previous_column = -1
    for part in re.finditer(r'[^,;]+|[,;]', encoded):
        segment = part.group()
        if segment == ';':
            generated_line += 1; generated_column = 0; previous_column = -1
            if generated_line > budgets['max_items'] * 100:
                truncated = True; break
            continue
        if segment == ',':
            continue
        if len(decoded) >= budgets['max_items']:
            truncated = True; break
        values = vlq(segment); generated_column += values[0]
        if generated_column < 0 or generated_column <= previous_column:
            fail('invalid_source_map', 'Generated columns must strictly increase within a line')
        previous_column = generated_column
        item = dict(generated_line=generated_line, generated_column=generated_column, mapped=len(values) > 1)
        if len(values) > 1:
            source_index += values[1]; original_line += values[2]; original_column += values[3]
            if not 0 <= source_index < len(sources) or original_line < 0 or original_column < 0:
                fail('invalid_source_map', 'Mapping source/original position is out of range')
            item.update(source_index=source_index, original_line=original_line + 1, original_column=original_column)
            if len(values) == 5:
                name_index += values[4]
                if not 0 <= name_index < len(names):
                    fail('invalid_source_map', 'Mapping name is out of range')
                item['name_index'] = name_index
        decoded.append(item)
    lookup = None
    if line is not None:
        candidates = [x for x in decoded if x['generated_line'] == line and x['generated_column'] <= column]
        lookup = dict(status='unknown' if truncated or not candidates or not candidates[-1]['mapped'] else 'mapped',
                      generated_line=line, generated_column=column, mapping=None if truncated or not candidates else candidates[-1])
    source_root = mapping.get('sourceRoot', '')
    return dict(status='partial' if truncated else 'succeeded', mappings=decoded, sources=source_records, names=names,
                source_root=safe_url(source_root) if urlsplit(source_root).scheme else source_root,
                lookup=lookup, truncated=truncated, missing_source_contents=sum(x['content_status'] != 'embedded' for x in source_records),
                limits=['Flat v3 maps only; lines are 1-based, columns are 0-based UTF-16 units.',
                        'Declared mappings are not independently verified against original source.',
                        'No source URL/path is fetched or opened; missing content remains unknown.'])


TOKEN = re.compile(r'(?P<string>"(?:\\[\s\S]|[^"\\\n])*"|\'(?:\\[\s\S]|[^\'\\\n])*\')|(?P<identifier>[$A-Za-z_][$\w]*)|(?P<punct>[^\s])')
BRIDGES = {'addJavascriptInterface', 'WebViewJavascriptBridge', 'ReactNativeWebView', 'postMessage', 'ipcRenderer', 'contextBridge', 'cordova', 'Capacitor'}


def lexical_tokens(text):
    """Consume each comment once; an unclosed comment consumes the remainder.

    Anchored token matching cannot retry a failed block-comment regex at every
    slash. Unsupported unclosed quotes also stop rather than retrying suffixes.
    """
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1; continue
        if text.startswith('//', position):
            end = text.find('\n', position + 2)
            end = len(text) if end < 0 else end
            yield 'comment', position, end, text[position:end]
        elif text.startswith('/*', position):
            closing = text.find('*/', position + 2)
            end = len(text) if closing < 0 else closing + 2
            yield 'unterminated_comment' if closing < 0 else 'comment', position, end, text[position:end]
            if closing < 0:
                return
        else:
            match = TOKEN.match(text, position)
            end = match.end()
            if match.lastgroup == 'punct' and match.group() in ('"', "'"):
                yield 'unterminated_string', position, len(text), text[position:]
                return
            yield match.lastgroup, position, end, match.group()
        position = end


def string_value(raw):
    """Declared small escape subset; unsupported strings remain unknown."""
    value = raw[1:-1]; decoded = []; position = 0
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0',
               '\\': '\\', "'": "'", '"': '"'}
    while position < len(value):
        char = value[position]; position += 1
        if char != '\\':
            decoded.append(char); continue
        if position >= len(value):
            return None
        char = value[position]; position += 1
        if char == '0' and position < len(value) and value[position] in '0123456789':
            return None  # Legacy octal and non-octal decimal escapes are outside this subset.
        if char in escapes:
            decoded.append(escapes[char])
        elif char in ('x', 'u'):
            length = 2 if char == 'x' else 4
            digits = value[position:position + length]
            if len(digits) != length or any(x not in '0123456789abcdefABCDEF' for x in digits):
                return None
            decoded.append(chr(int(digits, 16))); position += length
        else:
            return None
    return ''.join(decoded)


def javascript(raw, budgets, supplied_map=None):
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        fail('unsupported_javascript', 'Only plaintext UTF-8 JavaScript is supported; no HBC decoding')
    tokens = []; issues = []; byte_offset = previous = 0; line = 1; column = 0; map_match = None
    for match_index, (token_type, start, end, chunk) in enumerate(lexical_tokens(text)):
        if match_index >= budgets['max_items'] * 20:
            issues.append('Lexical token budget exhausted'); break
        prefix = text[previous:start]; byte_offset += len(prefix.encode())
        if '\n' in prefix:
            line += prefix.count('\n'); column = len(prefix.rsplit('\n', 1)[1].encode('utf-16le')) // 2
        else:
            column += len(prefix.encode('utf-16le')) // 2
        item = dict(token=chunk, type=token_type, byte_offset=byte_offset, line=line, column=column)
        if token_type.startswith('unterminated_'):
            issues.append('Unterminated or unsupported string/comment syntax'); break
        if token_type == 'comment':
            found = re.search(r'[#@]\s*sourceMappingURL\s*=\s*(\S+)', chunk)
            if found:
                map_match = found.group(1)
        else:
            tokens.append(item)
            if item['token'] in ('`', '/'):
                issues.append('Template/regex/division syntax is outside the lexical subset')
            if item['token'] in ('"', "'"):
                issues.append('Unterminated or unsupported string/comment syntax')
        byte_offset += len(chunk.encode()); previous = end
        if '\n' in chunk:
            line += chunk.count('\n'); column = len(chunk.rsplit('\n', 1)[1].encode('utf-16le')) // 2
        else:
            column += len(chunk.encode('utf-16le')) // 2
    findings = []
    for index, token in enumerate(tokens):
        name = token['token']; literal = string_value(name) if token['type'] == 'string' else None
        if token['type'] == 'string' and literal is None:
            issues.append('Unsupported string escape spelling')
        kind = None; value = None
        if name in BRIDGES or literal in BRIDGES:
            kind, value = 'bridge_lexical_clue', literal or name
        elif literal is not None and re.match(r'https?://', literal):
            kind, value = 'url_literal', safe_url(literal)
        elif literal is not None:
            prior = [x['token'] for x in tokens[max(0, index - 8):index]]
            if len(prior) >= 2 and prior[-2:] in (['require', '('], ['import', '(']):
                kind, value = 'module_literal', literal
            elif prior and (prior[-1] == 'import' or (prior[-1] == 'from' and any(x in prior for x in ('import', 'export')))):
                kind, value = 'module_literal', literal
        if kind:
            if len(findings) >= budgets['max_items']:
                issues.append('Lexical finding budget exhausted'); break
            findings.append(dict(kind=kind, value=value, literal_sha256=sha(name.encode()),
                                 byte_offset=token['byte_offset'], line=token['line'], column=token['column']))
    map_result = None; map_status = 'not_declared'
    if supplied_map is not None:
        map_result = source_map(supplied_map, budgets); map_status = 'explicit_map'
    elif map_match:
        if map_match.startswith('data:application/json;base64,'):
            encoded = map_match.split(',', 1)[1]
            if len(encoded) > 4 * ((budgets['max_bytes'] + 2) // 3):
                fail('resource_limit', 'Embedded map byte budget exceeded')
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                fail('invalid_source_map', 'Invalid embedded map base64')
            map_result = source_map(decoded, budgets); map_status = 'embedded_map'
        elif map_match.startswith('data:'):
            fail('unsupported_source_map', 'Only base64 application/json embedded maps are supported')
        else:
            map_status = 'declared_map_not_supplied'
    truncated = bool(issues)
    return dict(status='partial' if truncated or (map_result and map_result['truncated']) else 'succeeded',
                findings=findings, lexical_scan_complete=not truncated, source_map=map_result,
                source_map_status=map_status, truncated=truncated, unknowns=sorted(set(issues)),
                limits=['Lexical clues only: no AST, full import coverage, call graph, deobfuscation or behavior proof.',
                        'Comments and supported quoted strings are separated; template/regex syntax can make coverage partial.',
                        'Offsets are original UTF-8 byte offsets; columns are 0-based UTF-16 units; LF lines are 1-based.',
                        'Absent/unsupplied source maps and source contents remain unknown; no implicit filesystem/network reads.'])


def body_digest(content, budgets):
    if not isinstance(content, dict):
        fail('invalid_har', 'Response content must be an object')
    if 'text' not in content or content['text'] is None:
        return dict(status='not_recorded', sha256=None)
    text = content['text']
    if not isinstance(text, str):
        fail('invalid_har', 'Content text must be a string or null')
    if len(text.encode()) > budgets['max_content_bytes'] * 2:
        return dict(status='budget_exhausted', sha256=None)
    try:
        if content.get('encoding') == 'base64':
            data = base64.b64decode(text, validate=True)
        elif content.get('encoding') in (None, ''):
            data = text.encode('utf-8')
        else:
            return dict(status='unsupported_encoding', sha256=None)
    except (ValueError, binascii.Error):
        return dict(status='invalid_base64', sha256=None)
    if len(data) > budgets['max_content_bytes']:
        return dict(status='budget_exhausted', sha256=None)
    return dict(status='recorded', sha256=sha(data), bytes=len(data))


def har(raw, budgets):
    document = read_json(raw)
    log = document.get('log') if isinstance(document, dict) else None
    if not isinstance(log, dict) or log.get('version') != '1.2' or not isinstance(log.get('entries'), list):
        fail('unsupported_har', 'Only HAR 1.2 with an entries array is supported')
    entries = log['entries']; records = []; issues = []
    for ordinal, entry in enumerate(entries[:budgets['max_items']]):
        if not isinstance(entry, dict) or not isinstance(entry.get('request'), dict) or not isinstance(entry.get('response'), dict):
            fail('invalid_har', 'Entry request/response must be objects')
        request, response = entry['request'], entry['response']
        method, url, status = request.get('method'), request.get('url'), response.get('status')
        if not isinstance(method, str) or not method or not isinstance(url, str) or type(status) is not int or not 0 <= status <= 599:
            fail('invalid_har', 'Invalid entry method/URL/status')
        time = entry.get('time'); started = entry.get('startedDateTime')
        if time is not None and (type(time) not in (int, float) or time < -1):
            fail('invalid_har', 'Entry time must be a number at least -1')
        if started is not None and not isinstance(started, str):
            fail('invalid_har', 'startedDateTime must be text')
        if started is not None:
            try:
                if 'T' not in started or datetime.datetime.fromisoformat(started.replace('Z', '+00:00')).tzinfo is None:
                    raise ValueError('missing time zone')
            except ValueError:
                fail('invalid_har', 'startedDateTime must have an ISO date/time and time zone')
        content = body_digest(response.get('content', {}), budgets)
        if content['status'] != 'recorded':
            issues.append(content['status'])
        if time is None or time == -1 or started is None or status == 0:
            issues.append('missing_timing_or_response')
        displayed_url = safe_url(url)
        if displayed_url.startswith('<'):
            issues.append('unsupported_or_invalid_url')
        record_hash = sha(canonical(entry).encode())
        records.append(dict(ordinal=ordinal, record_sha256=record_hash, method=method,
                            url=displayed_url, url_sha256=sha(url.encode()), status=status,
                            startedDateTime=started, time_ms=time, content=content))
    truncated = len(entries) > budgets['max_items']
    if truncated:
        issues.append('record_budget_exhausted')
    return dict(status='partial' if issues else 'succeeded', records=records, declared_records=len(entries),
                truncated=truncated, unknowns=sorted(set(issues)),
                limits=['Recorded HAR subset only; no URL is fetched and no application is executed.',
                        'Display URLs redact userinfo/query values/fragments; full URL identity is retained only as a hash.',
                        'Header, cookie and body values are omitted; body hashes cover only recorded supported content.',
                        'Missing records/content or exhausted budgets do not establish equality.'])


def comparison(baseline, candidate, kind):
    field = 'findings' if kind == 'js' else 'records'
    left, right = baseline[field], candidate[field]
    if kind == 'js':
        key = lambda item: canonical({k: item[k] for k in ('kind', 'value', 'literal_sha256', 'byte_offset')})
    else:
        key = lambda item: canonical({k: item[k] for k in ('ordinal', 'method', 'url_sha256', 'status', 'content')})
    left_keys, right_keys = [key(x) for x in left], [key(x) for x in right]
    admitted = baseline['status'] == candidate['status'] == 'succeeded' and bool(left) and bool(right)
    return dict(status='succeeded' if admitted else 'partial', comparison_kind=kind, comparison='same_observed_subset' if admitted and left_keys == right_keys else 'different_observed_subset' if admitted else 'unknown',
                baseline_status=baseline['status'], candidate_status=candidate['status'],
                baseline_count=len(left), candidate_count=len(right),
                baseline_observation_sha256=sha(canonical(left_keys).encode()), candidate_observation_sha256=sha(canonical(right_keys).encode()),
                limits=['Only admitted lexical clues or the explicit recorded HAR sequence are compared.',
                        'Duplicate HAR requests retain ordinals; timing/header/body differences beyond the admitted projection are unknown.',
                        'No application behavior equivalence is claimed; empty, partial or truncated observations yield unknown.'])


def write_output(path, text):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    for command in ('js', 'source-map', 'har', 'compare'):
        sub = subs.add_parser(command)
        if command == 'compare':
            sub.add_argument('--baseline', type=Path, required=True); sub.add_argument('--candidate', type=Path, required=True)
            sub.add_argument('--kind', choices=('js', 'har'), required=True)
        else:
            sub.add_argument('--input', type=Path, required=True)
        if command == 'js':
            sub.add_argument('--map', type=Path)
        if command == 'source-map':
            sub.add_argument('--line', type=int); sub.add_argument('--column', type=int)
        sub.add_argument('--output', type=Path)
        for name, default in DEFAULT_BUDGETS.items():
            sub.add_argument('--' + name.replace('_', '-'), type=int, default=default)
    args = parser.parse_args(argv)
    budgets = {key: getattr(args, key) for key in DEFAULT_BUDGETS}
    inputs = []
    try:
        if not 1 <= budgets['max_bytes'] <= 32 * 1024 * 1024 or not 1 <= budgets['max_items'] <= 5000 or not 1 <= budgets['max_content_bytes'] <= 4 * 1024 * 1024:
            fail('invalid_request', 'Invalid byte/item/content budgets')
        if args.command == 'source-map' and ((args.line is None) != (args.column is None) or (args.line is not None and (args.line < 1 or args.column < 0))):
            fail('invalid_request', 'Position needs --line >=1 and --column >=0 together')
        paths = [args.baseline, args.candidate] if args.command == 'compare' else [args.input]
        if getattr(args, 'map', None):
            paths.append(args.map)
        if args.output and args.output.resolve() in {path.resolve() for path in paths}:
            fail('invalid_request', 'Output must differ from all inputs')
        raw_inputs = []
        for path in paths:
            raw, identity = artifact(path, budgets); inputs.append(identity); raw_inputs.append(raw)
        if sum(len(raw) for raw in raw_inputs) > budgets['max_bytes']:
            fail('resource_limit', 'Aggregate input byte budget exceeded')
        if args.command == 'js':
            fields = javascript(raw_inputs[0], budgets, raw_inputs[1] if len(raw_inputs) > 1 else None)
        elif args.command == 'source-map':
            fields = source_map(raw_inputs[0], budgets, args.line, args.column)
        elif args.command == 'har':
            fields = har(raw_inputs[0], budgets)
        else:
            analyze = javascript if args.kind == 'js' else har
            fields = comparison(analyze(raw_inputs[0], budgets), analyze(raw_inputs[1], budgets), args.kind)
        status = fields.pop('status')
        value = result(args.command, inputs, budgets, **fields); value['status'] = status
        # Re-read only explicitly provided files, with the same bounds. Changes
        # during analysis invalidate the output; no map source is opened here.
        for path, identity in zip(paths, inputs):
            if artifact(path, budgets)[1] != identity:
                fail('input_changed', 'Input changed during offline analysis')
        code = 0
    except (ValueError, OSError, RecursionError) as exc:
        value = dict(schema=SCHEMA, kind=args.command, inputs=inputs, configuration=budgets,
                     status='failed', error=dict(code=getattr(exc, 'code', 'invalid_input'), message=str(exc)))
        code = 2
    text = canonical(value) + '\n'
    if len(text.encode()) > MAX_OUTPUT_BYTES:
        text = canonical(dict(schema=SCHEMA, status='failed', inputs=inputs, error=dict(code='resource_limit', message='Output byte budget exceeded'))) + '\n'; code = 2
    if args.output:
        # A failed request must not overwrite its input either.
        protected = [getattr(args, key, None) for key in ('input', 'map', 'baseline', 'candidate')]
        if args.output.resolve() in {path.resolve() for path in protected if path is not None}:
            print(text, end=''); return 2
        write_output(args.output, text)
    print(text, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
