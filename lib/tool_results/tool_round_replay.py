"""Causal replay rules for persisted tool-round occurrences.

Tool-call ids and local round counters are correlation metadata, not proof that
an execution happened.  In particular, an early ``tool_start`` from a provider
attempt that was later discarded is a transport artifact even though it lives
beside real tool rounds for UI settlement.

This module is the backend semantic boundary used by Continue/checkpoint paths:

* discarded provider-attempt artifacts are transparent;
* delegated audit receipts covered by an accepted container are not additional
  model actions; receipt identity and invocation scope establish coverage;
* a terminal error/rejection with an authoritative result is still an execution
  fact and is replayable (``status == 'done'`` is not required);
* a real call without a result or valid caller authority stops the causal
  prefix, so later calls are never replayed across an invented gap;
* every ``tool_use`` a replayed batch's native provider content announces is
  paired with an occurrence: one whose own row never reached the record (a
  lost or amputated sibling) gets a typed "result not recorded" receipt.

The helpers are pure except for caller dictionaries being copied and
canonicalized in returned round records.
"""

from __future__ import annotations

import json
import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.tool_results.tool_caller_identity import (
    MAX_TOOL_CALLER_ID_CHARS,
    normalize_tool_caller,
)
from lib.tool_results.tool_round_identity import (
    execution_batch_keys,
    execution_identity,
    execution_llm_round,
)


SUPERSEDED_PROVIDER_ATTEMPT_FIELD = "_providerAttemptDiscarded"
_MAX_REPLAY_TOOL_NAME_CHARS = 512
TOOL_REPLAY_PROJECTION_VERSION = 2
_DELEGATED_EXECUTION_SOURCES = frozenset({'execute_calls', 'execute_program'})

#: Model-visible receipt of a call its provider response announced but whose
#: own occurrence (and so its result) never reached the durable record.
UNRECORDED_TOOL_RESULT_TEXT = (
    "Result not recorded: this call's execution was interrupted before its "
    "result was durably saved, so its effect is unknown. Check its effect "
    "before relying on it or repeating it."
)
#: Machine marker on the occurrence that carries that receipt.
UNRECORDED_TOOL_RESULT_FIELD = "_resultNotRecorded"


def _receipt_scope(row: Mapping[str, Any]) -> tuple | None:
    """Receipt coverage needs a proven invocation and model response boundary."""
    attempt_id, task_id = execution_identity(row)
    llm_round = execution_llm_round(row)
    if not (attempt_id or task_id) or llm_round is None:
        return None
    return attempt_id, task_id, llm_round


def _receipt_id(value: Any) -> str | None:
    return (value if isinstance(value, str) and 0 < len(value) <= MAX_TOOL_CALLER_ID_CHARS
            else None)


def _container_receipt_keys(row: Mapping[str, Any]) -> set[tuple]:
    """Gateway containers own model replay of their child results.

    The explicit adapter marker comes from tool presentation, not argument/name
    equality. A key names a child call the container shows the model and, when
    the entry still carries one, its canonical result ID. The model-only
    projection strips ``resultId`` (``gateway_result_projection``), so a key
    without it is proven by the call ID alone. Retain no result bodies.
    Unknown/compacted payloads prove no coverage and keep the old replay path.
    """
    scope = _receipt_scope(row)
    if scope is None or row.get('_hiddenToolAdapter') is not True:
        return set()
    from lib.tools.gateway_result_projection import gateway_result_children
    content = row.get('toolContent')
    children = gateway_result_children(content)
    if children is None and isinstance(content, str):
        # A retained-artifact envelope shows the receipt as its summary; only
        # an untruncated summary is the exact receipt.
        from lib.tools.result_projection_text import load_text_field
        envelope = load_text_field(content, 'summary')
        if isinstance(envelope, dict) and envelope.get('truncated') is not True:
            children = gateway_result_children(envelope['summary'])
    keys = set()
    for child in children or ():
        call_id = _receipt_id(child.get('call_id'))
        if call_id is None or ('resultId' in child and _receipt_id(child['resultId']) is None):
            continue
        keys.add((*scope, call_id, child.get('resultId')))
    return keys


def is_delegated_execution_receipt(row: Mapping[str, Any]) -> bool:
    """A gateway child's audit row: executed for a container, never a model call."""
    source = row.get('source')
    return isinstance(source, str) and source in _DELEGATED_EXECUTION_SOURCES


def _is_covered_delegated_receipt(row: Mapping[str, Any], covered: set[tuple]) -> bool:
    if not is_delegated_execution_receipt(row):
        return False
    evidence = row.get('toolResultEvidence')
    result_id = _receipt_id(evidence.get('resultId') if isinstance(evidence, Mapping) else None)
    call_id = _receipt_id(row.get('toolCallId'))
    scope = _receipt_scope(row)
    return (scope is not None and result_id is not None and call_id is not None
            and ((*scope, call_id, result_id) in covered
                 or (*scope, call_id, None) in covered))


def model_tool_content(round_entry: Mapping[str, Any]) -> Any:
    """Return a detached model receipt, honoring explicit compaction boundaries."""
    if (isinstance(round_entry.get('toolModelContent'), list)
            and not round_entry.get('_persistCompacted')
            and round_entry.get('compactionLayer') != 'L1'):
        return copy.deepcopy(round_entry['toolModelContent'])
    return round_entry.get('toolContent') or ''


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def normalize_replay_tool_arguments(value: Any) -> tuple[str | None, bool]:
    """Return safe JSON-object text and whether compatibility repair occurred.

    Provider-origin strings are preserved byte-for-byte when they decode to a
    JSON object. Malformed/non-object strings are mapped to ``{}``, matching
    the live post-parse sanitizer: the paired error result still tells the
    model why execution was rejected while the next provider cannot reject the
    history itself. Structured mappings are serialized deterministically so a
    legacy/storage adapter cannot silently erase real arguments. An
    unserializable mapping (or another non-protocol type) returns ``None``;
    callers must stop the causal replay prefix instead of inventing arguments.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, TypeError, ValueError):
            return "{}", True
        if not isinstance(decoded, dict):
            return "{}", True
        return value, False
    if isinstance(value, Mapping):
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ), False
        except (TypeError, ValueError, OverflowError):
            return None, False
    if value is None:
        return "{}", True
    return None, False


def is_superseded_provider_attempt_round(value: Any) -> bool:
    """Return whether *value* is a result-less discarded-attempt artifact.

    New rows carry an explicit machine marker.  The result-metadata fallback is
    retained for already-persisted rows created before that marker existed.
    A late real result does resurrect a legacy badge-only row because the
    badge is only compatibility evidence.  Only the explicit marker is
    unconditional.
    """
    if not isinstance(value, Mapping):
        return False
    if value.get(SUPERSEDED_PROVIDER_ATTEMPT_FIELD) is True:
        return True
    raw_results = value.get("results")
    if not isinstance(raw_results, (list, tuple)) or not raw_results:
        return False
    first = raw_results[0]
    if not isinstance(first, Mapping) or first.get("badge") != "superseded":
        return False
    fetched_chars = first.get("fetchedChars")
    has_fetched_chars = (
        isinstance(fetched_chars, (int, float))
        and not isinstance(fetched_chars, bool)
        and fetched_chars > 0
    )
    has_real_result = (
        value.get("toolContent") is not None
        or first.get("fetched") is True
        or has_fetched_chars
    )
    return not has_real_result


@dataclass(frozen=True)
class ToolRoundReplayPrefix:
    """One validated causal prefix and its positions in the raw projection."""

    rounds: tuple[dict[str, Any], ...]
    positions: tuple[int, ...]
    raw_prefix_length: int
    ignored_count: int
    blocked_position: int | None = None
    blocked_reason: str = ""
    delegated_receipt_count: int = 0


def scan_replayable_tool_round_prefix(values: Any) -> ToolRoundReplayPrefix:
    """Validate the longest replayable tool-execution prefix.

    Non-mapping and identity-free rows are display/progress carriers: live
    executable calls are assigned an id before dispatch, so those rows are
    transparent.  A discarded provider-attempt row is also transparent.  Once
    a row claims a tool-call identity, however, malformed identity/authority or
    a missing result is a causal gap and stops the scan.
    """
    raw_values = list(values) if isinstance(values, (list, tuple)) else []
    replayable: list[dict[str, Any]] = []
    positions: list[int] = []
    ignored_count = 0
    blocked_position: int | None = None
    blocked_reason = ""
    covered_receipts: set[tuple] = set()
    delegated_receipt_count = 0

    for position, raw_value in enumerate(raw_values):
        if not isinstance(raw_value, Mapping):
            ignored_count += 1
            continue
        if is_superseded_provider_attempt_round(raw_value):
            ignored_count += 1
            continue
        if _is_covered_delegated_receipt(raw_value, covered_receipts):
            # Execution/audit rows remain durable, but are not another action
            # emitted by the model. Their exact results already live in an
            # accepted container of this same attempt/task/LLM round.
            covered_receipts.update(_container_receipt_keys(raw_value))
            ignored_count += 1
            delegated_receipt_count += 1
            continue

        if ("toolCallId" not in raw_value
                or raw_value.get("toolCallId") is None):
            # Synthetic inbox/progress/display rows never reached tool dispatch.
            ignored_count += 1
            continue
        tool_call_id = raw_value.get("toolCallId")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or len(tool_call_id) > MAX_TOOL_CALLER_ID_CHARS
        ):
            blocked_position = position
            blocked_reason = "invalid_tool_call_id"
            break

        tool_name = raw_value.get("toolName")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or len(tool_name) > _MAX_REPLAY_TOOL_NAME_CHARS
        ):
            blocked_position = position
            blocked_reason = "invalid_tool_name"
            break

        # The model-visible result is the execution receipt.  Status is only a
        # verdict: error/rejected/aborted results remain valid protocol facts.
        tool_content = raw_value.get("toolContent")
        if not isinstance(tool_content, str):
            blocked_position = position
            blocked_reason = (
                "missing_tool_result" if tool_content is None
                else "invalid_tool_result"
            )
            break

        caller, caller_error = normalize_tool_caller(
            raw_value.get("caller") if "caller" in raw_value else None
        )
        if caller_error:
            blocked_position = position
            blocked_reason = "invalid_tool_caller"
            break

        tool_arguments, arguments_repaired = normalize_replay_tool_arguments(
            raw_value.get("toolArgs") if "toolArgs" in raw_value else None)
        if tool_arguments is None:
            blocked_position = position
            blocked_reason = "invalid_tool_arguments"
            break

        normalized = dict(raw_value)
        normalized["toolArgs"] = tool_arguments
        if arguments_repaired:
            normalized["_toolArgsSanitizedForReplay"] = True
        if caller is None:
            normalized.pop("caller", None)
        else:
            normalized["caller"] = caller
        replayable.append(normalized)
        positions.append(position)
        covered_receipts.update(_container_receipt_keys(normalized))

    raw_prefix_length = positions[-1] + 1 if positions else 0
    return ToolRoundReplayPrefix(
        rounds=tuple(replayable),
        positions=tuple(positions),
        raw_prefix_length=raw_prefix_length,
        ignored_count=ignored_count,
        blocked_position=blocked_position,
        blocked_reason=blocked_reason,
        delegated_receipt_count=delegated_receipt_count,
    )


def _announced_tool_uses(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Client ``tool_use`` blocks the provider returned in this row's batch.

    The batch's native Anthropic content rides on its first occurrence only
    (``tool_dispatch._parse._stamp_round_state``) and is replayed verbatim, so
    each block it announces needs a result in the next request. Hosted
    ``server_tool_use`` blocks carry their results inside the same content.
    """
    blocks = row.get("_anthropicContentBlocks")
    if not isinstance(blocks, list):
        return []
    return [
        block for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
        and isinstance(block.get("id"), str) and block["id"]
        and len(block["id"]) <= MAX_TOOL_CALLER_ID_CHARS
        and isinstance(block.get("name"), str) and block["name"]
        and len(block["name"]) <= _MAX_REPLAY_TOOL_NAME_CHARS
        and isinstance(block.get("input"), Mapping)
    ]


def _unrecorded_occurrence(
    carrier: Mapping[str, Any], block: Mapping[str, Any],
) -> dict[str, Any] | None:
    arguments, _repaired = normalize_replay_tool_arguments(block["input"])
    if arguments is None:
        return None
    occurrence: dict[str, Any] = {
        "toolCallId": block["id"],
        "toolName": block["name"],
        "toolArgs": arguments,
        "toolContent": UNRECORDED_TOOL_RESULT_TEXT,
        "status": "aborted",
        UNRECORDED_TOOL_RESULT_FIELD: True,
    }
    # Same model batch as its carrier, so replay groups it with its siblings.
    for field in ("llmRound", "attemptId", "taskId"):
        if carrier.get(field) is not None:
            occurrence[field] = carrier[field]
    # A provider-announced block is a direct call, never a gateway child.
    occurrence["source"] = "native_direct"
    return occurrence


def pair_announced_tool_calls(values: Any) -> list[Any]:
    """Return rows in which every announced ``tool_use`` has an occurrence.

    A batch is replayed with its provider's native content, which announces
    every call of the response, while its occurrences are recorded one by one
    as each call settles. A sibling whose row never reached the durable record
    (lost to a storage outage, or amputated as the causal gap of an
    interrupted attempt) would otherwise leave a ``tool_use`` without a
    ``tool_result`` and the provider rejects every later request of the Turn.
    Such a call gets one typed occurrence after its batch: the execution
    outcome is unknown, never invented. Rows are not mutated or reordered.
    """
    rows = list(values) if isinstance(values, (list, tuple)) else []
    if not any(isinstance(row, Mapping) and row.get("_anthropicContentBlocks")
               for row in rows):
        return rows
    keys = execution_batch_keys(rows)
    recorded_ids = {
        row.get("toolCallId") for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("toolCallId"), str)
    }
    paired: list[Any] = []
    start = 0
    while start < len(rows):
        end = start + 1
        while end < len(rows) and keys[end] == keys[start]:
            end += 1
        paired.extend(rows[start:end])
        for carrier in rows[start:end]:
            if not isinstance(carrier, Mapping):
                continue
            for block in _announced_tool_uses(carrier):
                if block["id"] in recorded_ids:
                    continue
                occurrence = _unrecorded_occurrence(carrier, block)
                if occurrence is not None:
                    recorded_ids.add(block["id"])
                    paired.append(occurrence)
        start = end
    return paired


def checkpoint_retention_positions(
    values: Any,
    prefix: ToolRoundReplayPrefix,
) -> tuple[int, list[int]]:
    """Return the raw retention boundary and row positions a checkpoint keeps.

    Replay authority stays the causal prefix, but the durable projection must
    retain every row before the first causal gap — display/progress carriers
    (program shells, inbox rows) included — or a checkpoint_resume would
    permanently erase rendered history the model legitimately produced.  Only
    explicitly superseded provider-attempt artifacts are filtered: they are
    transport noise about calls that never executed.  The gap row itself and
    everything after it is amputated; it has no result, and its diagnostic
    lives on in the activity timeline.
    """
    raw_values = list(values) if isinstance(values, (list, tuple)) else []
    boundary = (
        prefix.blocked_position
        if prefix.blocked_position is not None
        else len(raw_values)
    )
    positions = [
        position for position in range(boundary)
        if not is_superseded_provider_attempt_round(raw_values[position])
    ]
    return boundary, positions


def canonical_resume_anchor(
    operation: str,
    requested: Any,
    stored: Any,
) -> dict[str, Any] | None:
    """Return the stored anchor a request selects, or None when it differs.

    A checkpoint anchor's ``retainedToolRoundPositions`` is a derived index,
    not an independent selection: it is exactly the positions before the
    causal gap that are not superseded provider-attempt artifacts (see
    :func:`checkpoint_retention_positions`). A caller that echoes the other
    fields but omits that derived list — a Coordinator copying a long
    settlement by hand — selects the SAME durable rounds as the stored
    anchor, so refusing it as a conflict strands a valid saved checkpoint
    (production 2026-11-09, turn.attempt.create "the requested resume anchor
    is not current").

    The comparison is by value, never by position: an explicit list that does
    not match the stored one, or any other field that differs, returns None
    and the caller keeps the typed conflict. An omitted
    ``retainedToolRoundPositions`` is admitted only when every other field is
    present and equal; no default, repair or widening of the selection is
    invented. This preserves replay identity: the accepted anchor is the
    stored one, so a repeated command replays the same Attempt.

    The stored list is SUBSTITUTED, not recomputed: re-deriving it from the
    projection would be wrong, because the legacy no-positions fallback keeps
    ``rounds[:keptToolRounds]`` including superseded provider attempts (see
    ``_turns_write.py`` and ``turn.rs``). Callers must therefore use this
    function's return value as the accepted anchor, never the request's.
    """
    if not isinstance(stored, Mapping):
        return None
    canonical = dict(stored)
    if requested is None:
        return canonical
    if not isinstance(requested, Mapping):
        return None
    requested_dict = dict(requested)
    if requested_dict == canonical:
        return canonical
    if operation != 'checkpoint_resume':
        return None
    if 'retainedToolRoundPositions' in requested_dict:
        return None
    if 'retainedToolRoundPositions' not in canonical:
        # A settlement written before occurrence-selective checkpointing has
        # no derived list to normalize; the exact-value comparison above is
        # the only admission.
        return None
    if {**requested_dict, 'retainedToolRoundPositions': canonical['retainedToolRoundPositions']} != canonical:
        return None
    # Only the derived index was omitted; the caller's implicit selection is
    # exactly the stored checkpoint, so admit the stored anchor itself.
    return canonical


__all__ = [
    "TOOL_REPLAY_PROJECTION_VERSION",
    "SUPERSEDED_PROVIDER_ATTEMPT_FIELD",
    "UNRECORDED_TOOL_RESULT_FIELD",
    "UNRECORDED_TOOL_RESULT_TEXT",
    "canonical_resume_anchor",
    "checkpoint_retention_positions",
    "ToolRoundReplayPrefix",
    "is_superseded_provider_attempt_round",
    "normalize_replay_tool_arguments",
    "pair_announced_tool_calls",
    "scan_replayable_tool_round_prefix",
]
