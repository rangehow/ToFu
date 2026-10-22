"""Fresh-process task creation and public-tool affinity import comparison.

Reference: exact pre-change factory supplied in a frozen Git-source receipt.
Both arms compile/retain that reference and import the same common modules.
Cold clocks include first actual task/tool operation; interpreter/common imports
are excluded. Web case preloads Quart and uses its real request context.
Temporary tasks/files are owned and removed; no live inference or durable data.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.tasks_pkg.manager import _registry


def worker(variant, case, samples, reference):
    namespace = dict(vars(_registry))
    exec(compile(reference['factory_source'], '<frozen-task-factory>', 'exec'), namespace)
    if variant == 'reference':
        _registry._create_chat_task = namespace['_create_chat_task']
    owns = set()
    def create():
        task = _registry.create_task('', [], {}, user_id=900001, transient=True, supersede=False)
        owns.add(task['id'])
        return task
    def dispose(task):
        if case == 'tool':
            _registry.chat_task_runtime.finish(task['id'], result='owned offline fixture')
        _registry.chat_task_runtime.discard(task['id']); owns.remove(task['id'])
        assert _registry.chat_task_runtime.get(task['id']) is None
    def receipt(task):
        return {'owner': _registry.task_user_id(task), 'transient': task['_transientRuntime'],
                'reconnectable': task['_reconnectable'], 'affinity': task.get('_affinityKey', ''),
                'status': task['status']}
    with tempfile.TemporaryDirectory(prefix='tofu-task-affinity-') as directory:
        root = Path(directory)
        if case == 'tool':
            sys.path.insert(0, str(ROOT / 'benchmarks/repository_index'))
            from domain_measure import fixture
            from publication_port import PublishedRepositoryToolPort
            from task_measure import task_operation
            from lib.agent_core.execution_session import bind_tool_environment
            from lib.repo_map._native_rank import available_kernel
            assert available_kernel() is not None
            for name, text in fixture('python_extraction').items():
                (root / name).write_text(text)
        def operation():
            task = create()
            try:
                output = receipt(task)
                if case == 'tool':
                    session = task['_executionSession']
                    bind_tool_environment(session, lambda: None)
                    session.mark_routed(); session.mark_reserved(); session.mark_admitted()
                    assert _registry.chat_task_runtime.mark_running(task['id'])
                    output['tool'] = task_operation(PublishedRepositoryToolPort(root), task)
                return output
            finally:
                dispose(task)
        def timed():
            before = time.perf_counter_ns(); cpu = time.process_time_ns()
            output = operation()
            return {'wall_ms': (time.perf_counter_ns()-before)/1e6,
                    'cpu_ms': (time.process_time_ns()-cpu)/1e6, 'output': output}
        import time
        if case == 'web':
            from quart import Quart
            app = Quart('affinity-measure-fixture', root_path=directory)
            async def sequence():
                async with app.test_request_context('/', headers={'X-Tofu-Affinity-Key': 'owned-routing'}):
                    first = timed()
                    for _ in range(20): operation()
                    rows = [timed() for _ in range(samples)]
                    assert not owns
                    return first, rows
            first, rows = asyncio.run(sequence())
        else:
            assert 'quart' not in sys.modules, 'common imports must stay web-free'
            first = timed(); rows = []
        assert not owns
        from lib import log as audit
        audit._drain_audit_queue(timeout=5.0)
        assert audit._audit_queue is None or audit._audit_queue.unfinished_tasks == 0
        return {'audit_queue_settled': True, 'process_identity': os.getpid(), 'case': case, 'variant': variant,
                'first_operation': first, 'warm_operations': rows,
                'quart_loaded_after': 'quart' in sys.modules,
                'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                'factory_source_sha256': hashlib.sha256(
                    (ROOT/'lib/tasks_pkg/manager/_registry.py').read_bytes()).hexdigest(),
                'reference_source_sha256': reference['module_source_sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--case', choices=('create', 'tool', 'web'), required=True)
    parser.add_argument('--worker', choices=('reference', 'candidate'))
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--fresh-workers', type=int, default=100)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(); os.sched_setaffinity(0, {args.cpu})
    raw = args.reference.read_bytes(); reference = json.loads(raw)
    if args.worker:
        print(json.dumps(worker(args.worker, args.case, args.samples, reference))); return
    assert args.output and args.pairs > 0 and args.fresh_workers > 0
    source = (ROOT/'lib/tasks_pkg/manager/_registry.py').read_bytes()
    python = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    reports = {'scope': __doc__, 'case': args.case, 'pairs': [],
               'candidate_source_sha256': hashlib.sha256(source).hexdigest(),
               'reference_receipt_sha256': hashlib.sha256(raw).hexdigest(),
               'production_promoted': False, 'whole_system_admission': False}
    seen = set()
    for pair in range(args.pairs):
        values = {arm: [] for arm in ('reference', 'candidate')}
        for index in range(args.fresh_workers):
            for arm in (('reference','candidate') if (pair+index)%2==0 else ('candidate','reference')):
                assert (ROOT/'lib/tasks_pkg/manager/_registry.py').read_bytes()==source
                assert args.reference.read_bytes()==raw
                command = [python,__file__,'--reference',str(args.reference),'--case',args.case,
                           '--worker',arm,'--cpu',str(args.cpu),'--samples',str(args.samples)]
                with tempfile.TemporaryDirectory(prefix='tofu-affinity-worker-runtime-') as runtime_dir:
                    environment = dict(os.environ, TOFU_DATA_DIR=runtime_dir)
                    done = subprocess.run(command,cwd=ROOT,env=environment,
                                          capture_output=True,text=True,timeout=120)
                if done.returncode:
                    raise RuntimeError('owned benchmark worker failed: ' + done.stderr[-2000:])
                result = json.loads(done.stdout)
                assert result['process_identity'] not in seen; seen.add(result['process_identity'])
                assert result['factory_source_sha256']==reports['candidate_source_sha256']
                values[arm].append(result)
            before, after = values['reference'][-1], values['candidate'][-1]
            assert before['first_operation']['output']==after['first_operation']['output']
            assert [r['output'] for r in before['warm_operations']]==[r['output'] for r in after['warm_operations']]
        reports['pairs'].append(values); reports['complete']=len(reports['pairs'])==args.pairs
        args.output.write_text(json.dumps(reports,indent=2)+'\n')
        print(args.case,pair,statistics.median(r['first_operation']['wall_ms'] for r in values['candidate'])/
              statistics.median(r['first_operation']['wall_ms'] for r in values['reference']),flush=True)


if __name__ == '__main__':
    main()
