"""debug/test_key_stats_override_persistence.py

Regression test for the "manually-disabled API key re-enables on restart/
day rollover" bug.

Before the fix, `lib/key_stats.py::_load_unlocked` and `_ensure_fresh_unlocked`
wiped ``_cache['overrides']`` on day rollover, so a key the user manually
toggled off yesterday would silently come back online today.

After the fix, overrides PERSIST across both:
  - in-memory day rollovers (`_ensure_fresh_unlocked` boundary)
  - process restarts (re-reading the on-disk JSON after a day change)

Stats (success/failure/429 counters and the ``exhausted`` flag) still reset
daily — this test asserts both invariants hold.

Run: ``python3 debug/test_key_stats_override_persistence.py`` (exits 0 OK).
"""

import os
import sys
import tempfile
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.key_stats as ks  # noqa: E402


_TMP_DIR = tempfile.mkdtemp(prefix='keystats-persist-')
ks._STATS_PATH = os.path.join(_TMP_DIR, 'key_stats.json')


def _assert(cond, msg):
    if not cond:
        print('FAIL:', msg)
        sys.exit(1)
    print('  PASS:', msg)


def _reset_all():
    """Wipe in-memory state + on-disk file, start from a clean slate."""
    with ks._lock:
        ks._cache['day'] = ''
        ks._cache['stats'] = {}
        ks._cache['overrides'] = {}
        ks._cache['loaded'] = False
    ks._last_resort_logged.clear()
    if os.path.isfile(ks._STATS_PATH):
        os.remove(ks._STATS_PATH)


def _patch_siblings(mapping: dict):
    def fake(provider_id):
        names = mapping.get(provider_id or 'default', [])
        return [ks._pair_key(provider_id or 'default', n) for n in names]
    ks._list_siblings = fake


# ══════════════════════════════════════════════════════════════
#  Scenario 1: in-memory day rollover preserves overrides
# ══════════════════════════════════════════════════════════════
def scenario_in_memory_rollover():
    print('\n[1] in-memory day rollover preserves overrides')
    _reset_all()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})

    # User disables key_0 today.
    ks.set_key_override('p1', 'p1_key_0', False)
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is False,
            'key_0 disabled after manual override')

    # Also accumulate some stats so we can verify they DO reset.
    ks.record_outcome('p1', 'p1_key_1', success=True)
    ks.record_outcome('p1', 'p1_key_1', success=False, error='oops')
    _assert(ks._cache['stats'].get('p1::p1_key_1', {}).get('success') == 1,
            'stats accumulated for key_1')

    # Simulate calendar day rollover WITHOUT restart: monkey-patch _today.
    original_today = ks._today
    ks._today = lambda: '2099-12-31'   # far-future day string
    try:
        # Any read triggers _ensure_fresh_unlocked → rollover path.
        still_disabled = ks.is_key_enabled('p1', 'p1_key_0')
        _assert(still_disabled is False,
                'key_0 STILL disabled after in-memory day rollover')
        # Stats must have been cleared.
        row1 = ks.get_today_stats('p1', 'p1_key_1')
        _assert(row1['success'] == 0 and row1['failure'] == 0,
                'key_1 stats reset on rollover')
        # Override row reflects the persisted manual=False.
        row0 = ks.get_today_stats('p1', 'p1_key_0')
        _assert(row0['override'] is False,
                'row0 override still False after rollover')
        _assert(row0['enabled'] is False,
                'row0 enabled=False after rollover')
    finally:
        ks._today = original_today


# ══════════════════════════════════════════════════════════════
#  Scenario 2: process restart on a NEW day preserves overrides
# ══════════════════════════════════════════════════════════════
def scenario_restart_new_day():
    print('\n[2] process restart on a new day preserves overrides')
    _reset_all()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})

    # Day 1: user disables key_0 and records some stats.
    ks.set_key_override('p1', 'p1_key_0', False)
    ks.record_outcome('p1', 'p1_key_1', success=True)
    ks.record_outcome('p1', 'p1_key_1', success=False, error='err')
    _assert(os.path.isfile(ks._STATS_PATH),
            'key_stats.json persisted to disk')

    # Write a known-old day into the file to simulate "yesterday's state"
    # without waiting for the clock.  We keep overrides and stats as-is.
    import json
    with open(ks._STATS_PATH, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    payload['day'] = '2000-01-01'        # force any future run to "rollover"
    payload['stats']['p1::p1_key_1']['success'] = 99
    with open(ks._STATS_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f)

    # Simulate restart: clear the in-memory cache and re-run the eager load.
    with ks._lock:
        ks._cache['day'] = ''
        ks._cache['stats'] = {}
        ks._cache['overrides'] = {}
        ks._cache['loaded'] = False
    ks._last_resort_logged.clear()

    # This is what import-time code runs:
    with ks._lock:
        ks._load_unlocked()

    # Override must be preserved.
    _assert(ks._cache['overrides'].get('p1::p1_key_0') is False,
            'manual override=False survived restart on new day')
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is False,
            'key_0 still disabled after restart on new day')

    # Stats must have reset.
    row1 = ks.get_today_stats('p1', 'p1_key_1')
    _assert(row1['success'] == 0 and row1['failure'] == 0,
            'stats for key_1 reset after rollover (99 → 0)')

    # The persisted file must now carry today's day string and the
    # preserved override.
    with open(ks._STATS_PATH, 'r', encoding='utf-8') as f:
        reread = json.load(f)
    _assert(reread['day'] == ks._today(),
            "on-disk day advanced to today")
    _assert(reread['overrides'].get('p1::p1_key_0') is False,
            'on-disk override still False after rollover save')


# ══════════════════════════════════════════════════════════════
#  Scenario 3: clear_key_override DOES remove the persistent entry
# ══════════════════════════════════════════════════════════════
def scenario_clear_override():
    print('\n[3] clear_key_override removes persistent entry')
    _reset_all()
    _patch_siblings({'p1': ['p1_key_0']})
    ks.set_key_override('p1', 'p1_key_0', False)
    _assert(ks._cache['overrides'].get('p1::p1_key_0') is False,
            'override=False set')
    ks.clear_key_override('p1', 'p1_key_0')
    _assert('p1::p1_key_0' not in ks._cache['overrides'],
            'override cleared from cache')
    # And from disk.
    import json
    with open(ks._STATS_PATH, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    _assert('p1::p1_key_0' not in (payload.get('overrides') or {}),
            'override cleared from disk too')


# ══════════════════════════════════════════════════════════════
#  Scenario 4: exhausted flag resets on rollover, override does not
# ══════════════════════════════════════════════════════════════
def scenario_exhausted_resets_override_persists():
    print('\n[4] exhausted flag resets on rollover, override persists')
    _reset_all()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})

    # Manually disable key_0 AND have key_1 get exhausted.
    ks.set_key_override('p1', 'p1_key_0', False)
    ks.mark_key_exhausted('p1', 'p1_key_1', reason='402')
    _assert(ks._cache['stats']['p1::p1_key_1']['exhausted'] is True,
            'key_1 exhausted flag set')

    # Rollover in-memory.
    original_today = ks._today
    ks._today = lambda: '2099-06-15'
    try:
        # Trigger rollover via a read.
        ks.is_key_enabled('p1', 'p1_key_0')
        row0 = ks.get_today_stats('p1', 'p1_key_0')
        row1 = ks.get_today_stats('p1', 'p1_key_1')
        _assert(row0['override'] is False,
                'override=False preserved after rollover')
        _assert(row1['exhausted'] is False,
                'exhausted flag cleared after rollover (stats reset)')
    finally:
        ks._today = original_today


def main():
    try:
        scenario_in_memory_rollover()
        scenario_restart_new_day()
        scenario_clear_override()
        scenario_exhausted_resets_override_persists()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('UNEXPECTED ERROR:', e)
        sys.exit(2)
    print('\nAll override-persistence scenarios passed ✅')
    sys.exit(0)


if __name__ == '__main__':
    main()
