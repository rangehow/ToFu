"""Rollback drill probes, run in child interpreters against one checkout under test.

Ported from GATE-PUBLISH-CARRY's rollback_serve.py, rollback_crash.py and
rollback_killstop.py (confirmed by that gate 2026-11-12). Standard library
only: the storage client is imported from ``--checkout`` (the tree whose
engine is certified), never from the certifying tree, so a historical commit
is drilled with its own client.

  serve     serve one data root, digest every seeded conversation, read the
            2099 probe rows, write once, stop cleanly (stdin EOF)
  crash     commit, wait for the online GC's first location table, commit
            twice during the off-mutex packed-filter build, SIGKILL before it
            finishes
  killstop  acknowledge 30 writes, close stdin (ordered shutdown), SIGKILL at
            ``step=authority_checkpoint state=start``

Each writes one JSON result; engine stderr goes beside it as JSONL.
Usage: probes.py {serve,crash,killstop} --checkout DIR --engine BIN --root DIR --out FILE [--seed FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

KILLSTOP_COMMITS = 30
KILLSTOP_STEP = 'authority_checkpoint'
CRASH_COMMITS = ('before-first-table', 'during-build-1', 'during-build-2')


def _engine(engine: str, root: Path, token: str, durability: str, stderr) -> subprocess.Popen:
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'TOFU_DB_WRITE_OVERLAY': '1',
                   'TOFU_STORAGE_TOKEN': token, 'TOFU_DB_DURABILITY': durability}
    return subprocess.Popen([engine, 'serve', '--data-dir', str(root / 'tofudb-authority'),
                             '--owner-id', '1', '--tenant-id', '1'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=environment)


def _ready_port(child: subprocess.Popen) -> tuple[int | None, str]:
    """The engine's first stdout line: its ready record, or the refusal it printed instead."""
    line = child.stdout.readline()
    try:
        ready = json.loads(line) if line else None
    except ValueError:
        ready = None
    if isinstance(ready, dict) and isinstance(ready.get('port'), int):
        return ready['port'], ''
    return None, line.decode('utf-8', 'replace').strip() or 'daemon exited before readiness'


def _refused(child: subprocess.Popen, result: dict, error: str) -> dict:
    result['serve_error'] = error
    child.stdin.close()
    result['exit'] = child.wait()
    return result


def _client(port: int, token: str, timeout: float):
    from lib.storage.v2_client import StorageV2Client
    return StorageV2Client('127.0.0.1', port, token.encode('ascii'), owner_id=1, tenant_id=1, timeout=timeout)


def serve(args) -> dict:
    root = Path(args.root)
    seed = json.loads(Path(args.seed).read_text())
    ids = sorted(cid for kind in seed['conversations'].values() for cid in kind)
    token = secrets.token_hex(32)
    result = {'engine': args.engine, 'root': str(root)}
    started = time.time()
    with open(Path(args.out).with_suffix('.stderr.jsonl'), 'ab') as stderr:
        child = _engine(args.engine, root, token, 'normal', stderr)
        port, error = _ready_port(child)
        if port is None:
            return _refused(child, result, error)
        result['ready_s'] = round(time.time() - started, 1)
        client = _client(port, token, 3 * 3600)
        try:
            result['conversation_count'] = client.query('conversation.count', {'user_id': 1})
            digest, messages, refusals = hashlib.sha256(), 0, []
            for cid in ids:
                try:
                    value = client.query('conversation.get', {'conv_id': cid, 'user_id': 1})
                except Exception as exc:  # noqa: BLE001 - a refusal is evidence, counted below
                    value = {'refused': getattr(exc, 'code', '') or type(exc).__name__}
                    refusals.append([cid, value['refused']])
                if isinstance(value, dict):
                    turns = value.get('messages') or value.get('turns') or []
                    messages += len(turns) if isinstance(turns, list) else 0
                digest.update(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
            result.update(conversations=len(ids), messages=messages, sha256=digest.hexdigest(),
                          refusals=refusals[:20], refusal_count=len(refusals))
            # Rows the crash probe acknowledged (2099-12-01..), then the kill-during-shutdown probe's.
            result['december_2099'] = [row.get('date') for row in
                                       client.query('daily_cost.month', {'user_id': 1, 'year': 2099, 'month': 12}) or []]
            result['november_2099_rows'] = len(
                client.query('daily_cost.month', {'user_id': 1, 'year': 2099, 'month': 11}) or [])
            result['write'] = client.command('daily_cost.upsert', {
                'user_id': 1, 'date': '2099-12-28', 'cost': 1.0, 'conversations': {'rollback': 1},
                'computed_at': 1}, f'rollback-{token[:8]}')
        finally:
            client.close()
            child.stdin.close()
            result['exit'] = child.wait()
    result['wall_s'] = round(time.time() - started, 1)
    return result


def _drain(stream, path: Path, watch) -> None:
    with open(path, 'w', encoding='utf-8') as sink:
        for raw in stream:
            text = raw.decode('utf-8', 'replace')
            sink.write(text)
            watch(text)


def crash(args) -> dict:
    root = Path(args.root)
    token = secrets.token_hex(32)
    result = {'engine': args.engine, 'root': str(root), 'commits': []}
    first_table, built = threading.Event(), threading.Event()

    def watch(text):
        if '"target":"catalog_locations"' in text and 'source=first' in text:
            first_table.set()
        if '"target":"packed_filter"' in text and 'step=built' in text:
            built.set()
    child = _engine(args.engine, root, token, 'strict', subprocess.PIPE)
    threading.Thread(target=_drain, args=(child.stderr, Path(args.out).with_suffix('.stderr.jsonl'), watch),
                     daemon=True).start()
    started = time.time()
    port, error = _ready_port(child)
    if port is None:
        return _refused(child, result, error)
    client = _client(port, token, 600)

    def commit(label):
        receipt = client.command('daily_cost.upsert', {
            'user_id': 1, 'date': f'2099-12-{len(result["commits"]) + 1:02d}',
            'cost': float(len(result['commits']) + 1), 'conversations': {label: 1},
            'computed_at': int(time.time())}, f'crash-{label}-{token[:8]}')
        result['commits'].append({'label': label, 'at_s': round(time.time() - started, 1), 'receipt': receipt})
    commit(CRASH_COMMITS[0])
    if not first_table.wait(600):
        result['first_table_never_published'] = True
    else:
        result['first_table_s'] = round(time.time() - started, 1)
        time.sleep(5)  # Inside the packed-filter build that follows the first table.
        for label in CRASH_COMMITS[1:]:
            commit(label)
    result['built_before_kill'] = built.is_set()
    os.kill(child.pid, signal.SIGKILL)
    result['killed_at_s'] = round(time.time() - started, 1)
    result['exit'] = child.wait()
    client.close()
    return result


def killstop(args) -> dict:
    root = Path(args.root)
    token = secrets.token_hex(32)
    result = {'engine': args.engine, 'root': str(root), 'kill_step': KILLSTOP_STEP, 'commits': []}
    steps, reached = [], threading.Event()

    def watch(text):
        if '"target":"shutdown_timing"' in text and '"message":"' in text:
            message = text.split('"message":"', 1)[1].split('"', 1)[0]
            steps.append(message)
            if message.startswith(f'step={KILLSTOP_STEP} state=start'):
                reached.set()
    child = _engine(args.engine, root, token, 'normal', subprocess.PIPE)
    threading.Thread(target=_drain, args=(child.stderr, Path(args.out).with_suffix('.stderr.jsonl'), watch),
                     daemon=True).start()
    started = time.time()
    port, error = _ready_port(child)
    if port is None:
        return _refused(child, result, error)
    client = _client(port, token, 600)
    for index in range(KILLSTOP_COMMITS):
        receipt = client.command('daily_cost.upsert', {
            'user_id': 1, 'date': f'2099-11-{index + 1:02d}', 'cost': float(index + 1),
            'conversations': {'killstop': index + 1}, 'computed_at': int(time.time())},
            f'killstop-{index}-{token[:8]}')
        result['commits'].append({'date': f'2099-11-{index + 1:02d}', 'receipt': receipt})
    client.close()
    child.stdin.close()  # The parent lease ends: the engine starts its ordered shutdown.
    if reached.wait(3600):
        os.kill(child.pid, signal.SIGKILL)
        result['killed_at_s'] = round(time.time() - started, 1)
    else:
        result['kill_step_never_reached'] = True
    result['exit'] = child.wait()
    time.sleep(0.2)  # Let the drain thread record the last shutdown step.
    result['shutdown_steps_seen'] = steps
    result['last_step'] = steps[-1] if steps else None
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('probe', choices=('serve', 'crash', 'killstop'))
    parser.add_argument('--checkout', required=True, help='tree whose storage client is used')
    parser.add_argument('--engine', required=True)
    parser.add_argument('--root', required=True, help='private data root holding tofudb-authority/')
    parser.add_argument('--out', required=True)
    parser.add_argument('--seed', help='seed.json of the load run (serve only)')
    args = parser.parse_args(argv)
    checkout = Path(args.checkout).resolve()
    sys.path.insert(0, str(checkout))
    import lib.storage.v2_client as client_module
    if not Path(client_module.__file__).resolve().is_relative_to(checkout):
        raise SystemExit(f'the storage client did not load from {checkout}: {client_module.__file__}')
    result = {'serve': serve, 'crash': crash, 'killstop': killstop}[args.probe](args)
    Path(args.out).write_text(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
