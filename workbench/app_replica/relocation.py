"""One guarded schema-2 adoption. No lifecycle reconciliation or model execution."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .backend import HostSlots, process_identity, _same_process
from .ids import canonical_json
from .paths import PathBindings, effective_project, effective_writer_index, lexical, no_symlinks, read_regular, hash_regular
from .store import ReplicaError, Store


def digest(content):
    return hashlib.sha256(content).hexdigest()


def bound_file(reference):
    if (not isinstance(reference, dict) or not isinstance(reference.get('path'), str)
            or not re.fullmatch('[0-9a-f]{64}', str(reference.get('sha256', '')))):
        raise ReplicaError('Evidence requires a local path and exact SHA-256')
    path = lexical(reference['path'])
    content = read_regular(path)
    if digest(content) != reference['sha256']:
        raise ReplicaError(f'Evidence SHA-256 mismatch: {path}')
    return content


@contextmanager
def lock_file(path):
    path = no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReplicaError('Fence lock must be a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReplicaError(f'Migration fence lock is occupied: {path}') from exc
        yield
    finally:
        os.close(fd)


def validate_plan(store, state, request, source):
    if type(request.get('expected_schema')) is not int or request['expected_schema'] != 2:
        raise ReplicaError('Relocation expected_schema must be exactly 2')
    if state.get('schema') != 2:
        raise ReplicaError('Second relocation of an adopted workspace is unsupported')
    if (type(request.get('expected_revision')) is not int or request['expected_revision'] < 0
            or state['revision'] != request['expected_revision']
            or not re.fullmatch('[0-9a-f]{64}', str(request.get('expected_sha256', '')))
            or digest(source) != request['expected_sha256']):
        raise ReplicaError('Exact relocation source revision/SHA-256 guard failed')
    if type(request.get('expected_binding_revision')) is not int or request['expected_binding_revision'] != 0:
        raise ReplicaError('Initial relocation requires binding revision 0')
    for key in ('origin_workspace', 'target_workspace'):
        if not isinstance(request.get(key), str) or str(lexical(request[key])) != request[key]:
            raise ReplicaError(f'{key} must be a normalized absolute path')
    if request['target_workspace'] != str(store.workspace):
        raise ReplicaError('Relocation target must equal actual workspace')
    if request.get('continuation_backend') != 'native':
        raise ReplicaError('Relocation supports native continuation only')
    if any(not isinstance(request.get(k), str) or not request[k].strip() for k in ('actor', 'reason')):
        raise ReplicaError('Relocation requires actor and reason')
    rules = request.get('bindings')
    if not isinstance(rules, list) or not rules:
        raise ReplicaError('Relocation requires directory bindings')
    roots = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'from', 'to'}:
            raise ReplicaError('Bindings require only from and to roots')
        old, new = lexical(rule['from']), lexical(rule['to'])
        if str(old) != rule['from'] or str(new) != rule['to'] or old == new or old == Path('/') or new == Path('/'):
            raise ReplicaError('Binding roots must be distinct normalized absolute directories')
        no_symlinks(new)
        if not new.is_dir():
            raise ReplicaError(f'Binding destination directory is unavailable: {new}')
        roots.append((old, new))
    for i, (old, new) in enumerate(roots):
        for j, (other_old, other_new) in enumerate(roots):
            if i != j and (old.is_relative_to(other_old) or new == other_new):
                raise ReplicaError('Conflicting or ambiguous directory bindings')
            if new.is_relative_to(other_old) or other_old.is_relative_to(new):
                raise ReplicaError('Binding chains/cycles are unsupported')
    resolver = PathBindings(store.workspace, rules, 1)
    if resolver.path(request['origin_workspace']) != store.workspace:
        raise ReplicaError('Origin workspace does not bind to target workspace')
    evidence = request.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ReplicaError('Relocation requires hash-bound source evidence')
    for item in evidence:
        bound_file(item)
    return resolver


def validate_fence(store, state, request, resolver):
    fence = request.get('fence', {})
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if fence.get('boot_id') != boot:
        raise ReplicaError('Fresh current boot fence required')
    observation = json.loads(bound_file(fence.get('local_writer_observation')))
    now = time.time()
    observed = observation.get('observed_at')
    expires = observation.get('expires_at')
    if (type(observed) not in (int, float) or type(expires) not in (int, float)
            or not now - 300 <= observed <= now or not now < expires <= observed + 300):
        raise ReplicaError('Current-clone observation expired or invalid')
    if (observation.get('workspace') != str(store.workspace) or observation.get('boot_id') != boot
            or observation.get('authority_sha256') != request['expected_sha256']
            or observation.get('authority_revision') != state['revision']
            or observation.get('method') != 'main_current_clone_takeover'
            or observation.get('dispatcher_stopped') is not True
            or observation.get('unrecorded_writers') != []
            or not isinstance(observation.get('ownership_basis'), str) or not observation['ownership_basis'].strip()
            or not _same_process(observation.get('observer'), process_identity((observation.get('observer') or {}).get('pid')))):
        raise ReplicaError('Explicit live Main current-clone ownership evidence required')
    active = {key for key, run in state['runs'].items() if run['state'] != 'stopped'}
    proofs = fence.get('active_runs')
    if (not isinstance(proofs, list) or len(proofs) != len(active)
            or {item.get('run_id') for item in proofs} != active
            or set(observation.get('recorded_run_ids', [])) != active):
        raise ReplicaError('Fence must bind the exact nonstopped run set')
    for item in proofs:
        identifier = item['run_id']
        run = state['runs'][identifier]
        if run.get('backend') != 'codex':
            raise ReplicaError('Unknown or active native writer blocks relocation')
        records = {}
        for key, filename in (('record', 'run.json'), ('supervisor', 'supervisor.json'), ('completion', 'completion.json')):
            reference = item.get(key)
            expected = store.workspace / 'coordination/backend' / identifier / filename
            if not reference or resolver.path(reference['path']) != expected:
                raise ReplicaError('Run proof must use its exact restored backend file')
            records[key] = json.loads(bound_file(reference))
        record, supervisor, completion = (records[k] for k in ('record', 'supervisor', 'completion'))
        token = record.get('launch_token')
        identity = record.get('process')
        if (record.get('run_id') != identifier or not token
                or supervisor.get('launch_token') != token or completion.get('launch_token') != token
                or completion.get('kind') != 'tree_reaped' or completion.get('subreaper') is not True
                or supervisor.get('subreaper') is not True or type(completion.get('exit_code')) is not int
                or not _same_process(identity, supervisor.get('process'))
                or not _same_process(identity, completion.get('supervisor'))
                or resolver.path(record.get('request', {}).get('workspace')) != resolver.path(state['project'].get('execution_root', request['origin_workspace']))):
            raise ReplicaError('Restored run/token/supervisor/tree completion proof mismatch')
        handle = run.get('backend_handle')
        if handle is not None:
            if not isinstance(handle, dict):
                raise ReplicaError('Unknown authoritative backend handle blocks relocation')
            if (handle.get('run_id') != identifier
                    or not _same_process(handle.get('process'), identity)
                    or handle.get('paths') != record.get('paths')
                    or handle.get('requested') != {'model': run['model'], 'effort': run['effort']}
                    or record.get('request', {}).get('requested') != handle.get('requested')
                    or ('command' in handle and handle['command'] != record.get('command'))):
                raise ReplicaError('Authoritative run handle does not bind this restored tree')
            expected_token = (handle.get('supervisor_evidence') or {}).get('launch_token')
            if expected_token is not None and expected_token != token:
                raise ReplicaError('Authoritative supervisor launch token mismatch')
            prior_completion = handle.get('completion') or {}
            if prior_completion.get('launch_token') is not None and prior_completion['launch_token'] != token:
                raise ReplicaError('Authoritative completion launch token mismatch')
        for key, filename in (('record', 'run.json'), ('supervisor', 'supervisor.json'), ('completion', 'completion.json')):
            if resolver.path(record.get('paths', {}).get(key)) != store.workspace / 'coordination/backend' / identifier / filename:
                raise ReplicaError('Restored backend path identity mismatch')
        identities = [identity, supervisor.get('child_process')]
        reaped = completion.get('reaped')
        if not isinstance(reaped, list) or any(not isinstance(i.get('identity'), dict) or i.get('pid') != i['identity'].get('pid') for i in reaped):
            raise ReplicaError('Incomplete descendant completion evidence')
        identities.extend(i['identity'] for i in reaped)
        if supervisor.get('child_launched') is True and not supervisor.get('child_process'):
            raise ReplicaError('Missing launched child identity')
        for known in identities:
            if known is None:
                continue
            if any(type(known.get(k)) is not int for k in ('pid', 'start_ticks', 'uid')) or not known.get('boot_id'):
                raise ReplicaError('Incomplete process identity')
            current = process_identity(known['pid'])
            if _same_process(known, current):
                raise ReplicaError('Recorded writer process remains present')
    slots = no_symlinks(HostSlots.default_path())
    if slots.exists():
        local = json.loads(read_regular(slots))
        reservations = local.get('reservations')
        if not isinstance(reservations, dict):
            raise ReplicaError('Unknown host reservation format')
        for identifier, record in reservations.items():
            if record.get('state') == 'released':
                continue
            if (record.get('state') != 'occupied' or identifier in active
                    or record.get('workflow') in {str(store.workspace), request['origin_workspace']}
                    or resolver.path(record.get('workflow', '/'), operational=False) == store.workspace):
                raise ReplicaError('Conflicting or unknown current host reservation; reconcile before relocation')
    return {'scope': 'current clone only; original host resources unknown', 'active_run_ids': sorted(active),
            'local_slots': 'present_unchanged' if slots.exists() else 'absent_unchanged', 'observation': fence['local_writer_observation']}


def verify_snapshot_references(store, state, request, resolver):
    inventory = state['inventory']
    count = 0
    source_references = set()
    for key in sorted(inventory.manifest['shards']):
        records = inventory._shard(key)
        count += len(records)
        for item in records.values():
            # Only known source dictionaries are typed filesystem references.
            sources = [item.get('source'), *item.get('sources', [])]
            for source in sources:
                if isinstance(source, dict):
                    for reference in (source, source.get('index')):
                        if (isinstance(reference, dict) and isinstance(reference.get('path'), str)
                                and re.fullmatch('[0-9a-f]{64}', str(reference.get('sha256', '')))):
                            source_references.add((reference['path'], reference['sha256']))
        inventory.cache.clear()
    if count != len(inventory):
        raise ReplicaError('Full inventory count mismatch')
    for key in sorted(inventory.scopes.manifest['shards']):
        inventory.scopes._shard(key)
        inventory.scopes.cache.clear()
    for receipt in state['receipts'].values():
        store.receipt(receipt)
    adopted = {**state, 'schema': 3, 'relocation': {'format': 1, 'binding_revision': 1,
        'target_workspace': str(store.workspace), 'bindings': request['bindings'], 'continuation_backend': 'native'}}
    project = effective_project(store.workspace, adopted)
    index = effective_writer_index(store.workspace, adopted)
    from .engine import _output_path
    for raw in state['writers']:
        _output_path(store, adopted, raw)
    sources = list(project.get('source_paths', []))
    if project.get('source_path'):
        sources.append(project['source_path'])
    trusted = {str(lexical(e['path'])): e['sha256'] for e in request['evidence']}
    # The restored snapshot's hash-bound manifest supplies exact source bytes.
    for evidence in request['evidence']:
        if evidence['path'].endswith('.jsonl.gz'):
            with gzip.open(evidence['path'], 'rt') as stream:
                for line in stream:
                    item = json.loads(line)
                    path = lexical(item['path'], base=resolver.path(request['origin_workspace']).parent)
                    if str(path) in sources:
                        trusted[str(path)] = item['sha256']
    for path in sources:
        if path not in trusted or hash_regular(path) != trusted[path]:
            raise ReplicaError(f'Required source lacks matching trusted snapshot hash: {path}')
    fixed_sources = []
    source_hash_cache = {}
    for raw, expected in sorted(source_references):
        try:
            path = str(resolver.path(raw))
            if path not in source_hash_cache:
                source_hash_cache[path] = hash_regular(path)
            actual = source_hash_cache[path]
            result = 'match' if actual == expected else 'unsatisfied_source_hash'
        except (OSError, ReplicaError) as exc:
            path, actual, result = None, None, 'unavailable_source'
        fixed_sources.append({'original': raw, 'effective': path, 'recorded_sha256': expected,
                              'actual_sha256': actual, 'result': result})
    artifacts = []
    for task in state['tasks'].values():
        for number, delivery in enumerate(task['deliveries']):
            for item in delivery['artifacts']:
                path = resolver.path(item['path'])
                try:
                    actual = digest(read_regular(path))
                    result = 'match' if actual == item['sha256'] else 'changed_since_delivery'
                except FileNotFoundError:
                    actual, result = None, 'missing_delivered_file'
                artifacts.append({'task_id': task['id'], 'delivery_id': delivery['id'], 'latest': number == len(task['deliveries']) - 1,
                    'original': item['path'], 'effective': str(path), 'recorded_sha256': item['sha256'], 'actual_sha256': actual, 'result': result})
    return {'inventory_objects': count, 'immutable_receipts': len(state['receipts']),
            'verified_immutable_references': 2 + len(inventory.manifest['shards']) + len(inventory.scopes.manifest['shards']) + len(state['receipts']),
            'inventory_ref': deepcopy(state['inventory_ref']), 'scope_ref': deepcopy(state['scope_ref']),
            'counts': {key: len(state[key]) for key in ('tasks', 'runs', 'classes', 'messages', 'writers', 'entries', 'mvps')},
            'effective_project': project, 'effective_claims': len(index), 'collisions': [],
            'missing_future_outputs': [path for path in index if not Path(path).exists()],
            'source_files': sources, 'fixed_sources': fixed_sources, 'artifacts': artifacts,
            'limitations': 'Historical stale/missing artifacts remain unsatisfied; no delivery, run, or Android acceptance changes.'}


def verify_replay(store, state, request, fingerprint):
    binding = state.get('relocation', {})
    if binding.get('request_id') != request['request_id'] or binding.get('fingerprint') != fingerprint:
        raise ReplicaError('Relocation receipt does not match binding integrity')
    store.verify_backup(binding['backup'], binding['source_guard']['sha256'])
    plan = store.read_blob(binding['plan_ref'], 'relocations/plans')
    report = store.read_blob(binding['report_ref'], 'relocations/reports')
    guard = {'schema': 2, 'revision': request['expected_revision'], 'sha256': request['expected_sha256']}
    if (plan != request or binding.get('source_guard') != guard
            or binding.get('backup') != f'coordination/relocations/backups/{request["expected_sha256"]}.json'
            or binding.get('source_run_ids') != report.get('fence', {}).get('active_run_ids')
            or any(binding.get(key) != request.get(key) for key in ('origin_workspace', 'target_workspace', 'bindings', 'continuation_backend'))):
        raise ReplicaError('Relocation plan/binding integrity mismatch')
    with os.fdopen(os.open(store.authority, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        os.fsync(stream.fileno())
    store._sync_parents(store.authority.parent)


def relocate(workspace, request, *, dry_run=False):
    store = Store(workspace)
    fingerprint = digest(canonical_json(request).encode())
    from .engine import _id
    identifier = _id(request.get('request_id'), 'request_id')
    # Lock ordering: dispatcher, sorted per-run launch locks, state, host slots.
    with ExitStack() as stack:
        stack.enter_context(lock_file(store.metadata_path('coordination/dispatcher.lock')))
        # Metadata mutations cannot create backend launches without dispatcher.
        for directory in sorted(store.metadata_path('coordination/backend').glob('*')):
            if directory.is_dir():
                stack.enter_context(lock_file(directory / 'launch.lock'))
        state = stack.enter_context(store.locked())
        if state is None:
            raise ReplicaError('Replica workspace is not initialized')
        if identifier in state['receipts']:
            record = state['receipts'][identifier]
            if record['fingerprint'] != fingerprint:
                raise ReplicaError('request_id already used for different content')
            verify_replay(store, state, request, fingerprint)
            response = deepcopy(store.receipt(record)['response'])
            if not dry_run:
                try:
                    store.publish(state)
                except (OSError, ReplicaError) as exc:
                    response['projection_error'] = str(exc)
            return response
        source = read_regular(store.authority)
        resolver = validate_plan(store, state, request, source)
        slots = HostSlots.default_path()
        stack.enter_context(lock_file(slots.with_suffix(slots.suffix + '.lock')))
        fence = validate_fence(store, state, request, resolver)
        report = verify_snapshot_references(store, state, request, resolver)
        report['fence'] = fence
        if dry_run:
            return {'ok': True, 'prepared': True, 'dry_run': True, 'fingerprint': fingerprint,
                    'source_revision': state['revision'], 'prospective_schema': 3, 'validation': report}
        # Long source verification cannot replace a final fresh fence/guard.
        validate_plan(store, state, request, read_regular(store.authority))
        validate_fence(store, state, request, resolver)
        backup = f'coordination/relocations/backups/{request["expected_sha256"]}.json'
        if store.metadata_path(backup).exists():
            store.verify_backup(backup, request['expected_sha256'])
        else:
            store.write_bytes(backup, source)
            store.verify_backup(backup, request['expected_sha256'])
        plan_ref = store.write_blob('relocations/plans', request)
        report_ref = store.write_blob('relocations/reports', report)
        binding = {'format': 1, 'binding_revision': 1, 'request_id': identifier, 'fingerprint': fingerprint,
            'origin_workspace': request['origin_workspace'], 'target_workspace': request['target_workspace'],
            'bindings': deepcopy(request['bindings']), 'continuation_backend': 'native',
            'source_guard': {'schema': 2, 'revision': request['expected_revision'], 'sha256': request['expected_sha256']},
            'backup': backup, 'plan_ref': plan_ref, 'report_ref': report_ref,
            'source_run_ids': fence['active_run_ids'], 'fence_scope': fence['scope']}
        state['schema'] = 3
        state['revision'] += 1
        state['relocation'] = binding
        response = {'ok': True, 'committed': True, 'revision': state['revision'], 'relocation': deepcopy(binding)}
        state['receipts'][identifier] = {'fingerprint': fingerprint, 'response': deepcopy(response)}
        try:
            store.commit(state)
        except OSError as exc:
            # rename may have succeeded before a directory fsync failed. Do
            # not call this precommit failure or overwrite visible authority.
            try:
                visible = json.loads(read_regular(store.authority))
            except (OSError, ValueError, ReplicaError):
                raise exc
            record = visible.get('receipts', {}).get(identifier, {})
            if (visible.get('schema') != 3 or visible.get('relocation') != binding
                    or record.get('fingerprint') != fingerprint):
                raise
            response['ok'] = False
            response['authority_error'] = str(exc)
            response['durability'] = 'uncertain_after_authority_rename; retry identical request to fsync and repair'
            return response
        try:
            store.publish(state)
        except (OSError, ReplicaError) as exc:
            response['projection_error'] = str(exc)
        return response
