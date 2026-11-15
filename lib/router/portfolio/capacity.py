"""Measured resource admission of portfolio starts (docs/ROUTER.md R10, portfolio).

A ranked candidate starts only when the resources its start needs are available
now, read from their own authorities. Unavailability is recorded with what was
observed; nothing here estimates, waits or sizes work:

- model provider: the model the target Project Router would run, resolved as
  its scheduled turn resolves it (``scheduled_turn_model``), needs a route the
  dispatcher could pick now (``LLMDispatcher.route_admission``, read in the
  process that runs scheduled work and Project Router turns). When every
  route is held, the deferral records each hold, and ``retryAt`` is the reset
  the provider itself named when every hold carries one. Our own steering or
  backoff timing is never a retry time: such a deferral is re-checked by the
  next review.
- GPU: a validation that needs GPU hours names its queue and GPU count; the
  queue's real state, read through the registered read-only queue tool
  (``lib/response_surfaces/mcp.py``), must show that many GPUs available. A
  queue that cannot be read is recorded as such, never assumed free.

Host CPU, memory or thread counts never admit, size or refuse a start
(AGENTS.md principle 3); a start the operating system cannot launch is reported
by the dispatch itself and recorded by the review.

Entry points: resource_deferral(owner, opportunity, *, ports=None), CapacityPorts,
production_ports. Dependencies: conversations.project_coordination,
scheduler.conversation_dispatch.scheduled_turn_model, llm_dispatch,
response_surfaces.mcp.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from lib.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CapacityPorts:
    """Where the review reads capacity; specs replace them with recorded states."""

    model_route: Callable[[int, str], dict]
    queue_rows: Callable[[int, dict], list[dict]]
    now_ms: Callable[[], int]


def project_router_model(owner: int, project: str) -> str:
    """The model a forward to ``project``'s Project Router would run on."""
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.scheduler.conversation_dispatch import scheduled_turn_model
    binding = ProjectCoordinationRepository(owner).read(project)
    return scheduled_turn_model(binding['conversationId'] if binding['availability'] == 'available' else None,
                                user_id=owner)


def _model_route(owner: int, project: str) -> dict:
    from lib.llm_dispatch import get_dispatcher
    model = project_router_model(owner, project)
    return {'model': model, **get_dispatcher().route_admission(model)}


def _queue_rows(owner: int, gpu_queue: dict) -> list[dict]:
    from lib.response_surfaces.mcp import read_catalog
    catalog = read_catalog(gpu_queue['connection'], 'queue', user_id=owner, cancelled=lambda: False,
                           queue=gpu_queue['queue'])
    return catalog['queues'][0]['resources']


def production_ports() -> CapacityPorts:
    return CapacityPorts(model_route=_model_route, queue_rows=_queue_rows,
                         now_ms=lambda: int(time.time() * 1000))


def _deferral(reason: str, detail: str, *, observed_at: int, retry_at: int | None, observed: dict) -> dict:
    return {'reason': reason, 'detail': detail[:2000], 'observedAt': observed_at, 'retryAt': retry_at,
            'observed': observed}


def _provider_deferral(route: dict, now: int) -> dict | None:
    if route['admissible']:
        return None
    holds = [{'key': hold['key'], 'model': hold['model'], 'reason': hold['reason'],
              'untilMs': int(hold['until'] * 1000), 'providerNamed': bool(hold['providerNamed'])}
             for hold in route['holds']]
    if not holds:
        return _deferral('model_route_unavailable',
                         f'No admissible route serves {route["model"]} (missing route, billing stop or exclusion).',
                         observed_at=now, retry_at=None, observed={'model': route['model'], 'holds': []})
    named = all(hold['providerNamed'] for hold in holds)
    retry_at = min(hold['untilMs'] for hold in holds) if named else None
    causes = ', '.join(sorted({hold['reason'] for hold in holds}))
    return _deferral('provider_rate_limited',
                     f'Every route of {route["model"]} is held by its provider ({causes}).'
                     + (' The provider named its reset.' if named else ' No provider reset was named; the '
                                                                     'next review checks again.'),
                     observed_at=now, retry_at=retry_at, observed={'model': route['model'], 'holds': holds})


def _gpu_deferral(owner: int, opportunity: dict, ports: CapacityPorts, now: int) -> dict | None:
    validation = opportunity['validation']
    if validation['cost']['gpuHours'] <= 0:
        return None
    queue = validation['gpuQueue']
    try:
        rows = ports.queue_rows(owner, queue)
    except Exception as error:  # every unread state is unknown capacity, recorded with its cause
        logger.info('[Portfolio] GPU queue %s via %s unreadable; the start waits: %s',
                    queue['queue'], queue['connection'], error)
        return _deferral('gpu_queue_unreadable', f'{queue["queue"]} via {queue["connection"]}: '
                                                 f'{type(error).__name__}: {error}',
                         observed_at=now, retry_at=None, observed={'queue': queue['queue']})
    gpus = [row for row in rows if row.get('kind') == 'gpu'
            and (queue['gpuType'] is None or row.get('name') == queue['gpuType'])]
    observed = {'queue': queue['queue'], 'gpuCount': queue['gpuCount'],
                'rows': [{key: row.get(key) for key in ('name', 'total', 'allocated', 'available', 'pending')}
                         for row in gpus]}
    if any(isinstance(row.get('available'), (int, float)) and row['available'] >= queue['gpuCount']
           for row in gpus):
        return None
    return _deferral('gpu_queue_full', f'{queue["queue"]} has fewer than {queue["gpuCount"]} available '
                                       f'{queue["gpuType"] or "GPU"}(s) now.',
                     observed_at=now, retry_at=None, observed=observed)


def resource_deferral(owner: int, opportunity: dict, *, ports: CapacityPorts | None = None) -> tuple[dict | None, dict]:
    """Return (deferral or None, measured resources) for one start.

    The measured resources are what an admitted start records as its reason
    to start; a deferral is what the review records instead.
    """
    ports = ports or production_ports()
    now = ports.now_ms()
    route = ports.model_route(owner, opportunity['projectPath'])
    deferral = _provider_deferral(route, now)
    if deferral is not None:
        return deferral, {}
    deferral = _gpu_deferral(owner, opportunity, ports, now)
    if deferral is not None:
        return deferral, {}
    measured = {'model': route['model'], 'observedAt': now}
    if opportunity['validation']['gpuQueue'] is not None:
        measured['gpuQueue'] = opportunity['validation']['gpuQueue']['queue']
    return None, measured


__all__ = ['CapacityPorts', 'production_ports', 'project_router_model', 'resource_deferral']
