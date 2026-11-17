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


# ── Line breaks: widows and words split across lines ──

def _break(element_id, inner, balanced, clean=0, painted=False, widow=True):
    return {'type': 'text_line_break', 'elements': [element_id],
            'widow': widow, 'split': not widow, 'painted': painted,
            'innerWidth': inner, 'balancedWidth': balanced, 'cleanWidth': clean}


def test_line_break_repair_narrows_the_box_and_keeps_its_alignment_edge():
    from lib.slides.layout_qa import line_break_repairs

    def text(element_id, halign):
        return {'elementId': element_id, 'elementType': 'text',
                'bounds': [100, 50, 800, 120],
                'content': {'align': [halign, 'bottom'], 'text': 'x'}}

    page = {'elements': [text(eid, halign) for eid, halign in (
        ('l', 'left'), ('c', 'center'), ('r', 'right'), ('j', 'justify'),
        ('p', 'left'), ('clean', 'left'), ('split', 'left'))]}
    breaks = [_break('l', 800, 401.2), _break('c', 800, 400),
              _break('r', 800, 400), _break('j', 800, 400),
              _break('p', 800, 400, painted=True),
              # A clean width (no widow, no split) wins over the balanced one.
              _break('clean', 800, 400, clean=759),
              # A split word with no clean width has no geometric answer.
              _break('split', 800, 400, widow=False)]
    repairs = line_break_repairs(page, breaks)
    assert repairs['l'] == {'bounds': [100.0, 50.0, 402.0, 120.0],
                            'valign': 'bottom'}
    assert repairs['c']['bounds'] == [300.0, 50.0, 400.0, 120.0]
    assert repairs['r']['bounds'] == [500.0, 50.0, 400.0, 120.0]
    assert repairs['clean']['bounds'] == [100.0, 50.0, 759.0, 120.0]
    # Justified or painted text would visibly change: left to the author.
    assert set(repairs) == {'l', 'c', 'r', 'clean'}


def _one_title_deck(tmp_path, title):
    from lib.slides.pptd import Deck, parse_page_text
    page_yaml = yaml.safe_dump({'elements': [{
        'elementId': 'title', 'elementType': 'text',
        'bounds': [40, 40, 19 * 40, 200],
        'content': {'text': title, 'fontSize': 40, 'lineHeight': 1.2,
                    'align': ['left', 'top']}}]}, allow_unicode=True)
    page = parse_page_text(page_yaml, path='pages/01.page')
    return page_yaml, Deck(title='t', size=(1280, 720), theme={},
                           pages=[page], root=str(tmp_path))


@pytest.mark.parametrize('title,kind', [
    # 40px glyphs in a box 19 glyphs wide: the 20th wraps alone.
    ('模型数据集判定器固定只换框架结果仍然显著领先一'[:20], 'widow'),
    # The word 管理 straddles the wrap after the 19th glyph.
    ('一二三四五六七八九十一二三四五六七的管理团队负责交付', 'split'),
])
def test_measured_line_break_is_repaired_without_new_findings(tmp_path, title, kind):
    pytest.importorskip('playwright.sync_api')
    from lib.slides.layout_qa import inspect_deck_layout
    page_yaml, deck = _one_title_deck(tmp_path, title)
    before = inspect_deck_layout(deck)
    if not before.get('ran'):
        pytest.skip(f'no browser: {before.get("reason")}')
    assert [(b['elements'], b[kind]) for b in before['line_breaks']] == [
        (['title'], True)]
    assert before['findings'] == []

    text, trial = recipe._repair_page_line_breaks(deck, 0, page_yaml,
                                                  abort_check=None)
    after = inspect_deck_layout(trial)
    assert after['line_breaks'] == [] and after['findings'] == []
    narrowed = yaml.safe_load(text)['elements'][0]['bounds']
    assert narrowed[0] == 40 and narrowed[2] < 19 * 40


def test_heading_phrase_breaks_are_written_as_explicit_line_breaks():
    from lib.slides.layout_qa import line_break_repairs

    title = {'elementId': 'title', 'elementType': 'text',
             'bounds': [56, 35, 1090, 117],
             'content': {'style': '$title', 'align': ['left', 'top'],
                         'text': '<p><strong>先定位根因， '
                                 '<span style="color:$accent">17%</span>'
                                 '&amp;优于外采</strong></p>\n'}}
    page = {'elements': [title]}
    # Breaks are counted in visible non-space characters: after "，" (6)
    # and after "&" (10, an entity counts as one character).
    flaw = {'type': 'text_line_break', 'elements': ['title'], 'widow': False,
            'split': True, 'painted': False, 'innerWidth': 1090,
            'balancedWidth': 0, 'cleanWidth': 0, 'phraseBreaks': [6, 10]}
    repairs = line_break_repairs(page, [flaw])
    assert repairs == {'title': {'text': (
        '<p><strong>先定位根因，<br/><span style="color:$accent">17%</span>'
        '&amp;<br/>优于外采</strong></p>\n')}}
    apply_relayout(page, repairs)
    assert title['bounds'] == [56, 35, 1090, 117]
    assert title['content']['text'] == repairs['title']['text']


@pytest.mark.parametrize('title,expected', [
    # Greedy wrapping fills 19 glyphs, then strands 7: the heading breaks
    # at its comma instead, into two balanced lines.
    ('一二三四五六七八九十一二，三四五六七八九十一二三四五',
     '一二三四五六七八九十一二，<br/>三四五六七八九十一二三四五'),
    # The browser dictionary does not know 外采 and would split it; the
    # heading breaks at a phrase boundary, never inside an unknown word.
    ('六组回放先定位根因，降到十七了，优于外采二十四的新方案',
     '六组回放先定位根因，降到十七了，<br/>优于外采二十四的新方案'),
])
def test_cjk_heading_breaks_at_the_most_balanced_phrase_boundary(
        tmp_path, title, expected):
    pytest.importorskip('playwright.sync_api')
    from lib.slides.layout_qa import inspect_deck_layout
    from lib.slides.pptd import Deck, parse_page_text
    page_yaml = yaml.safe_dump({'elements': [{
        'elementId': 'title', 'elementType': 'text',
        'bounds': [40, 40, 19 * 40, 200],
        'content': {'style': '$title', 'text': title, 'fontSize': 40,
                    'lineHeight': 1.2, 'align': ['left', 'top']}}]},
        allow_unicode=True)
    deck = Deck(title='t', size=(1280, 720), theme={},
                pages=[parse_page_text(page_yaml, path='pages/01.page')],
                root=str(tmp_path))
    before = inspect_deck_layout(deck)
    if not before.get('ran'):
        pytest.skip(f'no browser: {before.get("reason")}')
    assert [b['elements'] for b in before['line_breaks']] == [['title']]

    text, trial = recipe._repair_page_line_breaks(deck, 0, page_yaml,
                                                  abort_check=None)
    element = yaml.safe_load(text)['elements'][0]
    assert element['content']['text'] == expected
    assert element['bounds'] == [40, 40, 19 * 40, 200]
    after = inspect_deck_layout(trial)
    assert after['line_breaks'] == [] and after['findings'] == []


def test_body_text_and_single_line_headings_keep_browser_wrapping(tmp_path):
    pytest.importorskip('playwright.sync_api')
    from lib.slides.layout_qa import inspect_deck_layout
    from lib.slides.pptd import Deck, parse_page_text
    page_yaml = yaml.safe_dump({'elements': [
        {'elementId': 'body', 'elementType': 'text',
         'bounds': [40, 40, 19 * 40, 200],
         'content': {'style': '$body', 'fontSize': 40, 'lineHeight': 1.2,
                     'text': '一二三四五六七八九十一二，三四五六七八九十一二三四五'}},
        {'elementId': 'title', 'elementType': 'text',
         'bounds': [40, 300, 19 * 40, 100],
         'content': {'style': '$title', 'fontSize': 40, 'lineHeight': 1.2,
                     'text': '一二三四五，六七八九十'}}]},
        allow_unicode=True)
    deck = Deck(title='t', size=(1280, 720), theme={},
                pages=[parse_page_text(page_yaml, path='pages/01.page')],
                root=str(tmp_path))
    result = inspect_deck_layout(deck)
    if not result.get('ran'):
        pytest.skip(f'no browser: {result.get("reason")}')
    assert not any(flaw.get('phraseBreaks') for flaw in result['line_breaks'])
    assert all(flaw['elements'] == ['body'] for flaw in result['line_breaks'])


# ── Containers: text placed in a card stays in it ──

def _record(element_id, z, top, bottom, left=100, right=300):
    return {'id': element_id, 'visible': True, 'z': z, 'fontSize': 16,
            'lineHeight': 0, 'outer': {'left': left, 'top': top,
                                       'right': right, 'bottom': bottom + 12},
            'rects': [{'left': left, 'top': top, 'right': right,
                       'bottom': top + 20, 'width': right - left,
                       'height': 20},
                      {'left': left, 'top': top + 20, 'right': right,
                       'bottom': bottom, 'width': right - left,
                       'height': bottom - top - 20}]}


def _shape(element_id, z, left, top, right, bottom):
    return {'id': element_id, 'z': z, 'visible': True,
            'outer': {'left': left, 'top': top, 'right': right,
                      'bottom': bottom}}


def test_text_running_past_its_card_is_a_layout_finding():
    card = _shape('card', 0, 80, 80, 320, 140)
    bar = _shape('accent-bar', 1, 96, 90, 100, 200)
    page_bg = _shape('page-bg', -1, 0, 0, 1280, 720)
    spill = _record('note', 2, 100, 150)       # starts in the card, ends 10px below
    inside = _record('ok', 3, 84, 124, left=80, right=96)
    findings = _page_findings([spill, inside], 0, tolerance=1.5,
                              shapes=[page_bg, card, bar])
    assert [(f['type'], f['elements'], f['outside']) for f in findings] == [
        ('text_outside_container', ['note', 'card'], {'bottom': 10.0})]
    page = {'elements': [_text('note', [100, 100, 200, 62], text='x' * 50)]}
    instruction = findings_text(findings, page)
    assert '"card" spans x=80..320, y=80..140' in instruction
    assert 'cut this text to about 40 characters (now 50)' in instruction
    # A shape painted above the text is not its container.
    above = _shape('card', 9, 80, 80, 320, 140)
    assert _page_findings([spill], 0, tolerance=1.5, shapes=[above]) == []


# ── Drawn geometry: strokes through glyphs, overlays on charts ──

def _ink_record(element_id, z, left, top, right, bottom):
    rect = {'left': left, 'top': top, 'right': right, 'bottom': bottom,
            'width': right - left, 'height': bottom - top}
    return {'id': element_id, 'visible': True, 'z': z, 'fontSize': 16,
            'lineHeight': 0, 'rects': [rect], 'ink': [dict(rect)],
            'outer': {'left': left, 'top': top, 'right': right,
                      'bottom': bottom}}


def _graphic(element_id, z, points, *, kind='line', stroked=True,
             filled=False, opaque=False):
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return {'id': element_id, 'kind': kind, 'z': z, 'visible': True,
            'stroked': stroked, 'filled': filled, 'opaque': opaque,
            'strokeWidth': 2 if stroked else 0, 'points': points,
            'outer': {'left': min(xs), 'top': min(ys), 'right': max(xs),
                      'bottom': max(ys)}}


def test_a_stroke_through_glyphs_is_a_finding_and_a_rule_beside_them_is_not():
    text = _ink_record('label', 1, 100, 100, 300, 120)
    through = _graphic('rule', 0, [(x, 110) for x in range(80, 320)])
    beside = _graphic('gap-rule', 0, [(x, 124) for x in range(80, 320)])
    # A faint fill edge below the text does not cut it; one painted above does.
    under = _graphic('wash', 0, [(200, y) for y in range(90, 130)],
                     kind='shape', stroked=False, filled=True)
    over = _graphic('badge', 2, [(250, y) for y in range(90, 130)],
                    kind='shape', stroked=False, filled=True)
    findings = _page_findings([text], 0, tolerance=1.5,
                              graphics=[through, beside, under, over])
    assert [(f['type'], f['elements']) for f in findings] == [
        ('stroke_through_text', ['label', 'rule']),
        ('stroke_through_text', ['label', 'badge'])]
    assert findings[0]['crossing'] == {'left': 101.5, 'top': 110,
                                       'right': 298.5, 'bottom': 110} or \
        findings[0]['crossing']['top'] == findings[0]['crossing']['bottom'] == 110
    assert 'Move or resize "rule"' in findings_text(findings[:1])


def test_any_drawing_over_a_chart_is_a_finding():
    chart = {'id': 'chart', 'z': 1, 'visible': True,
             'outer': {'left': 100, 'top': 100, 'right': 500, 'bottom': 400}}
    dot = _graphic('anno-dot', 2, [(300, 250), (305, 255)], kind='shape',
                   filled=True)
    below = _graphic('card', 0, [(90, 90), (510, 410)], kind='shape')
    outside = _graphic('rule', 3, [(x, 420) for x in range(100, 500)])
    findings = _page_findings([], 0, tolerance=1.5, charts=[chart],
                              graphics=[dot, below, outside])
    assert [(f['type'], f['elements']) for f in findings] == [
        ('graphic_over_chart', ['anno-dot', 'chart'])]
    assert 'series for a reference value' in findings_text(findings)


def _line(element_id, bounds):
    return {'elementId': element_id, 'elementType': 'line', 'bounds': bounds,
            'viewBox': [bounds[2], bounds[3]], 'points': '0,1 100,1'}


def _crossing(text_id, line_id, crossing, lines):
    return {'type': 'stroke_through_text', 'elements': [text_id, line_id],
            'crossing': crossing, 'strokeWidth': 2,
            'measured': {text_id: {'lines': lines}}}


def test_rules_through_text_move_off_the_glyphs_and_the_text_stays():
    lines = {'left': 100, 'top': 100, 'right': 300, 'bottom': 140}
    page = {'elements': [
        _text('label', [100, 100, 200, 40]),
        _line('top-rule', [80, 103, 240, 2]),       # crosses near the top
        _line('low-rule', [80, 131, 240, 2]),       # crosses near the bottom
        _line('divider', [180, 60, 2, 120]),        # passes through vertically
        _line('tick', [120, 80, 2, 30])]}           # ends inside the text
    findings = [
        _crossing('label', 'top-rule', {'left': 100, 'right': 300,
                                        'top': 104, 'bottom': 104}, lines),
        _crossing('label', 'low-rule', {'left': 100, 'right': 300,
                                        'top': 132, 'bottom': 132}, lines),
        _crossing('label', 'divider', {'left': 181, 'right': 181,
                                       'top': 102, 'bottom': 138}, lines),
        _crossing('label', 'tick', {'left': 121, 'right': 121,
                                    'top': 102, 'bottom': 109}, lines)]
    repairs = relayout_repairs(page, findings, page_height=720)
    assert repairs == {
        'top-rule': {'bounds': [80.0, 98, 240.0, 2.0]},     # 104 -> 99
        'low-rule': {'bounds': [80.0, 140, 240.0, 2.0]},    # 132 -> 141
        'divider': {'bounds': [98, 60.0, 2.0, 120.0]},      # x 181 -> 99
        'tick': {'bounds': [120.0, 70, 2.0, 30.0]}}         # ends at y=99
    apply_relayout(page, repairs)
    assert page['elements'][0]['bounds'] == [100, 100, 200, 40]
    assert page['elements'][1]['bounds'] == [80.0, 98, 240.0, 2.0]


def test_a_real_rule_through_a_heading_is_measured_and_moved_clear(tmp_path):
    pytest.importorskip('playwright.sync_api')
    from lib.slides.layout_qa import inspect_deck_layout
    from lib.slides.pptd import Deck, parse_page_text
    page_yaml = yaml.safe_dump({'elements': [
        {'elementId': 'heading', 'elementType': 'text',
         'bounds': [100, 100, 600, 60],
         'content': {'text': '模型与数据集固定只换框架', 'fontSize': 40,
                     'lineHeight': 1.2, 'align': ['left', 'top']}},
        {'elementId': 'rule', 'elementType': 'line',
         'bounds': [100, 120, 600, 2], 'viewBox': [600, 2],
         'points': '0,1 600,1', 'border': {'width': 2, 'color': '#000000'}}]},
        allow_unicode=True)
    deck = Deck(title='t', size=(1280, 720), theme={},
                pages=[parse_page_text(page_yaml, path='pages/01.page')],
                root=str(tmp_path))
    before = inspect_deck_layout(deck)
    if not before.get('ran'):
        pytest.skip(f'no browser: {before.get("reason")}')
    assert [(f['type'], f['elements']) for f in before['findings']] == [
        ('stroke_through_text', ['heading', 'rule'])]
    text, _trial, remaining = recipe._relayout_page_text(
        deck, 0, page_yaml, abort_check=None)
    assert remaining == []
    moved = {e['elementId']: e['bounds']
             for e in yaml.safe_load(text)['elements']}
    assert moved['heading'] == [100, 100, 600, 60]
    assert moved['rule'][1] != 120


def test_a_hand_sketched_curve_is_a_finding_and_connectors_are_not():
    from lib.slides.layout_qa import _sketched_plots
    page = {'elements': [
        {'elementId': 'u-curve', 'elementType': 'line',
         'points': '0,32 120,34 246,216 380,254', 'curve': 'smooth'},
        {'elementId': 'elbow', 'elementType': 'line',
         'points': '0,0 0,40 80,40'},
        {'elementId': 'leader', 'elementType': 'line',
         'points': '0,0 40,30 80,30', 'arrow': [None, 'triangle']},
        {'elementId': 'rule', 'elementType': 'line', 'points': '0,1 600,1'}]}
    findings = _sketched_plots(page, 3)
    assert [(f['type'], f['page'], f['elements']) for f in findings] == [
        ('sketched_plot', 4, ['u-curve'])]
    assert 'chart element built from the evidence numbers' in findings_text(
        findings)


def test_box_text_below_wcag_contrast_is_a_finding_and_takes_the_theme_ink():
    def coloured(element_id, colour, size, z=1, spans=(), top=100):
        record = _ink_record(element_id, z, 100, top, 300, top + 20)
        record['color'] = colour
        record['colors'] = [{'color': colour, 'size': size, 'bold': False},
                            *({'color': c, 'size': size, 'bold': False}
                              for c in spans)]
        return record

    pale = coloured('note', 'rgb(170, 170, 170)', 16)          # 2.2:1
    large = coloured('title', 'rgb(192, 101, 43)', 40,
                     top=200)                                  # 3.8:1, large
    accent = coloured('body', 'rgb(27, 36, 48)', 16, top=300,
                      spans=['rgb(192, 101, 43)'])             # inline accent
    findings = _page_findings([pale, large, accent], 0, tolerance=1.5,
                              page_background='rgb(247, 247, 245)')
    assert [(f['type'], f['elements'], f['required']) for f in findings] == [
        ('text_contrast', ['note'], 4.5)]
    # On an opaque dark card the same pale text passes 4.5:1 (5.5:1).
    card = _graphic('card', 0, [(90, 90), (310, 130)], kind='shape',
                    stroked=False, filled=True, opaque=True)
    card['fill'] = 'rgb(40, 40, 40)'
    card['outer'] = {'left': 90, 'top': 90, 'right': 310, 'bottom': 130}
    assert _page_findings([pale], 0, tolerance=1.5, graphics=[card],
                          page_background='rgb(247, 247, 245)') == []
    page = {'elements': [_text('note', [100, 100, 200, 20])]}
    assert relayout_repairs(page, findings, page_height=720) == {
        'note': {'color': '$ink'}}
    assert relayout_repairs(page, findings, page_height=720,
                            move='upper') == {'note': {'color': '$bg'}}
    apply_relayout(page, {'note': {'color': '$ink'}})
    assert page['elements'][0]['content']['color'] == '$ink'
    assert page['elements'][0]['bounds'] == [100, 100, 200, 20]


def test_an_arrow_must_point_at_an_element():
    target = _ink_record('next-step', 1, 300, 100, 400, 120)
    panel = _graphic('panel', 0, [(0, 0), (600, 400)], kind='shape',
                     stroked=False, filled=True)
    panel['outer'] = {'left': 0, 'top': 0, 'right': 600, 'bottom': 400}

    def arrow(element_id, start, end):
        line = _graphic(element_id, 2, [start, end])
        line['arrows'] = [False, True]
        return line

    short = arrow('short', (100, 110), (260, 110))      # 40px short of the text
    touching = arrow('touching', (100, 112), (290, 112))  # within 8 x 2px
    astray = arrow('astray', (100, 300), (260, 300))    # nothing ahead
    findings = _page_findings([target], 0, tolerance=1.5,
                              graphics=[panel, short, touching, astray])
    arrows = [f for f in findings if f['type'] == 'arrow_to_nothing']
    assert [f['elements'] for f in arrows] == [['short'], ['astray']]
    # The geometry is not guessed: the author ends it or drops its head.
    page = {'elements': [{'elementId': 'short', 'elementType': 'line',
                          'bounds': [100, 109, 160, 2], 'points': '0,1 160,1'}]}
    assert relayout_repairs(page, arrows, page_height=720) == {}
    assert 'drop its arrowhead' in findings_text(arrows[1:])
    # An arrow that stops short names its target and the exact end point:
    # told only "pointing at no element", the author ended it on the text
    # box's bounds (y=338) while the measured text starts lower (live e2e).
    assert arrows[0]['target'] == 'next-step' and arrows[0]['gap'] == 40
    assert 'stops 40px short of "next-step"' in arrows[0]['message']
    assert 'x=300, y=110' in findings_text(arrows[:1])
    assert 'no element' not in arrows[0]['message']


def test_table_cell_text_below_wcag_contrast_on_its_own_fill_is_a_finding():
    from lib.slides.layout_qa import _cell_contrast
    cells = [
        {'table': 'results', 'color': 'rgb(255, 255, 255)',
         'background': 'rgb(176, 196, 186)', 'size': 16, 'bold': True,
         'text': 'Model'},                                   # 1.8:1
        {'table': 'results', 'color': 'rgb(27, 36, 48)',
         'background': 'rgb(247, 247, 245)', 'size': 16, 'bold': False,
         'text': '1.96%'},
        {'table': 'plain', 'color': 'rgb(150, 150, 150)',
         'background': 'rgba(0, 0, 0, 0)', 'size': 16, 'bold': False,
         'text': 'unknown fill'}]
    findings = _cell_contrast(cells, 2)
    assert [(f['type'], f['page'], f['elements']) for f in findings] == [
        ('text_contrast', 3, ['results'])]
    assert '"Model"' in findings[0]['message']


def test_a_table_whose_rows_run_past_its_box_is_a_finding(tmp_path):
    from lib.slides.layout_qa import inspect_deck_layout
    from lib.slides.pptd import Deck, parse_page_text
    rows = [[{'text': 'Model'}, {'text': 'Before'}, {'text': 'After'}]] + [
        [{'text': f'model {i}'}, {'text': '4.87'}, {'text': '4.23'}]
        for i in range(8)]

    def deck(height):
        page_yaml = yaml.safe_dump({'elements': [
            {'elementId': 'grid', 'elementType': 'table',
             'bounds': [100, 100, 600, height], 'rows': rows}]})
        return Deck(title='t', size=(1280, 720), theme={},
                    pages=[parse_page_text(page_yaml, path='pages/01.page')],
                    root=str(tmp_path))

    cramped = inspect_deck_layout(deck(120))
    if not cramped.get('ran'):
        pytest.skip(f'no browser: {cramped.get("reason")}')
    assert [(f['type'], f['elements']) for f in cramped['findings']] == [
        ('table_overflow', ['grid'])]
    assert inspect_deck_layout(deck(560))['findings'] == []


def test_a_caption_painted_over_a_table_is_a_finding(tmp_path):
    from lib.slides.layout_qa import inspect_deck_layout
    from lib.slides.pptd import Deck, parse_page_text
    rows = [[{'text': 'Model'}, {'text': 'Score'}],
            [{'text': 'A'}, {'text': '4.87'}]]

    def deck(caption_top):
        page_yaml = yaml.safe_dump({'elements': [
            {'elementId': 'grid', 'elementType': 'table',
             'bounds': [100, 100, 600, 200], 'rows': rows},
            {'elementId': 'note', 'elementType': 'text',
             'bounds': [100, caption_top, 600, 30],
             'content': {'text': 'Oracle retrieval', 'fontSize': 18}}]})
        return Deck(title='t', size=(1280, 720), theme={},
                    pages=[parse_page_text(page_yaml, path='pages/01.page')],
                    root=str(tmp_path))

    over = inspect_deck_layout(deck(110))
    if not over.get('ran'):
        pytest.skip(f'no browser: {over.get("reason")}')
    assert ('text_table_collision', ['note', 'grid']) in [
        (f['type'], f['elements']) for f in over['findings']]
    assert inspect_deck_layout(deck(400))['findings'] == []
