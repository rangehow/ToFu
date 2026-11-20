"""Real scratch storage + JobService recovery, never a live compute backend.

The storage service belongs to tmp_path. Providers are synthetic and execute
nothing. No production service, credentials or model endpoints are contacted.
"""
from dataclasses import replace
import hashlib
import pytest

from tests.longform.test_experiment_stage import Actor, Cell, JobsBackend, JobService, Provider, RESULT_NAME
from tofu_environments.repository import HostRepository, MANIFEST

pytestmark = pytest.mark.api


def test_lost_submit_receipt_reconciles_with_real_host_repository(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    supervisor = StorageSupervisor(project_root=tmp_path / 'storage', backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        client = supervisor.client
        client.command('plugin.register', {'manifest': MANIFEST}, 'experiment-test-register')
        repo = HostRepository(lambda **_: client)
        actor = Actor(9, 'fixture', str(tmp_path))
        provider = Provider()
        service = JobService(repo, lambda *_: provider)
        from lib.longform.documents.experiment_source import create_experiment_source
        from lib.longform.documents.experiments import _digest
        code, plan_data = b'pass\n', {'hypothesis': 'fixture'}
        code_sha = hashlib.sha256(code).hexdigest()
        source = create_experiment_source(actor=actor, code=code, code_sha256=code_sha,
                                         plan=plan_data, provider='fixture', cell_name='method__s0',
                                         arm='method', seed=0, attempt=1)
        cell = Cell('method__s0', ('python', 'experiment.py', '--out', RESULT_NAME),
                    tmp_path / 'owned-result.json', '', '', 'fixture', 'project',
                    _digest(plan_data), code_sha, source['sha256'], 1,
                    source['uri'], code, plan_data, 'method', 0)
        spec = {'version': 1, 'project': cell.project, 'command': list(cell.argv),
                'source': {'sha256': cell.source_sha256, 'uri': cell.source_uri},
                'resources': {'cpu': 1, 'memory_gib': 1, 'gpu_count': 0},
                'limits': {'runtime_seconds': 60, 'gpu_seconds': 0, 'result_bytes': 1024},
                'outputs': [RESULT_NAME], 'provider_options': {}}
        plan = service.plan(actor, 'fixture', spec, 'durable-fixture')
        cell = replace(cell, operation_id=plan['operation_id'], plan_hash=plan['plan_hash'])
        approved = lambda a, h: a == actor and h == cell.plan_hash
        runner = JobsBackend(service, actor, approved, bindings={cell.name: cell}, cancel_authorize=approved)
        original = repo.put

        def lose_receipt(a, op, value, version):
            if value['phase'] == 'queued':
                raise RuntimeError('receipt write lost')
            return original(a, op, value, version)

        monkeypatch.setattr(repo, 'put', lose_receipt)
        with pytest.raises(RuntimeError, match='receipt write lost'):
            runner.run([cell])
        # Recreate the embedding against the actual selected scratch store;
        # the pre-submit claim survived the failed receipt write.
        recovered_repo = HostRepository(lambda **_: client)
        assert recovered_repo.get(actor, cell.operation_id)['value']['phase'] == 'submitting'
        recovered = JobsBackend(JobService(recovered_repo, lambda *_: provider), actor, approved,
                                bindings={cell.name: cell})
        recovered.run([cell])
        assert provider.calls == 1
        assert recovered_repo.get(Actor(10, 'fixture', str(tmp_path)), cell.operation_id) is None
        provider.snapshot = {'state': 'succeeded', 'exit_code': 0}
        result = recovered.run([cell])[cell.name]
        assert result['state'] == 'done' and cell.out.read_bytes() == provider.output
        assert recovered_repo.get(actor, cell.operation_id)['value']['outputs'] == result['receipt']['outputs']

        cancel_provider = Provider()
        cancel_provider.lost = True
        pending = JobService(recovered_repo, lambda *_: cancel_provider)
        stop_plan = pending.plan(actor, 'fixture', spec, 'durable-cancellation-fixture')
        stopping_cell = replace(cell, operation_id=stop_plan['operation_id'],
                                plan_hash=stop_plan['plan_hash'], out=tmp_path / 'cancelled-result.json')
        stop_authority = lambda a, h: a == actor and h == stopping_cell.plan_hash
        assert pending.submit(actor, stopping_cell.operation_id, stopping_cell.plan_hash,
                              stop_authority)['phase'] == 'uncertain'
        intent = pending.cancel(actor, stopping_cell.operation_id, stopping_cell.plan_hash, stop_authority)
        assert intent['cancellation']['state'] == 'requested' and cancel_provider.stops == 0
        resumed = JobsBackend(JobService(HostRepository(lambda **_: client), lambda *_: cancel_provider),
                              actor, stop_authority, bindings={stopping_cell.name: stopping_cell},
                              cancel_authorize=stop_authority)
        resumed.run([stopping_cell])
        assert cancel_provider.calls == 1 and cancel_provider.stops == 1
        cancel_provider.snapshot = {'state': 'cancelled', 'exit_code': None}
        assert resumed.run([stopping_cell])[stopping_cell.name]['state'] == 'cancelled'
        assert recovered_repo.get(actor, stopping_cell.operation_id)['value']['phase'] == 'cancelled'
    finally:
        supervisor.stop()
