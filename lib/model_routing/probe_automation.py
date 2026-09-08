"""Daily matrix maintenance: shared probe lane, fresh evidence, one owner CAS.

Entry point run_daily_probe is invoked by the durable scheduler. No HTTP or
ambient owner dependency; incomplete/conflicting runs never change grants.
"""
from concurrent.futures import TimeoutError
import copy
import time

from lib.identity import PrincipalContext
from lib import provider_probe as probe
from lib.provider_probe_policy import PROVIDER_PROBE_MAX_CELLS
from .repository import ModelRoutingRepository, OwnerBoundary
from .probe_plan import provider_bundle, matrix_probe_plan

DAILY_PROBE_MAX_RUNTIME = 1800


def apply_recommendations(document, bundle, snapshot):
    """Remove exact model grants; never enable cells or infer stale evidence."""
    if (snapshot.get('status') != 'done'
            or snapshot.get('done_count') != snapshot.get('total')
            or not snapshot.get('total')):
        raise ValueError('incomplete provider probe')
    models = {}
    for row in bundle['offerings']:
        if row.get('identity_state') == 'confirmed' and row.get('model'):
            ref = row['model']
            models.setdefault(ref['model_id'], set()).add(
                (ref['creator_id'], ref['model_id']))
    credentials = {r['credential_id']: r for r in document['credentials']}
    removed = 0
    for cell in snapshot['cells'].values():
        # deployment_enabled=False marks a discovery probe of a not-yet-enabled
        # request name — never evidence against the offering's grant.
        if not cell.get('recommend_disable') or cell.get('deployment_enabled') is False:
            continue
        refs = models.get(cell.get('root_model_id'), set())
        # Canonical names without Creator are ambiguous; fail closed.
        if len(refs) != 1:
            continue
        index = cell.get('key_idx')
        if type(index) is not int or not 0 <= index < len(bundle['credentials']):
            raise ValueError('invalid probe credential index')
        credential = credentials[bundle['credentials'][index]['credential_id']]
        grants = credential['authorization']['models']
        kept = [ref for ref in grants
                if (ref['creator_id'], ref['model_id']) not in refs]
        removed += len(grants) - len(kept)
        credential['authorization']['models'] = kept
    return removed


def run_daily_probe(*, principal, max_runtime=DAILY_PROBE_MAX_RUNTIME,
                    cancel_event=None, repository=None):
    if not isinstance(principal, PrincipalContext) or 'providers' not in principal.scopes:
        raise PermissionError('provider maintenance scope required')
    boundary = OwnerBoundary.create(principal.require_owner(context='daily provider probe'))
    repository = repository or ModelRoutingRepository()
    deadline = time.monotonic() + min(DAILY_PROBE_MAX_RUNTIME, max(1, max_runtime))
    authority = repository.get(boundary)
    document = copy.deepcopy(authority.document)
    removed = completed = total_cells = 0
    for provider in document['providers']:
        if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
            raise TimeoutError('daily probe cancelled or timed out')
        bundle = provider_bundle(document, provider['provider_id'])
        if not bundle['provider_access'] or not bundle['provider_access'].get('enabled', True):
            continue
        if not bundle['credentials'] or not bundle['offerings']:
            continue
        plan = matrix_probe_plan(repository, boundary, bundle)
        work = probe.build_probe_work(plan['provider'], plan['models'], plan['api_keys'],
                                      maximum_cells=PROVIDER_PROBE_MAX_CELLS + 1)
        total_cells += len(work)
        if not work:
            continue
        if total_cells > PROVIDER_PROBE_MAX_CELLS:
            raise ValueError('daily probe cell budget exceeded')
        key = f"u{boundary.owner_user_id}_{provider['provider_id']}"
        task = dict(provider_id=key, status='running', started_at=time.time(),
                    finished_at=None, total=len(work), done_count=0, cells={},
                    summary={'ok': 0, 'disable': 0}, error=None, attempts=3,
                    _abort=False, _base_url=plan['provider']['base_url'],
                    _protocol=plan['provider']['protocol'], _extra_headers=plan['extra_headers'],
                    _oauth=plan['oauth'], _adapter=plan['adapter'],
                    _disabled_cells=set(plan.get('disabled_cells') or ()),
                    _owner_user_id=boundary.owner_user_id)
        with probe.CELL_PROBE_LOCK:
            existing = probe.CELL_PROBE_TASKS.get(key)
            if existing and existing.get('status') == 'running':
                raise RuntimeError('provider probe already running')
            probe.CELL_PROBE_TASKS[key] = task
            try:
                future = probe.submit_provider_probe_task(task, work, 30)
            except Exception:
                probe.CELL_PROBE_TASKS.pop(key, None)
                raise
        work.clear()
        plan.clear()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (cancel_event and cancel_event.is_set()):
                    raise TimeoutError('daily probe cancelled or timed out')
                try:
                    future.result(timeout=min(1, remaining))
                    break
                except TimeoutError:
                    if future.done():
                        raise
        except BaseException:
            task['_abort'] = True
            raise
        removed += apply_recommendations(document, bundle, probe.public_probe_snapshot(task))
        completed += 1
    if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
        raise TimeoutError('daily probe cancelled or timed out')
    if removed:
        repository.compare_and_swap(boundary, document, expected_revision=authority.revision)
    return {'ok': True, 'providers': completed, 'cells': total_cells, 'disabled_grants': removed}
