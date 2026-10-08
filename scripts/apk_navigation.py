"""Read-only native reverse navigation; structural edges are not executions."""
import collections
import sqlite3


def rows(cursor):
    return [dict(row) for row in cursor]


def has_table(db, name):
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (name,)).fetchone())


def native_node(db, artifact, address, limit):
    functions = rows(db.execute('SELECT * FROM native_functions WHERE artifact=? AND start<=? AND end>?', (artifact, address, address)))
    starts = sorted({f['start'] for f in functions}) or [address]
    incoming = rows(db.execute('SELECT * FROM native_sites WHERE artifact=? AND target IN (' + ','.join('?' for _ in starts) + ') LIMIT ?', (artifact, *starts, limit + 1)))
    outgoing = []
    for start in starts:
        outgoing.extend(rows(db.execute('SELECT * FROM native_sites WHERE artifact=? AND func=? LIMIT ?', (artifact, start, limit + 1))))
    bindings = rows(db.execute('SELECT * FROM jni_bindings WHERE artifact=? AND address IN (' + ','.join('?' for _ in starts) + ')', (artifact, *starts)))
    reviewed = rows(db.execute('SELECT * FROM reviewed_edges WHERE artifact=? AND (dst IN (' + ','.join('?' for _ in starts) + ') OR src IN (' + ','.join('?' for _ in starts) + ') OR pc=?)', (artifact, *starts, *starts, address)))
    existing = []
    if has_table(db, 'native_reviews'):
        existing = rows(db.execute('SELECT r.id,r.source,r.source_sha256,r.original_id,r.summary,r.trigger,r.flow,r.state,n.start,n.end,n.status AS locator_status FROM native_reviews n JOIN review_records r ON r.id=n.review WHERE n.artifact=? AND n.start<=? AND n.end>?', (artifact, address, address)))
    interfaces = rows(db.execute('SELECT * FROM native_interface_sites WHERE artifact=? AND pc>=? AND pc<?', (artifact, min(starts), max((f['end'] for f in functions), default=address+4)))) if has_table(db, 'native_interface_sites') else []
    return dict(address=address, functions=functions, incoming=incoming[:limit], incoming_truncated=len(incoming)>limit,
                outgoing=outgoing[:limit], outgoing_truncated=len(outgoing)>limit, bindings=bindings,
                reviewed_edges=reviewed, existing_reviews=existing, known_interface_sites=interfaces)


def reverse_native(db, artifact, address, depth, limit):
    """Bounded BFS, preserves separate direct/tail/async evidence and cycles.

    Only bl and b to a recorded function entry form direct upstream edges.
    A reviewed async edge is retained with its condition/provenance. Other
    branches/indirect sites are displayed in a node but never guessed as calls.
    """
    if not 0 <= depth <= 8 or limit < 1:
        raise ValueError('depth must be 0..8 and limit positive')
    todo = collections.deque([(address, 0)]); seen = set(); nodes = []; edges = []
    truncated = False
    while todo:
        target, distance = todo.popleft()
        if target in seen:
            continue
        if len(seen) >= limit:
            truncated = True; break
        seen.add(target); node = native_node(db, artifact, target, limit); node['distance'] = distance; nodes.append(node)
        if distance == depth:
            continue
        starts = {f['start'] for f in node['functions']} or {target}
        for site in node['incoming']:
            if site['mnemonic'] not in ('bl', 'b') or site['func'] is None or site['func'] in starts:
                continue
            edge = dict(kind='direct_call' if site['mnemonic']=='bl' else 'tail_transfer_candidate', src=site['func'], dst=site['target'], pc=site['pc'],
                        status='decoded_static_reference', raw_hex=site['raw_hex'], file_offset=site['file_offset'], condition='instruction-site reachability and runtime invocation are separate')
            edges.append(edge); todo.append((site['func'], distance+1))
        for edge in node['reviewed_edges']:
            if edge['dst'] in starts and edge['src'] is not None:
                edges.append(edge); todo.append((edge['src'], distance+1))
        truncated |= node['incoming_truncated']
    return dict(nodes=nodes, edges=edges, nodes_truncated=truncated,
                limits=['References and reviewed async boundaries do not prove an executed path.', 'FDE/symbol function bounds may be incomplete; indirect targets are not synthesized.'])
