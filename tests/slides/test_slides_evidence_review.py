"""Evidence follows owner-approved plans through review, caching and repair."""

from __future__ import annotations

import copy
import json

import pytest

from lib.design_sys import visual_qa as vqa
from lib.slides import recipe
from lib.slides.review_context import build_review_context
from tests.slides.test_slides_visual_repair import repair_case as repair_case

pytestmark = pytest.mark.unit


def _outline(point='Accuracy 82% on held-out data; no causal claim.'):
    return {'title': 'Research results', 'scenario': 'academic-research',
            'pages': [{'purpose': 'Assess the evidence',
                       'key_message': 'The method improves prediction.',
                       'content_notes': 'Compare baseline and method [S1].',
                       'sources': [{'id': 'S1', 'title': 'Study, Table 2',
                                    'url': 'https://example.org/study',
                                    'point': point}]}]}


def test_evidence_context_matches_dispatched_digest(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    image.write_bytes(b'page-pixels')
    prompts = []
    monkeypatch.setattr('lib.model_info._capabilities.model_supports_vision',
                        lambda _model: True)

    def dispatch(messages, **kwargs):
        prompts.append(messages[0]['content'][0]['text'])
        return ('{"findings":[{"check":"claim-scope","severity":"major",'
                '"issue":"Prediction is presented as causation",'
                '"fix":"Use the supported predictive claim"}]}', {})

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', dispatch)
    context = build_review_context(_outline(), page_index=0)
    identity = vqa.qa_frame_input_sha256(
        str(image), model='vlm-test', review_context=context)
    result = vqa.qa_frame(str(image), model='vlm-test', review_context=context)

    assert result['ok'] and result['input_sha256'] == identity
    assert result['findings'][0]['check'] == 'claim-scope'
    assert 'held-out data' in prompts[0] and 'https://example.org/study' in prompts[0]
    assert 'not independent evidence' in prompts[0]
    changed = build_review_context(_outline('Accuracy 79%.'), page_index=0)
    assert vqa.qa_frame_input_sha256(
        str(image), model='vlm-test', review_context=changed) != identity


def test_deck_pagination_is_not_a_source_claim():
    """A reviewer refused "N / 12" page chrome against the source's own
    page count; the context names the deck's count and that chrome."""
    outline = _outline()
    outline['pages'] = outline['pages'] * 3
    for page_index in (None, 1):
        context = json.loads(build_review_context(outline, page_index=page_index))
        assert context['deck_page_count'] == 3
        assert 'not claims about the source' in context['deck_pagination']


def test_contact_sheet_review_is_scoped_to_deck_level_properties():
    """Thumbnails cannot show legibility; pages are reviewed at full size."""
    deck = json.loads(build_review_context(_outline()))
    page = json.loads(build_review_context(_outline(), page_index=0))
    assert 'Do not report text size' in deck['contact_sheet_scope']
    assert 'contact_sheet_scope' not in page


def _one_page_deck(tmp_path, title):
    import yaml
    root = tmp_path / 'deck'
    (root / 'pages').mkdir(parents=True)
    (root / 'pages' / '01.page').write_text(yaml.safe_dump({
        'pageType': 'content', 'elements': [{
            'elementId': 'title', 'elementType': 'text',
            'bounds': [80, 80, 900, 160], 'content': {'text': title}}]}))
    (root / 'deck.pptd').write_text(yaml.safe_dump({
        'version': 'v2', 'title': 't', 'size': [1280, 720], 'theme': {},
        'pages': ['pages/01.page']}))
    return str(root)


def test_review_sees_the_source_units_stating_the_pages_numbers(tmp_path):
    """A reviewer shown only the cited table (1.97) refused the abstract's
    true 1.96%; the author gate admits any number in the deck evidence, so
    the review receives the unit stating each other visible number."""
    outline = _outline('| model | % Resolved |\n| Claude 2 | 1.97 |')
    ctx = {'artifacts': {'research': {'material_cards': [
        {'id': 'M1', 'section': 'Abstract', 'point': 'Claude 2 solves a mere 1.96% of issues.'},
        {'id': 'M2', 'section': 'Results', 'point': 'Claude 2 resolves 1.96% again.'},
        {'id': 'M13', 'section': 'Table', 'point': '| Claude 2 | 1.97 |'}]}}}
    page = ('elements:\n- elementId: t\n  elementType: text\n  bounds: [0, 0, 100, 40]\n'
            '  content: {text: "Best model resolves 1.96% (1.97 in Table 5), page 1 of 3"}\n')
    evidence = recipe._page_review_evidence(ctx, outline, 0, page, total=3)
    assert [unit['id'] for unit in evidence['number_sources']] == ['M1']
    context = json.loads(build_review_context(outline, page_index=0, **evidence))
    assert '1.96%' in context['source_units_stating_other_visible_numbers'][0]['point']
    # The reviewer reads the page's numbers exactly, not off downscaled pixels.
    assert context['authored_page_text']['texts'] == [
        'Best model resolves 1.96% (1.97 in Table 5), page 1 of 3']
    assert 'source_units_stating_other_visible_numbers' not in json.loads(
        build_review_context(outline, page_index=0))


def test_page_cache_reuses_only_the_same_effective_evidence(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    image.write_bytes(b'unchanged-pixels')
    calls = []
    approved = _outline('Owner-corrected result: 79%.')
    deck_dir = _one_page_deck(tmp_path, 'Accuracy 79%')
    ctx = {'topic': 'Present the supplied study', 'deck_dir': deck_dir,
           'artifacts': {'outline': _outline(),
                         'outline_gate': {'outline': approved}}}

    def review(path, **kwargs):
        calls.append(kwargs['review_context'])
        identity = vqa.qa_frame_input_sha256(
            path, theme=kwargs['theme'], subject=kwargs['subject'],
            model=kwargs['model'], review_context=kwargs['review_context'])
        return {'ok': True, 'findings': [], 'input_sha256': identity}

    monkeypatch.setattr(vqa, 'qa_frame', review)
    cache = {'version': 'slide-visual-qa-v1', 'entries': {}}
    kwargs = {'theme': None, 'vqa': vqa, 'qa_model': 'vlm-test',
              'cache': cache, 'cache_path': str(tmp_path / 'qa.json')}
    recipe._run_page_visual_reviews(ctx, [str(image)], **kwargs)
    recipe._run_page_visual_reviews(ctx, [str(image)], **kwargs)
    assert len(calls) == 1
    assert 'Owner-corrected result' in calls[0] and 'Accuracy 82%' not in calls[0]

    approved['pages'][0]['sources'][0]['point'] = 'Owner-corrected result: 77%.'
    recipe._run_page_visual_reviews(ctx, [str(image)], **kwargs)
    assert len(calls) == 2 and '77%' in calls[-1]


def test_repaired_page_keeps_the_effective_source_context(repair_case, monkeypatch):
    ctx, state, source, preview, seed, candidate = repair_case
    ctx['artifacts']['outline_gate'] = {'outline': _outline()}
    previous = vqa.qa_frame
    contexts = []

    def review(path, **kwargs):
        contexts.append((kwargs['label'], kwargs['review_context']))
        return previous(path, **kwargs)

    monkeypatch.setattr(vqa, 'qa_frame', review)
    result = recipe._run_visual_qa(ctx)
    assert result['repairedPages'] == [1] and source.read_text() == candidate
    pages = [json.loads(context) for label, context in contexts if label == 'page-01']
    assert len(pages) == 2
    # Same source evidence for the original and the repair; each review reads
    # its own page's authored text.
    texts = [page.pop('authored_page_text')['texts'] for page in pages]
    assert texts == [['Original'], ['Improved']]
    assert pages[0] == pages[1]
    assert 'held-out data' in json.dumps(pages[0])
    decks = [json.loads(context) for label, context in contexts
             if label == 'deck-coherence']
    assert len(decks) == 2 and decks[0] == decks[1]
    assert decks[0]['model_authored_story_plan'][0]['key_message'].startswith('The method')


def test_context_projection_is_bounded_and_does_not_mutate_sources():
    outline = _outline('x' * 100_000)
    original = copy.deepcopy(outline)
    context = build_review_context(outline, page_index=0, request='y' * 100_000)
    assert len(context) < 12000
    assert json.loads(context)['cited_source_excerpts'][0]['point'].endswith('…')
    assert outline == original


def test_context_truncation_is_shared_by_cache_and_dispatch(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    image.write_bytes(b'page-pixels')
    prompts = []
    monkeypatch.setattr('lib.model_info._capabilities.model_supports_vision',
                        lambda _model: True)

    def dispatch(messages, **kwargs):
        prompts.append(messages[0]['content'][0]['text'])
        return '{"findings": []}', {}

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', dispatch)
    context = 'source ' * 100_000
    identity = vqa.qa_frame_input_sha256(
        str(image), model='vlm-test', review_context=context)
    result = vqa.qa_frame(str(image), model='vlm-test', review_context=context)
    assert identity == result['input_sha256']
    assert len(prompts[0]) < 20000 and '已截断' in prompts[0]


def test_review_context_does_not_blame_a_page_for_conflicting_sources():
    import json
    from lib.slides.review_context import build_review_context
    context = json.loads(build_review_context(
        {'title': 't', 'pages': [{'key_message': 'm'}]}, page_index=0))
    assert 'never a major or blocker' in context['source_conflicts']
