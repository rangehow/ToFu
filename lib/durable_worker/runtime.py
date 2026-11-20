"""One application-owned service for all qualified durable invocation kinds.

Entry points: create_service, ensure_workers_started, stop_workers. Composition
is lazy; one authority wake source serves chat and automation kinds. Domain
registrations retain effects and accounting, and shutdown joins before storage.
"""
import threading
import time
import uuid

from lib.identity import PrincipalContext
from .runtime_policy import runtime_policy

_composition_lock = threading.Lock()


def _storage(*, write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


# The execution host installs its process's claim gate here before boot
# (serverctl_pkg/execution_shards.py); other compositions claim ungated.
CLAIM_GATE_EXTENSION = 'tofu_execution_claim_gate'


def create_service(*, client_factory=_storage, registry=None, max_workers=None,
                   now_ms=None, environ=None, application=None):
    from runtime_guards import UNLIMITED, resolve_resource_budget
    from lib.automations.worker_registration import registrations
    from lib.conversation_sync.worker_execution import conversation_registration
    from lib.durable_worker.service import DurableWorkerService
    from lib.storage.worker_wake import StorageWorkerWakeSource
    policy = runtime_policy()
    if max_workers is None:
        max_workers = resolve_resource_budget(policy['capacityResource'], environ, maximum=UNLIMITED)
    if now_ms is None:
        now_ms = lambda: time.time_ns() // 1_000_000
    kinds = (*registrations(client_factory=client_factory, registry=registry,
                            now_ms=now_ms, policy=policy),
             conversation_registration(application=application))
    control = PrincipalContext.system(subject_id='durable-worker:' + uuid.uuid4().hex,
                                      scopes={policy['controlScope']})
    gate = (getattr(application, 'extensions', None) or {}).get(CLAIM_GATE_EXTENSION)
    service = DurableWorkerService(principal=control, client_factory=client_factory,
        registrations=kinds, max_workers=max_workers, now_ms=now_ms, policy=policy,
        wake_source=StorageWorkerWakeSource(client_factory, policy), claim_gate=gate)
    if gate is not None:
        gate.bind(service.wake)
    return service


def ensure_workers_started(app, *, environ=None):
    key = runtime_policy()['extensionKey']
    with _composition_lock:
        shutdown = app.extensions.get('tofu_shutdown_requested')
        if shutdown is not None and shutdown.is_set():
            raise RuntimeError('Cannot start durable workers during shutdown')
        service = app.extensions.get(key)
        if service is None or service.snapshot()['state'] in {'stopped', 'failed'}:
            service = create_service(environ=environ, application=app)
            app.extensions[key] = service
    # Stop must be able to interrupt enrollment while boot awaits its ACK.
    # If stop wins before start enters, the service is already stopped.
    return service.start()


def stop_workers(app):
    with _composition_lock:
        service = app.extensions.get(runtime_policy()['extensionKey'])
    if service is not None:
        service.stop()
