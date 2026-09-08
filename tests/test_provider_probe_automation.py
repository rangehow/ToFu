"""Daily detection preserves owner grants unless fresh complete evidence commits."""
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


def test_apply_removes_only_recommended_grants():
    doc, bundle, snap = fixture()
    doc['credentials'].append({'credential_id': 'other', 'authorization': {'models': [{'creator_id': 'maker', 'model_id': 'model'}]}})
    assert service.apply_recommendations(doc, bundle, snap) == 1
    assert doc['credentials'][0]['authorization']['models'] == []
    assert len(doc['credentials'][1]['authorization']['models']) == 1
    assert service.apply_recommendations(doc, bundle, snap) == 0


def test_incomplete_probe_does_not_apply():
    doc, bundle, snap = fixture()
    snap['done_count'] = 0
    before = deepcopy(doc)
    with pytest.raises(ValueError):
        service.apply_recommendations(doc, bundle, snap)
    assert doc == before


@pytest.mark.parametrize('conflict', [False, True])
def test_daily_run_uses_owner_and_revision(monkeypatch, conflict):
    doc, bundle, snap = fixture()
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
    if conflict:
        with pytest.raises(RuntimeError, match='revision changed'):
            service.run_daily_probe(principal=principal, repository=Repository())
        assert not commits
    else:
        result = service.run_daily_probe(principal=principal, repository=Repository())
        assert result['disabled_grants'] == 1
        assert commits[0]['credentials'][0]['authorization']['models'] == []
    assert doc['credentials'][0]['authorization']['models']


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
    called = []
    monkeypatch.setattr(manager, '_dispatch_maintenance_task', lambda task: called.append(task))
    manager._dispatch_claimed_task({'id': 'daily', 'task_type': 'provider_probe_daily'})
    assert len(called) == 1


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


def test_cell_budget_rejects_before_submission(monkeypatch):
    doc, bundle, _ = fixture()
    repository = SimpleNamespace(get=lambda boundary: SimpleNamespace(document=doc, revision=1))
    monkeypatch.setattr(service, 'provider_bundle', lambda *a: bundle)
    monkeypatch.setattr(service, 'matrix_probe_plan', lambda *a: {'provider': {}, 'models': [], 'api_keys': []})
    monkeypatch.setattr(service.probe, 'build_probe_work', lambda *a, **kw: [()] * (service.PROVIDER_PROBE_MAX_CELLS + 1))
    monkeypatch.setattr(service.probe, 'submit_provider_probe_task', lambda *a: pytest.fail('must not submit'))
    principal = PrincipalContext.system(subject_id='test', owner_user_id=23, scopes={'providers'})
    with pytest.raises(ValueError, match='budget exceeded'):
        service.run_daily_probe(principal=principal, repository=repository)
