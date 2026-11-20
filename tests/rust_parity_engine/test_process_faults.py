"""Driver plumbing tests only: the probe is NOT an engine/storage replica."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from .compare import PART_NAMES, _answers, compare_answers
from .process_faults import ProcessFaultError, run_crash_case

pytestmark = pytest.mark.unit


PROBE = '''
import argparse, json, os, signal, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--storage-config')
parser.add_argument('--data-root')
args = parser.parse_args()
request = json.loads(sys.stdin.readline())
root = Path(args.data_root)
assert Path(os.environ['TOFU_DATA_DIR']).is_relative_to(root)
assert os.environ['PORT'] == '9'
assert 'TOFU_STORAGE_TOKEN' not in os.environ
assert 'LLM_API_KEY' not in os.environ
assert 'TOFU_PROCESS_ROLE' not in os.environ
assert Path(args.storage_config).is_file()
phase = request['process']['phase']
with (root / 'probe.jsonl').open('a') as log:
    log.write(json.dumps({'pid':os.getpid(),'phase':phase})+'\\n')
mode = request['input'].get('probeMode')
receipt = {'storageAuthority':'protocol-probe-only', 'checkpoint':{'receipt':'not-engine-evidence'}}
if phase == 'initial':
    if mode == 'no-barrier':
        sys.exit(0)
    if mode == 'partial-line':
        print('{', end='', flush=True)
    else:
        barrier = {'kind':'fault_ready','requestId':request['id'], **receipt}
        if mode == 'missing-receipt':
            del barrier['checkpoint']
        print(json.dumps(barrier), flush=True)
    signal.pause()
else:
    if mode == 'wrong-receipt':
        receipt['checkpoint'] = {'receipt':'different'}
    answer = {'id':request['id'], 'parts':[], 'recovery':receipt}
    if mode == 'unsupported':
        answer['unsupported'] = 'real recovery not bound'
    if mode == 'wrong-id':
        answer['id'] = 'foreign'
    print(json.dumps(answer), flush=True)
    if mode == 'extra-answer':
        print(json.dumps(answer), flush=True)
'''


@pytest.fixture
def probe(tmp_path):
    script = tmp_path / 'protocol_probe.py'
    script.write_text(PROBE)
    config = tmp_path / 'storage.json'
    config.write_text('{}')
    return [sys.executable, str(script)], config


def _run(probe, root, mode=None, timeout=10):
    command, config = probe
    request = {'id': 'crash-case', 'boundary': 'engine_session',
               'input': {'restartAfterRequest': 1, 'probeMode': mode}}
    return run_crash_case(command, request, data_root=root,
                          storage_config=config, timeout=timeout)


def test_driver_kills_child_and_launches_new_process_with_same_root(probe, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_TOKEN', 'must-not-inherit')
    monkeypatch.setenv('LLM_API_KEY', 'must-not-inherit')
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'execution')
    root = tmp_path / 'instance'
    result = _run(probe, root)
    assert result['initialExit'] == -signal.SIGKILL
    assert result['initialPid'] != result['resumePid']
    observations = [json.loads(line) for line in (root / 'probe.jsonl').read_text().splitlines()]
    assert observations == [
        {'pid': result['initialPid'], 'phase': 'initial'},
        {'pid': result['resumePid'], 'phase': 'resume'},
    ]
    # A successful kill/reopen protocol is not engine parity evidence.
    assert not compare_answers('crash-case', result['answer'], result['answer'])['identical']


@pytest.mark.parametrize(('mode', 'message'), [
    ('no-barrier', 'before the crash barrier'),
    ('missing-receipt', 'durable crash barrier'),
    ('wrong-receipt', 'exact durable source'),
    ('unsupported', 'supported engine parts'),
    ('wrong-id', 'identity mismatch'),
    ('extra-answer', 'exactly one answer'),
])
def test_driver_refuses_incomplete_or_foreign_recovery(probe, tmp_path, mode, message):
    with pytest.raises(ProcessFaultError, match=message):
        _run(probe, tmp_path / 'instance', mode)


def test_partial_line_cannot_hold_driver_past_deadline(probe, tmp_path):
    with pytest.raises(ProcessFaultError, match='crash barrier'):
        _run(probe, tmp_path / 'instance', 'partial-line', timeout=0.1)


def test_driver_does_not_reuse_existing_data_root(probe, tmp_path):
    with pytest.raises(FileExistsError):
        _run(probe, tmp_path)


def _answer(case='case'):
    return {'id': case, 'parts': [
        {'name': name, 'b64': base64.b64encode(b'{}').decode()}
        for name in sorted(PART_NAMES)]}


@pytest.mark.parametrize('part_name', sorted(PART_NAMES))
def test_exact_parity_checks_all_parts_and_bytes_without_suppression(part_name):
    left, right = _answer(), _answer()
    assert compare_answers('case', left, right)['identical']
    part = next(part for part in right['parts'] if part['name'] == part_name)
    part['b64'] = base64.b64encode(b'{ }').decode()
    result = compare_answers('case', left, right)
    assert not result['identical']
    assert result['different'] == [part_name]


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'duplicate', 'invalid-base64',
                                      'foreign-id', 'error-with-parts', 'unsupported'])
def test_parity_refuses_malformed_or_unsupported_answers(mutation):
    left, right = _answer(), _answer()
    if mutation == 'missing':
        right['parts'].pop()
    elif mutation == 'extra':
        right['parts'].append({'name': 'extra', 'b64': 'e30='})
    elif mutation == 'duplicate':
        right['parts'][-1] = right['parts'][0]
    elif mutation == 'invalid-base64':
        right['parts'][0]['b64'] = '!'
    elif mutation == 'foreign-id':
        right['id'] = 'foreign'
    else:
        right['error' if mutation == 'error-with-parts' else 'unsupported'] = 'failure'
    assert not compare_answers('case', left, right)['identical']


@pytest.mark.parametrize('output', ['', '{"id":"case"}\n{"id":"extra"}', '{"id":"wrong"}'])
def test_line_comparison_refuses_missing_extra_and_reordered_answers(monkeypatch, output):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=output.encode(), stderr=b''))
    with pytest.raises(ValueError):
        _answers(['unused'], [{'id': 'case'}])


def test_line_comparison_refuses_nonzero_child_even_with_valid_answer(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout=b'{"id":"case"}\n', stderr=b'failed'))
    with pytest.raises(RuntimeError, match='exited 1'):
        _answers(['unused'], [{'id': 'case'}])


def test_reference_refuses_process_recovery_instead_of_simulating_it():
    request = {'id': 'process-case', 'boundary': 'engine_session',
               'input': {'model': 'alpha', 'restartAfterRequest': 1},
               'process': {'phase': 'resume'}}
    answers = _answers([sys.executable, '-m', 'tests.rust_parity_engine.reference'], [request])
    assert 'parts' not in answers[0]
    assert 'claimed Attempt' in answers[0]['unsupported']


def test_process_gate_is_red_without_real_recovery_adapter(tmp_path):
    from .process_faults import main
    config = tmp_path / 'storage.json'
    config.write_text('{}')
    report_path = tmp_path / 'report.json'
    status = main([
        '--python-command', json.dumps([sys.executable, '-m', 'tests.rust_parity_engine.reference']),
        '--rust-command', json.dumps(['unbound-rust-adapter']),
        '--python-storage-config', str(config), '--rust-storage-config', str(config),
        '--timeout', '10', '--report', str(report_path),
    ])
    report = json.loads(report_path.read_text())
    assert status == 1
    assert report['identical'] is False
    assert {row['case'] for row in report['cases']} == {
        'restart_after_first_request', 'restart_after_tool_round'}
    assert all(not row['identical'] and 'error' in row for row in report['cases'])
