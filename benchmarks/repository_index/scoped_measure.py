"""Actual execution-owned/quota-reserved scan/rank/render comparison.

One parent launch probe supplies all workers' budgets. Cold operations include
session creation, acquisition, registration and terminal release in both arms.
Quota exhaustion uses the canonical scan; no real application default changes.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from real_measure import snapshot, REPOSITORY, outline, rank, render_map
from budget import CacheBudgetPlan, derive_cache_budget
from quota import CacheQuotaExceeded, CacheReservationPool
from runtime_guards import probe_system_resources, resource_budget_manifest
from lib.agent_core.execution_session import ExecutionSession, ExecutionPhase, ResourceDisposition, bind_tool_environment

OWNER = 900001


def worker(source, variant, case, plan, samples, memory=False):
    with tempfile.TemporaryDirectory(prefix='tofu-scoped-index-') as folder:
        root = Path(folder)/'source'; shutil.copytree(source, root)
        edited = root/'lib/repo_map/outline.py'; original = edited.read_text()
        pool = CacheReservationPool(plan)
        active = None
        fallback_count = 0
        peak_cache_bytes = 0

        def prepare():
            nonlocal fallback_count
            execution = ExecutionSession(execution_id='scoped-index-'+uuid4().hex, kind='task', owner_user_id=OWNER)
            scope = None
            try:
                if variant == 'scoped':
                    try:
                        scope = pool.create_scope(OWNER, execution.execution_id, str(root), epoch_source=lambda: 0)
                    except CacheQuotaExceeded:
                        fallback_count += 1
                bind_tool_environment(execution, (lambda: scope.close(OWNER)) if scope else (lambda: None))
                execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
                execution.mark_dispatch_started()
                return execution, scope
            except BaseException:
                execution.settle(ExecutionPhase.FAILED, cause='benchmark_prepare_failure')
                if scope: scope.close(OWNER)
                raise

        def finish(current, outcome=ExecutionPhase.COMPLETED):
            execution, scope = current
            receipt = execution.settle(outcome)
            assert all(disposition == ResourceDisposition.RELEASED for _, disposition in receipt.resource_dispositions)
            if scope:
                scope.close(OWNER)
                assert scope.retained_bytes(OWNER) == 0
            assert pool.snapshot()['reserved_bytes'] == 0

        def operation(index):
            nonlocal active, peak_cache_bytes
            if case == 'edited':
                edited.write_text(original+f'\ndef benchmark_changed_{index}():\n    return scan\n')
            if active is None: active = prepare()
            try:
                _, scope = active
                repo = scope.scan(OWNER) if scope else outline.scan(str(root))
                if scope: peak_cache_bytes = max(peak_cache_bytes, scope.retained_bytes(OWNER))
                scores = rank.ensure_scores(repo, rank.sumbd(repo, {'lib/repo_map/outline.py'}))
                output = render_map(repo, scores)
            except BaseException:
                if case == 'cold':
                    current, active = active, None
                    try:
                        finish(current, ExecutionPhase.FAILED)
                    except Exception:
                        outline.logger.exception('Scoped benchmark cleanup failed; preserving scan failure')
                raise
            else:
                if case == 'cold':
                    current, active = active, None
                    finish(current)
                return output

        completed = False
        try:
            active = prepare()
            _, scope = active
            expected = outline.scan(str(root)); actual = scope.scan(OWNER) if scope else outline.scan(str(root))
            assert actual == expected
            assert rank.sumbd(actual, {'lib/repo_map/outline.py'}) == rank.sumbd(expected, {'lib/repo_map/outline.py'})
            finish(active); active = None
            for index in range(20): operation(index)
            raw = []; trace = hashlib.sha256()
            for index in range(20, 20+(20 if memory else samples)):
                started = time.perf_counter_ns(); cpu = time.process_time_ns()
                output = operation(index)
                raw.append({'wall_ms': (time.perf_counter_ns()-started)/1e6,
                            'cpu_ms': (time.process_time_ns()-cpu)/1e6})
                trace.update(json.dumps(output, sort_keys=True).encode()); trace.update(b'\0')
            wall = sorted(row['wall_ms'] for row in raw)
            result = {'pid': os.getpid(), 'p50_ms': statistics.median(wall),
                      'p95_ms': wall[int((len(wall)-1)*.95)], 'p99_ms': wall[int((len(wall)-1)*.99)],
                      'cpu_ms_per_operation': statistics.mean(row['cpu_ms'] for row in raw),
                      'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                      'peak_charged_cache_bytes': peak_cache_bytes, 'quota_fallback_count': fallback_count,
                      'trace_sha256': trace.hexdigest(), 'raw': [] if memory else raw}
            completed = True
            return result
        finally:
            primary = sys.exc_info()[1]
            try:
                if active is not None:
                    finish(active, ExecutionPhase.COMPLETED if completed else ExecutionPhase.FAILED)
                assert pool.snapshot()['active_scopes'] == 0 and pool.snapshot()['reserved_bytes'] == 0
            except Exception as cleanup:
                if primary is None:
                    raise
                primary.add_note('scoped cache cleanup also failed: '+type(cleanup).__name__)
                outline.logger.exception('Scoped benchmark cleanup failed; preserving primary failure')
            finally:
                pool.stop_admission()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', choices=('full', 'scoped'))
    parser.add_argument('--source', type=Path); parser.add_argument('--plan')
    parser.add_argument('--case', choices=('cold', 'warm', 'edited'))
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--samples', type=int, default=100); parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.source, args.worker, args.case, CacheBudgetPlan(**json.loads(args.plan)), args.samples, args.memory)))
        return
    assert args.output and args.samples > 0 and args.pairs > 0
    observed = probe_system_resources(); manifest = resource_budget_manifest(snapshot=observed)
    try: scopes = int(os.environ.get('TOFU_MAX_INFLIGHT_TASKS', ''))
    except ValueError: scopes = 0
    if scopes < 1: scopes = manifest['defaults']['TOFU_MAX_INFLIGHT_TASKS']
    plan = derive_cache_budget(observed, maximum_live_scopes=scopes)
    files = ('scoped_measure.py', 'index.py', 'budget.py', 'quota.py', 'owned_scope.py')
    sources = {name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in files}
    production_files = ('lib/repo_map/outline.py', 'lib/repo_map/rank.py', 'lib/repo_map/render.py',
                        'lib/agent_core/execution_session.py', 'runtime_guards.py')
    production = {name: hashlib.sha256((REPOSITORY/name).read_bytes()).hexdigest() for name in production_files}
    with tempfile.TemporaryDirectory(prefix='tofu-scoped-index-snapshot-') as folder:
        source = Path(folder)
        report = {'format': 'tofu.scoped-repository-index/v1', 'whole_system_verdict': 'incomplete',
                  'scope': __doc__, 'python': sys.version, 'affinity': sorted(os.sched_getaffinity(0)),
                  'probe': observed.as_dict(), 'budget': asdict(plan), 'corpus': snapshot(source),
                  'sources': sources, 'production_sources': production, 'samples': args.samples, 'pairs': args.pairs,
                  'memory_scope': 'separate process high-water including imports/fixture/oracle; sampled operations include cache owner/quota lifecycle', 'cases': []}
        for case in ((args.case,) if args.case else ('cold', 'warm', 'edited')):
            pairs = []; report['cases'].append({'case': case, 'pairs': pairs})
            for pair in range(args.pairs):
                result = {}
                for variant in (('full', 'scoped') if pair%2 == 0 else ('scoped', 'full')):
                    for name, digest in sources.items(): assert hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() == digest
                    for name, digest in production.items(): assert hashlib.sha256((REPOSITORY/name).read_bytes()).hexdigest() == digest
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__, '--worker', variant,
                               '--source', str(source), '--plan', json.dumps(asdict(plan)), '--case', case, '--samples', str(args.samples)]
                    done = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)
                    result[variant] = json.loads(done.stdout)
                    done = subprocess.run(command+['--memory'], check=True, capture_output=True, text=True, timeout=1800)
                    memory = json.loads(done.stdout)
                    result[variant]['memory_pid'] = memory['pid']; result[variant]['process_peak_rss_bytes'] = memory['process_peak_rss_bytes']
                assert result['full']['trace_sha256'] == result['scoped']['trace_sha256']
                pairs.append(result); args.output.write_text(json.dumps(report, indent=2)+'\n')
                print(case, pair, round(result['scoped']['p50_ms']/result['full']['p50_ms'], 3), flush=True)


if __name__ == '__main__': main()
