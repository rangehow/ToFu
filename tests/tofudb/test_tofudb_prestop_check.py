"""Executable specification of scripts/tofudb_prestop_check.py: the read-only
pre-restart WAL check (docs/NATIVE_STORAGE_LIFECYCLE.md, stop budget)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'tofudb_prestop_check.py'


def _load():
    spec = importlib.util.spec_from_file_location('tofudb_prestop_check', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_module = _load()
CONTRACT_THRESHOLD = json.loads((ROOT / 'contracts' / 'tofudb_ir_v1.json').read_text())[
    'activeWalRotation']['softThresholdBytes']


def _wal(authority: Path, generation: int, size: int) -> Path:
    authority.mkdir(parents=True, exist_ok=True)
    path = authority / f'active-{generation:016x}.wal'
    with open(path, 'wb') as handle:
        handle.truncate(size)  # sparse: no real bytes are written
    return path


def _run(authority: Path, capsys) -> tuple[int, dict]:
    code = check_module.main(['--data-dir', str(authority), '--json'])
    return code, json.loads(capsys.readouterr().out)


def test_the_threshold_is_the_contracts_rotation_soft_threshold(tmp_path):
    _wal(tmp_path / 'authority', 7, 1)
    result = check_module.check(tmp_path / 'authority')
    assert result['threshold_bytes'] == CONTRACT_THRESHOLD
    assert result['contract'] == str(ROOT / 'contracts' / 'tofudb_ir_v1.json')


def test_a_window_at_the_threshold_passes_and_one_byte_more_fails(tmp_path, capsys):
    authority = tmp_path / 'authority'
    newest = _wal(authority, 0xA89B, CONTRACT_THRESHOLD)
    code, result = _run(authority, capsys)
    assert code == 0 and result['verdict'] == 'pass'
    assert result['newest'] == {'file': newest.name, 'generation': 0xA89B, 'bytes': CONTRACT_THRESHOLD}
    with open(newest, 'r+b') as handle:
        handle.truncate(CONTRACT_THRESHOLD + 1)
    code, result = _run(authority, capsys)
    assert code == 1 and result['verdict'] == 'fail'
    assert result['over_threshold'] == [newest.name]


def test_a_held_predecessor_over_the_threshold_fails(tmp_path, capsys):
    # A large window that just sealed leaves the newest active WAL small while
    # the stop must still finish installing its predecessor.
    authority = tmp_path / 'authority'
    _wal(authority, 0x11, 4096)
    sealed = _wal(authority, 0x10, CONTRACT_THRESHOLD + 1)
    code, result = _run(authority, capsys)
    assert code == 1
    assert result['predecessor']['file'] == sealed.name
    assert result['over_threshold'] == [sealed.name]


def test_older_generations_are_reported_never_judged(tmp_path, capsys):
    authority = tmp_path / 'authority'
    orphan = _wal(authority, 0x24B4, CONTRACT_THRESHOLD * 2)
    _wal(authority, 0xA89A, CONTRACT_THRESHOLD // 2)
    _wal(authority, 0xA89B, 176_375)
    code, result = _run(authority, capsys)
    assert code == 0 and result['verdict'] == 'pass'
    assert result['predecessor']['generation'] == 0xA89A
    assert [window['file'] for window in result['older']] == [orphan.name]


def test_a_contracts_threshold_is_what_decides(tmp_path):
    contract = tmp_path / 'contract.json'
    contract.write_text(json.dumps({'activeWalRotation': {'softThresholdBytes': 1000}}))
    authority = tmp_path / 'authority'
    path = _wal(authority, 3, 1000)
    assert check_module.check(authority, contract)['verdict'] == 'pass'
    with open(path, 'r+b') as handle:
        handle.truncate(1001)
    assert check_module.check(authority, contract)['verdict'] == 'fail'


def test_it_cannot_decide_without_an_authority_a_wal_or_a_threshold(tmp_path, capsys):
    code, result = _run(tmp_path / 'missing', capsys)
    assert (code, result['verdict']) == (2, 'undecided')
    (tmp_path / 'empty').mkdir()
    (tmp_path / 'empty' / 'CONTROL').write_bytes(b'')
    code, result = _run(tmp_path / 'empty', capsys)
    assert (code, result['verdict']) == (2, 'undecided')
    assert 'no active-<generation>.wal' in result['reason']
    contract = tmp_path / 'no-threshold.json'
    contract.write_text(json.dumps({'activeWalRotation': {}}))
    _wal(tmp_path / 'authority', 1, 1)
    with pytest.raises(check_module.Undecided):
        check_module.check(tmp_path / 'authority', contract)


def test_the_check_writes_nothing(tmp_path, capsys):
    authority = tmp_path / 'authority'
    _wal(authority, 0x20, 10)
    _wal(authority, 0x1F, 20)
    (authority / 'CONTROL').write_bytes(b'x' * 8192)

    def snapshot():
        return sorted((entry.name, entry.stat().st_size, entry.stat().st_mtime_ns)
                      for entry in os.scandir(authority))

    before = snapshot()
    assert check_module.main(['--data-dir', str(authority)]) == 0
    assert 'PASS' in capsys.readouterr().out
    assert snapshot() == before


def test_the_default_authority_is_resolved_without_creating_anything(tmp_path, monkeypatch):
    base = tmp_path / 'base'
    monkeypatch.setenv('TOFU_DATA_DIR', str(base))
    assert check_module.default_authority() == base / 'data' / 'tofudb-authority'
    assert not base.exists()
