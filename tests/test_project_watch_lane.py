"""Tests for the Pillar #7 WATCH lane (lib/conversations/project_watch.py).

Owner's non-negotiable coverage:
  (a) NEUTER proving each item's response genuinely reads LIVE pillar state
      (not a stub) — mirrors the empty-state neuter from the status lane.
  (b) the human-facing-only SOURCE guard extended to the watch module.
  (c) "promote to charter goal" actually calls the charter-commit path and does
      NOT write into the agent prompt path.
  (d) append-only response trail + bounded retention + monotonic ordering.

DB-free where possible; the store uses a real in-memory sqlite3 connection
(the module uses `?` placeholders + db.commit()). Pillar reads are lazily
imported, so we monkeypatch the SOURCE modules.
"""

import sqlite3

import pytest

import lib.conversations.project_watch as pw

pytestmark = pytest.mark.unit


# ── Real in-memory DB for the two watch tables ─────────────────────────

def _make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE project_watch_items ('
        ' item_id TEXT PRIMARY KEY, project_path TEXT NOT NULL DEFAULT \'\','
        ' kind TEXT NOT NULL DEFAULT \'concern\', text TEXT NOT NULL DEFAULT \'\','
        ' status TEXT NOT NULL DEFAULT \'open\', promoted INTEGER NOT NULL DEFAULT 0,'
        ' promoted_text TEXT NOT NULL DEFAULT \'\','
        ' promoted_at INTEGER NOT NULL DEFAULT 0,'
        ' response_fingerprint TEXT NOT NULL DEFAULT \'\','
        ' created_by_conv TEXT NOT NULL DEFAULT \'\','
        ' created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)')
    conn.execute(
        'CREATE TABLE project_watch_responses ('
        ' item_id TEXT NOT NULL, seq INTEGER NOT NULL,'
        ' project_path TEXT NOT NULL DEFAULT \'\', response TEXT NOT NULL DEFAULT \'\','
        ' pillar_state TEXT NOT NULL DEFAULT \'{}\', trigger TEXT NOT NULL DEFAULT \'manual\','
        ' ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (item_id, seq))')
    conn.commit()
    return conn


@pytest.fixture
def db(monkeypatch):
    conn = _make_db()
    monkeypatch.setattr(pw, 'get_thread_db', lambda *a, **k: conn)
    yield conn
    conn.close()


_NORTH_STAR = 'Ship the watch lane so the human sees drift over time.'
_EPIC_TITLE = 'Refactor the evaluation harness'


def _wire_pillars(monkeypatch, *, done=5):
    """Monkeypatch the SOURCE modules collect_pillar_state lazily imports."""
    import lib.conversations.project_board as pb
    import lib.conversations.project_charter as pc
    import lib.presence.registry as pr
    import lib.conversations.project_feed as pf
    import lib.conversations.project_summary as ps
    monkeypatch.setattr(pb, 'read_board', lambda p: {
        'tasks': [{'title': _EPIC_TITLE, 'status': 'claimed',
                   'owner_conv_id': 'convA', 'kind': 'epic'}],
        'open': 2, 'claimed': 1, 'done': done, 'blocked': 0})
    monkeypatch.setattr(pc, 'read_charter', lambda p: {
        'exists': True, 'version': 8, 'content': _NORTH_STAR,
        'decisions': [{'text': 'No fan-out verb.'}]})
    monkeypatch.setattr(pc, 'pending_proposals', lambda p: [])
    monkeypatch.setattr(pr, 'snapshot', lambda p: {'peers': [{'convId': 'convA'}]})
    monkeypatch.setattr(pf, 'read_project_feed', lambda p, **k: {'events': []})
    monkeypatch.setattr(ps, 'project_digest_entries', lambda p, **k: [])


class _DispatchSpy:
    def __init__(self, answer='On track; no drift.'):
        self.calls = []
        self.answer = answer

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return (self.answer, {})


def _wire_llm(monkeypatch, spy):
    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_chat', spy)


# ════════════════════════════════════════════════════════════════════
#  Human CRUD
# ════════════════════════════════════════════════════════════════════

def test_add_lists_and_validates(db):
    r = pw.add_watch_item('/proj/x', 'goal', 'Reach 95% rater agreement')
    assert r['ok'] and r['item']['kind'] == 'goal'
    assert pw.add_watch_item('/proj/x', 'nonsense', 'x')['error'] == 'invalid kind'
    assert pw.add_watch_item('/proj/x', 'goal', '   ')['error'] == 'empty text'
    items = pw.list_watch_items('/proj/x')['items']
    assert len(items) == 1 and items[0]['text'] == 'Reach 95% rater agreement'
    assert items[0]['responses'] == []


def test_edit_resolve_reopen_delete(db):
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Artifacts may desync')['item']['item_id']
    assert pw.edit_watch_item(item_id, text='Artifacts desync under load')['ok']
    assert pw.set_watch_status(item_id, 'resolved')['ok']
    assert pw.list_watch_items('/proj/x')['items'][0]['status'] == 'resolved'
    assert pw.set_watch_status(item_id, 'open')['ok']
    assert pw.set_watch_status(item_id, 'bogus')['error'] == 'invalid status'
    assert pw.delete_watch_item(item_id)['ok']
    assert pw.list_watch_items('/proj/x')['items'] == []


# ════════════════════════════════════════════════════════════════════
#  (a) NEUTER — the response reads LIVE pillar state
# ════════════════════════════════════════════════════════════════════

def test_response_prompt_carries_live_pillar_state(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the watch lane')['item']['item_id']
    resp = pw.address_watch_item(item_id, trigger='manual', force=True)
    assert resp and resp['response'] == 'On track; no drift.'
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR in prompt, 'live north-star missing from watch-item prompt'
    assert _EPIC_TITLE in prompt, 'live in-flight epic missing from watch-item prompt'
    # The human's item text also rides into the prompt.
    assert 'Ship the watch lane' in prompt


def test_NC_response_on_empty_state_loses_live_values(db, monkeypatch):
    """NEUTER: stub the shared synthesis-source builder to ignore live state
    (as a stub generator would). The live north-star + epic then vanish from
    the prompt — proving the positive assertion is load-bearing on real pillar
    reads, not coincidence."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    import lib.conversations.project_status as pstat
    monkeypatch.setattr(pstat, '_build_synthesis_source',
                        lambda ps: 'STUBBED — no live state')
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the watch lane')['item']['item_id']
    pw.address_watch_item(item_id, trigger='manual', force=True)
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR not in prompt
    assert _EPIC_TITLE not in prompt


# ════════════════════════════════════════════════════════════════════
#  (d) Append-only trail + retention + monotonic ordering + staleness gate
# ════════════════════════════════════════════════════════════════════

def test_staleness_gate_and_append_only_trail(db, monkeypatch):
    _wire_pillars(monkeypatch, done=5)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Watch drift')['item']['item_id']

    r1 = pw.address_watch_item(item_id, trigger='manual')
    assert r1['seq'] == 1 and len(spy.calls) == 1

    # Nothing changed → cached, NO second LLM call, no new response row.
    r2 = pw.address_watch_item(item_id, trigger='on_open')
    assert r2['seq'] == 1 and len(spy.calls) == 1

    # Pillar state moves (an epic completes) → fresh response appended.
    _wire_pillars(monkeypatch, done=6)
    r3 = pw.address_watch_item(item_id, trigger='epic_completed')
    assert r3['seq'] == 2 and len(spy.calls) == 2

    trail = pw.list_watch_items('/proj/x')['items'][0]['responses']
    assert [t['seq'] for t in trail] == [2, 1], 'trail not newest-first / monotonic'


def test_response_retention_prunes(db, monkeypatch):
    monkeypatch.setattr(pw, '_RESPONSES_KEEP', 3)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Retention')['item']['item_id']
    for done in range(6):
        _wire_pillars(monkeypatch, done=done)
        pw.address_watch_item(item_id, trigger='epic_completed')
    trail = pw.list_watch_items('/proj/x', resp_limit=100)['items'][0]['responses']
    assert len(trail) == 3
    assert [t['seq'] for t in trail] == [6, 5, 4]


def test_editing_text_forces_readdress(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'question', 'Q1?')['item']['item_id']
    pw.address_watch_item(item_id)
    assert len(spy.calls) == 1
    # Edit the text → fingerprint cleared → next address re-synthesizes even
    # though pillar state is unchanged.
    pw.edit_watch_item(item_id, text='Q1 revised?')
    pw.address_watch_item(item_id)
    assert len(spy.calls) == 2


# ════════════════════════════════════════════════════════════════════
#  (c) Promote-to-charter bridge: calls charter commit, NOT the agent path
# ════════════════════════════════════════════════════════════════════

def test_promote_goal_writes_the_north_star_column_not_a_decision(db, monkeypatch):
    """REVERSED IN PLACE 2026-07-30. This test used to assert
    ``calls['add_decision'].startswith('[Goal')`` — i.e. it CERTIFIED that a
    goal is promoted as a committed DECISION.

    Why that old premise was false: the charter's decision list is injected
    tail-first through a 20-entry window (``_INJECTION_DECISION_WINDOW``) and
    FIFO-capped at 100 (``_MAX_DECISIONS``), so a goal parked there silently
    stops reaching the model as decisions accumulate. That is not a
    hypothetical — ``project_charter._NO_GOAL_NOTICE``'s comment records it
    happening once already ("a goal committed as a decision instead is subject
    to both, which is how one previously went invisible"), and the live project
    measured 20 committed decisions with ZERO carrying the ``[Goal`` prefix. The
    north star has its own ``content`` column precisely because it must never be
    evicted, so a goal belongs there.

    The test is reversed rather than deleted: deleting it would let the next
    person reintroduce the very routing this documents as broken."""
    calls = {}

    def _fake_commit(project_path, *, content=None, add_decision=None,
                     decision_kind='', summary='', updated_by_conv='',
                     expected_version=None, resolves_proposal=''):
        calls['project_path'] = project_path
        calls['content'] = content
        calls['add_decision'] = add_decision
        calls['expected_version'] = expected_version
        return {'ok': True, 'version': 9}

    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter', _fake_commit)

    item_id = pw.add_watch_item('/proj/x', 'goal', 'Reach 95% agreement')['item']['item_id']
    res = pw.promote_watch_item(item_id, updated_by_conv='convH',
                                expected_version=8)
    assert res['ok'] and res['version'] == 9
    assert calls['project_path'] == '/proj/x'
    # The north-star column carries the goal text VERBATIM — no '[Goal ...]'
    # prefix, because it is not a decision entry.
    assert calls['content'] == 'Reach 95% agreement'
    # And it must NOT have taken the decision path (which would be evictable).
    assert calls['add_decision'] is None
    # The version is threaded through as the HARD gate for the content path.
    assert calls['expected_version'] == 8


def test_promote_concern_appends_decision_with_kind_and_summary(db, monkeypatch):
    """A concern/question is NOT a goal: it keeps the committed-decision path.

    Also pins the two fields this bridge used to omit — ``decision_kind`` and
    ``summary``. The per-turn injection renders ONLY the summary line
    (``_decision_headline``), so omitting it left every promoted concern showing
    as a first line clipped by the generic fallback."""
    calls = {}

    def _fake_commit(project_path, *, content=None, add_decision=None,
                     decision_kind='', summary='', updated_by_conv='',
                     expected_version=None, resolves_proposal=''):
        calls.update(content=content, add_decision=add_decision,
                     decision_kind=decision_kind, summary=summary)
        return {'ok': True, 'version': 4}

    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter', _fake_commit)

    item_id = pw.add_watch_item('/proj/x', 'concern',
                                'Artifacts may desync under load')['item']['item_id']
    assert pw.promote_watch_item(item_id, updated_by_conv='convH')['ok']
    assert calls['content'] is None, 'a concern must never overwrite the north star'
    assert calls['add_decision'].startswith('[Concern')
    assert 'Artifacts may desync under load' in calls['add_decision']
    assert calls['decision_kind'] == 'invariant'
    assert calls['summary'] == 'Artifacts may desync under load'


def test_promote_records_the_receipt_for_divergence_diagnosis(db, monkeypatch):
    """A successful promotion persists promoted_text + promoted_at.

    Without that receipt, "never promoted" and "promoted then edited" are
    textually identical (both are text != content) and the UI cannot tell them
    apart — see goal_promotion_state."""
    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter',
                        lambda *a, **k: {'ok': True, 'version': 2})
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship it')['item']['item_id']
    pw.promote_watch_item(item_id)
    row = db.execute('SELECT promoted, promoted_text, promoted_at '
                     'FROM project_watch_items WHERE item_id=?',
                     (item_id,)).fetchone()
    assert row['promoted'] == 1
    assert row['promoted_text'] == 'Ship it'
    assert row['promoted_at'] > 0


def test_promote_propagates_charter_version_conflict(db, monkeypatch):
    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter',
                        lambda *a, **k: {'ok': False, 'error': 'version_conflict',
                                         'current_version': 12})
    item_id = pw.add_watch_item('/proj/x', 'concern', 'c')['item']['item_id']
    res = pw.promote_watch_item(item_id, expected_version=5)
    assert res['ok'] is False and res['error'] == 'version_conflict'
    # A failed commit must NOT record a promotion receipt — otherwise the item
    # would read as 'diverged' (implying it once reached agents) when in fact
    # nothing was ever written.
    row = db.execute('SELECT promoted, promoted_at FROM project_watch_items '
                     'WHERE item_id=?', (item_id,)).fetchone()
    assert row['promoted'] == 0 and row['promoted_at'] == 0
    assert pw.list_watch_items('/proj/x')['items'][0]['promotionState'] == pw.PROMOTION_NONE


# ════════════════════════════════════════════════════════════════════
#  The COMPUTED three-state promotion verdict
# ════════════════════════════════════════════════════════════════════

def _goal(text, *, promoted_text=None, promoted_at=0):
    return {'kind': 'goal', 'text': text,
            'promoted_text': promoted_text if promoted_text is not None else '',
            'promoted_at': promoted_at}


def test_promotion_state_matrix(db):
    """The three states + the whitespace-normalization boundaries."""
    live = {'exists': True, 'version': 3, 'content': 'Ship the lane',
            'decisions': []}
    none_charter = {'exists': False, 'version': 0, 'content': '', 'decisions': []}

    # ACTIVE — the item text IS the live north star.
    v = pw.goal_promotion_state(_goal('Ship the lane'), live)
    assert v['state'] == pw.PROMOTION_ACTIVE and v['divergedSide'] == ''

    # ACTIVE survives reflowed whitespace / newlines (same goal, retyped).
    assert pw.goal_promotion_state(
        _goal('Ship   the\n lane  '), live)['state'] == pw.PROMOTION_ACTIVE

    # Case is NOT folded — 'ship' is a different string, deliberately.
    assert pw.goal_promotion_state(
        _goal('ship the lane'), live)['state'] != pw.PROMOTION_ACTIVE

    # NONE — never promoted (no receipt), regardless of mismatch.
    assert pw.goal_promotion_state(
        _goal('Something else'), live)['state'] == pw.PROMOTION_NONE

    # DIVERGED / item — charter still holds the receipt; the item text moved.
    v = pw.goal_promotion_state(
        _goal('Ship the lane v2', promoted_text='Ship the lane', promoted_at=99),
        live)
    assert v['state'] == pw.PROMOTION_DIVERGED and v['divergedSide'] == 'item'

    # DIVERGED / charter — item still holds the receipt; the charter moved.
    v = pw.goal_promotion_state(
        _goal('Ship the lane', promoted_text='Ship the lane', promoted_at=99),
        {'exists': True, 'version': 4, 'content': 'A different north star',
         'decisions': []})
    assert v['state'] == pw.PROMOTION_DIVERGED and v['divergedSide'] == 'charter'

    # DIVERGED / both — neither side matches the receipt.
    v = pw.goal_promotion_state(
        _goal('Mine now', promoted_text='Original', promoted_at=99),
        {'exists': True, 'version': 4, 'content': 'Theirs now', 'decisions': []})
    assert v['state'] == pw.PROMOTION_DIVERGED and v['divergedSide'] == 'both'

    # The charter being DELETED is the case the stored boolean got wrong: a
    # promoted item must read diverged (not active, not none).
    v = pw.goal_promotion_state(
        _goal('Ship the lane', promoted_text='Ship the lane', promoted_at=99),
        none_charter)
    assert v['state'] == pw.PROMOTION_DIVERGED


def test_promotion_state_for_concern_reads_the_decision_list(db):
    charter = {'exists': True, 'version': 2, 'content': 'North star',
               'decisions': [{'text': '[Concern — promoted by owner] Desync'}]}
    item = {'kind': 'concern',
            'text': '[Concern — promoted by owner] Desync',
            'promoted_text': '', 'promoted_at': 0}
    assert pw.goal_promotion_state(item, charter)['state'] == pw.PROMOTION_ACTIVE
    # A concern must NOT be satisfied by matching the north-star column.
    item2 = {'kind': 'concern', 'text': 'North star',
             'promoted_text': '', 'promoted_at': 0}
    assert pw.goal_promotion_state(item2, charter)['state'] == pw.PROMOTION_NONE


def test_at_most_one_goal_is_the_north_star(db, monkeypatch):
    """Single-north-star semantics need NO uniqueness constraint: the verdict is
    text equality against ONE column, so promoting a second goal necessarily
    demotes the first to diverged."""
    charter = {'exists': True, 'version': 1, 'content': '', 'decisions': []}

    def _fake_commit(project_path, *, content=None, **kw):
        charter['content'] = content
        charter['version'] += 1
        return {'ok': True, 'version': charter['version']}

    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter', _fake_commit)
    monkeypatch.setattr(pc, 'read_charter', lambda p: dict(charter, exists=True))

    a = pw.add_watch_item('/proj/x', 'goal', 'Goal A')['item']['item_id']
    b = pw.add_watch_item('/proj/x', 'goal', 'Goal B')['item']['item_id']
    pw.promote_watch_item(a)
    states = {i['item_id']: i['promotionState']
              for i in pw.list_watch_items('/proj/x')['items']}
    assert states[a] == pw.PROMOTION_ACTIVE and states[b] == pw.PROMOTION_NONE

    # Promoting B replaces the column → A is displaced, not silently still-active.
    pw.promote_watch_item(b)
    items = {i['item_id']: i for i in pw.list_watch_items('/proj/x')['items']}
    assert items[b]['promotionState'] == pw.PROMOTION_ACTIVE
    assert items[a]['promotionState'] == pw.PROMOTION_DIVERGED
    assert items[a]['divergedSide'] == 'charter'
    active = [i for i in items.values() if i['promotionState'] == pw.PROMOTION_ACTIVE]
    assert len(active) == 1


def test_delete_and_resolve_never_touch_the_charter(db, monkeypatch):
    """Removing/resolving a tracking CARD must not clear shared intent."""
    calls = []
    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter',
                        lambda *a, **k: (calls.append(k) or {'ok': True, 'version': 1}))
    monkeypatch.setattr(pc, 'delete_charter',
                        lambda *a, **k: calls.append('delete'))
    item_id = pw.add_watch_item('/proj/x', 'goal', 'G')['item']['item_id']
    pw.promote_watch_item(item_id)
    calls.clear()
    pw.set_watch_status(item_id, 'resolved')
    pw.delete_watch_item(item_id)
    assert calls == [], 'resolve/delete must not write to the charter'


def test_item_text_cap_matches_charter_content(db):
    """A goal and the north star are one concept — so one ceiling, not two.

    Unequal caps would mean adopting the charter's side of a diverged goal could
    silently truncate the text being copied back."""
    from lib.conversations.project_charter import _CONTENT_MAX_CHARS
    assert pw._ITEM_TEXT_MAX == _CONTENT_MAX_CHARS


# ════════════════════════════════════════════════════════════════════
#  (b) HUMAN-FACING ONLY — not on the system-context injection path
# ════════════════════════════════════════════════════════════════════

def _system_context_package_sources():
    """Return {relpath: source} for EVERY .py under the system_context package.

    system_context was split from a single module into a package (commit
    86567af); the injection logic now spans several files. This guard scans the
    whole package dir so it keeps holding if the injection logic moves between
    package members. Fails loudly if the dir is absent (never a silent no-op)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = os.path.join(root, 'lib', 'tasks_pkg', 'system_context')
    assert os.path.isdir(pkg), (
        f'system_context package dir not found at {pkg!r} — the source guard '
        f'cannot verify the injection path; update this test to the new layout')
    sources = {}
    for dirpath, _dirs, files in os.walk(pkg):
        if '__pycache__' in dirpath:
            continue
        for fn in files:
            if fn.endswith('.py'):
                p = os.path.join(dirpath, fn)
                with open(p, encoding='utf-8') as f:
                    sources[os.path.relpath(p, root)] = f.read()
    assert sources, f'no .py sources found under {pkg!r}'
    return sources


def test_watch_not_in_system_context_source():
    sources = _system_context_package_sources()
    for banned in ('project_watch', 'address_watch_item', 'list_watch_items',
                   'add_watch_item', 'generate_item_response'):
        for rel, src in sources.items():
            assert banned not in src, (
                f'{rel} references {banned!r} — the watch lane must NOT '
                f'be on the ambient prompt-injection path')


def test_goal_reaches_agents_only_via_commit_charter():
    """The COMPLEMENT of the source guard above.

    That guard proves the watch lane is absent from the injection path. This one
    proves the promotion bridge did not grow a SECOND route in the other
    direction: the only way a watch item's text reaches an agent is
    ``commit_charter`` (whose output the pre-existing
    ``render_charter_injection_block`` already injects).

    Asserted over the AST — the set of names promote_watch_item actually CALLS —
    not over a substring scan of its source. A substring scan is the wrong
    instrument for this claim twice over: it cannot tell a call from a mention,
    and (unlike the whole-line-comment case the shared strip_comments handles) a
    docstring is not a comment, so no amount of comment-stripping would make it
    sound. The first version of this test failed on the word 'injection' inside
    this function's own docstring — a correct tree reported red."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(pw.promote_watch_item)))
    called = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.name)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert 'commit_charter' in called, 'the charter bridge vanished'
    assert 'commit_charter' in imported
    for banned in ('push_event', 'send_peer_message', 'enqueue', 'dispatch_chat',
                   'build_static_prompt', 'render_charter_injection_block'):
        assert banned not in called and banned not in imported, (
            f'promote_watch_item reaches {banned!r} — a goal must reach agents '
            f'ONLY through commit_charter, never a second channel')
    for mod in imported:
        assert 'system_context' not in mod, (
            f'promote_watch_item imports {mod!r} — the promotion bridge must '
            f'never touch the prompt-injection package directly')
