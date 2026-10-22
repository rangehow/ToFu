"""Benchmark adapter; native image lifetimes belong to lib.repo_map."""
from lib.repo_map._native_owned import (
    OwnedStreamScope as OwnedStreamScope,
    RuntimeStreamImage as RuntimeStreamImage,
)

def start_owned_workload(work, scope_factory):
    """Use the canonical task resource boundary in the explicit tool experiment."""
    from native_domain_worker import OWNER
    from publication_port import PublishedRepositoryToolPort
    from lib.agent_core.execution_session import bind_tool_environment

    if work.closed or work.task is not None:
        raise RuntimeError('repository workload cannot start another task')
    work.task = work.runtime.create(user_id=OWNER)
    scope = None
    bound = False
    try:
        scope = scope_factory(work.root, OWNER, work.task['abort_event'])
        session = work.task['_executionSession']
        bind_tool_environment(session, lambda: scope.close(OWNER))
        bound = True
        session.mark_routed(); session.mark_reserved(); session.mark_admitted()
        assert work.runtime.mark_running(work.task['id'])
        work.port = PublishedRepositoryToolPort(work.root, scope)
    except BaseException as primary:
        if scope is not None and not bound:
            try:
                scope.close(OWNER)
            except BaseException as cleanup:
                primary.add_note('unbound native scope cleanup: ' + type(cleanup).__name__)
        try:
            work.stop(True)
        except BaseException as cleanup:
            primary.add_note('native task setup cleanup: ' + type(cleanup).__name__)
        raise
