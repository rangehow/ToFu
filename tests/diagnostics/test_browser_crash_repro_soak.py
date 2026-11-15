"""Measurement contract of the browser crash-repro instruments.

``soak.py`` turns raw CDP output into the before/after numbers a crash fix is
judged by, and ``fetch_extension.py`` decides which extension package a soak
loads. Both run unattended for hours, so their parsing is pinned here with
synthetic inputs (no browser, no network).
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import struct
import time
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f'browser_crash_repro_{name}', ROOT / 'scripts' / 'browser_crash_repro' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


soak = _load('soak')
fetch_extension = _load('fetch_extension')

ORIGIN = 'https://127.0.0.1:18443'


def test_profile_self_time_is_split_by_owner():
    profile = {
        'startTime': 0, 'endTime': 10_000,
        'nodes': [
            {'id': 1, 'callFrame': {'functionName': '(root)', 'url': ''}},
            {'id': 2, 'callFrame': {'functionName': 'render', 'url': f'{ORIGIN}/proxy/15000/static/vite/assets/application-X.js', 'lineNumber': 3, 'columnNumber': 9}},
            {'id': 3, 'callFrame': {'functionName': 'y', 'url': 'chrome-extension://bpoadfkcbjbfhfodiogcnhhhpibjhbnh/content_main.js'}},
            {'id': 4, 'callFrame': {'functionName': '(garbage collector)', 'url': ''}},
            {'id': 5, 'callFrame': {'functionName': '(program)', 'url': ''}},
            {'id': 6, 'callFrame': {'functionName': 'track', 'url': 'https://cdn.example/analytics.js'}},
        ],
        'samples': [2, 2, 3, 4, 5, 5, 5, 6],
        'timeDeltas': [1000, 1000, 2000, 500, 1000, 1000, 1000, 500],
    }

    summary = soak.summarize_profile(profile, ORIGIN)

    assert summary['spanMs'] == 10
    assert summary['byClassMs'] == {'(program)': 3, 'tofu': 2, 'ext:bpoadfkcbjbfhfodiogcnhhhpibjhbnh': 2,
                                    '(garbage collector)': 0, 'other:https://cdn.example': 0}
    assert [row['fn'] for row in summary['topFunctions']][:2] == ['render', 'y']
    assert all(row['fn'] not in ('(program)', '(idle)', '(root)') for row in summary['topFunctions'])


def _sample(t, *, heap_mb, blocking, unattributed, over1s=0, rss=400.0, gc_mb=None):
    row = {'t': t, 'metrics': {'JSHeapUsedSize': heap_mb * 2**20}, 'heap': {'embedderHeapUsedSize': 2**20},
           'dom': {'nodes': 1000, 'jsEventListeners': 50},
           'page': {'frames': {'blocking': blocking, 'unattributed': unattributed, 'max': 300,
                               'over': {'1000': over1s}}},
           'processes': [{'type': 'renderer', 'rssMB': rss}, {'type': 'extension-renderer', 'rssMB': 9999.0}]}
    if gc_mb is not None:
        row['afterGc'] = {'usedSize': gc_mb * 2**20}
    return row


def test_summary_reports_fixed_checkpoints_and_crashes(tmp_path):
    rows = [_sample(30, heap_mb=50, blocking=0, unattributed=0),
            _sample(300, heap_mb=60, blocking=600, unattributed=300, gc_mb=40),
            _sample(900, heap_mb=80, blocking=1800, unattributed=900, over1s=2, rss=500.0)]
    (tmp_path / 'samples.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    events = [{'t': 0, 'event': 'start', 'browser': 'Chrome/155.0.8059.84', 'url': 'u', 'extensions': []},
              {'t': 650, 'event': 'renderer-crash', 'status': 'crashed', 'errorCode': 5},
              {'t': 400, 'event': 'cpu-profile', 'byClassMs': {'tofu': 7, '(program)': 3}}]
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))

    summary = soak.summarize(tmp_path)

    assert [c['t'] for c in summary['checkpoints']] == [300, 900]
    first, second = summary['checkpoints']
    assert first['jsHeapAfterGcMB'] == 40 and second['jsHeapAfterGcMB'] == 40
    assert second['rendererRssMB'] == 500.0, 'extension renderers are not the page renderer'
    assert first['blockedMsPerMinSinceLast'] == 120 and second['blockedMsPerMinSinceLast'] == 120
    assert (first['crashesSoFar'], second['crashesSoFar']) == (0, 1)
    assert summary['crashes'][0]['errorCode'] == 5
    assert summary['cpuProfileSelfMsByClass'] == {'tofu': 7, '(program)': 3}
    assert json.loads((tmp_path / 'summary.json').read_text()) == summary


def _crx3(key: bytes, crx_id: bytes, files: dict) -> bytes:
    def field(number, payload):
        tag = (number << 3) | 2
        out = bytearray()
        for value in (tag, len(payload)):
            while True:
                byte = value & 0x7F
                value >>= 7
                out.append(byte | (0x80 if value else 0))
                if not value:
                    break
        return bytes(out) + payload
    header = field(2, field(1, key) + field(2, b'signature')) + field(10000, field(1, crx_id))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as package:
        for name, data in files.items():
            package.writestr(name, data)
    return b'Cr24' + struct.pack('<I', 3) + struct.pack('<I', len(header)) + header + archive.getvalue()


def test_crx3_keeps_the_store_id_and_rejects_a_mismatch():
    key = b'public-key-bytes'
    store_id = fetch_extension.extension_id(key)
    blob = _crx3(key, hashlib.sha256(key).digest()[:16], {'manifest.json': '{"name": "x"}'})

    pinned_key, archive = fetch_extension.unpack_crx3(blob, store_id)

    assert pinned_key == key and len(store_id) == 32 and set(store_id) <= set('abcdefghijklmnop')
    assert zipfile.ZipFile(io.BytesIO(archive)).read('manifest.json') == b'{"name": "x"}'
    with pytest.raises(ValueError, match='declares'):
        fetch_extension.unpack_crx3(blob, 'a' * 32)


def test_stress_driver_survives_dropped_connections():
    """A loaded server dropping a request must not end the measured stream.

    The driver used to die on the first transport error; the stress test still
    passed with the stream stopped, which made the base/candidate pair
    incomparable after 85 minutes.
    """
    import socket
    import threading

    from tests.visual.test_browser_crash_repro_stress import TurnDriver

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    served = {'dropped': 0, 'answered': 0}

    def serve():
        while served['answered'] < 1:
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                if served['dropped'] < 2:
                    served['dropped'] += 1
                    continue
                connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                   b'Content-Length: 2\r\nConnection: close\r\n\r\n{}')
                served['answered'] += 1

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    driver = TurnDriver(f'http://127.0.0.1:{port}', 'stress-driver-spec')
    driver.stop.wait = lambda timeout=None: False  # no pause between retries in the spec

    driver.run(until=time.time() + 30, max_turns=1)

    server.join(timeout=10)
    listener.close()
    assert (driver.sent, driver.transport_errors, driver.error) == (1, 2, None)
    assert driver.last_accepted_at is not None


compare = _load('compare')


def test_compare_normalizes_main_thread_time_per_dispatched_event(tmp_path):
    run = tmp_path / 'L6-base'
    run.mkdir()
    rows = [
        {'t': 0, 'metrics': {'TaskDuration': 10.0, 'ScriptDuration': 5.0}, 'probeMs': 3,
         'page': {'stream': {'events': 100}, 'frames': {'max': 10, 'over': {'1000': 0, '5000': 0}}}},
        {'t': 600, 'metrics': {'TaskDuration': 70.0, 'ScriptDuration': 35.0}, 'probeMs': 18800,
         'page': {'stream': {'events': 1300}, 'frames': {'max': 23649, 'over': {'1000': 222, '5000': 22}}},
         'afterGc': {'usedSize': 112.4 * 2**20}},
        {'t': 630, 'probeMs': 90000, 'probeError': 'TimeoutError: '},
    ]
    (run / 'samples.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    (run / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in (
        {'t': 300, 'event': 'cpu-profile', 'byClassMs': {'(idle)': 500, 'tofu': 300, '(program)': 100}},
        {'t': 620, 'event': 'renderer-crash', 'status': 'crashed', 'errorCode': 5},
    )))
    (run / 'processes.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in (
        {'t': 0, 'processes': [{'type': 'renderer', 'rssMB': 400.0}, {'type': 'extension-renderer', 'rssMB': 9e3}]},
        {'t': 300, 'processes': [{'type': 'renderer', 'rssMB': 978.0}]},
        {'t': 600, 'processes': [{'type': 'renderer', 'rssMB': 576.0}]},
    )))

    arm = compare.arm_metrics(run)

    assert arm['eventsDispatched'] == 1200
    assert (arm['taskMsPerEvent'], arm['scriptMsPerEvent']) == (50.0, 25.0)
    assert (arm['busyMsPerS'], arm['scriptMsPerS']) == (100, 50)
    assert (arm['framesOver1s'], arm['framesOver5s'], arm['maxFrameMs']) == (222, 22, 23649)
    assert arm['tofuPctOfBusyCpu'] == 75, 'idle time is not busy time'
    assert (arm['rendererRssMedianMB'], arm['rendererRssMaxMB']) == (576, 978.0), 'extension renderers excluded'
    assert (arm['worstPageResponseS'], arm['pageHangs']) == (90.0, 1)
    assert arm['crashes'] == [{'t': 620, 'errorCode': 5}]
    assert arm['postGcHeapMB'] == [[10, 112.4]]


heap_duplicates = _load('heap_duplicates')


def _synthetic_heap_snapshot() -> dict:
    """Two Turns: turn A holds one tool output twice (segments and toolRounds);
    a second text is shared across turns A and B; one large string is unique."""
    node_types = ['hidden', 'array', 'string', 'object', 'code', 'closure', 'regexp', 'number', 'native',
                  'synthetic', 'concatenated string', 'sliced string', 'symbol', 'bigint', 'object shape']
    edge_types = ['context', 'element', 'property', 'internal', 'hidden', 'shortcut', 'weak']
    text, other, shared = 'x' * 20000, 'y' * 9000, 'z' * 10000
    spec = {
        'root': ('synthetic', '(GC roots)', 0, [('property', 'store', 'store')]),
        'store': ('object', 'Object', 32, [('property', 'values', 'values')]),
        'values': ('object', 'Object', 32, [('property', 'turnA-key', 'turnA'), ('property', 'turnB-key', 'turnB')]),
        'turnA': ('object', 'Object', 48, [('property', 'turnId', 'idA'), ('property', 'projection', 'projA')]),
        'idA': ('string', 'turn-aaaa', 24, []),
        'projA': ('object', 'Object', 48, [('property', 'segments', 'segsA'), ('property', 'toolRounds', 'roundsA'),
                                           ('property', 'content', 'sharedA')]),
        'segsA': ('array', 'Array', 16, [('element', 0, 'segA')]),
        'segA': ('object', 'Object', 32, [('property', 'result', 'resultA')]),
        'resultA': ('object', 'Object', 32, [('property', 'content', 'copyA')]),
        'roundsA': ('array', 'Array', 16, [('element', 0, 'roundA')]),
        'roundA': ('object', 'Object', 32, [('property', 'toolContent', 'copyB'), ('property', 'toolArgs', 'other')]),
        'copyA': ('string', text, 20000, []),
        'copyB': ('string', text, 20000, []),
        'other': ('string', other, 9000, []),
        'sharedA': ('string', shared, 10000, []),
        'turnB': ('object', 'Object', 48, [('property', 'turnId', 'idB'), ('property', 'projection', 'projB')]),
        'idB': ('string', 'turn-bbbb', 24, []),
        'projB': ('object', 'Object', 48, [('property', 'segments', 'segsB')]),
        'segsB': ('array', 'Array', 16, [('element', 0, 'segB')]),
        'segB': ('object', 'Object', 32, [('property', 'text', 'sharedB')]),
        'sharedB': ('string', shared, 10000, []),
    }
    labels = list(spec)
    strings: list[str] = []

    def string_index(value: str) -> int:
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    node_fields = ['type', 'name', 'id', 'self_size', 'edge_count']
    nodes, edges = [], []
    for number, label in enumerate(labels):
        kind, name, size, out = spec[label]
        nodes += [node_types.index(kind), string_index(name), number * 2 + 1, size, len(out)]
        for edge_kind, edge_name, target in out:
            edges += [edge_types.index(edge_kind),
                      edge_name if edge_kind == 'element' else string_index(edge_name),
                      labels.index(target) * len(node_fields)]
    return {'snapshot': {'meta': {'node_fields': node_fields, 'node_types': [node_types],
                                  'edge_fields': ['type', 'name_or_index', 'to_node'],
                                  'edge_types': [edge_types]}},
            'nodes': nodes, 'edges': edges, 'strings': strings}


def test_heap_duplicates_names_the_fields_holding_each_copy(tmp_path):
    result = heap_duplicates.analyze(_synthetic_heap_snapshot(), min_bytes=8192)

    assert result['largeStringBytes'] == 20000 * 2 + 9000 + 10000 * 2
    assert result['duplicateExtraBytes'] == 20000 + 10000
    assert result['patterns'] == [
        {'pattern': 'same turn: segments.[].result.content | toolRounds.[].toolContent',
         'extraBytes': 20000, 'copies': 2},
        {'pattern': 'across turns: content | segments.[].text', 'extraBytes': 10000, 'copies': 2},
    ]

    path = tmp_path / 'synthetic.heapsnapshot'
    path.write_text(json.dumps(_synthetic_heap_snapshot()))
    assert heap_duplicates.main(['--snapshot', str(path), '--json']) == 0
    assert path.exists(), 'a snapshot the script did not take is never deleted'
