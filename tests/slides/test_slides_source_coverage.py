"""Source coverage at the outline and authoring stage (lib/slides/coverage.py).

A paper deck once described its main figure in words and left out a whole
experiment's numbers. The outline is now checked against an inventory of the
source's headline results and captioned figures, gaps are filled before any
page is written, and a page that carries a result or figure must show its
numbers. Synthetic paper text only; no benchmark document is quoted.
"""

from __future__ import annotations

import json

import pytest
import yaml

from lib.slides import author, recipe
from lib.slides.coverage import (
    assign_coverage,
    figure_index,
    measured_numbers,
    parse_inventory,
)
from lib.slides.material import material_cards

pytestmark = pytest.mark.unit

# PDF-extracted text: no blank lines, captions and an appendix table inline.
PAPER = """Abstract
We study widget recall in long inputs.
Results
Figure 2 shows recall by position. Using Model-2.5 recall falls from 71.4%
at the start to 48.9% in the middle (Figure 2).
Figure 2: Recall by position of the relevant widget.
Section 3.1 Lookup task
On the lookup task the worst case is 37.2% (Fig-
ure 4), while Model-A is near perfect.
Figure 4: Lookup accuracy by position.
Table 1: Closed-book accuracy is 52.6% for Model-B.
Appendix
This section tabulates Figure 2: Index 0 71.4% Index 9 48.9% Index 19 66.0%
"""


def _cards():
    return material_cards(PAPER)


def _unit(cards, text):
    return next(c['id'] for c in cards if text in c['point'])


def test_captions_become_their_own_units_in_pdf_text():
    cards = _cards()
    starts = [c['point'][:9] for c in cards]
    assert 'Figure 2:' in starts and 'Figure 4:' in starts
    assert 'Table 1: ' in starts


def test_figure_index_links_each_figure_to_the_units_stating_its_values():
    cards = _cards()
    index = figure_index(cards)
    fig2 = index['Figure 2']
    assert fig2['caption'] == _unit(cards, 'Figure 2:')
    assert {'71.4', '48.9', '66.0'} <= set(fig2['numbers'])
    # A hyphen-broken "Fig- ure 4" still names Figure 4 and carries 37.2%.
    assert '37.2' in index['Figure 4']['numbers']
    # A version joined to a name is not a figure's value.
    assert '2.5' not in fig2['numbers']
    assert measured_numbers('GPT-3.5 and Claude-1.3 reach 45.6%') == {
        __import__('decimal').Decimal('45.6')}


def test_an_uncaptioned_reference_is_not_a_figure():
    cards = material_cards('代表3种方法，准确率 81.5%。\n')
    assert figure_index(cards) == {}


def test_inventory_keeps_only_existing_units_and_numbers_they_state():
    cards = _cards()
    figures = figure_index(cards)
    lookup = _unit(cards, 'lookup task the worst')
    reply = json.dumps({'results': [
        {'claim': 'Lookup worst case is 37.2%', 'units': [lookup, 'M999'],
         'figures': ['Figure 4'], 'numbers': ['37.2', '99.9']},
        {'claim': 'no evidence', 'units': ['M999'], 'numbers': ['1.0']},
    ]})
    entries = parse_inventory(reply, cards, figures, capacity=6)
    assert len(entries) == 1
    entry = entries[0]
    assert entry['id'] == 'H1' and entry['numbers'] == ['37.2']
    assert 'M999' not in entry['units']
    assert set(figures['Figure 4']['units']) <= set(entry['units'])


def _coverage(cards):
    figures = figure_index(cards)
    lookup = _unit(cards, 'lookup task the worst')
    inventory = parse_inventory(json.dumps({'results': [
        {'claim': 'Lookup worst case is 37.2%', 'units': [lookup],
         'figures': ['Figure 4'], 'numbers': ['37.2']}]}), cards, figures,
        capacity=6)
    return {'figures': figures, 'inventory': inventory}


def _page(cards, notes, *, units):
    by_id = {c['id']: c for c in cards}
    return {'pageType': 'content', 'key_message': 'k', 'content_notes': notes,
            'sources': [by_id[u] for u in units]}


def test_outline_describing_a_figure_in_words_and_missing_a_result_has_gaps():
    cards = _cards()
    coverage = _coverage(cards)
    results = _unit(cards, 'Figure 2 shows')
    words_only = [_page(cards, 'Figure 2: recall is U-shaped [%s]' % results,
                        units=[results])]
    _assigned, gaps = assign_coverage(coverage, words_only)
    assert any('presents Figure 2 without its values' in g for g in gaps)
    assert any('headline result [H1] is on no page' in g for g in gaps)


def test_outline_carrying_the_values_closes_every_gap_and_assigns_pages():
    cards = _cards()
    coverage = _coverage(cards)
    results = _unit(cards, 'Figure 2 shows')
    lookup = _unit(cards, 'lookup task the worst')
    pages = [_page(cards, 'Figure 2: 71.4% at start vs 48.9% middle',
                   units=[results]),
             _page(cards, 'worst case 37.2% on lookup', units=[lookup])]
    assigned, gaps = assign_coverage(coverage, pages)
    assert gaps == []
    assert [e['label'] for e in assigned[0]] == ['Figure 2']
    assert assigned[1][0]['label'] == 'Lookup worst case is 37.2%'
    assert assigned[1][0]['numbers'] == ['37.2']


def test_a_number_stated_without_citing_material_that_states_it_is_no_cover():
    cards = _cards()
    coverage = _coverage(cards)
    results = _unit(cards, 'Figure 2 shows')
    _assigned, gaps = assign_coverage(
        coverage, [_page(cards, 'worst case 37.2%', units=[results])])
    assert any('H1' in g for g in gaps)


def _outline_json(cards, notes_by_page):
    pages = [{'pageType': 'cover', 'key_message': 'Widget recall'}]
    pages += [{'pageType': 'content', 'key_message': 'k', 'content_notes': n}
              for n in notes_by_page]
    pages.append({'pageType': 'final', 'key_message': 'end'})
    return json.dumps({'title': 'Widget recall', 'pages': pages})


def _outline_ctx(cards):
    return {'topic': 'Widget recall', 'lang': 'en', 'creative_mode': 'standard',
            'max_pages': 8,
            'artifacts': {'research': {'cards': [], 'material_cards': cards}}}


def test_outline_stage_fills_coverage_gaps_before_handing_over(monkeypatch):
    cards = _cards()
    results = _unit(cards, 'Figure 2 shows')
    lookup = _unit(cards, 'lookup task the worst')
    calls = []

    def chat(messages, **kwargs):
        prefix = kwargs.get('log_prefix')
        calls.append(prefix)
        prompt = messages[0]['content']
        if prefix == '[Slides:coverage]':
            assert 'Figure 2: caption' in prompt
            return json.dumps({'results': [
                {'claim': 'Lookup worst case is 37.2%', 'units': [lookup],
                 'figures': ['Figure 4'], 'numbers': ['37.2']}]}), {}
        if prefix == '[Slides:outline:standard]':
            assert '[H1] Lookup worst case is 37.2%' in prompt
            return _outline_json(cards, [
                f'Figure 2: recall is U-shaped [{results}]']), {}
        assert prefix == '[Slides:outline-coverage]'
        assert 'presents Figure 2 without its values' in prompt
        assert 'headline result [H1] is on no page' in prompt
        return _outline_json(cards, [
            f'Figure 2: 71.4% at start vs 48.9% middle [{results}]',
            f'lookup worst case 37.2% [{lookup}]']), {}

    monkeypatch.setattr(recipe, '_llm_chat', chat)
    ctx = _outline_ctx(cards)
    outline = recipe._run_outline(ctx)
    assert calls == ['[Slides:coverage]', '[Slides:outline:standard]',
                     '[Slides:outline-coverage]']
    assert outline['coverage']['inventory'][0]['numbers'] == ['37.2']
    assert len(outline['coverage_filled']) == 2
    ctx['artifacts']['outline'] = outline
    assert recipe._gate_outline(ctx, outline) == []
    briefs = recipe._author_briefs(ctx, outline)
    assert [e['label'] for e in briefs[1]['coverage']] == ['Figure 2']


def test_outline_left_with_a_gap_is_rejected(monkeypatch):
    cards = _cards()
    results = _unit(cards, 'Figure 2 shows')
    lookup = _unit(cards, 'lookup task the worst')

    def chat(messages, **kwargs):
        if kwargs.get('log_prefix') == '[Slides:coverage]':
            return json.dumps({'results': [
                {'claim': 'Lookup worst case is 37.2%', 'units': [lookup],
                 'numbers': ['37.2']}]}), {}
        return _outline_json(cards, [f'recall by position [{results}]']), {}

    monkeypatch.setattr(recipe, '_llm_chat', chat)
    with pytest.raises(ValueError, match='H1'):
        recipe._run_outline(_outline_ctx(cards))


def _page_yaml(text, chart_values=None):
    elements = [{'elementId': 'title', 'elementType': 'text',
                 'bounds': [0, 0, 800, 80], 'content': {'text': text}}]
    if chart_values is not None:
        elements.append({'elementId': 'c', 'elementType': 'chart',
                         'bounds': [0, 100, 800, 400],
                         'data': {'categories': ['start', 'middle'],
                                  'series': [{'name': 'recall',
                                              'values': chart_values}]}})
    return yaml.safe_dump({'elements': elements}, allow_unicode=True)


def test_author_gate_rejects_a_carried_figure_shown_without_its_numbers():
    brief = {'key_message': 'k', 'content_notes': '71.4% vs 48.9%',
             'coverage': [{'kind': 'figure', 'label': 'Figure 2',
                           'numbers': ['48.9', '66.0', '71.4']}]}
    findings = author._brief_fidelity_findings(
        brief, _page_yaml('Recall is U-shaped by position'),
        page_index=1, total=6)
    assert findings and 'Figure 2' in findings[0]
    assert author._brief_fidelity_findings(
        brief, _page_yaml('Recall dips mid-context', [71.4, 48.9]),
        page_index=1, total=6) == []
    assert author._brief_fidelity_findings(
        brief, _page_yaml('Recall falls from 71.4% to 48.9%'),
        page_index=1, total=6) == []


def test_author_prompt_names_the_results_the_page_must_show():
    brief = {'pageType': 'content', 'key_message': 'k',
             'sources': _cards()[:1],
             'coverage': [{'kind': 'result', 'label': 'worst case 37.2%',
                           'numbers': ['37.2']}]}
    from lib.slides.pptd import Deck
    deck = Deck(title='T', size=(1280, 720), theme={}, pages=[], root='/tmp')
    prompt = author._build_prompt(deck, brief, 1, 4, '', '', '', [], 'en')
    assert 'must show with their numbers' in prompt
    assert 'worst case 37.2%' in prompt


def test_a_page_is_not_assigned_results_it_only_shares_a_number_with():
    cards = material_cards('Cost fell 8 points to 41.2%.\n\nWe ran 8 seeds.\n')
    coverage = {'figures': {}, 'inventory': parse_inventory(json.dumps(
        {'results': [{'claim': 'Cost fell 8 points', 'units': ['M1'],
                      'numbers': ['8', '41.2']}]}), cards, {}, capacity=6)}
    seeds_page = _page(cards, '8 seeds [M2]', units=['M2'])
    assigned, gaps = assign_coverage(coverage, [seeds_page])
    assert assigned == [[]] and gaps
    cost_page = _page(cards, 'cost 41.2% [M1]', units=['M1'])
    assigned, gaps = assign_coverage(coverage, [seeds_page, cost_page])
    assert assigned[0] == [] and assigned[1] and gaps == []


def test_each_result_is_held_to_one_page_not_the_closing_summary():
    cards = _cards()
    coverage = _coverage(cards)
    lookup = _unit(cards, 'lookup task the worst')
    pages = [_page(cards, f'worst case 37.2% [{lookup}]', units=[lookup]),
             dict(_page(cards, f'recap: 37.2% [{lookup}]', units=[lookup]),
                  pageType='final')]
    assigned, gaps = assign_coverage(coverage, pages)
    assert gaps == [] and len(assigned[0]) == 1 and assigned[1] == []


def test_inventory_ids_never_reach_the_page_author():
    from lib.slides.pptd import Deck
    brief = {'pageType': 'content', 'key_message': 'k',
             'content_notes': 'worst case 37.2% [H1] [M2]'}
    deck = Deck(title='T', size=(1280, 720), theme={}, pages=[], root='/tmp')
    prompt = author._build_prompt(deck, brief, 1, 4, '', '', '', [], 'en')
    assert '[H1]' not in prompt and 'worst case 37.2%' in prompt


def test_two_series_in_one_colour_are_an_author_finding():
    def page(colours):
        return yaml.safe_dump({'elements': [{
            'elementId': 'c', 'elementType': 'chart', 'chartType': 'line',
            'bounds': [0, 0, 800, 400],
            'data': {'categories': ['a', 'b'], 'series': [
                {'name': f's{i}', 'values': [1.5, 2.5],
                 **({'color': c} if c else {})}
                for i, c in enumerate(colours)]}}]})
    brief = {'key_message': '1.5 2.5'}
    same = author._brief_fidelity_findings(
        brief, page(['$ink', '$primary', '$ink']), page_index=1, total=6)
    assert same and '$ink' in same[0]
    # Uncoloured series repeat the four-colour palette from the fifth on.
    assert author._brief_fidelity_findings(
        brief, page([None] * 5), page_index=1, total=6)
    assert author._brief_fidelity_findings(
        brief, page([None] * 4), page_index=1, total=6) == []


def test_inventory_holds_no_more_results_than_the_deck_has_content_pages():
    from lib.slides.coverage import content_page_capacity, inventory_prompt
    cards = _cards()
    unit = _unit(cards, 'lookup task the worst')
    reply = json.dumps({'results': [
        {'claim': f'result {i}', 'units': [unit], 'numbers': []}
        for i in range(9)]})
    capacity = content_page_capacity(6)
    assert capacity == 4
    entries = parse_inventory(reply, cards, {}, capacity=capacity)
    assert [e['claim'] for e in entries] == [f'result {i}' for i in range(4)]
    assert 'at most 4' in inventory_prompt('', {}, lang='en', capacity=4)
