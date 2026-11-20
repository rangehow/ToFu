"""The Rust rewrite phase gate measures the plan's declared parity gates.

docs/RUST_REWRITE_EVIDENCE.md "Phased plan": a phase is done when its parity
gate passes against the Rust implementation, and the gate column names the
tests that gate it. ``scripts/rust_phase_gate.py`` copies each row's concrete
test paths (``tests/...`` and the tofu-search suite under
``packages/tofu-search/tests``) verbatim and reports, from the Phase 0
classification manifest, which files back the gate and how many are black-box
(retargetable at a server URL) or white-box (must be ported). The script's
table must not drift from the document, a declared path that is missing must
stay visible, and ``--check`` must fail while a gate cannot be measured.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/rust_phase_gate.py'
DOC = ROOT / 'docs/RUST_REWRITE_EVIDENCE.md'
PHASE_SECTION = '### Phased plan'
PHASE_ROW = re.compile(r'\*\*(\d+)\.')
TEST_PATH = re.compile(r'`((?:packages/[\w.-]+/)?tests(?:/[^`]+)?)`')


def _load_script():
    spec = importlib.util.spec_from_file_location('rust_phase_gate', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def gate():
    return _load_script()


@pytest.fixture(scope='module')
def manifest(gate):
    return gate.build_manifest(ROOT)


@pytest.fixture(scope='module')
def report(gate, manifest):
    return gate.build_report(manifest, ROOT)


def _doc_phase_paths() -> dict[int, list[str]]:
    """{phase: test paths in cell order} parsed from the parity-gate column."""
    lines = DOC.read_text(encoding='utf-8').splitlines()
    rows: dict[int, list[str]] = {}
    index = lines.index(PHASE_SECTION) + 1
    while index < len(lines) and not lines[index].startswith('#'):
        line = lines[index]
        index += 1
        if not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) != 3:
            continue
        phase = PHASE_ROW.match(cells[0])
        if phase is None:
            continue
        rows[int(phase.group(1))] = TEST_PATH.findall(cells[2])
    return rows


def _entry(path: str, kind: str, test_count: int, surfaces=()) -> dict:
    return {'path': path, 'language': 'python', 'owner': 'tests',
            'testCount': test_count, 'regexTestCount': test_count,
            'kind': kind, 'surfaces': list(surfaces), 'signals': []}


def _synthetic_manifest() -> dict:
    return {'files': [
        _entry('tests/sse/test_http.py', 'black-box', 3, ('http', 'sse')),
        _entry('tests/sse/test_internal.py', 'white-box', 2),
        _entry('tests/sse_extra/test_prefix.py', 'black-box', 7, ('http',)),
        _entry('tests/chaos/test_chaos_faults.py', 'white-box', 4),
        _entry('tests/chaos/test_chaos_nodes.py', 'black-box', 1, ('process',)),
        _entry('tests/chaos/test_other.py', 'black-box', 9, ('http',)),
    ]}


def _synthetic_tree(tmp_path: Path) -> None:
    (tmp_path / 'tests/sse').mkdir(parents=True)
    (tmp_path / 'tests/chaos').mkdir(parents=True)
    (tmp_path / 'tests/sse/test_internal.py').write_text('', encoding='utf-8')
    (tmp_path / 'tests/chaos/test_chaos_faults.py').write_text('', encoding='utf-8')


def test_script_declares_every_plan_phase_verbatim(gate, report):
    """(b) The authored table, and the report built from it, equal the document."""
    doc_rows = _doc_phase_paths()
    scripted = {row.phase: list(row.declared) for row in gate.PHASE_GATES}
    reported = {row['phase']: [item['path'] for item in row['declared']]
                for row in report['phases']}
    # The plan's "**0. Contracts first**" row is the pre-rewrite contract
    # phase, gated by the generator --check runs in make contracts-check.
    assert set(scripted) == set(doc_rows) - {0}
    assert reported == scripted
    for phase, declared in scripted.items():
        assert declared, f'phase {phase} declares no gate path'
        assert declared == doc_rows[phase], f'phase {phase} drifts from the plan table'


def test_phase_five_names_concrete_owner_directories_in_order(gate, report):
    """Phase 5's owners map to real directories, in the row's dependency order."""
    doc_paths = _doc_phase_paths()[5]
    plan_row = next(row for row in gate.PHASE_GATES if row.phase == 5)
    measured = next(row for row in report['phases'] if row['phase'] == 5)
    assert doc_paths == list(plan_row.declared) == [item['path'] for item in measured['declared']]
    assert all('<' not in path and '>' not in path for path in doc_paths)
    assert measured['missing'] == []
    assert measured['blackBox']['files'] > 0


def test_declared_paths_outside_the_manifest_scope_are_measured(gate, report):
    """The generator scans tests/ only; a declared package suite still counts."""
    measured = next(row for row in report['phases'] if row['phase'] == 5)
    declared = {item['path']: item for item in measured['declared']}
    outside = report['classification']['outsideManifestScope']
    assert declared['packages/tofu-search/tests']['files'] == outside['files'] > 0
    assert outside['tests'] > 0


def test_every_declared_path_exists_or_is_reported_missing(gate, report):
    """(a) No declared path is silently dropped; missing ones stay visible."""
    rows = {row['phase']: row for row in report['phases']}
    assert set(rows) == {row.phase for row in gate.PHASE_GATES}
    for plan_row in gate.PHASE_GATES:
        row = rows[plan_row.phase]
        assert [item['path'] for item in row['declared']] == list(plan_row.declared)
        assert set(row['missing']) <= set(plan_row.declared)
        for item in row['declared']:
            exists = (ROOT / item['path']).exists()
            assert item['exists'] is exists
            if exists:
                assert item['path'] not in row['missing']
            else:
                assert item['path'] in row['missing'], f'{item["path"]} dropped'
            assert item['template'] == ('<' in item['path'] or '>' in item['path'])


def test_phase_counts_conserve_the_selected_manifest_rows(report):
    for row in report['phases']:
        assert row['blackBox']['files'] + row['whiteBox']['files'] == row['files']
        assert row['blackBox']['tests'] + row['whiteBox']['tests'] == row['tests']
        assert len(row['blackBoxFiles']) == row['blackBox']['files']
        for totals in row['bySurface'].values():
            assert totals['files'] <= row['files']
            assert totals['tests'] <= row['tests']


def test_selection_uses_declared_directories_and_exact_files(gate, tmp_path):
    """(c) Selection is a function of the manifest and the declared paths only."""
    _synthetic_tree(tmp_path)
    phases = (
        gate.PhaseGate(1, 'Synthetic directory gate', ('tests/sse',)),
        gate.PhaseGate(2, 'Synthetic file gate',
                       ('tests/chaos/test_chaos_faults.py', 'tests/absent')),
    )
    synthetic = gate.build_report(_synthetic_manifest(), tmp_path, phases)
    directory, exact_file = synthetic['phases']

    assert directory['files'] == 2
    assert directory['tests'] == 5
    assert directory['blackBox'] == {'files': 1, 'tests': 3}
    assert directory['whiteBox'] == {'files': 1, 'tests': 2}
    assert directory['bySurface']['http'] == {'files': 1, 'tests': 3}
    assert directory['bySurface']['sse'] == {'files': 1, 'tests': 3}
    assert directory['bySurface']['process'] == {'files': 0, 'tests': 0}
    assert directory['blackBoxFiles'] == ['tests/sse/test_http.py']
    assert directory['missing'] == []
    assert [item['files'] for item in directory['declared']] == [2]

    assert exact_file['files'] == 1
    assert exact_file['tests'] == 4
    assert exact_file['blackBox'] == {'files': 0, 'tests': 0}
    assert exact_file['whiteBox'] == {'files': 1, 'tests': 4}
    assert exact_file['missing'] == ['tests/absent']

    assert synthetic['classification'] == {
        'files': 6, 'tests': 26,
        'blackBox': {'files': 4, 'tests': 20},
        'whiteBox': {'files': 2, 'tests': 6},
        'outsideManifestScope': {'files': 0, 'tests': 0},
    }


def test_check_names_absent_paths_and_phases_without_black_box(gate, tmp_path):
    """The findings that make --check fail, each with its reason."""
    _synthetic_tree(tmp_path)
    phases = (
        gate.PhaseGate(1, 'Measurable gate', ('tests/sse',)),
        gate.PhaseGate(2, 'Absent gate', ('tests/absent',)),
        gate.PhaseGate(3, 'White-box only gate', ('tests/sse/test_internal.py',)),
    )
    synthetic = gate.build_report(_synthetic_manifest(), tmp_path, phases)
    failures = synthetic['check']['failures']

    assert synthetic['check']['ok'] is False
    assert len(failures) == 3
    assert any('phase 2' in failure and 'tests/absent' in failure for failure in failures)
    assert sum('black-box' in failure for failure in failures) == 2
    assert not any('phase 1' in failure for failure in failures)


def test_check_exit_codes(gate, monkeypatch, tmp_path, manifest, capsys):
    """(d) 0 when a gate is measurable, 1 when a path is absent or nothing is black-box."""
    original_phases = gate.PHASE_GATES
    _synthetic_tree(tmp_path)
    passing = (gate.PhaseGate(1, 'Measurable gate', ('tests/sse',)),)
    failing = passing + (gate.PhaseGate(2, 'Absent gate', ('tests/absent',)),)
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    monkeypatch.setattr(gate, 'build_manifest', lambda root: _synthetic_manifest())

    monkeypatch.setattr(gate, 'PHASE_GATES', passing)
    assert gate.main(['--check', '--json']) == 0
    assert gate.main(['--json']) == 0

    monkeypatch.setattr(gate, 'PHASE_GATES', failing)
    assert gate.main(['--json']) == 0
    capsys.readouterr()
    assert gate.main(['--check', '--json']) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed['check']['ok'] is False
    assert any('tests/absent' in failure for failure in printed['check']['failures'])

    # The real tree's Phase 1 gate is measurable: the same CLI path exits 0.
    monkeypatch.setattr(gate, 'ROOT', ROOT)
    monkeypatch.setattr(gate, 'build_manifest', lambda root: manifest)
    monkeypatch.setattr(gate, 'PHASE_GATES', original_phases)
    assert gate.main(['--check', '--phase', '1']) == 0
    # Phase 5 is the set that previously had only a template path.
    assert gate.main(['--check', '--phase', '5']) == 0


def test_cli_json_selects_phases(gate):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), '--phase', '1', '--phase', '7', '--json'],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    printed = json.loads(completed.stdout)
    assert printed['contract'] == gate.CONTRACT
    assert [row['phase'] for row in printed['phases']] == [1, 7]
    assert all(row['declared'] for row in printed['phases'])
    assert all(row['blackBox']['files'] > 0 for row in printed['phases'])


def test_list_renders_every_phase_and_its_missing_paths(gate, report):
    text = gate.render(report)
    assert 'Declared gate paths' in text
    for row in report['phases']:
        assert row['title'] in text
        for path in row['missing']:
            assert f'phase {row["phase"]}: {path}' in text
    check = gate.render_check(report)
    if report['check']['ok']:
        assert check.startswith('Check: PASS')
    else:
        assert check.startswith('Check: FAIL')
        for failure in report['check']['failures']:
            assert failure in check
