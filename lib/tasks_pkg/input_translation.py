"""Carry an accepted attempt's deferred input translation; nothing waits for it.

Responsibility: the accepted attempt's start translates the authored input
(``lib.chat.turn_builder.start_deferred_input_translation``) while the executor
starts, and no model request waits for it (owner decision 2026-11-12). Entry
points:

* ``begin_input_translation`` at worker entry (``lib.tasks_pkg.spawn``). A
  translation that already finished settles before any runner reads the model
  view: its verdict is recorded on the input Turn, the model view carries the
  translated authored text, the request account binds to the settled revision
  and the replay proof is re-pinned. A running one stays on the task and the
  first request reads the authored text. The request account then binds to the
  authored input: when that pinned the input's revision (a Coordinator request)
  this attempt never writes the input Turn, otherwise it records the verdict
  the moment the translation completes.
* ``apply_input_translation`` in every model request's context preparation
  (``prepare_round_context``, root and native alike) and once more before the
  root run finalizes. Once the translation completed, that request and every
  later one carry the translated authored text; after the verdict the replay
  proof is re-pinned once.

A Stop before the translation completes records nothing. Only the request
account binding raises into the worker lifecycle, failing the attempt as before.
"""

from __future__ import annotations

import re
import threading
from typing import Any, NamedTuple

from lib.log import get_logger

logger = get_logger(__name__)

# Mirrors the model-view composition in conv_message_builder._transform: the
# authored text has its notranslate tags stripped, quotes/references precede
# it and inline document blocks follow this separator.
_NOTRANSLATE_TAGS = re.compile(r'</?(?:notranslate|nt)>', re.IGNORECASE)
_DOCUMENT_SEPARATOR = '\n\n' + '═' * 50
_TASK_KEY = '_inputTranslation'


class SettledInput(NamedTuple):
    """The accepted input Turn revision a worker's model view reflects.

    Both fields are ``None`` when the verdict could not establish it (Stop, a
    failed translator, an unconfirmed write); the attempt writes nothing more
    to the input Turn in any case.
    """

    turn_id: str | None
    projection_revision: int | None


_UNSETTLED = SettledInput(None, None)


class _InputTranslation:
    """One attempt's translation from worker entry until its task is released."""

    __slots__ = ('future', 'pinned', 'armed', 'settled', 'recorded', 'rebound',
                 'carried', 'reported')

    def __init__(self, future):
        self.future = future
        # A request account pinned the authored revision before completion.
        self.pinned = False
        # The attempt records the verdict when the translation completes.
        self.armed = False
        self.settled = _UNSETTLED
        self.recorded = threading.Event()
        self.rebound = False
        self.carried = False
        self.reported = False

    def outcome(self):
        """The completed translation; ``None`` while it runs or after it failed."""
        future = self.future
        if not future.done() or future.cancelled() or future.exception() is not None:
            return None
        return future.result()


def begin_input_translation(task: dict[str, Any]) -> None:
    """Worker entry: settle a finished translation, never wait for a running one."""
    future = task.pop('_deferredInputTranslation', None)
    if future is None:
        return
    from lib.observability import measure_task_startup
    from lib.project_dispatch.request_budget import bind_source_account

    carrier = _InputTranslation(future)
    task[_TASK_KEY] = carrier
    with measure_task_startup('input_translation', identity=_identity(task)):
        ready = future.done()
        if ready:
            # Recorded on this thread, before the request account pins the
            # input Turn's revision.
            _arm(task, carrier)
    if ready:
        _carry(task, carrier, task.get('messages'), round_num=None)
        bind_source_account(task, expected_input=carrier.settled)
        _rebind(task, carrier)
        return
    label = _label(task)
    logger.info('[Task %s] input translation still running at worker entry; '
                'the first model request reads the authored text', label)
    if bind_source_account(task) is not None:
        # The account and every Worker grant it issues pin this revision; a
        # verdict now would refuse the request's next model dispatch.
        carrier.pinned = True
        logger.info('[Task %s] request account pinned the authored input; the '
                    'translation reaches only later requests of this attempt', label)
        return
    _arm(task, carrier)


def apply_input_translation(task: dict[str, Any], messages: list | None, *,
                            round_num: int | None = None, final: bool = False) -> None:
    """Carry a completed translation into the request ``messages`` becomes.

    ``final`` (the root run's end) also waits for a verdict write already in
    flight, so the continuation the Turn publishes names the settled input.
    """
    carrier = task.get(_TASK_KEY)
    if carrier is None or not carrier.future.done():
        return
    _carry(task, carrier, messages, round_num=round_num)
    if carrier.rebound or not carrier.armed:
        return
    if final:
        carrier.recorded.wait()
    if carrier.recorded.is_set():
        _rebind(task, carrier)


def _label(task):
    return str(task.get('id') or '?')


def _identity(task):
    return {
        'task_id': task.get('id'), 'conversation_id': task.get('convId'),
        'turn_id': task.get('_turnId'), 'attempt_id': task.get('_attemptId'),
        'user_id': task.get('_userId'),
    }


def _aborted(task: dict[str, Any]) -> bool:
    abort_event = task.get('abort_event')
    return bool(task.get('aborted')
                or (abort_event is not None and abort_event.is_set()))


def _arm(task, carrier):
    carrier.armed = True
    # Runs at once when the translation already finished, otherwise on the
    # thread that completes it.
    carrier.future.add_done_callback(lambda _future: _record(task, carrier))


def _record(task, carrier):
    label = _label(task)
    try:
        if _aborted(task):
            logger.info('[Task %s] input translation finished after Stop; the '
                        'input Turn keeps the authored text', label)
            return
        error = carrier.future.exception()
        if error is not None:
            logger.warning('[Task %s] deferred input translation unavailable: %s',
                           label, error, exc_info=error)
            return
        outcome = carrier.future.result()
        if outcome is None:
            return
        revision = _record_verdict(task, outcome, label)
        if revision is not None:
            carrier.settled = SettledInput(outcome.input_turn_id, revision)
    finally:
        carrier.recorded.set()


def _record_verdict(task, outcome, task_label):
    from lib.chat.turn_builder import record_deferred_input_translation

    try:
        return record_deferred_input_translation(
            task.get('convId'), outcome, user_id=task.get('_userId'))
    except Exception as exc:
        logger.warning('[Task %s] input translation verdict not recorded: %s',
                       task_label, exc, exc_info=True)
        return None


def _rebind(task, carrier):
    carrier.rebound = True
    from lib.tasks_pkg.model_replay import rebind_history_proof
    rebind_history_proof(task, carrier.settled)


def _carry(task, carrier, messages, *, round_num):
    """Substitute the translation into ``messages`` and the task's input view.

    The task's input view is the pristine input a whole-turn retry restarts
    from and the view the replay-proof rebind checks.
    """
    outcome = carrier.outcome()
    if outcome is None or outcome.translated is None:
        return
    original = _model_text(outcome.original)
    translated = _model_text(outcome.translated)
    query = _rewrite_input(messages, outcome.input_turn_id, original, translated)
    durable = task.get('messages')
    if durable is not messages:
        _rewrite_input(durable, outcome.input_turn_id, original, translated)
    if query is None:
        if not carrier.reported:
            carrier.reported = True
            logger.warning('[Task %s] translated input not found in the model view; '
                           'the model sees the authored text', _label(task))
        return
    task['lastUserQuery'] = query
    if not carrier.carried:
        carrier.carried = True
        if round_num is not None:
            logger.info('[Task %s] model requests from round %s on carry the '
                        'translated input', _label(task), round_num)


def _model_text(text: str) -> str:
    if '<notranslate>' in text or '<nt>' in text:
        return _NOTRANSLATE_TAGS.sub('', text)
    return text


def _authored_span(text: str, authored: str) -> tuple[int, int] | None:
    """Locate authored text after any quote/reference prefix, before documents."""
    cut = text.find(_DOCUMENT_SEPARATOR)
    end = len(text) if cut < 0 else cut
    start = end - len(authored)
    if (not authored or start < 0 or text[start:end] != authored
            or (start and not text[:start].endswith('\n\n'))):
        return None
    return start, end


def _rewrite(text: str, original: str, translated: str) -> str | None:
    # Checked first: every later request re-applies the substitution, and a
    # translation ending in its own source must not be substituted again.
    if _authored_span(text, translated) is not None:
        # The committed translation was already read into this model view.
        return text
    span = _authored_span(text, original)
    if span is None:
        return None
    return text[:span[0]] + translated + text[span[1]:]


def _rewrite_input(messages, input_turn_id, original, translated) -> str | None:
    """Rewrite the input Turn's model message; return its text for ``lastUserQuery``.

    The message is found by the Turn identity the model-view builder keeps on
    it, so steers and context carriers appended after it are never mistaken
    for the input.
    """
    message = next((item for item in reversed(messages or ())
                    if isinstance(item, dict) and item.get('role') == 'user'
                    and item.get('_turnId') == input_turn_id), None)
    content = None if message is None else message.get('content')
    if isinstance(content, str):
        replaced = _rewrite(content, original, translated)
        if replaced is None:
            return None
        message['content'] = replaced
        return replaced[:500]
    if isinstance(content, list):
        for block in reversed(content):
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            replaced = _rewrite(str(block.get('text') or ''), original, translated)
            if replaced is not None:
                block['text'] = replaced
                return ' '.join(
                    str(item.get('text') or '') for item in content
                    if isinstance(item, dict) and item.get('type') == 'text')[:500]
    return None


def model_view_carries(task: dict[str, Any], text: Any) -> bool:
    """Whether the latest user message of the model view holds ``text`` as its authored span."""
    if not isinstance(text, str) or not text:
        return False
    authored = _model_text(text)
    for message in reversed(task.get('messages') or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return _authored_span(content, authored) is not None
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get('type') == 'text'
                and _authored_span(str(block.get('text') or ''), authored) is not None
                for block in content)
        return False
    return False


__all__ = ['SettledInput', 'apply_input_translation', 'begin_input_translation',
           'model_view_carries']
