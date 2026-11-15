#!/usr/bin/env python3
"""Responsibility: measure duplicated large strings retained by conversation Turns in
a soak tab's JS heap, by the projection fields that hold each copy.

Entry: ``python3 scripts/browser_crash_repro/heap_duplicates.py --profile-dir DIR``
(a ``soak.py`` run's ``soak-profile-*`` directory; the soak browser must be
running) or ``--snapshot FILE.heapsnapshot``. Dependencies: numpy, websockets.

Two copies of one tool output in the same TurnRecord (for example
``segments[].result.content`` and ``toolRounds[].toolContent``) cost memory twice
for every live-updated Turn. The report names field patterns and byte totals only;
it never prints string content or raw ids. A snapshot taken here is deleted after
the analysis unless ``--keep`` is given: it contains the page's data.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

STRING_KINDS = ('string', 'concatenated string', 'sliced string')
RETAINER_DEPTH = 16


def analyze(snapshot: dict, min_bytes: int = 8192) -> dict:
    meta = snapshot['snapshot']['meta']
    node_fields, edge_fields = meta['node_fields'], meta['edge_fields']
    node_types, edge_types = meta['node_types'][0], meta['edge_types'][0]
    nf, ef = len(node_fields), len(edge_fields)
    nodes = np.asarray(snapshot['nodes'], dtype=np.int64).reshape(-1, nf)
    edges = np.asarray(snapshot['edges'], dtype=np.int64).reshape(-1, ef)
    strings = snapshot['strings']
    type_i, name_i, size_i, count_i = (node_fields.index(k) for k in ('type', 'name', 'self_size', 'edge_count'))
    etype_i, ename_i, eto_i = (edge_fields.index(k) for k in ('type', 'name_or_index', 'to_node'))
    count = len(nodes)
    first_edge = np.zeros(count + 1, dtype=np.int64)
    first_edge[1:] = np.cumsum(nodes[:, count_i])
    from_node = np.repeat(np.arange(count), nodes[:, count_i])
    to_node = edges[:, eto_i] // nf
    etype = edges[:, etype_i]
    strong = etype != edge_types.index('weak')
    order = np.argsort(to_node[strong], kind='stable')
    rev_from = from_node[strong][order]
    rev_edge = np.nonzero(strong)[0][order]
    rev_start = np.searchsorted(to_node[strong][order], np.arange(count + 1))
    string_types = [node_types.index(k) for k in STRING_KINDS if k in node_types]

    def edge_label(edge: int) -> str:
        kind = edge_types[etype[edge]]
        if kind in ('element', 'hidden'):
            return '[]'
        name = str(strings[edges[edge, ename_i]])
        return '[]' if name.isdigit() else name

    def turn_identity(turn: int) -> str:
        for edge in range(first_edge[turn], first_edge[turn + 1]):
            if strings[edges[edge, ename_i]] == 'turnId':
                return hashlib.sha1(strings[nodes[to_node[edge], name_i]].encode()).hexdigest()[:12]
        return f'node{turn}'

    def locate(node: int) -> tuple[str, str]:
        """(turn identity, field path inside its projection), following one retainer chain."""
        fields, current, seen = [], node, {node}
        for _ in range(RETAINER_DEPTH):
            # Prefer the object's own named property/element edge over V8 backing
            # stores (PropertyArray/FixedArray slots), which carry no field names.
            parent = via = None
            best_rank = 3
            for k in range(rev_start[current], rev_start[current + 1]):
                candidate = rev_from[k]
                if candidate in seen or nodes[candidate, type_i] in string_types:
                    continue
                structured = node_types[nodes[candidate, type_i]] in ('object', 'array')
                named = edge_types[etype[rev_edge[k]]] in ('property', 'element')
                rank = 0 if structured and named else 1 if structured else 2
                if rank < best_rank:
                    parent, via, best_rank = candidate, rev_edge[k], rank
                if rank == 0:
                    break
            if parent is None:
                break
            label = edge_label(via)
            if label == 'projection':
                return turn_identity(parent), '.'.join(reversed(fields))
            if label not in ('elements', 'properties'):
                fields.append(label)
            seen.add(parent)
            current = parent
        return '', '(not under a Turn projection)'

    groups: dict[str, list[int]] = collections.defaultdict(list)
    for node in np.nonzero(np.isin(nodes[:, type_i], string_types) & (nodes[:, size_i] >= min_bytes))[0]:
        text = strings[nodes[node, name_i]]
        key = hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest() if text else f'n{node}'
        groups[key].append(int(node))

    extra_by_pattern: collections.Counter = collections.Counter()
    copies_by_pattern: collections.Counter = collections.Counter()
    total = duplicate = 0
    for members in groups.values():
        size = int(nodes[members[0], size_i])
        total += size * len(members)
        if len(members) < 2:
            continue
        duplicate += size * (len(members) - 1)
        located = [locate(node) for node in members]
        turns = {turn for turn, _ in located}
        scope = 'same turn' if len(turns) == 1 and '' not in turns else 'across turns'
        pattern = f"{scope}: {' | '.join(sorted({field for _, field in located}))}"
        extra_by_pattern[pattern] += size * (len(members) - 1)
        copies_by_pattern[pattern] += len(members)
    return {
        'minBytes': min_bytes,
        'largeStringBytes': total,
        'duplicateExtraBytes': duplicate,
        'patterns': [{'pattern': pattern, 'extraBytes': extra, 'copies': copies_by_pattern[pattern]}
                     for pattern, extra in extra_by_pattern.most_common()],
    }


async def take_snapshot(profile_dir: Path, out: Path) -> None:
    import websockets
    port, path = (profile_dir / 'DevToolsActivePort').read_text().split()[:2]
    async with websockets.connect(f'ws://127.0.0.1:{port}{path}', max_size=None) as ws:
        counter = 0
        with out.open('w') as handle:
            async def call(method, params=None, session=None):
                nonlocal counter
                counter += 1
                mine = counter
                message = {'id': mine, 'method': method, 'params': params or {}}
                if session:
                    message['sessionId'] = session
                await ws.send(json.dumps(message))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get('method') == 'HeapProfiler.addHeapSnapshotChunk':
                        handle.write(reply['params']['chunk'])
                    elif reply.get('id') == mine:
                        return reply

            targets = (await call('Target.getTargets'))['result']['targetInfos']
            page = next(t for t in targets if t['type'] == 'page' and t['url'].startswith(('http://', 'https://')))
            session = (await call('Target.attachToTarget', {'targetId': page['targetId'], 'flatten': True}))['result']['sessionId']
            await call('HeapProfiler.enable', {}, session)
            await call('HeapProfiler.collectGarbage', {}, session)
            await call('HeapProfiler.takeHeapSnapshot', {'reportProgress': False, 'captureNumericValue': False}, session)
            await call('Target.detachFromTarget', {'sessionId': session})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--profile-dir', type=Path)
    source.add_argument('--snapshot', type=Path)
    parser.add_argument('--min-bytes', type=int, default=8192, help='smallest string counted (measurement scope)')
    parser.add_argument('--keep', action='store_true', help='keep a snapshot this script took')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    taken = None
    if args.profile_dir:
        directory = Path(glob.glob(str(args.profile_dir))[0])
        taken = directory.parent / f'heap-{os.getpid()}.heapsnapshot'
        asyncio.run(take_snapshot(directory, taken))
    path = taken or args.snapshot
    try:
        result = analyze(json.loads(path.read_text()), args.min_bytes)
    finally:
        if taken and not args.keep:
            taken.unlink()
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    print(f"large strings (>= {result['minBytes']} B): {result['largeStringBytes'] / 2**20:.1f} MB, "
          f"duplicate copies: {result['duplicateExtraBytes'] / 2**20:.1f} MB")
    for row in result['patterns'][:20]:
        print(f"  {row['extraBytes'] / 2**20:7.2f} MB extra  ({row['copies']} copies)  {row['pattern'][:200]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
