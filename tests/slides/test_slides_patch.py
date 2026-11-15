"""tests/slides/test_slides_patch.py — deterministic zero-LLM page-patch pins.

Executable specification for lib/slides/patch.py: a chat-addressable op
set (set_text / set_style / set_bounds / swap_image / delete / duplicate)
mutates ONE page's raw YAML, passes the author's own zero-LLM validation
and a measured layout gate, then re-renders + re-exports; ANY failure
rolls the page file back to its pre-patch bytes. The patch_slides tool
handler resolves a finished deck, records its pre-edit undo point, and
reports the engine result.
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

pytestmark = pytest.mark.unit

from lib.slides.patch import apply_page_patch

_PAGE = """\
pageType: content
background:
  type: solid
  color: '#FFFFFF'
elements:
  - elementId: title-1
    elementType: text
    bounds: [72, 60, 800, 80]
    content:
      style: "$title"
      text: |
        <p>旧标题</p>
  - elementId: pic-1
    elementType: image
    bounds: [72, 200, 400, 300]
    src: media/photo.png
components:
  - componentType: metric
    componentId: kpi-1
    bounds: [560, 200, 400, 220]
    value: 42%
    label: 转化率
    support: 环比上周
"""

_THEME = {
    'colors': {'bg': '#F7F7F5', 'ink': '#1B2430', 'primary': '#1F5EFF',
               'hairline': '#D8D8D2'},
    'textStyles': {
        'title': {'fontSize': 40, 'fontFamily': 'MiSans', 'color': '$ink',
                  'bold': True},
        'body': {'fontSize': 18, 'fontFamily': 'MiSans', 'color': '$ink'},
        'bignum': {'fontSize': 64, 'fontFamily': 'MiSans',
                   'color': '$primary', 'bold': True},
        'caption': {'fontSize': 12, 'fontFamily': 'MiSans',
                    'color': '$ink'},
    },
}


def _write_deck(tmp_path, page_yaml: str = _PAGE):
    deck_dir = tmp_path / 'deck'
    (deck_dir / 'pages').mkdir(parents=True)
    (deck_dir / 'media').mkdir(parents=True)
    (deck_dir / 'pages' / '01.page').write_text(page_yaml, encoding='utf-8')
    (deck_dir / 'media' / 'photo.png').write_bytes(b'png')
    manifest = {'version': 'v2', 'title': 'patch 测试', 'size': [1280, 720],
                'theme': _THEME, 'pages': ['pages/01.page']}
    (deck_dir / 'deck.pptd').write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding='utf-8')
    return str(deck_dir)


@pytest.fixture
def offline_pipeline(monkeypatch):
    # Browser + exporter are infrastructure, not the unit under test.
    monkeypatch.setattr('lib.slides.render_png.render_page_png',
                        lambda *a, **k: a[2] if len(a) > 2 else '')
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx',
                        lambda deck, out, **k: out)
    monkeypatch.setattr('lib.slides.patch._page_layout_findings',
                        lambda deck, index: 0)


@pytest.fixture
def deck_dir(tmp_path, offline_pipeline):
    return _write_deck(tmp_path)


def _page_data(deck_dir):
    return yaml.safe_load((open(os.path.join(deck_dir, 'pages', '01.page'),
                              encoding='utf-8')).read())


# ── set_text ────────────────────────────────────────────────

def test_set_text_on_text_element(deck_dir):
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_text', 'target': 'title-1', 'text': '<p>新标题</p>'}])
    assert res['ok'], res
    assert _page_data(deck_dir)['elements'][0]['content']['text'] == '<p>新标题</p>'
    change = res['changes'][0]
    assert change['op'] == 'set_text' and change['target'] == 'title-1'
    assert '旧标题' in change['before'] and '新标题' in change['after']
    assert res['layout_checked'] is True


def test_set_text_on_metric_component_default_and_named_field(deck_dir):
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_text', 'target': 'kpi-1', 'text': '98%'},
        {'op': 'set_text', 'target': 'kpi-1', 'field': 'label',
         'text': '完成率'}])
    assert res['ok'], res
    comp = _page_data(deck_dir)['components'][0]
    assert comp['value'] == '98%' and comp['label'] == '完成率'
    assert res['changes'][0]['kind'] == 'metric.value'
    assert res['changes'][1]['kind'] == 'metric.label'


def test_set_text_rejects_non_text_element_and_empty_text(deck_dir):
    before = open(os.path.join(deck_dir, 'pages', '01.page'),
                  encoding='utf-8').read()
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_text', 'target': 'pic-1', 'text': 'x'},
        {'op': 'set_text', 'target': 'title-1', 'text': '  '},
        {'op': 'set_text', 'target': 'kpi-1', 'field': 'bounds', 'text': 'x'}])
    assert not res['ok']
    assert len(res['errors']) == 3
    assert 'edit_slides' in res['errors'][0]
    assert open(os.path.join(deck_dir, 'pages', '01.page'),
                encoding='utf-8').read() == before


# ── set_style / set_bounds ──────────────────────────────────

def test_set_style_whitelisted_keys_only(deck_dir):
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_style', 'target': 'title-1',
         'props': {'fontSize': 44, 'bold': False, 'color': '$primary'}}])
    assert res['ok'], res
    content = _page_data(deck_dir)['elements'][0]['content']
    assert content['fontSize'] == 44 and content['bold'] is False
    assert content['color'] == '$primary'

    bad = apply_page_patch(deck_dir, 0, [
        {'op': 'set_style', 'target': 'title-1',
         'props': {'position': 'absolute'}}])
    assert not bad['ok'] and 'unknown style keys' in bad['errors'][0]

    bad2 = apply_page_patch(deck_dir, 0, [
        {'op': 'set_style', 'target': 'title-1', 'props': {'fontSize': 0}}])
    assert not bad2['ok'] and 'fontSize' in bad2['errors'][0]


def test_set_bounds_validates_shape(deck_dir):
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_bounds', 'target': 'title-1',
         'bounds': [100, 100, 600, 90]}])
    assert res['ok'], res
    assert _page_data(deck_dir)['elements'][0]['bounds'] == [100, 100, 600, 90]

    bad = apply_page_patch(deck_dir, 0, [
        {'op': 'set_bounds', 'target': 'title-1', 'bounds': [0, 0, -5, 10]}])
    assert not bad['ok']


# ── swap_image / delete / duplicate ────────────────────────

def test_swap_image_requires_existing_media(deck_dir):
    ok = apply_page_patch(deck_dir, 0, [
        {'op': 'swap_image', 'target': 'pic-1', 'src': 'media/photo.png'}])
    assert ok['ok'], ok
    assert ok['changes'][0]['kind'] == 'media'

    missing = apply_page_patch(deck_dir, 0, [
        {'op': 'swap_image', 'target': 'pic-1', 'src': 'media/ghost.png'}])
    assert not missing['ok'] and 'missing' in missing['errors'][0]

    escape = apply_page_patch(deck_dir, 0, [
        {'op': 'swap_image', 'target': 'pic-1', 'src': '../outside.png'}])
    assert not escape['ok']

    wrong_type = apply_page_patch(deck_dir, 0, [
        {'op': 'swap_image', 'target': 'title-1', 'src': 'media/photo.png'}])
    assert not wrong_type['ok']


def test_delete_and_duplicate(deck_dir):
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'duplicate', 'target': 'title-1', 'dx': 0, 'dy': 120},
        {'op': 'delete', 'target': 'pic-1'}])
    assert res['ok'], res
    data = _page_data(deck_dir)
    ids = [el['elementId'] for el in data['elements']]
    assert 'pic-1' not in ids
    clone = next(el for el in data['elements']
                 if el['elementId'].startswith('title-1-copy'))
    assert clone['bounds'][1] == 60 + 120
    assert res['changes'][0]['op'] == 'duplicate'
    assert res['changes'][1]['op'] == 'delete'

    # Duplicate with an explicit id; colliding ids are rejected.
    again = apply_page_patch(deck_dir, 0, [
        {'op': 'duplicate', 'target': 'title-1', 'newId': 'title-2'}])
    assert again['ok'], again
    clash = apply_page_patch(deck_dir, 0, [
        {'op': 'duplicate', 'target': 'title-1', 'newId': 'title-2'}])
    assert not clash['ok'] and 'already exists' in clash['errors'][0]


# ── op envelope validation ─────────────────────────────────

def test_unknown_op_target_and_envelope_errors(deck_dir):
    before = open(os.path.join(deck_dir, 'pages', '01.page'),
                  encoding='utf-8').read()
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'teleport', 'target': 'title-1'},
        {'op': 'set_text', 'target': 'ghost-9', 'text': 'x'},
        {'op': 'set_text', 'text': 'x'}])
    assert not res['ok'] and len(res['errors']) == 3
    assert 'unknown op' in res['errors'][0]
    assert 'ghost-9' in res['errors'][1]
    assert open(os.path.join(deck_dir, 'pages', '01.page'),
                encoding='utf-8').read() == before

    assert not apply_page_patch(deck_dir, 0, [])['ok']
    assert not apply_page_patch(deck_dir, 9, [
        {'op': 'delete', 'target': 'title-1'}])['ok']


def test_validation_failure_leaves_page_untouched(deck_dir):
    """An op sequence whose RESULT breaks the zero-LLM gate (empty text)
    must not leave the mutated page on disk."""
    before = open(os.path.join(deck_dir, 'pages', '01.page'),
                  encoding='utf-8').read()
    # set_text guards emptiness itself, so reach the validator via a page
    # whose only remaining text element gets deleted → empty page.
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'delete', 'target': 'title-1'},
        {'op': 'delete', 'target': 'kpi-1'},
        {'op': 'delete', 'target': 'pic-1'}])
    assert not res['ok']
    assert any('validation' in e or '非空' in e for e in res['errors'])
    assert open(os.path.join(deck_dir, 'pages', '01.page'),
                encoding='utf-8').read() == before


# ── layout / export rollback gates ─────────────────────────

def test_layout_regression_rolls_back(deck_dir, monkeypatch):
    calls = {'n': 0}

    def _rising(deck, index):
        calls['n'] += 1
        return 0 if calls['n'] == 1 else 2   # baseline 0 → measured 2
    monkeypatch.setattr('lib.slides.patch._page_layout_findings', _rising)
    before = open(os.path.join(deck_dir, 'pages', '01.page'),
                  encoding='utf-8').read()
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_text', 'target': 'title-1',
         'text': '<p>一个非常非常非常长的标题</p>'}])
    assert not res['ok']
    assert 'layout regression' in res['errors'][0]
    assert open(os.path.join(deck_dir, 'pages', '01.page'),
                encoding='utf-8').read() == before


def test_export_failure_restores_page(deck_dir, monkeypatch):
    def _boom(deck, out, **kw):
        raise RuntimeError('pptx writer exploded')
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx', _boom)
    before = open(os.path.join(deck_dir, 'pages', '01.page'),
                  encoding='utf-8').read()
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_text', 'target': 'title-1', 'text': '<p>新标题</p>'}])
    assert not res['ok'] and 'export failed' in res['errors'][0]
    assert open(os.path.join(deck_dir, 'pages', '01.page'),
                encoding='utf-8').read() == before


def test_unmeasurable_layout_reports_honestly(deck_dir, monkeypatch):
    monkeypatch.setattr('lib.slides.patch._page_layout_findings',
                        lambda deck, index: None)
    res = apply_page_patch(deck_dir, 0, [
        {'op': 'set_text', 'target': 'title-1', 'text': '<p>新标题</p>'}])
    assert res['ok'] and res['layout_checked'] is False


# ── patch_slides tool handler ──────────────────────────────

_TOOL_TASK_ID = 'slides_patch1'


@pytest.fixture
def job_deck_dir(tmp_path, monkeypatch, offline_pipeline):
    """A finished produce_slides job on disk: the anchor patch_slides
    resolves once the deck's live task is gone (e.g. after a restart)."""
    from lib.production.jobs import write_manifest
    monkeypatch.setattr('lib.slides.engine.slides_root',
                        lambda: str(tmp_path))
    job = tmp_path / 'jobs' / _TOOL_TASK_ID
    path = _write_deck(job)
    write_manifest(str(job), {'task_id': _TOOL_TASK_ID, 'user_id': 1},
                   fields=('task_id', 'user_id'), kind='slides-deck',
                   state='done', log_label='test')
    return path


def _run_patch_slides(monkeypatch, ops):
    """Call the registered handler; returns (tool result, round badges)."""
    from lib.tasks_pkg.handlers import motion_video as handler
    metas = []
    monkeypatch.setattr(handler, '_finalize_tool_round',
                        lambda task, rn, entry, round_metas:
                        metas.extend(round_metas))
    _, content, _ = handler._handle_patch_slides(
        {'_userId': 1}, None, 'patch_slides', 'call-1',
        {'task_id': _TOOL_TASK_ID, 'page': 1, 'ops': ops},
        1, {}, {}, None, False)
    return json.loads(content), [meta['badge'] for meta in metas]


def test_patch_slides_tool_applies_valid_ops_behind_an_undo_point(
        job_deck_dir, monkeypatch):
    result, badges = _run_patch_slides(monkeypatch, [
        {'op': 'set_text', 'target': 'title-1', 'text': '<p>新标题</p>'},
        {'op': 'set_style', 'target': 'title-1', 'props': {'fontSize': 44}},
        {'op': 'set_text', 'target': 'kpi-1', 'text': '98%'}])
    assert result['ok'] is True, result
    assert badges == ['edited']
    assert [c['op'] for c in result['changes']] == [
        'set_text', 'set_style', 'set_text']
    data = _page_data(job_deck_dir)
    assert data['elements'][0]['content']['text'] == '<p>新标题</p>'
    assert data['elements'][0]['content']['fontSize'] == 44
    assert data['components'][0]['value'] == '98%'
    # revert_slides can restore the pre-edit page; the undo point names
    # each distinct op once.
    assert [(v['version'], v['op'], v['summary'])
            for v in result['versions']] == [
        (1, 'patch', 'page 1: set_style,set_text')]
    snapshot = os.path.join(job_deck_dir, '.versions', '1', 'pages',
                            '01.page')
    assert '旧标题' in open(snapshot, encoding='utf-8').read()
    assert result['task_id'] == _TOOL_TASK_ID
    assert result['download'] == f'/api/v4/slides/{_TOOL_TASK_ID}/file'
    assert result['preview_url'] == (
        f'/api/v4/slides/{_TOOL_TASK_ID}/pages/1.png')


def test_patch_slides_tool_undo_summary_skips_unnamed_ops(
        job_deck_dir, monkeypatch):
    """Ops with a missing or empty name, or that are not objects, leave no
    blank entry in the undo summary; the engine still rejects the patch."""
    page_path = os.path.join(job_deck_dir, 'pages', '01.page')
    before = open(page_path, encoding='utf-8').read()
    result, badges = _run_patch_slides(monkeypatch, [
        {'op': 'set_text', 'target': 'title-1', 'text': '<p>新标题</p>'},
        {'target': 'title-1'},
        {'op': '', 'target': 'title-1'},
        'delete'])
    assert result['ok'] is False and badges == ['failed']
    errors = result.get('errors') or []
    assert [e.split(':', 1)[0] for e in errors] == [
        'op 2', 'op 3', 'op 4'], result
    assert "unknown op ''" in errors[0] and "unknown op ''" in errors[1]
    assert errors[2] == 'op 4: not an object'
    assert open(page_path, encoding='utf-8').read() == before
    assert [v['summary'] for v in result['versions']] == ['page 1: set_text']
