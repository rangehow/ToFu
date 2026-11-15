#!/usr/bin/env python3
"""Past research must be DISCOVERABLE without knowing its direction text.

The one-way-hash trap (owner-found, epic pt_a40dbd9569194b52).
``/api/v1/research/lookup`` requires the caller to already know the direction
TEXT. But the stored key is ``sha256(normalised direction)[:32]`` — a one-way
hash — and nothing indexed which directions had been researched. So a user
returning a week later had to recall their exact wording or the artifacts were
unreachable. The fix is cheap because ``meta.direction`` already stores the
original text; what was missing was an endpoint that lists it.

The list tests drive real HTTP against a real DB with the task registry
emptied, so no test here merely asserts that a function exists.

Run:  pytest tests/research/test_research_discoverability.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

TEST_OWNER_USER_ID = 1

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit

_DIR_A = 'long-context KV-cache compression'
_DIR_B = 'diffusion language models'

_GAPS = {'schema_version': 1, 'direction': _DIR_A, 'lang': 'en',
         'open_gaps': [{'id': 'gap_1', 'gap': 'needle recall unmeasured',
                        'why_open': 'only perplexity reported',
                        'evidence': ['2305.11111']}]}

_IDEATE = {
    'accepted': [{
        'id': 'idea_1', 'title': 'Per-layer learnable compression rate',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'core_mechanism': 'attention entropy varies per layer',
        'novelty_claim': 'unlike 2305.11111 the rate is learned',
        'falsifiable_prediction': 'needle recall drops <2% at 4x',
        'why_not_AB': 'derived from the spectrum, not bolted on',
        'scores': {'novelty': 4, 'falsifiability': 5,
                   'mechanism_depth': 4, 'value': 4}, 'overall': 4.25}],
    'rejected': [{
        'id': 'idea_2', 'title': 'KV compression + speculative decoding',
        'reject_stage': 'rubric', 'reject_reason': 'overall 2.75 < 4.0',
        'scores': {'novelty': 2, 'falsifiability': 3,
                   'mechanism_depth': 2, 'value': 4}, 'overall': 2.75}],
    'threshold': 4.0, 'gate_reached': 'accepted'}


def _new_loop_run(coro):
    """Drive one coroutine on a private loop.

    The suite runs with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, so pytest-asyncio is
    NOT loaded and an ``@pytest.mark.asyncio`` test would be silently skipped
    rather than run — a guard that never executes is worse than none.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    import lib.research.persistence as persistence
    from lib.storage import StorageSupervisor

    supervisor = StorageSupervisor(
        project_root=tmp_path / 'sidecar', backend='sqlite', startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(
        persistence, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield supervisor
    finally:
        supervisor.stop()


@pytest.fixture()
def client(fresh_db):
    import server
    server.app.config['TESTING'] = True
    return server.app.test_client()


def _empty_the_task_registry():
    from lib.research.runtime import _research_runtime
    with _research_runtime._lock:
        _research_runtime._tasks.clear()


def _get_json(client, path, **query):
    resp = _new_loop_run(client.get(path, query_string=query))
    body = _new_loop_run(resp.get_json())
    # The v4 surface wraps every success in ``{data, meta}``; assertions in
    # this file reason about the domain payload, not the transport envelope.
    if isinstance(body, dict) and isinstance(body.get('data'), dict) and 'meta' in body:
        return resp.status_code, body['data']
    return resp.status_code, body


# ═══ 1. ★ Discoverability: find past work WITHOUT knowing the wording ═══

def test_past_directions_are_listable_without_knowing_the_text(client):
    """★ The decisive test for the one-way-hash trap.

    The only input is "I researched something before". If the list cannot
    return the direction TEXT, a user who forgot their exact wording can never
    reach the artifacts again — the hash is not reversible.
    """
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(
        _DIR_A, 'en', '# Survey A', _GAPS, model='m',
        user_id=TEST_OWNER_USER_ID)
    persist_ideate(
        _DIR_A, 'en', _IDEATE, model='m', user_id=TEST_OWNER_USER_ID)
    _empty_the_task_registry()

    status, body = _get_json(client, '/api/v4/research/list')
    assert status == 200, f'list failed: {status} {body}'
    items = body.get('items') or []
    assert items, 'no past research listed — the artifacts are undiscoverable'

    dirs = [it.get('direction') for it in items]
    assert _DIR_A in dirs, (
        f'the ORIGINAL direction text was not recoverable from the list: '
        f'{dirs} — a forgotten wording means unreachable artifacts')

    # And the listed direction must actually work as a lookup key: the whole
    # point is list → click → read.
    row = next(it for it in items if it['direction'] == _DIR_A)
    st2, got = _get_json(client, '/api/v4/research/lookup',
                         direction=row['direction'], lang=row.get('lang') or 'en')
    assert st2 == 200 and got.get('found') is True, (
        'a direction returned by /list did not resolve through /lookup — the '
        'two endpoints disagree on the identity')


def test_list_carries_the_counts_needed_to_choose(client):
    """A list of bare strings is not usable — the user picks by outcome."""
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(
        _DIR_A, 'en', '# Survey A', _GAPS, model='m',
        user_id=TEST_OWNER_USER_ID)
    persist_ideate(
        _DIR_A, 'en', _IDEATE, model='m', user_id=TEST_OWNER_USER_ID)
    _empty_the_task_registry()

    _, body = _get_json(client, '/api/v4/research/list')
    row = next(it for it in (body.get('items') or [])
               if it['direction'] == _DIR_A)
    assert row.get('accepted') == 1, f'accepted count missing/wrong: {row}'
    assert row.get('rejected') == 1, f'rejected count missing/wrong: {row}'
    assert row.get('lang') == 'en'
    assert isinstance(row.get('created_at'), int) and row['created_at'] > 0, (
        f'no usable timestamp to sort/display by: {row}')


def test_list_covers_multiple_directions_and_is_newest_first(client):
    from lib.research.persistence import persist_survey
    persist_survey(
        _DIR_A, 'en', '# A', _GAPS, model='m',
        user_id=TEST_OWNER_USER_ID)
    persist_survey(
        _DIR_B, 'en', '# B', _GAPS, model='m',
        user_id=TEST_OWNER_USER_ID)
    _empty_the_task_registry()

    _, body = _get_json(client, '/api/v4/research/list')
    dirs = [it['direction'] for it in (body.get('items') or [])]
    assert _DIR_A in dirs and _DIR_B in dirs, f'a direction was dropped: {dirs}'
    ts = [it['created_at'] for it in body['items']]
    assert ts == sorted(ts, reverse=True), f'not newest-first: {ts}'


def test_empty_store_lists_nothing_without_erroring(client):
    status, body = _get_json(client, '/api/v4/research/list')
    assert status == 200 and body.get('items') == [], (
        'an empty store must be an honest empty list, not an error')


def test_list_never_leaks_a_real_papers_report(client, fresh_db):
    """paper_reports also holds per-paper reports. The list must return only
    research rows, keyed by their composite lang prefix.

    The decoy row deliberately carries a WELL-FORMED meta with a `direction`
    field, so the only thing that can exclude it is the lang-prefix filter.
    (An empty-meta decoy would be dropped by the "no recorded text" guard
    instead, letting a `WHERE 1=1` regression pass unnoticed — measured: that
    exact NEUTER did not bite until this fixture was tightened.)
    """
    from lib.research.persistence import persist_survey
    fresh_db.client.command('paper.report.upsert', {
        'user_id': TEST_OWNER_USER_ID,
        'paper_hash': 'deadbeef' * 4, 'lang': 'en',
        'report': 'A REAL PAPER REPORT', 'model': '',
        'meta': {'kind': 'insight',
                 'direction': 'NOT A RESEARCH DIRECTION'},
        'created_at': 9999999999,
    }, 'research-discovery-paper-decoy')
    persist_survey(
        _DIR_A, 'en', '# A', _GAPS, model='m',
        user_id=TEST_OWNER_USER_ID)
    _empty_the_task_registry()

    _, body = _get_json(client, '/api/v4/research/list')
    dirs = [it.get('direction') for it in (body.get('items') or [])]
    assert dirs == [_DIR_A], f'a non-research row leaked into the list: {dirs}'
