"""lib/tasks_pkg/segments/_types.py — the closed vocabulary constants.

These are the segment-type tags and the resumable-finish-reason set. They are
defined here ONCE and re-exported from the package facade so every consumer
shares the SAME constant object (tests import ``SEG_THINKING`` / ``SEG_TEXT`` /
``SEG_TOOL_USE`` / ``RESUMABLE_FINISH_REASONS`` by name).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# Segment type constants — the closed vocabulary (Anthropic content-blocks shape).
SEG_THINKING = 'thinking'
SEG_TEXT = 'text'
SEG_TOOL_USE = 'tool_use'
# The nested tool-result shape lives inside a tool_use segment under
# ``result`` ({content, status}); there is no standalone tool_result segment
# tag today. Provided as a named constant for symmetry / forward-compat so
# consumers that reference it by name resolve against this single object.
SEG_TOOL_RESULT = 'tool_result'

# Finish reasons under which the terminal deliverable text is a RESUMABLE
# prefix (a turn cut off mid-answer), not a settled answer. Continue can feed
# this tail back as an assistant prefill so a capable provider continues the
# SAME tokens rather than regenerating from scratch. ``length`` (model hit
# max_tokens) is the canonical Continue case; the three interrupt reasons cover
# a dropped transport / server crash / frontend stop.
RESUMABLE_FINISH_REASONS = frozenset({
    'interrupted', 'server_offline', 'premature_close', 'length',
})
