"""Daily observation cannot revoke owner intent; verified success may heal access."""
import json
from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import Future

import pytest
from lib.identity import PrincipalContext
from lib.model_routing import probe_automation as service

pytestmark = pytest.mark.unit


def fixture():
    ref = {'creator_id': 'maker', 'model_id': 'model'}
    cred = {'credential_id': 'c', 'authorization': {'models': [ref]}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::model': {'key_idx': 0, 'root_model_id': 'model',
                     'status': 'rate_limited', 'recommend_disable': True}}}
    return doc, bundle, snap


@pytest.mark.parametrize("status", ["rate_limited", "unavailable", "error", "unauthorized", "not_found"])
def test_failed_observation_never_revokes_existing_authorization(status):
    doc, bundle, snap = fixture()
    snap['cells']['0::model']['status'] = status
    doc['credentials'].append({'credential_id': 'other', 'authorization': {'models': [{'creator_id': 'maker', 'model_id': 'model'}]}})
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before
    assert len(doc['credentials'][1]['authorization']['models']) == 1
    assert service.apply_recommendations(doc, bundle, snap) == 0


def test_failed_wire_does_not_create_a_persistent_exclusion():
    # A successful sibling does not turn transient failure into durable policy.
    ref = {'creator_id': 'maker', 'model_id': 'model'}
    cred = {'credential_id': 'c', 'authorization': {'models': [ref]}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 2, 'done_count': 2, 'cells': {
        '0::wire-a': {'key_idx': 0, 'model_id': 'wire-a', 'root_model_id': 'model',
                      'status': 'ok', 'recommend_disable': False},
        '0::wire-b': {'key_idx': 0, 'model_id': 'wire-b', 'root_model_id': 'model',
                      'status': 'rate_limited', 'recommend_disable': True}}}
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc['credentials'][0]['authorization']['models'] == [ref]


def test_apply_mints_grant_for_ok_cell_without_grant():
    # Credential grants a different model; a fresh ok on an ungranted wire
    # mints a grant reaching that wire.
    ref = {'creator_id': 'maker', 'model_id': 'model'}
    cred = {'credential_id': 'c', 'authorization': {'models': []}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::model': {'key_idx': 0, 'model_id': 'model', 'root_model_id': 'model',
                     'status': 'ok', 'recommend_disable': False}}}
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [ref]


def test_apply_lifts_exclusion_on_fresh_ok():
    ref = {'creator_id': 'maker', 'model_id': 'model',
           'excluded_wire_model_ids': ['wire-a', 'wire-b']}
    cred = {'credential_id': 'c', 'authorization': {'models': [deepcopy(ref)]}}
    bundle = {'provider_access': {'enabled': True}, 'credentials': [cred],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    doc = {'providers': [{'provider_id': 'p'}], 'credentials': [deepcopy(cred)]}
    snap = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::wire-a': {'key_idx': 0, 'model_id': 'wire-a', 'root_model_id': 'model',
                      'status': 'ok', 'recommend_disable': False}}}
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [
        {'creator_id': 'maker', 'model_id': 'model',
         'excluded_wire_model_ids': ['wire-b']}]


@pytest.mark.parametrize('provider_id, expected_changes', [('oauth_codex', 0), ('oauth_kimi', 1)])
@pytest.mark.parametrize('existing_exclusion', [False, True])
def test_apply_positive_probe_respects_managed_codex_access(provider_id, expected_changes, existing_exclusion):
    doc, bundle, snap = fixture()
    doc['provider_accesses'] = [{'provider_access_id': 'access-1', 'provider_id': provider_id}]
    doc['credentials'][0]['provider_access_id'] = 'access-1'
    ref = bundle['offerings'][0]['model']
    grants = [dict(ref, excluded_wire_model_ids=['model'])] if existing_exclusion else []
    doc['credentials'][0]['authorization']['models'] = grants
    snap['cells']['0::model'].update(status='ok', model_id='model')
    before = deepcopy(doc)

    assert service.apply_recommendations(doc, bundle, snap) == expected_changes
    if provider_id == 'oauth_codex':
        assert doc == before
    else:
        assert doc['credentials'][0]['authorization']['models'] == [ref]


def test_incomplete_probe_does_not_apply():
    doc, bundle, snap = fixture()
    snap['done_count'] = 0
    before = deepcopy(doc)
    with pytest.raises(ValueError):
        service.apply_recommendations(doc, bundle, snap)
    assert doc == before


@pytest.mark.parametrize('conflict', [False, True])
@pytest.mark.parametrize('observed_status', ['ok', 'rate_limited'])
def test_daily_run_uses_owner_and_revision(monkeypatch, conflict, observed_status):
    doc, bundle, snap = fixture()
    if observed_status == 'ok':
        doc['credentials'][0]['authorization']['models'] = []
        bundle['credentials'][0]['authorization']['models'] = []
    before = deepcopy(doc)
    snap['cells']['0::model'].update(status=observed_status, model_id='model')
    commits = []
    class Repository:
        def get(self, boundary):
            assert boundary.owner_user_id == 23
            return SimpleNamespace(document=doc, revision=9)
        def compare_and_swap(self, boundary, document, *, expected_revision):
            assert boundary.owner_user_id == 23
            assert expected_revision == 9
            if conflict:
                raise RuntimeError('revision changed')
            commits.append(document)
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: {
        'provider': {'base_url': 'https://example.test', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': {}})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [(0,)])
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', {})
    monkeypatch.setattr(service.probe, 'public_probe_snapshot', lambda task: task)
    def submit(task, work, timeout):
        task.update(deepcopy(snap))
        result = Future()
        result.set_result(None)
        return result
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task', submit)
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    if observed_status != 'ok':
        result = service.run_daily_probe(principal=principal, repository=Repository())
        assert result['grant_changes'] == 0
        assert commits == []
    elif conflict:
        with pytest.raises(RuntimeError, match='revision changed'):
            service.run_daily_probe(principal=principal, repository=Repository())
        assert not commits
    else:
        result = service.run_daily_probe(principal=principal, repository=Repository())
        assert result['grant_changes'] == 1
        assert commits[0]['credentials'][0]['authorization']['models'] == [{'creator_id': 'maker', 'model_id': 'model'}]
    assert doc == before


@pytest.mark.parametrize('confirm_pending', [False, True])
def test_daily_codex_probe_keeps_diagnostics_and_identity_but_not_grants(monkeypatch, tmp_path, confirm_pending):
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from tests.model_routing.test_model_routing_contract import _full_document

    doc = _full_document()
    doc['providers'][0]['provider_id'] = doc['provider_accesses'][0]['provider_id'] = 'oauth_codex'
    doc['provider_accesses'][1]['enabled'] = False
    doc['credentials'][0]['authorization']['models'] = [{
        'creator_id': 'creator', 'model_id': 'beta', 'excluded_wire_model_ids': ['beta-wire-a'],
    }]
    if confirm_pending:
        pending = dict(doc['offerings'][0], offering_id='alpha-pending',
                       identity_state='pending_identity', pending_model_id='alpha')
        pending.pop('model')
        doc['offerings'].append(pending)
        doc['deployments'].append(dict(doc['deployments'][0], deployment_id='alpha-pending-deployment',
                                       offering_id='alpha-pending', wire_model_id='alpha-extra',
                                       identity_confidence='pending', probe_status='unprobed'))
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(23)
    initial = repository.compare_and_swap(boundary, doc, expected_revision=0)
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', {})
    monkeypatch.setattr(service.probe, 'probe_cache_path', lambda pid: str(tmp_path / f'{pid}.json'))
    monkeypatch.setattr(service.probe, 'probe_cell_multi', lambda *a, **kw: ('ok', 'HTTP 200'))
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})

    result = service.run_daily_probe(principal=principal, repository=repository)

    wires = {'alpha-wire-a', 'beta-wire-a'} | ({'alpha-extra'} if confirm_pending else set())
    assert result == {'ok': True, 'providers': 1, 'cells': len(wires), 'grant_changes': 0}
    snapshot = json.loads((tmp_path / 'u23_oauth_codex.json').read_text())
    assert snapshot['status'] == 'done'
    assert {cell['model_id']: cell['status'] for cell in snapshot['cells'].values()} == dict.fromkeys(wires, 'ok')
    stored = repository.get(boundary)
    assert stored.revision == initial.revision + int(confirm_pending)
    assert stored.document['credentials'] == initial.document['credentials']
    if confirm_pending:
        pending = next(row for row in stored.document['offerings'] if row['offering_id'] == 'alpha-pending')
        assert pending['identity_state'] == 'confirmed'
        assert pending['model'] == {'creator_id': 'creator', 'model_id': 'alpha'}


def test_missing_scope_denied_before_storage():
    with pytest.raises(PermissionError):
        service.run_daily_probe(principal=PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'scheduler:run'}))


def test_daily_schedule_defaults_and_async_dispatch(monkeypatch):
    from lib.scheduler.manager import ScheduledTaskManager
    manager = ScheduledTaskManager()
    captured = {}
    monkeypatch.setattr(manager, '_ensure_default_task', lambda **kw: (captured.update(kw) or ({'id': 'daily'}, True)))
    manager._ensure_default_provider_probe_task()
    assert captured['schedule'] == '0 8 * * *'
    assert captured['enabled'] is True
    assert captured['reconcile_enabled'] is False
    assert captured['max_runtime'] == service.DAILY_PROBE_MAX_RUNTIME
    import threading
    runs = []
    monkeypatch.setattr(manager, '_run_and_record', lambda task: runs.append(threading.current_thread()))
    assert manager._dispatch_claimed_task({'id': 'daily', 'task_type': 'provider_probe_daily'}) is True
    assert manager.stop(timeout=5)
    assert len(runs) == 1 and runs[0] is not threading.current_thread()


def test_cancelled_run_never_starts_probe(monkeypatch):
    import threading
    doc, _, _ = fixture()
    stopped = threading.Event()
    stopped.set()
    repository = SimpleNamespace(get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: pytest.fail('must not plan'))
    with pytest.raises(TimeoutError):
        service.run_daily_probe(principal=principal, repository=repository, cancel_event=stopped)


def test_large_daily_matrix_is_submitted_without_count_refusal(monkeypatch):
    doc, bundle, snap = fixture()
    repository = SimpleNamespace(get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: {
        'provider': {'base_url': 'https://example.invalid', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': {}})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [()] * 5000)
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', {})
    submitted = []
    def submit(task, work, timeout):
        submitted.append(len(work))
        task.update(snap)
        result = Future(); result.set_result(None)
        return result
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task', submit)
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    service.run_daily_probe(principal=principal, repository=repository)
    assert submitted == [5000]


def test_daily_run_skips_provider_without_enabled_offerings(monkeypatch):
    from lib.model_routing.domain import ModelRoutingError
    doc, bundle, _ = fixture()
    repository = SimpleNamespace(
        get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)

    def _plan(*_a):
        raise ModelRoutingError(
            'provider access has no confirmed enabled offerings to probe',
            kind='provider_probe_no_offerings')

    monkeypatch.setattr(service, 'matrix_probe_plan', _plan)
    monkeypatch.setattr(
        service.probe, 'submit_provider_probe_task',
        lambda *_a, **_k: pytest.fail('must not submit'))
    principal = PrincipalContext.system(
        subject_id='test', owner_user_id=23, scopes={'providers'})
    result = service.run_daily_probe(
        principal=principal, repository=repository)
    assert result == {
        'ok': True, 'providers': 0, 'cells': 0, 'grant_changes': 0,
    }


@pytest.mark.parametrize('status', ['bad_request', 'invalid_response'])
def test_malformed_probe_does_not_revoke_model_grants(status):
    doc, bundle, snap = fixture()
    snap['cells']['0::model'].update(status=status,
        detail='HTTP 400 Unsupported parameter: _conversion_report')
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before


def test_neutral_sibling_prevents_removing_entire_model_grant():
    doc, bundle, snap = fixture()
    snap.update(total=2, done_count=2)
    snap['cells']['0::model']['model_id'] = 'wire-bad'
    snap['cells']['0::neutral'] = {
        'key_idx': 0, 'root_model_id': 'model', 'model_id': 'wire-neutral',
        'status': 'bad_request', 'recommend_disable': True}
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before



@pytest.mark.parametrize('reported_total', [1, 3])
def test_probe_cell_count_must_match_completed_count(reported_total):
    doc, bundle, snap = fixture()
    snap.update(total=reported_total, done_count=reported_total)
    snap['cells'] = {} if reported_total == 1 else snap['cells']
    before = deepcopy(doc)
    with pytest.raises(ValueError, match='incomplete provider probe'):
        service.apply_recommendations(doc, bundle, snap)
    assert doc == before


def test_legacy_disabled_sibling_cannot_disappear_from_removal_decision():
    doc, bundle, snap = fixture()
    snap.update(total=2, done_count=2)
    snap['cells']['0::model']['model_id'] = 'wire-bad'
    snap['cells']['0::legacy'] = {
        'key_idx': 0, 'root_model_id': 'model', 'model_id': 'wire-legacy',
        'status': 'not_found', 'recommend_disable': True,
        'deployment_enabled': False}
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before



def test_unprobed_connection_preserves_sibling_grant():
    doc, bundle, snap = fixture()
    bundle['offerings'][0]['offering_id'] = 'o'
    bundle['deployments'] = [
        {'offering_id': 'o', 'wire_model_id': 'bad-wire'},
        {'offering_id': 'o', 'wire_model_id': 'offline-wire'}]
    snap['cells']['0::model']['model_id'] = 'bad-wire'
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before


def test_new_positive_grant_excludes_failed_and_unprobed_sibling_wires():
    doc, bundle, snap = fixture()
    doc['credentials'][0]['authorization']['models'] = []
    bundle['credentials'][0]['authorization']['models'] = []
    bundle['offerings'][0]['offering_id'] = 'o'
    bundle['deployments'] = [
        {'offering_id': 'o', 'wire_model_id': wire}
        for wire in ('good', 'failed', 'unprobed')]
    snap.update(total=2, done_count=2)
    snap['cells'] = {
        'good': {'key_idx': 0, 'model_id': 'good', 'root_model_id': 'model', 'status': 'ok'},
        'failed': {'key_idx': 0, 'model_id': 'failed', 'root_model_id': 'model',
                   'status': 'unavailable', 'recommend_disable': True}}
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [{
        'creator_id': 'maker', 'model_id': 'model',
        'excluded_wire_model_ids': ['failed', 'unprobed']}]


def test_daily_probe_respects_explicit_revocation_history():
    doc, bundle, snap = fixture()
    doc['credentials'][0]['authorization']['models'] = []
    snap['cells']['0::model'].update(model_id='wire-a', status='ok')
    history = {'credential_id': 'c', 'creator_id': 'maker', 'model_id': 'model',
               'model_revoked': True, 'excluded_wire_model_ids': ['wire-a', 'absent']}
    doc['revocation_history'] = [history]
    before = deepcopy(doc)
    assert service.apply_recommendations(doc, bundle, snap) == 0
    assert doc == before

    # An unrelated identity must not suppress discovery.
    history['credential_id'] = 'other'
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == [
        {'creator_id': 'maker', 'model_id': 'model'}]


def test_daily_probe_preserves_historic_wires_on_mint_and_healing():
    doc, bundle, snap = fixture()
    doc['credentials'][0]['authorization']['models'] = []
    doc['revocation_history'] = [{
        'credential_id': 'c', 'creator_id': 'maker', 'model_id': 'model',
        'model_revoked': False, 'excluded_wire_model_ids': ['wire-a', 'absent']}]
    snap.update(total=2, done_count=2, cells={
        wire: {'key_idx': 0, 'model_id': wire, 'root_model_id': 'model', 'status': 'ok'}
        for wire in ('wire-a', 'wire-b')})
    assert service.apply_recommendations(doc, bundle, snap) == 1
    grant = doc['credentials'][0]['authorization']['models'][0]
    assert grant['excluded_wire_model_ids'] == ['absent', 'wire-a']
    assert service.apply_recommendations(doc, bundle, snap) == 0
    # An exclusion without a tombstone can still heal from positive evidence.
    grant['excluded_wire_model_ids'].append('wire-b')
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'][0]['excluded_wire_model_ids'] == ['absent', 'wire-a']


@pytest.mark.parametrize('tenant_id', [None, 'synthetic-tenant'])
def test_scheduled_daily_probe_preserves_full_owner_boundary(monkeypatch, tenant_id):
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from lib.scheduler.manager import ScheduledTaskManager
    from tests.model_routing.test_model_routing_contract import _full_document

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(23, tenant_id)
    document = _full_document()
    document['credentials'][0]['authorization']['models'] = []
    document['provider_accesses'][1]['enabled'] = False
    initial = repository.compare_and_swap(boundary, document, expected_revision=0)
    other_boundary = OwnerBoundary.create(23, 'other-tenant')
    other = repository.compare_and_swap(other_boundary, document, expected_revision=0)
    monkeypatch.setattr(service, 'ModelRoutingRepository', lambda: repository)
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', {})
    monkeypatch.setattr(service.probe, 'public_probe_snapshot', lambda task: task)
    def submit(task, work, timeout):
        task.update(status='done', done_count=len(work), cells={
            str(i): {'key_idx': 0, 'root_model_id': item[1],
                     'model_id': 'alpha-wire-a', 'status': 'ok'}
            for i, item in enumerate(work)})
        result = Future()
        result.set_result(None)
        return result
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task', submit)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda repo, owner, bundle: {
        'provider': {'base_url': 'https://example.test', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': {}})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [(0, 'alpha')])
    manager = ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='tenant-scheduler', owner_user_id=23, tenant_id=tenant_id,
        scopes={'scheduler:run'})

    ok, text = manager._execute_task({
        'id': 'synthetic-daily', 'user_id': 23, 'task_type': 'provider_probe_daily',
        'command': '', 'max_runtime': 30})

    assert ok, text
    assert json.loads(text)['grant_changes'] == 1
    stored = repository.get(boundary)
    assert stored.revision == initial.revision + 1
    assert stored.document['credentials'][0]['authorization']['models'] == [
        {'creator_id': 'creator', 'model_id': 'alpha'}]
    assert repository.get(other_boundary) == other


def test_withdrawal_during_commit_preparation_prevents_dispatch(monkeypatch):
    from threading import Event
    from lib.model_routing import repository, registration

    cancelled = Event()
    store = repository.ModelRoutingRepository()
    scope = repository.OwnerBoundary(23, None)
    document = repository.empty_document()
    monkeypatch.setattr(store, 'get', lambda _: SimpleNamespace(revision=0, document=document))
    prepare = registration.prepare_registration_document

    def prepare_and_withdraw(*args, **kwargs):
        result = prepare(*args, **kwargs)
        cancelled.set()
        return result

    monkeypatch.setattr(registration, 'prepare_registration_document', prepare_and_withdraw)
    monkeypatch.setattr(store, '_client', lambda **_: SimpleNamespace(
        command=lambda *_: pytest.fail('withdrawn publication reached storage')))
    with pytest.raises(TimeoutError, match='cancelled before dispatch'):
        store.compare_and_swap(scope, document, expected_revision=0, cancel_event=cancelled)


def test_entered_provider_cell_does_not_retry_after_abort(monkeypatch):
    calls = []

    def entered(*_args, **_kwargs):
        calls.append('entered')
        return 'unavailable', 'fake transient failure'

    monkeypatch.setattr(service.probe, 'probe_one_cell', entered)
    with pytest.raises(RuntimeError, match='cancelled before dispatch'):
        service.probe.probe_cell_multi('https://example.test', 'fake', 'model', {}, 30,
                                       retry_delay=0, cancelled=lambda: bool(calls))
    assert calls == ['entered']


def test_withdrawal_at_probe_lock_prevents_registration_and_submission(monkeypatch):
    from contextlib import contextmanager
    from threading import Event

    doc, bundle, _ = fixture()
    cancelled = Event()
    registered = {}
    repository = SimpleNamespace(get=lambda _: SimpleNamespace(document=doc, revision=1))

    @contextmanager
    def acquire():
        # Model the lock becoming available only after the primary saw EOF.
        assert not cancelled.is_set()
        cancelled.set()
        yield

    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: {
        'provider': {'base_url': 'https://example.test', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': {}})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [(0,)])
    monkeypatch.setattr(service.probe, 'CELL_PROBE_LOCK', acquire())
    monkeypatch.setattr(service.probe, 'CELL_PROBE_TASKS', registered)
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task',
                        lambda *_: pytest.fail('withdrawn probe reached submission'))
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    with pytest.raises(TimeoutError, match='cancelled before submission'):
        service.run_daily_probe(principal=principal, repository=repository, cancel_event=cancelled)
    assert registered == {}
