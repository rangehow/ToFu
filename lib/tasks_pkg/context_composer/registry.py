"""Closed policy inventory for automatic conversational context.

Entry points: ``context_block`` for snapshots; ``injection_spec`` for runtime
carriers. Source domains acquire evidence and decide their documented trigger;
this registry alone owns placement, authority, budgets and necessity metadata.
Execution permissions, inbox acknowledgements and compaction are NOT owned here.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from ._models import ContextBlock


@dataclass(frozen=True)
class InjectionSpec:
    id: str
    source: str
    necessity: str
    trigger: str
    lane: Literal['block', 'event', 'inbox', 'bootstrap', 'telemetry'] = 'block'
    authority: str = 'workflow'
    stability: str = 'turn'
    lifecycle: str = 'task'
    priority: int = 10
    max_tokens: int | None = None
    layer: str = 'hot_tail'
    required: bool = False
    note_kind: str = ''


_SPECS = (
    InjectionSpec('platform_static', 'system_prompt_cc', 'required_contract',
                  'Platform contract unless operator supplied replace-mode system',
                  authority='platform', stability='static', lifecycle='conversation',
                  priority=0, layer='objective_constraints', required=True),
    InjectionSpec('role_directive', 'endpoint.role', 'required_contract',
                  'Explicit specialized role', stability='round', lifecycle='round',
                  priority=0, max_tokens=5000, layer='objective_constraints', required=True),
    InjectionSpec('project_rules', 'project.AGENTS_CLAUDE', 'required_contract',
                  'Attached enabled project; rules only, history is on demand',
                  authority='project', stability='conversation', lifecycle='conversation',
                  priority=0, layer='objective_constraints', required=True),
    InjectionSpec('user_context', 'memory.user_context', 'user_preference',
                  'Preferences enabled and nonempty', authority='preference',
                  stability='conversation', lifecycle='conversation', priority=20,
                  max_tokens=1000, layer='objective_constraints'),
    InjectionSpec('memory_guidance', 'memory', 'capability_guidance',
                  'Memory enabled and real tools available', authority='ambient',
                  stability='conversation', lifecycle='conversation', priority=60,
                  max_tokens=180, layer='cold_history'),
    InjectionSpec('skills_index', 'skills.registry', 'capability_discovery',
                  'Real tools and enabled installed skills; frozen per task',
                  stability='conversation', lifecycle='conversation', priority=40,
                  max_tokens=1400, layer='cold_history'),
    InjectionSpec('credential_vault', 'credentials_vault', 'capability_discovery',
                  'Real tools and nonempty name-only vault index', authority='user',
                  stability='conversation', lifecycle='conversation', priority=30,
                  max_tokens=1000, layer='cold_history'),
    InjectionSpec('parallel_execution', 'swarm', 'capability_guidance',
                  'Enabled available multi-agent lane and independent task shape',
                  stability='static', lifecycle='conversation', priority=50,
                  max_tokens=128, layer='cold_history'),
    InjectionSpec('programmatic_execution', 'tools.programmatic', 'capability_guidance',
                  'Enabled programmatic tool lane', stability='static', lifecycle='conversation',
                  priority=55, max_tokens=128, layer='cold_history'),
    InjectionSpec('project_context', 'project.brain', 'required_coordination',
                  'Attached storage-backed project with current coordination state',
                  authority='project', max_tokens=1800, layer='objective_constraints', required=True),
    InjectionSpec('active_checklist', 'task.todo_state', 'required_continuity',
                  'Accepted current checklist missing from visible context',
                  priority=5, layer='task_state', required=True),
    InjectionSpec('task_state_v1', 'task.events.projection', 'required_continuity',
                  'Explicit global context plan enabled', priority=5, max_tokens=2400,
                  layer='task_state', required=True),
    InjectionSpec('relevant_memories', 'memory.prefetch', 'retrieved_evidence',
                  'High-confidence owner-scoped prefetch matches', authority='evidence',
                  priority=70, max_tokens=1500, layer='evidence'),
    InjectionSpec('plan_mode', 'plan_mode.contract', 'required_contract',
                  'Plan mode explicitly enabled', priority=15, max_tokens=900,
                  layer='objective_constraints', required=True),
    InjectionSpec('environment', 'platform.environment', 'required_routing',
                  'Current execution environment unless explicitly disabled',
                  authority='platform', priority=80, max_tokens=400, required=True),
    InjectionSpec('project_path_change', 'platform.environment.transition', 'required_routing',
                  'Observed path transition, never first sight or steady state',
                  authority='platform', priority=79, max_tokens=160, required=True),
    InjectionSpec('mcp_tools_delta', 'mcp.catalog', 'capability_discovery',
                  'Reachable MCP names absent from frozen wire and schema discovery available',
                  priority=40, max_tokens=700, layer='evidence'),
    InjectionSpec('disclosed_tools', 'tools.disclosure', 'capability_recovery',
                  'Previously disclosed deferred names remain executable',
                  priority=40, max_tokens=700, layer='evidence'),
    InjectionSpec('current_date', 'clock', 'ambient_fact', 'Current day changed or absent',
                  authority='ambient', priority=90, max_tokens=80),
    InjectionSpec('modified_files', 'orchestrator.attachments', 'state_reminder',
                  'Write burst ended and no reminder since latest write', authority='ambient',
                  stability='round', lifecycle='round', max_tokens=1600),
    InjectionSpec('tool_progress', 'orchestrator.tool_loop_breaker', 'safety_recovery',
                  'Verified identical outcome, explicit no-op, redundant read or unresolved artifact',
                  lane='event', required=True, note_kind='system-intervention'),
    InjectionSpec('tool_stagnation', 'orchestrator.tool_loop_breaker', 'safety_recovery',
                  'Persistent identical failure or identical successful polling; bounded correction',
                  lane='event', required=True, note_kind='system-intervention'),
    InjectionSpec('partial_stream_retry', 'stream_handler.analyse', 'protocol_recovery',
                  'Partial transport cut on a provider without assistant prefill',
                  lane='event', required=True, note_kind='system-intervention'),
    InjectionSpec('terminal_recovery', 'stream_handler.terminal_recovery', 'protocol_recovery',
                  'Empty terminal response or unexecuted tool packet; at most two retries',
                  lane='event', required=True, note_kind='system-intervention'),
    InjectionSpec('intermediate_response', 'stream_handler.analyse', 'protocol_recovery',
                  'Provider explicitly marks a commentary-only response; bounded retries',
                  lane='event', required=True, note_kind='system-intervention'),
    InjectionSpec('todo_incomplete', 'stream_handler.analyse', 'required_continuity',
                  'Actionable incomplete accepted checklist at stop, no human handoff',
                  lane='event', required=True, note_kind='todo-continuation'),
    InjectionSpec('intent_stall', 'stream_handler.intent_stall', 'safety_recovery',
                  'Retryable tool failure followed by prose-only stop, no human handoff; once per task',
                  lane='event', required=True, note_kind='intent-stall'),
    InjectionSpec('fallback_synthesis', 'orchestrator.finalize', 'result_recovery',
                  'Fetched evidence but no final answer', lane='event', authority='evidence', required=True),
    InjectionSpec('inbox_delivery', 'orchestrator.inbox', 'external_delivery',
                  'Pending swarm/peer/human/background/overlap payload at closed tool boundary; ACK stays with producer',
                  lane='inbox', authority='evidence', required=True),
    InjectionSpec('url_prefetch', 'message_builder.prefetch', 'retrieved_evidence',
                  'Successful prefetch of user-provided URL', lane='event', authority='evidence'),
    InjectionSpec('lifecycle_recovery', 'conv_message_builder.load', 'required_safety',
                  'Interrupted lifecycle command has unknown outcome; do not blindly refire',
                  lane='event', required=True),
    InjectionSpec('branch_context', 'conv_message_builder.load', 'user_projection',
                  'Explicit branch title/selection', lane='event', authority='user', required=True),
    InjectionSpec('paper_tool_guidance', 'paper.tools', 'capability_guidance',
                  'Paper schema discovery and execution gateways both available', lane='event'),
    InjectionSpec('worker_policy', 'swarm.agent', 'required_contract',
                  'Scoped worker role/tool/read-only/shared-workspace policy before dialogue',
                  lane='bootstrap', authority='platform', required=True),
    InjectionSpec('worker_artifacts', 'swarm.agent', 'capability_discovery',
                  'Shared artifact store available; names only, bounded', lane='event', authority='evidence'),
    InjectionSpec('worker_dependencies', 'swarm.scheduler', 'delegated_evidence',
                  'Explicit prerequisite completed', lane='event', authority='evidence', required=True),
    InjectionSpec('worker_wrapup', 'swarm.agent', 'result_recovery',
                  'Worker halted without final answer; tool-less honest partial report', lane='event', required=True),
    InjectionSpec('virtual_user_directive', 'autopilot', 'required_contract',
                  'Explicit autopilot simulated-user turn; pinned objective and role only',
                  lane='event', required=True),
    InjectionSpec('context_budget', 'compaction.pipeline', 'telemetry_only',
                  'Working-message estimate crosses diagnostic threshold; compaction owns capacity', lane='telemetry'),
)
INJECTION_REGISTRY = MappingProxyType({spec.id: spec for spec in _SPECS})
if len(INJECTION_REGISTRY) != len(_SPECS):
    raise ValueError('Duplicate context injection policy ID')


def injection_spec(injection_id: str) -> InjectionSpec:
    """Unknown production IDs fail closed rather than silently bypass policy."""
    return INJECTION_REGISTRY[injection_id]


def context_block(injection_id: str, content: str, *, instance: str = '',
                  reason: str = '', provenance: dict | None = None,
                  observed_at_ms: int = 0, world_version: str = '',
                  recovery_handle: str = '') -> ContextBlock:
    spec = injection_spec(injection_id)
    if spec.lane != 'block':
        raise ValueError(f'{injection_id} is not a snapshot block')
    if instance and (len(instance) > 64 or not all(c.isalnum() or c in '_-' for c in instance)):
        raise ValueError('Invalid context instance identity')
    return ContextBlock(
        id=spec.id + ('_' + instance if instance else ''), source=spec.source,
        content=content or '', authority=spec.authority, placement='tail',
        stability=spec.stability, lifecycle=spec.lifecycle, priority=spec.priority,
        max_tokens=spec.max_tokens, layer=spec.layer, required=spec.required,
        suppressed_reason=reason, provenance=provenance or {},
        observed_at_ms=observed_at_ms, world_version=world_version,
        recovery_handle=recovery_handle,
    )


def block_spec(block_id: str) -> InjectionSpec | None:
    """Resolve registered snapshot identities; custom renderer inputs stay pure."""
    spec = INJECTION_REGISTRY.get(block_id)
    if spec:
        return spec
    for prefix in ('project_path_change', 'modified_files'):
        if block_id.startswith(prefix + '_'):
            return INJECTION_REGISTRY[prefix]
    return None
