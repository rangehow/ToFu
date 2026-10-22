"""Real transient registration/publication equality and private lifecycle checks."""
from pathlib import Path
import tempfile
from bootstrap_epoch_task_measure import create_bootstrap_group
from budget import CacheBudgetPlan
from quota import CacheReservationPool
from publication_port import TransientChatRuntime, PublishedRepositoryToolPort
from task_measure import task_operation, end_task
from lib.agent_core.execution_session import bind_tool_environment
from lib.tasks_pkg.manager.runtime import chat_task_runtime

OWNER = 900001


def check():
    sentinel = chat_task_runtime.create(user_id=OWNER+1)
    with tempfile.TemporaryDirectory(prefix='tofu-repo-publication-') as directory:
        root = Path(directory)/'owned'; root.mkdir()
        other = Path(directory)/'foreign'; other.mkdir()
        (root/'module.py').write_text('def original():\n    return reference\n')
        runtime = TransientChatRuntime(); task = runtime.create(user_id=OWNER)
        pool = CacheReservationPool(CacheBudgetPlan(2*1024*1024, 1024*1024, 2, 'fixture'))
        group = create_bootstrap_group(pool, OWNER, task['id'], [root], epoch_source=lambda root:0)
        bind_tool_environment(task['_executionSession'], lambda:group.close(OWNER))
        session = task['_executionSession']; session.mark_routed();session.mark_reserved();session.mark_admitted()
        assert runtime.mark_running(task['id'])
        baseline = PublishedRepositoryToolPort(root); candidate = PublishedRepositoryToolPort(root, group.for_root(OWNER,root))
        try:
            for i in range(4):
                if i == 3:(root/'module.py').write_text('def edited():\n    return changed\n')
                assert task_operation(baseline,task)==task_operation(candidate,task)
            before=len(chat_task_runtime.poll(task['id'])['events']); calls=[]
            result=candidate.invoke(task,{},lambda *args,**kw:calls.append(kw['status']),project_root=other)
            assert result[2] and calls==['error']
            events=chat_task_runtime.poll(task['id'])['events']
            assert len(events)==before+1
            tool_events=[e for e in events if e['type']=='tool_result']
            assert len(tool_events)==9 and tool_events[-1]['status']=='error'
            assert all(e['toolName']=='repo_map' and e['toolCallId'].startswith('repository-port-call-') for e in tool_events)
            assert 'original' not in tool_events[-2]['results'][0].get('snippet','')
        finally:
            end_task(runtime,task); group.close(OWNER)
        assert pool.snapshot()['reserved_bytes']==0
        assert chat_task_runtime.get(task['id']) is None
        assert chat_task_runtime.get(sentinel['id']) is sentinel
    chat_task_runtime.discard(sentinel['id'])
    print('real transient chat registration, registry handler, canonical publication: cold/warm/edit equality; error once; quota0; unrelated task preserved')


if __name__=='__main__':check()
