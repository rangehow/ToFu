"""Local owned records and accepted Stop intent cannot depend on provider import."""
import pytest
import test_jobs
from tofu_environments.jobs import JobService

context = test_jobs.context
pytestmark = pytest.mark.unit


def offline_service(context):
    p, repo, _, actor, spec, plan = context
    resolved = []

    def unavailable(*_):
        resolved.append(True)
        raise RuntimeError('provider unavailable')
    return JobService(repo, unavailable), resolved


def test_inspection_and_planned_stop_are_provider_independent(context):
    p, repo, _, actor, spec, plan = context
    service, resolved = offline_service(context)
    assert service.inspect(actor, plan['operation_id'])['phase'] == 'planned'
    assert service.cancel(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)['phase'] == 'cancelled'
    assert service.status(actor, plan['operation_id'])['phase'] == 'cancelled'
    assert not resolved and p.calls == 0


def test_stop_intent_survives_unavailable_provider(context):
    p, repo, original, actor, spec, plan = context
    original.submit(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    service, resolved = offline_service(context)
    with pytest.raises(RuntimeError, match='provider unavailable'):
        service.cancel(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    record = repo.get(actor, plan['operation_id'])['value']
    assert record['cancellation'] == {'plan_hash': plan['plan_hash'], 'state': 'requested'}
    assert record['phase'] == 'queued'
    assert service.inspect(actor, plan['operation_id'])['cancellation']['state'] == 'requested'
    p.cancel_job = lambda *_: None
    assert original.progress_cancellation(actor, plan['operation_id'])['phase'] == 'cancelling'


def test_terminal_or_already_claimed_reads_need_no_provider(context):
    p, repo, original, actor, spec, plan = context
    original.submit(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    original.status(actor, plan['operation_id'])
    service, resolved = offline_service(context)
    assert service.submit(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)['phase'] == 'succeeded'
    assert service.status(actor, plan['operation_id'])['phase'] == 'succeeded'
    assert service.cancel(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)['phase'] == 'succeeded'
    assert not resolved


def test_known_receipt_settlement_does_not_resolve_provider_again(context):
    p, repo, service, actor, spec, plan = context
    submit = p.submit_job
    def unavailable(*_):
        raise RuntimeError('provider removed')
    def completed(*args):
        reference = submit(*args)
        service.resolve_provider = unavailable
        return reference
    p.submit_job = completed
    result = service.submit(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    assert result['phase'] == 'queued' and result['reference'] == p.reference and p.calls == 1
    assert service.inspect(actor, plan['operation_id'])['reference'] == p.reference


def test_claimed_stop_never_resolves_or_replays_provider(context):
    p, repo, original, actor, spec, plan = context
    original.submit(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    stops = []
    p.cancel_job = lambda *_: stops.append(True)
    original.cancel(actor, plan['operation_id'], plan['plan_hash'], lambda *_: True)
    service, resolved = offline_service(context)
    assert service.progress_cancellation(actor, plan['operation_id'])['phase'] == 'cancelling'
    assert not resolved and len(stops) == 1
