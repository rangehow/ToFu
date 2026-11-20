"""Dispatch-only Coordinator policy shared by catalog and execution admission.

The owner-scoped project binding selects the role, never a model/request flag.
A selected turn remains restricted if the binding changes mid-turn. Independent
worker conversations use ordinary task admission; Swarm children are not the
Coordinator execution route. The Global Router (docs/ROUTER.md) is the bound
Coordinator of the owner's router home; coordinator_tool_ceiling widens only
its ceiling to lib.router.policy.ROUTER_TOOLS through these same functions.
The Router Watchdog (R7) is the bound Coordinator of the owner's watchdog path;
its ceiling is the narrower lib.router.watchdog.policy.WATCHDOG_TOOLS.
"""
from __future__ import annotations

from lib.llm.llm_errors import AbortedError
from lib.log import get_logger
from lib.tools.execution_timing import timing_phase

logger = get_logger(__name__)

COORDINATOR_TOOLS = frozenset({
    'get_conversation_status', 'control_conversation',
    'ask_human', 'todo_write', 'list_conversations', 'get_conversation',
    'create_conversation', 'dispatch_project_assignment', 'get_project_assignment', 'report_to_user',
    # poll reaches only this role's Workers (lib.tasks_pkg.handlers.poll).
    'delegate_project_work', 'poll', 'review_project_work',
    'get_project_workers', 'cancel_project_work', 'finish_project_request',
    'update_task_progress', 'send_project_message', 'get_project_messages', 'consider_project_message',
    # Every project lead's directory of the owner's other leads and resource registry (docs/ROUTER.md R13).
    'list_project_leads',
    # Registers a device-online wake of this conversation; it executes nothing on the device.
    'await_device',
    # Check-ins that wake this Coordinator and control of its project's wakes
    # (lib.scheduler.authority; docs/modules/scheduling_ops.md).
    'schedule_create', 'schedule_list', 'schedule_manage',
    # An oversized result arrives as a partial envelope whose only recovery is
    # these readers; without them the role receives an unreadable pointer.
    'read_tool_artifact', 'search_tool_artifact',
    'search_tools', 'execute_tools',
    # Read-only owner-memory lookup (mode=owner, memory_id).
    'search_memories',
    # A lead's one memory write: record a lesson (new or a repeat) as an agent
    # report bound to its Turn and role (lib/tasks_pkg/handlers/memory.py).
    # Correcting, deleting and merging lessons stay with Workers.
    'create_memory',
    # Every lead restarts under the owner's full delegation (docs/ROUTER.md R9,
    # owner decision 2026-11-18); the gate refuses without it.
    'restart_server', 'repair_project',
    # A Project Router messages the owner's own command chat directly (R9, "Leads reach
    # the owner directly"); lib.channels.tools refuses every other thread.
    'channel_send',
})
# The Project Coordinator's direct core: its role tools in recorded-use order until
# core plus the discovery pair fill the Sankuai Claude tool-name budget. Recorded
# use is the three Project Coordinators' calls (2026-10-16..11-08), where poll
# inherits the calls of await_project_workers it absorbed; for the artifact
# readers, which this ceiling refused until 2026-11-08, it is the partial results
# that named them as their recovery. Two tools are direct by owner request rather
# than use: list_project_leads (R13: leads reach each other without the owner) and
# search_memories (a model must see that it has memory). They displace the core's
# least used tool, dispatch_project_assignment (4 recorded calls, only for
# already-authorized assignments; 2026-11-13). The Global Router keeps its own core
# (lib.router.policy.ROUTER_DIRECT_TOOLS) and every Watchdog tool is direct. Other
# role tools stay eager but may be hidden behind search_tools/execute_tools on that
# route (docs/LLM_COST_OPTIMIZATION.md section 6, "Coordinator-role surfaces").
COORDINATOR_DIRECT_TOOLS = frozenset({
    'poll', 'delegate_project_work', 'send_project_message', 'review_project_work',
    'get_conversation', 'read_tool_artifact', 'search_tool_artifact', 'update_task_progress',
    'get_project_workers', 'consider_project_message', 'cancel_project_work', 'report_to_user',
    'get_conversation_status', 'get_project_assignment', 'get_project_messages', 'todo_write',
    'list_conversations', 'ask_human', 'finish_project_request', 'control_conversation',
    'list_project_leads', 'search_memories',
})


def coordinator_scope(task: dict, project_path: str | None = None) -> dict | None:
    """Resolve the authoritative binding, pinning restrictions for this turn.

    Lookup errors deliberately propagate: callers must fail closed, not silently
    restore general-purpose tools. Child invocations are not independent worker
    conversations and never inherit this conversational role. A project-dispatch
    task takes any role from its grant. Once bound, that is the immutable role
    ``bind_account`` pinned from the grant it validated, read without storage:
    model dispatch intents and tool admission enforce live authority before
    every effect. A lost carrier, and for an unbound task any failed grant
    validation, raises the dispatch AbortedError: the authority verdict, not a
    role lookup failure.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    if (is_child_invocation(task) or task.get('_parentTaskId')
            or task.get('_transientRuntime') or task.get('_invocationStorageFree')
            or (task.get('config') or {}).get('_storageFreeRuntime')):
        return None
    from lib.project_dispatch.admission import (
        granted_coordinator_scope, is_dispatch_task, validate_task,
    )
    if is_dispatch_task(task):
        bound, scope = granted_coordinator_scope(task)
        if bound:
            return scope
        validate_task(task, require_account=False)
        from lib.project_dispatch.continuation import current_continuation
        row = current_continuation(task)
        if row is None:
            return None
        return {'project_path': row['request']['projectPath'],
                'binding_revision': row['grant']['bindingRevision'],
                'conversation_id': row['request']['coordinatorId'], 'user_id': row['grant']['owner']}
    pinned = task.get('_coordinatorDispatchScope')
    if pinned:
        return dict(pinned)
    config = task.get('config') or {}
    path = config.get('canonicalProjectPath') or project_path or config.get('projectPath')
    if not path or not task.get('convId'):
        return None
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_identity import normalize_project_path
    from lib.tasks_pkg.manager import task_user_id
    owner = task_user_id(task)
    path = normalize_project_path(path)
    # The role is fixed for a Turn in both directions: a positive answer is
    # pinned above, and a negative one is remembered for the exact (owner,
    # conversation, project) it read. A binding made while the Turn runs takes
    # effect from the next Turn, as an unbinding already does.
    identity = (owner, task['convId'], path)
    unbound = task.get('_coordinatorUnboundScopes')
    if isinstance(unbound, tuple) and identity in unbound:
        return None
    binding = ProjectCoordinationRepository(owner).read(path)
    if (binding.get('availability') != 'available'
            or binding.get('conversationId') != task['convId']):
        task['_coordinatorUnboundScopes'] = (
            unbound if isinstance(unbound, tuple) else ()) + (identity,)
        return None
    result = {'project_path': path, 'binding_revision': binding['revision'],
              'conversation_id': task['convId'], 'user_id': owner}
    task['_coordinatorDispatchScope'] = result
    return dict(result)


def coordinator_tool_ceiling(scope: dict, task: dict | None = None) -> frozenset:
    """The tool names an active coordinator scope may use: one shared ceiling.

    ``task`` lets the Global Router narrow the ceiling of one Turn (a review of
    other people's channel messages, docs/CHANNELS.md C11).
    """
    from lib.router.policy import is_router_scope, router_tool_ceiling
    if is_router_scope(scope):
        return router_tool_ceiling(scope, task)
    from lib.router.watchdog.binding import is_watchdog_scope
    if is_watchdog_scope(scope):
        from lib.router.watchdog.policy import WATCHDOG_TOOLS
        return WATCHDOG_TOOLS
    return COORDINATOR_TOOLS


# The human's own tool settings, kept beside the role's narrowed ones: the
# Workers a request delegates run under the settings an ordinary conversation
# of this human would (docs/PROJECT_COORDINATION.md).
SUBMITTED_TOOL_SETTINGS_KEY = '_coordinatorSubmittedToolSettings'


def coordinator_tool_settings(config: dict) -> dict:
    """The request's tool settings under this role, which never runs programs."""
    from lib.context_experiment_flags import normalize_context_experiment_flags
    return {**normalize_context_experiment_flags(config)['tools'], 'programmaticCalling': 'off'}


def submitted_tool_settings(config: dict) -> dict:
    """The tool settings the human submitted before the role narrowed them."""
    from lib.context_experiment_flags import normalize_context_experiment_flags
    submitted = config.get(SUBMITTED_TOOL_SETTINGS_KEY)
    return normalize_context_experiment_flags(
        {'tools': submitted} if isinstance(submitted, dict) else config)['tools']


def pin_coordinator_request_policy(conversation_id: str, config: dict, owner_user_id: int) -> dict:
    """Apply the role's tool settings before model replay pins the request policy.

    Tool assembly enforces the same settings later. Applied only there, every
    human Coordinator checkpoint recorded the submitted settings instead of the
    ones the request ran under, unlike its continuations. Dispatch admission
    already fixed a continuation's settings. A failed role lookup keeps the
    submitted settings: replay stays conservative and tool assembly still
    fails closed. Asking the human is intrinsic to the role (like Plan Mode
    clarification), so ask_human never depends on the composer toggle.

    Scheduler-built turns stamp ``schedulerEnabled=True`` although no scheduler
    tool name is in this role's ceiling, so the flag is inert here (unlike an
    ordinary conversation, where it survives untouched). Pinning it keeps an
    owner/scheduled alternation from an authority-bearing drift that used to
    cold-rebuild the whole history on every other request.

    ``desktopEnabled`` is inert for the same reason: the owner's attended turn
    resolves it to True (conv_config), scheduled turns and continuations carry
    False, and no desktop tool name is in any role ceiling; await_device, the
    one device tool a Coordinator or Router holds, is offered beside
    delegate_project_work regardless of the flag.
    """
    from lib.project_dispatch.admission import KEY
    if KEY in config:
        return config
    try:
        active = coordinator_scope({'convId': conversation_id, 'config': config,
                                    '_userId': owner_user_id})
    except Exception:
        logger.warning('[Coordinator] role lookup failed at request ingress; '
                       'replay keeps the submitted tool settings', exc_info=True)
        return config
    if not active:
        return config
    return {**config, 'tools': coordinator_tool_settings(config), 'humanGuidanceEnabled': True,
            'schedulerEnabled': False, 'desktopEnabled': False,
            SUBMITTED_TOOL_SETTINGS_KEY: submitted_tool_settings(config)}


def coordinator_call_rejection(task: dict, name: str, arguments: dict,
                               project_path: str | None = None) -> str | None:
    """Return a denial before approval, prefetch, hooks, or tool execution.

    A dispatch authority abort propagates and ends the task; any other role
    lookup failure refuses only this call.
    """
    try:
        with timing_phase('coordinator_policy'):
            active = coordinator_scope(task, project_path)
    except AbortedError:
        raise
    except Exception:
        logger.warning('[Coordinator] policy lookup failed; refusing tool %s', name,
                       exc_info=True)
        return 'Coordinator policy unavailable; tool execution refused (fail closed).'
    if not active:
        return None
    try:
        ceiling = coordinator_tool_ceiling(active, task)
    except Exception:
        logger.warning('[Coordinator] tool ceiling lookup failed; refusing tool %s', name,
                       exc_info=True)
        return 'Coordinator policy unavailable; tool execution refused (fail closed).'
    if name in ceiling:
        # Batch transport is allowed, arbitrary program execution is not. Every
        # child still crosses the same pipeline/executor admission boundary.
        if name != 'execute_tools':
            return None
        calls = arguments.get('calls')
        if (isinstance(calls, list) and calls and not arguments.get('program')
                and all(isinstance(call, dict)
                        and call.get('name') not in {'execute_tools', 'search_tools'}
                        and call.get('name') in ceiling
                        for call in calls)):
            return None
    # A narrowed ceiling (lib.channels.assistant.ReviewCeiling) names what its Turn may do instead.
    if getattr(ceiling, 'denial', ''):
        return f'{name} is not allowed here. {ceiling.denial}'
    return (f'Coordinator dispatch-only policy: {name} is not allowed. '
            'Delegate the current human request with delegate_project_work, '
            'wait for independent worker results with poll, '
            'and follow evidence with get_project_assignment '
            'or get_conversation. Do not execute file, shell, browser, program, '
            'or temporary Swarm work in the Coordinator.')


def filter_coordinator_tools(task: dict, tools: list[dict] | None,
                             project_path: str | None = None) -> list[dict] | None:
    """Project the same policy before provider discovery can expose tools."""
    if not tools:
        return tools
    scope = coordinator_scope(task, project_path)
    if not scope:
        return tools
    ceiling = coordinator_tool_ceiling(scope, task)
    return [tool for tool in tools
            if (tool.get('function') or {}).get('name') in ceiling]


def prepare_coordinator_tool_surface(task: dict, cfg: dict, tools: list[dict] | None,
                                     project_path: str | None = None) -> list[dict] | None:
    """Keep this role's admitted tools eager, independent of intent/discovery.

    Only the role's direct core is required-direct: ``ROUTER_DIRECT_TOOLS`` for
    the Global Router, ``COORDINATOR_DIRECT_TOOLS`` for a Project Coordinator
    and every admitted Watchdog tool. The Router's and a Project Coordinator's
    whole surfaces exceed a route's tool-name budget, which may hide their
    other tools behind search_tools/execute_tools.
    Call after ordinary registry, owner, explicit selection and Plan filters.
    The existing executable catalog is the ceiling: this never builds schemas
    or restores a denied capability. Continuation grant filtering still runs
    afterward and every tool call retains its ordinary execution gates.
    """
    scope = coordinator_scope(task, project_path)
    if not scope:
        return tools
    ceiling = coordinator_tool_ceiling(scope, task)
    catalog = cfg.get('_executableToolCatalog')
    if not isinstance(catalog, list):
        catalog = tools or []
    admitted = [tool for tool in catalog
                if (tool.get('function') or {}).get('name') in ceiling]
    from lib.conversations.project_coordination import COORDINATOR_QUESTION_DISCIPLINE
    admitted = [
        {**tool, 'function': {**tool['function'], 'description':
            tool['function'].get('description', '').removesuffix(
                '\n' + COORDINATOR_QUESTION_DISCIPLINE) + '\n' + COORDINATOR_QUESTION_DISCIPLINE}}
        if tool['function']['name'] == 'ask_human' else tool
        for tool in admitted]
    allowed = {tool['function']['name'] for tool in admitted}
    cfg['_executableToolCatalog'] = admitted
    for key in ('_toolContractDocumentsByName', '_executableToolNamespaceByName',
                '_toolScriptSafeByName', '_executableToolSearchTextByName'):
        cfg[key] = {name: value for name, value in (cfg.get(key) or {}).items()
                    if name in allowed}
    cfg['_toolNamespaceByName'] = dict(cfg['_executableToolNamespaceByName'])
    cfg['_frontendSelectedToolNames'] = sorted(
        set(cfg.get('_frontendSelectedToolNames') or ()) & allowed)
    # Shared direct-wire projection protects these exact live schemas across
    # local/native discovery, schema budgets and replayed initial visibility.
    # Do not mark entire ToolSpec families eager: project_integration also owns
    # workspace mutation tools that the Coordinator must never acquire. Tools
    # outside a role's core stay eager but unprotected so a route's tool-name
    # budget can fit them (docs/LLM_COST_OPTIMIZATION.md section 6).
    from lib.router.policy import ROUTER_DIRECT_TOOLS, is_router_scope
    from lib.router.watchdog.binding import is_watchdog_scope
    core = (ROUTER_DIRECT_TOOLS if is_router_scope(scope)
            else allowed if is_watchdog_scope(scope) else COORDINATOR_DIRECT_TOOLS)
    cfg['_requiredDirectToolNames'] = sorted(allowed & core)
    cfg['_toolDiscoveryPolicyByName'] = {name: 'eager' for name in allowed}
    cfg['_toolSearchCatalogSize'] = len(admitted)
    cfg['_toolSearchableCount'] = 0
    return list(admitted)


def receives_owner_identity(task: dict) -> bool:
    """Whether this role may see the owner's My Context ``identity`` items.

    Delegated Workers and sub-agents never receive them (design record
    "Owner understanding", Decision 3): neither in the always-present profile
    nor through the owner-memory lookup. A failed role lookup withholds them.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task) or task.get('_parentTaskId'):
        return False
    try:
        from lib.project_dispatch.admission import is_dispatch_task
        if not is_dispatch_task(task):
            return True
        return bool(coordinator_scope(task))
    except Exception:
        logger.warning('[Coordinator] role lookup failed; identity items withheld', exc_info=True)
        return False


def runs_on_watchdog_path(task: dict, user_id: int | None,
                          project_path: str | None = None) -> bool:
    """Whether ``task`` works in the owner's Watchdog project: the Watchdog or its Workers.

    Pure: compares the task's project paths with the owner's watchdog path
    (``lib.router.watchdog.binding.is_watchdog_location``), no storage read.
    """
    owner = user_id or task.get('_userId')
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        return False
    from lib.router.watchdog.binding import is_watchdog_location
    config = task.get('config') or {}
    return any(isinstance(path, str) and path and is_watchdog_location(owner, path)
               for path in (config.get('canonicalProjectPath'), config.get('projectPath'),
                            project_path))
