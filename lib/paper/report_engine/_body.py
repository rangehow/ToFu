"""Report text projection across dispatch rounds and terminal adoption.

Stream callbacks are a live presentation channel and may replay text after a
transport retry.  The terminal assistant message is the durable response
authority. ``ReportBodyProjection`` owns the streamed draft and convergence
events; ``resolve_canonical_report_body`` is the pure selection boundary.
The worker supplies task/event/log ports and retains post-processing,
publication and terminal task settlement. Its later edits to ``text`` remain
available to the worker's partial-result error/abort paths.

When a no-tool round stops at the output-token cap, the worker asks the model
to continue. The terminal body is then the joined partial responses plus the
final continuation (``join_report_continuation``), not the last message alone.
"""


def resolve_canonical_report_body(
    streamed_body: str,
    terminal_body: str | None,
) -> tuple[str, bool]:
    """Return ``(canonical_body, live_projection_needs_reset)``.

    A missing terminal body means dispatch never supplied a durable response,
    so the streamed projection remains the only available body.  Any supplied
    terminal body—including an intentional empty string—is authoritative.
    """
    if terminal_body is None:
        return streamed_body, False
    return terminal_body, terminal_body != streamed_body


def join_report_continuation(prefix: str, continuation: str) -> str:
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


class ReportBodyProjection:
    """One report's mutable body, interim draft and clean terminal response."""

    def __init__(self, task, *, append_event, logger):
        self.text = ''
        self._round_text = ''
        self._terminal_text = None
        # Canonical body of earlier length-truncated rounds; None until the
        # worker continues a truncated response.
        self._continued_prefix = None
        self._task = task
        self._append_event = append_event
        self._logger = logger

    def begin_round(self):
        self._round_text = ''

    def append_delta(self, text, round_index):
        self._round_text += text
        self.text += text
        self._task['full_text'] = self.text
        self._append_event(self._task, {
            'type': 'delta', 'delta': text, 'llmRound': round_index,
        })

    def observe_response(self, message):
        # A tool round's prose is provisional. A supplied empty terminal body
        # is authoritative too; it must not promote an earlier streamed draft.
        if isinstance(message, dict) and not message.get('tool_calls'):
            content = message.get('content') or ''
            if self._continued_prefix is None:
                self._terminal_text = content
            else:
                self._terminal_text = self._continued_prefix + join_report_continuation(
                    self._continued_prefix, content)

    def continue_truncated_response(self):
        """Keep the observed truncated response as the next round's prefix.

        Returns False when the round added no visible text, so the caller
        stops continuing instead of looping on a model that cannot progress.
        """
        prefix = self._continued_prefix or ''
        terminal = self._terminal_text or ''
        if not terminal[len(prefix):].strip():
            return False
        self._continued_prefix = terminal
        return True

    def discard_tool_draft(self, round_index):
        # A model can draft before requesting tools, then rewrite after their
        # results. Withdraw only this round's prose; keep its thinking/tools.
        if self._round_text:
            self._logger.info(
                '[Paper:Report] Task %s — discarding %d-char interim draft '
                'emitted alongside tool calls (round %d)',
                self._task['task_id'], len(self._round_text), round_index + 1)
            self.text = self.text[:-len(self._round_text)]
            self._task['full_text'] = self.text
            self._append_event(self._task, {
                'type': 'delta_reset', 'llmRound': round_index,
            })

    def adopt_terminal_response(self):
        # Stream retries may deliver a whole response more than once. Adopt
        # the returned terminal response before enrichment or persistence and
        # converge pollers only when their projection actually differs.
        canonical_body, needs_reset = resolve_canonical_report_body(
            self.text, self._terminal_text)
        if not (canonical_body or '').strip():
            raise ValueError('Paper report completed without a non-empty body')
        if needs_reset:
            self._logger.info(
                '[Paper:Report] Task %s — replacing %d-char streamed body with '
                '%d-char terminal returned content (deltas were display-only; '
                'likely a mid-stream re-stream)',
                self._task['task_id'], len(self.text), len(canonical_body))
            self.text = canonical_body
            self._task['full_text'] = self.text
            self._append_event(self._task, {'type': 'delta_reset'})
            self._append_event(self._task, {'type': 'delta', 'delta': self.text})


__all__ = [
    "ReportBodyProjection",
    "join_report_continuation",
    "resolve_canonical_report_body",
]
