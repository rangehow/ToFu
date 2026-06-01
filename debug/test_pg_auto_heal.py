"""debug/test_pg_auto_heal.py — Smoke test for PG self-heal cooldown.

Verifies two invariants of ``lib.database._core._maybe_reboot_pg``:

  1. Two rapid calls within the cooldown window → only one bootstrap attempt.
  2. When backend is not PG, the function is a fast no-op.

We avoid fully mocking psycopg2 / pg_ctl (those require a real PG binary
and a writable pgdata) — instead we patch
``lib.database._bootstrap._ensure_pg_running`` with a counter spy.

Manual end-to-end reproduction of the outage fix:

    # 1. Start the server normally (PG auto-bootstraps).
    python server.py &
    # 2. Kill PG hard so the postmaster crashes.
    pg_ctl -D data/pgdata stop -m immediate
    # 3. Issue any API call that hits the DB, e.g.:
    curl http://localhost:8080/api/project/recent
    # 4. Within seconds, grep the log — you should see:
    #      [DB] PG appears dead (...) — attempting re-bootstrap once
    #      [DB] Retrying psycopg2.connect after PG re-bootstrap
    #    followed by a successful response.
"""

from __future__ import annotations

import os
import sys

# Import the module whose behavior we want to test. Using an absolute
# path so the script works whether run from project root or debug/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _force_backend_pg():
    """Flip the module-level backend flag to 'pg' regardless of runtime state."""
    from lib.database import _core
    _core._BACKEND = 'pg'
    return _core


def test_no_op_when_not_pg():
    from lib.database import _core
    saved = _core._BACKEND
    try:
        _core._BACKEND = 'sqlite'
        assert _core._maybe_reboot_pg('sentinel') is False, \
            'maybe_reboot_pg should be a no-op when backend != pg'
    finally:
        _core._BACKEND = saved
    print('✅ no-op-when-not-pg')


def test_cooldown_coalesces_calls():
    _core = _force_backend_pg()
    from lib.database import _bootstrap

    # Spy counters
    call_count = {'ensure': 0, 'owned': True}

    def _fake_ensure(*args, **kwargs):
        call_count['ensure'] += 1
        return {'PG_HOST': '127.0.0.1', 'PG_PORT': 15432,
                'PG_DSN': 'host=127.0.0.1 port=15432 dbname=chatui'}

    def _fake_is_owned():
        return call_count['owned']

    saved_ensure = _bootstrap._ensure_pg_running
    saved_owned = _bootstrap.is_pg_owned_locally
    saved_ts = _core._last_pg_reboot_attempt_ts
    saved_cooldown = _core._PG_REBOOT_COOLDOWN_S
    try:
        _bootstrap._ensure_pg_running = _fake_ensure
        _bootstrap.is_pg_owned_locally = _fake_is_owned
        # Reset cooldown state
        _core._last_pg_reboot_attempt_ts = 0.0
        _core._PG_REBOOT_COOLDOWN_S = 60

        r1 = _core._maybe_reboot_pg('Connection refused @ call1')
        r2 = _core._maybe_reboot_pg('Connection refused @ call2')

        assert r1 is True, 'first call should have attempted a reboot'
        assert r2 is False, 'second call should be suppressed by cooldown'
        assert call_count['ensure'] == 1, (
            f'expected exactly 1 bootstrap call, got {call_count["ensure"]}')
    finally:
        _bootstrap._ensure_pg_running = saved_ensure
        _bootstrap.is_pg_owned_locally = saved_owned
        _core._last_pg_reboot_attempt_ts = saved_ts
        _core._PG_REBOOT_COOLDOWN_S = saved_cooldown
    print('✅ cooldown-coalesces-calls')


def test_skipped_when_not_owned():
    _core = _force_backend_pg()
    from lib.database import _bootstrap

    call_count = {'ensure': 0}

    def _fake_ensure(*args, **kwargs):
        call_count['ensure'] += 1
        return None

    def _fake_is_owned():
        return False  # simulate attached-to-remote-PG case

    saved_ensure = _bootstrap._ensure_pg_running
    saved_owned = _bootstrap.is_pg_owned_locally
    saved_ts = _core._last_pg_reboot_attempt_ts
    try:
        _bootstrap._ensure_pg_running = _fake_ensure
        _bootstrap.is_pg_owned_locally = _fake_is_owned
        _core._last_pg_reboot_attempt_ts = 0.0

        r = _core._maybe_reboot_pg('Connection refused @ remote')
        assert r is False, 'should skip when we do not own PG'
        assert call_count['ensure'] == 0, 'must not call _ensure_pg_running'
    finally:
        _bootstrap._ensure_pg_running = saved_ensure
        _bootstrap.is_pg_owned_locally = saved_owned
        _core._last_pg_reboot_attempt_ts = saved_ts
    print('✅ skipped-when-not-owned')


def test_timer_str_args_coerced():
    """Regression for the 2026-04-25 TypeError on max(poll_interval, 10)."""
    from lib.scheduler import timer as _tmr

    # Stub out DB persistence inside create_timer — we only care that
    # the coercion + max() logic no longer raises TypeError.
    call_log = []

    class _FakeDB:
        def execute(self, sql, params):
            call_log.append(('execute', sql[:40], params))

        def commit(self):
            call_log.append(('commit',))

    def _fake_get_thread_db(domain):
        return _FakeDB()

    def _fake_get_row(timer_id):
        return {
            'id': timer_id, 'conv_id': 'c', 'source_task_id': '',
            'check_instruction': 'x', 'check_command': '',
            'continuation_message': 'y',
            'poll_interval': 60, 'max_polls': 120, 'status': 'active',
            'tools_config': '{}', 'created_at': '', 'updated_at': '',
            'poll_count': 0,
        }

    # Patch DB + row fetcher
    from lib import database as _db_pkg
    saved_get = getattr(_db_pkg, 'get_thread_db', None)
    _db_pkg.get_thread_db = _fake_get_thread_db
    saved_row = _tmr._get_timer_row
    _tmr._get_timer_row = _fake_get_row
    try:
        # This used to raise `TypeError: '>' not supported between
        # instances of 'int' and 'str'` at line 73.
        rec = _tmr.create_timer(
            conv_id='c',
            check_instruction='x',
            continuation_message='y',
            poll_interval='60',      # ← string!
            max_polls='100',         # ← string!
        )
        assert rec is not None
    finally:
        _tmr._get_timer_row = saved_row
        if saved_get is not None:
            _db_pkg.get_thread_db = saved_get

    print('✅ timer-str-args-coerced')


if __name__ == '__main__':
    # Order matters: run the isolated unit tests first, then the
    # Timer integration test which pulls more of the app.
    test_no_op_when_not_pg()
    test_cooldown_coalesces_calls()
    test_skipped_when_not_owned()
    test_timer_str_args_coerced()
    print('\nALL OK')
