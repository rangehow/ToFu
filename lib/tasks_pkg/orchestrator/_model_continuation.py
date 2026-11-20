"""A resumed Attempt runs on the conversation's accepted model.

Entry: :func:`continue_resumed_model`, called once per ``run_task`` invocation
between the accepted model-config resolution and tool assembly.

An in-Attempt refusal/availability fallback lives only in the process: the
interrupted Attempt's ``task['_fallback_model']`` dies with it. A resume
continues the WORK, it does not re-decide the model — and a ``content_filter``
refusal is a per-request content verdict, never a demotion. So a resume returns
to the accepted model (the owner/lead's conversation or session choice, already
resolved by the admission boundary in ``project_dispatch/admission``), and a
repeated refusal on the first resumed round falls back again for that round
only. The exception is a deliberate model choice the owner or a lead made for
this resume: the acceptance-time ``_resumeModelSwitchFrom`` marker
(``RESUME_MODEL_SWITCH_FROM_CONFIG_KEY``) — a pause → set_model → continue.

The paid ledger (``checkpointApiRounds``) still records every round that ran on
the fallback, and :func:`fallback_rounds` derives the review-visible receipt
from it (or from the in-process ledger for a live attempt), so a draft written
partly on the fallback is never relabelled as primary-only work.

Decision rules (docs/RUN_CONTINUITY.md, "A resumed Turn returns to the accepted
model"):

* No resume evidence (no ``checkpointApiRounds``) → accepted model.
* Explicit switch marker → accepted model (the deliberate choice).
* Anything else → accepted model. The old rule, "highest-round paid
  response-authoring row elects the model", is retained as
  :func:`last_authoring_model` only because the receipt needs that row's model;
  it no longer decides the resume's model.

Alternatives rejected and the measured cost of the previous behaviour are in
docs/RUN_CONTINUITY.md.
"""

from __future__ import annotations

from typing import Any

from lib.conversation_sync.dispatch_contract import (
    RESUME_MODEL_SWITCH_FROM_CONFIG_KEY,
)
from lib.log import get_logger, task_log_tag

logger = get_logger(__name__)


def last_authoring_model(api_rounds: Any) -> str:
    """Logical model of the highest-round paid response-authoring row.

    Refused attempts and discarded resends are billed with
    ``responseAuthoring=False``; a missing flag means authoring. Any tag
    naming a discarded/refused charge is skipped too, so a legacy row that
    predates the flag cannot elect a model that produced no adopted response.
    """
    best_round = 0
    model = ''
    if not isinstance(api_rounds, (list, tuple)):
        return ''
    for row in api_rounds:
        if not isinstance(row, dict):
            continue
        round_id = row.get('round')
        if type(round_id) is not int or round_id <= best_round:
            continue
        if row.get('responseAuthoring') is False:
            continue
        tag = str(row.get('tag') or '').upper()
        if 'REFUSED' in tag or 'DISCARDED' in tag:
            continue
        candidate = row.get('model')
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        best_round = round_id
        model = candidate.strip()
    return model


def continue_resumed_model(task: dict[str, Any], cfg: dict[str, Any],
                           accepted_model: str) -> str:
    """Return the model this Attempt runs on: the accepted model.

    Kept as the single decision point so the run loop's model-dependent
    resolution stays in one place. A resume never elects a model from the paid
    ledger; only the accepted selection (or an explicit switch, which IS the
    accepted selection) decides. The function keeps its name and signature
    because callers and native continuations name it.
    """
    if not cfg.get('checkpointApiRounds'):
        return accepted_model
    if str(cfg.get(RESUME_MODEL_SWITCH_FROM_CONFIG_KEY) or '').strip():
        return accepted_model
    candidate = last_authoring_model(cfg.get('checkpointApiRounds'))
    if candidate and candidate != accepted_model:
        # Evidence for the receipt and the log: the interrupted attempt ended
        # on another model. The resume still runs on the accepted one.
        logger.info(
            '[Task %s] conv=%s Resume returns to the accepted model %s '
            '(interrupted attempt ran on %s; a repeated refusal falls back '
            'for that round only)',
            task_log_tag(task['id']), task.get('convId', ''),
            accepted_model, candidate)
    return accepted_model


def fallback_rounds(api_rounds: Any) -> dict[str, list[int]]:
    """Round numbers per model, from the paid ledger (authoring rows only).

    The source of truth for "which rounds ran on which model". Refused and
    discarded charges are not answers, so they do not count as a model's
    rounds; the round numbers keep the attempt's own numbering across resumes.
    """
    by_model: dict[str, list[int]] = {}
    if not isinstance(api_rounds, (list, tuple)):
        return by_model
    for row in api_rounds:
        if not isinstance(row, dict) or row.get('responseAuthoring') is False:
            continue
        round_id = row.get('round')
        if type(round_id) is not int:
            continue
        tag = str(row.get('tag') or '').upper()
        if 'REFUSED' in tag or 'DISCARDED' in tag:
            continue
        model = row.get('model')
        if not isinstance(model, str) or not model.strip():
            continue
        by_model.setdefault(model.strip(), []).append(round_id)
    return by_model


def model_fallback_receipt(api_rounds: Any, accepted_model: str) -> dict[str, Any]:
    """Review-visible receipt of the rounds that ran on a non-primary model.

    ``rounds`` lists them; ``byModel`` is the full per-model breakdown so a
    reviewer sees the primary's share too. Empty when every authoring round ran
    on the accepted model (or the model is unknown).
    """
    by_model = fallback_rounds(api_rounds)
    primary = str(accepted_model or '').strip()
    if not primary or not by_model:
        return {}
    others = {model: rounds for model, rounds in by_model.items() if model != primary}
    if not others:
        return {}
    rounds = sorted(r for values in others.values() for r in values)
    total = sum(len(values) for values in by_model.values())
    names = ', '.join(
        f'{model} ({len(values)} round{"" if len(values) == 1 else "s"})'
        for model, values in sorted(others.items()))
    # Whether the primary was attempted again is a fact of the ledger, not a
    # policy claim: a REFUSED row is not an authoring round, so byModel cannot
    # see it. Count any later primary attempt, answered or refused.
    attempted_later = any(
        isinstance(row, dict)
        and str(row.get('model') or '').strip() == primary
        and type(row.get('round')) is int
        and row['round'] > min(rounds)
        for row in (api_rounds or ()))
    tail = (
        'A content_filter refusal is per-request, so later rounds retried '
        'the primary.' if attempted_later else
        'The primary was never attempted again after the switch: no later '
        'round in the paid ledger carries it.'
    )
    return {
        'primaryModel': primary,
        'byModel': {model: sorted(values) for model, values in by_model.items()},
        'fallbackRounds': rounds,
        'totalRounds': total,
        'notice': (
            f'{len(rounds)} of {total} model rounds ran on a fallback model '
            f'instead of {primary}: {names}. {tail}'
        ),
    }


__all__ = ['last_authoring_model', 'continue_resumed_model', 'fallback_rounds',
           'model_fallback_receipt']
