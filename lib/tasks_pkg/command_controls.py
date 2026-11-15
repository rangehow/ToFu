"""Apply interactive controls at the process that owns TaskRuntime and its swarms.

Entry points accept an explicit owner and resolve the existing foreground
command slots, swarm sessions, production work units or pending human gates of
THIS process. They never create a process or treat request delivery as a
successful handoff; the command worker, swarm session, TaskRuntime and human
gate owner decide the actual result. ``None`` means this process holds no such
task, session or gate, so a split API asks the execution incarnation instead.
``storage_rpc_metrics`` reports this process's storage RPC histogram for the
API's admin ``/metrics``; it carries no owner data.
"""
import time

from lib.tasks_pkg.manager.runtime import chat_task_runtime


def _admit(deadline_unix_ms):
    if deadline_unix_ms is not None and deadline_unix_ms <= time.time_ns() // 1_000_000:
        raise TimeoutError('Command control expired before application')


def interrupt_command(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
    task = chat_task_runtime.get_owned(task_id, user_id=user_id)
    if task is None:
        return None
    if task.get('status') != 'running' or task.get('aborted'):
        return {'interrupted': False, 'reason': 'task_not_running'}
    pid = task.get('_subprocess_pid')
    if not pid:
        return {'interrupted': False, 'reason': 'no_active_command'}
    chat_task_runtime.update_fields(task_id, fields={'_cmd_interrupt': {
        'source': 'user', 'ts': time.time(), 'note': '', 'pid': pid,
    }}, only_if_status='running')
    return {'interrupted': True, 'pid': pid}


def background_command(task_id: str, *, user_id: int, round_num: int, tool_call_id: str,
                       deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
    task = chat_task_runtime.get_owned(task_id, user_id=user_id)
    if task is None:
        return None
    abort_event = task.get('abort_event')
    if (task.get('status') != 'running' or task.get('aborted')
            or (abort_event is not None and abort_event.is_set())):
        return {'backgrounded': False, 'reason': 'task_not_running'}
    control = task.get('_cmd_handoff_control')
    if control is None or control.task is not task:
        return {'backgrounded': False, 'reason': 'no_active_command'}
    options = {} if deadline_unix_ms is None else {'deadline_unix_ms': deadline_unix_ms}
    return control.request(round_num=round_num, tool_call_id=tool_call_id, **options)


def abort_swarm_session(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
    from lib.swarm.integration import abort_swarm
    result = abort_swarm(task_id, user_id=user_id)
    if result.get('success'):
        return {'aborted': True}
    if result.get('outcome') == 'failed':
        return {'aborted': False, 'reason': 'abort_failed'}
    return None


def stop_swarm_session_agent(swarm_id: str, *, user_id: int, agent_id: str,
                             deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
    from lib.swarm.integration import stop_swarm_agent
    result = stop_swarm_agent(swarm_id, agent_id, user_id=user_id)
    if result['outcome'] == 'not_found':
        return None
    if result['outcome'] == 'stopped':
        return {'stopped': True, 'status': result['status']}
    return {'stopped': False, 'reason': 'not_running', 'status': result['status']}


def swarm_session_status(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    """The owner's swarm session resident here, as the status route serves it."""
    _admit(deadline_unix_ms)
    from lib.swarm.integration import resident_swarm_status
    return resident_swarm_status(task_id, user_id=user_id)


def view_production_task(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    """This owner's live work unit here, as GET /api/v4/tasks/{id} projects it."""
    _admit(deadline_unix_ms)
    from lib.production.runtime import find_owned_production_task
    found = find_owned_production_task(task_id, user_id=user_id)
    if found is None:
        return None
    from lib.production.work_unit import project_work_unit
    from lib.task_replay.task_replay import public_task_view
    task = found[1]
    return {'task': public_task_view(task), 'workUnit': project_work_unit(task)}


def abort_production_task(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    """Signal this owner's live work unit here, exactly as the task API aborts."""
    _admit(deadline_unix_ms)
    from lib.production.runtime import find_owned_production_task
    found = find_owned_production_task(task_id, user_id=user_id)
    if found is None:
        return None
    runtime, task = found
    if not runtime.abort_owned(task_id, user_id=user_id):
        return {'aborting': False, 'status': str(task.get('status') or '')}
    if 'aborted' in task:
        task['aborted'] = True
    return {'aborting': True, 'status': 'aborting'}


def answer_guidance(guidance_id: str, *, user_id: int, response: str,
                    deadline_unix_ms: int | None = None):
    """Resolve a human gate waiting in THIS process through its existing owner."""
    _admit(deadline_unix_ms)
    from lib.tasks_pkg.human_guidance import is_human_guidance_pending, resolve_human_guidance
    if not is_human_guidance_pending(guidance_id, owner_user_id=user_id):
        return None
    return {'resolved': resolve_human_guidance(guidance_id, response, owner_user_id=user_id)}


def background_activity(*, user_id: int, deadline_unix_ms: int | None = None):
    """This process's live swarm sessions and running background commands."""
    _admit(deadline_unix_ms)
    from lib.swarm.integration import list_active_swarms
    from lib.tasks_pkg.background_commands import list_background_commands
    return {'swarms': list_active_swarms(user_id=user_id),
            'backgroundCommands': [item for item in list_background_commands(user_id=user_id)
                                   if item.get('status') == 'running']}


def storage_rpc_metrics(*, user_id: int, deadline_unix_ms: int | None = None):
    """This process's storage RPC latency histogram; it holds no owner data."""
    _admit(deadline_unix_ms)
    from lib.observability import storage_rpc_snapshot
    return {'histograms': storage_rpc_snapshot()}


def merge_background_activity(*views):
    """Union per-process views; one swarm run or command appears once."""
    merged = {'swarms': [], 'backgroundCommands': []}
    for field, identity in (('swarms', 'swarm_id'), ('backgroundCommands', 'command_id')):
        seen = set()
        for view in views:
            for row in view[field]:
                if row.get(identity) not in seen:
                    seen.add(row.get(identity))
                    merged[field].append(row)
    return merged


def apply_command_control(method, task_id, *, user_id, deadline_unix_ms=None, **params):
    if method == 'interrupt' and not params:
        return interrupt_command(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'background' and set(params) == {'round_num', 'tool_call_id'}:
        return background_command(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms, **params)
    if method == 'swarm_abort' and not params:
        return abort_swarm_session(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'swarm_agent_stop' and set(params) == {'agent_id'}:
        return stop_swarm_session_agent(task_id, user_id=user_id,
                                        deadline_unix_ms=deadline_unix_ms, **params)
    if method == 'swarm_status' and not params:
        return swarm_session_status(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'task_view' and not params:
        return view_production_task(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'task_abort' and not params:
        return abort_production_task(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'guidance_answer' and set(params) == {'response'}:
        return answer_guidance(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms, **params)
    if method == 'background_activity' and not params and task_id == '':
        return background_activity(user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'storage_rpc_metrics' and not params and task_id == '':
        return storage_rpc_metrics(user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    raise ValueError('Unsupported execution command control')
