"""A coordinator wake of a multi-Worker request is admitted on segmented storage.

Incident 2026-11-12: from 15:54, after the restart that deployed 214650e681,
every coordinator wake that needed a new wake row was retained with
``WakeEvidenceChanged`` (1,629 times by 21:36, across the parallel, ndp and Tofu
Project Routers and the Global Router), so Worker results never woke them.
``recover_coordinator`` derives the wake from the request's whole source cohort,
then re-derives it inside the admission CAS to prove nothing changed. For a
request with a request budget, that CAS read only the budget's first row
(``firstOfBudgets``, ``cohort=False``), so with more than one Worker the
re-derived wake listed fewer Workers and never matched. The portable
continuation fixture keeps dispatch storage v1, which reads the whole record
and so cannot show it; these runs use the segmented v2 layout on real
authorities.
"""
import uuid

import pytest

from tests.project_mod.test_coordinator_continuation import (
    _bound_request, controls, delegate, end, run as run)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime as base_runtime
from tests.project_mod.test_project_dispatch_repository import context as context

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

__all__ = ('run', 'runtime', 'base_runtime', 'context')


@pytest.fixture(autouse=True)
def coordinator_authority_owner(context, monkeypatch):
    # The native Hello admits the coordinator contracts' explicit owner.
    from scripts import tofudb_perf_compare
    monkeypatch.setattr(tofudb_perf_compare, '_OWNER_ID', context.repo.owner)


@pytest.fixture
def segmented_run(detached_semantic_authority, run):
    factory, _, _ = detached_semantic_authority
    r = run
    for (namespace, key), row in r.c.client.rows.items():
        factory(write=True).command('record.put', {'namespace': namespace, 'key': key,
            'expected_version': 0, 'value': row['value']}, uuid.uuid4().hex)
    r.c.repo.client = factory
    r.c.repo.storage_version = 2
    return r


def test_a_results_wake_of_a_budgeted_request_with_two_workers_is_admitted(segmented_run):
    r = segmented_run
    _bound_request(r)
    first, second = delegate(r, 'one'), delegate(r, 'two')
    end(r, first)
    end(r, second)
    assert r.wake() == []  # The live original task still owns follow-through.
    r.attempts['coordinator-attempt']['status'] = 'completed'

    assert r.wake() == ['accepted']

    [wake] = controls(r)
    assert wake['request']['coordinatorWake']['reason'] == 'results'
    assert first['id'] + ': completed' in wake['request']['brief']
    assert second['id'] + ': completed' in wake['request']['brief']
    assert r.wake() == [] and len(controls(r)) == 1
