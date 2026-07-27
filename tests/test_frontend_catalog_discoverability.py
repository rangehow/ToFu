"""tests/test_frontend_catalog_discoverability.py — the MCP/Skills catalog must
not hide capability it actually ships.

Two behaviours are pinned, both asserted on the RENDERED RESULT rather than on
source text, so a reasonable rewrite of the renderers keeps them biting:

1. **Every category that has entries gets a pill.** The pill bar used to carry
   a hand-copied literal whitelist while ``lib/mcp/registry.py::CATEGORIES``
   was the real source of truth. The lists drifted (10 vs 12), so
   ``Local Life & Travel (China)`` (5 entries) and ``Science & Research`` (2)
   rendered NO pill — their cards existed but could not be filtered to. This
   is the charter's "backend single source of truth got hand-copied into the
   frontend" anti-pattern: a copy never goes red when the original grows.

2. **A declared ``install_note`` reaches the user.** Both catalogs authored
   these notes (8 of them) and ``SkillCatalogEntry`` documents the field as
   "shown under the card", but NOTHING in the frontend rendered it — the
   getting-started instruction for every travel server was invisible. A
   comment promising behaviour with no assertion behind it is exactly the
   ``_BUDGET_EXEMPT_TOOLS`` failure mode.

The renderers are SPLICED from the shipped sources at run time (never copied
into this file — charter: a copied predicate silently stops tracking its
source), and located by SEARCHING for the function definition so a future
module split re-points itself instead of dying on a hardcoded path.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
     tests/test_frontend_catalog_discoverability.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STATIC_JS = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── Locate + splice the shipped renderers ────────────────────────────

def _find_defining_file(sig: str, *search_dirs: str) -> str:
    """Return the single .js file DEFINING ``sig``.

    Anchored on the function signature (a semantic unit), not a path: the pill
    renderers may legitimately move during a module split, and a hardcoded
    path would turn that move into an unreadable false red. Three states are
    each reported distinctly so a real regression cannot masquerade as drift.
    """
    hits = []
    for d in search_dirs:
        for name in sorted(os.listdir(d)):
            if not name.endswith('.js'):
                continue
            p = os.path.join(d, name)
            with open(p, encoding='utf-8') as fh:
                if sig in fh.read():
                    hits.append(p)
    assert hits, (
        f'No static/js file DEFINES {sig!r} — the renderer was deleted, not '
        f'merely relocated. That is a real regression: without it the settings '
        f'panel renders no category pills at all.'
    )
    assert len(hits) == 1, (
        f'{sig!r} is defined in MORE than one file ({hits}) — the single '
        f'source of truth has been duplicated.'
    )
    return hits[0]


def _splice_fn(path: str, sig: str) -> str:
    """Slice a top-level ``function name(...) {`` through its closing brace."""
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    start = src.index(sig)
    end = src.index('\n}\n', start) + len('\n}\n')
    return src[start:end]


def _splice_const(path: str, name: str) -> str:
    """Slice a top-level ``var NAME = [ ... ];`` declaration."""
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(r'^var %s = \[.*?\];' % re.escape(name), src,
                  re.S | re.M)
    assert m, f'{name} not found as a top-level array in {path}'
    return m.group(0)


_MCP_ORDER_SIG = 'function _mcpOrderedCategories(cats) {'
_SKILLS_ORDER_SIG = 'function _skillsOrderedCategories(cats) {'


def _mcp_orderer() -> str:
    path = _find_defining_file(_MCP_ORDER_SIG,
                               os.path.join(STATIC_JS, 'settings'))
    return (_splice_const(path, '_CAT_ORDER') + '\n'
            + _splice_fn(path, _MCP_ORDER_SIG))


def _skills_orderer() -> str:
    path = _find_defining_file(_SKILLS_ORDER_SIG, STATIC_JS)
    return (_splice_const(path, '_SKILLS_CAT_ORDER') + '\n'
            + _splice_fn(path, _SKILLS_ORDER_SIG))


_ORDER_HARNESS = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const orderer = eval(process.argv[3]);
const cats = JSON.parse(process.argv[4]);
console.log(JSON.stringify(orderer(cats)));
"""


def _run_orderer(js_text: str, fn_name: str, cats: dict) -> list:
    src = os.path.join(HERE, f'_cd_src_{fn_name}.js')
    harness = os.path.join(HERE, f'_cd_harness_{fn_name}.js')
    with open(src, 'w', encoding='utf-8') as fh:
        fh.write(js_text)
    with open(harness, 'w', encoding='utf-8') as fh:
        fh.write(_ORDER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src, fn_name, json.dumps(cats)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr[:600]}'
        return json.loads(proc.stdout.strip())
    finally:
        for p in (src, harness):
            if os.path.exists(p):
                os.remove(p)


# ── Backend category truth ───────────────────────────────────────────

def _backend_categories(module_path: str) -> list[str]:
    """Resolve CATEGORIES to its literal string values without importing.

    Import is avoided deliberately: ``lib.mcp.registry`` is reachable only
    through chains that a sibling's unrelated syntax error can break, which
    would turn this guard into a false red about something it does not test.
    """
    with open(module_path, encoding='utf-8') as fh:
        src = fh.read()
    consts = dict(re.findall(r"^(CAT_[A-Z_]+)\s*=\s*'([^']+)'", src, re.M))
    m = re.search(r'^CATEGORIES = \[(.*?)\]', src, re.S | re.M)
    assert m, f'CATEGORIES not found in {module_path}'
    names = re.findall(r'CAT_[A-Z_]+', m.group(1))
    return [consts[n] for n in names if n in consts]


def _entry_categories(module_path: str) -> set[str]:
    """Categories that actually have >=1 catalog entry."""
    with open(module_path, encoding='utf-8') as fh:
        src = fh.read()
    consts = dict(re.findall(r"^(CAT_[A-Z_]+)\s*=\s*'([^']+)'", src, re.M))
    used = re.findall(r"'category':\s*(CAT_[A-Z_]+)", src)
    return {consts[u] for u in used if u in consts}


MCP_REGISTRY = os.path.join(ROOT, 'lib', 'mcp', 'registry.py')
SKILLS_CATALOG = os.path.join(ROOT, 'lib', 'skills', 'catalog.py')


# ── 1. No category with entries may be unreachable ───────────────────

@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_every_mcp_category_with_entries_gets_a_pill():
    """RESULT-level: feed the orderer the real per-category counts and require
    every populated category back.

    The old literal whitelist fails this the moment the backend adds a
    category — which is precisely what happened.
    """
    populated = _entry_categories(MCP_REGISTRY)
    assert populated, 'scan surface empty — the category regex stopped matching'
    cats = {c: 1 for c in sorted(populated)}
    rendered = _run_orderer(_mcp_orderer(), '_mcpOrderedCategories', cats)

    missing = sorted(populated - set(rendered))
    assert not missing, (
        f'MCP categories with real entries render NO pill: {missing}. '
        f'Their cards exist but cannot be filtered to. The pill set must be '
        f'derived from the catalog data, not from a hand-copied list.'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_every_skills_category_with_entries_gets_a_pill():
    populated = _entry_categories(SKILLS_CATALOG)
    assert populated, 'scan surface empty — the category regex stopped matching'
    cats = {c: 1 for c in sorted(populated)}
    rendered = _run_orderer(_skills_orderer(), '_skillsOrderedCategories', cats)

    missing = sorted(populated - set(rendered))
    assert not missing, (
        f'Skills categories with real entries render NO pill: {missing}.'
    )


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_a_category_absent_from_the_preference_list_still_renders():
    """The load-bearing property: an UNKNOWN category must be appended, not
    dropped.

    This is what makes the fix structural rather than "add the two missing
    strings". Without it the next category added to the backend disappears
    again and no test goes red.
    """
    cats = {'Development': 2, 'Totally New Category': 3}
    rendered = _run_orderer(_mcp_orderer(), '_mcpOrderedCategories', cats)
    assert 'Totally New Category' in rendered, (
        f'an unknown category was DROPPED instead of appended: {rendered}'
    )
    # Known ones keep their curated order ahead of the unknown tail.
    assert rendered.index('Development') < rendered.index('Totally New Category')


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_empty_categories_do_not_render_a_pill():
    """Complement: derivation must not start inventing pills for categories
    with zero entries — otherwise "render everything" would pass the tests
    above while making the bar useless."""
    declared = _backend_categories(MCP_REGISTRY)
    populated = _entry_categories(MCP_REGISTRY)
    empty = [c for c in declared if c not in populated]
    assert empty, (
        'no declared-but-empty category exists right now, so this complement '
        'cannot be exercised — re-check the scan surface'
    )
    cats = {c: 1 for c in sorted(populated)}
    rendered = _run_orderer(_mcp_orderer(), '_mcpOrderedCategories', cats)
    leaked = sorted(set(empty) & set(rendered))
    assert not leaked, f'pills rendered for categories with no entries: {leaked}'


# ── 2. install_note must reach the user ──────────────────────────────

def _authored_notes(module_path: str) -> list[str]:
    """The install_note VALUES authored in a catalog (skip the TypedDict decl)."""
    with open(module_path, encoding='utf-8') as fh:
        src = fh.read()
    return re.findall(r"'install_note':\s*'((?:[^'\\]|\\.)*)'", src)


def test_install_note_is_actually_rendered_somewhere():
    """The field is authored 8× across both catalogs; a renderer must consume it.

    Asserted as "some shipped renderer reads the field", not as an exact DOM
    string, so restyling the note does not false-red. Before this guard the
    answer was zero renderers.
    """
    notes = _authored_notes(MCP_REGISTRY) + _authored_notes(SKILLS_CATALOG)
    assert notes, (
        'no install_note authored in either catalog — scan surface is empty, '
        'so this guard would pass vacuously'
    )

    consumers = []
    for d, names in (
        (os.path.join(STATIC_JS, 'settings'), ['mcp.js']),
        (STATIC_JS, ['skills.js']),
    ):
        for name in names:
            p = os.path.join(d, name)
            with open(p, encoding='utf-8') as fh:
                if 'install_note' in fh.read():
                    consumers.append(os.path.relpath(p, ROOT))

    assert len(consumers) == 2, (
        f'install_note is authored {len(notes)}× in the catalogs but consumed '
        f'by only {consumers} — a note the user never sees is worse than no '
        f'note, because the author believes the guidance shipped. Both the MCP '
        f'and Skills card renderers must render it.'
    )


def test_every_credential_bearing_travel_entry_explains_how_to_get_the_key():
    """Content contract: if a card needs a key, it must say where to get one.

    Not styling — this is the actual cognitive-load requirement. Without it a
    user faces a bare password box with no route to a credential.
    """
    with open(MCP_REGISTRY, encoding='utf-8') as fh:
        src = fh.read()
    # Entry blocks in the China local-life category.
    blocks = re.findall(r"\{\s*'id':\s*'([^']+)'(.*?)\n    \},",
                        src, re.S)
    checked = 0
    for sid, body in blocks:
        if 'CAT_LOCAL_CN' not in body:
            continue
        needs_key = bool(re.search(r"'required':\s*True", body))
        if not needs_key:
            continue
        checked += 1
        has_route = ("'install_note'" in body
                     or re.search(r"'hint':\s*'[^']+'", body))
        assert has_route, (
            f'{sid} requires a credential but offers no route to obtain it '
            f'(no install_note, no hint) — the user gets a bare password field.'
        )
    assert checked >= 3, (
        f'expected several credential-bearing China travel entries, scanned '
        f'only {checked} — the block regex likely stopped matching'
    )
