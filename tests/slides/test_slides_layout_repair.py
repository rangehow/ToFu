"""Layout repair contracts: the pipeline fixes measured overflows itself.

The layout gate's predicate is untouched; these specify the repairs that
make a refused page pass it: exact anchor-aware box fits, collision moves
chosen by measurement, measured copy budgets for the author, and the rule
that tightening copy keeps the title claim's numbers.
"""

from __future__ import annotations

import pytest
import yaml

from lib.slides import author, recipe
from lib.slides.layout_qa import (_page_findings, apply_relayout,
                                  findings_text, relayout_repairs)

pytestmark = pytest.mark.unit


def _text(element_id, bounds, valign='top', text='x'):
    return {'elementId': element_id, 'elementType': 'text', 'bounds': bounds,
            'content': {'align': ['left', valign], 'text': text}}


def _measured(element_id, top, bottom, content_bottom, reserve, outer):
    return {element_id: {'outer': outer, 'lines': {
        'left': 0, 'right': 300, 'top': top, 'bottom': bottom},
        'contentBottom': content_bottom, 'reserve': reserve}}


def _risk(element_id, **measured):
    return {'type': 'pptx_text_overflow_risk', 'elements': [element_id],
            'measured': _measured(element_id, **measured)}


def test_box_fit_is_exact_for_each_vertical_anchor():
    outer = {'left': 10, 'top': 100, 'right': 510, 'bottom': 140}
    page = {'elements': [
        _text('top', [10, 100, 500, 40]),
        _text('mid', [10, 100, 500, 60], 'middle'),
        _text('bot', [10, 100, 500, 60], 'bottom'),
        _text('wide', [0, 0, 10, 10])]}
    findings = [
        _risk('top', top=100, bottom=150, content_bottom=152, reserve=8,
              outer=outer),
        _risk('mid', top=95, bottom=158, content_bottom=160, reserve=8,
              outer=outer),
        _risk('bot', top=120.4, bottom=160, content_bottom=161.8, reserve=7,
              outer=outer),
        {'type': 'text_overflow', 'elements': ['wide'],
         'overflow': {'right': 4.0}, 'measured': _measured(
             'wide', top=0, bottom=10, content_bottom=10, reserve=2,
             outer=outer)}]
    repairs = relayout_repairs(page, findings, page_height=720)
    # Top: keeps its top, ends at content bottom + reserve.
    assert repairs['top'] == {'bounds': [10.0, 100.0, 500.0, 60.0],
                              'valign': 'top'}
    # Middle: grows around its centre (130) to cover 95 .. 168.
    assert repairs['mid'] == {'bounds': [10.0, 92.0, 500.0, 76.0],
                              'valign': 'middle'}
    # Bottom: re-anchored at the text's first line so the text stays put.
    assert repairs['bot'] == {'bounds': [10.0, 120.0, 500.0, 49.0],
                              'valign': 'top'}
    # An unbreakable run wider than its box has no geometric answer.
    assert 'wide' not in repairs
    apply_relayout(page, repairs)
    assert page['elements'][2]['content']['align'] == ['left', 'top']
    assert page['elements'][2]['bounds'] == [10.0, 120.0, 500.0, 49.0]


def test_fitted_box_passes_the_unchanged_gate():
    def record(outer_bottom):
        # Two 25px lines whose 29px font content areas overhang by 2px.
        return {'id': 't', 'visible': True, 'fontSize': 20, 'lineHeight': 25,
                'outer': {'left': 0, 'top': 100, 'right': 400,
                          'bottom': outer_bottom},
                'rects': [{'left': 0, 'top': 98, 'right': 300, 'bottom': 127,
                           'width': 300, 'height': 29},
                          {'left': 0, 'top': 123, 'right': 300, 'bottom': 152,
                           'width': 300, 'height': 29}]}

    findings = _page_findings([record(140)], 0, tolerance=1.5)
    assert [f['type'] for f in findings] == ['text_overflow']
    page = {'elements': [_text('t', [0, 100, 400, 40])]}
    repairs = relayout_repairs(page, findings, page_height=720)
    _x, y, _w, height = repairs['t']['bounds']
    assert _page_findings([record(y + height)], 0, tolerance=1.5) == []
    # One pixel less would still be refused: the fit is exact, not padded.
    assert _page_findings([record(y + height - 1)], 0, tolerance=1.5)


def test_collision_moves_either_text_clear_and_never_off_page():
    outer = {'left': 0, 'top': 0, 'right': 300, 'bottom': 0}
    measured = {**_measured('a', top=100, bottom=150, content_bottom=152,
                            reserve=8, outer=outer),
                **_measured('b', top=145, bottom=175, content_bottom=177,
                            reserve=8, outer=outer)}
    page = {'elements': [_text('a', [0, 100, 300, 52]),
                         _text('b', [0, 145, 300, 32])]}
    collision = [{'type': 'text_collision', 'elements': ['b', 'a'],
                  'measured': measured}]
    assert relayout_repairs(page, collision, page_height=720, move='lower') \
        == {'b': {'bounds': [0.0, 158.0, 300.0, 32.0], 'valign': 'top'}}
    assert relayout_repairs(page, collision, page_height=720, move='upper') \
        == {'a': {'bounds': [0.0, 87.0, 300.0, 52.0], 'valign': 'top'}}
    assert relayout_repairs(page, collision, page_height=180,
                            move='lower') == {}


def test_author_gets_measured_budget_not_a_bare_message():
    outer = {'left': 0, 'top': 100, 'right': 300, 'bottom': 180}
    finding = {**_risk('body', top=100, bottom=210, content_bottom=212,
                       reserve=8, outer=outer),
               'message': 'page 1: multiline text "body" has only -32px'}
    page = {'elements': [_text('body', [0, 100, 300, 80],
                               text='<p>' + '字' * 60 + '</p>')]}
    text = findings_text([finding], page)
    assert 'box is 80px tall' in text and 'needs 120px' in text
    assert 'about 40 characters (now 60)' in text
    assert 'keep its claim and numbers' in text


def test_tightened_copy_must_keep_the_title_claim_numbers():
    seed = yaml.safe_dump({'elements': [
        _text('title', [0, 0, 900, 80], text='解决率最高仅 3.79%'),
        _text('body', [0, 100, 900, 200], text='细节 46.56% 与 0.70%')]})
    cut_body = yaml.safe_dump({'elements': [
        _text('title', [0, 0, 900, 80], text='解决率最高仅 3.79%'),
        _text('body', [0, 100, 900, 200], text='细节 46.56%')]})
    cut_claim = yaml.safe_dump({'elements': [
        _text('title', [0, 0, 900, 80], text='解决率很低'),
        _text('body', [0, 100, 900, 200], text='细节 46.56%')]})
    assert author._claim_number_findings(seed, cut_body) == []
    assert '3.79' in author._claim_number_findings(seed, cut_claim)[0]


def test_relayout_keeps_the_measured_better_collision_variant(
        tmp_path, monkeypatch):
    deck_dir = tmp_path / 'deck'
    (deck_dir / 'pages').mkdir(parents=True)
    page = {'pageType': 'content', 'elements': [
        _text('a', [0, 100, 300, 52]), _text('b', [0, 145, 300, 32])]}
    page_path = deck_dir / 'pages' / '01.page'
    original = yaml.safe_dump(page, allow_unicode=True, sort_keys=False)
    page_path.write_text(original, encoding='utf-8')
    (deck_dir / 'deck.pptd').write_text(yaml.safe_dump({
        'version': 'v2', 'title': 't', 'size': [1280, 720],
        'theme': {}, 'pages': ['pages/01.page']}), encoding='utf-8')
    outer = {'left': 0, 'top': 0, 'right': 300, 'bottom': 0}
    finding = {'type': 'text_collision', 'page': 1, 'elements': ['a', 'b'],
               'measured': {
                   **_measured('a', top=100, bottom=150, content_bottom=152,
                               reserve=8, outer=outer),
                   **_measured('b', top=145, bottom=175, content_bottom=177,
                               reserve=8, outer=outer)}}

    def fake_inspect(_deck, **_kwargs):
        data = yaml.safe_load(page_path.read_text(encoding='utf-8'))
        bounds = {e['elementId']: e['bounds'] for e in data['elements']}
        # Moving "b" down lands on another text; moving "a" up is clean.
        count = 0 if bounds['a'][1] < 100 else 1
        return {'ran': True, 'pages': [
            {'index': 0, 'findings': [finding] * count}]}

    import lib.slides.layout_qa as layout_qa
    monkeypatch.setattr(layout_qa, 'inspect_deck_layout', fake_inspect)
    from lib.slides.pptd import parse_deck
    deck = parse_deck(str(deck_dir / 'deck.pptd'))
    kept = recipe._relayout_pages({'deck_dir': str(deck_dir)}, deck,
                                  {'findings': [finding]})
    assert kept == {0}
    on_disk = yaml.safe_load(page_path.read_text(encoding='utf-8'))
    assert on_disk['elements'][0]['bounds'] == [0.0, 87.0, 300.0, 52.0]
    assert on_disk['elements'][1]['bounds'] == [0, 145, 300, 32]


def _deck_dir(tmp_path, page: dict, theme=None):
    deck_dir = tmp_path / 'deck'
    (deck_dir / 'pages').mkdir(parents=True)
    (deck_dir / 'pages' / '01.page').write_text(
        yaml.safe_dump(page, allow_unicode=True, sort_keys=False),
        encoding='utf-8')
    (deck_dir / 'deck.pptd').write_text(yaml.safe_dump({
        'version': 'v2', 'title': 't', 'size': [1280, 720],
        'theme': theme or {}, 'pages': ['pages/01.page']}), encoding='utf-8')
    return deck_dir


def test_type_below_the_legibility_floor_is_lifted_not_refused(tmp_path):
    from lib.slides.pptd import parse_deck
    theme = {'textStyles': {'caption': {'fontSize': 12},
                            'body': {'fontSize': 18}}}
    deck = parse_deck(str(_deck_dir(tmp_path, {'elements': [
        _text('t', [0, 0, 10, 10])]}, theme) / 'deck.pptd'))
    page = {'elements': [
        {**_text('small', [0, 0, 400, 40]),
         'content': {'fontSize': 11, 'text': 'a'}},
        {**_text('styled', [0, 50, 400, 40]),
         'content': {'style': '$caption', 'text':
                     '<span style="font-size:12px">b</span>'}},
        {**_text('fine', [0, 100, 400, 40]),
         'content': {'style': '$body', 'fontSize': 20, 'text': 'c'}}]}
    text = yaml.safe_dump(page, allow_unicode=True, sort_keys=False)
    lifted = yaml.safe_load(author._raise_to_legibility_floor(deck, text))
    small, styled, fine = lifted['elements']
    assert small['content']['fontSize'] == 14
    assert styled['content']['fontSize'] == 14
    assert 'font-size:14px' in styled['content']['text']
    assert fine['content'] == page['elements'][2]['content']
    assert author._legibility_findings(deck, lifted['elements']) == []
    clean = yaml.safe_dump({'elements': [page['elements'][2]]})
    assert author._raise_to_legibility_floor(deck, clean) == clean


def test_visual_repair_candidate_is_relaid_out_before_judgement(
        tmp_path, monkeypatch):
    from lib.slides.pptd import parse_deck
    page = {'pageType': 'content',
            'elements': [_text('title', [0, 100, 900, 40])]}
    deck = parse_deck(str(_deck_dir(tmp_path, page) / 'deck.pptd'))
    outer = {'left': 0, 'top': 100, 'right': 900, 'bottom': 140}
    short = _risk('title', top=100, bottom=150, content_bottom=152, reserve=8,
                  outer=outer)

    def fake_inspect(trial, **_kwargs):
        height = trial.pages[0].elements[0]['bounds'][3]
        return {'ran': True, 'findings': [] if height >= 60 else [short]}

    import lib.slides.layout_qa as layout_qa
    monkeypatch.setattr(layout_qa, 'inspect_deck_layout', fake_inspect)
    candidate = yaml.safe_dump(page, allow_unicode=True, sort_keys=False)
    fitted = recipe._relaid_out_candidate(deck, 0, candidate,
                                          abort_check=None)
    assert fitted is not None
    text, trial = fitted
    assert yaml.safe_load(text)['elements'][0]['bounds'] == [0, 100, 900, 60]
    assert trial.pages[0].elements[0]['bounds'] == [0, 100, 900, 60]

    monkeypatch.setattr(layout_qa, 'inspect_deck_layout',
                        lambda trial, **_kw: {'ran': True,
                                              'findings': [short]})
    assert recipe._relaid_out_candidate(deck, 0, candidate,
                                        abort_check=None) is None


def _author_deck(tmp_path):
    from lib.slides.pptd import parse_deck
    return parse_deck(str(_deck_dir(tmp_path, {'elements': [
        _text('t', [0, 0, 10, 10])]}) / 'deck.pptd'))


def _page_yaml(body_text):
    return yaml.safe_dump({'pageType': 'content', 'elements': [
        _text('title', [72, 40, 1000, 80], text='判断 3.79%'),
        _text('body', [72, 200, 600, 100], text=body_text)]},
        allow_unicode=True, sort_keys=False)


def _scripted_llm(monkeypatch, replies):
    calls = []

    def llm(messages, **_kwargs):
        calls.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(author, '_llm', llm)
    return calls


def test_a_cut_off_author_reply_is_never_accepted(tmp_path, monkeypatch):
    deck = _author_deck(tmp_path)
    complete = _page_yaml('完整')
    calls = _scripted_llm(monkeypatch, [
        (_page_yaml('<p><'), {'finish_reason': 'length'}),
        (complete, {'finish_reason': 'stop'})])
    result = author.author_page(deck, {'key_message': '判断 3.79%'}, 0, 1,
                                max_rounds=2)
    assert result['mode'] == 'authored' and result['yaml'] == complete.strip()
    assert '截断' in calls[1][-1]['content']


def test_tightening_rounds_iterate_on_measured_layout(tmp_path, monkeypatch):
    deck = _author_deck(tmp_path)
    long_copy, short_copy = _page_yaml('长' * 80), _page_yaml('短')
    calls = _scripted_llm(monkeypatch, [
        (long_copy, {'finish_reason': 'stop'}),
        (short_copy, {'finish_reason': 'stop'})])
    measured = []

    def layout(text):
        measured.append(text)
        return ['body needs 140px; cut to about 40 characters'] \
            if '长' in text else []

    result = author.author_page(
        deck, {'key_message': '判断 3.79%'}, 0, 1, max_rounds=2,
        seed_yaml=_page_yaml('原文'), layout_findings=layout)
    assert result['mode'] == 'authored' and result['yaml'] == short_copy.strip()
    assert 'cut to about 40 characters' in calls[1][-1]['content']
    assert measured == [long_copy.strip(), short_copy.strip()]


def test_last_measured_candidate_is_returned_for_the_caller_to_compare(
        tmp_path, monkeypatch):
    deck = _author_deck(tmp_path)
    still_long = _page_yaml('长' * 60)
    _scripted_llm(monkeypatch, [
        (still_long, {'finish_reason': 'stop'}),
        ('not: [yaml', {'finish_reason': 'stop'})])
    result = author.author_page(
        deck, {'key_message': '判断 3.79%'}, 0, 1, max_rounds=2,
        seed_yaml=_page_yaml('原文'),
        layout_findings=lambda text: ['still 20px short'])
    assert result['mode'] == 'authored' and result['yaml'] == still_long.strip()
    assert result['findings'] == ['still 20px short']
