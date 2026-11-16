"""Blind vision judging and text-level content audit for benchmark decks.

Both scorers call the model through Tofu's server routing (``llm_bridge``),
never the canvas platform.

Judge (vision): each deck is scored on its own, under a random label, in a
seeded random order, with arm names, file names and tool branding removed
from everything the judge sees. The judge receives the source document and
the LibreOffice renders of every slide and scores the written RUBRIC.

Audit (text): python-pptx extracts each slide's visible text; a text model
classifies every sentence/bullet against the source as a grounded specific
fact, grounded but generic, unsupported, or filler. A deterministic number
check counts numbers on slides that do not occur in the source.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import re

from evaluations.slides_bench import llm_bridge

JUDGE_MODEL = os.environ.get('SLIDES_BENCH_JUDGE_MODEL', 'gpt-5.5')
_JUDGE_IMAGE_WIDTH_PX = 1280

RUBRIC = """\
Score each dimension 1-10 (integers). Anchors: 1-2 unusable, 3-4 weak,
5-6 acceptable, 7-8 strong, 9-10 professional conference/interview quality.

CONTENT (judge against the SOURCE document):
- specificity: slides state concrete claims with the source's numbers, names,
  conditions and results. Low if bullets are generic ("improves efficiency",
  "key challenges") or could describe any project.
- faithfulness: every claim and number matches the source; no invented,
  inflated or mis-attributed facts; caveats the source attaches (not
  significant, single run, estimate) are preserved. Low if anything is made up.
- takeaways: each content slide has one clear message, ideally an assertion
  title ("X cut Y by 40%"), and the body supports it. Low for topic titles
  ("Results", "Overview") with loose bullets.
- coverage: the deck carries the source's most important results and
  conclusions, not peripheral material; nothing essential is missing.
- no_filler: no empty phrases, slogans, agenda/thank-you padding, or
  repeated content. 10 = every sentence carries information.
NARRATIVE:
- narrative: a clear arc for the audience (problem -> approach -> evidence ->
  limits -> conclusion); slides build on each other; ending answers the opening.
VISUAL:
- visual_design: professional, consistent and attractive styling; purposeful
  color/typography; real charts/tables/diagrams where data exists instead of
  text walls or decoration.
- layout_readability: legible font sizes, no overflow/clipping/overlap,
  balanced whitespace, clear hierarchy; readable when projected.
"""

DIMENSIONS = ('specificity', 'faithfulness', 'takeaways', 'coverage',
              'no_filler', 'narrative', 'visual_design', 'layout_readability')
CONTENT_DIMENSIONS = DIMENSIONS[:5]


def slide_texts(pptx: str) -> list[str]:
    from pptx import Presentation
    deck = Presentation(pptx)
    out = []
    for slide in deck.slides:
        parts: list[str] = []

        def visit(shape) -> None:
            if shape.shape_type == 6:  # group
                for child in shape.shapes:
                    visit(child)
                return
            if getattr(shape, 'has_text_frame', False) and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if getattr(shape, 'has_table', False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(' | '.join(cell.text.strip()
                                            for cell in row.cells))
            if getattr(shape, 'has_chart', False) and shape.has_chart:
                chart = shape.chart
                try:
                    categories = list(chart.plots[0].categories)
                    parts.append('[chart] ' + ', '.join(map(str, categories)))
                    for series in chart.plots[0].series:
                        parts.append(f'[series {series.name}] '
                                     + ', '.join(map(str, series.values)))
                except (IndexError, KeyError, ValueError):
                    parts.append('[chart]')

        for shape in slide.shapes:
            visit(shape)
        out.append('\n'.join(parts))
    return out


def _image_part(path: str) -> dict:
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert('RGB')
        if image.width > _JUDGE_IMAGE_WIDTH_PX:
            height = round(image.height * _JUDGE_IMAGE_WIDTH_PX / image.width)
            image = image.resize((_JUDGE_IMAGE_WIDTH_PX, height))
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=85)
    data = base64.b64encode(buffer.getvalue()).decode('ascii')
    return {'type': 'image_url',
            'image_url': {'url': f'data:image/jpeg;base64,{data}'}}


def _json_object(content: str) -> dict:
    match = re.search(r'\{.*\}', content or '', re.DOTALL)
    if not match:
        raise ValueError(f'no JSON object in reply: {content[:200]!r}')
    return json.loads(match.group(0))


def judge_deck(source: str, pngs: list[str], *, label: str, lang: str) -> dict:
    prompt = (
        'You are an expert presentation reviewer (conference program chair '
        'and senior interviewer). Review ONE anonymous slide deck that was '
        'generated from the SOURCE document below. You see every slide as an '
        'image, in order. Judge only what is visible.\n\n'
        f'## Rubric\n{RUBRIC}\n'
        '## Output\nReturn ONLY JSON: {"scores": {"specificity": n, '
        '"faithfulness": n, "takeaways": n, "coverage": n, "no_filler": n, '
        '"narrative": n, "visual_design": n, "layout_readability": n}, '
        '"worst_slides": [{"slide": n, "problem": "concrete defect"}], '
        '"best_slide": n, "summary": "two sentences: biggest content gap and '
        'biggest visual gap"}. Up to 4 worst slides.\n\n'
        f'## Deck\nLabel: {label}; {len(pngs)} slides; deck language {lang}.\n\n'
        f'## SOURCE document\n{source}\n')
    content: list = [{'type': 'text', 'text': prompt}]
    for index, png in enumerate(pngs):
        content.append({'type': 'text', 'text': f'Slide {index + 1}:'})
        content.append(_image_part(png))
    reply, usage = llm_bridge.chat(
        [{'role': 'user', 'content': content}], model=JUDGE_MODEL,
        max_tokens=3000, temperature=0.0)
    result = _json_object(reply)
    scores = result.get('scores') or {}
    result['scores'] = {name: int(scores[name]) for name in DIMENSIONS}
    result['usage_model'] = usage.get('model')
    return result


def blind_order(doc_id: str, arms: list[str], seed: str) -> list[tuple[str, str]]:
    """Seeded random (label, arm) assignment; labels carry no arm identity."""
    rng = random.Random(hashlib.sha256(f'{seed}:{doc_id}'.encode()).digest())
    shuffled = list(arms)
    rng.shuffle(shuffled)
    return [(f'Deck-{hashlib.sha256(f"{seed}:{doc_id}:{arm}".encode()).hexdigest()[:6]}',
             arm) for arm in shuffled]


_NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)*')


def _numbers(text: str) -> set[str]:
    return {token.replace(',', '') for token in _NUMBER_RE.findall(text)
            if len(token.replace(',', '').replace('.', '')) >= 2}


#: A reasoning judge spends output budget before it writes; 6000 returned an
#: empty reply for a 10-slide deck. 16384 is the judge's output allowance
#: used by the slide recipe's outline stage.
_AUDIT_MAX_TOKENS = 16384


def audit_deck(source: str, texts: list[str]) -> dict:
    source_numbers = _numbers(source)
    slide_numbers = [_numbers(text) for text in texts]
    unsupported_numbers = sorted({n for numbers in slide_numbers
                                  for n in numbers if n not in source_numbers})
    listing = '\n\n'.join(f'### Slide {i + 1}\n{text or "(no text)"}'
                          for i, text in enumerate(texts))
    prompt = (
        'Audit the text of a slide deck against its SOURCE document. Split each '
        "slide's text into statements (one bullet, sentence or table row each; "
        'ignore page numbers and pure section labels). Classify each:\n'
        '- specific: a concrete fact, number, name, condition or result that the '
        'SOURCE supports;\n'
        '- generic: supported by the source but vague (could fit many '
        'projects/papers);\n'
        '- unsupported: not stated in the SOURCE, contradicts it, or drops a '
        'caveat the source attaches;\n'
        '- filler: carries no information (slogans, "thank you", "key '
        'insights", transitions).\n'
        'Also mark title_is_assertion: true when the slide title states a '
        'claim/result rather than a topic.\n'
        'Return ONLY JSON: {"slides": [{"slide": n, "specific": n, '
        '"generic": n, "unsupported": n, "filler": n, "title_is_assertion": '
        'true, "unsupported_examples": ["..."]}]}\n\n'
        f'## SOURCE\n{source}\n\n## DECK TEXT\n{listing}\n')
    reply, _usage = llm_bridge.chat(
        [{'role': 'user', 'content': prompt}], model=JUDGE_MODEL,
        max_tokens=_AUDIT_MAX_TOKENS, temperature=0.0)
    slides = _json_object(reply).get('slides') or []
    totals = {key: sum(int(row.get(key) or 0) for row in slides)
              for key in ('specific', 'generic', 'unsupported', 'filler')}
    count = max(1, len(texts))
    statements = max(1, sum(totals.values()))
    return {
        'slides': slides,
        'slide_count': len(texts),
        'totals': totals,
        'specific_per_slide': round(totals['specific'] / count, 2),
        'specific_share': round(totals['specific'] / statements, 3),
        'unsupported_per_slide': round(totals['unsupported'] / count, 2),
        'filler_per_slide': round(totals['filler'] / count, 2),
        'assertion_title_share': round(
            sum(bool(row.get('title_is_assertion')) for row in slides) / count, 3),
        'numbers_on_slides': sum(len(n) for n in slide_numbers),
        'numbers_not_in_source': unsupported_numbers[:40],
        'numbers_not_in_source_count': len(unsupported_numbers),
    }
