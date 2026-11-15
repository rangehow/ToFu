#!/usr/bin/env python3
"""Responsibility: turn finished soak runs into the before/after table a frontend
change is judged by.

Entry: ``python3 scripts/browser_crash_repro/compare.py RUN_DIR [RUN_DIR ...]``
(``--json`` prints machine-readable rows). Dependencies: standard library only.

Each RUN_DIR is a ``soak.py --out`` directory. Arms of an A/B must have run
at the same time against the same stream (see README "Live A/B"); this script
only normalizes and prints them. Per-event costs divide main-thread time by
EventSource events the page dispatched, so an arm that falls behind the stream
is not rewarded for processing less.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def arm_metrics(run: Path) -> dict:
    rows = _jsonl(run / 'samples.jsonl')
    sampled = [row for row in rows if row.get('page') and row.get('metrics')]
    if len(sampled) < 2:
        raise ValueError(f'{run}: fewer than two complete samples')
    events = _jsonl(run / 'events.jsonl')
    processes = _jsonl(run / 'processes.jsonl') or rows
    first, last = sampled[0], sampled[-1]
    span = last['t'] - first['t']

    def delta(name: str) -> float:
        return last['metrics'].get(name, 0) - first['metrics'].get(name, 0)

    dispatched = ((last['page'].get('stream') or {}).get('events', 0)
                  - (first['page'].get('stream') or {}).get('events', 0))
    frames = last['page'].get('frames') or {}
    over = frames.get('over') or {}
    profile = Counter()
    for event in events:
        if event.get('event') == 'cpu-profile':
            profile.update(event.get('byClassMs') or {})
    busy = sum(ms for cls, ms in profile.items() if cls != '(idle)')
    renderer_rss = [max([p['rssMB'] for p in row.get('processes', []) if p['type'] == 'renderer'] or [0])
                    for row in processes]
    return {
        'run': run.name,
        'minutes': round(span / 60, 1),
        'eventsDispatched': dispatched,
        'framesOver1s': over.get('1000', 0),
        'framesOver5s': over.get('5000', 0),
        'maxFrameMs': round(frames.get('max', 0)),
        'busyMsPerS': round(delta('TaskDuration') / span * 1000) if span else None,
        'scriptMsPerS': round(delta('ScriptDuration') / span * 1000) if span else None,
        'taskMsPerEvent': round(delta('TaskDuration') * 1000 / dispatched, 1) if dispatched else None,
        'scriptMsPerEvent': round(delta('ScriptDuration') * 1000 / dispatched, 1) if dispatched else None,
        'tofuPctOfBusyCpu': round(100 * profile.get('tofu', 0) / busy) if busy else None,
        'postGcHeapMB': [[round(row['t'] / 60), round(row['afterGc']['usedSize'] / 2**20, 1)]
                         for row in sampled if 'afterGc' in row],
        'rendererRssMedianMB': round(statistics.median(renderer_rss)) if renderer_rss else None,
        'rendererRssMaxMB': max(renderer_rss) if renderer_rss else None,
        'worstPageResponseS': round(max(row.get('probeMs', 0) for row in rows) / 1000, 1),
        'pageHangs': sum(1 for row in rows if 'probeError' in row),
        'crashes': [{'t': e['t'], 'errorCode': e.get('errorCode')} for e in events
                    if e.get('event') == 'renderer-crash'],
    }


COLUMNS = ('minutes', 'eventsDispatched', 'framesOver1s', 'framesOver5s', 'maxFrameMs', 'busyMsPerS',
           'scriptMsPerS', 'taskMsPerEvent', 'scriptMsPerEvent', 'tofuPctOfBusyCpu', 'rendererRssMedianMB',
           'rendererRssMaxMB', 'worstPageResponseS', 'pageHangs', 'crashes', 'postGcHeapMB')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    arms = [arm_metrics(run) for run in args.runs]
    if args.json:
        print(json.dumps(arms, indent=1))
        return 0
    print('| metric | ' + ' | '.join(arm['run'] for arm in arms) + ' |')
    print('|---|' + '---|' * len(arms))
    for column in COLUMNS:
        print(f'| {column} | ' + ' | '.join(json.dumps(arm[column]) for arm in arms) + ' |')
    return 0


if __name__ == '__main__':
    sys.exit(main())
