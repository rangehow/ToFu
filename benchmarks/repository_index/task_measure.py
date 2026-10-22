"""Canonical TaskRuntime identity and execution-group cache through real tool/API.

Includes transient task creation/terminal cleanup in cold calls. Recording
finalization excludes durable publication, registry/epoch authority and real
agent call frequency. Memory runs the complete cohort in separate workers.
"""
import inspect

import scoped_measure
from task_indexes import ExecutionRepositoryIndexes
from task_port import TaskRepositoryToolPort
from tool_port import handler_template
from lib.agent_core.task_runtime import TaskRuntime


def task_operation(port, task):
    settlements=[]
    def finalize(task, number, entry, metadata, **options):
        settlements.append((metadata, options.get('status','done')))
    result=port.invoke(task, {}, finalize)
    assert len(settlements)==1 and settlements[0][1]=='done' and not result[2]
    assert 'user_id' not in task and task['_userId']==scoped_measure.OWNER
    return result,settlements[0]


def end_task(kernel,task,failed=False):
    if task is None:return
    kernel.finish(task['id'],result='offline fixture',error='fixture failure' if failed else None)
    kernel.cleanup_stale(max_age=0)
    assert kernel.task_count==0


def finish_current(current,outcome,pool):
    execution,scope,port,kernel,task=current
    try:
        end_task(kernel,task,outcome!=scoped_measure.ExecutionPhase.COMPLETED)
        receipt=execution.settle(outcome)
        assert all(value==scoped_measure.ResourceDisposition.RELEASED for _,value in receipt.resource_dispositions)
        if scope:
            scope.close(scoped_measure.OWNER);assert scope.retained_bytes(scoped_measure.OWNER)==0
        assert pool.snapshot()['reserved_bytes']==0
    except BaseException as primary:
        if kernel.task_count:
            try:end_task(kernel,task,True)
            except Exception as cleanup:
                primary.add_note('transient task cleanup also failed: '+type(cleanup).__name__)
        raise


def task_worker():
    source=inspect.getsource(scoped_measure.worker)
    changes={
        '        peak_cache_bytes = 0':'        peak_cache_bytes = 0\n        template = handler_template()',
        '            scope = None':'            scope = None; kernel = None; task = None',
        "            execution = ExecutionSession(execution_id='scoped-index-'+uuid4().hex, kind='task', owner_user_id=OWNER)":
            '            execution = None',
        "            try:\n                if variant == 'scoped':":
            "            try:\n                kernel = TaskRuntime('benchmark.canonical-repository-tool', push_channel='')\n"
            "                task = kernel.create(task_id='canonical-index-'+uuid4().hex, user_id=OWNER)\n"
            "                execution = task['_executionSession']\n                if variant == 'scoped':",
        '                execution.mark_dispatch_started()': "                assert kernel.mark_running(task['id'])",
        "                execution.settle(ExecutionPhase.FAILED, cause='benchmark_prepare_failure')":
            "                if execution: execution.settle(ExecutionPhase.FAILED, cause='benchmark_prepare_failure')",
        'scope = pool.create_scope(OWNER, execution.execution_id, str(root), epoch_source=lambda: 0)':
            'scope = ExecutionRepositoryIndexes(pool, OWNER, execution.execution_id, [str(root)], epoch_source=lambda root: 0)',
        '                return execution, scope':
            '                return execution, scope, TaskRepositoryToolPort(str(root), scope.for_root(OWNER, root) if scope else None, template=template), kernel, task',
        '                if scope: scope.close(OWNER)\n                raise':
            "                if scope: scope.close(OWNER)\n                primary = sys.exc_info()[1]\n"
            "                try: end_task(kernel, task, True)\n"
            "                except Exception as cleanup: primary.add_note('prepare task cleanup also failed: '+type(cleanup).__name__)\n                raise",
        '                _, scope = active':'                execution, scope, port, kernel, task = active',
        '            _, scope = active':'            _, scope, _, _, _ = active',
        'actual = scope.scan(OWNER) if scope else outline.scan(str(root))':
            'actual = scope.scan(OWNER, root) if scope else outline.scan(str(root))',
        '20+(20 if memory else samples)':'20+samples',
    }
    for before,after in changes.items():
        assert source.count(before)==1,before
        source=source.replace(before,after)
    start=source.index('        def finish(current,');end=source.index('        def operation(index):',start)
    source=source[:start]+'''        def finish(current, outcome=ExecutionPhase.COMPLETED):
            finish_current(current,outcome,pool)

'''+source[end:]
    block='''                repo = scope.scan(OWNER) if scope else outline.scan(str(root))
                if scope: peak_cache_bytes = max(peak_cache_bytes, scope.retained_bytes(OWNER))
                scores = rank.ensure_scores(repo, rank.sumbd(repo, {'lib/repo_map/outline.py'}))
                output = render_map(repo, scores)'''
    assert source.count(block)==1
    source=source.replace(block,'''                output = task_operation(port,task)
                if scope: peak_cache_bytes = max(peak_cache_bytes, scope.retained_bytes(OWNER))''')
    namespace=dict(scoped_measure.worker.__globals__,TaskRuntime=TaskRuntime,
        ExecutionRepositoryIndexes=ExecutionRepositoryIndexes,TaskRepositoryToolPort=TaskRepositoryToolPort,
        handler_template=handler_template,task_operation=task_operation,end_task=end_task,finish_current=finish_current)
    exec(compile(source,'<canonical-repository-task-worker>','exec'),namespace)
    return namespace['worker']


def main():
    source=inspect.getsource(scoped_measure.main)
    changes={
        "    files = ('scoped_measure.py',":
            "    files = ('task_measure.py', 'task_port.py', 'task_indexes.py', 'tool_port.py', 'scoped_measure.py',",
        "    production_files = ('lib/repo_map/outline.py',":
            "    production_files = ('lib/repo_map/api.py', 'lib/tasks_pkg/handlers/project.py',\n"
            "                        'lib/tasks_pkg/manager/_registry.py', 'lib/identity.py',\n"
            "                        'lib/agent_core/task_runtime.py', 'lib/repo_map/outline.py',",
    }
    for before,after in changes.items():
        assert source.count(before)==1,before
        source=source.replace(before,after)
    namespace=dict(scoped_measure.main.__globals__,__file__=__file__,__doc__=__doc__,worker=task_worker())
    exec(compile(source,'<canonical-repository-task-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
