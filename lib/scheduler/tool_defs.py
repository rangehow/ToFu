"""lib/scheduler/tool_defs.py — LLM tool schema definitions for the scheduler."""

# The task types a model may create or switch a row among. Built-in types
# (system maintenance) and service types (the Router Watchdog review) are
# created by their owning service and never change type: their handlers run
# system-scoped or deployment-wide work.
SCHEDULABLE_TASK_TYPES = ('command', 'python', 'prompt', 'agent')
# The fields schedule_manage 'update' may set. Poll/execution bookkeeping,
# conversation routing and agent tool settings stay with their writers.
TOOL_UPDATABLE_FIELDS = ('name', 'schedule', 'command', 'task_type', 'description', 'max_runtime')
# Project roles (a Worker, a Coordinator, the Global Router) schedule only
# wakes of their project's Coordinator (lib.scheduler.authority); a wake row's
# ``command`` is its grant, so an update never reaches it.
PROJECT_WAKE_TYPE = 'project_wake'
# What a project role creates (docs/modules/scheduling_ops.md, "Agent-created
# schedules"): a wake runs nothing, a prompt is one model call without tools,
# and code types run code, so only a creator holding code execution creates them.
ROLE_CODE_TASK_TYPES = ('command', 'python')
ROLE_CREATE_TASK_TYPES = (PROJECT_WAKE_TYPE, 'prompt', *ROLE_CODE_TASK_TYPES)
# The scheduler tools every Coordinator continuation and project Worker grant
# admits (lib.project_dispatch.scope.granted_tools).
ROLE_SCHEDULER_TOOL_NAMES = frozenset({'schedule_create', 'schedule_list', 'schedule_manage'})
# An agent-created prompt or code task carries its creator grant in
# ``tools_config`` under this key; only the scheduler writes it.
CREATOR_GRANT_KEY = 'creatorGrant'
# Set on an agent-created task the owner paused: no agent resumes it.
OWNER_PAUSE_KEY = 'ownerPaused'
ROLE_MANAGE_ACTIONS = ('disable', 'enable', 'delete', 'run', 'update', 'log')
ROLE_UPDATABLE_FIELDS = ('name', 'schedule', 'description')
# Delegated control of the owner's own, service and system rows.
DELEGATED_MANAGE_ACTIONS = ('disable', 'enable')

SCHEDULE_TOOL_CREATE = {
    "type": "function",
    "function": {
        "name": "schedule_create",
        "description": (
            "Register a calendar job that fires on a clock. "
            "Waiters use timer_create. Use local-time five-field cron "
            "`minute hour day month weekday` for recurrence, or "
            "`once:YYYY-MM-DD HH:MM` for one run and auto-disable. "
            "Examples: `*/5 * * * *`, `0 9 * * 1-5`. Cron repeats until "
            "disabled; max_executions=1 also makes it one-shot. For "
            "approximate times avoid :00/:30 contention and choose an "
            "off-minute such as :07; honor an exact user-specified time.\n"
            "Types: command=shell and python=code (either may be disabled by "
            "deployment), prompt=one LLM call without tools, "
            "agent=clock-triggered independent polls followed by a visible "
            "full-tool turn in the target conversation. Use agent for "
            "monitoring, recurring analysis, or event triggers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short human-readable task name."
                },
                "schedule": {
                    "type": "string",
                    "description": "Clock cron or once:YYYY-MM-DD HH:MM."
                },
                "command": {
                    "type": "string",
                    "description": (
                        "Shell/Python/prompt payload. For agent: required, never "
                        "empty; the instruction its turn executes when ready. Without "
                        "condition_command the poll LLM also decides readiness from it."
                    )
                },
                "task_type": {
                    "type": "string",
                    "enum": list(SCHEDULABLE_TASK_TYPES),
                    "description": (
                        "command (default), python, prompt, or proactive agent."
                    )
                },
                "description": {
                    "type": "string",
                    "description": "Optional documentation."
                },
                "max_runtime": {
                    "type": "integer",
                    "description": "Max seconds before killing (default 300, not used for 'agent')",
                    "default": 300
                },
                "target_conv_id": {
                    "type": "string",
                    "description": (
                        "Required for agent: target conversation ID; use current "
                        "for this conversation."
                    )
                },
                "tools_config": {
                    "type": "object",
                    "description": (
                        "Agent tool settings. Keys: "
                        "searchMode, fetchEnabled, projectPath, codeExecEnabled, "
                        "browserEnabled, memoryEnabled, imageGenEnabled. "
                        "Missing keys inherit target-conversation settings; "
                        "the model always follows the target conversation."
                    )
                },
                "max_executions": {
                    "type": "integer",
                    "description": "Agent auto-disable count; 0=unlimited (default), 1=one-shot.",
                    "default": 0
                },
                "expires_at": {
                    "type": "string",
                    "description": "Agent auto-disable ISO datetime."
                },
                "condition_command": {
                    "type": "string",
                    "description": (
                        "Agent-only decisive shell predicate, zero-LLM polls: exit 0 "
                        "(and a condition_regex stdout match, if set) runs command; "
                        "exit 1 or no match waits; other errors never act, and "
                        "sustained errors hand readiness to the poll LLM (hybrid). "
                        "Prefer for deterministic triggers."
                    )
                },
                "condition_regex": {
                    "type": "string",
                    "description": (
                        "Agent-only readiness regex over predicate stdout; omit to "
                        "use exit code 0."
                    )
                }
            },
            "required": ["name", "schedule", "command"]
        }
    }
}

SCHEDULE_TOOL_LIST = {
    "type": "function",
    "function": {
        "name": "schedule_list",
        "description": "List all scheduled tasks with their status, next run time, and execution history.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_disabled": {
                    "type": "boolean",
                    "description": "Include disabled tasks (default false)",
                    "default": False
                }
            }
        }
    }
}

SCHEDULE_TOOL_MANAGE = {
    "type": "function",
    "function": {
        "name": "schedule_manage",
        "description": (
            "Manage a scheduled task: run immediately, enable/disable, delete, or update.\n"
            "Actions: 'run' (trigger now), 'enable', 'disable', 'delete', 'update', 'log' (view execution log)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "enable", "disable", "delete", "update", "log"],
                    "description": "Management action"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (not needed for 'log' action)"
                },
                "updates": {
                    "type": "object",
                    "description": "Fields to update (for 'update' action): " + ", ".join(TOOL_UPDATABLE_FIELDS)
                }
            },
            "required": ["action"]
        }
    }
}

AWAIT_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "await_task",
        "description": (
            "Wait for another conversation's task to finish before continuing. "
            "Use this when you need to block until a long-running task in another "
            "conversation completes.\n\n"
            "You can also list all currently active (running) tasks to discover "
            "which conversations are busy.\n\n"
            "Actions:\n"
            "  'list'  — show all currently running tasks (no task_id needed)\n"
            "  'wait'  — block until the specified task finishes (requires task_id)\n"
            "  'status' — check status of a task without blocking (requires task_id)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "wait", "status"],
                    "description": "Action to perform"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID to wait for or check (not needed for 'list')"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait (default 600, max 3600)",
                    "default": 600
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between status checks (default 5)",
                    "default": 5
                }
            },
            "required": ["action"]
        }
    }
}

TIMER_TOOL_CREATE = {
    "type": "function",
    "function": {
        "name": "timer_create",
        "description": (
            "Start a readiness watcher that injects one continuation. "
            "For deferred follow-ups. Cron/once jobs use "
            "schedule_create. Durable single-shot: return "
            "immediately; do not wait or poll manually. Survives tasks and server restarts. "
            "timer_manage exposes evidence; ready starts a fresh authoritative turn with "
            "continuation_message and auto-disables when ready.\n"
            "Only self-resolving external conditions (CI/job/file/download), "
            "never a human-only action such as restarting/redeploying this Tofu "
            "server; stop and ask the user, verify after they act. Polls are "
            "independent with no cross-poll history and can use the same search, "
            "shell, and file tools as this task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "check_instruction": {
                    "type": "string",
                    "description": (
                        "Optional tool-capable poll LLM instruction: exact target/source, "
                        "ready/error criteria. Omit only with condition_command."
                    )
                },
                "continuation_message": {
                    "type": "string",
                    "description": (
                        "Required user message injected when ready for a full tool-capable turn. "
                        "Specify the next action."
                    )
                },
                "check_command": {
                    "type": "string",
                    "description": (
                        "Optional evidence command; output informs but does not decide readiness."
                    )
                },
                "condition_command": {
                    "type": "string",
                    "description": (
                        "Decisive shell predicate: exit 0 is ready, 1 waits, other exits error; "
                        "condition_regex matches successful stdout instead. Cheapest zero-LLM "
                        "for deterministic conditions. With check_instruction: hybrid "
                        "retains model verification."
                    )
                },
                "condition_regex": {
                    "type": "string",
                    "description": (
                        "Readiness regex over successful condition_command stdout; omit to "
                        "use exit code 0."
                    )
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between polls. Minimum 10. Default 60.",
                    "default": 60
                },
                "max_polls": {
                    "type": "integer",
                    "description": (
                        "Polls before exhausted; default 120, 0=unlimited (caution)."
                    ),
                    "default": 120
                }
            },
            "required": ["continuation_message"]
        }
    }
}

TIMER_TOOL_MANAGE = {
    "type": "function",
    "function": {
        "name": "timer_manage",
        "description": (
            "Manage Timer Watchers — cancel, check status, list active timers, "
            "or view the poll log."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["cancel", "status", "list", "log"],
                    "description": (
                        "'cancel' — cancel an active timer\n"
                        "'status' — get details of a specific timer\n"
                        "'list' — list all timers\n"
                        "'log' — view poll log for a timer"
                    )
                },
                "timer_id": {
                    "type": "string",
                    "description": "Timer ID (required for cancel/status/log)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max log entries to return (default 20)",
                    "default": 20
                }
            },
            "required": ["action"]
        }
    }
}

# AWAIT_TASK_TOOL stays defined (and dispatchable for conversation-latched
# schemas) but is retired from the emitted surface: the unified ``poll``
# tool absorbs cross-conversation waits.
SCHEDULER_TOOLS = [
    SCHEDULE_TOOL_CREATE,
    SCHEDULE_TOOL_LIST,
    SCHEDULE_TOOL_MANAGE,
    TIMER_TOOL_CREATE,
    TIMER_TOOL_MANAGE,
]

SCHEDULER_TOOL_NAMES = {
    'schedule_create',
    'schedule_list',
    'schedule_manage',
    'await_task',
    'timer_create',
    'timer_manage',
}


_ROLE_WITHHELD_TOOLS = frozenset({'timer_create', 'timer_manage', 'await_task'})
_ROLE_NAME_MAPS = ('_toolNamespaceByName', '_executableToolNamespaceByName', '_toolDiscoveryPolicyByName',
                   '_toolScriptSafeByName', '_executableToolSearchTextByName')


def _role_create_schema(role, *, code_execution=False):
    wake = ("project_wake (default) wakes your project's Coordinator once (once: only) with "
            "your purpose as its note" if role == 'worker' else
            "project_wake (default) wakes this conversation, your project's Coordinator, with "
            "its purpose as a nonhuman note at each due time")
    code = ("; command (shell) and python run code like your run_command, in your project "
            "root" if code_execution else
            "; code tasks (command, python) need code execution, which you do not hold: "
            "delegate a Worker that holds run_command to schedule one")
    description = (
        "Schedule recurring or one-time work on the owner's scheduler, under your own grant. "
        f"task_type: {wake}; prompt runs one model call without tools{code}. The task records "
        "you, your request and its purpose, appears in the owner's scheduled-task list, and "
        "runs until the owner, your lead or you stop it, also after your work ends. A retry "
        "with the same idempotency_key never schedules twice.")
    types = [kind for kind in ROLE_CREATE_TASK_TYPES
             if code_execution or kind not in ROLE_CODE_TASK_TYPES]
    return {'type': 'function', 'function': {
        'name': 'schedule_create', 'description': description,
        'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
            'name': {'type': 'string', 'description': 'Short title the owner and your lead see.'},
            'schedule': {'type': 'string', 'description': (
                'Local-time five-field cron `minute hour day month weekday`, or '
                'once:YYYY-MM-DD HH:MM.')},
            'task_type': {'type': 'string', 'enum': types,
                          'description': 'What runs when due (default project_wake).'},
            'purpose': {'type': 'string', 'description': (
                'Required: why this task exists and what it checks, for which request. The '
                'owner sees it beside your name; a wake delivers it as its note.')},
            'command': {'type': 'string', 'description': (
                'The prompt, shell command or Python code that runs; omit for a wake.')},
            'max_runtime': {'type': 'integer', 'description': (
                'Seconds before a code task is killed (default 300).')},
            'idempotency_key': {'type': 'string',
                                'description': 'Stable key of this task; a retry returns the same task.'},
        }, 'required': ['name', 'schedule', 'purpose', 'idempotency_key']}}}


def _role_manage_schema(role):
    subject = {'worker': 'a task you created',
               'coordinator': "every agent-created task of your project, yours and your Workers'",
               'router': "every agent-created task of the owner's projects (you lead their "
                         'Coordinators)'}[role]
    description = (
        f'Manage a scheduled task you control: {subject}. Actions: disable (pause), enable '
        '(resume), delete (cancel), run (fire now), update (' + ', '.join(ROLE_UPDATABLE_FIELDS)
        + '), log (recent runs). Other tasks are read-only for you; schedule_list marks '
        'the ones you control. A refusal changes nothing.')
    properties = {
        'action': {'type': 'string', 'enum': list(ROLE_MANAGE_ACTIONS)},
        'task_id': {'type': 'string', 'description': 'Task ID from schedule_list.'},
        'updates': {'type': 'object', 'description': 'For update: ' + ', '.join(ROLE_UPDATABLE_FIELDS)},
    }
    if role == 'router':
        description += (
            " The owner's own, service and system tasks (such as the nightly storage backup) "
            'accept only disable and enable, under the owner\u2019s delegation: pass '
            'owner_delegation (the reason and the owner trigger, exactly as restart_server '
            'takes them) and idempotency_key. Without the grant it is refused; every call is '
            "in the owner's audit log and a retry with the same key never acts twice.")
        from lib.router.contract import schema
        properties['owner_delegation'] = schema('ownerDelegationAuthority')
        properties['idempotency_key'] = {
            'type': 'string', 'description': 'Required with owner_delegation.'}
    return {'type': 'function', 'function': {
        'name': 'schedule_manage', 'description': description,
        'parameters': {'type': 'object', 'additionalProperties': False, 'properties': properties,
                       'required': ['action', 'task_id']}}}


def role_scheduler_tools(role, *, code_execution=False):
    """The scheduler schemas a project role sees, by name (``schedule_list`` unchanged).

    ``code_execution`` says whether the role's admitted catalog runs code
    (``run_command``); only then does ``schedule_create`` offer code tasks.
    """
    if role not in ('worker', 'coordinator', 'router'):
        raise ValueError(f'not a project role: {role!r}')
    return {'schedule_create': _role_create_schema(role, code_execution=code_execution),
            'schedule_list': SCHEDULE_TOOL_LIST,
            'schedule_manage': _role_manage_schema(role)}


def project_role_scheduler_surface(cfg, tools, *, role):
    """Replace the scheduler family with a project role's projection.

    Runs after the role's ceiling filtered the assembled surface, so it never
    adds a tool the ceiling did not admit. The ordinary schemas describe the
    owner's clock jobs; a role schedules only Coordinator wakes. Every
    task-owned name map follows: an admitted call is validated against the
    projected contract document, which keeps its registry permission and
    idempotency metadata. Readiness watchers and ``await_task`` are withheld.
    """
    import copy

    admitted = cfg.get('_executableToolCatalog') if isinstance(
        cfg.get('_executableToolCatalog'), list) else tools
    projected = role_scheduler_tools(role, code_execution=any(
        (tool.get('function') or {}).get('name') == 'run_command' for tool in admitted or ()))

    def project(catalog):
        result = []
        for tool in catalog or []:
            name = (tool.get('function') or {}).get('name')
            if name in _ROLE_WITHHELD_TOOLS:
                continue
            result.append(copy.deepcopy(projected[name]) if name in projected else tool)
        return result

    if isinstance(cfg.get('_executableToolCatalog'), list):
        cfg['_executableToolCatalog'] = project(cfg['_executableToolCatalog'])
    documents = cfg.get('_toolContractDocumentsByName')
    if isinstance(documents, dict):
        documents = {name: value for name, value in documents.items() if name not in _ROLE_WITHHELD_TOOLS}
        for name, schema in projected.items():
            if isinstance(documents.get(name), dict):
                document = copy.deepcopy(documents[name])
                function = schema['function']
                document.update(arguments_schema=copy.deepcopy(function['parameters']),
                                summary=function['description'], help=function['description'])
                documents[name] = document
        cfg['_toolContractDocumentsByName'] = documents
    for key in _ROLE_NAME_MAPS:
        if isinstance(cfg.get(key), dict):
            cfg[key] = {name: value for name, value in cfg[key].items() if name not in _ROLE_WITHHELD_TOOLS}
    if isinstance(cfg.get('_requiredDirectToolNames'), list):
        cfg['_requiredDirectToolNames'] = [name for name in cfg['_requiredDirectToolNames']
                                           if name not in _ROLE_WITHHELD_TOOLS]
    return project(tools)


def schedule_tools_without_current_conversation(tools, *, reason):
    """Describe scheduler tools for a task that owns no conversation turn.

    A headless request, a child invocation or a task without a conversation
    has no conversation a timer could continue or ``current`` could name
    (lib.tasks_pkg.conversation_delivery refuses both), so ``timer_create`` is
    withheld and the target hint must not offer ``current``. ``reason`` is the
    lower-case clause naming why. A project role's ``schedule_create`` names no
    target conversation, so it has no hint to rewrite. Other schemas are
    unchanged.
    """
    import copy

    projected = []
    for tool in tools:
        name = tool.get('function', {}).get('name')
        if name == 'timer_create':
            continue
        if (name == 'schedule_create'
                and 'target_conv_id' in tool['function']['parameters']['properties']):
            tool = copy.deepcopy(tool)
            tool['function']['parameters']['properties']['target_conv_id'][
                'description'] = (
                f'Required for agent: an existing conversation ID. `current` '
                f'is unavailable: {reason}.')
        projected.append(tool)
    return projected


__all__ = [
    'SCHEDULE_TOOL_CREATE', 'SCHEDULE_TOOL_LIST', 'SCHEDULE_TOOL_MANAGE',
    'AWAIT_TASK_TOOL', 'TIMER_TOOL_CREATE', 'TIMER_TOOL_MANAGE',
    'SCHEDULER_TOOLS', 'SCHEDULER_TOOL_NAMES', 'PROJECT_WAKE_TYPE',
    'ROLE_CODE_TASK_TYPES', 'ROLE_CREATE_TASK_TYPES', 'ROLE_SCHEDULER_TOOL_NAMES', 'CREATOR_GRANT_KEY',
    'OWNER_PAUSE_KEY',
    'ROLE_MANAGE_ACTIONS', 'ROLE_UPDATABLE_FIELDS', 'DELEGATED_MANAGE_ACTIONS',
    'project_role_scheduler_surface', 'role_scheduler_tools',
    'schedule_tools_without_current_conversation',
]
