"""Fault-first tests on the real scratch HostRepository; providers execute nothing."""
from dataclasses import replace
from pathlib import Path
from threading import Event
import pytest

from tests.longform import test_experiment_stage as stage_tests
from tests.longform.test_experiment_stage import JobService, JobsBackend
from tofu_environments.repository import HostRepository, MANIFEST

pytestmark = pytest.mark.api
synthetic_context = stage_tests.context


@pytest.fixture
def real_job(tmp_path, synthetic_context):
    from lib.storage import StorageSupervisor
    p, _, old, actor, _, cell = synthetic_context
    supervisor = StorageSupervisor(project_root=tmp_path / 'sqlite', backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        client = supervisor.client
        client.command('plugin.register', {'manifest': MANIFEST}, 'fault-register')
        repo = HostRepository(lambda **_: client)
        service = JobService(repo, lambda *_: p)
        spec = old.status(actor, cell.operation_id)['plan']['spec']
        plan = service.plan(actor, 'fixture', spec, 'fault-cell')
        cell = replace(cell, operation_id=plan['operation_id'], plan_hash=plan['plan_hash'])
        approve = lambda a, h: a == actor and h == cell.plan_hash
        runner = JobsBackend(service, actor, approve, bindings={cell.name: cell}, cancel_authorize=approve)
        yield p, repo, service, actor, runner, cell
    finally:
        supervisor.stop()


def test_deadline_rechecked_after_last_blocking_read(real_job, monkeypatch):
    p, repo, service, actor, runner, cell = real_job
    now = [0]
    get = repo.get

    def blocking(a, op):
        row = get(a, op)
        if row['value']['phase'] == 'submitting':
            now[0] = 20
        return row

    monkeypatch.setattr(repo, 'get', blocking)
    from tofu_environments.contracts import EnvironmentError
    with pytest.raises(EnvironmentError) as error:
        service.submit(actor, cell.operation_id, cell.plan_hash, runner.authorize,
                       before_submit=lambda: now[0] < 10)
    assert error.value.code == 'submission_aborted' and p.calls == 0


def test_per_call_deadline_cannot_widen_host_expiry(real_job, monkeypatch):
    from lib.longform.documents import compute_backends as cb
    p, _, _, _, runner, cell = real_job
    runner.expires_at = 10
    from types import SimpleNamespace
    monkeypatch.setattr(cb, 'time', SimpleNamespace(time=lambda: 20))
    runner.run([cell], deadline=100)
    assert p.calls == 0


def test_known_receipt_survives_cancel_winning_local_settlement_cas(real_job, monkeypatch):
    p, repo, service, actor, runner, cell = real_job
    put = repo.put
    raced = []
    p.reconcile_job = lambda *_: None

    def conflict(a, op, value, version):
        if value['phase'] == 'queued' and not raced:
            raced.append(True)
            service.cancel(actor, cell.operation_id, cell.plan_hash, runner.cancel_authorize)
        return put(a, op, value, version)

    monkeypatch.setattr(repo, 'put', conflict)
    service.submit(actor, cell.operation_id, cell.plan_hash, runner.authorize)
    service.progress_cancellation(actor, cell.operation_id)
    record = repo.get(actor, cell.operation_id)['value']
    assert record['reference'] == p.reference
    assert p.calls == 1 and p.stops == 1


@pytest.mark.parametrize('existing_intent', [False, True])
def test_stop_not_blocked_by_provider_status_failure(real_job, existing_intent):
    p, repo, service, actor, runner, cell = real_job
    if existing_intent:
        p.lost = True
    service.submit(actor, cell.operation_id, cell.plan_hash, runner.authorize)
    if existing_intent:
        service.cancel(actor, cell.operation_id, cell.plan_hash, runner.cancel_authorize)

    def failed(*_):
        raise RuntimeError('status read unavailable')

    p.job_status = failed
    runner.cancel([cell])
    assert p.stops == 1
    assert repo.get(actor, cell.operation_id)['value']['cancellation']['state'] == 'claimed'


def test_output_ancestor_symlink_refuses_before_writes(real_job, tmp_path):
    p, _, _, _, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    outside = tmp_path / 'foreign'
    outside.mkdir()
    (tmp_path / 'results').symlink_to(outside, target_is_directory=True)
    cell = replace(cell, out=tmp_path / 'results/nested/result.json')
    runner.bindings[cell.name] = cell
    from tofu_environments.contracts import EnvironmentError
    with pytest.raises(EnvironmentError) as error:
        runner.run([cell])
    assert error.value.code == 'invalid_destination'
    assert list(outside.iterdir()) == []


def test_arm_seed_checked_before_publication(real_job):
    p, _, _, _, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    p.output = b'{"arm":"wrong","seed":99,"metrics":{"loss":0.1}}'
    with pytest.raises(ValueError, match='arm|seed'):
        runner.run([cell])
    assert not cell.out.exists()


def test_stop_during_collect_prevents_publish_and_cleans_snapshots(real_job):
    p, _, _, _, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    stopped = Event()

    def read(*_):
        yield p.output
        stopped.set()

    p.read_job_output = read
    row = runner.run([cell], abort_check=stopped.is_set)[cell.name]
    assert row['state'] == 'cancelled' and not cell.out.exists()
    assert not list(cell.out.parent.glob('compute-result-*'))


def _registered_gate(real_job, tmp_path):
    import hashlib
    import json
    from lib.longform.documents import experiments as ex
    from lib.longform.documents.experiment_source import create_experiment_source
    p, repo, service, actor, runner, cell = real_job
    plan = {'hypothesis': 'fixture', 'arms': [{'name': a} for a in ('method', 'baseline', 'ablation')],
            'seeds': [0], 'comparison': {'method': 'method', 'baseline': 'baseline'},
            'primary_metric': {'name': 'loss', 'direction': 'lower'}}
    code = tmp_path / 'code/experiment.py'
    code.parent.mkdir()
    code.write_bytes(cell.source_code)
    base = service.inspect(actor, cell.operation_id)['plan']['spec']
    cells = {}
    for arm in ('method', 'baseline', 'ablation'):
        name = arm + '__s0'
        source = create_experiment_source(actor=actor, code=cell.source_code,
            code_sha256=cell.audited_code_sha256, plan=plan, provider='fixture',
            cell_name=name, arm=arm, seed=0, attempt=1)
        spec = {**base, 'source': source}
        sealed = service.plan(actor, 'fixture', spec, 'gate-' + name)
        cells[name] = replace(cell, name=name, arm=arm, scientific_plan=plan,
            scientific_plan_sha256=ex._digest(plan), source_uri=source['uri'], source_sha256=source['sha256'],
            operation_id=sealed['operation_id'], plan_hash=sealed['plan_hash'],
            out=tmp_path / f'results/cells/{name}.json')
    runner.bindings = cells
    runner.authorize = lambda *_: True
    art = {'backend': 'fixture', 'in_flight': [], 'failed': [], 'done': list(cells),
           'code_sha256': cell.audited_code_sha256, 'receipts': {}, 'receipt_text': {}, 'observations': {}}
    for name, bound in cells.items():
        data = {'arm': bound.arm, 'seed': 0, 'metrics': {'loss': 0.1}}
        text = json.dumps(data)
        bound.out.parent.mkdir(parents=True, exist_ok=True)
        bound.out.write_text(text)
        art['observations'][name], art['receipt_text'][name] = data, text
        art['receipts'][name] = {'operation_id': bound.operation_id, 'reference': {'kind': 'fixture', 'id': 'one'},
            'outputs': [{'name': 'result.json', 'bytes': len(text.encode()),
                         'sha256': hashlib.sha256(text.encode()).hexdigest()}]}
    ctx = {'run_dir': str(tmp_path), 'backend': runner,
           'artifacts': {'plan': {'plan': plan}, 'run': art}}
    return ex, p, repo, service, actor, runner, cells, ctx, art


def test_fabricated_checkpoint_receipts_rejected_by_real_store(real_job, tmp_path):
    ex, p, repo, service, actor, runner, cells, ctx, art = _registered_gate(real_job, tmp_path)
    assert any('durable' in error for error in ex._gate_run(ctx, art))
    with pytest.raises(RuntimeError, match='Analysis refused'):
        ex._run_analyze(ctx)
    assert not (tmp_path / 'results/summary.json').exists()


def test_missing_registered_ablation_never_passes_resume_gate(real_job, tmp_path):
    ex, p, repo, service, actor, runner, cells, ctx, art = _registered_gate(real_job, tmp_path)
    for field in ('done', 'receipts', 'receipt_text', 'observations'):
        if isinstance(art[field], list):
            art[field].remove('ablation__s0')
        else:
            del art[field]['ablation__s0']
    assert any('Every registered' in error for error in ex._gate_run(ctx, art))


def test_genuine_all_arm_receipts_pass_then_missing_durable_receipt_refuses(real_job, tmp_path):
    import json
    ex, p, repo, service, actor, runner, cells, ctx, art = _registered_gate(real_job, tmp_path)
    # Remove only synthetic fixture files created above, never frozen pilot files.
    for bound in cells.values():
        bound.out.unlink()
    by_op = {bound.operation_id: bound for bound in cells.values()}

    def read(a, ref, op, name):
        yield json.dumps({'arm': by_op[op].arm, 'seed': 0, 'metrics': {'loss': 0.1}}).encode()

    p.read_job_output = read
    runner.run(list(cells.values()))
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    rows = runner.run(list(cells.values()))
    for name, row in rows.items():
        for field, key in [('receipts', 'receipt'), ('receipt_text', 'receipt_text'), ('observations', 'observation')]:
            art[field][name] = row[key]
    assert ex._gate_run(ctx, art) == []
    bound = cells['ablation__s0']
    saved = repo.get(actor, bound.operation_id)
    value = saved['value']
    del value['outputs']
    repo.put(actor, bound.operation_id, value, saved['version'])
    assert any('durable' in error for error in ex._gate_run(ctx, art))


def test_local_control_survives_provider_resolution_failure(real_job):
    p, repo, service, actor, runner, cell = real_job
    def unavailable(*_):
        raise RuntimeError('provider import unavailable')
    broken = JobService(repo, unavailable)
    assert broken.inspect(actor, cell.operation_id)['phase'] == 'planned'
    assert broken.cancel(actor, cell.operation_id, cell.plan_hash, runner.cancel_authorize)['phase'] == 'cancelled'
    assert p.calls == 0


def test_queued_stop_is_durable_before_provider_resolution(real_job):
    p, repo, service, actor, runner, cell = real_job
    runner.run([cell])
    def unavailable(*_):
        raise RuntimeError('provider import unavailable')
    broken = JobService(repo, unavailable)
    with pytest.raises(RuntimeError, match='provider import unavailable'):
        broken.cancel(actor, cell.operation_id, cell.plan_hash, runner.cancel_authorize)
    assert broken.inspect(actor, cell.operation_id)['cancellation']['state'] == 'requested'
    assert repo.get(actor, cell.operation_id)['value']['reference'] == p.reference


def test_stop_after_link_reports_committed_publication(real_job, monkeypatch):
    from lib.longform.documents import compute_backends as cb
    p, repo, service, actor, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    stop = Event()
    link = cb.os.link
    def committed(*args, **kwargs):
        value = link(*args, **kwargs)
        stop.set()
        return value
    monkeypatch.setattr(cb.os, 'link', committed)
    row = runner.run([cell], abort_check=stop.is_set)[cell.name]
    assert row['state'] == 'done' and cell.out.read_bytes() == p.output
    assert row['publication_committed'] is True and row['stop_after_publication'] is True


@pytest.mark.parametrize('metrics', [{}, {'loss': True}, {'loss': 'wrong'}])
def test_resume_repeats_registered_metric_validation(real_job, tmp_path, metrics):
    import json
    ex, p, repo, service, actor, runner, cells, ctx, art = _registered_gate(real_job, tmp_path)
    for bound in cells.values():
        bound.out.unlink()
    by_op = {bound.operation_id: bound for bound in cells.values()}
    def read(a, ref, op, name):
        yield json.dumps({'arm': by_op[op].arm, 'seed': 0, 'metrics': metrics}).encode()
    p.read_job_output = read
    for bound in cells.values():
        service.submit(actor, bound.operation_id, bound.plan_hash, lambda *_: True)
        p.snapshot = {'state': 'succeeded', 'exit_code': 0}
        service.status(actor, bound.operation_id)
        receipt = service.collect(actor, bound.operation_id, tmp_path)
        text = (Path(receipt['directory']) / 'result.json').read_text()
        art['receipts'][bound.name] = receipt
        art['receipt_text'][bound.name] = text
        art['observations'][bound.name] = json.loads(text)
    assert any('metric' in error.lower() for error in ex._gate_run(ctx, art))
    with pytest.raises(RuntimeError, match='Analysis refused'):
        ex._run_analyze(ctx)


def test_link_visible_but_directory_sync_unconfirmed_then_resume(real_job, monkeypatch):
    import stat
    from lib.longform.documents import compute_backends as cb
    p, _, _, _, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    sync = cb.os.fsync
    def fail_after_link(fd):
        if stat.S_ISDIR(cb.os.fstat(fd).st_mode) and cell.out.exists():
            raise OSError('synthetic output directory sync failure')
        sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(cb.os, 'fsync', fail_after_link)
        row = runner.run([cell])[cell.name]
    assert row['state'] == 'needs_attention'
    assert row['publication_committed'] is True
    assert row['publication_durability_confirmed'] is False
    assert 'observation' not in row
    assert cell.out.read_bytes() == p.output
    assert not list(cell.out.parent.glob('.owned-result-*'))
    assert not list(cell.out.parent.glob('compute-result-*'))
    resumed = runner.run([cell])[cell.name]
    assert resumed['state'] == 'done' and resumed['publication_durability_confirmed'] is True
    assert p.calls == 1


@pytest.mark.parametrize('on_wire, in_copy', [(True, 1), (1, 1.0), (1, True)])
def test_resume_rejects_python_equal_but_type_distinct_receipt_copy(real_job, on_wire, in_copy):
    import json
    p, _, service, actor, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    service.status(actor, cell.operation_id)
    p.output = json.dumps({'arm': cell.arm, 'seed': cell.seed, 'metrics': {'loss': on_wire}}).encode()
    cell.out.parent.mkdir()
    receipt = service.collect(actor, cell.operation_id, cell.out.parent)
    copy = {'arm': cell.arm, 'seed': cell.seed, 'metrics': {'loss': in_copy}}
    with pytest.raises(ValueError, match='metric|receipt'):
        runner.validate_receipt(cell, receipt, p.output.decode(), copy)


@pytest.mark.parametrize('durability_error', [False, True])
def test_post_link_cleanup_preserves_publication_outcome(real_job, monkeypatch, durability_error):
    import stat
    from lib.longform.documents import compute_backends as cb

    p, _, _, _, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}
    sync, unlink = cb.os.fsync, cb.os.unlink

    def sync_output(fd):
        if durability_error and stat.S_ISDIR(cb.os.fstat(fd).st_mode) and cell.out.exists():
            raise OSError('synthetic sync fault')
        sync(fd)

    def unlink_temporary(path, *args, **kwargs):
        if str(path).startswith('.owned-result-'):
            raise OSError('synthetic temporary cleanup fault')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cb.os, 'fsync', sync_output)
        patch.setattr(cb.os, 'unlink', unlink_temporary)
        row = runner.run([cell])[cell.name]
    assert cell.out.read_bytes() == p.output
    assert row['publication_committed'] is True
    assert row['publication_durability_confirmed'] is not durability_error
    assert len(row['cleanup_errors']) == 1 and 'synthetic temporary cleanup fault' in row['cleanup_errors'][0]
    if durability_error:
        assert row['state'] == 'needs_attention' and 'synthetic sync fault' in row['errors'][0]
        assert 'observation' not in row
    else:
        assert row['state'] == 'done' and row['observation']['seed'] == cell.seed


def test_snapshot_cleanup_failure_precedes_publication(real_job, monkeypatch):
    from tofu_environments.job_files import OutputDirectory
    p, _, _, _, runner, cell = real_job
    runner.run([cell])
    p.snapshot = {'state': 'succeeded', 'exit_code': 0}

    def remove_snapshot(self, name):
        assert name.startswith('compute-result-')
        assert not cell.out.exists()
        raise OSError('synthetic snapshot cleanup fault')

    monkeypatch.setattr(OutputDirectory, 'remove_tree', remove_snapshot)
    with pytest.raises(OSError, match='synthetic snapshot cleanup fault'):
        runner.run([cell])
    assert not cell.out.exists() and p.calls == 1
