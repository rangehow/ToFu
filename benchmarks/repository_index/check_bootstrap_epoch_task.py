"""Fresh misses, forced witnessed rebuild, automatic rules and failed reuse."""
from pathlib import Path
import tempfile
from unittest.mock import patch

from bootstrap_epoch_task_measure import create_bootstrap_group, ReuseExtractionEpoch
from budget import CacheBudgetPlan
from extraction_epoch import loaded_identity
from index import outline
from quota import CacheReservationPool
from task_port import TaskRepositoryToolPort
from task_measure import task_operation, end_task
from lib.agent_core.execution_session import bind_tool_environment
from lib.agent_core.task_runtime import TaskRuntime

OWNER = 900001


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-bootstrap-epoch-') as folder:
        roots = [Path(folder)/name for name in ('left', 'right')]
        for root in roots:
            root.mkdir(); (root/'module.py').write_text('def original():\n    return source_reference\n')
        pool = CacheReservationPool(CacheBudgetPlan(2*1024*1024, 1024*1024, 2, 'fixture'))
        kernel = TaskRuntime('benchmark.bootstrap-epoch', push_channel='')
        task = kernel.create(user_id=OWNER); execution = task['_executionSession']
        calls = [0]; queries = [0]; failed = [False]
        original = outline._collect_source_tags
        def extract(*args, **kwargs):
            calls[0] += 1
            return original(*args, **kwargs)
        def identity():
            queries[0] += 1
            if failed[0]:
                raise OSError('injected bootstrap witness read failure')
            return loaded_identity()
        witness = ReuseExtractionEpoch(identity_source=identity)
        group = create_bootstrap_group(pool, OWNER, task['id'], roots, epoch_source=lambda root: 0,
                                       witness_factory=lambda: witness)
        bind_tool_environment(execution, lambda: group.close(OWNER))
        execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
        assert kernel.mark_running(task['id'])
        try:
            with patch.object(outline, '_collect_source_tags', extract):
                first = group.scan(OWNER, roots[0])
                assert queries[0] == 0 and calls[0] == 1
                assert group.retained_bytes(OWNER) == 0, 'one-off scan packed unused cache data'
                # A failed first actual witness cannot reuse provisional tags.
                failed[0] = True
                try:
                    group.scan(OWNER, roots[0])
                except OSError as error:
                    assert str(error) == 'injected bootstrap witness read failure'
                else:
                    raise AssertionError('provisional cache reused after witness failure')
                assert calls[0] == 1
                failed[0] = False
                before = calls[0]
                assert group.scan(OWNER, roots[0]) == first
                assert calls[0] == before + 1, 'first witnessed scan failed to rebuild provisional entries'
                before = calls[0]
                assert group.scan(OWNER, roots[0]) == first and calls[0] == before
                # The shared witness also handles another authorized root.
                before_queries = queries[0]
                assert group.scan(OWNER, roots[1]) == outline.scan(str(roots[1]))
                assert queries[0] == before_queries, 'fresh other root queried a reuse witness'
                assert group._scopes[str(roots[1])].retained_bytes(OWNER) == 0
                with patch.object(outline, 'STOP_WORDS', outline.STOP_WORDS | {'source_reference'}):
                    assert group.scan(OWNER, roots[0]) == outline.scan(str(roots[0]))
                port = TaskRepositoryToolPort(str(roots[0]), group.for_root(OWNER, roots[0]))
                assert task_operation(port, task) == task_operation(TaskRepositoryToolPort(str(roots[0])), task)
                failed[0] = True
                settlements = []
                def finalize(task, number, entry, metadata, **options):
                    settlements.append(options.get('status'))
                result = port.invoke(task, {}, finalize)
                assert settlements == ['error'] and 'injected bootstrap witness read failure' in str(result)
                for owner, root in ((OWNER+1, roots[0]), (OWNER, Path(folder)/'foreign')):
                    before = queries[0]
                    try:
                        group.scan(owner, root)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError('foreign owner/root admitted')
                    assert queries[0] == before
        finally:
            end_task(kernel, task, True); group.close(OWNER)
        assert pool.snapshot()['reserved_bytes'] == 0 and kernel.task_count == 0
        assert group.retained_bytes(OWNER) == 0
    print('fresh miss without query; mandatory witnessed rebuild before hits; multi-root/rule/owner checks; failed reuse error settlement and terminal release')


if __name__ == '__main__':
    check()
