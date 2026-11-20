"""contracts/events_v1.json is the event authority; the emission registry cannot drift from it."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gen_event_contract as event_gen

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def contract():
    return event_gen.load_contract()


@pytest.fixture(scope='module')
def registry():
    return event_gen.registry_document()


def test_registry_matches_contract_exactly(contract, registry):
    assert event_gen.registry_drift(contract, registry) == []


def test_event_registered_in_code_but_missing_from_contract_fails(contract, registry):
    trimmed = copy.deepcopy(contract)
    removed = trimmed['events'].pop(0)['type']
    assert event_gen.registry_drift(trimmed, registry) == [
        f"events: {removed!r} is registered in lib/agent_core/events.py but missing "
        'from contracts/events_v1.json']


def test_event_in_contract_but_not_registered_fails(contract, registry):
    extended = copy.deepcopy(contract)
    extended['events'].append({**extended['events'][0], 'type': 'never_registered'})
    assert event_gen.registry_drift(extended, registry) == [
        "events: 'never_registered' is in contracts/events_v1.json but not registered "
        'in lib/agent_core/events.py']


@pytest.mark.parametrize('section,key', [('phases', 'phase'), ('pushFrames', 'type')])
def test_phase_and_push_frame_drift_fails_both_ways(contract, registry, section, key):
    trimmed = copy.deepcopy(contract)
    removed = trimmed[section].pop()[key]
    assert any(f'{removed!r} is registered' in problem
               for problem in event_gen.registry_drift(trimmed, registry))
    extended = copy.deepcopy(contract)
    extended[section].append({**extended[section][0], key: 'never_registered'})
    assert any("'never_registered' is in contracts/events_v1.json" in problem
               for problem in event_gen.registry_drift(extended, registry))


def test_field_drift_names_the_event(contract, registry):
    changed = copy.deepcopy(contract)
    spec = next(item for item in changed['events'] if item['schema'])
    spec['schema'].append({'name': 'undeclaredField', 'kind': 'str', 'required': False})
    problems = event_gen.registry_drift(changed, registry)
    assert len(problems) == 1 and problems[0].startswith(f"events: {spec['type']!r} differs")
    assert 'undeclaredField' in problems[0]


def test_check_cli_fails_on_drift(tmp_path, contract):
    drifted = copy.deepcopy(contract)
    drifted['events'].pop()
    path = tmp_path / 'events_v1.json'
    path.write_text(json.dumps(drifted), encoding='utf-8')
    code = (
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'from scripts import gen_event_contract as g; g.CONTRACT = sys.argv[2]; '
        'sys.argv = ["gen_event_contract.py"]; raise SystemExit(g.main())')
    completed = subprocess.run([sys.executable, '-c', code, str(ROOT), str(path)],
                               capture_output=True, text=True, check=False, cwd=ROOT)
    assert completed.returncode == 1
    assert 'missing from contracts/events_v1.json' in completed.stderr
