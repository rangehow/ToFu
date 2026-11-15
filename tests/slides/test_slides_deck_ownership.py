"""tests/slides/test_slides_deck_ownership.py — owner isolation of the
conversational deck-revision tools.

edit_slides, patch_slides, restyle_slides and revert_slides address a
finished deck by its produce_slides task id. Both deck anchors record the
owning principal: the live slides registry record and the on-disk job
manifest that keeps a deck revisable after a restart. Only a Chat task
running for that owner may change the deck. Any other caller settles a
typed ``deck_not_owned`` refusal before a snapshot, model call, patch,
restyle, revert or version listing touches the deck; an id that names no
deck stays the ordinary "no deck found" failure.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os

import pytest
import yaml

from lib.slides.versions import snapshot_deck as seed_snapshot

pytestmark = pytest.mark.unit

OWNER_USER_ID = 1
OTHER_USER_ID = 2
DECK_TASK_ID = 'slides_owned1'

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
        <p>{title}</p>
"""

#: One valid call per tool against the deck's first page.
TOOL_CALLS = {
    'edit_slides': {'page': 1, 'instruction': '把标题改成新标题'},
    'patch_slides': {'page': 1, 'ops': [
        {'op': 'set_text', 'target': 'title-1', 'text': '<p>新标题</p>'}]},
    'restyle_slides': {'colors': {'primary': '#0E7A5F'}},
    'revert_slides': {'version': 1},
}

#: Every deck-reading or deck-changing entry the four handlers can reach.
DOMAIN_ENTRIES = (
    ('lib.slides.author', 'edit_page'),
    ('lib.slides.patch', 'apply_page_patch'),
    ('lib.slides.theme_ops', 'apply_deck_restyle'),
    ('lib.slides.versions', 'revert_deck'),
    ('lib.slides.versions', 'snapshot_deck'),
    ('lib.slides.versions', 'list_versions'),
)


def _write_page(deck_dir: str, title: str) -> None:
    with open(os.path.join(deck_dir, 'pages', '01.page'), 'w',
              encoding='utf-8') as f:
        f.write(_PAGE.format(title=title))


def _page_text(deck_dir: str) -> str:
    with open(os.path.join(deck_dir, 'pages', '01.page'),
              encoding='utf-8') as f:
        return f.read()


def _tree(root: str) -> dict:
    """Every file under ``root`` → sha256, snapshots and exports included."""
    digests = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, 'rb') as f:
                digests[os.path.relpath(path, root)] = hashlib.sha256(
                    f.read()).hexdigest()
    return digests


def _accept_edit(deck_dir, page_index, instruction, **kwargs):
    """edit_page stand-in: the page author's model call is external."""
    return {'ok': True, 'page': page_index + 1}


@pytest.fixture
def slides_root(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.slides.engine.slides_root',
                        lambda: str(tmp_path))
    # Browser rendering, PPTX export, font staging and layout measurement
    # are infrastructure, not the authorization boundary under test.
    monkeypatch.setattr('lib.slides.render_png.render_page_png',
                        lambda *a, **k: a[2] if len(a) > 2 else '')
    monkeypatch.setattr('lib.slides.render_png.render_previews',
                        lambda deck, out, **kw: {'ok': True, 'pages': [],
                                                 'failed': []})
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx',
                        lambda deck, out, **kw: out)
    monkeypatch.setattr('lib.slides.patch._page_layout_findings',
                        lambda deck, index: 0)
    monkeypatch.setattr('lib.design_sys.fonts.ensure_font',
                        lambda *a, **k: True)
    return str(tmp_path)


@pytest.fixture
def seed_deck(slides_root, request):
    """Seed DECK_TASK_ID: a finished deck whose title changed after one
    revert point, anchored by its disk manifest (a deck finished before a
    restart) or by its live registry record alone."""
    def seed(*, owner_user_id: int, anchor: str) -> str:
        from lib.design_sys.themes import get_theme
        from lib.slides.theme_ops import build_theme_tokens
        workdir = os.path.join(slides_root, 'jobs', DECK_TASK_ID)
        deck_dir = os.path.join(workdir, 'deck')
        os.makedirs(os.path.join(deck_dir, 'pages'))
        _write_page(deck_dir, '原标题')
        manifest = {'version': 'v2', 'title': '归属测试', 'size': [1280, 720],
                    'theme': build_theme_tokens(get_theme('paper-engineer')),
                    'pages': ['pages/01.page']}
        with open(os.path.join(deck_dir, 'deck.pptd'), 'w',
                  encoding='utf-8') as f:
            yaml.safe_dump(manifest, f, allow_unicode=True)
        assert seed_snapshot(deck_dir, op='patch',
                             summary='page 1: set_text') == 1
        _write_page(deck_dir, '现标题')
        if anchor == 'manifest':
            from lib.production.jobs import write_manifest
            write_manifest(workdir, {'task_id': DECK_TASK_ID,
                                     'user_id': owner_user_id},
                           fields=('task_id', 'user_id'),
                           kind='slides-deck', state='done',
                           log_label='test')
        else:
            from lib.slides.runtime import _new_slides_task, _slides_runtime
            request.addfinalizer(
                lambda: _slides_runtime.discard(DECK_TASK_ID))
            _new_slides_task(DECK_TASK_ID, topic='归属测试', workdir=workdir,
                             lang='zh', style='', max_pages=1,
                             size=(1280, 720), user_id=owner_user_id)
        return deck_dir
    return seed


@pytest.fixture
def domain_calls(monkeypatch):
    """(entry name, args, kwargs) of every deck entry a handler reached.
    edit_page is an accepting stand-in; the other entries run for real."""
    calls = []
    for module_name, attr in DOMAIN_ENTRIES:
        module = importlib.import_module(module_name)
        target = _accept_edit if attr == 'edit_page' else getattr(module, attr)

        def record(*args, _target=target, _name=attr, **kwargs):
            calls.append((_name, args, kwargs))
            return _target(*args, **kwargs)
        monkeypatch.setattr(module, attr, record)
    return calls


def _call(monkeypatch, fn_name: str, caller: dict,
          task_id: str = DECK_TASK_ID):
    """Dispatch one call through the registered tool; returns the decoded
    result and the badges its tool round settled with."""
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers import motion_video
    badges = []
    monkeypatch.setattr(motion_video, '_finalize_tool_round',
                        lambda _task, _rn, _entry, metas:
                        badges.extend(meta['badge'] for meta in metas))
    handler = tool_registry.lookup(fn_name)
    _, content, _ = handler(dict(caller), {}, fn_name, 'call-1',
                            {'task_id': task_id, **TOOL_CALLS[fn_name]},
                            1, {}, {}, None, False)
    return json.loads(content), badges


# ── another principal is refused before any deck access ───

@pytest.mark.parametrize('anchor', ['manifest', 'live'])
@pytest.mark.parametrize('fn_name', sorted(TOOL_CALLS))
def test_another_users_call_is_refused_before_it_touches_the_deck(
        fn_name, anchor, seed_deck, domain_calls, monkeypatch):
    deck_dir = seed_deck(owner_user_id=OWNER_USER_ID, anchor=anchor)
    before = _tree(os.path.dirname(deck_dir))
    result, badges = _call(monkeypatch, fn_name, {'_userId': OTHER_USER_ID})
    assert result['ok'] is False, result
    assert result.get('error') == 'deck_not_owned', result
    # The refusal carries no version summaries, changes or links of the deck.
    assert set(result) == {'ok', 'error', 'detail'}
    assert badges == ['failed']
    assert domain_calls == []
    assert _tree(os.path.dirname(deck_dir)) == before


@pytest.mark.parametrize('fn_name', sorted(TOOL_CALLS))
def test_a_caller_without_an_owner_changes_no_deck(
        fn_name, seed_deck, domain_calls, monkeypatch):
    deck_dir = seed_deck(owner_user_id=OWNER_USER_ID, anchor='manifest')
    before = _tree(os.path.dirname(deck_dir))
    result, badges = _call(monkeypatch, fn_name, {})
    assert result['ok'] is False, result
    assert badges == ['failed']
    assert domain_calls == []
    assert _tree(os.path.dirname(deck_dir)) == before


# ── same-owner controls: the owner's calls still reach the deck ──

@pytest.mark.parametrize('anchor', ['manifest', 'live'])
def test_owner_edit_slides_reaches_the_page_author(
        anchor, seed_deck, domain_calls, monkeypatch):
    deck_dir = seed_deck(owner_user_id=OWNER_USER_ID, anchor=anchor)
    result, badges = _call(monkeypatch, 'edit_slides',
                           {'_userId': OWNER_USER_ID})
    assert result['ok'] is True, result
    assert badges == ['edited']
    edits = [(args, kwargs) for name, args, kwargs in domain_calls
             if name == 'edit_page']
    assert len(edits) == 1
    args, kwargs = edits[0]
    assert args[:3] == (deck_dir, 0, '把标题改成新标题')
    assert kwargs['owner_user_id'] == OWNER_USER_ID
    assert [v['op'] for v in result['versions']] == ['edit', 'patch']


@pytest.mark.parametrize('anchor', ['manifest', 'live'])
def test_owner_patch_slides_is_admitted(
        anchor, seed_deck, domain_calls, monkeypatch):
    """Admission only: what an admitted patch does is the patch engine's
    contract (test_slides_patch.py)."""
    seed_deck(owner_user_id=OWNER_USER_ID, anchor=anchor)
    result, _ = _call(monkeypatch, 'patch_slides', {'_userId': OWNER_USER_ID})
    assert result.get('error') != 'deck_not_owned', result
    assert not str(result.get('detail') or '').startswith('no deck found'), \
        result


@pytest.mark.parametrize('anchor', ['manifest', 'live'])
def test_owner_restyle_slides_restyles_the_deck(
        anchor, seed_deck, domain_calls, monkeypatch):
    deck_dir = seed_deck(owner_user_id=OWNER_USER_ID, anchor=anchor)
    result, badges = _call(monkeypatch, 'restyle_slides',
                           {'_userId': OWNER_USER_ID})
    assert result['ok'] is True, result
    assert badges == ['edited']
    with open(os.path.join(deck_dir, 'deck.pptd'), encoding='utf-8') as f:
        assert yaml.safe_load(f)['theme']['colors']['primary'] == '#0E7A5F'


@pytest.mark.parametrize('anchor', ['manifest', 'live'])
def test_owner_revert_slides_restores_the_snapshot(
        anchor, seed_deck, domain_calls, monkeypatch):
    deck_dir = seed_deck(owner_user_id=OWNER_USER_ID, anchor=anchor)
    assert '现标题' in _page_text(deck_dir)
    result, badges = _call(monkeypatch, 'revert_slides',
                           {'_userId': OWNER_USER_ID})
    assert result['ok'] is True, result
    assert badges == ['edited']
    assert '原标题' in _page_text(deck_dir)


# ── missing-deck control: absence is not an authorization failure ──

@pytest.mark.parametrize('fn_name', sorted(TOOL_CALLS))
@pytest.mark.parametrize('task_id, caller_user_id', [
    ('slides_missing', OWNER_USER_ID),
    # A path is not a deck address, not even toward an existing deck.
    (f'../jobs/{DECK_TASK_ID}', OTHER_USER_ID),
])
def test_an_id_naming_no_deck_stays_not_found(
        fn_name, task_id, caller_user_id, seed_deck, domain_calls,
        monkeypatch):
    deck_dir = seed_deck(owner_user_id=OWNER_USER_ID, anchor='manifest')
    before = _tree(os.path.dirname(deck_dir))
    result, badges = _call(monkeypatch, fn_name,
                           {'_userId': caller_user_id}, task_id=task_id)
    assert result == {'ok': False,
                      'detail': f'no deck found for task {task_id}'}
    assert badges == ['failed']
    assert domain_calls == []
    assert _tree(os.path.dirname(deck_dir)) == before
