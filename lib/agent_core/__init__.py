"""lib.agent_core — The reusable agent base, as one browsable package.

What this is
------------
``lib/agent_core/`` is the **human-readable mirror** of the core/plugin
boundary declared in :mod:`lib.agent_core_manifest`.  Open this package and you
see, in one place, exactly what constitutes Tofu's reusable foundation — the
run loop, model dispatch, swarm scheduling, the Planner→Worker→Critic endpoint
loop, context compaction, the push hub, the background-task runtime, and
capability profiles — together with the two registry seams through which the
base reaches plugins.

Why a facade instead of physically moving the files
----------------------------------------------------
The core is a *cross-cutting subset* of code that lives inside packages
(``tasks_pkg``, ``llm_dispatch``, ``swarm``) which also hold non-core
siblings.  A literal file move would either (a) leave ``agent_core →
tasks_pkg`` back-imports — inverting the very dependency direction the split
advertises — or (b) drag the whole sibling graph along until "core" stops
mirroring the manifest.  And it would rewrite ~960 import sites.

So the base keeps living where it is, and THIS package re-exports its public
surface.  Anyone browsing ``lib/`` learns the base/plugin split immediately;
existing imports keep working unchanged; the machine-readable source of truth
stays :mod:`lib.agent_core_manifest`, enforced by
``tests/test_agent_core_boundary.py``.

Recommended usage
-----------------
New base-level call sites may import from here for clarity::

    from lib.agent_core import run_task, dispatch_chat, apply_profile

The underlying modules (``lib.tasks_pkg.orchestrator`` etc.) remain valid and
are what the rest of the codebase already uses — this facade does not replace
them, it *names* them.

Membership
----------
``CORE_MEMBERS`` below maps each public symbol to its defining module, so the
mapping is introspectable and testable.  It is kept consistent with
:data:`lib.agent_core_manifest.CORE_MODULES` by
``tests/test_agent_core_boundary.py``.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Orchestration: the run loop + endpoint (Planner→Worker→Critic) ──
from lib.tasks_pkg.orchestrator import run_task
from lib.tasks_pkg.model_config import _assemble_tool_list
from lib.tasks_pkg.compaction import run_compaction_pipeline

# ── Model communication + routing / load balancing ──
from lib.llm import build_body, chat, stream_chat
from lib.llm_dispatch import dispatch_chat, dispatch_stream, get_dispatcher, reset_dispatcher

# ── Background-task runtime + real-time push hub ──
from lib.task_runtime import TaskRuntime
from lib.push import hub, push_event

# ── Capability profiles ──
from lib.agent_profiles import apply_profile, get_profile, list_profiles, resolve_profile_name

# ── Registry seams: how the base reaches plugins (no concrete plugin imports) ──
from lib.tools.registry import ToolContext, ToolSpec, assemble_tool_list, register_tool_spec
from lib.llm_dispatch.provider_registry import BodyDialect, register_dialect


# ── Introspectable membership map (symbol → defining module) ──
# Kept in lockstep with lib.agent_core_manifest.CORE_MODULES by the boundary test.
CORE_MEMBERS: dict[str, str] = {
    'run_task':                'lib.tasks_pkg.orchestrator',
    '_assemble_tool_list':     'lib.tasks_pkg.model_config',
    'run_compaction_pipeline': 'lib.tasks_pkg.compaction',
    'build_body':              'lib.llm',
    'chat':                    'lib.llm',
    'stream_chat':             'lib.llm',
    'dispatch_chat':           'lib.llm_dispatch',
    'dispatch_stream':         'lib.llm_dispatch',
    'get_dispatcher':          'lib.llm_dispatch',
    'reset_dispatcher':        'lib.llm_dispatch',
    'TaskRuntime':             'lib.task_runtime',
    'hub':                     'lib.push',
    'push_event':              'lib.push',
    'apply_profile':           'lib.agent_profiles',
    'get_profile':             'lib.agent_profiles',
    'list_profiles':           'lib.agent_profiles',
    'resolve_profile_name':    'lib.agent_profiles',
    # Registry seams (the only bridge core uses to reach plugins)
    'ToolSpec':                'lib.tools.registry',
    'ToolContext':             'lib.tools.registry',
    'assemble_tool_list':      'lib.tools.registry',
    'register_tool_spec':      'lib.tools.registry',
    'BodyDialect':             'lib.llm_dispatch.provider_registry',
    'register_dialect':        'lib.llm_dispatch.provider_registry',
}

__all__ = list(CORE_MEMBERS.keys())
