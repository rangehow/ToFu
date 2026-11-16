"""Canonical storage vocabulary for durable task-event streams.

This module is dependency-free so producers, the Sidecar schema operations,
maintenance, and read projections all consume the same machine-readable
classification instead of duplicating retention policy.
"""

from __future__ import annotations


EVENT_READ_GUARD_CONTRACT = 'event-read-guard/v1'
EVENT_READ_GUARD_SCOPED_CONTRACT = 'event-read-guard/v2'
EVENT_READ_GUARD_MAX_TYPES = 64
TASK_STREAM_KIND = 'task'
PROJECT_BRAIN_STREAM_KIND = 'project_brain'
TASK_EVENT_STREAMING_RETENTION_MS = 6 * 60 * 60 * 1000
TASK_EVENT_STRUCTURAL_RETENTION_MS = 30 * 24 * 60 * 60 * 1000

STREAM_KINDS = frozenset({
    TASK_STREAM_KIND,
    PROJECT_BRAIN_STREAM_KIND,
})

STRUCTURAL_EVENT_TYPES = frozenset({
    'messages_snapshot',
    'tool_wire_projection',
    'round_usage',
    'round_start',
    'round_end',
    'flow_iteration',
})

TERMINAL_EVENT_TYPES = frozenset({
    'done',
    'error',
    'aborted',
    'interrupted',
})

# An attempt event whose diagnostic evidence frame is held by the task-event row
# committed in the same transaction names that row under this key instead of
# storing the frame again: ``{'taskId': str, 'sequence': int}``. Only rows of the
# structural retention class are named, because they outlive conversation
# replay (TASK_EVENT_STRUCTURAL_RETENTION_MS >= the sync replay retention).
ATTEMPT_EVIDENCE_REFERENCE_KEY = 'eventRef'


def attempt_evidence_reference(body):
    """``(task_id, sequence)`` named by an attempt-event payload, or ``None``."""
    reference = body.get(ATTEMPT_EVIDENCE_REFERENCE_KEY) if isinstance(body, dict) else None
    if not isinstance(reference, dict):
        return None
    task_id, sequence = reference.get('taskId'), reference.get('sequence')
    if not isinstance(task_id, str) or not task_id or type(sequence) is not int or sequence < 0:
        return None
    return task_id, sequence


__all__ = [
    'ATTEMPT_EVIDENCE_REFERENCE_KEY',
    'EVENT_READ_GUARD_CONTRACT',
    'EVENT_READ_GUARD_SCOPED_CONTRACT',
    'EVENT_READ_GUARD_MAX_TYPES',
    'PROJECT_BRAIN_STREAM_KIND',
    'STREAM_KINDS',
    'STRUCTURAL_EVENT_TYPES',
    'TASK_EVENT_STREAMING_RETENTION_MS',
    'TASK_EVENT_STRUCTURAL_RETENTION_MS',
    'TASK_STREAM_KIND',
    'TERMINAL_EVENT_TYPES',
    'attempt_evidence_reference',
]
