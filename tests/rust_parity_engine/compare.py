"""Diff the Rust engine against the Python reference on the engine_session corpus.

``python3 -m tests.rust_parity_engine.compare --rust <tofu-engine binary>``
feeds every case of ``cases.jsonl`` to the Python reference
(``tests.rust_parity_engine.reference``) and to ``<binary> parity``, then
compares their parts byte for byte. Exit 0 only when every case is
identical (unsupported or errored answers count as not green). Once the
shared harness (``tests/rust_parity``) carries the ``engine_session``
boundary, its ``run --target 'cmd:<binary> parity'`` is the same gate.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]

# Diagnostic provenance only: these annotations never suppress a diff.
KNOWN_MISMATCH_CAUSES = {
    'restart_after_first_request': {
        'outcome': 'content lane after a crash: the reference resumes in the SAME Python '
                   'process and keeps the crashed stream\'s text ("Writing. ") in front of the '
                   'resumed answer; the Rust resume restores the checkpoint content (empty here). '
                   'Whether production Python glues the text is unverified: its real crash '
                   'recovery (conversation_sync/worker_recovery) is not exercised by this harness.',
    },
    'restart_after_tool_round': {
        'outcome': 'same content-lane cause as restart_after_first_request: the crashed '
                   'request\'s streamed answer precedes the resumed answer in Python only.',
    },
}


def _answers(argv: list[str], requests: list[dict]) -> list[dict]:
    payload = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in requests)
    done = subprocess.run(argv, input=payload.encode('utf-8'), capture_output=True,
                          cwd=str(CHECKOUT), check=False)
    if done.returncode:
        detail = done.stderr.decode('utf-8', 'replace')[-4000:]
        raise RuntimeError(f'parity child exited {done.returncode}: {detail}')
    answers = [json.loads(line) for line in done.stdout.decode('utf-8').splitlines() if line.strip()]
    if [answer.get('id') for answer in answers] != [request['id'] for request in requests]:
        raise ValueError('parity answer identities/count do not match requests')
    return answers


PART_NAMES = frozenset({'outcome', 'requests', 'tools', 'fallbacks', 'events'})


def _parts(answer: dict) -> dict:
    parts = answer['parts']
    names = [part['name'] for part in parts]
    if len(names) != len(PART_NAMES) or set(names) != PART_NAMES:
        raise ValueError('engine_session must return each contracted part exactly once')
    return {p['name']: base64.b64decode(p['b64'], validate=True).decode('utf-8')
            for p in parts}


def compare_answers(case: str, reference: dict, target: dict) -> dict:
    row = {'case': case, 'identical': False}
    for side, answer in (('python', reference), ('rust', target)):
        if answer.get('id') != case:
            row[f'{side}_error'] = 'answer identity mismatch'
        for key in ('unsupported', 'error'):
            if key in answer:
                row[f'{side}_{key}'] = answer[key]
    if len(row) != 2:
        return row
    try:
        left, right = _parts(reference), _parts(target)
    except (KeyError, ValueError, TypeError, UnicodeError) as error:
        row['error'] = str(error)
        return row
    row['different'] = [name for name in sorted(PART_NAMES) if left[name] != right[name]]
    row['diffs'] = {name: {'python': left[name], 'rust': right[name]}
                    for name in row['different']}
    row['identical'] = not row['different']
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--rust', required=True, help='path to the tofu-engine binary')
    parser.add_argument('--case', action='append', default=[], help='restrict to these case names')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--write-golden', type=Path,
                        help='write the Python parts as the cargo-test golden file')
    args = parser.parse_args(argv)
    cases = [json.loads(line) for line in (HERE / 'cases.jsonl').read_text().splitlines() if line.strip()]
    if args.case:
        cases = [c for c in cases if c['name'] in args.case]
    requests = [{'id': c['name'], 'boundary': 'engine_session', 'input': c['input']} for c in cases]
    python = _answers([sys.executable, '-m', 'tests.rust_parity_engine.reference'], requests)
    rust = _answers([args.rust, 'parity'], requests)
    report, green = [], True
    for case, ref, target in zip(cases, python, rust):
        row = compare_answers(case['name'], ref, target)
        green &= row['identical']
        report.append(row)
    summary = {'cases': len(report), 'identical': sum(r['identical'] for r in report),
               'notIdentical': [r['case'] for r in report if not r['identical']]}
    if args.write_golden:
        golden = {'generatedBy': 'python3 -m tests.rust_parity_engine.compare --write-golden',
                  'knownMismatchCauses': KNOWN_MISMATCH_CAUSES,
                  'cases': {case['name']: _parts(ref) for case, ref in zip(cases, python)
                            if 'parts' in ref}}
        args.write_golden.write_text(json.dumps(golden, indent=1, ensure_ascii=False,
                                                sort_keys=True) + '\n')
    if args.report:
        args.report.write_text(json.dumps({'summary': summary, 'cases': report}, indent=1,
                                          ensure_ascii=False) + '\n')
    print(json.dumps(summary, indent=1))
    return 0 if green else 1


if __name__ == '__main__':
    sys.exit(main())
