"""Structured tool-call reconstruction for the conversation message builder.

Expands stored ``toolRounds`` back into OpenAI-style
``assistant(tool_calls=[...])`` + ``tool(tool_call_id=..., content=...)``
message sequences.  This mirrors what
``lib.tasks_pkg.message_builder.inject_tool_history`` produces for Continue
requests, so the debug preview and the real request see the same structure.
"""

from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)


def _reconstruct_tool_call_messages(rounds: list[dict]) -> list[dict] | None:
    """Expand ``toolRounds`` into structured assistant/tool message pairs.

    Returns a list of messages on success, or ``None`` if any round
    lacks the data needed to reconstruct a proper tool_call sequence.
    Callers fall back to the legacy summary placeholder on ``None``.

    Required per-round fields:
      * ``toolCallId`` (non-empty str) — uniquely identifies the call
      * ``toolName`` (non-empty str)
      * ``status == 'done'`` — round ran to completion
      * ``toolContent`` (str) — the tool's result as seen by the model

    ``toolArgs`` is best-effort normalized to a JSON string suitable for
    ``function.arguments``.  ``assistantContent`` on the first round of
    a batch becomes the batch's assistant ``content`` (text written
    alongside the tool_calls, à la Claude).
    """
    # First pass: validate every round has the required data.
    for r in rounds:
        if not r.get('toolCallId'):
            return None
        if not r.get('toolName'):
            return None
        if r.get('status') != 'done':
            return None
        if r.get('toolContent') is None:
            return None

    # Group into batches by llmRound (preferred) or roundNum gap (legacy).
    has_llm_round = any(r.get('llmRound') is not None for r in rounds)
    batches: list[list[dict]] = []
    current: list[dict] = []
    prev_key = None
    for r in rounds:
        if has_llm_round:
            key = r.get('llmRound')
        else:
            key = r.get('roundNum')
            if current and isinstance(prev_key, int) and isinstance(key, int):
                # legacy: gap > 1 in roundNum → new batch
                if key > prev_key + 1:
                    batches.append(current)
                    current = []
        if current and has_llm_round and key != prev_key:
            batches.append(current)
            current = []
        current.append(r)
        prev_key = key
    if current:
        batches.append(current)

    out: list[dict] = []
    for batch in batches:
        tool_calls = []
        tool_results = []
        assistant_text = ''
        assistant_thinking = ''
        assistant_thinking_sig = ''
        for r in batch:
            tc_id = r['toolCallId']
            args_raw = r.get('toolArgs')
            if isinstance(args_raw, str):
                args_str = args_raw
            elif isinstance(args_raw, dict):
                try:
                    args_str = json.dumps(args_raw, ensure_ascii=False)
                except (TypeError, ValueError) as _e_audit:
                    logger.debug('[conv_message_builder] _reconstruct_tool_call_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
                    args_str = '{}'
            else:
                args_str = '{}'
            # Defense-in-depth: if a stored toolArgs string is itself not
            # valid JSON, replay it as ``'{}'`` so the upstream gateway
            # doesn't HTTP 400 ``invalid function arguments json string``.
            # The matching tool_result still tells the model the original
            # call failed, so this replay path stays equivalent to live
            # execution. See orchestrator.py:1364 (live sanitizer) and
            # the May 2026 incident memory.
            try:
                json.loads(args_str)
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[conv_message_builder] _reconstruct_tool_call_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
                args_str = '{}'
            tc_entry: dict = {
                'id': tc_id,
                'type': 'function',
                'function': {
                    'name': r['toolName'],
                    'arguments': args_str,
                },
            }
            # Gemini: echo back thought_signature verbatim — the OpenAI-compat
            # proxy requires it on every replayed tool_call or returns HTTP 400.
            # Unused by other providers (they strip unknown fields server-side).
            if r.get('extraContent'):
                tc_entry['extra_content'] = r['extraContent']
            tool_calls.append(tc_entry)
            tool_results.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': r['toolContent'] or '',
            })
            # First-seen assistantContent / thinking in the batch become the
            # assistant message's text + reasoning (Claude-style prefix).
            if not assistant_text and r.get('assistantContent'):
                assistant_text = r['assistantContent']
            if not assistant_thinking and r.get('thinking'):
                assistant_thinking = r['thinking']
            if not assistant_thinking_sig and r.get('thinkingSignature'):
                assistant_thinking_sig = r['thinkingSignature']

        asst_msg: dict = {'role': 'assistant', 'tool_calls': tool_calls}
        if assistant_text:
            asst_msg['content'] = assistant_text
        # Only attach thinking block when we have BOTH text and signature —
        # Anthropic rejects a thinking block with no signature; other
        # providers just ignore both fields.  This matches the gating in
        # lib/tasks_pkg/message_builder.inject_tool_history.
        if assistant_thinking and assistant_thinking_sig:
            asst_msg['reasoning_content'] = assistant_thinking
            asst_msg['thinking_signature'] = assistant_thinking_sig
        out.append(asst_msg)
        out.extend(tool_results)

    return out
