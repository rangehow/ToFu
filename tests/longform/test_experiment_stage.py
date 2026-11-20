"""Offline experiment contracts: sealed JobService safety and reported statistics.

Providers here are in-memory fixtures, never local/remote process launchers.
Historical uncontrolled subprocess execution is deliberately not a contract.
"""
from dataclasses import replace
import hashlib
import json
import sys
from pathlib import Path
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.longform.documents import experiments as ex
from lib.longform.documents.compute_backends import Cell, JobsBackend, RESULT_NAME

pytestmark = pytest.mark.unit

# Optional SDK source is loaded directly for these hermetic integration tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/tofu-environments'))
from tofu_environments.contracts import Actor, EnvironmentError
from tofu_environments.jobs import JobService
from tofu_environments.repository import MemoryRepository


class FixtureRepository(MemoryRepository):
    durable = True


class Provider:
    def __init__(self):
        self.calls = 0
        self.stops = 0
        self.reference = None
        self.lost = False
        self.snapshot = {'state': 'running', 'exit_code': None}
        self.output = b'{"arm":"method","seed":0,"metrics":{"loss":0.1}}'

    def validate_job(self, actor, spec, *, operation_id):
        return {'source': spec['source']}

    def submit_job(self, actor, spec, operation_id, admission):
        self.calls += 1
        self.reference = {'kind': 'fixture', 'id': 'one'}
        if self.lost:
            raise TimeoutError('receipt lost')
        return self.reference

    def reconcile_job(self, actor, record):
        return self.reference

    def job_status(self, actor, reference, operation_id):
        return self.snapshot

    def cancel_job(self, *args):
        self.stops += 1

    def read_job_output(self, *args):
        yield self.output


@pytest.fixture
def context(tmp_path):
    provider = Provider()
    repo = FixtureRepository()
    actor = Actor(7, 'tenant', str(tmp_path))
    service = JobService(repo, lambda *_: provider)
    from lib.longform.documents.experiment_source import create_experiment_source
    code = b'pass\n'
    plan_data = {'hypothesis': 'fixture'}
    code_sha = hashlib.sha256(code).hexdigest()
    source = create_experiment_source(actor=actor, code=code, code_sha256=code_sha,
                                     plan=plan_data, provider='fixture', cell_name='method__s0',
                                     arm='method', seed=0, attempt=1)
    cell = Cell('method__s0', ('python', 'experiment.py', '--out', RESULT_NAME),
                tmp_path / 'results/method__s0.json', '', '', 'fixture', 'project',
                ex._digest(plan_data), code_sha, source['sha256'], 1,
                source['uri'], code, plan_data, 'method', 0)
    spec = {'version': 1, 'project': cell.project, 'command': list(cell.argv),
            'source': {'sha256': cell.source_sha256, 'uri': cell.source_uri},
            'resources': {'cpu': 1, 'memory_gib': 1, 'gpu_count': 0},
            'limits': {'runtime_seconds': 60, 'gpu_seconds': 0, 'result_bytes': 1024},
            'outputs': [RESULT_NAME], 'provider_options': {}}
    plan = service.plan(actor, 'fixture', spec, 'same-cell')
    cell = replace(cell, operation_id=plan['operation_id'], plan_hash=plan['plan_hash'])
    approved = lambda a, h: a == actor and h == plan['plan_hash']
    runner = JobsBackend(service, actor, approved, bindings={cell.name: cell}, cancel_authorize=approved)
    return provider, repo, service, actor, runner, cell


def test_expiry_before_submit_remains_terminal(context):
    p, r, s, a, runner, cell = context
    assert runner.run([cell], deadline=0)[cell.name]['state'] == 'cancelled'
    assert runner.run([cell])[cell.name]['state'] == 'cancelled'
    assert p.calls == 0


def test_cancel_terminal_no_resubmit(context):
    p, r, s, a, runner, cell = context
    runner.run([cell])
    runner.cancel([cell])
    p.snapshot = {'state': 'cancelled', 'exit_code': None}
    assert runner.run([cell])[cell.name]['state'] == 'cancelled'
    runner.run([cell])
    assert (p.calls, p.stops) == (1, 1)


def test_uncertain_submit_reconciles_without_replay(context):
    p, r, s, a, runner, cell = context
    p.lost = True
    assert runner.run([cell])[cell.name]['job_state'] == 'uncertain'
    runner.run([cell])
    runner.run([cell])
    assert p.calls == 1


def test_unknown_without_reference_stays_uncertain(context):
    p, r, s, a, runner, cell = context
    p.lost = True
    runner.run([cell])
    p.reference = None
    assert runner.run([cell])[cell.name]['job_state'] == 'uncertain'
    runner.cancel([cell])
    assert (p.calls, p.stops) == (1, 0)


def test_receipt_checkpoint_lost_does_not_replay(context, monkeypatch):
    p, r, s, a, runner, cell = context
    original = r.put

    def fail(actor, op, value, version):
        if value['phase'] == 'queued':
            raise RuntimeError('checkpoint lost')
        return original(actor, op, value, version)

    monkeypatch.setattr(r, 'put', fail)
    with pytest.raises(RuntimeError):
        runner.run([cell])
    monkeypatch.setattr(r, 'put', original)
    restarted = JobsBackend(JobService(r, lambda *_: p), a, runner.authorize,
                            bindings={cell.name: cell}, cancel_authorize=runner.cancel_authorize)
    restarted.run([cell])
    assert p.calls == 1


def test_concurrent_same_cell_claims_one_submit(context):
    p, r, s, a, runner, cell = context
    entered, release = Event(), Event()
    submit = p.submit_job

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return submit(*args)

    p.submit_job = slow
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(runner.run, [cell])
        assert entered.wait(5)
        # No reference yet, reconciliation cannot manufacture a second submit.
        assert runner.run([cell])[cell.name]['job_state'] == 'submitting'
        release.set()
        first.result()
    assert p.calls == 1


@pytest.mark.parametrize('field,value', [('plan_hash', 'x'), ('provider', 'other'),
    ('project', 'other'), ('scientific_plan_sha256', 'd'*64),
    ('audited_code_sha256', 'd'*64), ('source_sha256', 'd'*64), ('attempt', 2),
    ('argv', ('wrong',))])
def test_binding_mismatch_refuses_before_submit(context, field, value):
    p, r, s, a, runner, cell = context
    changed = replace(cell, **{field: value})
    runner.bindings[cell.name] = changed
    with pytest.raises(ValueError):
        runner.run([changed])
    assert p.calls == 0


def test_foreign_actor_and_no_authority_refused(context):
    p, r, s, a, runner, cell = context
    runner.actor = Actor(8, 'tenant', a.project_root)
    with pytest.raises((ValueError, EnvironmentError)):
        runner.run([cell])
    runner.actor = a
    runner.authorize = lambda *_: False
    with pytest.raises(EnvironmentError, match='authorization'):
        runner.run([cell])
    assert p.calls == 0


def test_existing_file_does_not_establish_execution_and_collect_is_bound(context):
    p, r, s, a, runner, cell = context
    cell.out.parent.mkdir()
    cell.out.write_text('unowned')
    assert runner.run([cell])[cell.name]['state'] == 'in_flight'
    assert p.calls == 1
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    with pytest.raises(ValueError, match='Existing output differs'):
        runner.run([cell])
    assert cell.out.read_text() == 'unowned'


def test_collect_proves_success_and_receipt_hash(context):
    p, r, s, a, runner, cell = context
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    result = runner.run([cell])[cell.name]
    assert result['state'] == 'done'
    assert result['receipt']['operation_id'] == cell.operation_id
    assert hashlib.sha256(cell.out.read_bytes()).hexdigest() == result['receipt']['outputs'][0]['sha256']
    p.output = b'changed'
    with pytest.raises(EnvironmentError, match='differ'):
        runner.run([cell])
    assert p.calls == 1


def test_generated_smoke_is_not_executed_with_host_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_PRIVATE_TOKEN', 'fixture-only')
    marker = tmp_path / 'executed'
    (tmp_path / 'experiment.py').write_text(f'open({str(marker)!r}, "w").write("ran")')
    with pytest.raises(RuntimeError, match='isolated smoke'):
        ex._smoke({}, tmp_path)
    assert not marker.exists()


def test_registered_criterion_and_source_numbers():
    plan = {'hypothesis': 'lower loss', 'dataset': {'name': 'fixture'}, 'seeds': [0, 1],
            'arms': [{'name': 'method', 'role': 'method'}, {'name': 'baseline', 'role': 'baseline'}],
            'primary_metric': {'name': 'loss', 'direction': 'lower'},
            'comparison': {'method': 'method', 'baseline': 'baseline'},
            'decision_rule': {'direction': 'lower', 'minimum_effect': 0.01, 'uncertainty_multiplier': 2.0}}
    cells = {f'{arm}__s{seed}': {'arm': arm, 'seed': seed, 'metrics': {'loss': loss}}
             for arm, loss in [('method', 0.2), ('baseline', 0.3)] for seed in [0, 1]}
    summary = ex.summarize_cells(plan, cells)
    assert summary['decision']['outcome'] == 'supported'
    plan['decision_rule']['minimum_effect'] = 0.2
    assert ex.summarize_cells(plan, cells)['decision']['outcome'] == 'refuted'


def test_preregistration_cannot_move_after_results(tmp_path):
    plan = {'decision_rule': {}, 'comparison': {}, 'primary_metric': {}, 'seeds': [0, 1]}
    ctx = {'run_dir': str(tmp_path), 'artifacts': {'plan': {'plan': plan}}}
    ex._preregister(ctx)
    (tmp_path / 'results/cells').mkdir(parents=True)
    (tmp_path / 'results/cells/result.json').write_text('{}')
    plan['seeds'] = [2, 3]
    with pytest.raises(RuntimeError, match='plan changed'):
        ex._preregister(ctx)


def test_code_audit_happens_before_smoke_and_never_grants_isolation(tmp_path, monkeypatch):
    marker = tmp_path / 'executed'
    source = f'open({str(marker)!r}, "w").write("ran")'
    calls = []

    def chat(ctx, role, prompt, label):
        calls.append(label)
        return '```python\n' + source + '\n```' if role == 'writer' else '[]'

    monkeypatch.setattr(ex, '_chat', chat)
    plan = {'decision_rule': {}, 'comparison': {}, 'primary_metric': {}, 'seeds': [0, 1]}
    ctx = {'run_dir': str(tmp_path), 'environment': 'fixture',
           'artifacts': {'plan': {'plan': plan}}}
    with pytest.raises(RuntimeError, match='isolated smoke'):
        ex._run_code(ctx)
    assert calls == ['experiment-code', 'experiment-code-audit']
    assert not marker.exists()


def test_malformed_audit_is_not_silently_approval(monkeypatch):
    monkeypatch.setattr(ex, '_chat', lambda *_: '{}')
    with pytest.raises(ValueError, match='malformed is not approval'):
        ex._audit_code({'artifacts': {'plan': {'plan': {}}}}, 'pass')


def test_abort_at_authorization_prevents_submit(context, monkeypatch):
    p, r, s, a, runner, cell = context
    checks = iter([False, True])
    with pytest.raises(EnvironmentError) as error:
        runner.run([cell], abort_check=lambda: next(checks, True))
    assert error.value.code == 'submission_aborted'
    assert p.calls == 0


def test_transient_repository_is_not_recovery_authority(context):
    p, r, s, a, runner, cell = context
    service = JobService(MemoryRepository(), lambda *_: p)
    with pytest.raises(ValueError, match='durable'):
        JobsBackend(service, a, lambda *_: True, bindings={cell.name: cell})


def test_atomic_result_publication_never_leaves_partial_final(context, monkeypatch):
    from lib.longform.documents import compute_backends as cb
    p, r, s, a, runner, cell = context
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    link = cb.os.link

    def lost(*args, **kwargs):
        if Path(args[1]).name == cell.out.name and 'dst_dir_fd' in kwargs:
            raise OSError('crash before publication')
        return link(*args, **kwargs)

    monkeypatch.setattr(cb.os, 'link', lost)
    with pytest.raises(OSError, match='before publication'):
        runner.run([cell])
    assert not cell.out.exists()
    monkeypatch.setattr(cb.os, 'link', link)
    row = runner.run([cell])[cell.name]
    assert row['state'] == 'done' and cell.out.read_bytes() == p.output
    assert row['observation'] == json.loads(p.output)
    assert row['receipt_text'].encode() == p.output
    assert p.calls == 1


def test_terminal_discrepancies_are_not_scientific_output(context):
    p, r, s, a, runner, cell = context
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0,
                  'discrepancies': [{'code': 'outside_plan', 'message': 'fixture'}]}
    assert runner.run([cell])[cell.name]['state'] == 'failed'
    assert not cell.out.exists()


def test_cancel_drains_siblings_and_retains_all_errors(context, monkeypatch):
    p, r, s, a, runner, cell = context
    calls = []

    def fail(cells, **kwargs):
        calls.append(cells[0].name)
        raise ValueError(cells[0].name)

    monkeypatch.setattr(runner, 'run', fail)
    with pytest.raises(ExceptionGroup) as error:
        runner.cancel([cell, replace(cell, name='other')])
    assert calls == [cell.name, 'other']
    assert len(error.value.exceptions) == 2


def test_no_interpreter_probe_and_cleanup_preserves_primary(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from threading import Event
    monkeypatch.setattr(ex.subprocess, 'run', lambda *_a, **_kw: pytest.fail('host interpreter launch'))
    assert 'unverified' in ex.probe_environment('untrusted-python')
    monkeypatch.setattr(ex, 'read_direction', lambda *_: {'topic': 'fixture'})
    aborted = Event()

    def fail(*args, **kwargs):
        aborted.set()
        raise RuntimeError('original workflow error')

    def cleanup(cells):
        raise ValueError('cleanup error')

    monkeypatch.setattr(ex, 'run_workflow', fail)
    backend = SimpleNamespace(bindings={}, cancel=cleanup)
    with pytest.raises(BaseExceptionGroup) as error:
        ex.run_experiment_stage(str(tmp_path), backend=backend, abort_event=aborted,
                                environment='host-admitted fixture')
    assert [str(e) for e in error.value.exceptions] == ['original workflow error', 'cleanup error']


def test_analysis_uses_retained_receipt_observations_not_local_json(tmp_path, monkeypatch):
    from types import SimpleNamespace
    plan = {'hypothesis': 'lower loss', 'dataset': {'name': 'fixture'}, 'seeds': [0, 1],
            'arms': [{'name': 'method', 'role': 'method'}, {'name': 'baseline', 'role': 'baseline'}],
            'primary_metric': {'name': 'loss', 'direction': 'lower'},
            'comparison': {'method': 'method', 'baseline': 'baseline'},
            'decision_rule': {'direction': 'lower', 'minimum_effect': 0.01, 'uncertainty_multiplier': 2.0}}
    observed = {f'{arm}__s{seed}': {'arm': arm, 'seed': seed, 'metrics': {'loss': loss}}
                for arm, loss in [('method', 0.2), ('baseline', 0.3)] for seed in [0, 1]}
    cells = tmp_path / 'results/cells'
    cells.mkdir(parents=True)
    (cells / 'unowned.json').write_text('not JSON and must not be read')
    (tmp_path / 'preregistration.json').write_text(json.dumps(
        {'plan_sha256': ex._digest(plan), 'registered_at': '2026-11-18T01:00:00+0000'}))
    work = tmp_path / '.experiment'
    work.mkdir()

    def plot(argv, **kwargs):
        assert argv[1] == '-I'
        assert set(kwargs['env']) == {'PATH', 'HOME', 'LANG', 'MPLCONFIGDIR'}
        Path(argv[-1]).write_bytes(b'%PDF-fixture')
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(ex.subprocess, 'run', plot)
    ctx = {'run_dir': str(tmp_path), 'workdir': str(work), 'python': 'fixture-python',
           'artifacts': {'plan': {'plan': plan}, 'code': {'sha256': 'a'*64},
                         'run': {'observations': {}}}}
    # Analysis consumes the checked decoder's output, not the checkpoint copy.
    # Real-store tests exercise admission and verify the independent copy.
    monkeypatch.setattr(ex, '_checked_run', lambda *_: ([], observed))
    ex._run_analyze(ctx)
    summary = json.loads((tmp_path / 'results/summary.json').read_text())
    assert summary['decision']['outcome'] == 'supported'
    assert json.loads((tmp_path / 'results/cells_index.json').read_text()) == observed


@pytest.mark.parametrize('payload', ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'])
def test_ambiguous_or_nonfinite_observations_never_publish(context, payload):
    p, r, s, a, runner, cell = context
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    p.output = payload.encode()
    with pytest.raises(ValueError):
        runner.run([cell])
    assert not cell.out.exists()


def test_historical_code_checkpoint_does_not_bypass_isolation(tmp_path):
    code = tmp_path / 'experiment.py'
    code.write_text('pass')
    art = {'path': 'experiment.py', 'sha256': hashlib.sha256(code.read_bytes()).hexdigest(),
           'smoke_failures': [], 'blocking_findings': []}
    assert 'Authorized isolated smoke execution is not wired' in ex._gate_code(
        {'run_dir': str(tmp_path)}, art)


def test_run_stage_forwards_host_expiry_and_abort_not_direction_budget(tmp_path):
    from types import SimpleNamespace
    plan = {'arms': [{'name': 'method'}], 'seeds': [0]}
    code_dir = tmp_path / 'code'
    code_dir.mkdir()
    code = code_dir / 'experiment.py'
    code.write_text('pass')
    code_sha = hashlib.sha256(code.read_bytes()).hexdigest()
    (tmp_path / 'preregistration.json').write_text(json.dumps({'plan_sha256': ex._digest(plan)}))
    cell = SimpleNamespace(scientific_plan_sha256=ex._digest(plan), audited_code_sha256=code_sha,
                           out=tmp_path / 'results/cells/method__s0.json')
    seen = []

    def run(cells, **kwargs):
        seen.append(kwargs)
        return {'method__s0': {'state': 'in_flight'}}

    abort = lambda: False
    backend = SimpleNamespace(bindings={'method__s0': cell}, expires_at=123.0,
                              name='fixture', run=run)
    ctx = {'run_dir': str(tmp_path), 'workdir': str(tmp_path / '.experiment'),
           'direction': {'compute': {'budget_hours': 999}}, 'backend': backend,
           'abort_check': abort, 'artifacts': {'plan': {'plan': plan}, 'code': {'sha256': code_sha}}}
    ex._run_run(ctx)
    assert seen[0]['deadline'] == 123.0 and seen[0]['abort_check'] is abort


def test_source_corruption_cannot_prevent_owned_cancellation(context):
    p, r, s, a, runner, cell = context
    runner.run([cell])
    archive = Path(a.project_root) / '.experiment-sources' / f'{cell.source_sha256}.zip'
    archive.chmod(0o600)
    archive.write_bytes(b'corrupted fixture')
    assert runner.cancel([cell])[cell.name]['state'] == 'in_flight'
    assert p.stops == 1


def test_submit_authority_does_not_implicitly_grant_cancellation(context):
    p, r, s, a, runner, cell = context
    runner.run([cell])
    without_cancel = JobsBackend(s, a, runner.authorize, bindings={cell.name: cell})
    with pytest.raises(ExceptionGroup) as error:
        without_cancel.cancel([cell])
    assert error.value.exceptions[0].code == 'approval_required'
    assert p.stops == 0
