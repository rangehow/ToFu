"""Measured visual repair must preserve retained pages until it can improve them."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml

from lib.slides import recipe
from lib.production.stages import StageAborted, StageFailed, run_stages

pytestmark = pytest.mark.unit


@pytest.fixture
def repair_case(tmp_path, monkeypatch):
    import lib.design_sys.visual_qa as vqa

    root = tmp_path / 'deck'
    (root / 'pages').mkdir(parents=True)
    (root / 'preview').mkdir()
    page = {'pageType': 'content', 'elements': [{
        'elementId': 'title', 'elementType': 'text',
        'bounds': [80, 80, 900, 160], 'content': {'text': 'Original'}}]}
    seed = yaml.safe_dump(page)
    page['elements'][0]['content']['text'] = 'Improved'
    candidate = yaml.safe_dump(page)
    source = root / 'pages' / '01.page'
    source.write_text(seed)
    manifest = {'version': 'v2', 'title': 'Visual repair', 'size': [1280, 720],
                'theme': {}, 'pages': ['pages/01.page']}
    (root / 'deck.pptd').write_text(yaml.safe_dump(manifest))
    preview = root / 'preview' / '01.png'
    preview.write_bytes(b'original')
    ctx = {'deck_dir': str(root), 'owner_user_id': 17, 'provider_pin_id': 'owner-route',
           'qa_model': 'test-vlm', 'abort_event': threading.Event(),
           'artifacts': {'outline': {'pages': [{'key_message': 'Original'}]},
                         'design': {'theme_id': 'paper-engineer'}, 'author': {},
                         'render': {'previews': [str(preview)]}}}
    state = {'author_calls': 0, 'reviews': [], 'layout_ok': True,
             'repair_review_ok': True, 'render_ok': True, 'worsen_deck': False,
             'abort_during_recheck': False, 'remaining': False}
    finding = {'check': 'hierarchy', 'element': 'title', 'issue': 'Unreadable title',
               'severity': 'major', 'fix': 'Increase type size'}
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '1')
    monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))
    monkeypatch.setattr(vqa, 'resolve_visual_qa_model', lambda value: value)

    def contact(paths, output, **kwargs):
        Path(output).write_bytes(b'|'.join(Path(path).read_bytes() for path in paths))
        return output

    monkeypatch.setattr('lib.design_sys.contact_sheet.build_contact_sheet', contact)

    def author(deck, brief, index, total, **kwargs):
        assert kwargs['seed_yaml'] == seed
        assert kwargs['owner_user_id'] == 17
        assert kwargs['provider_pin_id'] == 'owner-route'
        state['author_calls'] += 1
        return {'mode': 'authored', 'yaml': candidate}

    monkeypatch.setattr('lib.slides.author.author_page', author)

    def layout(deck, **kwargs):
        assert kwargs['page_indexes'] == [0]
        assert 'Improved' in deck.pages[0].raw['elements'][0]['content']['text']
        assert source.read_text() == seed
        return {'ran': True, 'ok': state['layout_ok'],
                'findings': [] if state['layout_ok'] else [{'message': 'overflow'}]}

    monkeypatch.setattr('lib.slides.layout_qa.inspect_deck_layout', layout)

    def render(deck, index, output, **kwargs):
        assert source.read_text() == seed
        if not state['render_ok']:
            raise RuntimeError('browser died')
        Path(output).write_bytes(b'improved')

    monkeypatch.setattr('lib.slides.render_png.render_page_png', render)

    def review(path, **kwargs):
        assert kwargs['owner_user_id'] == 17
        assert kwargs['provider_pin_id'] == 'owner-route'
        improved = Path(path).read_bytes() == b'improved'
        state['reviews'].append((kwargs['label'], improved))
        if kwargs['label'] == 'deck-coherence':
            if state.get('only_global'):
                return {'ok': True, 'findings': [] if improved else [
                    {**finding, 'element': 'whole deck', 'issue': 'Repeated layout'}]}
            return {'ok': True, 'findings': [finding] if improved and state['worsen_deck'] else []}
        if improved and state['abort_during_recheck']:
            ctx['abort_event'].set()
        return {'ok': not improved or state['repair_review_ok'],
                'findings': [finding] * state.get('page_majors', 1)
                if (not improved and not state.get('only_global'))
                or state['remaining'] else []}

    monkeypatch.setattr(vqa, 'qa_frame', review)
    # No paired verdict: adoption falls back to comparing the two reviews.
    monkeypatch.setattr(vqa, 'qa_compare', lambda *a, **k: {
        'ok': False, 'reason': 'no verdict', 'present': []})
    return ctx, state, source, preview, seed, candidate


def test_verified_repair_rechecks_page_and_deck_and_preserves_owner(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    result = recipe._run_visual_qa(ctx)
    assert result['repairedPages'] == [1]
    assert result['remaining'] == []
    assert source.read_text() == candidate
    assert preview.read_bytes() == b'improved'
    assert ('page-01', True) in state['reviews']
    assert ('deck-coherence', True) in state['reviews']
    assert recipe._gate_visual_qa(ctx, result) == []


@pytest.mark.parametrize('failure', ['layout_ok', 'repair_review_ok', 'render_ok'])
def test_unverified_candidate_cannot_replace_page_or_preview(repair_case, failure):
    ctx, state, source, preview, seed, candidate = repair_case
    state[failure] = False
    result = recipe._run_visual_qa(ctx)
    assert result['repaired'] == 0
    assert result['rejectedPages'] == [1]
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'
    assert recipe._gate_visual_qa(ctx, result)


def test_unchanged_visual_defect_is_rejected_and_blocks_export(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['remaining'] = True
    stages = [stage for stage in recipe.slides_recipe_stages() if stage.name == 'visual_qa']
    with pytest.raises(StageFailed, match='visual_qa'):
        run_stages(stages, ctx, state_path=str(source.parent.parent / 'state.json'))
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'
    assert state['author_calls'] == 1


def test_whole_deck_regression_rolls_back_verified_local_repair(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['worsen_deck'] = True
    result = recipe._run_visual_qa(ctx)
    assert result['repaired'] == 0
    assert result['rejectedPages'] == [1]
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'


def test_page_fixes_outweighing_a_new_coherence_finding_are_kept(repair_case):
    """Deck plus pages: two page majors fixed, one coherence major drawn."""
    ctx, state, source, preview, seed, candidate = repair_case
    state['worsen_deck'] = True
    state['page_majors'] = 2
    result = recipe._run_visual_qa(ctx)
    assert result['repairedPages'] == [1]
    assert source.read_text() == candidate
    # The coherence finding still blocks export.
    assert len(result['remaining']) == 1


def test_visual_repair_continues_while_the_page_improves(repair_case, monkeypatch):
    """Fixed point: 2 majors -> 1 -> 0, then nothing left to repair."""
    import lib.design_sys.visual_qa as vqa
    ctx, state, source, preview, seed, candidate = repair_case
    versions = [candidate.replace('Improved', f'Improved-{n}') for n in ('a', 'b')]
    seeds = []

    def author(deck, brief, index, total, **kwargs):
        seeds.append(kwargs['seed_yaml'])
        return {'mode': 'authored', 'yaml': versions[len(seeds) - 1]}

    def render(deck, index, output, **kwargs):
        Path(output).write_bytes(
            deck.pages[0].raw['elements'][0]['content']['text'].encode())

    finding = {'check': 'typography', 'element': 'title', 'issue': 'Widow',
               'severity': 'major', 'fix': 'Rebreak'}

    def review(path, **kwargs):
        if kwargs['label'] == 'deck-coherence':
            return {'ok': True, 'findings': []}
        pixels = Path(path).read_bytes()
        majors = {b'Improved-a': 1, b'Improved-b': 0}.get(pixels, 2)
        return {'ok': True, 'findings': [finding] * majors}

    monkeypatch.setattr('lib.slides.author.author_page', author)
    monkeypatch.setattr('lib.slides.render_png.render_page_png', render)
    monkeypatch.setattr('lib.slides.layout_qa.inspect_deck_layout',
                        lambda deck, **kwargs: {'ran': True, 'ok': True,
                                                'findings': [], 'line_breaks': []})
    monkeypatch.setattr(vqa, 'qa_frame', review)
    result = recipe._run_visual_qa(ctx)
    assert seeds == [seed, versions[0]]
    assert source.read_text() == versions[1]
    assert result['repairedPages'] == [1] and result['remaining'] == []


def test_rejected_visual_repair_is_not_retried(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['repair_review_ok'] = False
    result = recipe._run_visual_qa(ctx)
    assert state['author_calls'] == 1
    assert result['rejectedPages'] == [1]


def test_abort_during_visual_recheck_preserves_original(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['abort_during_recheck'] = True
    with pytest.raises(StageAborted):
        recipe._run_visual_qa(ctx)
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'


def test_global_coherence_findings_enter_repair_even_without_page_defects(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['only_global'] = True
    result = recipe._run_visual_qa(ctx)
    assert state['author_calls'] == 1
    assert result['repairedPages'] == [1]
    assert result['coherence'] == result['remaining'] == []


@pytest.mark.parametrize('second,blocks', [
    ({'ok': True, 'findings': []}, False),        # not reproduced: cleared
    ('same', True),                               # reproduced: still refuses
    ({'ok': False, 'reason': 'budget'}, True),    # unreadable: never clears
])
def test_a_refusal_must_reproduce_on_the_identical_page(repair_case, monkeypatch,
                                                        second, blocks):
    import lib.design_sys.visual_qa as vqa
    ctx, state, source, preview, seed, candidate = repair_case
    state['repair_review_ok'] = False     # the repair is rejected; page stays
    finding = {'check': 'hierarchy', 'element': 'title', 'issue': 'Unreadable',
               'severity': 'major', 'fix': 'Bigger'}
    calls = []

    def review(path, **kwargs):
        if kwargs['label'] == 'deck-coherence':
            return {'ok': True, 'findings': []}
        pixels = Path(path).read_bytes()
        if pixels == b'improved':
            return {'ok': False, 'reason': 'rejected candidate'}
        calls.append(pixels)
        if len(calls) == 1 or second == 'same':
            return {'ok': True, 'findings': [finding]}
        return second

    monkeypatch.setattr(vqa, 'qa_frame', review)
    result = recipe._run_visual_qa(ctx)
    # The page review ran twice on the retained original pixels.
    assert calls == [b'original', b'original']
    assert bool(recipe._gate_visual_qa(ctx, result)) is blocks
    assert result['unconfirmed'] == ([] if blocks else [1])
    assert source.read_text() == seed


def test_a_clean_deck_is_reviewed_once(repair_case, monkeypatch):
    import lib.design_sys.visual_qa as vqa
    ctx, state, *_ = repair_case
    labels = []

    def review(path, **kwargs):
        labels.append(kwargs['label'])
        return {'ok': True, 'findings': []}

    monkeypatch.setattr(vqa, 'qa_frame', review)
    result = recipe._run_visual_qa(ctx)
    assert sorted(labels) == ['deck-coherence', 'page-01']
    assert result['unconfirmed'] == [] and result['remaining'] == []


def _numbered_page(text):
    return yaml.safe_dump({'elements': [{
        'elementId': 'body', 'elementType': 'text', 'bounds': [80, 80, 900, 160],
        'content': {'text': f'<p>{text}</p>'}}]}, allow_unicode=True)


def test_a_repair_never_drops_a_number_the_reviewer_did_not_dispute():
    seed = _numbered_page('240 分钟跑 30 轮，每轮 8.4 万到 11 万 token')
    cut = _numbered_page('跑 30 轮')
    assert {str(n) for n in recipe._dropped_numbers(seed, cut, 'Too dense')} == {
        '240', '8.4', '11'}
    # A number the finding names may go (the reviewer disputed it).
    assert {str(n) for n in recipe._dropped_numbers(
        seed, cut, 'page says 240 minutes; source says 24.0')} == {'8.4', '11'}
    assert recipe._dropped_numbers(seed, seed + ' ', '') == set()


def test_a_visual_repair_that_drops_a_number_is_rejected(repair_case, monkeypatch):
    ctx, state, source, preview, seed, candidate = repair_case
    numbered = _numbered_page('方案 A 41 题，方案 B 38 题')
    source.write_text(numbered)

    def author(deck, brief, index, total, **kwargs):
        state['author_calls'] += 1
        return {'mode': 'authored', 'yaml': _numbered_page('方案 A 41 题')}

    monkeypatch.setattr('lib.slides.author.author_page', author)
    result = recipe._run_visual_qa(ctx)
    assert state['author_calls'] == 1
    assert result['rejectedPages'] == [1] and result['repaired'] == 0
    assert source.read_text() == numbered


def _paired_case(monkeypatch, ctx, versions, standalone, present):
    """Scripted repair: ``standalone[text]`` is the independent review of the
    page whose title reads ``text``; ``present(issue, before, after)`` is the
    paired before/after verdict on one finding."""
    import lib.design_sys.visual_qa as vqa
    seeds = []

    def author(deck, brief, index, total, **kwargs):
        seeds.append(kwargs['seed_yaml'])
        return {'mode': 'authored', 'yaml': versions[len(seeds) - 1]}

    def render(deck, index, output, **kwargs):
        Path(output).write_bytes(
            deck.pages[0].raw['elements'][0]['content']['text'].encode())

    def major(issue):
        return {'check': 'claim-scope', 'element': 'title', 'issue': issue,
                'severity': 'major', 'fix': 'Reword'}

    def review(path, **kwargs):
        if kwargs['label'] == 'deck-coherence':
            return {'ok': True, 'findings': []}
        text = Path(path).read_bytes().decode()
        return {'ok': True, 'findings': [major(i) for i in standalone[text]]}

    compared = []

    def compare(before, after, findings, **kwargs):
        pair = (Path(before).read_bytes().decode(),
                Path(after).read_bytes().decode())
        compared.append((pair, [f['issue'] for f in findings]))
        return {'ok': True, 'reason': '', 'present': [
            present(f['issue'], *pair) for f in findings]}

    monkeypatch.setattr('lib.slides.author.author_page', author)
    monkeypatch.setattr('lib.slides.render_png.render_page_png', render)
    monkeypatch.setattr('lib.slides.layout_qa.inspect_deck_layout',
                        lambda deck, **kwargs: {'ran': True, 'ok': True,
                                                'findings': [], 'line_breaks': []})
    monkeypatch.setattr(vqa, 'qa_frame', review)
    monkeypatch.setattr(vqa, 'qa_compare', compare)
    return seeds, compared


def test_repair_judged_against_the_findings_both_versions_share(
        repair_case, monkeypatch):
    """The e2e non-convergence: the repair fixes the split title, and its
    independent re-review reports two scope problems the first review never
    sampled although the original has them too. A count of the two reviews
    rejected the fix (1 -> 2 majors) forever. Judged side by side, the
    original has three problems and the candidate two, so the fix is kept
    and the next round targets the two that remain."""
    ctx, state, source, preview, seed, candidate = repair_case
    preview.write_bytes(b'Original')
    versions = [candidate.replace('Improved', f'Improved-{n}') for n in 'ab']
    standalone = {'Original': ['split'], 'Improved-a': ['window', 'path'],
                  'Improved-b': []}
    still = {'Original': {'split', 'window', 'path'},
             'Improved-a': {'window', 'path'}, 'Improved-b': set()}
    seeds, compared = _paired_case(
        monkeypatch, ctx, versions, standalone,
        lambda issue, before, after: (issue in still[before],
                                      issue in still[after]))
    result = recipe._run_visual_qa(ctx)
    assert seeds == [seed, versions[0]]
    assert compared[0] == (('Original', 'Improved-a'), ['split', 'window', 'path'])
    assert compared[1][0] == ('Improved-a', 'Improved-b')
    assert source.read_text() == versions[1]
    assert result['repairedPages'] == [1] and result['remaining'] == []


def test_a_repair_that_trades_a_fix_for_a_new_problem_is_rejected(
        repair_case, monkeypatch):
    ctx, state, source, preview, seed, candidate = repair_case
    preview.write_bytes(b'Original')
    versions = [candidate.replace('Improved', 'Improved-a')]
    standalone = {'Original': ['split'], 'Improved-a': ['overlap']}
    still = {'Original': {'split'}, 'Improved-a': {'overlap'}}
    seeds, _compared = _paired_case(
        monkeypatch, ctx, versions, standalone,
        lambda issue, before, after: (issue in still[before],
                                      issue in still[after]))
    result = recipe._run_visual_qa(ctx)
    assert seeds == [seed]
    assert source.read_text() == seed
    assert result['rejectedPages'] == [1]
    assert [f['issue'] for f in result['remaining']] == ['split']


def test_paired_repairs_stop_once_the_known_problems_stop_falling(
        repair_case, monkeypatch):
    """Every round's candidate is judged better than its own original, yet
    the page never gets better than the first adopted state: the chain of
    adopted states must strictly improve, so the loop ends."""
    ctx, state, source, preview, seed, candidate = repair_case
    preview.write_bytes(b'Original')
    versions = [candidate.replace('Improved', f'Improved-{n}') for n in 'abc']
    standalone = {'Original': ['p0'], 'Improved-a': ['p1'],
                  'Improved-b': ['p2'], 'Improved-c': ['p3']}
    # Each original is judged to have one more problem than its candidate.
    seeds, _compared = _paired_case(
        monkeypatch, ctx, versions, standalone,
        lambda issue, before, after: (True, issue == standalone[after][0]))
    result = recipe._run_visual_qa(ctx)
    assert seeds == [seed, versions[0]]
    assert source.read_text() == versions[0]
