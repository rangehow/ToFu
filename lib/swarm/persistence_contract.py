"""Durable swarm-session status vocabulary shared with the Storage Sidecar.

This module is dependency-free on purpose. The swarm runtime and the Sidecar
operation catalog both import it, while neither becomes an owner of the
other's implementation details.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, NotRequired, TypedDict


SWARM_DELETE_EXACT_OPERATION = 'swarm.session.delete_exact'
SWARM_LIFECYCLE_GET_OPERATION = 'swarm.session.lifecycle.get'
SWARM_CONTINUE_REBIND_MODE = 'continue_rebind'
SwarmSessionSaveMode = Literal['launch', 'update', 'roster', 'admit', 'continue_rebind']
SwarmDisposition = Literal['ordinary_retirement', 'stop', 'replacement']
SWARM_DISPOSITION_PRIORITY: dict[SwarmDisposition, int] = {
    'ordinary_retirement': 0, 'stop': 1, 'replacement': 1,
}
SWARM_MAILBOX_DISPOSITION_FIELD = 'runDisposition'


class SwarmRunIdentity(TypedDict):
    swarm_key: str
    run_id: str
    owner_id: int
    tenant_id: str
    conversation_id: str
    project_path: str


class SwarmContinueRebindRequest(SwarmRunIdentity):
    save_mode: Literal['continue_rebind']
    expected_updated_at: int
    turn_id: str
    attempt_id: str
    caller_task_id: str
    config: dict
    activation_failed: NotRequired[Literal[True]]


class SwarmContinueRebindResult(TypedDict):
    rebound: Literal[True]
    checkpoint_writer_epoch: int


class SwarmContinueActivationFailureResult(TypedDict):
    failed_activation_recorded: Literal[True]


class SwarmDeleteExactRequest(SwarmRunIdentity):
    disposition: SwarmDisposition


class SwarmDeleteExactResult(TypedDict):
    deleted: bool
    mailboxes_changed: int


def swarm_mailbox_key(owner: int, tenant: str, conversation: str, run: str, agent: str) -> str:
    return hashlib.sha256(json.dumps([owner, tenant, conversation, run, agent],
                                     separators=(',', ':')).encode()).hexdigest()


def swarm_run_result_key(owner: int, run: str, agent: str) -> str:
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode('utf-8')).hexdigest()
    return f'{owner}:{digest(run)}:{digest(agent)}'


SWARM_SESSION_STATUS_RUNNING = "running"
SWARM_SESSION_STATUS_TERMINATED = "terminated"
SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS = "quarantined:ownerless"

# Only ordinary lifecycle code may write these states. Quarantine has a
# dedicated semantic operation so a caller cannot bypass its owner check.
SWARM_SESSION_WRITABLE_STATUSES = frozenset({
    SWARM_SESSION_STATUS_RUNNING,
    SWARM_SESSION_STATUS_TERMINATED,
})

SWARM_SESSION_TERMINAL_STATUSES = frozenset({
    SWARM_SESSION_STATUS_TERMINATED,
    SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS,
})

# ``swarm.agent.checkpoint.mark`` overlays only these compact result fields on
# an agent's last full checkpoint. Values are JSON scalars and strings stay
# within the text bound, so a marker never grows with the transcript.
# contracts/tofudb_ir_v1.json declares the same vocabulary for native TofuDB.
SWARM_CHECKPOINT_MARKER_RESULT_FIELDS = (
    "actuals_complete",
    "checkpoint_phase",
    "completion_tokens",
    "cost_usd",
    "elapsed_seconds",
    "prompt_tokens",
    "rounds_used",
    "total_tokens",
)
SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS = 64

# Durable per-run agent outcomes. The resumable session/agent rows above are
# keyed by the reusable conversation swarm key and are reclaimed at TTL
# eviction and new-wave replacement; these records are user state keyed by
# (owner, swarm_run_id, agent_id) and outlive both. They are portable generic
# records, so SQL and native TofuDB authorities store them identically.
SWARM_RUN_RESULT_NAMESPACE = "swarm.run_results.v1"
SWARM_RUN_RESULT_FORMAT = "tofu.swarm-run-result/v1"
# ``SubAgentResult`` fields a run record retains: the outcome, the final
# answer and its failure evidence, and usage. Transcripts stay resumable state.
SWARM_RUN_RESULT_FIELDS = (
    "status",
    "final_answer",
    "error_message",
    "contract_error",
    "recovery_reason",
    "elapsed_seconds",
    "rounds_used",
    "tool_calls_made",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
)


def swarm_archive_result(result: dict, *, rounds_used: int | None = None) -> dict:
    """Project checkpoint evidence identically for archival and verification."""
    value = {name: result[name] for name in SWARM_RUN_RESULT_FIELDS if name in result}
    if rounds_used is not None and (rounds_used or 'rounds_used' in value):
        value['rounds_used'] = rounds_used
    return value


__all__ = [
    "SWARM_SESSION_STATUS_RUNNING",
    "SWARM_SESSION_STATUS_TERMINATED",
    "SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS",
    "SWARM_SESSION_WRITABLE_STATUSES",
    "SWARM_SESSION_TERMINAL_STATUSES",
    "SWARM_CHECKPOINT_MARKER_RESULT_FIELDS",
    "SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS",
    "SWARM_RUN_RESULT_NAMESPACE",
    "SWARM_RUN_RESULT_FORMAT",
    "SWARM_RUN_RESULT_FIELDS",
]
