"""A resumed Attempt continues on the model the interrupted Attempt ran on.

Entry: :func:`continue_resumed_model`, called once per ``run_task`` invocation
between the accepted model-config resolution and tool assembly.

An in-Attempt refusal/availability fallback lives only in the process: the
interrupted Attempt's ``task['_fallback_model']`` dies with it, so a restart
resume would silently return to the Turn's accepted ``config.model`` and pay a
fresh prompt-cache write (and usually a second refusal) on a model the user
already saw fail. The interrupted Attempt's paid ledger is durable
(``checkpointApiRounds``), and each row names the logical model that produced
that response. The highest-round paid response-authoring row therefore IS the
model the interrupted Attempt was running on; a resume whose request did not
deliberately switch models continues there.

Decision rules (docs/RUN_CONTINUITY.md, "A resumed Turn continues on the model
it was running on"):

* Highest integer ``round`` whose row is paid response-authoring:
  ``responseAuthoring`` is not ``False`` and the tag is not a discarded/refused
  charge (those attempts were billed but never adopted).
* Only when that model differs from the accepted model, the resume did not
  explicitly choose a different model, and the model is still admitted by the
  same boundary the refusal fallback path uses
  (``llm_fallback._retry._get_fallback_model``). Otherwise normal selection.
* Explicit choice is the acceptance-time marker
  ``_resumeModelSwitchFrom`` (``RESUME_MODEL_SWITCH_FROM_CONFIG_KEY``):
  ``command_service.create_attempt`` stamps it when a continue /
  checkpoint_resume / answer_guidance request's ``config.model`` differs from
  the submitted Turn's rail snapshot model (pause → switch → continue).
  The marker travels in the durable Attempt config, so request replay and
  durable worker recovery honor the same choice.
* Preset and thinking stay the round's own values, exactly as the in-Attempt
  refusal fallback keeps them; only the model identity changes.

The model-dependent fields (``max_tokens``, the compaction context window,
the executable catalog bound to the model) must resolve for the continued
model BEFORE tools/context are assembled: the caller re-resolves the model
config with the continued model and runs this before tool assembly.
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


def admitted_continuation_model(task: dict[str, Any], candidate: str) -> str:
    """The candidate when the refusal-fallback admission boundary still allows it.

    ``_get_fallback_model`` is the single admission check the fallback path
    applies before switching models: it honors the per-task
    ``disableModelFallback`` opt-out and the configured fallback model. A
    candidate that is no longer admitted yields normal (accepted) selection.
    """
    from lib.tasks_pkg.llm_fallback import _retry

    admitted = _retry._get_fallback_model(task)
    return candidate if admitted and candidate == admitted else ''


def continue_resumed_model(task: dict[str, Any], cfg: dict[str, Any],
                           accepted_model: str) -> str:
    """Return the model this Attempt runs on, restamping fallback reporting.

    ``accepted_model`` is the already-resolved accepted ``mcfg['model']``.
    Returns the accepted model unchanged for a non-resume, an explicit model
    switch, no authoring evidence, or a no-longer-admitted candidate. On a
    continuation, ``task['_fallback_model']`` / ``_fallback_from`` are stamped
    exactly as the in-Attempt fallback stamps them so done/persist still
    report ``fallbackModel`` / ``actualModel``.
    """
    if not cfg.get('checkpointApiRounds'):
        return accepted_model
    if str(cfg.get(RESUME_MODEL_SWITCH_FROM_CONFIG_KEY) or '').strip():
        return accepted_model
    candidate = last_authoring_model(cfg.get('checkpointApiRounds'))
    if not candidate or candidate == accepted_model:
        return accepted_model
    continued = admitted_continuation_model(task, candidate)
    if not continued:
        logger.info(
            '[Task %s] Resume model continuation skipped: %s is not admitted '
            'by the fallback selection boundary; using accepted model %s',
            task_log_tag(task['id']), candidate, accepted_model)
        return accepted_model
    task['_fallback_model'] = continued
    task['_fallback_from'] = accepted_model
    task['_fallback_kind'] = 'resume_continuation'
    task['_fallback_reason'] = (
        'Resuming on the model the interrupted attempt was running on, '
        'keeping its provider prompt cache.')
    logger.info(
        '[Task %s] conv=%s Resume model continuation: %s → %s '
        '(last paid authoring round; preset/thinking unchanged)',
        task_log_tag(task['id']), task.get('convId', ''),
        accepted_model, continued)
    return continued


__all__ = [
    'admitted_continuation_model',
    'continue_resumed_model',
    'last_authoring_model',
]
