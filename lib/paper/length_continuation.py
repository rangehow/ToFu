"""Continue paper answers cut off by the model's output-token cap.

Responsibility: one policy for report, Q&A and Deepen when a no-tool round
ends with ``finish_reason == 'length'``. The worker asks for the rest and the
published text is the joined partial responses plus the final one.
Entry points: ``OUTPUT_TOKEN_CEILING`` (the ``max_tokens`` every paper answer
passes; dispatch's ``_clamp_max_tokens`` reduces it to the model's native
output limit), ``LengthContinuation`` (joined terminal text across rounds) and
``length_continuation_hook`` (the shared-loop ``decide_round`` hook).
Dependencies: ``lib.agent_loop`` directives and
``prompts.length_continuation_prompt``.

There is no continuation cap: the loop ends when the model finishes, on Stop,
or when a continuation adds no visible text (the text as generated is then
published). A fixed count would be a tuning knob (AGENTS.md principle 5).
"""

from lib.agent_loop.agent_loop import LoopDirective
from lib.log import get_logger
from lib.paper.prompts import length_continuation_prompt

logger = get_logger(__name__)

# "As much as the model allows": dispatch clamps this to each model's native
# output limit; a round that still reaches it is continued, not truncated.
OUTPUT_TOKEN_CEILING = 128000


def join_length_continuation(prefix: str, continuation: str) -> str:
    """Return the part of ``continuation`` that extends ``prefix``.

    The seam is joined exactly (a cut can fall mid-word or mid-table-row).
    Models asked to continue sometimes restart the line they were cut in, or
    repeat the last complete line; when the continuation begins with exactly
    that trailing line, the repeated copy is dropped.
    """
    line_start = prefix.rfind('\n', 0, len(prefix.rstrip('\n'))) + 1
    last_line = prefix[line_start:]
    if last_line.strip() and continuation.startswith(last_line):
        return continuation[len(last_line):]
    return continuation


class LengthContinuation:
    """Terminal (non-tool) response text of one answer across continuations."""

    def __init__(self):
        # None until a no-tool response is observed; an observed empty string
        # is an authoritative empty response.
        self.terminal_text = None
        # Joined text of earlier length-truncated rounds; None until continued.
        self._continued_prefix = None

    @property
    def continued(self) -> bool:
        return self._continued_prefix is not None

    def observe_response(self, message):
        # A tool round's prose is provisional and never part of the answer.
        if not isinstance(message, dict) or message.get('tool_calls'):
            return
        content = message.get('content') or ''
        if self._continued_prefix is None:
            self.terminal_text = content
        else:
            self.terminal_text = self._continued_prefix + join_length_continuation(
                self._continued_prefix, content)

    def continue_truncated_response(self) -> bool:
        """Keep the observed truncated response as the next round's prefix.

        Returns False when the round added no visible text, so the caller
        stops continuing instead of looping on a model that cannot progress.
        """
        prefix = self._continued_prefix or ''
        terminal = self.terminal_text or ''
        if not terminal[len(prefix):].strip():
            return False
        self._continued_prefix = terminal
        return True


def length_continuation_hook(continuation, messages, abort_signal, *,
                             ui_lang, task_id, log_prefix):
    """Return the ``decide_round`` hook that resumes output-cap truncation.

    The caller's ``on_round_result`` must feed every round's message to
    ``continuation.observe_response`` before this hook runs.
    """

    def _continue_truncated(rnd, msg, finish, usage):
        if (finish != 'length' or abort_signal.aborted
                or not isinstance(msg, dict) or msg.get('tool_calls')):
            return None
        if not continuation.continue_truncated_response():
            logger.warning(
                '%s Task %s round %d — output cap reached with no new text; '
                'publishing the answer as generated (%d chars)',
                log_prefix, task_id, rnd + 1,
                len(continuation.terminal_text or ''))
            return None
        logger.info('%s Task %s round %d — output cap reached; requesting '
                    'continuation', log_prefix, task_id, rnd + 1)
        messages.append({key: value for key, value in msg.items()
                         if key != 'tool_calls'})
        messages.append({'role': 'user',
                         'content': length_continuation_prompt(ui_lang)})
        return LoopDirective.continue_round()

    return _continue_truncated


__all__ = [
    'LengthContinuation',
    'OUTPUT_TOKEN_CEILING',
    'join_length_continuation',
    'length_continuation_hook',
]
