"""Executable negotiation stop: no supported request-owned applicability port.

Candidate names below are probes, not admitted fields or a new contract. Only
synthetic delegation/Turn ports run, backed by disposable selected SQL storage.
"""
from copy import deepcopy
import uuid

import pytest

from lib.project_dispatch.repository import ProjectDispatchError
from lib.project_dispatch.segmented import SegmentedDispatch
from tests.project_mod.test_coordinator_continuation import run as run, delegate, end
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api, pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)]


@pytest.fixture(params=[1, 2], ids=['legacy', 'segmented'])
def durable_run(detached_semantic_authority, run, request):
    factory, _, _ = detached_semantic_authority
    r = run
    for (namespace, key), row in r.c.client.rows.items():
        factory(write=True).command('record.put', {'namespace': namespace,
            'key': key, 'expected_version': 0, 'value': row['value']}, uuid.uuid4().hex)
    r.c.repo.client = factory
    r.c.repo.storage_version = request.param
    return r


@pytest.mark.parametrize('candidate', ['required', 'not_applicable', None, '', 'unknown', True, {}])
def test_applicability_has_no_admitted_producer_or_request_port(durable_run, candidate):
    r = durable_run
    row = delegate(r)
    before = deepcopy(r.c.repo.list()), deepcopy(r.attempts), list(r.calls), list(r.scheduled), list(r.reports)
    head = SegmentedDispatch(r.c.repo).head()
    refused = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'unsupported',
                                               'delivery_applicability': candidate})
    assert 'error' in refused and 'Invalid dispatch delegateRequest' in str(refused)
    proposed = {**deepcopy(row['request']), 'deliveryApplicability': candidate}
    with pytest.raises(ProjectDispatchError, match='Invalid dispatch request'):
        r.c.repo.authorize_from_coordinator(proposed, task=r.task, command_id='unsupported-request')
    with pytest.raises(ProjectDispatchError, match='cannot change dispatch authority/association'):
        r.c.repo.mutate(row['id'], lambda value: value['request'].update(deliveryApplicability=candidate))
    assert r.c.repo.find_command('unsupported-request') is None
    assert SegmentedDispatch(r.c.repo).head() == head
    assert (r.c.repo.list(), r.attempts, r.calls, r.scheduled, r.reports) == before


@pytest.mark.parametrize('delivered', [False, True], ids=['no-receipt', 'fresh-receipt'])
def test_omitted_applicability_roundtrips_without_inventing_intent(durable_run, delivered):
    from lib.project_dispatch.continuation import worker_summaries

    r = durable_run
    row = delegate(r)
    end(r, row, delivered=delivered)
    reviewed = r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                      decision='accept', note='Synthetic current result reviewed.')
    assert reviewed['decision'] == 'accept'
    retained = r.c.repo.read(row['id'])
    assert retained['request'] == row['request']
    assert 'deliveryApplicability' not in retained['request']
    assert retained['review']['resultRevision'] == 1 and not retained.get('publication')
    before = deepcopy(r.c.repo.list()), deepcopy(r.attempts), list(r.calls), list(r.scheduled), list(r.reports)
    head = SegmentedDispatch(r.c.repo).head()
    first = worker_summaries(r.c.repo, [retained])[0]
    second = r.service.view_for_coordinator(row['id'], scope={
        'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1})
    assert first['delivery'] == second['delivery']
    assert (first['delivery'] is not None) == delivered
    if delivered:
        assert first['delivery']['status'] == 'delivered'
    assert r.c.repo.read(row['id']) == retained
    assert SegmentedDispatch(r.c.repo).head() == head
    assert (r.c.repo.list(), r.attempts, r.calls, r.scheduled, r.reports) == before


@pytest.mark.parametrize('durable_run', [2], indirect=True, ids=['segmented'])
@pytest.mark.parametrize('candidate', ['required', 'not_applicable'])
def test_segmented_envelope_retains_json_but_has_no_applicability_ownership(durable_run, candidate):
    from lib.project_dispatch.segmented import key

    r = durable_run
    row = delegate(r)
    store = SegmentedDispatch(r.c.repo)
    envelope = store.query('get', collection='assignments', id=key(row['id']))
    assert 'deliveryApplicability' not in envelope['value']
    original_request = deepcopy(row['request'])
    original_head = store.head()
    original_effects = list(r.calls), list(r.scheduled), list(r.reports)

    # Deliberate storage-level probe, not an admitted producer: the storage
    # contract accepts objects and delegates domain meaning to the repository.
    store.document_command([{'collection': 'assignments', 'id': key(row['id']),
        'expected_version': envelope['version'],
        'value': {**envelope['value'], 'deliveryApplicability': candidate}}])
    assert r.c.repo.read(row['id']) == row
    end(r, row, delivered=False)
    reviewed = r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                      decision='accept', note='Synthetic envelope retention probe.')
    assert reviewed['decision'] == 'accept'
    retained = store.query('get', collection='assignments', id=key(row['id']))
    assert retained['version'] > envelope['version']
    assert retained['value']['deliveryApplicability'] == candidate
    assert retained['value']['record']['request'] == original_request
    domain = r.c.repo.read(row['id'])
    assert domain['request'] == original_request and 'deliveryApplicability' not in domain

    # Unlike the immutable request, this annotation can be replaced after review,
    # even with an invalid value, without domain validation noticing the change.
    store.document_command([{'collection': 'assignments', 'id': key(row['id']),
        'expected_version': retained['version'],
        'value': {**retained['value'], 'deliveryApplicability': {'invalid': True}}}])
    replaced = store.query('get', collection='assignments', id=key(row['id']))
    assert replaced['value']['deliveryApplicability'] == {'invalid': True}
    assert r.c.repo.read(row['id']) == domain
    assert store.head() == original_head
    assert (list(r.calls), list(r.scheduled), list(r.reports)) == original_effects
