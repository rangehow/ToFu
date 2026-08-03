#!/usr/bin/env python3
"""ONE task = ONE assistant row — the same-``_taskId`` error-twin FOLD.

THE BUG (live, conv ``mscns5i0fcofgl``, task ``48ee5c90``, 2026-08-03)
----------------------------------------------------------------------
A turn that streamed partial prose (35 chars content + 8,642 thinking + 12
tool rounds) and then died on a 429 saturation persisted as TWO assistant
bubbles for ONE logical turn:

  * row 1 — the backend's own slot: the partial content + ``finishReason=
    'error'`` but NO error envelope and no ``_taskId`` until the terminal
    sync (the content-guard branch of ``_sync_result_to_conversation``
    writes finishReason/usage/_taskId but never the ``error`` field);
  * row 2 — a frontend reconnect placeholder (minted on a mid-task page
    reload, pushed back by the keep-local full-PUT): EMPTY content, the
    typed 429 envelope, the same toolRounds/segments echo, and (after the
    SSE bind) the same ``_taskId``.

The user sees a content bubble with a bare '错误' tag and, below it, a
second bubble holding the actual explanation. Worse: the Continue verdict
needs the tool rounds ON the same row that carries the error tail — split
this way the error bubble computes ``keptRounds=0`` → only "Regenerate",
never "Continue".

WHY THE EXISTING GUARDS COULD NOT FIRE
--------------------------------------
``is_duplicate_task_twin`` deliberately keeps a divergent pair: dropping a
twin that carries a terminal fact the keeper lacks (here: the error
envelope) would HIDE a real terminal outcome. Correct instinct, wrong
conclusion — the terminal fact must not be dropped WITH the twin, it must
be FOLDED ONTO the keeper first.

WHAT THIS SUITE PINS
--------------------
  A. reconcile level (pure): a payload-subsumed twin whose terminal fields
     diverge is FOLDED into the keeper (fill-absent) and dropped — never
     the other way round:
       keeper wins on conflict (its verdict is the terminal authority);
       an error envelope is never folded onto a CLEAN-finished keeper
       (a mid-stream transient contradicting 'stop' is noise — the same
       verdict the normal sync path already makes by popping stale errors).
  B. terminal-sync level (DB-driven): the settle writes the terminal
     ``error`` onto the task's OWN slot even when the content guard
     protects fuller existing content, and folds the twin row away in the
     same write — the DB converges to ONE row at settle time.

Guards that must NOT change: payload-divergent pairs, user-turn-spanning
pairs, endpoint/VU/special rows, cache-prefix rows all stay put.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_ENV_365 = {'kind': 'ratelimit', 'message': '⚠️ API 请求已达限频（429）',
            'detail': '429 saturation … 365 cycles'}
_ENV_364 = {'kind': 'ratelimit', 'message': '⚠️ API 请求已达限频（429）',
            'detail': '429 saturation … 364 cycles'}

_ROUNDS = [{'status': 'done', 'toolCallId': 'tc0', 'toolContent': 'out'}]


def _rc(messages, prefix=0):
    from lib.conversations.reconcile import reconcile_conversation_messages
    return reconcile_conversation_messages(copy.deepcopy(messages), prefix)


def _user(**kw):
    m = {'role': 'user', 'content': 'q'}
    m.update(kw)
    return m


def _asst(task_id, content='ANSWER', **kw):
    m = {'role': 'assistant', 'content': content, '_taskId': task_id}
    m.update(kw)
    return m


# ═══════════════════════════════════════════════════════════════════════
# A. reconcile-level fold contract
# ═══════════════════════════════════════════════════════════════════════

def test_error_envelope_folds_onto_unfinished_keeper():
    """★ THE INVARIANT. The keeper (a mid-stream checkpoint row: payload but
    NO terminal verdict yet) must ABSORB the twin's terminal verdict — the
    envelope is never dropped with the twin. One row survives, carrying the
    content AND the explanation."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'PID 谜团解开了', thinking='reasoning',
              _msgId='tmp_slot', toolRounds=copy.deepcopy(_ROUNDS)),
        _asst('task-A', '', _msgId='tmp_twin',
              finishReason='error', error=copy.deepcopy(_ENV_365),
              toolRounds=copy.deepcopy(_ROUNDS)),
    ]
    out, changed = _rc(msgs)
    assert changed, 'the error twin was not folded'
    assert len(out) == 2, (
        f'expected [user, one assistant], got '
        f'{[(m["role"], (m.get("_msgId") or "")[:12]) for m in out]}')
    kept = out[-1]
    assert kept['_msgId'] == 'tmp_slot', 'the content-bearing row must win'
    assert kept['finishReason'] == 'error', (
        "the twin's finishReason must fold onto the verdict-less keeper")
    assert kept['error'] == _ENV_365, (
        'the twin\'s error envelope must fold onto the keeper — dropping it '
        'with the twin is exactly the "hide the terminal outcome" outcome '
        'the old keep-both rule was protecting against')
    assert kept['content'] == 'PID 谜团解开了'
    assert kept['thinking'] == 'reasoning'


def test_conflicting_error_envelopes_keeper_wins():
    """Both rows carry an envelope (the frontend's twin holds an EARLIER
    attempt's 429 detail, the keeper the terminal one). The keeper is the
    terminal authority — the twin's stale detail is discarded with it."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'PID 谜团解开了', thinking='reasoning',
              _msgId='tmp_slot', finishReason='error',
              error=copy.deepcopy(_ENV_364),
              toolRounds=copy.deepcopy(_ROUNDS)),
        _asst('task-A', '', _msgId='tmp_twin', finishReason='error',
              error=copy.deepcopy(_ENV_365),
              toolRounds=copy.deepcopy(_ROUNDS)),
    ]
    out, changed = _rc(msgs)
    assert changed, 'the terminal-conflicting twin was not folded'
    assert len(out) == 2
    kept = out[-1]
    assert kept['_msgId'] == 'tmp_slot'
    assert kept['error'] == _ENV_364, (
        'the keeper\'s own (terminal) envelope must win over the twin\'s '
        'stale earlier-attempt detail')


def test_transient_error_on_clean_finished_keeper_is_discarded():
    """A mid-stream transient envelope contradicting the task's CLEAN finish
    is noise — the normal sync path already pops stale errors for exactly
    this reason ('absence of an error IS the verdict'). The fold must NOT
    glue an error onto a 'stop' row; the twin is dropped WITHOUT folding
    the error."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='srv-1', finishReason='stop',
              toolRounds=copy.deepcopy(_ROUNDS)),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin',
              error=copy.deepcopy(_ENV_365),
              toolRounds=copy.deepcopy(_ROUNDS)),
    ]
    out, changed = _rc(msgs)
    assert changed, 'the transient-error twin was not folded'
    assert len(out) == 2
    kept = out[-1]
    assert kept['finishReason'] == 'stop'
    assert not kept.get('error'), (
        'an error envelope must never be folded onto a CLEAN-finished row — '
        'the task succeeded; the twin\'s envelope was a mid-stream transient')


def test_twin_usage_folds_when_keeper_lacks_it():
    """usage is a terminal fact like the others: fill-absent, keeper-wins."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='srv-1',
              toolRounds=copy.deepcopy(_ROUNDS)),
        _asst('task-A', 'ANSWER', _msgId='tmp_twin',
              usage={'output_tokens': 42},
              toolRounds=copy.deepcopy(_ROUNDS)),
    ]
    out, changed = _rc(msgs)
    assert changed
    assert len(out) == 2
    assert out[-1]['usage'] == {'output_tokens': 42}


# ── Guards that must NOT change ─────────────────────────────────────────

def test_divergent_payload_is_still_never_folded():
    """A twin with genuinely different toolRounds is NOT payload-subsumed —
    both rows survive (the measured 64-group data-loss guard)."""
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1', finishReason='error',
              toolRounds=[{'status': 'done', 'toolContent': 'FIRST'}]),
        _asst('task-A', 'ANSWER', _msgId='a2', finishReason='error',
              error=copy.deepcopy(_ENV_365),
              toolRounds=[{'status': 'done', 'toolContent': 'SECOND'}]),
    ]
    out, changed = _rc(msgs)
    assert len(out) == 3, (
        'a payload-DIVERGENT pair was folded — that destroys real content; '
        'only payload-subsumed twins may fold')
    assert not changed


def test_fold_never_spans_a_user_turn():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _user(_msgId='u2'),
        _asst('task-A', '', _msgId='a2', finishReason='error',
              error=copy.deepcopy(_ENV_365)),
    ]
    out, changed = _rc(msgs)
    assert len(out) == 4, 'a fold across a user turn reshapes history'
    assert not changed


def test_fold_never_touches_endpoint_rows():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'PLAN', _msgId='p1', _isEndpointPlanner=True),
        _asst('task-A', '', _msgId='w1', _epIteration=1,
              finishReason='error', error=copy.deepcopy(_ENV_365)),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, 'endpoint planner/worker rows must never fold'


def test_fold_never_touches_cache_prefix_rows():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', '', _msgId='a2', finishReason='error',
              error=copy.deepcopy(_ENV_365)),
        _user(_msgId='u2'),
    ]
    out, changed = _rc(msgs, prefix=4)
    assert len(out) == 4, (
        'folding an in-prefix row shifts prefix bytes and busts the prompt '
        'cache')
    assert [m.get('_msgId') for m in out] == ['u1', 'a1', 'a2', 'u2'], (
        'the fold removed or reordered an in-prefix row — the surviving pair '
        'may still be marked by OTHER passes (e.g. the fragment mark, which '
        'is cache-neutral), but the fold itself must not touch the prefix')


def test_fold_is_idempotent():
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', '', _msgId='a2', finishReason='error',
              error=copy.deepcopy(_ENV_365)),
    ]
    once, _ = _rc(msgs)
    twice, changed2 = _rc(once)
    assert not changed2, 'the fold is not idempotent — it would rewrite forever'
    assert once == twice


def test_NC_neutered_fold_restores_the_duplicate(monkeypatch):
    """NEUTER: bypass the fold predicate and the terminal-divergent twin must
    come back — proving the fold is load-bearing for this pair class."""
    import lib.conversations.reconcile as rec
    monkeypatch.setattr(rec, 'fold_duplicate_task_twins',
                        lambda messages, cache_prefix_count=0: (messages, 0),
                        raising=True)
    msgs = [
        _user(_msgId='u1'),
        _asst('task-A', 'ANSWER', _msgId='a1'),
        _asst('task-A', '', _msgId='a2', finishReason='error',
              error=copy.deepcopy(_ENV_365)),
    ]
    out, _ = _rc(msgs)
    assert len(out) == 3, (
        'with the fold neutered the duplicate must reappear — otherwise this '
        'suite is not exercising the guard it claims to')


# ═══════════════════════════════════════════════════════════════════════
# B. terminal-sync level (DB-driven): error settles on the OWN slot and
#    the twin is folded away in the same settle write.
# ═══════════════════════════════════════════════════════════════════════

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/same_task_twin_fold_unittest.db')


def _seed_conv(conv_id, messages):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'twin-fold',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_messages(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row or not row[0]:
        return []
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _cleanup(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1',
                              (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?',
                              (conv_id,))
        db.commit()
    except Exception:
        pass


class TestTerminalSyncTwinFold(unittest.TestCase):
    """Drive the REAL settle path — create_task → persist_task_result →
    _sync_result_to_conversation — over a conversation already holding the
    frontend's pushed-back twin, exactly as conv mscns5i0fcofgl stood at
    11:40:29 on 2026-08-03."""

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-twinfold-' + str(id(self))
        _cleanup(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    def _settle(self):
        from lib.tasks_pkg.manager import create_task, persist_task_result

        rounds = [dict(r) for r in _ROUNDS]
        slot = {'role': 'assistant', 'content': 'PID 谜团解开了：`pid_max = 4194304`',
                'thinking': 'deep reasoning', 'toolRounds': [dict(r) for r in rounds],
                'model': 'kimi-k3', '_msgId': 'tmp_slot'}
        twin = {'role': 'assistant', 'content': '', 'thinking': '',
                'toolRounds': [dict(r) for r in rounds], 'model': 'kimi-k3',
                '_msgId': 'tmp_twin', '_taskId': None,  # filled below
                'finishReason': 'error', 'error': copy.deepcopy(_ENV_365),
                'timestamp': 1785728173357}
        _seed_conv(self.conv_id, [
            {'role': 'user', 'content': 'investigate', 'timestamp': 1},
            slot, twin])

        task = create_task(
            self.conv_id,
            [{'role': 'user', 'content': 'investigate'}],
            {'model': 'kimi-k3', 'projectEnabled': True},
        )
        twin['_taskId'] = task['id']           # the SSE bind stamped it
        # re-seed with the twin now carrying the task id (the 11:38:30 state)
        _seed_conv(self.conv_id, [
            {'role': 'user', 'content': 'investigate', 'timestamp': 1},
            slot, twin])
        task['_assistantMsgId'] = 'tmp_slot'   # the original placeholder id
        # Terminal state: the final attempt saturated with NO new prose.
        task['content'] = ''
        task['thinking'] = ''
        task['toolRounds'] = [dict(r) for r in rounds]
        task['error'] = copy.deepcopy(_ENV_364)
        task['finishReason'] = 'error'
        task['status'] = 'error'
        persist_task_result(task)
        return task

    def test_settle_writes_error_on_own_slot_and_folds_twin(self):
        """★ THE CONTRACT. After the settle the conversation holds exactly
        ONE row for the task — the content-bearing slot — and it carries the
        terminal error envelope. The frontend's twin is folded away in the
        same write (not on the next GET)."""
        task = self._settle()
        msgs = _read_messages(self.conv_id)
        asst = [m for m in msgs if m.get('role') == 'assistant']
        self.assertEqual(
            len(asst), 1,
            f'expected ONE assistant row for the task after settle, got '
            f'{[(m.get("_msgId"), bool(m.get("error"))) for m in asst]} — the '
            f'frontend twin was not folded')
        kept = asst[0]
        self.assertEqual(kept.get('_msgId'), 'tmp_slot',
                         'the content-bearing slot must be the survivor')
        self.assertEqual(kept.get('error'), _ENV_364,
                         'the terminal error envelope must settle on the '
                         "task's OWN slot even when the content guard "
                         'protects fuller existing content — a bare '
                         "finishReason='error' with no envelope is the other "
                         'half of the two-bubble bug')
        self.assertEqual(kept.get('finishReason'), 'error')
        self.assertEqual(kept.get('_taskId'), task['id'])
        self.assertEqual(kept.get('content'),
                         'PID 谜团解开了：`pid_max = 4194304`',
                         'the content guard must still protect the fuller body')

    def test_NC_neutered_fold_leaves_the_twin(self):
        """NEUTER: with the settle-time fold bypassed, the twin row survives
        the settle — proving the fold call in the terminal write is what
        converges the pair, not some pre-existing pass."""
        import lib.conversations.reconcile as rec
        import unittest.mock as _um

        with _um.patch.object(rec, 'fold_duplicate_task_twins',
                              lambda messages, cache_prefix_count=0: (messages, 0)):
            self._settle()
        msgs = _read_messages(self.conv_id)
        asst = [m for m in msgs if m.get('role') == 'assistant']
        self.assertEqual(
            len(asst), 2,
            'NEUTER expected the twin to survive once the settle-time fold '
            'is bypassed — the fold is not load-bearing')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_same_task_error_twin_fold.__main__', init_schema=False)
    unittest.main(verbosity=2)
