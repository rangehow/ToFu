"""Tofu arm: build one deck with the slide recipe in this checkout.

Usage: python -m evaluations.slides_bench.run_tofu <doc_id> <out_dir>

The run uses the public recipe entry point ``build_deck_from_topic`` exactly
as ``produce_slides`` does, with two benchmark-only substitutions recorded in
``run.json``:

- model calls go through the running server's routing (``llm_bridge``);
- web research and image generation are disabled, because the competitor arms
  receive no web or image service either and the inputs are the authority.
"""

from __future__ import annotations

import json
import os
import sys
import time

from evaluations.slides_bench import llm_bridge
from evaluations.slides_bench.corpus import load_document


def _no_research(topic: str, **_kwargs) -> dict:
    return {'cards': [], 'as_of': '', 'degraded': False,
            'reason': 'benchmark: web research disabled for every arm'}


def _no_image(*_args, **_kwargs) -> dict:
    return {'ok': False, 'error': 'benchmark: image generation disabled'}


# Baseline-only: the shipped outline call asks for 4096 output tokens, which a
# material-grounded 10-page Chinese outline exceeds (reply truncated, JSON
# parse fails on every candidate, stage fails; see the report, finding F0).
# The baseline arm lifts only that budget so its later stages can be measured.
_BASELINE_OUTLINE_MAX_TOKENS = 16384


def _lift_outline_budget() -> None:
    import lib.slides.recipe as recipe
    original = recipe._llm_chat

    def chat(messages, **kwargs):
        if str(kwargs.get('log_prefix', '')).startswith('[Slides:outline:'):
            kwargs['max_tokens'] = max(int(kwargs.get('max_tokens') or 0),
                                       _BASELINE_OUTLINE_MAX_TOKENS)
        return original(messages, **kwargs)

    recipe._llm_chat = chat


def build(doc_id: str, out_dir: str) -> dict:
    doc = load_document(doc_id)
    llm_bridge.install()
    if os.environ.get('SLIDES_BENCH_TOFU_BASELINE') == '1':
        _lift_outline_budget()
    import lib.production.research as research
    import lib.image_gen as image_gen
    research.research_topic = _no_research
    image_gen.generate_image = _no_image
    from lib.slides.recipe import build_deck_from_topic
    os.makedirs(out_dir, exist_ok=True)
    started = time.time()
    result = build_deck_from_topic(
        doc['title'], out_dir, lang=doc['lang'], max_pages=doc['pages'],
        model=llm_bridge.DEFAULT_MODEL, material=doc['material_for_tofu'],
        creative_mode='director')
    record = {
        'arm': 'tofu', 'doc_id': doc_id, 'model': llm_bridge.DEFAULT_MODEL,
        'seconds': round(time.time() - started, 1),
        'pptx': result['pptx_path'], 'pages': result['pages'],
        'quality': result.get('quality'),
        'material_chars_supplied': len(doc['material_for_tofu']),
        'source_chars': len(doc['text']),
    }
    with open(os.path.join(out_dir, 'run.json'), 'w', encoding='utf-8') as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    return record


if __name__ == '__main__':
    print(json.dumps(build(sys.argv[1], sys.argv[2]), ensure_ascii=False))
