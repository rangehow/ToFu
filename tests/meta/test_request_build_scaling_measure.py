"""The request-build scaling benchmark runs the production round path.

``benchmarks/request_build_scaling.py`` is evidence only while every measured
stage really runs on one task across rounds: the production round builder,
the selected slot's adaptation and ``prepare_request`` with its sealed
lineage and raw-archive capture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'request_build_scaling_measure', ROOT / 'benchmarks' / 'request_build_scaling.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rounds_report_every_stage_over_a_growing_history(benchmark):
    rows = benchmark.run(rounds=6, every=3, sample=2)
    assert [row['round'] for row in rows] == [3, 6]
    assert rows[1]['messages'] > rows[0]['messages']
    for row in rows:
        assert row['build'] > 0 and row['prepare'] > 0
        assert row['total'] == pytest.approx(row['build'] + row['prepare'], abs=0.02)
        assert row['cold'] > 0


def test_one_round_seals_lineage_and_prepares_the_raw_archive(benchmark):
    messages = benchmark.initial_history()
    benchmark.append_tool_round(messages, 0)
    state = benchmark.Round()
    body, _ = state.build(messages, 0)
    plan, _ = state.prepare(body)
    assert plan.body.context_diagnostics()['sources']
    assert plan.raw_dumper.raw_archive_ref is not None
