"""Startup diagnostics preserve failures and never admit identity labels."""
import pytest

from lib import observability

pytestmark = pytest.mark.unit


def test_startup_stage_metrics_are_closed_and_record_failure(monkeypatch):
    samples = []
    monkeypatch.setattr(
        observability._STORE, 'observe',
        lambda name, duration, buckets, **labels: samples.append((name, duration, labels)),
    )
    with observability.measure_task_startup('registration'):
        pass
    with pytest.raises(RuntimeError, match='original startup failure'):
        with observability.measure_task_startup('private-conversation-id'):
            raise RuntimeError('original startup failure')
    assert [labels for _, _, labels in samples] == [
        {'stage': 'registration', 'outcome': 'success'},
        {'stage': 'other', 'outcome': 'error'},
    ]
    assert all(duration >= 0 for _, duration, _ in samples)


def test_failed_diagnostic_observer_cannot_break_worker_entry(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError('diagnostic observer unavailable')
    monkeypatch.setattr(observability._STORE, 'observe', unavailable)
    with observability.measure_task_startup('worker_entry'):
        pass
    with pytest.raises(ValueError, match='worker rejected'):
        with observability.measure_task_startup('worker_entry'):
            raise ValueError('worker rejected')


@pytest.mark.parametrize('stage', sorted(
    stage for stage in observability._TASK_STARTUP_STAGES
    if stage.startswith('workspace_')))
def test_workspace_stages_emit_paired_content_free_anchors(monkeypatch, stage):
    import json
    records, samples = [], []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(json.loads(payload)))
    monkeypatch.setattr(observability._STORE, 'observe',
                        lambda name, duration, buckets, **labels: samples.append(labels))
    with observability.measure_task_startup(stage, identity={
            'task_id': 'owned-task', 'user_id': 7, 'secret': 'must-not-log'}):
        assert records[0]['boundary'] == 'start'
    assert [record['boundary'] for record in records] == ['start', 'complete']
    assert all(record['stage'] == stage and record['task_id'] == 'owned-task'
               and 'secret' not in record for record in records)
    assert records[1]['durationMs'] >= 0
    assert samples == [{'stage': stage, 'outcome': 'success'}]


@pytest.mark.parametrize(('stage', 'details'), [
    ('context_read', {'turns': 12}),
    ('context_rebuild', {'replay': 'checkpoint_missing'}),
    ('startup_context_compose', {'slowestProvider': 'skills', 'slowestProviderMs': 812.5}),
    ('startup_context_guidance', {}),
    ('preparation_account', {}),
    ('preparation_abort_fence', {}),
    ('preparation_native', {}),
])
def test_child_stages_join_their_parent_with_closed_details(monkeypatch, stage, details):
    import json
    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(json.loads(payload)))
    with observability.measure_task_startup('context', identity={'attempt_id': 'a-1'}):
        with observability.measure_task_startup(
                stage, details={**details, 'prompt': 'must-not-log'}):
            pass
    child = [record for record in records if record['stage'] == stage]
    assert [record['boundary'] for record in child] == ['start', 'complete']
    assert all(record['attempt_id'] == 'a-1' for record in child)
    completion = child[1]
    assert {key: completion[key] for key in details} == details
    assert 'prompt' not in completion


def test_paired_anchors_survive_the_app_log_budget(caplog):
    import logging
    from lib.logging_pkg.log_signals import LogBudgetFilter
    budget = LogBudgetFilter(per_minute=1)
    caplog.set_level(logging.INFO, logger=observability.logger.name)
    for _ in range(3):
        with observability.measure_task_startup('context_read', identity={'task_id': 't'}):
            pass
        with observability.measure_task_completion('persistence', identity={'task_id': 't'}):
            pass
    anchors = [record for record in caplog.records if record.name == observability.logger.name]
    assert len(anchors) == 12
    assert all(budget.filter(record) for record in anchors)
    # Ordinary INFO lines of the same module keep the flood insurance, and the
    # anchors consumed none of their budget.
    observability.logger.info('routine line one')
    observability.logger.info('routine line two')
    routine = [record for record in caplog.records if record.msg.startswith('routine')]
    assert [budget.filter(record) for record in routine] == [True, False]


def test_nested_startup_identity_is_scoped_and_restored(monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(json.loads(payload)))
    def measure(stage, identity=None):
        with observability.measure_task_startup(stage, identity=identity):
            pass
    with observability.measure_task_startup('workspace_binding', identity={'task_id': 'outer'}):
        measure('workspace_index_seed')
        with pytest.raises(ValueError):
            with observability.measure_task_startup('workspace_snapshot', identity={'task_id': 'inner'}):
                raise ValueError('failure')
        measure('workspace_index_add')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(measure, 'workspace_index_tree').result()
    measure('workspace_index_tree')
    starts = [r for r in records if r['boundary'] == 'start']
    assert [(r['stage'], r['task_id']) for r in starts] == [
        ('workspace_binding', 'outer'), ('workspace_index_seed', 'outer'),
        ('workspace_snapshot', 'inner'), ('workspace_index_add', 'outer')]
    assert observability._STARTUP_IDENTITY.get() is None


def test_claim_stages_publish_once_under_the_identity_named_after_them(monkeypatch):
    """A claim's stages precede its Attempt; each publishes one pair on its own clock."""
    import json
    records, samples = [], []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(json.loads(payload)))
    monkeypatch.setattr(observability._STORE, 'observe',
                        lambda name, duration, buckets, **labels: samples.append((name, labels)))
    boundaries = iter([(1_000_000, 10.0), (1_000_250, 10.25),    # wake
                       (1_001_000, 11.0), (1_001_500, 11.5),     # claim, ACK lost
                       (1_002_000, 12.0), (1_002_125, 12.125)])  # same claim, retried
    monkeypatch.setattr(observability.TaskStartupIntervals, 'instant',
                        staticmethod(lambda: next(boundaries)))
    intervals = observability.TaskStartupIntervals()
    intervals.add('claim_wake', intervals.instant())
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        with intervals.measure('claim_next'):
            raise RuntimeError('lost acknowledgement')
    with intervals.measure('claim_next'):
        pass
    assert records == [] and samples == []  # No identity exists before the claim.
    intervals.publish({'attempt_id': 'attempt-1', 'config': 'must-not-log'})
    intervals.publish({'attempt_id': 'attempt-1'})
    assert [(r['stage'], r['boundary'], r['observedAtMs'], r.get('durationMs'), r.get('outcome'))
            for r in records] == [
        ('claim_wake', 'start', 1_000_000, None, None),
        ('claim_wake', 'complete', 1_000_250, 250.0, 'success'),
        ('claim_next', 'start', 1_002_000, None, None),
        ('claim_next', 'complete', 1_002_125, 125.0, 'success')]
    assert all(r['attempt_id'] == 'attempt-1' and 'config' not in r for r in records)
    assert samples == [
        ('tofu_task_startup_stage_seconds', {'stage': 'claim_wake', 'outcome': 'success'}),
        ('tofu_task_startup_stage_seconds', {'stage': 'claim_next', 'outcome': 'success'})]


def test_unpublished_or_unidentified_claim_stages_stay_silent(monkeypatch):
    records, samples = [], []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(payload))
    monkeypatch.setattr(observability._STORE, 'observe',
                        lambda name, duration, buckets, **labels: samples.append(labels))
    intervals = observability.TaskStartupIntervals()
    with intervals.measure('claim_horizon'):
        pass
    intervals.publish({'attempt_id': '  ', 'user_id': None})
    intervals.publish({'attempt_id': 'attempt-1'})  # The blank publish consumed them.
    observability.TaskStartupIntervals().publish({'attempt_id': 'attempt-1'})
    assert records == [] and samples == []


def test_published_claim_anchors_survive_the_app_log_budget(caplog):
    import logging
    from lib.logging_pkg.log_signals import LogBudgetFilter
    budget = LogBudgetFilter(per_minute=1)
    caplog.set_level(logging.INFO, logger=observability.logger.name)
    intervals = observability.TaskStartupIntervals()
    for stage in ('claim_wake', 'claim_horizon', 'claim_next', 'claim_proof'):
        with intervals.measure(stage):
            pass
    intervals.publish({'attempt_id': 'attempt-1'})
    anchors = [record for record in caplog.records if record.name == observability.logger.name]
    assert len(anchors) == 8
    assert all(budget.filter(record) for record in anchors)
