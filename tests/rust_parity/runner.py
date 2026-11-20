"""Replay the corpus through implementations and diff their wire bytes.

The line protocol every implementation speaks (Python reference, the Rust
prototype, any later Rust owner) on stdin/stdout, one JSON object per line,
answers in request order:

  request   {"id": str, "boundary": str, "input": {...}}
  answer    {"id": str, "parts": [{"name": str, "b64": str}, ...]}
          | {"id": str, "unsupported": str}     input or boundary not ported
          | {"id": str, "error": str}           the implementation failed

A case's output is its ordered list of named byte parts. Two answers are
identical when, after the reviewed wire rules of ``normalisation.json``, they
have the same part names in the same order and every part's bytes are equal.
For boundaries whose recorded bytes are in the corpus, the reference output is
also compared with those recorded bytes (``recorded``): the Python of this
tree must still send what Python sent in production.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

from tests.rust_parity.corpus import CHECKOUT, boundary_files, read_jsonl

HERE = Path(__file__).resolve().parent
NORMALISATION = json.loads((HERE / 'normalisation.json').read_text())
CONTEXT_BYTES = 80


def _recorded_sse(case_input: dict) -> list[tuple[str, bytes]]:
    return [('frame', case_input['frame'].encode('utf-8'))]


def _recorded_snapshot(case_input: dict) -> list[tuple[str, bytes]]:
    return [('status', b'200'), ('body', case_input['body'].encode('utf-8'))]


def _recorded_push(case_input: dict) -> list[tuple[str, bytes]]:
    return [('text', case_input['text'].encode('utf-8'))]


# Boundaries whose corpus carries the bytes production actually sent.
RECORDED = {'sse_frame': _recorded_sse, 'sync_snapshot': _recorded_snapshot, 'push_frame': _recorded_push}


def _contract_vocabularies() -> dict[str, set[str]]:
    """Phase 0 contracts: the frame, event and operation names a corpus can exercise."""
    contracts = CHECKOUT / 'contracts'
    events = json.loads((contracts / 'events_v1.json').read_text())
    operations = json.loads((contracts / 'storage_operations_v1.json').read_text())
    return {
        'push_frame': {f['type'] for f in events['pushFrames']} | set(events['transportTypes']),
        'task_event': {e['type'] for e in events['events']},
        'storage_request': {o['name'] for o in operations['operations']},
    }


def _vocabulary(boundary: str, case_input: dict) -> list[tuple[str, str]]:
    """``(vocabulary, name)`` pairs one case exercises."""
    if boundary == 'push_frame':
        return [('push_frame', json.loads(case_input['text']).get('type', ''))]
    if boundary == 'storage_request':
        names = [('storage_request', case_input['operation'])]
        event = (case_input.get('payload') or {}).get('event') or {}
        return names + ([('task_event', event['type'])] if event.get('type') else [])
    if boundary == 'sse_frame':
        data = [line[len('data: '):] for line in case_input['frame'].split('\n') if line.startswith('data: ')]
        event = (json.loads(data[-1]).get('payload') or {}).get('event') or {}
        return [('task_event', event['type'])] if isinstance(event, dict) and event.get('type') else []
    return []


def contract_coverage(seen: dict[str, dict[str, int]]) -> dict:
    vocabularies = _contract_vocabularies()
    coverage = {}
    for name, counts in seen.items():
        known = vocabularies[name]
        coverage[name] = {
            'exercised': sorted(set(counts) & known),
            'outsideContract': {key: count for key, count in sorted(counts.items()) if key not in known},
            'notExercised': sorted(known - set(counts)),
        }
    return coverage


def normalise_parts(boundary: str, parts: list[tuple[str, bytes]]) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Apply the wire rules; return the parts and the ids of the rules that fired."""
    fired = []
    for rule in NORMALISATION['wireRules']:
        if rule['boundary'] != boundary:
            continue
        if rule['action'] == 'drop':
            kept = [(name, data) for name, data in parts if not name.endswith(rule['partSuffix'])]
            if len(kept) != len(parts):
                fired.append(rule['id'])
            parts = kept
        else:
            raise ValueError(f"unknown wire normalisation action {rule['action']!r}")
    return parts, fired


def first_difference(left: list[tuple[str, bytes]], right: list[tuple[str, bytes]]) -> dict | None:
    left_names, right_names = [name for name, _ in left], [name for name, _ in right]
    if left_names != right_names:
        return {'kind': 'parts', 'reference': left_names, 'target': right_names}
    for (name, a), (_, b) in zip(left, right):
        if a == b:
            continue
        offset = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        start = max(0, offset - CONTEXT_BYTES)
        return {'kind': 'bytes', 'part': name, 'offset': offset,
                'referenceBytes': len(a), 'targetBytes': len(b),
                'reference': a[start:offset + CONTEXT_BYTES].decode('utf-8', 'replace'),
                'target': b[start:offset + CONTEXT_BYTES].decode('utf-8', 'replace')}
    return None


# ── targets ────────────────────────────────────────────────────────────────

def python_command() -> list[str]:
    return [sys.executable, '-m', 'tests.rust_parity.python_impl']


def rust_core_command() -> list[str]:
    raise SystemExit(
        'Automatic local cargo builds are retired (docs/RUST_BUILDS.md). '
        'Build through scripts/remote_cargo.py --after-build and pass '
        'cmd:<fresh-bin> parity inside the same remote job.'
    )


def resolve_target(spec: str) -> tuple[str, list[str]]:
    if spec == 'python':
        return 'python', python_command()
    if spec == 'rust-core':
        return 'rust-core', rust_core_command()
    if spec.startswith('cmd:'):
        argv = shlex.split(spec[len('cmd:'):])
        return Path(argv[0]).name, argv
    raise SystemExit(f'unknown target {spec!r}: use python, rust-core or cmd:<argv>')


class Implementation:
    """One implementation process fed the corpus by its own writer thread."""

    def __init__(self, name: str, argv: list[str], boundary: str, path: Path):
        self.name = name
        env = {**os.environ, 'PYTHONPATH': str(CHECKOUT)}
        self.process = subprocess.Popen(argv, cwd=str(CHECKOUT), env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, bufsize=1 << 20)
        self.writer = threading.Thread(target=self._feed, args=(boundary, path),
                                       name=f'rust-parity-feed-{name}', daemon=True)
        self.feed_error: BaseException | None = None
        self.writer.start()

    def _feed(self, boundary: str, path: Path) -> None:
        try:
            for case in read_jsonl(path):
                line = json.dumps({'id': case['id'], 'boundary': boundary, 'input': case['input']},
                                  ensure_ascii=False, separators=(',', ':'))
                self.process.stdin.write(line.encode('utf-8') + b'\n')
            self.process.stdin.close()
        except BrokenPipeError as error:
            self.feed_error = error

    def answers(self) -> Iterator[dict]:
        for line in self.process.stdout:
            yield json.loads(line)

    def close(self) -> int:
        self.writer.join()
        return self.process.wait()


def _parts(answer: dict) -> list[tuple[str, bytes]]:
    return [(part['name'], base64.b64decode(part['b64'])) for part in answer['parts']]


def _empty_tally() -> dict:
    return {'cases': 0, 'identical': 0, 'different': 0, 'unsupported': 0, 'errors': 0,
            'unsupportedReasons': {}, 'diffs': []}


def run_boundary(boundary: str, path: Path, targets: list[tuple[str, list[str]]], *,
                 check_recorded: bool) -> dict:
    reference = Implementation('python-reference', python_command(), boundary, path)
    others = [Implementation(name, argv, boundary, path) for name, argv in targets]
    tallies = {f'{index}:{impl.name}': _empty_tally() for index, impl in enumerate(others)}
    recorded = {'cases': 0, 'identical': 0, 'different': 0, 'diffs': []} if (
        check_recorded and boundary in RECORDED) else None
    reference_errors: list[dict] = []
    rules_fired: dict[str, int] = {}
    streams = [impl.answers() for impl in others]
    reference_stream = reference.answers()
    seen: dict[str, dict[str, int]] = {}
    for case in read_jsonl(path):
        ref_answer = next(reference_stream, {'id': case['id'], 'error': 'reference exited before answering'})
        for vocabulary, name in _vocabulary(boundary, case['input']):
            counts = seen.setdefault(vocabulary, {})
            counts[name] = counts.get(name, 0) + 1
        if ref_answer.get('id') != case['id']:
            raise RuntimeError(f'reference answered {ref_answer.get("id")} for {case["id"]}')
        target_answers = [next(stream, {'id': case['id'], 'error': 'implementation exited before answering'})
                          for stream in streams]
        if 'parts' not in ref_answer:
            reference_errors.append({'id': case['id'], **{k: ref_answer[k] for k in ('error', 'unsupported')
                                                         if k in ref_answer}})
            continue
        ref_parts, fired = normalise_parts(boundary, _parts(ref_answer))
        for rule in fired:
            rules_fired[rule] = rules_fired.get(rule, 0) + 1
        if recorded is not None:
            recorded['cases'] += 1
            difference = first_difference(RECORDED[boundary](case['input']), ref_parts)
            if difference is None:
                recorded['identical'] += 1
            else:
                recorded['different'] += 1
                recorded['diffs'].append({'id': case['id'], **difference})
        for (key, tally), answer in zip(tallies.items(), target_answers):
            tally['cases'] += 1
            if answer.get('id') != case['id']:
                raise RuntimeError(f'{key} answered {answer.get("id")} for {case["id"]}')
            if 'unsupported' in answer:
                tally['unsupported'] += 1
                reason = answer['unsupported']
                tally['unsupportedReasons'][reason] = tally['unsupportedReasons'].get(reason, 0) + 1
                continue
            if 'error' in answer:
                tally['errors'] += 1
                tally['diffs'].append({'id': case['id'], 'kind': 'error', 'error': answer['error']})
                continue
            target_parts, _ = normalise_parts(boundary, _parts(answer))
            difference = first_difference(ref_parts, target_parts)
            if difference is None:
                tally['identical'] += 1
            else:
                tally['different'] += 1
                tally['diffs'].append({'id': case['id'], **difference})
    if sum(1 for _ in reference_stream):
        reference_errors.append({'error': 'reference produced extra answers'})
    for (key, tally), stream in zip(tallies.items(), streams):
        if sum(1 for _ in stream):
            tally['errors'] += 1
            tally['diffs'].append({'kind': 'protocol', 'error': 'implementation produced extra answers'})
    exits = {'python-reference': reference.close(), **{key: impl.close()
                                                       for key, impl in zip(tallies, others)}}
    return {'targets': tallies, 'recorded': recorded, 'referenceErrors': reference_errors,
            'normalisationFired': rules_fired, 'exitCodes': exits, 'contractCoverage': contract_coverage(seen)}


def run_corpus(corpus: Path, target_specs: list[str], *, boundaries: list[str],
               check_recorded: bool = True) -> dict:
    targets = [resolve_target(spec) for spec in target_specs]
    files = boundary_files(corpus)
    selected = boundaries or list(files)
    report = {'corpus': str(corpus), 'targets': [name for name, _ in targets], 'boundaries': {}}
    for boundary in selected:
        if boundary not in files:
            raise SystemExit(f'corpus has no boundary {boundary!r} (has {sorted(files)})')
        print(f'rust-parity: {boundary} ...', file=sys.stderr, flush=True)
        report['boundaries'][boundary] = run_boundary(boundary, files[boundary], targets,
                                                      check_recorded=check_recorded)
    report['summary'] = summarize(report)
    return report


def summarize(report: dict) -> dict:
    summary: dict = {'boundaries': {}, 'green': True}
    for boundary, result in report['boundaries'].items():
        row: dict = {'referenceErrors': len(result['referenceErrors'])}
        outside = {name: sum(cov['outsideContract'].values())
                   for name, cov in result['contractCoverage'].items() if cov['outsideContract']}
        if outside:
            row['outsideContract'] = outside
        if result['recorded'] is not None:
            row['recorded'] = {k: result['recorded'][k] for k in ('cases', 'identical', 'different')}
            summary['green'] &= result['recorded']['different'] == 0
        summary['green'] &= not result['referenceErrors'] and not any(result['exitCodes'].values())
        if any(result['exitCodes'].values()):
            row['exitCodes'] = result['exitCodes']
        for key, tally in result['targets'].items():
            row[key] = {k: tally[k] for k in ('cases', 'identical', 'different', 'unsupported', 'errors')}
            summary['green'] &= tally['different'] == 0 and tally['errors'] == 0 and tally['unsupported'] == 0
        summary['boundaries'][boundary] = row
    return summary
