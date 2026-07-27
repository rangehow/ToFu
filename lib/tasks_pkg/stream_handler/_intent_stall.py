"""Intent-stall nudge — the four-criterion classifier (epic pt_33ba079f5cea4841).

THE SHAPE (measured, docs/INTENT_STALL_MEASUREMENT.md): a tool round is
rejected or errors, and the model's NEXT round is plain prose with
``finish_reason=stop`` and zero tool calls — text that says it will continue,
followed by nothing. The task settles normally and the user sees the
conversation stop mid-thought. Ground truth: conv ms34yw0k74o2lq R18
(2026-07-27 21:25:27) — ``run_command`` blocked by a pre-execution hook, the
model replied "Let me use explicit paths only." and never called the tool.

WHY FOUR CRITERIA AND NOT THE TWO THE TICKET AUTHORIZED
-------------------------------------------------------
A 7-day full scan (256 convs / 962 assistant messages) measured the
ticket's A∧B pair at **12/20 = 60% false positives**. Nudging on A∧B alone
would have fired on:

  * 5 hand-backs — the model asked the USER a question; nudging jumps the
    queue and answers on their behalf;
  * 4 VU turns — an automation loop's own designed ending;
  * 3 non-retryable — the tool is absent from the whole turn's toolset, so
    a nudge can only make the model reach for it again (unbounded burn).

So C and D are NOT polish; without them the majority of nudges are wrong.
Real target after all four: 8 cases / 7 days (opus-5 2.39%, 4.8 0.38%).

WORDING IS NEVER A CRITERION
----------------------------
Phrase matching was falsified from two directions — this scan measured 45%
FP on a "Let me / 让我" regex, a sibling's independent wording-axis scan
measured 49%. Hand-backs routinely contain BOTH a question and a
first-person action verb ("我建议…你定" + "我会直接"), so no regex separates
them. Every criterion below reads structure: tool-round status, tool-call
count, rejection class, and an explicit self-reported end reason.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ── D2: the model's own structured end-of-turn declaration ──
# The turn-ending contract: when a model deliberately ends its turn it may
# state WHY in machine-readable form. This exists because D1 (state probe)
# structurally cannot see a hand-back phrased in prose — the model asks its
# question in natural language without calling ask_user, and no amount of
# state inspection reveals intent the model never declared. D2 closes that
# gap by making the intent declarable.
#
# Kept as a literal sentinel (not prose) so detection is exact-match, never
# fuzzy — mirroring VU_DONE_SENTINEL in lib/agent_verdict/_handoff.py.
END_TURN_MARKER = '[END_TURN:'
END_TURN_AWAITING_HUMAN = 'awaiting_human'
END_TURN_DONE = 'done'
END_TURN_BLOCKED = 'blocked'
_VALID_END_REASONS = frozenset({
    END_TURN_AWAITING_HUMAN, END_TURN_DONE, END_TURN_BLOCKED,
})

# Tools whose presence in the round means the model explicitly handed control
# back to the human (D1's state probe).
_HUMAN_HANDOFF_TOOLS = frozenset({'ask_user', 'request_human_input'})

# Rejection classes that can NEVER succeed on retry — the tool is absent from
# this turn's dispatched toolset, so nudging just re-drives the same dead end.
# Surfaced to the user as a `tool_not_available` envelope instead
# (orchestrator/_finalize, epic pt_88791cb08cb2495c).
_NON_RETRYABLE_MARKERS = (
    'is not a real tool',
    'not in the list of tools available',
)


def parse_end_turn_reason(content: str) -> str | None:
    """Return the model's self-declared end-of-turn reason, or None.

    D2's reader. Accepts ``[END_TURN: <reason>]`` anywhere in the text and
    validates the reason against a closed set — an unrecognised value reads
    as "no declaration" rather than being trusted, so a model inventing its
    own reason can never accidentally suppress a nudge.
    """
    if not content or END_TURN_MARKER not in content:
        return None
    try:
        tail = content.split(END_TURN_MARKER, 1)[1]
        reason = tail.split(']', 1)[0].strip().lower()
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[IntentStall] end-turn parse failed: %s', e)
        return None
    if reason in _VALID_END_REASONS:
        return reason
    logger.debug('[IntentStall] unrecognised end-turn reason %r — ignoring',
                 reason[:40])
    return None


def _last_tool_round(task: dict[str, Any]) -> dict | None:
    """The most recent tool round, ordered the way the orchestrator appends."""
    rounds = task.get('toolRounds') or []
    if not rounds:
        return None
    try:
        return sorted(
            rounds,
            key=lambda r: (r.get('llmRound') or 0, r.get('roundNum') or 0),
        )[-1]
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[IntentStall] tool-round ordering failed: %s', e)
        return rounds[-1]


def _round_failed(entry: dict[str, Any]) -> bool:
    """Criterion A: did this tool round reject or error?

    Reads the same structural fields the UI renders from — status, the
    per-result badge, notRun, and a non-zero exit code.
    """
    if (entry.get('status') or '').lower() in ('rejected', 'error', 'failed',
                                               'timeout'):
        return True
    for res in (entry.get('results') or []):
        if res.get('notRun'):
            return True
        if (res.get('badge') or '').lower() in ('blocked', 'error', 'failed',
                                                'timeout'):
            return True
        if (res.get('type') or '') == 'error':
            return True
        code = res.get('exitCode')
        if code not in (None, '', 0, '0') and str(code) != '0':
            return True
    return False


def _is_non_retryable(entry: dict[str, Any]) -> bool:
    """Criterion C: was the tool absent from this turn's toolset?

    Checks the dispatcher's own rejection descriptor first (a structured
    signal it sets itself), then falls back to the hard-reject text the
    dispatch layer generates — also its own output, never model wording.
    """
    if entry.get('_rejected') or any(
            (r.get('rejected') or r.get('_rejected'))
            for r in (entry.get('results') or [])):
        blob = str(entry.get('_rejected') or '')
        for res in (entry.get('results') or []):
            blob += str(res.get('reason') or res.get('content') or '')
        low = blob.lower()
        return any(m in low for m in _NON_RETRYABLE_MARKERS)
    blob = ' '.join(
        str(r.get('reason') or r.get('content') or '')
        for r in (entry.get('results') or [])).lower()
    return any(m in blob for m in _NON_RETRYABLE_MARKERS)


def _awaiting_human(task: dict[str, Any], assistant_msg: dict[str, Any],
                    content: str) -> bool:
    """Criterion D: is this turn deliberately waiting on the human?

    Two independent axes, per owner's decision to ship BOTH:

    * **D1 — state probe (zero wording).** The model called ``ask_user`` /
      ``request_human_input`` this turn, or the task carries a live
      human-guidance request. Exact, but blind to a question asked in prose.
    * **D2 — self-declaration.** The model stated ``[END_TURN: awaiting_human]``.
      Covers precisely D1's blind spot; also honours ``done`` / ``blocked``,
      because a model that explicitly declares its turn finished must not be
      re-driven either.
    """
    # D1a — a human-handoff tool was called in this very round.
    for tc in (assistant_msg.get('tool_calls') or []):
        name = ((tc.get('function') or {}).get('name') or '')
        if name in _HUMAN_HANDOFF_TOOLS:
            return True
    # D1b — a human-handoff tool was called in any earlier round of the turn.
    for entry in (task.get('toolRounds') or []):
        if (entry.get('tool') or entry.get('toolName') or '') in _HUMAN_HANDOFF_TOOLS:
            return True
    # D1c — the task is parked on a live human-guidance request.
    if task.get('_pendingHumanGuidance') or task.get('pendingGuidanceId'):
        return True
    # D2 — the model declared its own end-of-turn reason.
    return parse_end_turn_reason(content) is not None


def should_nudge_intent_stall(
    task: dict[str, Any],
    assistant_msg: dict[str, Any],
    content: str,
) -> tuple[bool, str]:
    """Decide whether this stop is an intent stall worth one nudge.

    Returns ``(should_nudge, reason)`` — ``reason`` names the criterion that
    rejected it, so the caller can log WHY a stop was left alone. Total: any
    internal failure reports "no nudge", because wrongly nudging costs a
    billed round and can talk over a waiting user, while wrongly skipping
    just preserves today's behaviour.
    """
    try:
        if not (content or '').strip():
            return False, 'no_content'
        # B — this round must be prose only.
        if assistant_msg.get('tool_calls'):
            return False, 'has_tool_calls'
        # D — never talk over a human who is being asked something.
        #     Evaluated BEFORE A on purpose: a hand-back often IS the most
        #     recent tool round (ask_user succeeds), so checking A first would
        #     read "previous tool was fine" and return before D ever ran —
        #     the suppressor would be unreachable exactly when it matters.
        if _awaiting_human(task, assistant_msg, content):
            return False, 'awaiting_human'
        entry = _last_tool_round(task)
        if entry is None:
            return False, 'no_tool_rounds'
        # A — the previous tool round must have failed.
        if not _round_failed(entry):
            return False, 'prev_tool_ok'
        # C — an absent tool can never succeed on retry.
        if _is_non_retryable(entry):
            return False, 'non_retryable'
        return True, 'intent_stall'
    except Exception as e:
        logger.warning('[IntentStall] classification failed (not nudging): %s', e)
        return False, 'classify_error'


# The nudge itself: a structured instruction, never a paraphrase of what the
# model just said. Restating its wording would invite it to re-justify the
# same sentence; naming the STRUCTURAL fact (a tool failed, nothing ran since)
# gives it something to act on.
NUDGE_TEXT = (
    '[SYSTEM: TOOL CALL DID NOT RUN]\n'
    'Your previous tool call was rejected or failed, so it never executed, '
    'and this turn ended without running any tool.\n\n'
    'If you still intend to perform that action, issue the tool call now — '
    'adjusting the arguments if the failure explained what was wrong.\n'
    'If you are waiting on the user, or the task is genuinely finished or '
    f'blocked, end your turn with {END_TURN_MARKER} awaiting_human], '
    f'{END_TURN_MARKER} done] or {END_TURN_MARKER} blocked] so this is not '
    'mistaken for an interrupted turn.'
)
