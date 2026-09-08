"""Action-specific research context projections and token admission.

Durable workspace bytes are never edited by projection. Omitted text is marked;
compile/publication requiring exact sources fail before dispatch if they do not
fit, rather than compiling a silently truncated manuscript.
"""
from __future__ import annotations

from copy import deepcopy
import json

ACTION_INPUT_TOKEN_CEILING = 48_000
ACTION_OUTPUT_TOKEN_CEILING = 8_000
UNKNOWN_MODEL_CONTEXT_WINDOW = 32_000


def action_window(model: str) -> int:
    from lib.model_info._context import context_profile
    return int(context_profile(model).get('window') or UNKNOWN_MODEL_CONTEXT_WINDOW)


def output_budget(model: str) -> int:
    return min(ACTION_OUTPUT_TOKEN_CEILING, max(512, action_window(model) // 8))


def project_workspace(workspace, action):
    context = deepcopy(dict(workspace))
    context.pop('publication', None)
    context.pop('capability_bindings', None)  # sent once, outside workspace
    omitted = []
    if action not in {'manuscript', 'compile', 'publish'}:
        context.pop('source_files', None)
    for run in context.get('runs') or []:
        for receipt in run.get('tool_receipts') or []:
            if receipt.get('result_excerpt'):
                receipt.pop('result_excerpt', None)
                receipt['excerpt_omitted'] = True
                omitted.append('historical_receipt_excerpts')
    context['context_projection'] = {
        'omitted': sorted(set(omitted)),
        'note': 'Use recorded artifact references to inspect original evidence; omitted text is not verified.',
    }
    return context


def admit_messages(messages, *, model, tools=None, max_output=None):
    """Fail closed before a paid call, counting tools and output reservation."""
    from lib.token_counter import count_text
    wire = json.dumps({'messages': messages, 'tools': tools or []},
                      ensure_ascii=False, separators=(',', ':'), default=str)
    tokens = count_text(wire, model=model)
    reserve = output_budget(model) if max_output is None else max_output
    ceiling = min(ACTION_INPUT_TOKEN_CEILING, action_window(model) - reserve - 2048)
    if tokens > ceiling:
        raise ValueError(f'research context requires {tokens} input tokens; budget {ceiling}. '
                         'Reduce the working set or split the action; exact sources were not truncated.')
    return tokens
