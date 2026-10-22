"""Three-root public-tool task comparison; one operation calls all three roots.

Each root has a private copy of the same frozen real corpus, so both variants
perform identical work. Edited calls rotate the mutated root. One execution
reservation covers all roots; publication remains a recording fixture.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import tempfile
import time

import public_tool_measure
from bootstrap_epoch_task_measure import create_bootstrap_group
from extraction_epoch import loaded_identity
from quota import CacheQuotaExceeded, CacheReservationPool
from public_tool_port import PublicTaskRepositoryToolPort
from task_measure import task_operation, end_task
from lib.agent_core.execution_session import bind_tool_environment, ResourceDisposition, ExecutionPhase
from lib.agent_core.task_runtime import TaskRuntime

OWNER = 900001


def worker(source, variant, case, plan, samples, memory=False):
    with tempfile.TemporaryDirectory(prefix='tofu-public-multi-root-') as folder:
        roots = [Path(folder)/name for name in ('left', 'right', 'third')]
        for root in roots:
            shutil.copytree(source, root)
        edits = [root/'lib/repo_map/outline.py' for root in roots]
        originals = [path.read_text() for path in edits]
        pool = CacheReservationPool(plan)
        active = None; peak_cache_bytes = 0; fallback_count = 0
        def prepare():
            nonlocal fallback_count
            kernel = TaskRuntime('benchmark.multi-root-public-measure', push_channel='')
            task = kernel.create(user_id=OWNER); session = task['_executionSession']; group = None
            try:
                if variant == 'scoped':
                    try:
                        group = create_bootstrap_group(pool, OWNER, task['id'], roots, epoch_source=lambda root: 0)
                    except CacheQuotaExceeded:
                        fallback_count += 1
                bind_tool_environment(session, (lambda: group.close(OWNER)) if group else (lambda: None))
                session.mark_routed(); session.mark_reserved(); session.mark_admitted()
                assert kernel.mark_running(task['id'])
                ports = [PublicTaskRepositoryToolPort(root, group.for_root(OWNER, root) if group else None) for root in roots]
                return kernel, task, session, group, ports
            except BaseException as primary:
                try:
                    end_task(kernel, task, True)
                    if group: group.close(OWNER)
                except BaseException as cleanup:
                    primary.add_note('multi-root preparation cleanup also failed: '+type(cleanup).__name__)
                raise
        def finish(current, failed=False):
            kernel, task, session, group, ports = current
            end_task(kernel, task, failed)
            receipt = session.settle(ExecutionPhase.FAILED if failed else ExecutionPhase.COMPLETED)
            assert all(value == ResourceDisposition.RELEASED for _, value in receipt.resource_dispositions)
            if group:
                group.close(OWNER); assert group.retained_bytes(OWNER) == 0
            assert pool.snapshot()['reserved_bytes'] == 0
        def operation(index):
            nonlocal active, peak_cache_bytes
            if case == 'edited':
                selected = index % len(roots)
                edits[selected].write_text(originals[selected]+f'\ndef multi_root_edit_{index}():\n    return scan\n')
            if active is None: active = prepare()
            try:
                kernel, task, session, group, ports = active
                output = [task_operation(port, task) for port in ports]
                if group: peak_cache_bytes = max(peak_cache_bytes, group.retained_bytes(OWNER))
                return output
            except BaseException as primary:
                if case == 'cold':
                    current, active = active, None
                    try: finish(current, True)
                    except BaseException as cleanup: primary.add_note('cold multi-root cleanup also failed: '+type(cleanup).__name__)
                raise
            finally:
                if case == 'cold' and active is not None:
                    current, active = active, None; finish(current)
        primary = None
        try:
            for i in range(20): operation(i)
            raw = []; trace = hashlib.sha256()
            for i in range(20, 20+samples):
                started = time.perf_counter_ns(); cpu = time.process_time_ns()
                output = operation(i)
                raw.append({'wall_ms':(time.perf_counter_ns()-started)/1e6,
                            'cpu_ms':(time.process_time_ns()-cpu)/1e6})
                trace.update(json.dumps(output,sort_keys=True).encode());trace.update(b'\0')
            ordered = sorted(r['wall_ms'] for r in raw)
            return {'pid':os.getpid(),'p50_ms':statistics.median(ordered),
                'p95_ms':ordered[int((samples-1)*.95)],'p99_ms':ordered[int((samples-1)*.99)],
                'cpu_ms_per_operation':statistics.mean(r['cpu_ms'] for r in raw),
                'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                'peak_charged_cache_bytes':peak_cache_bytes,'quota_fallback_count':fallback_count,
                'trace_sha256':trace.hexdigest(),'raw':raw,'roots_per_execution':3,
                'tool_calls_per_operation':3,'warmup_operations':20,
                'loaded_extractor_packages':loaded_identity()['packages']}
        except BaseException as error:
            primary = error; raise
        finally:
            try:
                if active is not None: finish(active, primary is not None)
                assert pool.snapshot()['active_scopes']==0 and pool.snapshot()['reserved_bytes']==0
            except BaseException as cleanup:
                if primary is None: raise
                primary.add_note('multi-root terminal cleanup also failed: '+type(cleanup).__name__)
            finally: pool.stop_admission()


def main():
    # Keep the independent-process driver/order/identity assertions; replace
    # only its worker and add this implementation to its source witness.
    source = inspect.getsource(public_tool_measure.main)
    boundary = "'public_tool_measure.py', 'public_tool_port.py',"
    assert source.count(boundary)==1, 'public driver source inventory changed'
    source = source.replace(boundary, "'multi_root_measure.py', 'public_tool_measure.py', 'public_tool_port.py',")
    namespace = dict(public_tool_measure.main.__globals__, __file__=__file__, __doc__=__doc__, task_worker=lambda: worker)
    exec(compile(source,'<public-multi-root-task-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
