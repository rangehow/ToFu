"""Offline continuity replay over frozen production archives and summaries.

Run: python -m benchmarks.compaction_continuity --archives ARCHIVE.json ...
     --summaries SAMPLES.json --output REPORT.json
Inputs remain private, outside version control. This measures exact projected
state retention and size through repeated real captured narratives. It makes
no model calls and cannot establish semantic quality or avoided tool rounds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from collections import Counter

from lib.tasks_pkg.compaction._continuity import (
    attach_continuity, build_continuity, encode_state, read_continuity,
    render_continuity, state_digest,
)
from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens


def replay(messages, narratives, *, cycles=100):
    if cycles < 1 or not narratives:
        raise ValueError('Replay requires positive cycles and captured narratives')
    expected = build_continuity(messages)
    baseline = encode_state(expected)
    source_tokens = _estimate_total_tokens(messages)
    maximum_projection_tokens = 0
    for generation in range(cycles):
        narrative = narratives[generation % len(narratives)]
        text, digest = attach_continuity(narrative, messages)
        messages = [make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)]
        actual = read_continuity(messages)
        if encode_state(actual) != baseline:
            raise AssertionError(f'Continuation changed at generation {generation + 1}')
        maximum_projection_tokens = max(maximum_projection_tokens, _estimate_total_tokens(messages))
    return {
        'cycles': cycles, 'stateDigest': state_digest(expected),
        'observations': len(expected['observations']),
        'observationTypes': dict(Counter(row['type'] for row in expected['observations'])),
        'exactStateRetainedEveryCycle': True,
        'stateJsonBytes': len(baseline.encode()),
        'stateEstimatedTokens': _estimate_total_tokens([
            {'role': 'user', 'content': render_continuity(expected)}]),
        'sourceEstimatedTokens': source_tokens,
        'maximumHandoffEstimatedTokens': maximum_projection_tokens,
        'modelCalls': 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives', nargs='+', type=Path, required=True)
    parser.add_argument('--summaries', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=100)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    samples_bytes = args.summaries.read_bytes()
    samples = json.loads(samples_bytes)
    narratives = [row['summary'] for row in samples
                  if isinstance(row, dict) and isinstance(row.get('summary'), str)]
    reports = []
    for path in args.archives:
        raw = path.read_bytes()
        archive = json.loads(raw)
        messages = archive.get('messages')
        if not isinstance(messages, list):
            raise ValueError('Expected inspector/archive messages array')
        reports.append({'inputSha256': hashlib.sha256(raw).hexdigest(),
                        **replay(messages, narratives, cycles=args.cycles)})
    report = {
        'kind': 'offline_captured_narrative_continuity_replay',
        'scope': 'Exact source-derived projection retention, not semantic or task-speed acceptance',
        'capturedNarratives': len(narratives),
        'narrativesSha256': hashlib.sha256(samples_bytes).hexdigest(),
        'sources': {str(path.relative_to(Path(__file__).parents[1])):
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [Path(__file__),
                        Path(__file__).parents[1] / 'lib/tasks_pkg/compaction/_continuity.py',
                        Path(__file__).parents[1] / 'lib/tasks_pkg/compaction/_evidence.py']},
        'cases': reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'cases': len(reports), 'cyclesPerCase': args.cycles,
                      'allRetained': True, 'modelCalls': 0}))


if __name__ == '__main__':
    main()
