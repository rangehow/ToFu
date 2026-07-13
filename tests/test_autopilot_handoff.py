"""tests/test_autopilot_handoff.py — Autopilot HANDOFF terminal verdict.

The gap this covers: an autopilot run whose objective is substantively met
EXCEPT for a residual criterion that is blocked on an EXTERNAL commit (a sibling
conversation must land a file first) used to collapse into a clean
``[VU: TASK_DONE]`` — a false green — because the virtual-user verdict was
binary (done | keep-going). There was no terminal state for "blocked-incomplete
on a dependency the assistant cannot itself resolve".

HANDOFF is that third terminal verdict. It reuses the project board's existing
wait-on-path primitive (epic → path, keyed on the live lease, self-expiring, no
reaper) rather than inventing a resume engine: the VU emits
``[VU: HANDOFF paths=a,b]`` when it recognises the external-commit blocker, the
hook posts ONE board epic capturing the residual work + a ``[sibling] … path=a,b``
block reason (which auto-populates the epic's ``wait_paths``), and concludes the
run with a distinct ``reason='parked'`` sidecar record. When the sibling commits
(lease released) the board's wait-on-path clears at read time and the dispatch
flywheel picks the fold-in up on an idle sibling — end-to-end autonomous, with
ZERO extra LLM turns (the VU's own handoff reasoning IS the parked report).

Pure-logic + monkeypatched DB/board — no live LLM / orchestrator / DB.
"""

import json
import threading

import pytest

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  1. parse_vu_handoff — the structured [VU: HANDOFF paths=…] token
# ══════════════════════════════════════════════════════════

def test_parse_vu_handoff_structured_token():
    from lib.agent_verdict import parse_vu_handoff
    # Present with a comma-separated path list → the list.
    assert parse_vu_handoff(
        'All code done; fresh-HEAD verify blocked on Epic E.\n'
        '[VU: HANDOFF paths=lib/paper/report_engine.py,lib/paper/images.py]'
    ) == ['lib/paper/report_engine.py', 'lib/paper/images.py']


def test_parse_vu_handoff_absent_returns_none():
    from lib.agent_verdict import parse_vu_handoff
    # No handoff sentinel at all → None (distinct from an empty path list).
    assert parse_vu_handoff('Keep going, run the tests.') is None
    assert parse_vu_handoff('[VU: TASK_DONE]') is None
    assert parse_vu_handoff('') is None


def test_parse_vu_handoff_singular_and_plural_key():
    from lib.agent_verdict import parse_vu_handoff
    # Accept both `paths=` and `path=` from the VU (robust to model phrasing).
    assert parse_vu_handoff('[VU: HANDOFF path=a.py]') == ['a.py']
    assert parse_vu_handoff('[VU: HANDOFF paths=a.py,b.py]') == ['a.py', 'b.py']


def test_parse_vu_handoff_present_without_paths_is_empty_list():
    from lib.agent_verdict import parse_vu_handoff
    # A bare HANDOFF (no path token) is still a handoff signal → [] not None,
    # so classify treats it as handoff (parked) rather than TASK_DONE.
    assert parse_vu_handoff('[VU: HANDOFF]') == []


def test_parse_vu_handoff_trailing_prose_not_consumed_and_deduped():
    from lib.agent_verdict import parse_vu_handoff
    # The value ends at the first whitespace run (trailing prose is NOT eaten
    # into the last path), and duplicates collapse.
    assert parse_vu_handoff(
        '[VU: HANDOFF paths=a.py,b.py,a.py] because the sibling owns them'
    ) == ['a.py', 'b.py']


# ══════════════════════════════════════════════════════════
#  2. classify_verdict — the 'handoff' phase (virtual_user)
# ══════════════════════════════════════════════════════════

def test_classify_verdict_handoff_phase():
    from lib.agent_verdict import classify_verdict
    v = classify_verdict(
        'Code is complete; the fresh-HEAD verify is blocked until the sibling '
        'commits.\n[VU: HANDOFF paths=a.py,b.py]\n[PROGRESS: resolved=3 remaining=1]',
        verifier_role='virtual_user')
    assert v['phase'] == 'handoff'
    assert v.get('handoff_paths') == ['a.py', 'b.py']


def test_classify_verdict_handoff_beats_taskdone_and_unresolved_markers():
    """HANDOFF is the most specific signal: it MEANS 'remaining but externally
    blocked', so it must NOT be downgraded by ❌ / 'NOT met' markers (which
    downgrade a bare TASK_DONE) and must win over a co-emitted TASK_DONE.

    NEGATIVE CONTROL: removing the handoff branch from classify_verdict makes
    this fall through to the ❌-downgrade (phase='worker') or the stop path —
    either way phase != 'handoff', failing here.
    """
    from lib.agent_verdict import classify_verdict
    v = classify_verdict(
        'The remaining criterion is ❌ NOT met yet — it is blocked on an '
        'external commit, nothing more I can do here.\n'
        '[VU: HANDOFF paths=a.py]\n[PROGRESS: resolved=2 remaining=1]',
        verifier_role='virtual_user')
    assert v['phase'] == 'handoff'
    assert v.get('handoff_paths') == ['a.py']


def test_classify_verdict_plain_taskdone_still_stops():
    """Regression guard: a plain clean TASK_DONE (no handoff token) still ends
    the loop with phase='stop' — HANDOFF must not shadow the normal close-out."""
    from lib.agent_verdict import classify_verdict
    v = classify_verdict('Objective met and verified.\n[VU: TASK_DONE]\n'
                         '[PROGRESS: resolved=4 remaining=0]',
                         verifier_role='virtual_user')
    assert v['phase'] == 'stop'
    assert not v.get('handoff_paths')


def test_classify_verdict_keepgoing_is_not_handoff():
    """A normal keep-going nudge (no sentinel) stays 'worker'."""
    from lib.agent_verdict import classify_verdict
    v = classify_verdict('Run the tests and report back.\n'
                         '[PROGRESS: resolved=1 remaining=3]',
                         verifier_role='virtual_user')
    assert v['phase'] == 'worker'


# ══════════════════════════════════════════════════════════
#  3. run_virtual_user — handoff branch stamps flags + audits
# ══════════════════════════════════════════════════════════

def _vu_task():
    return {
        'id': 'task-ho-0001',
        'convId': 'conv-ho',
        'config': {'model': 'm', 'autopilot': True,
                   'projectPath': '/proj/ho'},
        'messages': [
            {'role': 'user', 'content': 'Land the fix with TDD.'},
            {'role': 'assistant', 'content': 'Code done; verify blocked on Epic E.'},
        ],
    }


def test_run_virtual_user_handoff_sets_flags(monkeypatch):
    """When the VU emits [VU: HANDOFF paths=…], run_virtual_user returns None
    (loop ends) and stamps the handoff flag + parsed paths + cleaned reasoning
    text on the parent task, so maybe_run_autopilot can conclude it as parked.
    """
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    handoff_reply = (
        'Everything is implemented and 20/20 tests pass. The only remaining '
        'acceptance criterion — the fresh-HEAD worktree verify — cannot run '
        'until the sibling commits report_engine.py and images.py. Handing the '
        'residual off to the board.\n'
        '[VU: HANDOFF paths=lib/paper/report_engine.py,lib/paper/images.py]\n'
        '[PROGRESS: resolved=3 remaining=1]')

    def _fake_turn(sub_task):
        sub_task['toolRounds'] = []
        return {'content': handoff_reply}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Land the fix with TDD.')

    audits = []
    monkeypatch.setattr(ap, 'audit_log',
                        lambda ev, **kw: audits.append((ev, kw)))

    task = _vu_task()
    result = ap.run_virtual_user(task)

    # Loop ends (same shape as TASK_DONE) …
    assert result is None
    # … but via the HANDOFF flag, not the DONE flag.
    assert task.get('_vu_emitted_handoff') is True
    assert not task.get('_vu_emitted_done')
    assert task['_vu_handoff_paths'] == ['lib/paper/report_engine.py',
                                         'lib/paper/images.py']
    # The cleaned reasoning is stashed for the parked report — WITHOUT the
    # machine sentinel token.
    assert '[VU: HANDOFF' not in task['_vu_handoff_text']
    assert 'residual' in task['_vu_handoff_text'].lower()
    # Audited as a handoff stop (distinct reason).
    assert any(ev == 'autopilot_stop' and kw.get('reason') == 'vu_handoff'
               for ev, kw in audits)


# ══════════════════════════════════════════════════════════
#  4. _store_run_record — the 'parked' reason
# ══════════════════════════════════════════════════════════

def _fake_settings_db(monkeypatch, state):
    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def fetchone(_self):
                    return (state['settings'],)
            return _R()

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql or 'settings=' in sql:
            state['settings'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)


def test_store_run_record_parked_carries_waitpaths_and_is_not_incomplete(monkeypatch):
    """A parked record is concluded with reason='parked', carries the waitPaths
    + boardTaskId, and is NOT flagged `incomplete` — parked is a DELIBERATE,
    clean handoff, not a safety-cap cutoff (which would render 'needs review').
    """
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    rec = ap._store_run_record('conv-p', 'ar-p', reason='parked',
                               text='Parked: residual blocked on Epic E.',
                               wait_paths=['a.py', 'b.py'],
                               board_task_id='pt_deadbeef')
    assert rec is not None
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'parked'
    assert rec['waitPaths'] == ['a.py', 'b.py']
    assert rec['boardTaskId'] == 'pt_deadbeef'
    assert rec['content'] == 'Parked: residual blocked on Epic E.'
    # NOT a safety-cap cutoff → no incomplete flag.
    assert 'incomplete' not in rec

    stored = json.loads(state['settings'])['autopilotSummaries']['ar-p']
    assert stored['reason'] == 'parked'
    assert stored['waitPaths'] == ['a.py', 'b.py']


def test_store_run_record_parked_not_downgraded_by_stopped(monkeypatch):
    """A later bare `stopped` conclude must NOT downgrade a `parked` record
    (they can race, mirroring the task_done stickiness), and task_done still
    supersedes parked."""
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    ap._store_run_record('conv-pr', 'ar-pr', reason='parked',
                         wait_paths=['a.py'], text='parked note')
    ap._store_run_record('conv-pr', 'ar-pr', reason='stopped')  # racing stop
    rec = json.loads(state['settings'])['autopilotSummaries']['ar-pr']
    assert rec['reason'] == 'parked'          # not downgraded
    assert rec['waitPaths'] == ['a.py']       # preserved
    assert rec['content'] == 'parked note'    # preserved

    ap._store_run_record('conv-pr', 'ar-pr', reason='task_done',
                         text='Report.')       # clean done supersedes
    rec = json.loads(state['settings'])['autopilotSummaries']['ar-pr']
    assert rec['reason'] == 'task_done'


# ══════════════════════════════════════════════════════════
#  5. _conclude_handoff — post board epic + wait-on-path + park
# ══════════════════════════════════════════════════════════

def _handoff_ready_task():
    t = _vu_task()
    t['_vu_emitted_handoff'] = True
    t['_vu_handoff_paths'] = ['lib/paper/report_engine.py', 'lib/paper/images.py']
    t['_vu_handoff_text'] = ('Everything is implemented; the fresh-HEAD verify '
                             'is blocked until the sibling commits those files.')
    return t


def _patch_board(monkeypatch):
    from lib.conversations import project_board as board
    calls = {'post': [], 'block': []}

    def _fake_post(project_path, conv_id, title, **kw):
        calls['post'].append({'project_path': project_path, 'conv_id': conv_id,
                              'title': title})
        return {'ok': True, 'id': 'pt_ho12345678'}

    def _fake_block(project_path, conv_id, task_id, reason):
        calls['block'].append({'task_id': task_id, 'reason': reason})
        return {'ok': True}

    monkeypatch.setattr(board, 'post_task', _fake_post)
    monkeypatch.setattr(board, 'block_task', _fake_block)
    return calls


def test_conclude_handoff_posts_board_epic_and_wait_and_parks(monkeypatch):
    """_conclude_handoff posts ONE board epic for the residual work, blocks it
    with a `[sibling] … path=a,b` reason (which auto-populates the epic's
    wait-on-path hold), and stores a parked sidecar record carrying the
    waitPaths + boardTaskId + the VU's handoff reasoning as the report.

    NEGATIVE CONTROL: the `path=` token in the block reason is what makes the
    board derive the wait-on-path hold (via _parse_sibling_wait_paths). A reason
    lacking `[sibling]` or `path=` would populate NO wait → the run would never
    auto-resume. The assertion on the reason shape is that guard.
    """
    import lib.tasks_pkg.autopilot as ap
    calls = _patch_board(monkeypatch)

    stored = {}
    monkeypatch.setattr(ap, '_store_run_record',
                        lambda conv_id, run_id, *, reason='task_done', text='',
                        translated='', wait_paths=None, board_task_id='':
                        stored.update(conv_id=conv_id, run_id=run_id,
                                      reason=reason, text=text,
                                      wait_paths=wait_paths,
                                      board_task_id=board_task_id) or
                        {'runId': run_id, 'status': 'concluded',
                         'reason': reason, 'content': text,
                         'waitPaths': wait_paths, 'boardTaskId': board_task_id})
    concluded = []
    monkeypatch.setattr(ap, '_emit_run_concluded',
                        lambda conv_id, run_id, text, cfg: concluded.append(text))
    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: events.append(ev))

    rec = ap._conclude_handoff(_handoff_ready_task(), 'conv-ho', 'ar-ho')

    # Board epic posted for the residual work.
    assert len(calls['post']) == 1
    assert calls['post'][0]['project_path'] == '/proj/ho'
    # Blocked with a sibling class tag + the path= token (the two things
    # _parse_sibling_wait_paths requires to derive the wait-on-path hold).
    assert len(calls['block']) == 1
    reason = calls['block'][0]['reason']
    assert '[sibling]' in reason.lower()
    assert 'path=lib/paper/report_engine.py,lib/paper/images.py' in reason
    assert calls['block'][0]['task_id'] == 'pt_ho12345678'
    # The board reason really does yield the wait paths (cross-check the actual
    # board parser, not just our string).
    from lib.conversations.project_board import _parse_sibling_wait_paths
    assert _parse_sibling_wait_paths(reason) == [
        'lib/paper/report_engine.py', 'lib/paper/images.py']

    # Parked sidecar record carries the waitPaths + board link + VU reasoning.
    assert stored['reason'] == 'parked'
    assert stored['wait_paths'] == ['lib/paper/report_engine.py',
                                    'lib/paper/images.py']
    assert stored['board_task_id'] == 'pt_ho12345678'
    assert 'blocked' in stored['text'].lower()
    # run_concluded feed pulse + RUN_CONCLUDED SSE fold record emitted.
    assert concluded and rec is not None
    assert any(ev.get('type') == 'autopilot_run_concluded' for ev in events)


def test_conclude_handoff_no_project_skips_board_still_parks(monkeypatch):
    """With no projectPath (non-project conversation) there is no board to post
    to — the run still parks (honest terminal state) but posts no epic."""
    import lib.tasks_pkg.autopilot as ap
    calls = _patch_board(monkeypatch)

    stored = {}
    monkeypatch.setattr(ap, '_store_run_record',
                        lambda conv_id, run_id, *, reason='task_done', text='',
                        translated='', wait_paths=None, board_task_id='':
                        stored.update(reason=reason, board_task_id=board_task_id)
                        or {'runId': run_id, 'status': 'concluded',
                            'reason': reason})
    monkeypatch.setattr(ap, '_emit_run_concluded', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: None)

    task = _handoff_ready_task()
    task['config']['projectPath'] = ''
    ap._conclude_handoff(task, 'conv-ho', 'ar-ho')

    assert calls['post'] == []            # no board post without a project
    assert stored['reason'] == 'parked'   # still parked
    assert stored['board_task_id'] == ''  # no epic link


# ══════════════════════════════════════════════════════════
#  6. maybe_run_autopilot — HANDOFF concludes parked, disarms
# ══════════════════════════════════════════════════════════

def test_maybe_run_autopilot_handoff_concludes_parked(monkeypatch):
    """On a HANDOFF verdict, maybe_run_autopilot must conclude the run via
    _conclude_handoff, disarm the marker + clear the run pin, emit vu_cancel,
    and return None (no follow-up baton — the loop is ending)."""
    import lib.tasks_pkg.autopilot as ap

    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda conv_id: 'ar-ho')
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda conv_id: False)
    monkeypatch.setattr(ap, '_successor_already_running',
                        lambda task, conv_id: False)

    def _fake_vu(task, vu_msg_id=None):
        task['_vu_emitted_handoff'] = True
        task['_vu_handoff_paths'] = ['a.py']
        task['_vu_handoff_text'] = 'blocked on sibling commit'
        return None
    monkeypatch.setattr(ap, 'run_virtual_user', _fake_vu)

    concluded = []
    monkeypatch.setattr(ap, '_conclude_handoff',
                        lambda task, conv_id, run_id:
                        concluded.append((conv_id, run_id)) or
                        {'runId': run_id, 'status': 'concluded',
                         'reason': 'parked'})
    cleared = {'marker': [], 'run_pin': []}
    import lib.message_queue as _mq
    monkeypatch.setattr(_mq, 'clear_autopilot_marker',
                        lambda cid: cleared['marker'].append(cid))
    monkeypatch.setattr(ap, '_clear_run_id',
                        lambda cid: cleared['run_pin'].append(cid))
    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: events.append(ev))
    # A HANDOFF must NOT spawn the LLM close-out reporter.
    reporter = []
    monkeypatch.setattr(ap, '_spawn_async_run_summary',
                        lambda *a, **k: reporter.append(a))

    task = {'id': 'task-ho-9', 'convId': 'conv-ho',
            'config': {'model': 'm', 'autopilot': True, 'projectPath': '/p'},
            'messages': [{'role': 'user', 'content': 'go'},
                         {'role': 'assistant', 'content': 'done-ish'}]}
    result = ap.maybe_run_autopilot(task)

    assert result is None
    assert concluded == [('conv-ho', 'ar-ho')], 'must conclude via handoff path'
    assert cleared['marker'] == ['conv-ho']
    assert cleared['run_pin'] == ['conv-ho']
    assert any(ev.get('type') == 'autopilot_vu_cancel' for ev in events)
    assert reporter == [], 'handoff must not run the LLM close-out reporter'


# ══════════════════════════════════════════════════════════
#  7. TASK_DONE-with-remaining>0 gate — the ROOT-CAUSE fix
# ══════════════════════════════════════════════════════════
#
#  The original bug: a VU emitted a clean [VU: TASK_DONE] while work remained,
#  and the verdict path IGNORED the mandatory [PROGRESS: remaining=Y] signal —
#  a false green. HANDOFF only helps if the VU CHOOSES to emit it; this gate is
#  the backend-authoritative backstop that refuses a self-contradictory
#  TASK_DONE (done-claim vs its own remaining>0) regardless of prompt behaviour.
#  It cross-checks the SAME hard signal detect_diminishing_returns trusts, and
#  fails open when PROGRESS is unparseable (can't prove incompleteness).

def test_taskdone_with_remaining_downgrades_to_worker():
    """A [VU: TASK_DONE] whose own [PROGRESS: remaining=1] contradicts the
    done-claim must NOT stop — downgrade to 'worker' (keep going). This is the
    exact false-conclude from the reported bug.

    NEGATIVE CONTROL: deleting the remaining>0 gate makes this return
    phase='stop' (the original false green), failing here.
    """
    from lib.agent_verdict import classify_verdict
    v = classify_verdict(
        'Everything looks complete to me.\n[VU: TASK_DONE]\n'
        '[PROGRESS: resolved=3 remaining=1]',
        verifier_role='virtual_user')
    assert v['phase'] == 'worker'


def test_taskdone_with_remaining_zero_still_stops():
    """The complementary case: TASK_DONE with remaining=0 is internally
    consistent → still stops (clean close-out). The gate only bites on a
    CONTRADICTION, never on a genuinely-complete run."""
    from lib.agent_verdict import classify_verdict
    v = classify_verdict('Objective met and verified.\n[VU: TASK_DONE]\n'
                         '[PROGRESS: resolved=4 remaining=0]',
                         verifier_role='virtual_user')
    assert v['phase'] == 'stop'


def test_taskdone_without_parseable_progress_fails_open_stops():
    """FAIL-OPEN: a TASK_DONE with NO parseable [PROGRESS] line still stops —
    we cannot prove incompleteness without the hard signal, so we do not block
    a done-claim on a missing signal (mirrors detect_diminishing_returns)."""
    from lib.agent_verdict import classify_verdict
    v = classify_verdict('All done here.\n[VU: TASK_DONE]',
                         verifier_role='virtual_user')
    assert v['phase'] == 'stop'
    # A malformed / non-numeric PROGRESS also fails open.
    v2 = classify_verdict('Done.\n[VU: TASK_DONE]\n[PROGRESS: resolved=x remaining=y]',
                          verifier_role='virtual_user')
    assert v2['phase'] == 'stop'


def test_handoff_with_remaining_still_routes_to_handoff():
    """A HANDOFF with remaining>0 is the NORMAL, correct case (that IS why it
    hands off) — it must route to 'handoff', never be caught by this new
    remaining>0 gate (which only guards the TASK_DONE stop path). HANDOFF is
    checked first, so this proves the two mechanisms don't collide."""
    from lib.agent_verdict import classify_verdict
    v = classify_verdict(
        'Blocked on the sibling commit.\n[VU: HANDOFF paths=a.py]\n'
        '[PROGRESS: resolved=2 remaining=1]',
        verifier_role='virtual_user')
    assert v['phase'] == 'handoff'
    assert v.get('handoff_paths') == ['a.py']


def test_taskdone_remaining_gate_audits(monkeypatch):
    """The downgrade is AUDITED so it is visible (mirrors the ❌-downgrade
    vu_done_override audit) — the reason distinguishes it from the marker scan."""
    import lib.agent_verdict as av
    audits = []
    monkeypatch.setattr(av, 'audit_log',
                        lambda ev, **kw: audits.append((ev, kw)))
    av.classify_verdict('Done.\n[VU: TASK_DONE]\n[PROGRESS: resolved=1 remaining=2]',
                        verifier_role='virtual_user')
    assert any(kw.get('reason') == 'progress_remaining_in_vu_done'
               for _ev, kw in audits), audits


# ══════════════════════════════════════════════════════════
#  8. VU role prompt teaches the HANDOFF rule
# ══════════════════════════════════════════════════════════

def test_vu_role_prompt_teaches_handoff():
    from lib.tasks_pkg.autopilot import _VU_ROLE_PROMPT as p
    low = p.lower()
    # The prompt must instruct the VU to emit the HANDOFF sentinel (with a path
    # token) when the remaining criteria are blocked on an external commit, and
    # explain it parks the residual on the board (auto-resolves).
    assert '[vu: handoff' in low
    assert 'paths=' in low
    assert 'blocked' in low and ('external' in low or 'sibling' in low)
