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
