#!/usr/bin/env python3
"""Measure the parity gate each row of the Rust rewrite phase plan declares.

``docs/RUST_REWRITE_EVIDENCE.md`` "Phased plan": every phase ships to the
owner's running install, and a phase is done when its parity gate passes
against the Rust implementation. The gate column of that table names existing
tests; tests that exercise HTTP, SSE, storage wire or process behaviour run
black-box against a server URL, and white-box Python tests for a moved owner
are rewritten as Rust tests from the same cases.

This script turns the plan's declared test paths into a measured report:

* ``PHASE_GATES`` is the authored phase 1-7 mapping. Each row's declared
  ``tests/...`` paths (plus the ``packages/tofu-search/tests`` suite, which the
  plan's tofu-search owner names) are copied verbatim from the plan, so a row
  that names a template (``tests/<owner>/``) or a path that does not exist
  keeps it and it is reported, never dropped. Contract artefacts named in the
  same column (``contracts/restart_preparation_v1.json``, the ``native_agent_*``
  schemas, frontend vitest) are not test files and carry no classification, so
  they are outside this table.
* Files are selected from ``scripts/gen_test_classification.py``'s
  ``build_manifest()``: every classified test file at, or below, a declared
  path. That generator classifies the ``tests/`` tree; a declared path outside
  it (``packages/tofu-search/tests``) is classified here with the generator's
  own rules and a fresh HTTP-fixture scan, so the owner's suite is measured
  instead of silently contributing zero files. A phase's black-box files are
  what can run against a server URL today; its white-box files are what still
  needs porting (or Rust test cases).

The plan's "**0. Contracts first**" row precedes the rewrite; its gate is the
set of generator ``--check`` runs in ``make contracts-check``, so this gate
measures the seven rewrite phases (1-7).

Usage: ``python3 scripts/rust_phase_gate.py`` prints the human tables (``--list``
is the same); ``--json`` prints the machine-readable report; ``--phase N``
(repeatable) limits the report; ``--check`` exits 1 and prints why when a
declared gate path is absent or a phase has no black-box file. There are no
environment variables, thresholds or tuning knobs: every constant is the
plan's own text. Standard library only; deterministic for a given work tree.
"""
import argparse
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DOC = 'docs/RUST_REWRITE_EVIDENCE.md'
PHASE_SECTION = '### Phased plan'
CONTRACT = 'tofu.rust-phase-gate/v1'

_spec = importlib.util.spec_from_file_location(
    '_tofu_test_classification', Path(__file__).with_name('gen_test_classification.py'))
_classification = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_classification)
build_manifest = _classification.build_manifest
GenerationError = _classification.GenerationError
SURFACES = _classification.SURFACES


@dataclass(frozen=True)
class PhaseGate:
    """One plan row: its phase number, title, and declared test paths, verbatim."""

    phase: int
    title: str
    declared: tuple[str, ...]


PHASE_GATES = (
    PhaseGate(1, 'Rust front door', (
        'tests/sse', 'tests/websocket', 'tests/static_serving', 'tests/auth',
        'tests/identity', 'tests/e2e', 'tests/visual')),
    PhaseGate(2, 'Manager and lifecycle', (
        'tests/server', 'tests/lifecycle_approval', 'tests/chaos/test_chaos_nodes.py')),
    PhaseGate(3, 'Storage clients and SQL authority', (
        'tests/storage', 'tests/storage_authority_sql', 'tests/tofudb', 'tests/json_store')),
    PhaseGate(4, 'Execution engine', (
        'tests/tasks_pkg', 'tests/turn_lifecycle', 'tests/compaction', 'tests/llm',
        'tests/llm_dispatch', 'tests/swarm', 'tests/chaos/test_chaos_faults.py')),
    # Concrete test directories for the row's owners, in its dependency order:
    # conversations and sync; project_mod (with run_command); model routing and
    # registration; MCP (rmcp); channels; router, orchestration and swarm;
    # scheduler and automations; memory, knowledge and research; paper and
    # slides orchestration; browser and desktop server side; tofu-search.
    PhaseGate(5, 'Domains, one owner at a time', (
        'tests/conversation', 'tests/conversation_sync',
        'tests/project_mod',
        'tests/model_routing',
        'tests/mcp_protocol',
        'tests/channels', 'tests/feishu',
        'tests/router', 'tests/orchestration', 'tests/swarm',
        'tests/scheduler', 'tests/automations',
        'tests/memory', 'tests/knowledge', 'tests/research',
        'tests/paper', 'tests/slides',
        'tests/browser', 'tests/desktop',
        'tests/search', 'packages/tofu-search/tests')),
    PhaseGate(6, 'Node and distribution', (
        'tests/desktop', 'tests/desktop_dist', 'tests/installer', 'tests/mobile')),
    PhaseGate(7, 'Endpoint', (
        'tests/slides', 'tests/pdf_parser', 'tests/doc_parser')),
)


def _is_template(path: str) -> bool:
    return '<' in path or '>' in path


def _under(path: str, declared: str) -> bool:
    base = declared.rstrip('/')
    return path == base or path.startswith(base + '/')


def _extra_files(root, declared, classified_paths) -> list[dict]:
    """Classified entries for declared test paths outside the generator's scope.

    ``build_manifest()`` scans ``tests/`` only; a declared owner suite elsewhere
    (``packages/tofu-search/tests``) would otherwise measure zero files while
    the path exists. Reuse the generator's classification rules so both scopes
    stay one definition.
    """
    extras = []
    seen = set()
    for path in declared:
        base = path.rstrip('/')
        if any(_under(known, base) for known in classified_paths):
            continue
        if not (root / base).exists():
            continue
        tracked = _classification._git_ls_files(root, base)
        conftests = [item for item in tracked if item.rsplit('/', 1)[-1] == 'conftest.py']
        by_directory = _classification.conftest_http_fixtures(root, conftests)
        for item in tracked:
            if item in classified_paths or item in seen:
                continue
            if _classification.is_python_test(item):
                entry = _classification.classify_python(
                    item, (root / item).read_bytes(),
                    _classification._http_fixtures_for(item, by_directory))
                language = 'python'
            elif _classification.is_js_test(item):
                count = len(_classification.JS_TEST.findall(
                    (root / item).read_text('utf-8', 'replace')))
                entry = {'testCount': count, 'regexTestCount': count,
                         'kind': 'white-box', 'surfaces': [], 'signals': []}
                language = 'js'
            else:
                continue
            seen.add(item)
            extras.append({'path': item, 'language': language,
                           'owner': '/'.join(item.split('/')[:2]), **entry})
    return extras


def select_files(entries, declared) -> list[dict]:
    """Manifest rows at, or below, any declared gate path (a file or a directory)."""
    return [entry for entry in entries if any(_under(entry['path'], path) for path in declared)]


def _totals(entries) -> dict:
    return {'files': len(entries), 'tests': sum(entry['testCount'] for entry in entries)}


def _kind_totals(entries, kind: str) -> dict:
    return _totals([entry for entry in entries if entry['kind'] == kind])


def _surface_totals(entries, surface: str) -> dict:
    return _totals([entry for entry in entries if surface in entry['surfaces']])


def check_failures(rows) -> list[str]:
    """Why a phase gate cannot be measured today, in phase order."""
    failures = []
    for row in rows:
        for item in row['declared']:
            if item['exists']:
                continue
            if item['template']:
                failures.append(
                    f"phase {row['phase']}: declared gate path {item['path']!r} is a template, not "
                    "a concrete directory; the plan names each owner's subset, so the gate cannot "
                    'be measured here')
            else:
                failures.append(f"phase {row['phase']}: declared gate path {item['path']!r} is absent")
        if row['blackBox']['files'] == 0:
            failures.append(
                f"phase {row['phase']}: no black-box test file under the declared gate paths, "
                'so nothing can run against a server URL today')
    return failures


def build_report(manifest, root=ROOT, phases=PHASE_GATES) -> dict:
    classified = {entry['path'] for entry in manifest['files']}
    extras = _extra_files(root, [path for gate in phases for path in gate.declared],
                          classified)
    entries = [*manifest['files'], *extras]
    rows = []
    for gate in phases:
        declared = []
        selected = []
        seen = set()
        for path in gate.declared:
            matched = select_files(entries, (path,))
            declared.append({'path': path, 'exists': (root / path).exists(),
                             'template': _is_template(path), 'files': len(matched)})
            for entry in matched:
                if entry['path'] not in seen:
                    seen.add(entry['path'])
                    selected.append(entry)
        rows.append({
            'phase': gate.phase,
            'title': gate.title,
            'declared': declared,
            'missing': [item['path'] for item in declared if not item['exists']],
            'files': len(selected),
            'tests': sum(entry['testCount'] for entry in selected),
            'blackBox': _kind_totals(selected, 'black-box'),
            'whiteBox': _kind_totals(selected, 'white-box'),
            'bySurface': {surface: _surface_totals(selected, surface) for surface in SURFACES},
            'blackBoxFiles': [entry['path'] for entry in selected if entry['kind'] == 'black-box'],
        })
    failures = check_failures(rows)
    return {
        'contract': CONTRACT,
        'authority': {'doc': EVIDENCE_DOC, 'section': PHASE_SECTION},
        'classification': {
            'files': len(manifest['files']),
            'tests': sum(entry['testCount'] for entry in manifest['files']),
            'blackBox': _kind_totals(manifest['files'], 'black-box'),
            'whiteBox': _kind_totals(manifest['files'], 'white-box'),
            'outsideManifestScope': {
                'files': len(extras),
                'tests': sum(entry['testCount'] for entry in extras),
            },
        },
        'phases': rows,
        'check': {'ok': not failures, 'failures': failures},
    }


def _counts(totals: dict) -> str:
    return f"{totals['files']}f/{totals['tests']}t"


def render(report: dict) -> str:
    classification = report['classification']
    lines = [
        'Rust rewrite phase gate',
        (f"Authority: {report['authority']['doc']} {report['authority']['section']!r} — a phase is "
         'done when its parity gate passes against the Rust implementation'),
        (f'Classification (scripts/gen_test_classification.py): {classification["files"]} test files / '
         f'{classification["tests"]} tests; black-box {_counts(classification["blackBox"])}, '
         f'white-box {_counts(classification["whiteBox"])}'),
        '',
        'Phase coverage',
        f"{'Phase':<6}{'Plan row':<34}{'Files':>6}{'Tests':>7}{'Black-box':>16}{'White-box':>16}",
    ]
    for row in report['phases']:
        lines.append(f"{row['phase']:<6}{row['title']:<34}{row['files']:>6}{row['tests']:>7}"
                     f"{_counts(row['blackBox']):>16}{_counts(row['whiteBox']):>16}")
    lines += ['', 'Per-surface counts (files/tests; a file can carry several surfaces)',
              f"{'Phase':<6}" + ''.join(f'{surface:>16}' for surface in SURFACES)]
    for row in report['phases']:
        lines.append(f"{row['phase']:<6}" +
                     ''.join(f"{_counts(row['bySurface'][surface]):>16}" for surface in SURFACES))
    lines += ['', 'Declared gate paths', f"{'Phase':<6}{'Path':<36}{'Files':>6}  Status"]
    for row in report['phases']:
        for item in row['declared']:
            if item['exists']:
                status = 'present'
            elif item['template']:
                status = 'MISSING (template, not a concrete directory)'
            else:
                status = 'MISSING'
            lines.append(f"{row['phase']:<6}{item['path']:<36}{item['files']:>6}  {status}")
    missing = [(row['phase'], path) for row in report['phases'] for path in row['missing']]
    lines += ['', f'Missing declared paths: {len(missing)}']
    lines += [f'  phase {phase}: {path}' for phase, path in missing]
    return '\n'.join(lines)


def render_check(report: dict) -> str:
    failures = report['check']['failures']
    if not failures:
        return 'Check: PASS — every declared gate path exists and every phase has a black-box test file'
    return '\n'.join([f'Check: FAIL — {len(failures)} finding(s)'] +
                     [f'  {failure}' for failure in failures])


def selected_phases(requested) -> tuple:
    if not requested:
        return PHASE_GATES
    wanted = set(requested)
    return tuple(gate for gate in PHASE_GATES if gate.phase in wanted)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Measure the Rust rewrite phase gates declared by '
                    f'{EVIDENCE_DOC} "Phased plan".')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--list', action='store_true', help='human-readable tables (the default)')
    output.add_argument('--json', action='store_true', help='machine-readable report')
    parser.add_argument('--phase', type=int, action='append', metavar='N',
                        choices=tuple(gate.phase for gate in PHASE_GATES),
                        help='limit the report to phase N (repeatable)')
    parser.add_argument('--check', action='store_true',
                        help='exit 1 when a declared gate path is absent or a phase has no '
                             'black-box file, printing why')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_manifest(ROOT)
    except GenerationError as error:
        print(f'rust_phase_gate: {error}', file=sys.stderr)
        return 2
    report = build_report(manifest, ROOT, selected_phases(args.phase))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
        if args.check:
            print()
            print(render_check(report))
    if not args.check:
        return 0
    return 0 if report['check']['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
