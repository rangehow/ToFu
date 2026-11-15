#!/usr/bin/env python3
"""Anthropic recount CPU of a repeated round over one long tool history.

Builds a 200-message OpenAI-shape history (about 100 assistant tool calls,
some with 50-200 KB arguments, CJK/emoji text, a thinking replay and an
image) and replays the stream-ingress steps of two consecutive rounds the way
the root loop runs them: a per-round text-identity scope on the task's count
memo, build_body's deep wire copy of the history, the canonical provider-view
admission, ``openai_body_to_anthropic`` and ``recount_anthropic_input``.
Round N+1 appends one assistant tool call and its result.

Reported: calling-thread CPU per ``recount_anthropic_input`` call in round
N+1 (after round N ran), and per translation, as p50 over ``--repeats``
alternating round pairs. ``counts`` lists the recount results; compare trees
only when they are equal. Compare trees in fresh processes with
``--source-root`` naming an exported tree.

    python3 benchmarks/anthropic_recount_rounds.py --repeats 15
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'claude-opus-4.6'
_WORDS = ('the', 'request', 'cache', 'tool', 'result', 'def', 'return', 'self',
          'value', 'None', 'path', 'storage', '上下文', '请求', '工具', '🙂', '"q"', '\\n')


def _text(rng, chars):
    words, total = [], 0
    while total < chars:
        word = rng.choice(_WORDS)
        words.append(word)
        total += len(word) + 1
    return ' '.join(words)


def _arguments(rng, index):
    if index % 9 == 0:
        # write_file / patch style: one large body.
        return json.dumps({'path': f'lib/module_{index}.py',
                           'content': _text(rng, rng.randint(50_000, 200_000))})
    if index % 5 == 0:
        return json.dumps({'calls': [{'name': 'read_files', 'arguments': {
            'reads': [{'path': f'lib/x{n}.py', 'start_line': n, 'end_line': n + 40}
                      for n in range(rng.randint(2, 12))]}}
            for _ in range(3)], 'note': _text(rng, 300), 'flag': True, 'ratio': 0.25},
            ensure_ascii=bool(index % 2))
    return json.dumps({'path': f'lib/m{index}.py', 'pattern': _text(rng, 40)})


def history(pairs=100, seed=7):
    rng = random.Random(seed)
    messages = [{'role': 'system', 'content': 'You are a coding agent. ' + _text(rng, 4000)},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': _text(rng, 600)},
                    {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/a.png'}}]}]
    for index in range(pairs):
        call = f'call_{index:04d}'
        assistant = {'role': 'assistant', 'content': _text(rng, 120) if index % 3 else '',
                     'tool_calls': [{'id': call, 'type': 'function', 'function': {
                         'name': 'write_file' if index % 9 == 0 else 'grep_search',
                         'arguments': _arguments(rng, index)}}]}
        if index % 7 == 0:
            assistant['reasoning_content'] = _text(rng, 800)
            assistant['thinking_signature'] = 'sig' + str(index)
        messages.append(assistant)
        messages.append({'role': 'tool', 'tool_call_id': call,
                         'content': _text(rng, rng.randint(200, 3000))})
    return messages


def tool_schemas():
    return [{'type': 'function', 'function': {
        'name': name, 'description': _text(random.Random(1), 400),
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}
        for name in ('write_file', 'grep_search')]


def run(repeats):
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm.request import detach_input, recount_anthropic_input
    from lib.llm_sanitize._fields import _strip_non_api_fields
    from lib.token_counter import RequestTextCounts
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY
    from lib.token_counter import count_tokens
    tools = tool_schemas()
    try:
        from lib.llm.anthropic_outbound._tool_inputs import (
            ANTHROPIC_TOOL_INPUTS_KEY, ToolInputs)
    except ImportError:  # trees before the task-owned tool-input memo
        ANTHROPIC_TOOL_INPUTS_KEY, ToolInputs = None, None
    full = history()
    recount_ms, translate_ms, counts = [], [], []
    for _ in range(repeats):
        memo = RequestTextCounts()
        inputs = ToolInputs() if ToolInputs else None
        for messages in (full[:-2], full):
            with memo.shared_text_identities():
                view = memo.view('provider')
                body = {'model': MODEL, 'max_tokens': 4096, 'tools': tools,
                        'messages': _strip_non_api_fields(messages)}
                detach_input(body)
                count_tokens(body['messages'], model=MODEL, tools=tools,
                             mode='tiktoken', text_count_memo=view)
                body[TEXT_COUNT_VIEW_KEY] = view
                if inputs is not None:
                    body[ANTHROPIC_TOOL_INPUTS_KEY] = inputs
                started = time.thread_time()
                wire = openai_body_to_anthropic(body)
                translated = time.thread_time()
                tokens = recount_anthropic_input(
                    wire, model=MODEL, provider_id='bench', text_count_view=view)
                finished = time.thread_time()
        translate_ms.append((translated - started) * 1000)
        recount_ms.append((finished - translated) * 1000)
        counts.append(tokens)
    return {'messages': len(full),
            'toolUse': sum(bool(m.get('tool_calls')) for m in full),
            'argumentBytes': sum(len(m['tool_calls'][0]['function']['arguments'].encode())
                                 for m in full if m.get('tool_calls')),
            'recountMsP50': round(statistics.median(recount_ms), 2),
            'recountMsMin': round(min(recount_ms), 2),
            'translateMsP50': round(statistics.median(translate_ms), 2),
            'counts': sorted(set(counts))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--repeats', type=int, default=15)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.source_root))
    print(json.dumps(run(args.repeats), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
