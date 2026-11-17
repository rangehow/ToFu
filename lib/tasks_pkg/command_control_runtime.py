"""Compose local command controls with the selected resident execution owner.

The manager's existing private process record locates the remote incarnation.
Application startup supplies the project explicitly; requests carry their own
owner. Reading that record neither creates tasks nor selects a storage backend.
"""
import sys

from lib.tasks_pkg.command_controls import apply_command_control, merge_background_activity
from lib.log import get_logger
from lib.tasks_pkg.command_control_transport import CommandControlUnavailable, call

logger = get_logger(__name__)

_CLIENT_KEY = 'tofu_command_controls'


class CommandControls:
    def __init__(self, *, service=None):
        self._service = service

    def readiness(self) -> dict:
        """One verified snapshot distinguishes live startup from an unavailable owner."""
        if self._service is None:
            return {'state': 'ready', 'error': '', 'endpoint': None}
        record = self._service.record()
        if not record or not self._service.host_matches(record):
            return {'state': 'unavailable', 'error': 'The execution owner is unavailable'}
        if record.get('state') == 'starting':
            return {'state': 'starting', 'error': ''}
        if record.get('state') != 'ready':
            return {'state': 'unavailable', 'error': 'The execution owner is unavailable'}
        endpoint = record.get('commandControl')
        if (not isinstance(endpoint, dict)
                or endpoint.get('serviceId') != record.get('serviceId')):
            return {'state': 'unavailable', 'error': 'The execution control incarnation differs'}
        return {'state': 'ready', 'error': '', 'endpoint': endpoint}

    def _endpoint(self):
        readiness = self.readiness()
        if readiness['state'] != 'ready':
            raise CommandControlUnavailable(readiness['error'] or 'The execution owner is starting')
        return readiness['endpoint']

    def execute(self, method, task_id, *, user_id, **params):
        result = apply_command_control(method, task_id, user_id=user_id, **params)
        if result is not None or self._service is None:
            return result
        return call(self._endpoint(), method, task_id, user_id=user_id, params=params)

    def background_activity(self, *, user_id):
        local = apply_command_control('background_activity', '', user_id=user_id)
        if self._service is None:
            return local
        remote = call(self._endpoint(), 'background_activity', '', user_id=user_id, params={})
        return merge_background_activity(local, remote)

    def execution_storage_rpc_metrics(self, *, user_id):
        if self._service is None:
            return None
        return call(self._endpoint(), 'storage_rpc_metrics', '', user_id=user_id, params={})

    def execution_mcp_reconcile(self, *, user_id, server, action):
        if self._service is None:
            return None
        return call(self._endpoint(), 'mcp_reconcile', '', user_id=user_id,
                    params={'server': server, 'action': action})


def configure(app, *, process_role, environ):
    service = None
    if process_role == 'api':
        from serverctl_pkg.execution_service import ExecutionService
        project = environ.get('TOFU_PROJECT_PATH')
        if not project:
            raise RuntimeError('A separate API needs its explicit execution project')
        service = ExecutionService(project, sys.executable)
    app.extensions[_CLIENT_KEY] = CommandControls(service=service)


def execute(app, method, task_id, *, user_id, **params):
    # Apps without a resident split retain the same local owner, including
    # embedding/test compositions that do not run production background boot.
    controls = app.extensions.get(_CLIENT_KEY)
    if controls is None:
        return apply_command_control(method, task_id, user_id=user_id, **params)
    return controls.execute(method, task_id, user_id=user_id, **params)


def execution_storage_rpc_metrics(app, *, user_id):
    """The resident execution host's storage RPC histogram; None without a split.

    Without a separate execution host this process runs every task, so its own
    ``tofu_storage_rpc_seconds`` already covers them.
    """
    controls = app.extensions.get(_CLIENT_KEY)
    if controls is None:
        return None
    return controls.execution_storage_rpc_metrics(user_id=user_id)


def execution_mcp_reconcile(app, *, user_id, server, action):
    """Repeat an MCP management action on the execution incarnation's bridge.

    The route already applied it to this process's bridge; Turns of a split
    deployment call tools through the execution host's, so it must follow.
    None without a split: this process's bridge serves every Turn.
    """
    controls = app.extensions.get(_CLIENT_KEY)
    if controls is None:
        return None
    return controls.execution_mcp_reconcile(user_id=user_id, server=server, action=action)


def background_activity(app, *, user_id):
    """The owner's live swarms and background commands in every process."""
    controls = app.extensions.get(_CLIENT_KEY)
    if controls is None:
        return apply_command_control('background_activity', '', user_id=user_id)
    return controls.background_activity(user_id=user_id)


def swarm_status(app, task_id, *, user_id, swarm_run_id='', agent_ids=()):
    """The owner's swarm status from the process that holds its session.

    The resident session answers, here first and then in the execution
    incarnation; only when neither holds it does the durable row answer, so a
    live session is never reported from its non-terminal row as unknown. An
    unconfirmed incarnation raises CommandControlUnavailable instead of
    falling back to the row. None means no trace of the swarm anywhere.

    A probe naming ``swarm_run_id`` is answered for that run only: the session
    and row of the conversation's later wave do not answer for an earlier run,
    and once neither holds the run, its ``agent_ids`` are answered from their
    durable run results (``recorded_run_status``).
    """
    from lib.swarm.integration import persisted_swarm_status, recorded_run_status

    def answers(status):
        return status is not None and (
            not swarm_run_id or status.get('swarm_run_id') == swarm_run_id)

    resident = execute(app, 'swarm_status', task_id, user_id=user_id)
    if answers(resident):
        return resident
    status = resident
    if status is None:
        # The durable row belongs to the session a process would hold.
        status = persisted_swarm_status(task_id, user_id=user_id)
        if answers(status):
            return status
    if not swarm_run_id:
        answer = status
    elif not agent_ids:
        answer = None
    else:
        answer = recorded_run_status(task_id, swarm_run_id=swarm_run_id,
                                     agent_ids=agent_ids, user_id=user_id)
    if answer is None or answer.get('known') is False:
        # The browser settles a panel after repeated unknown answers; name
        # what was asked and what the resident session held at that moment.
        durable = status if status is not resident else None
        logger.info('[Swarm] status unknown task=%s run=%s agents=%s '
                    'resident_run=%s resident_active=%s durable_run=%s',
                    task_id, swarm_run_id or '-', ','.join(agent_ids) or '-',
                    (resident or {}).get('swarm_run_id', '-'),
                    (resident or {}).get('active', '-'),
                    (durable or {}).get('swarm_run_id', '-'))
    return answer
