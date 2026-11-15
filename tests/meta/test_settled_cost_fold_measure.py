"""Self-check for benchmarks/settled_cost_fold.py (equal work gates clocks)."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[2] / 'benchmarks' / 'settled_cost_fold.py'


def _measure():
    spec = importlib.util.spec_from_file_location('settled_cost_fold', _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_digest_is_deterministic_and_independent_of_gil_load():
    measure = _measure()
    idle = measure.run_worker(30, 1, 2, 0, 3)
    again = measure.run_worker(30, 1, 2, 0, 3)
    loaded = measure.run_worker(30, 1, 2, 1, 3)
    assert idle['digest'] == again['digest'] == loaded['digest']
    assert idle['frames'] == {'unchanged': 2, 'append': 1}
    assert idle['ledgerRounds'] == 32
    assert set(idle['medianMs']) == {'unchanged', 'append'}
    assert set(idle['medianMs']['append']) == {'fold', 'sanitize', 'project', 'frame'}
    assert measure.run_worker(31, 1, 2, 0, 3)['digest'] != idle['digest']


def test_worker_restores_the_seams_it_times():
    import lib.cost as cost_module
    import lib.turn_lifecycle as turn_lifecycle

    fold = cost_module.compute_api_rounds_cost
    sanitize = turn_lifecycle.sanitize_api_rounds_for_persist
    _measure().run_worker(4, 1, 1, 0, 3)
    assert cost_module.compute_api_rounds_cost is fold
    assert turn_lifecycle.sanitize_api_rounds_for_persist is sanitize


def test_driver_refuses_clocks_when_trees_do_unequal_work(monkeypatch, capsys):
    measure = _measure()
    digests = iter(['a' * 64, 'b' * 64])

    def fake_arm(_root, rounds, cycles, unchanged, busy, seed):
        return {'rounds': rounds, 'busy': busy, 'seed': seed, 'cycles': cycles,
                'unchanged': unchanged, 'digest': next(digests), 'frames': {},
                'medianMs': {}, 'ledgerRounds': rounds, 'source': '', 'python': '',
                'pid': 0}

    monkeypatch.setattr(measure, '_run_arm', fake_arm)
    with pytest.raises(SystemExit, match='different projections'):
        measure.main(['--source-root', str(_PATH.parents[1]), '--rounds', '4',
                      '--busy', '0'])
    assert 'fold' not in capsys.readouterr().out
