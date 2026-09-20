#!/usr/bin/env python3
"""Probe a local MCP stdio server and retain its JSON-RPC evidence (stdlib only)."""
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
import queue
import signal
import subprocess
import sys
import threading
import time


class Session:
    def __init__(self, spec, out, timeout=120):
        self.spec, self.out, self.timeout = spec, Path(out), timeout
        self.out.mkdir(parents=True, exist_ok=True)
        self.pending = queue.Queue()
        self.sequence = 0

    def __enter__(self):
        self.err = (self.out / 'stderr.log').open('w')
        self.trace = (self.out / 'rpc.jsonl').open('w')
        env = dict(os.environ, **self.spec.get('env', {}))
        try:
            self.process = subprocess.Popen([self.spec['command'], *self.spec.get('args', [])],
                cwd=self.spec.get('cwd'), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.err, text=True, encoding='utf-8', errors='replace', bufsize=1,
                start_new_session=True)
        except BaseException:
            self.trace.close()
            self.err.close()
            raise
        def reader():
            try:
                for line in self.process.stdout:
                    self.pending.put(line)
            finally:
                self.pending.put(None)
        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()
        return self

    def send(self, msg):
        self.trace.write(json.dumps({'direction': 'send', 'message': msg}) + '\n')
        self.trace.flush()
        self.process.stdin.write(json.dumps(msg) + '\n')
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'{method}: timed out after {self.timeout}s; {self.out}')
            try:
                line = self.pending.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f'{method}: timed out; {self.out}') from exc
            if line is None:
                raise RuntimeError(f'{method}: server closed stdout; see {self.out / "stderr.log"}')
            self.trace.write(json.dumps({'direction': 'receive', 'raw': line.rstrip()}) + '\n')
            self.trace.flush()
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError('Non-JSON data on MCP stdout: ' + line[:200]) from exc
            if msg.get('jsonrpc') != '2.0':
                raise RuntimeError('Invalid JSON-RPC version')
            if 'method' in msg:
                if 'id' in msg:
                    if msg['method'] == 'ping':
                        self.send({'jsonrpc': '2.0', 'id': msg['id'], 'result': {}})
                    else:
                        self.send({'jsonrpc': '2.0', 'id': msg['id'], 'error': {
                            'code': -32601, 'message': 'Probe client does not support this method'}})
                continue
            if msg.get('id') != ident:
                raise RuntimeError('Unexpected JSON-RPC response id')
            if 'error' in msg:
                raise RuntimeError(f'{method}: {msg["error"]}')
            if 'result' not in msg:
                raise RuntimeError('JSON-RPC response has no result')
            result = msg['result']
            if isinstance(result, dict) and result.get('isError'):
                raise RuntimeError(f'{method}: tool returned isError: {result}')
            return result

    def initialize(self):
        info = self.request('initialize', {'protocolVersion': '2025-03-26',
            'capabilities': {}, 'clientInfo': {'name': 'android-static-env-probe', 'version': '1'}})
        if not info.get('serverInfo') or 'tools' not in info.get('capabilities', {}):
            raise RuntimeError('Server did not advertise MCP tools')
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        tools, cursor, seen = [], None, set()
        for _ in range(100):
            page = self.request('tools/list', {'cursor': cursor} if cursor else {})
            tools.extend(page['tools'])
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError('Repeated tools/list cursor')
            seen.add(cursor)
        else:
            raise RuntimeError('Too many tools/list pages')
        if not tools:
            raise RuntimeError('Server returned no tools')
        self.tools = {tool['name']: tool for tool in tools}
        (self.out / 'tools.json').write_text(json.dumps(tools, ensure_ascii=False, indent=2) + '\n')
        return {'server': info['serverInfo'], 'protocol': info['protocolVersion'], 'tools': sorted(self.tools)}

    def call(self, name, arguments, expect=None):
        if name not in self.tools:
            raise RuntimeError('Tool not advertised: ' + name)
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        payloads = [result.get('structuredContent', {})]
        for content in result.get('content', []):
            if content.get('type') == 'text':
                try:
                    payloads.append(json.loads(content.get('text', '')))
                except json.JSONDecodeError:
                    pass
        if any(isinstance(p, dict) and (p.get('success') is False or p.get('status') == 'error') for p in payloads):
            raise RuntimeError(f'{name}: tool returned an application error: {result}')
        if expect and expect not in json.dumps(result, ensure_ascii=False):
            raise RuntimeError(f'{name}: expected evidence {expect!r} absent; see {self.out}')
        return result

    def __exit__(self, *_):
        # First EOF gives servers a chance to close Ghidra projects and databases.
        try:
            self.process.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        finally:
            self.reader.join(timeout=1)
            self.process.stdout.close()
            self.err.close()
            self.trace.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='Generated servers.json')
    parser.add_argument('--server', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--calls', type=Path, help='JSON list of {name, arguments, expect?}')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    spec = json.loads(args.config.read_text())[args.server]
    report = {'server_id': args.server, 'status': 'failed',
              'scope': 'tool-calls' if args.calls else 'protocol'}
    try:
        with Session(spec, args.output, args.timeout) as client:
            report.update(client.initialize())
            report['calls'] = []
            for call in json.loads(args.calls.read_text()) if args.calls else []:
                client.call(call['name'], call.get('arguments', {}), call.get('expect'))
                report['calls'].append(call['name'])
            report['status'] = 'passed'
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    sys.exit(main())
