"""A delivery whose only conflict is in Rust/Python storage-contract outputs lands.

The storage contract generators (``scripts/gen_storage_v2_contract.py``,
``scripts/gen_tofudb_ir_contract.py``) render rustfmt-formatted Rust and Python
constants from ``contracts/*.json``. Two independent contract edits a few lines
apart merge cleanly in the JSON, but their rendered constants are adjacent and
conflict. The publisher rebuilds the outputs from the merged contract.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lib.project_mod import task_delta_delivery

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = 'contracts/storage_v2.json'
SEMANTICS = 'lib/storage/generated_v2_semantics.py'
INPUTS = (CONTRACT, 'scripts/gen_storage_v2_contract.py', 'scripts/generated_rust.py',
          'packages/tofu-db/Cargo.toml')

pytestmark = pytest.mark.unit


def _toolchain():
    from serverctl_pkg.native_engine import resolve_cargo
    cargo, environment = resolve_cargo(os.environ, root=ROOT)
    rustfmt = shutil.which('rustfmt', path=environment['PATH']) if cargo else None
    # rustup picks the toolchain from RUSTUP_HOME; a proxy without one cannot format.
    if not rustfmt or subprocess.run([rustfmt, '--version'], env=environment,
                                     capture_output=True).returncode:
        pytest.skip('generated Rust needs a Rust toolchain (CARGO_HOME/RUSTUP_HOME)')
    return environment


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def generate(root, environment):
    subprocess.run([sys.executable, 'scripts/gen_storage_v2_contract.py'], cwd=root, check=True,
                   capture_output=True, env={**environment, 'PYTHONPATH': str(ROOT)})


def edit_contract(root, old, new, environment, message):
    path = root / CONTRACT
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))
    generate(root, environment)
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', message)
    return git(root, 'rev-parse', 'HEAD')


def test_storage_contract_output_conflict_is_rebuilt_from_the_merged_contract(tmp_path, monkeypatch):
    environment = _toolchain()
    monkeypatch.setenv('CARGO_HOME', environment.get('CARGO_HOME', ''))
    monkeypatch.setenv('RUSTUP_HOME', environment.get('RUSTUP_HOME', ''))
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Contract test')
    git(root, 'config', 'user.email', 'contract@example.invalid')
    from lib.project_mod.generated_integration import CONFLICT_OUTPUT_PATTERNS
    for path in CONFLICT_OUTPUT_PATTERNS['storage_v2']:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
    for path in INPUTS:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, root / path)
    generate(root, environment)
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'contract base')
    baseline = git(root, 'rev-parse', 'HEAD')
    git(root, 'switch', '-qc', 'task')
    task = edit_contract(root, '"recoveryPointSeconds": 72000', '"recoveryPointSeconds": 72001',
                         environment, 'task: recovery point')
    git(root, 'switch', '-q', 'main')
    head = edit_contract(root, '"schedulerSystemKey": "storage-backup"',
                         '"schedulerSystemKey": "storage-backup-2"', environment, 'canonical: key')
    # The rendered constants are adjacent: a plain merge conflicts in the output only.
    merged = subprocess.run(['git', '-C', str(root), 'merge', '--no-commit', task],
                            text=True, capture_output=True)
    conflicted = git(root, 'diff', '--name-only', '--diff-filter=U').split()
    git(root, 'merge', '--abort')
    assert merged.returncode == 1 and SEMANTICS in conflicted and CONTRACT not in conflicted

    target, conflict, paths = task_delta_delivery._merge_workspace_delta(
        root, head, tmp_path / 'task.patch', 'task', tmp_path / 'index', baseline, task)

    assert conflict == '' and paths == [], (conflict, paths)
    semantics = git(root, 'show', f'{target}:{SEMANTICS}')
    assert 'NATIVE_ONLINE_BACKUP_RECOVERY_POINT_SECONDS = 72001' in semantics
    assert "NATIVE_ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY = 'storage-backup-2'" in semantics
    # The outputs are exactly what the generator makes from the merged contract.
    git(root, 'switch', '-q', '--detach', target)
    subprocess.run([sys.executable, 'scripts/gen_storage_v2_contract.py', '--check'], cwd=root,
                   check=True, capture_output=True, env={**environment, 'PYTHONPATH': str(ROOT)})
