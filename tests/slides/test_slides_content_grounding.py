"""Content-grounding contracts from the slides benchmark (docs/SLIDES_BENCHMARK.md).

Supplied material becomes verbatim evidence units that reach page authors;
visible numbers must come from the evidence; text below the projection
legibility floor is rejected. Layout repair lives in test_slides_layout_repair.py.
"""

from __future__ import annotations

import json

import pytest
import yaml

from lib.slides import author, recipe
from lib.slides.material import evidence_numbers, material_cards
from lib.slides.pptd import Deck

pytestmark = pytest.mark.unit

MATERIAL = """# Results
Agent A vs Agent B on Bench-X: 512 vs 487 (p=0.0031).

- Token use 12.5% lower on average.
- Model-Y difference not significant (p=0.090).

| metric | A | B |
|---|---|---|
| cost/task | $3.21 | $3.08 |
"""


def _deck(title='评测'):
    return Deck(title=title, size=(1280, 720),
                theme={'textStyles': {'body': {'fontSize': 18},
                                      'caption': {'fontSize': 14}}},
                pages=[], root='/tmp')


def test_material_units_are_verbatim_and_lossless():
    cards = material_cards(MATERIAL)
    assert [c['id'] for c in cards] == [f'M{i}' for i in range(1, 6)]
    assert cards[0]['section'] == 'Results'
    assert cards[1]['point'] == '- Token use 12.5% lower on average.'
    assert cards[4]['point'] == ('| metric | A | B |\n'
                                 '| cost/task | $3.21 | $3.08 |')
    joined = '\n'.join(c['point'] for c in cards)
    for line in MATERIAL.splitlines():
        if line.strip() and not line.startswith('#') and '---' not in line:
            assert line.strip() in joined


def test_supplied_material_replaces_web_research(monkeypatch):
    import lib.production.research as research
    monkeypatch.setattr(research, 'research_topic',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    out = recipe._run_research({'topic': 'Agent A', 'material': MATERIAL})
    assert out['cards'] == [] and len(out['material_cards']) == 5


def test_outline_attaches_cited_units_and_gate_requires_citations():
    research = {'cards': [], 'material_cards': material_cards(MATERIAL)}
    pages = [{'pageType': 'cover', 'key_message': 'Agent A'},
             {'pageType': 'content', 'key_message': '512 vs 487',
              'content_notes': 'Bench-X [M1]; caveat [M3]'},
             {'pageType': 'content', 'key_message': 'cost',
              'content_notes': 'no citation'},
             {'pageType': 'final', 'key_message': 'end'}]
    candidate = recipe._outline_candidate(
        json.dumps({'title': 'T', 'pages': pages}),
        topic='T', research=research, maximum=12, usage={})
    assert [s['id'] for s in candidate['pages'][1]['sources']] == ['M1', 'M3']
    errors = recipe._outline_errors(
        {'artifacts': {'research': research}}, candidate)
    assert any('content pages [3]' in e for e in errors)


def _cut_off_outline():
    """An outline reply that hit the output limit after two complete pages
    (slides_6a8ef6c574674ddb: 4096 completion tokens, reasoning included)."""
    pages = [{'pageType': 'cover', 'key_message': '先把账算对'},
             {'pageType': 'content', 'key_message': '排班 47 → 9 分钟',
              'content_notes': '月报 [M1]'},
             {'pageType': 'content', 'key_message': '违约率 2.8%'}]
    full = json.dumps({'title': 'T', 'pages': pages}, ensure_ascii=False)
    return full[:full.rindex('{"pageType": "content", "key_message": "违约') + 30]


@pytest.mark.parametrize('usage', [{'finish_reason': 'length',
                                    'completion_tokens': 4096}, {}])
def test_a_cut_off_outline_reply_is_reported_as_cut_off(usage):
    """A reply truncated at the output limit is not malformed JSON: the
    greedy object match ended at the last complete page and json reported
    "Expecting ',' delimiter", hiding the real cause from logs and retries."""
    research = {'cards': [], 'material_cards': material_cards(MATERIAL)}
    with pytest.raises(ValueError) as caught:
        recipe._outline_candidate(_cut_off_outline(), topic='T',
                                  research=research, maximum=12, usage=usage)
    message = str(caught.value)
    assert 'delimiter' not in message
    assert 'cut off' in message
    if usage:
        assert '4096' in message


def test_outline_prompt_output_budget_fits_the_largest_material_deck():
    """A 16-page material-grounded Chinese outline measured 5.5-6k completion
    tokens (reasoning included) on gpt-5.6-sol; 4096 cut every reply off."""
    assert recipe._OUTLINE_MAX_TOKENS >= 12000


def test_author_prompt_carries_verbatim_evidence():
    brief = {'pageType': 'content', 'key_message': 'k',
             'sources': material_cards(MATERIAL)[:1]}
    prompt = author._build_prompt(_deck(), brief, 1, 4, '', '', '', [], 'zh')
    assert '512 vs 487 (p=0.0031)' in prompt
    assert '[M1]' not in prompt


def test_invented_text_number_is_rejected():
    numbers = sorted(str(n) for n in evidence_numbers([MATERIAL]))
    brief = {'key_message': 'k', 'deck_evidence_numbers': numbers}
    page = {'elements': [{'elementId': 't', 'elementType': 'text',
                          'bounds': [0, 0, 100, 100],
                          'content': {'text': '512 vs 487，节省 23.4%'}}]}
    findings = author._brief_fidelity_findings(
        brief, yaml.safe_dump(page, allow_unicode=True), page_index=1, total=8)
    assert findings and '23.4' in findings[0] and '512' not in findings[0]
    page['elements'][0]['content']['text'] = '512 vs 487，第 3 步'
    assert author._brief_fidelity_findings(
        brief, yaml.safe_dump(page, allow_unicode=True),
        page_index=1, total=8) == []


def test_legibility_floor_and_decorative_latin_labels():
    small = [{'elementId': 'cap', 'elementType': 'text',
              'content': {'style': '$body', 'fontSize': 11, 'text': 'x'}},
             {'elementId': 'ok', 'elementType': 'text',
              'content': {'style': '$caption', 'text': 'x'}}]
    findings = author._legibility_findings(_deck(), small)
    assert len(findings) == 1 and 'cap' in findings[0]
    text = yaml.safe_dump({'elements': [
        {'elementId': 'k', 'elementType': 'text', 'bounds': [0, 0, 400, 40],
         'content': {'fontSize': 16, 'text': 'RISK RADAR'}}]})
    assert any('英文' in f for f in author._validate_page_text(
        _deck(), 'pages/01.page', text))


def test_overflow_is_measured_on_line_boxes_not_font_content_areas():
    from lib.slides.layout_qa import _page_findings

    def record(outer_top, outer_bottom):
        return {'id': 't', 'visible': True, 'fontSize': 20, 'lineHeight': 25,
                'outer': {'left': 0, 'top': outer_top, 'right': 400,
                          'bottom': outer_bottom},
                'rects': [{'left': 0, 'top': 98, 'right': 300,
                           'bottom': 127, 'width': 300, 'height': 29}]}

    # 29px content area on a 25px line: 2px half-leading above and below.
    assert _page_findings([record(100, 125)], 0, tolerance=1.0) == []
    overflow = _page_findings([record(100, 118)], 0, tolerance=1.0)
    assert overflow[0]['overflow'] == {'bottom': 7.0}

    # Stacked boxes whose line boxes touch: content areas overlap by the
    # half-leading (2 + 2 px), which paints no glyph over another.
    upper = {**record(100, 125), 'id': 'upper'}
    lower = {**record(125, 150), 'id': 'lower',
             'rects': [{'left': 0, 'top': 123, 'right': 300, 'bottom': 152,
                        'width': 300, 'height': 29}]}
    assert _page_findings([upper, lower], 0, tolerance=1.0) == []
    lower['rects'][0].update(top=113, bottom=142)
    assert _page_findings([upper, lower], 0, tolerance=1.0)[0]['type'] in (
        'text_overflow', 'text_collision')


def test_a_latin_caps_label_the_evidence_names_is_a_fact_not_decoration():
    text = yaml.safe_dump({'elements': [
        {'elementId': 'k', 'elementType': 'text', 'bounds': [0, 0, 400, 40],
         'content': {'fontSize': 16, 'text': 'ACMEQA'}}]})
    brief = {'sources': [{'point': '第二个 benchmark 是 ACMEQA，测长文档问答。'}]}
    assert not any('英文' in f for f in author._validate_page_text(
        _deck(), 'pages/01.page', text, brief))
    assert any('英文' in f for f in author._validate_page_text(
        _deck(), 'pages/01.page', text))


def test_a_tag_outside_the_rich_text_vocabulary_is_an_author_finding():
    def page(text):
        return yaml.safe_dump({'elements': [
            {'elementId': 'k', 'elementType': 'text', 'bounds': [0, 0, 600, 80],
             'content': {'fontSize': 18, 'text': text}}]})
    dropped = author._validate_page_text(
        _deck(), 'pages/01.page',
        page('<p>把配置写进提示词的 <ctx> 块</p>'))
    assert any("'<ctx>'" in f for f in dropped)
    assert not any('标签' in f for f in author._validate_page_text(
        _deck(), 'pages/01.page',
        page('<p>把配置写进 <strong>提示词</strong> 的 &lt;ctx&gt; 块<br/>次行</p>')))


def _one_text_page(text):
    return yaml.safe_dump({'elements': [
        {'elementId': 'k', 'elementType': 'text', 'bounds': [0, 0, 600, 80],
         'content': {'fontSize': 18, 'text': text}}]}, allow_unicode=True)


def test_an_english_deck_page_written_in_chinese_is_refused():
    """e2e (2026-11-17): the English brief's content pages came back in
    Chinese; the English prompt carries Chinese format examples and theme
    labels and never named the page language."""
    deck = _deck('Maya Okafor interview walkthrough')
    brief = {'key_message': 'LoadCast is 13.2x faster than vendor A',
             'content_notes': 'Office: 港湾电网 (Harborline Grid) [M3]'}
    chinese = _one_text_page('<p>LoadCast 快 13.2 倍</p>')
    refused = author._validate_page_text(deck, 'pages/01.page', chinese, brief,
                                         lang='en')
    assert any('English' in finding for finding in refused)
    # A CJK name the evidence itself quotes is a fact, not a language slip.
    assert not any('English' in finding for finding in author._validate_page_text(
        deck, 'pages/01.page', _one_text_page('<p>Harborline Grid (港湾电网)</p>'),
        brief, lang='en'))
    # The deck language is what was requested, never guessed from a title.
    assert not any('English' in finding for finding in author._validate_page_text(
        deck, 'pages/01.page', chinese, brief, lang='zh'))


def test_the_author_refuses_an_english_page_in_chinese(tmp_path, monkeypatch):
    deck = _deck('Grid ML')
    english = _one_text_page('<p>LoadCast is 13.2x faster</p>')
    replies = [(_one_text_page('<p>LoadCast 快 13.2 倍</p>'), {'finish_reason': 'stop'}),
               (english, {'finish_reason': 'stop'})]
    calls = []

    def llm(messages, **_kwargs):
        calls.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(author, '_llm', llm)
    result = author.author_page(deck, {'pageType': 'content', 'key_message':
                                       'LoadCast is 13.2x faster'}, 0, 1,
                                lang='en', max_rounds=2)
    assert result['mode'] == 'authored' and result['yaml'] == english.strip()
    assert 'English' in calls[1][-1]['content']


def test_english_author_prompt_names_the_page_language():
    prompt = author._build_prompt(_deck('Grid ML'), {'pageType': 'content',
                                                    'key_message': 'k'},
                                  1, 4, '', '', '', [], 'en')
    assert 'Write every visible word on the page in English' in prompt
