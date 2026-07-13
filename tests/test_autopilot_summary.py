"""tests/test_autopilot_summary.py — Autopilot run close-out summary reporter.

Covers the "fold the run + summary report at the end" feature:
  • ``run_summary_reporter`` is READ-ONLY — its sub-task config strips every
    tool-enabling feature so the reporter only synthesises the existing
    transcript (no new state changes, cheap). This is the negative-control:
    reverting the read-only stripping makes ``test_reporter_subconfig_is_read_only``
    fail.
  • the reporter prompt encodes the objective-anchored debrief structure
    (outcome verdict + what was done + verification + gaps) and is honest.
  • ``REPORTER_PROMPT_VERSION`` is content-derived (stale-process detection),
    mirroring ``VU_PROMPT_VERSION``.
  • ``_store_run_record`` persists ONE authoritative per-run record in the
    conversation SIDECAR (``settings.autopilotSummaries[runId]``) carrying BOTH
    the terminal ``status='concluded'`` + ``reason`` AND the optional close-out
    ``content`` — a human-only record, NOT a ``role='assistant'`` chat message.
    ``_store_run_summary`` is the clean-close-out wrapper (reason=task_done).
    The negative control here re-appends it as a message and asserts that fails
    the "not a message" contract.
  • the ``autopilot_run_concluded`` event is registered in the streaming
    contract and carries the sidecar ``record`` (both the concluded status and
    the optional report) — the single BACKEND-AUTHORITATIVE run-end fact.

No live LLM / orchestrator — ``_run_single_turn`` is stubbed.
"""

import hashlib
import json
import threading
import time

import pytest


# ── reporter prompt: structure + honesty + version marker ──────────────

def test_reporter_prompt_has_debrief_structure_and_honesty():
    from lib.tasks_pkg.autopilot import _REPORTER_ROLE_PROMPT as p
    low = p.lower()
    # Objective-anchored debrief sections.
    assert 'objective' in low
    assert 'outcome' in low
    assert 'verification' in low
    assert 'gaps' in low or 'risks' in low
    # Honest debrief, not a victory lap; read-only (no new tools).
    assert 'not a victory lap' in low or 'honest' in low
    assert 'do not run any tools' in low or 'do not start new work' in low
    # Readability contract: the report must be self-contained for a reader
    # who did NOT watch the run, and must translate the transcript into plain
    # language rather than replaying tool calls / internal jargon verbatim.
    assert 'self-contained' in low
    assert 'did not watch' in low
    assert 'translate' in low and 'transcribe' in low
    assert 'tool-call' in low or 'tool call' in low


def test_reporter_prompt_version_is_content_derived():
    import lib.tasks_pkg.autopilot as ap
    expected = hashlib.sha256(
        ap._REPORTER_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]
    assert ap.REPORTER_PROMPT_VERSION == expected
    assert len(ap.REPORTER_PROMPT_VERSION) == 8


# ── run_summary_reporter: read-only sub-config (the negative control) ──

def _summary_task():
    return {
        'id': 'task-sum-test-0001',
        'convId': 'conv-sum-test',
        'config': {
            'model': 'm',
            'autopilot': True,
            # Deliberately turn things ON to prove the reporter strips them.
            'searchMode': 'multi',
            'fetchEnabled': True,
            'projectPath': '/some/project',
            'codeExecEnabled': True,
            'browserEnabled': True,
            'memoryEnabled': True,
            'swarmEnabled': True,
        },
        'messages': [
            {'role': 'user', 'content': 'Ship a working feature.'},
            {'role': 'assistant', 'content': 'Done — I built X and tests pass.'},
            {'role': 'user', 'content': 'keep going', '_isVirtualUser': True},
            {'role': 'assistant', 'content': 'Refined X, verified the edge case.'},
        ],
    }


def test_reporter_subconfig_is_read_only(monkeypatch):
    """The reporter sub-task must run with EVERY tool-enabling feature OFF.

    NEGATIVE CONTROL: this is the assertion that the read-only stripping in
    ``run_summary_reporter`` exists. Reverting that stripping (leaving the
    parent's searchMode/projectPath/etc. on the sub-config) makes this fail.
    """
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    captured = {}

    def _fake_turn(sub_task):
        captured['cfg'] = dict(sub_task.get('config') or {})
        captured['messages'] = sub_task.get('messages') or []
        sub_task['toolRounds'] = []
        return {'content': 'Objective: ship X.\nOutcome: met.\nDone: built X.'}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Ship a working feature.')

    report = ap.run_summary_reporter(_summary_task())
    assert report is not None
    assert 'Outcome' in report['text']

    cfg = captured['cfg']
    assert cfg.get('searchMode') == 'off'
    assert cfg.get('fetchEnabled') is False
    assert cfg.get('projectPath') == ''
    assert cfg.get('codeExecEnabled') is False
    assert cfg.get('browserEnabled') is False
    assert cfg.get('memoryEnabled') is False
    assert cfg.get('swarmEnabled') is False
    assert cfg.get('autopilot') is False
    assert cfg.get('endpointMode') is False

    # The directive carries the reporter role + objective anchor + version.
    directive = captured['messages'][-1]
    assert directive.get('_isReporterDirective') is True
    assert directive.get('_reporterPromptVersion') == ap.REPORTER_PROMPT_VERSION
    assert 'ORIGINAL OBJECTIVE' in directive['content']
    assert 'Ship a working feature.' in directive['content']


def test_reporter_empty_output_returns_none(monkeypatch):
    """An empty report produces None (nothing to fold/show)."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    monkeypatch.setattr(orch, '_run_single_turn',
                        lambda st: (st.__setitem__('toolRounds', []),
                                    {'content': '   '})[1])
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Obj.')
    assert ap.run_summary_reporter(_summary_task()) is None


def test_reporter_error_returns_none(monkeypatch):
    """A sub-task error skips the report (non-fatal)."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    monkeypatch.setattr(orch, '_run_single_turn',
                        lambda st: {'error': 'llm down'})
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Obj.')
    assert ap.run_summary_reporter(_summary_task()) is None


# ── event contract ────────────────────────────────────────────────────

def test_autopilot_run_concluded_event_registered():
    from lib.agent_core.events import EventType, is_registered, get_event_spec
    # The unified BACKEND-AUTHORITATIVE run-end fact (renamed from the old
    # summary-only 'autopilot_summary'), fired on BOTH close-out paths.
    assert EventType.AUTOPILOT_RUN_CONCLUDED == 'autopilot_run_concluded'
    assert is_registered(EventType.AUTOPILOT_RUN_CONCLUDED)
    assert not hasattr(EventType, 'AUTOPILOT_SUMMARY')
    spec = get_event_spec(EventType.AUTOPILOT_RUN_CONCLUDED)
    assert 'runId' in spec.fields
    # The payload carries the SIDECAR record (`record`) — both the concluded
    # status/reason AND the optional report — not a chat message.
    assert 'record' in spec.fields
    assert 'summary' not in spec.fields
    assert 'summaryMessage' not in spec.fields


# ── _store_run_summary: SIDECAR persistence, NOT a chat message ────────

def test_store_run_summary_writes_sidecar_not_messages(monkeypatch):
    """The summary must land in settings.autopilotSummaries[runId] as a
    human-only record — never as a role='assistant' row in `messages`.

    NEGATIVE CONTROL: re-pointing _store_run_summary at the message list
    (appending a role='assistant' summary row) makes the "messages untouched"
    and "no role/_msgId" assertions fail.
    """
    import lib.tasks_pkg.autopilot as ap

    # In-memory fake DB capturing the settings/messages UPDATE.
    state = {'messages': '[{"role":"user","content":"hi"}]', 'settings': '{}'}

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def __init__(self, row):
                    self._row = row
                def fetchone(self):
                    return self._row
            if 'SELECT settings' in sql:
                return _R((state['settings'],))
            if 'SELECT messages' in sql:
                return _R((state['messages'],))
            return _R(None)

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql:
            state['settings'] = params[0]
        elif 'messages=' in sql:
            # The migration must NOT touch messages — capture if it does.
            state['messages'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)

    rec = ap._store_run_summary('conv-x', 'ar-1', 'Outcome: shipped X.',
                                translated='结果：已交付 X。')
    assert rec is not None
    # The record is NOT a message: no role, no _msgId.
    assert 'role' not in rec
    assert '_msgId' not in rec
    assert rec['runId'] == 'ar-1'
    assert rec['content'] == 'Outcome: shipped X.'
    assert rec['translatedContent'] == '结果：已交付 X。'
    # ★ The record now carries the terminal fold-fact too (clean close-out).
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'task_done'

    # It landed in the sidecar, keyed by runId.
    settings = json.loads(state['settings'])
    assert 'autopilotSummaries' in settings
    assert settings['autopilotSummaries']['ar-1']['content'] == 'Outcome: shipped X.'
    assert settings['autopilotSummaries']['ar-1']['status'] == 'concluded'

    # The messages column was NOT touched (the summary is not a chat message).
    msgs = json.loads(state['messages'])
    assert len(msgs) == 1
    assert msgs[0]['content'] == 'hi'
    for m in msgs:
        assert not m.get('_isAutopilotSummary')


def test_store_run_summary_multiple_runs_keyed_by_runid(monkeypatch):
    """Two runs in one conversation store side-by-side under their runIds."""
    import lib.tasks_pkg.autopilot as ap

    state = {'settings': '{}'}

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def fetchone(_self):
                    return (state['settings'],)
            return _R()

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql:
            state['settings'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)

    ap._store_run_summary('conv-y', 'ar-1', 'Report A')
    ap._store_run_summary('conv-y', 'ar-2', 'Report B')
    settings = json.loads(state['settings'])
    summaries = settings['autopilotSummaries']
    assert summaries['ar-1']['content'] == 'Report A'
    assert summaries['ar-2']['content'] == 'Report B'


# ── _store_run_record: the manual-stop (bare, no report) concluded fact ──

def _fake_settings_db(monkeypatch, state):
    """Wire a monkeypatched DB whose only column is settings JSON.

    ``_store_run_record`` / ``conclude_run`` route settings writes through
    ``lib.conversations.settings_store.update_conversation_settings``, which
    binds ``get_thread_db`` / ``db_execute_with_retry`` in the settings_store
    namespace at import — so those are the names that must be patched (patching
    ``lib.database.*`` alone would not intercept the settings write). We patch
    BOTH namespaces so the message-list path (``_append_vu_message_to_conv``,
    still on ``lib.database``) and the settings path both hit the fake.
    """
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


def test_store_run_record_manual_stop_is_concluded_without_report(monkeypatch):
    """A manual stop writes a concluded record with a reason but NO content.

    This is the crux of the symmetric-conclude fix: the manual-stop path now
    produces a BACKEND-AUTHORITATIVE fold-fact (status=concluded, reason=
    stopped) that the frontend keys on — instead of the frontend having to
    infer run-end from stream/task absence.
    """
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    rec = ap._store_run_record('conv-z', 'ar-stop', reason='stopped')
    assert rec is not None
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec           # a manual stop has no report
    assert 'role' not in rec and '_msgId' not in rec  # not a chat message

    stored = json.loads(state['settings'])['autopilotSummaries']['ar-stop']
    assert stored['status'] == 'concluded'
    assert stored['reason'] == 'stopped'
    assert 'content' not in stored


def test_conclude_run_writes_authoritative_stopped_record(monkeypatch):
    """conclude_run() is the manual-stop close-out seam: it resolves the run id,
    writes the BACKEND-AUTHORITATIVE concluded(stopped) record, and clears the
    run pin so the next run is fresh.

    NEGATIVE CONTROL: making conclude_run a no-op (returning None without
    writing the record) makes this test fail — proving the manual-stop fold
    depends on the backend fact, not on any frontend inference.
    """
    import lib.tasks_pkg.autopilot as ap
    # Seed a conv whose settings pin a live run id + carry a VU turn stamp.
    state = {'settings': json.dumps({'autopilotRunId': 'ar-live'}),
             'messages': json.dumps([
                 {'role': 'user', 'content': 'obj'},
                 {'role': 'user', 'content': 'vu', '_isVirtualUser': True,
                  '_autopilotRunId': 'ar-live'}])}

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def __init__(self, row):
                    self._row = row
                def fetchone(self):
                    return self._row
            if 'SELECT settings, messages' in sql:
                return _R((state['settings'], state['messages']))
            if 'SELECT settings' in sql:
                return _R((state['settings'],))
            return _R(None)

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql:
            state['settings'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)

    rec = ap.conclude_run('conv-live', reason='stopped')
    assert rec is not None
    assert rec['runId'] == 'ar-live'
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec  # a manual stop has no report

    settings = json.loads(state['settings'])
    # The concluded record landed in the sidecar under the resolved run id.
    assert settings['autopilotSummaries']['ar-live']['status'] == 'concluded'
    # The run pin was cleared so the next run mints a fresh id.
    assert 'autopilotRunId' not in settings


def test_store_run_record_task_done_reason_is_sticky(monkeypatch):
    """A later bare `stopped` conclude must NOT downgrade an earlier clean
    `task_done` record's verdict OR erase its report (they can race)."""
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    ap._store_run_record('conv-race', 'ar-r', reason='stopped')
    ap._store_run_record('conv-race', 'ar-r', reason='task_done',
                         text='Outcome: shipped.')
    ap._store_run_record('conv-race', 'ar-r', reason='stopped')  # racing stop

    rec = json.loads(state['settings'])['autopilotSummaries']['ar-r']
    assert rec['reason'] == 'task_done'       # never downgraded
    assert rec['content'] == 'Outcome: shipped.'  # report preserved
    assert rec['status'] == 'concluded'



# ── _emit_run_summary: report ONLY on a NORMAL end ─────────────────────
#  A run that finishes cleanly ([VU: TASK_DONE]) earns the close-out report.
#  A run CUT OFF by the budget/stuck/no_progress guard is an ABNORMAL end —
#  the objective is unverified, so we conclude the run (fold + disarm) but
#  must NOT spend an LLM reporter turn. `with_report=False` is that gate.

def _emit_task():
    return {
        'id': 'task-emit-0001',
        'convId': 'conv-emit',
        'config': {'model': 'm', 'autopilot': True, 'projectPath': '/proj/e'},
        'messages': [
            {'role': 'user', 'content': 'Ship it.'},
            {'role': 'assistant', 'content': 'Working on it.'},
        ],
    }


def test_emit_run_summary_normal_end_generates_report(monkeypatch):
    """A NORMAL end (with_report=True, the default) runs the reporter and
    persists the report content into the concluded record."""
    import lib.tasks_pkg.autopilot as ap

    reporter_calls = []
    monkeypatch.setattr(ap, 'run_summary_reporter',
                        lambda task: (reporter_calls.append(task),
                                      {'text': 'Outcome: met. Shipped it.'})[1])
    stored = {}
    monkeypatch.setattr(ap, '_store_run_record',
                        lambda conv_id, run_id, *, reason='task_done', text='',
                        translated='': stored.update(
                            reason=reason, text=text) or
                        {'runId': run_id, 'status': 'concluded',
                         'reason': reason, 'content': text})
    monkeypatch.setattr(ap, '_translate_summary_sync', lambda t: '')
    monkeypatch.setattr(ap, '_emit_run_concluded', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: None)

    rec = ap._emit_run_summary(_emit_task(), 'conv-emit', 'ar-e')
    assert len(reporter_calls) == 1, 'normal end MUST run the reporter'
    assert rec is not None
    assert stored['reason'] == 'task_done'
    assert stored['text'] == 'Outcome: met. Shipped it.'


def test_emit_run_summary_abnormal_end_skips_reporter(monkeypatch):
    """An ABNORMAL end (with_report=False) must NOT run the reporter, yet still
    conclude the run: a concluded record (with the incomplete reason, NO report
    text) is persisted and the run_concluded pulse is emitted."""
    import lib.tasks_pkg.autopilot as ap

    reporter_calls = []
    monkeypatch.setattr(ap, 'run_summary_reporter',
                        lambda task: (reporter_calls.append(task),
                                      {'text': 'SHOULD NOT APPEAR'})[1])
    stored = {}
    monkeypatch.setattr(ap, '_store_run_record',
                        lambda conv_id, run_id, *, reason='task_done', text='',
                        translated='': stored.update(
                            reason=reason, text=text) or
                        {'runId': run_id, 'status': 'concluded',
                         'reason': reason})
    concluded = []
    monkeypatch.setattr(ap, '_emit_run_concluded',
                        lambda conv_id, run_id, text, cfg: concluded.append(text))
    monkeypatch.setattr(ap, '_translate_summary_sync', lambda t: '')
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: None)

    rec = ap._emit_run_summary(_emit_task(), 'conv-emit', 'ar-e',
                               reason='budget_exhausted', with_report=False)

    assert reporter_calls == [], 'abnormal end must NOT run the LLM reporter'
    assert rec is not None, 'the run is still concluded (fold + disarm)'
    assert stored['reason'] == 'budget_exhausted'
    assert stored['text'] == '', 'no report text on an abnormal end'
    # The run_concluded feed pulse still fires (empty summary text is fine).
    assert concluded == [''], 'run_concluded still emitted with no report text'


def test_budget_stop_callsite_passes_with_report_false():
    """The budget/stuck guard call site must invoke _emit_run_summary with
    with_report=False — the wiring, not just the helper, is load-bearing.

    NEGATIVE CONTROL companion: were the call site to omit with_report (default
    True), an abnormal cap-stop would spend a full LLM reporter turn — exactly
    the behaviour the owner asked to remove ("if autopilot does not end
    normally, we should not proceed to generate the summary")."""
    import os
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'lib', 'tasks_pkg', 'autopilot.py')
    src = open(src_path, encoding='utf-8').read()
    # The clean-close-out (normal end) call site: with_report is still gated on
    # the per-run VU turn count (a single-/short-run clean close-out skips the
    # reporter), but the summary generation is now dispatched OFF the hot path
    # (daemon thread) so it can't hold the SSE stream open inside the
    # _autopilot_deciding window. The turn-count read MUST be captured on the
    # synchronous path (before _clear_run_id clears autopilotTurnCount) and
    # handed to the async spawn.
    assert 'with_report = _should_generate_run_summary(conv_id)' in src, \
        'the clean [VU: TASK_DONE] call site must gate the report on turn count'
    assert '_spawn_async_run_summary(task, conv_id, run_id, with_report)' in src, \
        'the clean call site must dispatch the summary OFF-THREAD (async spawn)'
    # The budget-guard (abnormal end) call site: explicitly with_report=False.
    assert ('_emit_run_summary(task, conv_id, run_id, reason=reason,\n'
            '                                            with_report=False)') in src, \
        'the budget/stuck guard call site must pass with_report=False'


# ── _should_generate_run_summary: report ONLY for a MULTI-ROUND run ────
#  A clean [VU: TASK_DONE] always CONCLUDES, but the LLM reporter turn is only
#  worth it when the run drove enough VU follow-up turns to be "too much to
#  read". A conversation with autopilot merely toggled ON, where the VU
#  concludes on its first look (autopilotTurnCount 0), skips the report.

def _seed_turncount_db(monkeypatch, settings_dict):
    """Wire a fake DB returning the given settings blob for the eligibility
    read (`SELECT settings FROM conversations`)."""
    import json as _json
    blob = _json.dumps(settings_dict)

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def fetchone(_self):
                    return (blob,)
            return _R()

    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())


def test_should_generate_run_summary_skips_single_round_run(monkeypatch):
    """turns=0 (< default floor 1) → no report (the reported waste case)."""
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.delenv('TOFU_AUTOPILOT_SUMMARY_MIN_TURNS', raising=False)
    _seed_turncount_db(monkeypatch, {'autopilotTurnCount': 0})
    assert ap._should_generate_run_summary('conv-single') is False


def test_should_generate_run_summary_reports_multi_round_run(monkeypatch):
    """turns>=floor → report (a genuine multi-round run)."""
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.delenv('TOFU_AUTOPILOT_SUMMARY_MIN_TURNS', raising=False)
    _seed_turncount_db(monkeypatch, {'autopilotTurnCount': 3})
    assert ap._should_generate_run_summary('conv-multi') is True


def test_should_generate_run_summary_gate_disabled_env(monkeypatch):
    """min-turns 0 disables the gate: even a single-round run reports (the
    pre-gate behaviour is recoverable via env)."""
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setenv('TOFU_AUTOPILOT_SUMMARY_MIN_TURNS', '0')
    _seed_turncount_db(monkeypatch, {'autopilotTurnCount': 0})
    assert ap._should_generate_run_summary('conv-disabled') is True


def test_should_generate_run_summary_fail_open_on_error(monkeypatch):
    """A DB/settings error must fail OPEN (report) — never silently suppress a
    legitimate report because of a glitch."""
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setenv('TOFU_AUTOPILOT_SUMMARY_MIN_TURNS', '1')

    class _BoomDB:
        def execute(self, sql, params=None):
            raise RuntimeError('db down')

    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _BoomDB())
    assert ap._should_generate_run_summary('conv-err') is True


def test_should_generate_run_summary_respects_custom_floor(monkeypatch):
    """A custom floor of 3: turns=2 skips, turns=3 reports."""
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setenv('TOFU_AUTOPILOT_SUMMARY_MIN_TURNS', '3')
    _seed_turncount_db(monkeypatch, {'autopilotTurnCount': 2})
    assert ap._should_generate_run_summary('conv-below') is False
    _seed_turncount_db(monkeypatch, {'autopilotTurnCount': 3})
    assert ap._should_generate_run_summary('conv-at') is True



# ── maybe_run_autopilot TASK_DONE: SETTLE FIRST, summarise OFF-THREAD ──
#  The reported bug: on a clean [VU: TASK_DONE], maybe_run_autopilot used to run
#  the reporter LLM turn + synchronous EN→ZH translation (~63s measured) BEFORE
#  it emitted the terminal signal — all INSIDE the _autopilot_deciding window
#  that _task_terminal()/chat_poll treat as still-running. The SSE stream stayed
#  open and the conv froze on "回答中" for a minute (and if the stream then
#  exceeded an idle/proxy timeout the client dropped to poll, which — with NO
#  follow-up baton on TASK_DONE — could stay stuck until a manual refresh).
#
#  Fix: settle the turn FIRST (disarm marker + clear run pin + let done fire) and
#  run the summary in a daemon thread. These tests pin that maybe_run_autopilot
#  RETURNS + clears _autopilot_deciding WITHOUT the reporter on the sync path,
#  and that the async body still lands the concluded record.

def _task_done_task():
    return {
        'id': 'task-done-0001',
        'convId': 'conv-td',
        'config': {'model': 'm', 'autopilot': True},
        'messages': [
            {'role': 'user', 'content': 'Ship it.'},
            {'role': 'assistant', 'content': 'Done.'},
        ],
        '_autopilot_deciding': True,
    }


def _stub_task_done_path(monkeypatch, ap, *, on_summary=None):
    """Wire maybe_run_autopilot's TASK_DONE branch for a no-DB unit test.

    - is_autopilot_enabled → True (config-driven, no marker probe).
    - _get_or_persist_run_id → a fixed run id.
    - run_virtual_user → None + stamp _vu_emitted_done (the TASK_DONE verdict).
    - _should_generate_run_summary → True (so a report WOULD be generated
      synchronously in the buggy path — the test proves it is NOT).
    - clear_autopilot_marker / _clear_run_id → captured, no DB.
    - append_event → captured (the vu_cancel emit).
    - _emit_run_summary → routed through `on_summary` so the test can observe
      WHICH THREAD invokes the reporter and WHEN.
    """
    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda conv_id: 'ar-td')

    def _fake_vu(task, vu_msg_id=None):
        task['_vu_emitted_done'] = True
        return None
    monkeypatch.setattr(ap, 'run_virtual_user', _fake_vu)
    monkeypatch.setattr(ap, '_should_generate_run_summary', lambda conv_id: True)
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda conv_id: False)
    monkeypatch.setattr(ap, '_successor_already_running',
                        lambda task, conv_id: False)

    cleared = {'marker': [], 'run_pin': []}
    import lib.message_queue as _mq
    monkeypatch.setattr(_mq, 'clear_autopilot_marker',
                        lambda cid: cleared['marker'].append(cid))
    monkeypatch.setattr(ap, '_clear_run_id',
                        lambda cid: cleared['run_pin'].append(cid))

    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: events.append(ev))

    if on_summary is not None:
        monkeypatch.setattr(ap, '_emit_run_summary', on_summary)
    return cleared, events


def test_task_done_settles_without_synchronous_reporter(monkeypatch):
    """On [VU: TASK_DONE], maybe_run_autopilot must RETURN None and clear
    _autopilot_deciding WITHOUT running the reporter on the calling thread.

    NEGATIVE CONTROL: reverting to the synchronous `_emit_run_summary(...)` call
    (the pre-fix behaviour) makes ``reporter_thread`` equal the main thread here
    — i.e. the assertion `!= main thread` fails. This is the regression that
    would have caught the ~63s freeze.
    """
    import lib.tasks_pkg.autopilot as ap

    main_thread = threading.current_thread()
    summary_done = threading.Event()
    seen = {'thread': None, 'run_id': None, 'with_report': None}

    def _obs_summary(task, conv_id, run_id, *, reason='task_done',
                     with_report=True):
        # Record WHICH thread ran the reporter + WHAT args it captured.
        seen['thread'] = threading.current_thread()
        seen['run_id'] = run_id
        seen['with_report'] = with_report
        summary_done.set()
        return {'runId': run_id, 'status': 'concluded', 'reason': 'task_done'}

    cleared, events = _stub_task_done_path(monkeypatch, ap,
                                           on_summary=_obs_summary)

    task = _task_done_task()
    result = ap.maybe_run_autopilot(task)

    # The hook returned immediately with NO follow-up baton (loop is ending).
    assert result is None
    # The deciding latch is cleared by the caller (orchestrator) — but the
    # SYNC path must not have run the reporter before returning.
    # The turn was settled synchronously: marker + run pin cleared, vu_cancel
    # emitted — all on the calling thread, cheaply.
    assert cleared['marker'] == ['conv-td']
    assert cleared['run_pin'] == ['conv-td']
    assert any(ev.get('type') == 'autopilot_vu_cancel' for ev in events)

    # The summary MUST land — but OFF the calling thread. Wait for the daemon.
    assert summary_done.wait(timeout=5), 'async summary never ran'
    assert seen['thread'] is not None
    assert seen['thread'] is not main_thread, \
        'the reporter ran on the CALLING thread (synchronous) — the freeze bug'
    # The ordering-hazard capture: run_id + with_report were passed through to
    # the async body (not re-read after _clear_run_id wiped the pin).
    assert seen['run_id'] == 'ar-td'
    assert seen['with_report'] is True


def test_task_done_captures_with_report_before_clearing_run_pin(monkeypatch):
    """The with-report decision (reads autopilotTurnCount) MUST be captured on
    the sync path BEFORE _clear_run_id clears it — proving the ordering hazard
    is handled by capture-up-front, not by the async body re-reading cleared
    state.

    We make _should_generate_run_summary observe call order relative to
    _clear_run_id: the eligibility read must happen first.
    """
    import lib.tasks_pkg.autopilot as ap

    order = []
    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda conv_id: 'ar-td')
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda conv_id: False)
    monkeypatch.setattr(ap, '_successor_already_running',
                        lambda task, conv_id: False)

    def _fake_vu(task, vu_msg_id=None):
        task['_vu_emitted_done'] = True
        return None
    monkeypatch.setattr(ap, 'run_virtual_user', _fake_vu)

    def _elig(conv_id):
        order.append('should_generate')
        return True
    monkeypatch.setattr(ap, '_should_generate_run_summary', _elig)
    monkeypatch.setattr(ap, '_clear_run_id',
                        lambda cid: order.append('clear_run_id'))
    import lib.message_queue as _mq
    monkeypatch.setattr(_mq, 'clear_autopilot_marker', lambda cid: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: None)
    # Swallow the async spawn so the daemon summary doesn't run in this test.
    spawned = {}
    monkeypatch.setattr(ap, '_spawn_async_run_summary',
                        lambda task, conv_id, run_id, with_report:
                        spawned.update(run_id=run_id, with_report=with_report))

    ap.maybe_run_autopilot(_task_done_task())

    # The eligibility read (autopilotTurnCount) precedes the run-pin clear.
    assert order == ['should_generate', 'clear_run_id']
    # And the captured decision is what gets handed to the async spawn.
    assert spawned == {'run_id': 'ar-td', 'with_report': True}


def test_run_summary_async_falls_back_to_bare_concluded_record(monkeypatch):
    """The daemon body must ALWAYS conclude the run: when _emit_run_summary
    returns None (empty/errored report), fall back to a bare
    _store_run_record(reason='task_done') so the run can still fold."""
    import lib.tasks_pkg.autopilot as ap

    monkeypatch.setattr(ap, '_emit_run_summary',
                        lambda task, conv_id, run_id, with_report=True: None)
    stored = {}
    monkeypatch.setattr(ap, '_store_run_record',
                        lambda conv_id, run_id, *, reason='task_done', text='',
                        translated='': stored.update(conv_id=conv_id,
                                                     run_id=run_id,
                                                     reason=reason))

    ap._run_summary_async(_task_done_task(), 'conv-td', 'ar-td', True)
    assert stored == {'conv_id': 'conv-td', 'run_id': 'ar-td',
                      'reason': 'task_done'}


def test_run_summary_async_skips_fallback_when_report_written(monkeypatch):
    """When _emit_run_summary DID write a concluded record, the daemon body must
    NOT also write the bare fallback (no redundant second write)."""
    import lib.tasks_pkg.autopilot as ap

    monkeypatch.setattr(ap, '_emit_run_summary',
                        lambda task, conv_id, run_id, with_report=True:
                        {'runId': run_id, 'status': 'concluded',
                         'reason': 'task_done', 'content': 'Report.'})
    fallback_calls = []
    monkeypatch.setattr(ap, '_store_run_record',
                        lambda *a, **k: fallback_calls.append((a, k)))

    ap._run_summary_async(_task_done_task(), 'conv-td', 'ar-td', True)
    assert fallback_calls == [], 'no bare fallback when the reporter wrote a record'


def test_spawn_async_run_summary_is_daemon_thread(monkeypatch):
    """The summary MUST be dispatched on a daemon thread (mirrors
    _spawn_async_commit_round) so it can never wedge shutdown or the hot path."""
    import lib.tasks_pkg.autopilot as ap

    ran = threading.Event()
    observed = {}

    def _fake_body(task, conv_id, run_id, with_report):
        observed['thread'] = threading.current_thread()
        ran.set()
    monkeypatch.setattr(ap, '_run_summary_async', _fake_body)

    ap._spawn_async_run_summary(_task_done_task(), 'conv-td', 'ar-td', True)
    assert ran.wait(timeout=5), 'daemon summary body never ran'
    assert observed['thread'].daemon is True
    assert observed['thread'] is not threading.current_thread()
