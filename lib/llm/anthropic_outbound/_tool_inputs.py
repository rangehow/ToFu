"""Task-owned exact memo: tool-call ``arguments`` text -> Messages ``input``.

Every model round translates the whole history, so every canonical tool call
was decoded into a fresh ``tool_use.input`` dict, and the final Anthropic
recount encoded each of those dicts again. The canonical ``arguments`` strings
are the only objects that keep their identity across rounds (build_body and
ingress detachment copy containers but share strings), so this memo maps a
held ``arguments`` string to the settled (deep-immutable) decoded input. A
settled input keeps derived values, such as the recount's compact encoding,
in its own memo slot (``lib.llm.request.tool_input_text``), so a later round
over the same history decodes and encodes only new calls.

Exactness: a key holds its string, so an identity hit is the same immutable
text, and decoding is a pure function of it. Retention: ``publish`` replaces
the owner's entries with exactly the calls of the latest translated request,
so it never outgrows one request and calls compaction removed leave with the
next round. The task dict owns the memo (``ANTHROPIC_TOOL_INPUTS_KEY`` on its
bodies); it is dropped with the task's other heavy terminal fields.

Entry points: ``ToolInputs``, ``tool_inputs_for_body``.
Dependencies: ``lib.conversation_sync.settled_rows``, ``lib.llm.conversion``.
"""

from __future__ import annotations

from lib.conversation_sync.settled_rows import settle
from lib.llm.conversion import object_arguments

ANTHROPIC_TOOL_INPUTS_KEY = '_anthropic_tool_inputs'
"""A task body's ``ToolInputs`` owner; never a wire field (underscore)."""


class ToolInputs:
    """One owner's decoded inputs of its latest translated request."""

    __slots__ = ('_held',)

    def __init__(self) -> None:
        # id(arguments) -> (arguments, settled input). Replaced, never edited.
        self._held: dict[int, tuple[str, object]] = {}

    def __copy__(self) -> 'ToolInputs':
        return self

    def __deepcopy__(self, _memo) -> 'ToolInputs':
        # Copied task dicts and request bodies share the owner's memo.
        return self

    def request(self) -> '_RequestToolInputs':
        return _RequestToolInputs(self)


class _RequestToolInputs:
    """Decode one request's calls, reusing the previous request's inputs."""

    __slots__ = ('_owner', '_previous', '_current')

    def __init__(self, owner: ToolInputs) -> None:
        self._owner = owner
        self._previous = owner._held
        self._current: dict[int, tuple[str, object]] = {}

    def __call__(self, arguments):
        """Exactly ``object_arguments(arguments)``, settled for string input."""
        if type(arguments) is not str:
            return object_arguments(arguments)
        key = id(arguments)
        held = self._current.get(key) or self._previous.get(key)
        if held is not None and held[0] is arguments:
            value = held[1]
        else:
            value = settle(object_arguments(arguments))
        self._current[key] = (arguments, value)
        return value

    def publish(self) -> None:
        self._owner._held = self._current


def tool_inputs_for_body(body: dict):
    """The request decoder for ``body``: memoized with an owner, else plain."""
    owner = body.get(ANTHROPIC_TOOL_INPUTS_KEY)
    return owner.request() if isinstance(owner, ToolInputs) else None
