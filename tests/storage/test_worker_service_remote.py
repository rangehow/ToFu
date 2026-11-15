"""A cold execution service receives work/control solely from its authority."""
import threading
import time
import uuid

import pytest

from lib.durable_worker import StorageWorkerJobStore, WorkerJobOutcome, WorkerKindRegistration, WorkerKindSafety
from lib.durable_worker.service import DurableWorkerService
from lib.identity import PrincipalContext
from lib.storage.worker_wake import StorageWorkerWakeSource

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_empty_service_wakes_and_routes_durable_stop(semantic_authority):
    from lib.automations.runtime_policy import runtime_policy
    factory, owner, _ = semantic_authority
    policy = runtime_policy()
    empty = threading.Event()
    entered = threading.Event()
    observed_cancel = threading.Event()
    completed = threading.Event()

    class Client:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def query(self, operation, *args, **kwargs):
            value = self.client.query(operation, *args, **kwargs)
            if operation == 'worker_job.claim_horizon':
                empty.set()
            return value

    observed_factory = lambda **kwargs: Client(factory(**kwargs))
    source = StorageWorkerWakeSource(observed_factory, policy)

    def execute(context, payload):
        entered.set()
        assert context._cancel_requested.wait(5), 'cross-process Stop did not wake heartbeat'
        observed_cancel.set()
        return WorkerJobOutcome(status='cancelled', result_ref=context.claim.task_id)

    def settle(context, payload, outcome, now_ms):
        value = StorageWorkerJobStore(client_factory=factory).complete(
            context.claim, outcome=outcome, now_ms=now_ms)
        completed.set()
        return value

    registration = WorkerKindRegistration(task_kind='remote-wake-test',
        safety=WorkerKindSafety(True, True, True, True), handler=execute,
        cancel_handler=execute, completion_handler=settle)
    service = DurableWorkerService(
        principal=PrincipalContext.system(subject_id='test-control', scopes={policy['controlScope']}),
        client_factory=observed_factory, registrations=[registration], max_workers=1,
        now_ms=lambda: int(time.time() * 1000), policy=policy, wake_source=source)
    service.start()
    try:
        assert empty.wait(5)
        task = 'remote-' + uuid.uuid4().hex
        factory(write=True).command('worker_job.enqueue', {
            'task_id': task, 'user_id': owner, 'tenant_id': '', 'task_kind': registration.task_kind,
            'payload': {'contract': 'test/v1', 'principal': PrincipalContext.user(
                subject_id='test-owner', owner_user_id=owner).to_payload()},
            'idempotency_key': task, 'priority': 0, 'available_at_ms': 0,
            'now_ms': int(time.time() * 1000),
        }, uuid.uuid4().hex)
        assert entered.wait(5)
        factory(write=True).command('worker_job.request_cancel', {
            'task_id': task, 'user_id': owner, 'reason': 'Stop',
            'now_ms': int(time.time() * 1000),
        }, uuid.uuid4().hex)
        assert observed_cancel.wait(5)
        assert completed.wait(5)
    finally:
        assert service.stop()
    assert service.snapshot()['inflight'] == 0
    assert source.signals._active == {}
    assert factory().query('worker_job.get', {'task_id': task, 'user_id': owner})['status'] == 'cancelled'


def test_a_woken_claim_times_its_acquisition_from_the_commit_hint(semantic_authority, monkeypatch):
    """The enqueue's hint, horizon read, claim and fence proof are the claim's startup.

    They precede the job's identity, so the handler that owns it publishes them
    (docs/LOGGING.md); the hint's arrival is ``claim_wake``'s start.
    """
    import json
    from lib import observability
    from lib.automations.runtime_policy import runtime_policy
    factory, owner, _ = semantic_authority
    policy = runtime_policy()
    idle = threading.Event()
    completed = threading.Event()
    records = []
    published = []

    def info(fmt, *args, **kwargs):
        if fmt == '[TaskStartupTiming] %s':
            records.append(json.loads(args[0]))

    class Client:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def query(self, operation, *args, **kwargs):
            value = self.client.query(operation, *args, **kwargs)
            if operation == 'worker_job.claim_horizon':
                idle.set()
            return value

    def execute(context, payload):
        published.append(int(time.time() * 1000))
        context.startup.publish({'task_id': context.claim.task_id})
        return WorkerJobOutcome(status='succeeded', result_ref=context.claim.task_id)

    def settle(context, payload, outcome, now_ms):
        value = StorageWorkerJobStore(client_factory=factory).complete(
            context.claim, outcome=outcome, now_ms=now_ms)
        completed.set()
        return value

    monkeypatch.setattr(observability.logger, 'info', info)
    observed_factory = lambda **kwargs: Client(factory(**kwargs))
    registration = WorkerKindRegistration(task_kind='woken-claim-timing',
        safety=WorkerKindSafety(True, True, True, True), handler=execute,
        completion_handler=settle)
    service = DurableWorkerService(
        principal=PrincipalContext.system(subject_id='test-control', scopes={policy['controlScope']}),
        client_factory=observed_factory, registrations=[registration], max_workers=1,
        now_ms=lambda: int(time.time() * 1000), policy=policy,
        wake_source=StorageWorkerWakeSource(observed_factory, policy))
    service.start()
    try:
        assert idle.wait(5)
        task = 'woken-' + uuid.uuid4().hex
        factory(write=True).command('worker_job.enqueue', {
            'task_id': task, 'user_id': owner, 'tenant_id': '', 'task_kind': registration.task_kind,
            'payload': {'contract': 'test/v1', 'principal': PrincipalContext.user(
                subject_id='test-owner', owner_user_id=owner).to_payload()},
            'idempotency_key': task, 'priority': 0, 'available_at_ms': 0,
            'now_ms': int(time.time() * 1000),
        }, uuid.uuid4().hex)
        assert completed.wait(5)
    finally:
        assert service.stop()
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('claim_wake', 'start'), ('claim_wake', 'complete'),
        ('claim_horizon', 'start'), ('claim_horizon', 'complete'),
        ('claim_next', 'start'), ('claim_next', 'complete'),
        ('claim_proof', 'start'), ('claim_proof', 'complete')]
    assert {record['task_id'] for record in records} == {task}
    # The first probe found the queue empty, so only a hint can wake the claim;
    # every stage happened before the handler published it.
    starts = [record['observedAtMs'] for record in records if record['boundary'] == 'start']
    assert starts == sorted(starts) and starts[-1] <= published[0]
