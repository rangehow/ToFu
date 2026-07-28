"""tests/test_project_charter_kind_routing.py — every commit declares its kind,
and the kind decides where the text LANDS (owner-directed 2026-07-28).

## The contract under test

  invariant — a binding rule constraining FUTURE decisions. Lands in the
              charter. REQUIRES a one-line `summary` (the rule itself); the
              per-turn injection renders ONLY summaries — the full text is
              the project_charter_read detail path.
  lesson    — a methodology experience note. Routed to PROJECT MEMORY with
              BM25 dedup (same-topic lessons fold into ONE living memory,
              owner directive: "append-only variant accumulation is the
              charter-side disease, don't bring it to the memory side").
              MUST NOT grow the charter.
  report    — a completion/rejection record. REJECTED with a pointer to
              JOURNAL.md. MUST NOT grow the charter.

## Why these assertions are shaped this way

Per the charter's own guard discipline: every test asserts an OBSERVABLE
RESULT (charter decision count, memory file contents, block contents) and
never an implementation constant. The dedup test calibrates against the REAL
BM25 scorer with real same-family/cross-topic texts — measured separation is
~36 vs 0 around the 3.0 threshold, so the threshold itself is not what is
being asserted.

NEUTER x2 both bite: short-circuit the memory route -> the "lesson reaches
memory" test goes red; make the injection headline fall through to full text
-> the "summary only" test goes red.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests._nc_harness import patch_restore as _patch_restore

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHARTER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_charter.py')

_PROJ = '/tmp/tofu-kind-routing'

_RULE = ('凭证脱敏是 fail-closed 白名单,禁止改回按字段名排除')
_EVIDENCE_TAIL = '三个携密载体被连续漏掉(env → headers → url),每次都靠作者当时记得'
_FULL_DECISION = (
    f'{_RULE}。完整取证:{_EVIDENCE_TAIL}。默认从「暴露」翻成「丢弃」后第四个洞不会存在。')

_LESSON_A = ('守卫必须断言结果而非实现:行为守卫 MUST 断言「任何东西都不落进 logs/」'
             '这类结果,而不是断言某个私有常量等于某个路径;棘轮守卫锚在语义单元'
             '(AST 节点、函数跨度)而非行号或私有符号名。')
_LESSON_B = ('守卫失效新形态——扫描面残缺:新写任何扫描/遍历类守卫(AST 扫描、'
             '正则扫源码、遍历文件树),第一步是把实际扫到的样本量打印出来人工比对;'
             'NEUTER 必须打在扫描面的边缘形态(私有名/带前缀名/多行跨度)。')
_LESSON_C = ('MCP 传输判定必须显式 is_stdio():实测 `!= "sse"` 这类二值假设有四处,'
             '其中 registry.build_server_config 是真雷(会把 streamable-http 条目当'
             '本地命令拉起);新增传输类型时只需扩 REMOTE_TRANSPORTS。')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app):
    shutil.rmtree(_PROJ, ignore_errors=True)
    os.makedirs(_PROJ, exist_ok=True)
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_charter')
        db.execute('DELETE FROM project_events')
        db.commit()
    yield
    shutil.rmtree(_PROJ, ignore_errors=True)


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _commit(flask_app, args):
    from lib.conversations.project_charter import execute_charter_tool
    with flask_app.app_context():
        return execute_charter_tool('project_charter_commit', args,
                                    current_conv_id='conv-test',
                                    project_path=_PROJ)


def _decisions(flask_app):
    from lib.conversations.project_charter import read_charter
    with flask_app.app_context():
        return read_charter(_PROJ)['decisions']


def _injection(flask_app):
    from lib.conversations.project_charter import render_charter_injection_block
    with flask_app.app_context():
        return render_charter_injection_block(_PROJ)


def _tool_read(flask_app):
    from lib.conversations.project_charter import execute_charter_tool
    with flask_app.app_context():
        return execute_charter_tool('project_charter_read', {},
                                    current_conv_id='conv-test',
                                    project_path=_PROJ)


def _project_memories():
    from lib.memory.storage import list_memories
    return [m for m in list_memories(_PROJ, scope='project')
            if not m.get('is_package')]


# ── invariant path ───────────────────────────────────────────────────────

def test_invariant_commit_lands_in_charter_with_kind_and_summary(flask_app):
    resp = _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                               'summary': _RULE})
    assert 'committed' in resp.lower(), resp
    decs = _decisions(flask_app)
    assert len(decs) == 1
    assert decs[0].get('kind') == 'invariant'
    assert decs[0].get('summary') == _RULE
    assert decs[0]['text'] == _FULL_DECISION


def test_injection_renders_the_summary_not_the_evidence(flask_app):
    """The two-tier split (owner): the per-turn injection shows the RULE;
    the evidence chain stays in the tool detail path."""
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})
    inj = _injection(flask_app)
    assert _RULE in inj, 'the binding rule must be resident per-turn'
    assert _EVIDENCE_TAIL not in inj, (
        'the evidence chain leaked into the per-turn injection — the split '
        'is gone and the block will bloat back to 2k/entry')
    full = _tool_read(flask_app)
    assert _EVIDENCE_TAIL in full, (
        'the tool detail path must keep the full evidence')


def test_a_legacy_decision_without_summary_still_renders_a_headline(flask_app):
    """Entries from before the summary field must not wall-of-text the
    injection either: they abridge to a first-line headline."""
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        commit_charter(_PROJ, add_decision=('旧条目第一行是规则本体\n'
                                            '第二行开始是两千字考古 ' + 'x' * 600),
                       updated_by_conv='agent')
    inj = _injection(flask_app)
    assert '旧条目第一行是规则本体' in inj
    assert 'x' * 600 not in inj


def test_invariant_without_summary_is_rejected(flask_app):
    resp = _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION})
    assert 'summary' in resp, resp
    assert _decisions(flask_app) == []


def test_missing_kind_is_rejected_with_guidance(flask_app):
    resp = _commit(flask_app, {'decision': _FULL_DECISION, 'summary': _RULE})
    assert 'invariant' in resp and 'lesson' in resp and 'report' in resp, resp
    assert _decisions(flask_app) == []


# ── report path ──────────────────────────────────────────────────────────

def test_report_is_rejected_toward_journal(flask_app):
    resp = _commit(flask_app, {'kind': 'report',
                               'decision': 'TTFT 看门狗已落地 commit 69cd968c'})
    assert 'JOURNAL' in resp, resp
    assert _decisions(flask_app) == []


# ── lesson path ──────────────────────────────────────────────────────────

def test_lesson_goes_to_memory_not_charter(flask_app):
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    assert 'memory' in resp.lower(), resp
    assert _decisions(flask_app) == [], 'a lesson MUST NOT grow the charter'
    mems = _project_memories()
    assert len(mems) == 1, f'expected exactly one project memory, got {len(mems)}'
    assert _LESSON_A in (mems[0].get('body') or '')


def test_lesson_is_searchable_after_routing(flask_app):
    """Owner's write-then-verify order, as a standing guard: a routed lesson
    must be findable by the SAME BM25 search the prefetch uses."""
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    from lib.memory.relevance._search import search_memories_scored
    hits = search_memories_scored('守卫 断言 结果 实现', _PROJ, top_k=3)
    proj = [m for sc, m in hits if m.get('scope') == 'project']
    assert proj, 'the routed lesson is not searchable — it would be invisible'


def test_same_family_lessons_fold_via_explicit_target(flask_app):
    """THE dedup directive, primary channel: the model READ the family memory
    (prefetch/search) while working, so when committing a new variant it
    passes into_memory — even when the texts share almost no vocabulary
    (measured: real same-family pairs score ~0.10 containment; a lexical
    threshold can NEVER catch a semantic family)."""
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    family_id = _project_memories()[0]['id']
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_B,
                               'into_memory': family_id})
    assert 'folded' in resp.lower(), resp
    mems = _project_memories()
    assert len(mems) == 1, (f'same-family lessons must fold into ONE memory, '
                            f'got {len(mems)} files')
    body = mems[0].get('body') or ''
    assert _LESSON_A in body and _LESSON_B in body


def test_explicit_into_memory_also_accepts_the_memory_name(flask_app):
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    name = _project_memories()[0].get('name') or ''
    assert name
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_B,
                               'into_memory': name})
    assert 'folded' in resp.lower(), resp
    assert len(_project_memories()) == 1


def test_unknown_into_memory_is_rejected_without_writing_anything(flask_app):
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_B,
                               'into_memory': 'no-such-memory'})
    assert 'no-such-memory' in resp, resp
    mems = _project_memories()
    assert len(mems) == 1
    assert _LESSON_B not in (mems[0].get('body') or '')


def test_near_duplicate_autofolds_without_an_explicit_target(flask_app):
    """Channel 2: heavy vocabulary overlap (>= 0.5 containment) folds
    automatically — verbatim-ish repeats never create a second file."""
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    almost_same = _LESSON_A + ' 补充:违者守卫会在实现重写后静默失效。'
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': almost_same})
    assert 'folded' in resp.lower(), resp
    mems = _project_memories()
    assert len(mems) == 1
    assert almost_same in (mems[0].get('body') or '')


def test_create_response_advises_the_closest_candidates(flask_app):
    """Channel 3: when a new memory IS created, the response names the
    closest existing memories so the model can immediately fold explicitly —
    a missed fold is self-correcting, not silent."""
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_B})
    assert 'into_memory' in resp, resp


def test_cross_topic_lesson_gets_its_own_memory(flask_app):
    """COMPLEMENT: dedup must not become "everything merges into one file" —
    a genuinely different topic creates a new memory."""
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_C})
    mems = _project_memories()
    assert len(mems) == 2, (f'cross-topic lessons must stay separate, got '
                            f'{len(mems)} files')


def test_repeating_the_same_lesson_is_a_noop(flask_app):
    _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
    assert 'already' in resp.lower(), resp
    mems = _project_memories()
    assert len(mems) == 1
    assert (mems[0].get('body') or '').count(_LESSON_A[:60]) == 1


# ── Negative controls ────────────────────────────────────────────────────

def test_NC1_short_circuiting_the_memory_route_breaks_the_lesson_test(flask_app):
    """NEUTER the route body (return ok WITHOUT writing any memory) ->
    test_lesson_goes_to_memory_not_charter's memory assertion must fail."""
    def run(_mod=None):
        resp = _commit(flask_app, {'kind': 'lesson', 'decision': _LESSON_A})
        assert 'memory' in resp.lower()
        mems = _project_memories()
        assert not mems, (
            'NC-1 did not bite: a memory exists even though the route was '
            'short-circuited — the lesson test may be passing vacuously')

    _patch_restore(
        _CHARTER_SRC,
        "    try:\n        from lib.memory.storage import (create_memory, "
        "list_memories,\n"
        "                                        update_memory)",
        "    try:\n        return {'ok': True, 'action': 'created', "
        "'memory_id': 'nc1-fake'}\n"
        "        from lib.memory.storage import (create_memory, "
        "list_memories,\n"
        "                                        update_memory)",
        run)


def test_NC2_falling_back_to_full_text_breaks_the_two_tier_split(flask_app):
    """NEUTER the summary preference in _decision_headline (fall through to
    full text) -> the evidence tail leaks into the injection."""
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})

    def run(_mod=None):
        inj = _injection(flask_app)
        assert _EVIDENCE_TAIL in inj, (
            'NC-2 did not bite: the headline still hides the evidence tail '
            'after the summary preference was neutered')

    _patch_restore(
        _CHARTER_SRC,
        "        summary = (d.get('summary') or '').strip()\n"
        "        if summary:\n"
        "            return summary",
        "        summary = (d.get('summary') or '').strip()\n"
        "        if summary:\n"
        "            pass  # NC-2: fall through to the full text",
        run)
