"""scripts/resource_inventory.py: repeatable measurement of Tofu's artifacts and their growth."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from serverctl_pkg import local_placement

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'resource_inventory.py'


def load():
    spec = importlib.util.spec_from_file_location('resource_inventory_script', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recorded_measurements_report_growth_and_owners(tmp_path, monkeypatch, capsys):
    project, tmp, home = tmp_path / 'project', tmp_path / 'tmp', tmp_path / 'home'
    for path in (project / 'data', tmp, home):
        path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    workspaces = tmp / 'tofu-1-workspaces' / 'data-key'
    owner = workspaces / 'repo-key' / 'u1'
    (owner / 'project-worker-a').mkdir(parents=True)
    (owner / 'project-worker-a' / 'file').write_bytes(b'x' * 8192)
    (tmp / f'tofu-{os.getuid()}-tmp' / '0123456789ab').mkdir(parents=True)
    (owner / 'stray.log').write_text('agent scratch\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(local_placement, 'temporary_root', lambda: str(tmp))
    monkeypatch.setattr(local_placement, 'candidate_roots', lambda purpose: (
        [tmp / 'tofu-1-workspaces'] if purpose == local_placement.PURPOSE_WORKSPACES else []))
    script = load()

    assert script.main(['--project', str(project), '--record']) == 0
    (owner / 'project-worker-a' / 'more').write_bytes(b'y' * 65536)
    assert script.main(['--project', str(project), '--record']) == 0

    history = [json.loads(line) for line in
               (project / 'data' / 'resource-inventory.jsonl').read_text().splitlines()]
    assert len(history) == 2
    first, second = (record['artifacts'] for record in history)
    assert second['task checkouts']['bytes'] > first['task checkouts']['bytes']
    assert first['stray files beside task checkouts']['paths'] == 1
    assert first['task command scratch']['paths'] == 1
    assert all(item['owner'] for item in second.values())
    output = capsys.readouterr().out
    assert 'growth since' in output and 'task checkouts' in output


def test_dolphinfs_stores_are_sized_by_stat_without_a_deep_walk(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    authority = project / 'data' / 'tofudb-authority'
    (authority / 'payload-segments').mkdir(parents=True)
    (authority / 'payload-segments' / 'segment-1').write_bytes(b's' * 1000)
    (authority / 'active-1.wal').write_bytes(b'w' * 100)
    (authority / 'blocks' / '00').mkdir(parents=True)
    (authority / 'blocks' / '00' / 'loose-block').write_bytes(b'b' * 7)
    run = project / '.eval-runs' / 'eval-matrix-20261112'
    (run / 'runs' / 'model' / 'r1').mkdir(parents=True)
    (run / 'plan.json').write_bytes(b'p' * 10)
    (run / 'runs' / 'model' / 'r1' / 'trial.log').write_bytes(b't' * 5)
    script = load()
    listed = []
    real_scandir = os.scandir
    monkeypatch.setattr(script.os, 'scandir', lambda path: listed.append(Path(path)) or real_scandir(path))

    measured = {name: script.measure_stat(paths) for name, _, paths in script.stat_artifacts(project)}

    assert measured['data/tofudb-authority']['bytes'] == 1100
    assert measured['.eval-runs']['bytes'] == 10
    assert all(item['walk'] == f'stat to depth {script.STAT_DEPTH}' for item in measured.values())
    assert all(len(path.relative_to(project).parts) <= 3 for path in listed)
    assert authority / 'blocks' / '00' not in listed and run / 'runs' not in listed
