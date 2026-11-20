"""Tofu arm: build one deck with the slide recipe in this checkout.

Usage: python -m evaluations.slides_bench.run_tofu <doc_id> <out_dir>

The run goes through the common deliverable launcher
(``lib/production/deliverable_launch.py``, kind ``slides``), whose model-free
preflight checks the material file, the output folder and the Chromium
renderer before any model call; it calls the public recipe entry point
``build_deck_from_topic`` exactly as ``produce_slides`` does, with two
benchmark-only substitutions recorded in ``run.json``:

- model calls go through the running server's routing (``llm_bridge``),
  including the visual-QA vision calls: this process has no dispatcher, so
  the vision-slot probe answers with the bridged model instead;
- web research and image generation are disabled, because the competitor arms
  receive no web or image service either and the inputs are the authority.

The authored deck, as it stood before the layout and visual repair stages
edited it, is copied to the sibling arm ``tofu-authored/<doc>/deck``
(``run_arm_all.sh`` exports and renders it), so every generation yields a
paired repair-off / repair-on comparison from the same authored pages.
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


def _vision_through_bridge() -> None:
    import lib.design_sys.visual_qa as vqa
    vqa._vision_model = lambda: llm_bridge.DEFAULT_MODEL


def _snapshot_authored_deck(out_dir: str) -> None:
    import shutil

    import lib.slides.recipe as recipe
    original = recipe._run_layout_qa
    run_dir = os.path.dirname(os.path.dirname(os.path.abspath(out_dir)))
    target = os.path.join(run_dir, 'tofu-authored', os.path.basename(
        os.path.abspath(out_dir)), 'deck')

    def run_layout_qa(ctx):
        # A stage retry starts from already repaired pages: keep the first copy.
        if not os.path.isdir(target):
            shutil.copytree(ctx['deck_dir'], target,
                            ignore=shutil.ignore_patterns('preview', '.tofu-qa'))
        return original(ctx)

    recipe._run_layout_qa = run_layout_qa


def build(doc_id: str, out_dir: str) -> dict:
    from lib.production import deliverable_launch as dl
    doc = load_document(doc_id)
    _vision_through_bridge()
    if os.environ.get('SLIDES_BENCH_TOFU_BASELINE') == '1':
        _lift_outline_budget()
    import lib.production.research as research
    import lib.image_gen as image_gen
    research.research_topic = _no_research
    image_gen.generate_image = _no_image
    _snapshot_authored_deck(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    # The corpus material becomes a declared input file, so the common
    # launcher's preflight checks it like any other deliverable input.
    material = os.path.join(out_dir, 'material.md')
    with open(material, 'w', encoding='utf-8') as fh:
        fh.write(doc['material_for_tofu'])
    contract = dl.load_contract({
        'schema_version': dl.CONTRACT_SCHEMA_VERSION, 'kind': 'slides',
        'inputs': {'material': material}, 'output_dir': out_dir,
        'model': {'name': llm_bridge.DEFAULT_MODEL, 'endpoint': llm_bridge.ENDPOINT},
        'parameters': {'topic': doc['title'], 'lang': doc['lang'],
                       'max_pages': doc['pages'], 'creative_mode': 'director'}})
    started = time.time()
    launched = dl.launch(contract, install_model_route=llm_bridge.install_model_route,
                         infra_exceptions=(llm_bridge.ModelEndpointUnavailable,))
    if not launched['delivered']:
        raise RuntimeError(f'{launched["outcome"]}: {launched.get("detail")}')
    summary = launched['summary']
    record = {
        'arm': 'tofu', 'doc_id': doc_id, 'model': llm_bridge.DEFAULT_MODEL,
        'seconds': round(time.time() - started, 1),
        'pptx': summary['pptx'], 'pages': summary['pages'],
        'quality': summary['quality'], 'visual_qa': summary['visual_qa'],
        'material_chars_supplied': len(doc['material_for_tofu']),
        'source_chars': len(doc['text']),
    }
    with open(os.path.join(out_dir, 'run.json'), 'w', encoding='utf-8') as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    return record


if __name__ == '__main__':
    print(json.dumps(build(sys.argv[1], sys.argv[2]), ensure_ascii=False))
